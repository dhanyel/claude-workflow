import json, os, subprocess, unittest
from unittest import mock
from .helpers import run, temp_state, fake_repo, import_bin

timeline = import_bin("timeline")


def _events(repo, env):
    with mock.patch.dict(os.environ, env):
        return timeline.events(repo, timeline.demand(repo))


class TestPhase(unittest.TestCase):
    def test_start_without_remote_stamps_and_shows_dashes(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, err = run("phase.py", "start", cwd=repo, env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn("—", out)
            with mock.patch.dict(os.environ, env):
                self.assertEqual([e["event"] for e in timeline.events(repo, timeline.demand(repo))], ["start"])

    def test_named_phase_is_stamped(self):
        with temp_state() as env, fake_repo() as repo:
            run("phase.py", "review", cwd=repo, env=env)
            with mock.patch.dict(os.environ, env):
                self.assertEqual([e["event"] for e in timeline.events(repo, timeline.demand(repo))], ["review"])

    def test_report_without_events_is_all_dashes(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, _ = run("phase.py", "report", cwd=repo, env=env)
            self.assertEqual(rc, 0)
            self.assertIn("—", out)

    def test_start_with_issue_records_it_and_source_phase(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, err = run("phase.py", "start", "#58", cwd=repo, env=env)
            self.assertEqual(rc, 0, err)
            ev = _events(repo, env)
            self.assertEqual(ev[0]["detail"], {"issue": "58"})
            self.assertEqual(ev[0]["source"], "phase")
            self.assertIn("58", out)

    def test_start_uses_branch_number_without_argument(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "feat/77-x"], check=True)
            run("phase.py", "start", cwd=repo, env=env)
            self.assertEqual(_events(repo, env)[0]["detail"], {"issue": "77"})

    def test_start_shows_commits_ahead_of_the_base(self):
        import tempfile
        with temp_state() as env, fake_repo() as repo, tempfile.TemporaryDirectory() as d:
            # B-I5: the base is fetched first, so it needs a real (local, bare) origin
            bare = os.path.join(d, "origin.git")
            subprocess.run(["git", "clone", "-q", "--bare", repo, bare], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
            subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("base: origin/main", out.splitlines())
            self.assertIn("commits ahead of the base: 1", out.splitlines())

    def test_start_without_remote_shows_dashes_on_base_and_ahead(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("base: —", out.splitlines())
            self.assertIn("commits ahead of the base: —", out.splitlines())

    def test_invalid_name_is_rc2_and_writes_nothing(self):
        with temp_state() as env, fake_repo() as repo:
            for bad in ("Bad", "1x", "a-b", "../x"):
                rc, out, err = run("phase.py", bad, cwd=repo, env=env)
                self.assertEqual(rc, 2, bad)
                self.assertTrue(err.strip())
            self.assertEqual(_events(repo, env), [])

    def test_start_is_only_valid_through_the_subcommand(self):
        with temp_state() as env, fake_repo() as repo:
            rc, _, _ = run("phase.py", "start", "x", cwd=repo, env=env)
            self.assertEqual(rc, 2)
            self.assertEqual(_events(repo, env), [])

    def test_start_outside_a_repo_is_rc0_and_writes_nothing(self):
        import tempfile
        with temp_state() as env, tempfile.TemporaryDirectory() as d:
            rc, out, err = run("phase.py", "start", cwd=os.path.realpath(d), env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn("—", out)
            self.assertFalse(os.path.exists(os.path.join(env["PLAN_GATE_DIR"], "timeline")))

    def test_report_after_a_full_run_has_a_total(self):
        with temp_state() as env, fake_repo() as repo:
            with mock.patch.dict(os.environ, env):
                for ev, t in (("start", "2026-10-03T09:00:00-03:00"), ("plan_written", "2026-10-03T10:00:00-03:00"),
                              ("delivered", "2026-10-03T12:00:00-03:00")):
                    timeline.append(repo, ev, "test", now=t)
            rc, out, err = run("phase.py", "report", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            total = [l for l in out.splitlines() if "Total" in l][0]
            self.assertIn("3h 0m", total)
            self.assertNotEqual(total.count("—"), 5)


TOKENS = ("GITHUB_TOKEN", "GITLAB_TOKEN")


def _no_tokens(env):
    return {k: v for k, v in env.items() if k not in TOKENS}


class TestPhaseOpenRequest(unittest.TestCase):
    """R56: `start` shows the open MR/PR of the branch, consulted on the remote -- or why it was not."""
    def test_without_remote_it_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(_no_tokens(env), PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("open MR/PR: not consulted (no remote)", out.splitlines())

    def test_without_token_it_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(_no_tokens(env), PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("open MR/PR: not consulted (no token)", out.splitlines())
            self.assertEqual([e["event"] for e in _events(repo, env)], ["start"])

    def start_in_process(self, env, repo, lookup, **extra_env):
        import contextlib, io
        phase = import_bin("phase")
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(dict(env, GITLAB_TOKEN="tok-abcdef", GITLAB_HOST="gitlab.example",
                                                   GH_HOST="", PLAN_GATE_LANG="en"), **extra_env)), \
             mock.patch.object(phase.delivery, "open_request_for", lookup) as patched, \
             contextlib.redirect_stdout(out):
            rc = phase.start(repo, [])
        return rc, out.getvalue(), patched

    def test_found_request_is_shown_with_a_10s_timeout(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            lookup = mock.Mock(return_value={"iid": 9, "web_url": "https://gitlab.example/g/p/-/merge_requests/9"})
            rc, out, patched = self.start_in_process(env, repo, lookup)
            self.assertEqual(rc, 0)
            self.assertIn("open MR/PR: !9 https://gitlab.example/g/p/-/merge_requests/9", out.splitlines())
            args, kwargs = patched.call_args
            self.assertEqual(args[:3], ("gitlab", "g/p", "main"))
            self.assertEqual(kwargs["timeout"], 10)

    def test_none_open_is_said(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            rc, out, _ = self.start_in_process(env, repo, mock.Mock(return_value=None))
            self.assertIn("open MR/PR: none open", out.splitlines())

    def test_any_failure_is_not_consulted_with_the_reason(self):
        delivery = import_bin("phase").delivery
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            for exc, reason in ((delivery.RequestFailed("503"), "503"), (TimeoutError("x"), "TimeoutError")):
                rc, out, _ = self.start_in_process(env, repo, mock.Mock(side_effect=exc))
                self.assertEqual(rc, 0)
                self.assertIn(f"open MR/PR: not consulted ({reason})", out.splitlines())


    def test_r61_token_not_bound_to_the_origin_host(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@bitbucket.org:o/r.git"], check=True)
            lookup = mock.Mock(return_value=None)
            rc, out, _ = self.start_in_process(env, repo, lookup)
            self.assertEqual(rc, 0)
            self.assertIn("open MR/PR: not consulted (token not bound to bitbucket.org)", out.splitlines())
            lookup.assert_not_called()

    def test_r67_repo_api_url_on_a_foreign_host_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            os.makedirs(os.path.join(repo, ".claude"))
            for cfg in ({"api_url": "https://evil.example/api/v4"},
                        {"platform": "github", "api_url": "https://evil.example/api/v3"}):
                with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                    json.dump(cfg, f)
                lookup = mock.Mock(return_value=None)
                rc, out, _ = self.start_in_process(env, repo, lookup, GITHUB_TOKEN="tok-gh")
                self.assertIn("open MR/PR: not consulted (token not bound to evil.example)", out.splitlines())
                lookup.assert_not_called()

    def test_r70_repo_api_url_on_the_origin_host_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@code.example.org:g/p.git"], check=True)
            os.makedirs(os.path.join(repo, ".claude"))
            for cfg in ({"api_url": "https://code.example.org/api/v4"},
                        {"platform": "github", "api_url": "https://code.example.org/api/v3"}):
                with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                    json.dump(cfg, f)
                lookup = mock.Mock(return_value=None)
                rc, out, _ = self.start_in_process(env, repo, lookup, GITLAB_HOST="", GITHUB_TOKEN="tok-gh")
                self.assertIn("open MR/PR: not consulted (token not bound to code.example.org)", out.splitlines())
                lookup.assert_not_called()

    def test_r78_a_hostile_origin_with_a_trusted_api_url_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                            "git@evil.example:victim-group/secret-proj.git"], check=True)
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"platform": "gitlab", "api_url": "https://gitlab.com/api/v4"}, f)
            lookup = mock.Mock(return_value=None)
            rc, out, _ = self.start_in_process(env, repo, lookup)
            self.assertIn("open MR/PR: not consulted (token not bound to evil.example)", out.splitlines())
            lookup.assert_not_called()

    def test_r70_gh_host_lets_phase_consult_ghe(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@ghe.example.org:o/r.git"], check=True)
            lookup = mock.Mock(return_value={"number": 4, "html_url": "https://ghe.example.org/o/r/pull/4"})
            rc, out, patched = self.start_in_process(env, repo, lookup, GH_HOST="ghe.example.org", GITHUB_TOKEN="t")
            self.assertIn("open MR/PR: #4 https://ghe.example.org/o/r/pull/4", out.splitlines())
            self.assertEqual(patched.call_args.kwargs["base_url"], "https://ghe.example.org/api/v3")

    def test_r68_ambiguous_remote_host_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@evil.com@gitlab.com:g/p.git"],
                           check=True)
            lookup = mock.Mock(return_value=None)
            rc, out, _ = self.start_in_process(env, repo, lookup, GITLAB_HOST="gitlab.com")
            self.assertIn("open MR/PR: not consulted (unrecognised-remote)", out.splitlines())
            lookup.assert_not_called()

    def write_cfg(self, repo, cfg):
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))

    def test_r71_a_repo_http_api_url_is_not_consulted(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.com:g/p.git"], check=True)
            for cfg, extra in (({"api_url": "http://gitlab.com/api/v4"}, {"GITLAB_HOST": ""}),
                               ({"api_url": "http://gitlab.com:80/api/v4"}, {"GITLAB_HOST": ""}),
                               ({"api_url": "http://gitlab.com/api/v4"}, {"GITLAB_HOST": "https://gitlab.com"})):
                with self.subTest(cfg=cfg, env=extra):
                    self.write_cfg(repo, cfg)
                    lookup = mock.Mock(return_value=None)
                    rc, out, _ = self.start_in_process(env, repo, lookup, **extra)
                    self.assertIn("open MR/PR: not consulted (token not bound to gitlab.com)", out.splitlines())
                    lookup.assert_not_called()

    def test_r71_an_http_env_host_is_consulted_over_http(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            lookup = mock.Mock(return_value=None)
            rc, out, patched = self.start_in_process(env, repo, lookup, GITLAB_HOST="http://gitlab.example")
            self.assertIn("open MR/PR: none open", out.splitlines())
            self.assertEqual(patched.call_args.kwargs["base_url"], "http://gitlab.example/api/v4")

    def test_r72_a_broken_config_never_raises(self):
        """R72: `start` is stamped before the lookup; a config that cannot be read must still give a line."""
        cases = ({"api_url": "https://user:hunter2@gitlab.com/api/v4"}, {"api_url": "https://gitlab.com/api/v4#x"},
                 '{"api_url": ', {"language": "pt-BR", "plans_dir": "/abs"})
        # the subprocess never sees PLAN_GATE_LANG (helpers strips it): the fallback is en. pt-BR: the test below
        for cfg in cases:
            with self.subTest(cfg=cfg), temp_state() as env, fake_repo() as repo:
                subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.com:g/p.git"], check=True)
                self.write_cfg(repo, cfg)
                env = dict(env, GITLAB_TOKEN="tok-abcdef")
                rc, out, err = run("phase.py", "start", cwd=repo, env=env)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
                hits = [l for l in out.splitlines() if l.startswith("open MR/PR: not consulted (config: ")]
                self.assertEqual(len(hits), 1, out)
                self.assertTrue(hits[0].endswith(")"), hits[0])
                self.assertNotIn(".claude/plan-gate.json:", hits[0])
                self.assertNotIn(repo, hits[0])               # the reason is short: no absolute path
                self.assertNotIn("hunter2", out + err)
                self.assertNotIn("tok-abcdef", out + err)
                self.assertEqual([e["event"] for e in _events(repo, env)], ["start"])

    def test_r72_in_process_a_broken_config_falls_back_to_the_environment_language(self):
        phase = import_bin("phase")
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.com:g/p.git"], check=True)
            self.write_cfg(repo, {"api_url": "https://gitlab.com/api/v4#x"})
            lookup = mock.Mock(return_value=None)
            rc, out, _ = self.start_in_process(env, repo, lookup, PLAN_GATE_LANG="pt-BR")
            self.assertEqual(rc, 0)
            self.assertIn("MR/PR aberto: não consultado (config: Invalid 'api_url': must not have a query or a "
                          "fragment)", out.splitlines())
            lookup.assert_not_called()

    def test_r65_the_lookup_has_a_total_deadline(self):
        import time
        phase = import_bin("phase")
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            slow = mock.Mock(side_effect=lambda *a, **k: time.sleep(2))
            with mock.patch.object(phase.delivery, "LOOKUP_TIMEOUT", 0.3), \
                 mock.patch.dict(os.environ, dict(env, GITLAB_TOKEN="tok-abcdef", GITLAB_HOST="gitlab.example",
                                                  PLAN_GATE_LANG="en")), \
                 mock.patch.object(phase.delivery, "open_request_for", slow):
                began = time.monotonic()
                line = phase.open_request(repo, "main")
                elapsed = time.monotonic() - began
            self.assertEqual(line, "not consulted (timeout)")
            self.assertLess(elapsed, 1.5)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class TestPhaseFinalReview(unittest.TestCase):
    """B-I4/B-I5 (R81), B-M2, L255."""

    def test_start_with_branch_stamps_that_demand(self):
        with temp_state() as env, fake_repo() as repo:
            rc, out, err = run("phase.py", "start", "--branch", "feat/90-new", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("branch: feat/90-new", out.splitlines())
            self.assertIn("issue: 90", out.splitlines())
            with mock.patch.dict(os.environ, env):
                self.assertEqual([(e["event"], e["detail"]) for e in timeline.events(repo, "feat/90-new")],
                                 [("start", {"issue": "90"})])
                self.assertEqual(timeline.events(repo, "main"), [])
            rc, out, err = run("phase.py", "start", "#12", "--branch=feat/x", cwd=repo, env=env)
            self.assertEqual(rc, 0, err)
            with mock.patch.dict(os.environ, env):
                self.assertEqual(timeline.events(repo, "feat/x")[0]["detail"], {"issue": "12"})

    def test_a_bad_branch_argument_is_rc2_and_writes_nothing(self):
        with temp_state() as env, fake_repo() as repo:
            for argv in (["--branch"], ["--branch", "a..b"], ["--branch", "x", "--branch", "y"], ["--branch", ""]):
                with self.subTest(argv=argv):
                    rc, out, err = run("phase.py", "start", *argv, cwd=repo, env=env)
                    self.assertEqual(rc, 2, out + err)
            self.assertFalse(os.path.exists(os.path.join(env["PLAN_GATE_DIR"], "timeline")))

    def test_start_fetches_the_base_from_a_local_bare_remote(self):
        import tempfile
        with temp_state() as env, fake_repo() as repo, tempfile.TemporaryDirectory() as d:
            bare = os.path.join(d, "origin.git")
            subprocess.run(["git", "clone", "-q", "--bare", repo, bare], check=True, capture_output=True)
            _git(repo, "remote", "add", "origin", bare)
            _git(repo, "fetch", "-q", "origin")
            _git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            # the remote's main moves to include the local commit; the local origin/main is now stale
            subprocess.run(["git", "--git-dir", bare, "fetch", "-q", repo, "main:main"], check=True, capture_output=True)
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            self.assertIn("commits ahead of the base: 0", out.splitlines())    # stale ref would say 1

    def test_a_failed_fetch_says_so_instead_of_counting_a_stale_base(self):
        with temp_state() as env, fake_repo() as repo:
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")     # a tracking ref, no remote
            _git(repo, "commit", "-q", "--allow-empty", "-m", "x")
            rc, out, err = run("phase.py", "start", cwd=repo, env=dict(env, PLAN_GATE_LANG="en"))
            self.assertEqual(rc, 0, err)
            line = [l for l in out.splitlines() if l.startswith("commits ahead of the base: ")][0]
            self.assertTrue(line.startswith("commits ahead of the base: — (not fetched: "), line)

    def test_the_fetch_never_prompts_and_has_a_deadline(self):
        phase = import_bin("phase")
        with temp_state() as env, fake_repo() as repo:
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            calls = []
            real = phase.subprocess.run

            def spy(cmd, *a, **kw):
                if cmd[:1] == ["git"] and "fetch" in cmd:
                    calls.append((cmd, kw))
                    return subprocess.CompletedProcess(cmd, 128, "", "fatal: could not read from remote\n")
                return real(cmd, *a, **kw)
            with mock.patch.dict(os.environ, dict(env, PLAN_GATE_LANG="en")), \
                    mock.patch.object(phase.subprocess, "run", side_effect=spy):
                text = phase.fetch_base(repo, "origin/main", deadline=phase.time.monotonic() + 10)
            self.assertEqual(len(calls), 1)
            cmd, kw = calls[0]
            self.assertEqual(cmd[-4:], ["fetch", "-q", "origin", "main"])
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kw["env"]["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
            self.assertLessEqual(kw["timeout"], 10)
            self.assertIn("could not read from remote", text)

    def test_hook_owned_names_are_refused(self):
        with temp_state() as env, fake_repo() as repo:
            for name in ("released", "approved", "spec_released", "spec_approved", "first_commit", "commit", "push",
                         "plan_written", "spec_written", "prompt", "stop"):
                with self.subTest(name=name):
                    rc, out, err = run("phase.py", name, cwd=repo, env=env)
                    self.assertEqual(rc, 2, out + err)
                    self.assertTrue(err.strip())
            self.assertEqual(_events(repo, env), [])
            self.assertEqual(run("phase.py", "delivered", cwd=repo, env=env)[0], 0)   # `start` stays the subcommand

    def test_name_and_report_never_traceback_on_a_broken_config(self):
        for cfg in ('{"api_url": ', '{"plans_dir": "/abs"}'):
            with self.subTest(cfg=cfg), temp_state() as env, fake_repo() as repo:
                os.makedirs(os.path.join(repo, ".claude"))
                with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                    f.write(cfg)
                rc, out, err = run("phase.py", "review", cwd=repo, env=env)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
                self.assertIn("review", out)
                self.assertEqual([e["event"] for e in _events(repo, env)], ["review"])
                rc, out, err = run("phase.py", "report", cwd=repo, env=env)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
                self.assertIn("Review", out)


if __name__ == "__main__":
    unittest.main()
