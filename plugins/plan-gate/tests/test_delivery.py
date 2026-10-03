import ast, contextlib, io, json, os, subprocess, tempfile, unittest
from unittest import mock
from .helpers import BIN, import_bin, fake_repo, temp_state, run

delivery = import_bin("delivery")

TOKENS = ("GITHUB_TOKEN", "GITLAB_TOKEN")


def no_tokens(env):
    return {k: v for k, v in env.items() if k not in TOKENS}


class TestDelivery(unittest.TestCase):
    def test_platform_from_remote(self):
        self.assertEqual(delivery.platform("git@github.com:a/b.git"), "github")
        self.assertEqual(delivery.platform("https://github.com/a/b"), "github")
        self.assertEqual(delivery.platform("git@git.example.org:x/y.git"), "gitlab")

    def test_closes_only_targeting_the_default_branch(self):
        self.assertEqual(delivery.closes_line("58", "main", "main"), "Closes #58")
        self.assertIsNone(delivery.closes_line("58", "dev", "main"))

    def test_render_has_every_section_in_both_languages(self):
        for lang, heads in {"en": ["## Summary", "## Verification", "## ⏱ Development time"],
                            "pt-BR": ["## Resumo", "## Verificação", "## ⏱ Tempo de desenvolvimento"]}.items():
            md = delivery.render("s", "c", ["cmd"], ["x"], "|t|", "Closes #1", lang)
            for h in heads:
                self.assertIn(h, md)

    def test_not_run_is_declared(self):
        md = delivery.render("s", "c", ["unittest"], ["cargo doc"], "|t|", None, "en")
        self.assertIn("cargo doc", md.split("## Verification")[1])

    def test_publish_never_prints_the_token(self):
        calls = []
        def transport(method, url, headers, body):
            calls.append(headers)
            return 201, {"iid": 7, "web_url": "https://example/mr/7"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "SECRET-TOKEN"}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            delivery.publish("gitlab", "g/p", "feat/1-x", "main", "t", "b", transport=transport,
                             base_url="https://gitlab.example")
        self.assertNotIn("SECRET-TOKEN", out.getvalue() + err.getvalue())
        self.assertEqual(calls[0]["PRIVATE-TOKEN"], "SECRET-TOKEN")

    def test_verify_fails_when_description_came_back_short(self):
        short = lambda m, u, h, b: (200, {"description": "x"})
        full = lambda m, u, h, b: (200, {"description": "x" * 500})
        self.assertFalse(delivery.verify("gitlab", "g/p", 7, 500, transport=short, base_url="https://gitlab.example"))
        self.assertTrue(delivery.verify("gitlab", "g/p", 7, 500, transport=full, base_url="https://gitlab.example"))

    def test_comment_issue_posts_to_the_issue_notes(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url))
            return 201, {"id": 1}
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "t"}):
            delivery.comment_issue("gitlab", "g/p", "58", "|t|", transport=transport, base_url="https://gitlab.example")
        self.assertEqual(seen[0][0], "POST")
        self.assertTrue(seen[0][1].endswith("/projects/g%2Fp/issues/58/notes"))

    def test_open_request_for_branch(self):
        found = lambda m, u, h, b: (200, [{"iid": 9, "web_url": "https://example/mr/9"}])
        none = lambda m, u, h, b: (200, [])
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "t"}):
            self.assertEqual(delivery.open_request_for("gitlab", "g/p", "feat/1-x", transport=found,
                                                       base_url="https://gitlab.example")["iid"], 9)
            self.assertIsNone(delivery.open_request_for("gitlab", "g/p", "feat/1-x", transport=none,
                                                        base_url="https://gitlab.example"))


GL = "https://gitlab.example/api/v4"


def answer(status, body=None):
    return lambda m, u, h, b: (status, body)


def raising(exc):
    def transport(m, u, h, b):
        raise exc
    return transport


class TestOutcome(unittest.TestCase):
    """R53: classify by STATUS. Once the POST left, a 5xx or no answer is `unknown` -- the MR may exist."""
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GITLAB_TOKEN": "tok-123456", "GITHUB_TOKEN": "gh-123456"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, transport, platform="gitlab"):
        base = GL if platform == "gitlab" else None
        return delivery.publish(platform, "g/p", "feat/1-x", "main", "t", "b", transport=transport, base_url=base)

    def test_2xx_is_ok_with_status_and_body(self):
        r = self.publish(answer(201, {"iid": 7}))
        self.assertEqual((r["outcome"], r["status"], r["body"]), ("ok", 201, {"iid": 7}))

    def test_4xx_is_refused(self):
        for status in (400, 403, 409, 422):
            self.assertEqual(self.publish(answer(status, {"message": "no"}))["outcome"], "refused", status)

    def test_429_is_retryable(self):
        self.assertEqual(self.publish(answer(429))["outcome"], "retryable")

    def test_5xx_is_unknown_and_points_to_the_lookup(self):
        for status in (500, 502, 504):
            r = self.publish(answer(status))
            self.assertEqual((r["outcome"], r["status"]), ("unknown", status))
            self.assertEqual(r["next"], "open-request")

    def test_no_answer_after_sending_is_unknown(self):
        import socket, urllib.error
        for exc in (TimeoutError("timed out"), socket.timeout("timed out"), ConnectionResetError(),
                    urllib.error.URLError("closed"), RuntimeError("boom")):
            r = self.publish(raising(exc))
            self.assertEqual(r["outcome"], "unknown", exc)
            self.assertIsNone(r["status"])
            self.assertEqual(r["next"], "open-request")

    def test_failure_before_sending_is_error(self):
        r = self.publish(raising(delivery.NotSent("gaierror")))
        self.assertEqual(r["outcome"], "error")
        self.assertNotEqual(r.get("next"), "open-request")

    def test_status_outside_the_http_range_is_error(self):
        for status in (600, 99, 0, "201", None):
            self.assertEqual(self.publish(answer(status))["outcome"], "error", status)

    def test_no_token_is_error_and_nothing_is_sent(self):
        calls = []
        def transport(*a):
            calls.append(a)
            return 201, {}
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": ""}):
            r = self.publish(transport)
        self.assertEqual(r["outcome"], "error")
        self.assertEqual(calls, [])

    def test_comment_is_classified_the_same_way(self):
        for status, outcome in ((201, "ok"), (422, "refused"), (429, "retryable"), (502, "unknown"), (600, "error")):
            r = delivery.comment_issue("gitlab", "g/p", "58", "x", transport=answer(status), base_url=GL)
            self.assertEqual(r["outcome"], outcome, status)
        r = delivery.comment_issue("gitlab", "g/p", "58", "x", transport=raising(TimeoutError()), base_url=GL)
        self.assertEqual(r["outcome"], "unknown")

    def test_github_publish_payload_and_headers(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url, headers, body))
            return 201, {"number": 3}
        self.assertEqual(self.publish(transport, platform="github")["outcome"], "ok")
        method, url, headers, body = seen[0]
        self.assertEqual((method, url), ("POST", "https://api.github.com/repos/g/p/pulls"))
        self.assertEqual(headers["Authorization"], "Bearer gh-123456")
        self.assertEqual(body, {"title": "t", "head": "feat/1-x", "base": "main", "body": "b"})

    def test_gitlab_publish_payload(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((url, body))
            return 201, {}
        self.publish(transport)
        self.assertEqual(seen[0], (GL + "/projects/g%2Fp/merge_requests",
                                   {"source_branch": "feat/1-x", "target_branch": "main", "title": "t",
                                    "description": "b"}))

    def test_returned_dict_never_carries_the_token(self):
        r = self.publish(raising(RuntimeError("header PRIVATE-TOKEN: tok-123456")))
        self.assertNotIn("tok-123456", json.dumps(r))
        r = self.publish(answer(422, {"echo": "tok-123456"}))
        self.assertNotIn("tok-123456", json.dumps(r))

    def test_a_number_that_is_not_a_number_is_rejected_before_sending(self):
        calls = []
        def transport(*a):
            calls.append(a)
            return 201, {}
        r = delivery.comment_issue("gitlab", "g/p", "58/../../x", "b", transport=transport, base_url=GL)
        self.assertEqual(r["outcome"], "error")
        self.assertFalse(delivery.verify("gitlab", "g/p", "7?x", 1, transport=transport, base_url=GL))
        self.assertEqual(calls, [])


class TestDefaultTransport(unittest.TestCase):
    """The urllib transport, without the network: a URL that cannot be opened never reaches a socket."""
    def test_unusable_url_is_not_sent(self):
        transport = delivery.urllib_transport(timeout=1)
        for url in ("not a url", "nope://gitlab.example/api/v4/x"):
            with self.subTest(url=url), self.assertRaises(delivery.NotSent):
                transport("POST", url, {}, {"a": 1})

    def test_publish_through_it_is_error_not_unknown(self):
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "tok-123456"}):
            r = delivery.publish("gitlab", "g/p", "s", "main", "t", "b", base_url="nope://gitlab.example/api/v4")
        self.assertEqual((r["outcome"], r["sent"]), ("error", False))


class TestVerifyAndLookup(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GITLAB_TOKEN": "t", "GITHUB_TOKEN": "t"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verify_any_non_2xx_is_false(self):
        for status in (301, 404, 429, 500, 600):
            self.assertFalse(delivery.verify("gitlab", "g/p", 7, 1, transport=answer(status, {"description": "xx"}),
                                             base_url=GL), status)
        self.assertFalse(delivery.verify("gitlab", "g/p", 7, 1, transport=raising(TimeoutError()), base_url=GL))

    def test_verify_reads_the_github_body(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url))
            return 200, {"body": "y" * 10}
        self.assertTrue(delivery.verify("github", "o/r", 3, 10, transport=transport))
        self.assertEqual(seen[0], ("GET", "https://api.github.com/repos/o/r/pulls/3"))

    def test_verify_with_a_null_body_is_false(self):
        self.assertFalse(delivery.verify("gitlab", "g/p", 7, 1, transport=answer(200, {"description": None}),
                                         base_url=GL))

    def test_verify_never_passes_a_vacuous_length(self):
        self.assertFalse(delivery.verify("gitlab", "g/p", 7, 0, transport=answer(200, {"description": ""}),
                                         base_url=GL))

    def test_github_lookup_uses_owner_colon_branch(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url))
            return 200, [{"number": 4, "html_url": "https://github.com/o/r/pull/4"}]
        self.assertEqual(delivery.open_request_for("github", "o/r", "feat/1-x", transport=transport)["number"], 4)
        self.assertEqual(seen[0][0], "GET")
        self.assertIn("/repos/o/r/pulls?", seen[0][1])
        self.assertIn("head=o%3Afeat%2F1-x", seen[0][1])
        self.assertIn("state=open", seen[0][1])

    def test_gitlab_lookup_query(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append(url)
            return 200, []
        delivery.open_request_for("gitlab", "g/p", "feat/1-x", transport=transport, base_url=GL)
        self.assertIn("/projects/g%2Fp/merge_requests?", seen[0])
        self.assertIn("source_branch=feat%2F1-x", seen[0])
        self.assertIn("state=opened", seen[0])

    def test_lookup_failure_raises_never_none(self):
        for transport in (answer(500), answer(401), raising(TimeoutError())):
            with self.assertRaises(delivery.RequestFailed):
                delivery.open_request_for("gitlab", "g/p", "b", transport=transport, base_url=GL)
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": ""}):
            with self.assertRaises(delivery.RequestFailed) as ctx:
                delivery.open_request_for("gitlab", "g/p", "b", transport=answer(200, []), base_url=GL)
            self.assertEqual(ctx.exception.reason, "no token")


class TestRender(unittest.TestCase):
    def test_closes_is_english_even_in_portuguese(self):
        md = delivery.render("s", "c", ["cmd"], [], "|t|", delivery.closes_line("9", "main", "main"), "pt-BR")
        self.assertIn("Closes #9", md)

    def test_no_closes_warns_on_stderr(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}), contextlib.redirect_stderr(err):
            md = delivery.render("s", "c", ["cmd"], [], "|t|", None, "en")
        self.assertNotIn("Closes", md)
        self.assertIn("close", err.getvalue().lower())

    def test_nothing_not_run_is_still_declared(self):
        md = delivery.render("s", "c", ["unittest"], [], "|t|", None, "en")
        self.assertIn("Not run", md.split("## Verification")[1])

    def test_values_are_not_rescanned_for_placeholders(self):
        md = delivery.render("{{timing}}", "c", ["x"], [], "TABLE", None, "en")
        self.assertEqual(md.count("TABLE"), 1)

    def test_repo_template_is_used(self):
        with tempfile.TemporaryDirectory() as repo:
            with open(os.path.join(repo, "mr.md"), "w") as f:
                f.write("CUSTOM {{summary}} {{changes}} {{verification}} {{timing}} {{closes}}\n")
            md = delivery.render("s", "c", ["x"], [], "|t|", None, "en", template="mr.md", repo=repo)
            self.assertTrue(md.startswith("CUSTOM s c"))

    def test_bad_repo_templates_fail_loudly(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as repo:
            with open(os.path.join(outside, "t.md"), "w") as f:
                f.write("{{summary}} {{changes}} {{verification}} {{timing}} {{closes}}")
            os.symlink(os.path.join(outside, "t.md"), os.path.join(repo, "link.md"))
            with open(os.path.join(repo, "partial.md"), "w") as f:
                f.write("{{summary}} only")
            with open(os.path.join(repo, "typo.md"), "w") as f:
                f.write("{{summary}} {{changes}} {{verification}} {{timing}} {{closes}} {{sumary}}")
            for value in ("../t.md", "a/../../t.md", os.path.join(outside, "t.md"), "~/t.md", "missing.md",
                          "link.md", "partial.md", "typo.md", ""):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    delivery.render("s", "c", ["x"], [], "|t|", None, "en", template=value, repo=repo)
            with self.assertRaises(ValueError):          # relative path without a repo: no guessing the cwd
                delivery.render("s", "c", ["x"], [], "|t|", None, "en", template="mr.md")

    def test_comment_template_has_the_table(self):
        for lang, head in (("en", "## ⏱ Development time"), ("pt-BR", "## ⏱ Tempo de desenvolvimento")):
            md = delivery.render_comment("|t|", lang, request_url="https://example/mr/9")
            self.assertIn(head, md)
            self.assertIn("|t|", md)
            self.assertIn("https://example/mr/9", md)


class TestRemote(unittest.TestCase):
    def test_api_base_from_the_remote(self):
        self.assertEqual(delivery.api_base("gitlab", "git@git.example.org:group/sub/proj.git"),
                         "https://git.example.org/api/v4")
        self.assertEqual(delivery.api_base("gitlab", "https://git.example.org:8443/group/proj"),
                         "https://git.example.org:8443/api/v4")
        self.assertEqual(delivery.api_base("gitlab", "ssh://git@git.example.org:2222/g/p.git"),
                         "https://git.example.org/api/v4")
        self.assertEqual(delivery.api_base("github", "git@github.com:o/r.git"), "https://api.github.com")

    def test_target_reads_origin_and_encodes_the_project(self):
        with fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@git.example.org:group/sub/proj.git"],
                           check=True)
            plat, project, base = delivery.target(repo)
            self.assertEqual((plat, project, base), ("gitlab", "group/sub/proj", "https://git.example.org/api/v4"))
            self.assertEqual(delivery.project_path("gitlab", project), "/projects/group%2Fsub%2Fproj")

    def test_config_overrides_platform_and_api_url(self):
        with fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://code.example.org/o/r.git"],
                           check=True)
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"platform": "github", "api_url": "https://code.example.org/api/v3/"}, f)
            self.assertEqual(delivery.target(repo), ("github", "o/r", "https://code.example.org/api/v3"))

    def test_no_remote_is_a_delivery_error(self):
        with fake_repo() as repo:
            with self.assertRaises(delivery.DeliveryError) as ctx:
                delivery.target(repo)
            self.assertEqual(ctx.exception.code, "no-remote")


class CliMixin:
    """Runs delivery.main in-process inside `repo`, with an isolated state dir and HOME."""
    def cli(self, repo, argv, transport, env):
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        if not hasattr(self, "state"):
            self.state = tempfile.mkdtemp(prefix="plan-gate-test-")
            self.home = tempfile.mkdtemp(prefix="plan-gate-home-")
            if hasattr(self, "addCleanup"):
                import shutil
                self.addCleanup(shutil.rmtree, self.state, True)
                self.addCleanup(shutil.rmtree, self.home, True)
        # never the real state dir: `publish` stamps `delivered`. GITLAB_HOST binds the token to the fake host (R70)
        full = dict({"PLAN_GATE_DIR": self.state, "HOME": self.home, "GITLAB_HOST": "gitlab.example", "GH_HOST": ""},
                    **env)
        os.chdir(repo)
        try:
            with mock.patch.dict(os.environ, full), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = delivery.main(argv, transport=transport)
        finally:
            os.chdir(cwd)
        return rc, out.getvalue(), err.getvalue()

    def repo_with(self, remote):
        ctx = fake_repo()
        repo = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", remote], check=True)
        return repo

    def body_file(self, repo, text="body"):
        path = os.path.join(repo, "body.md")
        with open(path, "w") as f:
            f.write(text)
        return path



class TestCli(CliMixin, unittest.TestCase):
    """R55: JSON on stdout, never a token, rc 0 only for ok."""
    def test_transport_exception_carrying_the_token_never_leaks(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        secret = "SECRET-TOKEN-123"
        rc, out, err = self.cli(repo, ["publish", "--title", "t", "--source", "main", "--target", "main",
                                       "--body-file", self.body_file(repo)],
                                raising(RuntimeError(f"failed with PRIVATE-TOKEN: {secret}")),
                                {"GITLAB_TOKEN": secret, "PLAN_GATE_LANG": "en"})
        self.assertNotIn(secret, out + err)
        self.assertEqual(json.loads(out)["outcome"], "unknown")
        self.assertNotEqual(rc, 0)

    def test_publish_ok_is_rc0_with_ref_and_url(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        rc, out, err = self.cli(repo, ["publish", "--title", "t", "--source", "main", "--target", "main",
                                       "--body-file", self.body_file(repo)],
                                answer(201, {"iid": 7, "web_url": "https://gitlab.example/g/p/-/merge_requests/7"}),
                                {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"})
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual((data["outcome"], data["ref"]), ("ok", 7))
        self.assertIn("merge_requests/7", data["url"])

    def test_every_non_ok_outcome_is_nonzero(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        for transport in (answer(422), answer(429), answer(503), answer(600)):
            rc, out, _ = self.cli(repo, ["comment", "--issue", "5", "--body-file", self.body_file(repo)], transport,
                                  {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"})
            self.assertNotEqual(rc, 0, out)

    def test_verify_and_open_request(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        env = {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"}
        rc, out, _ = self.cli(repo, ["verify", "--ref", "7", "--expected-len", "3"],
                              answer(200, {"description": "abc"}), env)
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.cli(repo, ["verify", "--ref", "7", "--expected-len", "4"],
                              answer(200, {"description": "abc"}), env)
        self.assertNotEqual(rc, 0, out)
        rc, out, _ = self.cli(repo, ["open-request", "--branch", "feat/1-x"],
                              answer(200, [{"iid": 9, "web_url": "u"}]), env)
        self.assertEqual((rc, json.loads(out)["ref"]), (0, 9))
        rc, out, _ = self.cli(repo, ["open-request", "--branch", "feat/1-x"], answer(500), env)
        self.assertNotEqual(rc, 0)

    def test_platform_never_echoes_credentials_in_the_remote(self):
        repo = self.repo_with("https://oauth2:PASSWORD-XYZ@gitlab.example/g/p.git")
        rc, out, err = self.cli(repo, ["platform"], None, {"PLAN_GATE_LANG": "en"})
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual((data["platform"], data["project"], data["api_url"]),
                         ("gitlab", "g/p", "https://gitlab.example/api/v4"))
        self.assertNotIn("PASSWORD-XYZ", out + err)

    def test_render_writes_the_body_and_reports_its_length(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        subprocess.run(["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
                       check=True)
        spec = os.path.join(repo, "in.json")
        with open(spec, "w") as f:
            json.dump({"summary": "S", "changes": "C", "verification": ["make test"], "not_run": ["make e2e"],
                       "issue": "58", "target": "main"}, f)
        out_md = os.path.join(repo, "out.md")
        with temp_state() as env:
            env = no_tokens(env)
            env["PLAN_GATE_LANG"] = "en"
            with mock.patch.dict(os.environ, env, clear=True):
                rc, out, err = self.cli(repo, ["render", "--input", spec, "--out", out_md], None, {})
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        with open(out_md) as f:
            body = f.read()
        self.assertEqual(data["length"], len(body.strip()))
        self.assertEqual(data["closes"], "Closes #58")
        self.assertIn("Closes #58", body)
        self.assertIn("make e2e", body.split("## Verification")[1])
        self.assertIn("## ⏱ Development time", body)

    def test_render_without_the_default_branch_drops_closes_and_warns(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        spec = os.path.join(repo, "in.json")
        with open(spec, "w") as f:
            json.dump({"summary": "S", "changes": "C", "verification": ["x"], "not_run": [],
                       "issue": "58", "target": "dev"}, f)
        with temp_state() as env:
            env = no_tokens(env)
            env["PLAN_GATE_LANG"] = "en"
            with mock.patch.dict(os.environ, env, clear=True):
                rc, out, err = self.cli(repo, ["render", "--input", spec, "--out", os.path.join(repo, "o.md")],
                                        None, {})
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertIsNone(data["closes"])
        self.assertTrue(data["warnings"])
        self.assertTrue(err.strip())

    def test_script_runs_as_a_subprocess(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@github.com:o/r.git"], check=True)
            rc, out, err = run("delivery.py", "platform", cwd=repo, env=no_tokens(env))
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["platform"], "github")


class TestUpdate(unittest.TestCase):
    """R58: the description is rewritten once the table is closed -- the description ONLY."""
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GITLAB_TOKEN": "tok-123456", "GITHUB_TOKEN": "gh-123456"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, status=200):
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url, body))
            return status, {"iid": 7}
        return seen, transport

    def test_gitlab_update_puts_the_description_only(self):
        seen, transport = self.record()
        r = delivery.update("gitlab", "g/p", 7, "new body", transport=transport, base_url=GL)
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(seen, [("PUT", GL + "/projects/g%2Fp/merge_requests/7", {"description": "new body"})])

    def test_github_update_patches_the_body_only(self):
        seen, transport = self.record()
        r = delivery.update("github", "o/r", "#3", "new body", transport=transport)
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(seen, [("PATCH", "https://api.github.com/repos/o/r/pulls/3", {"body": "new body"})])

    def test_update_payload_never_carries_state(self):
        for plat in ("gitlab", "github"):
            seen, transport = self.record()
            delivery.update(plat, "o/r", 3, "b", transport=transport, base_url="https://api.example")
            keys = set(seen[0][2])
            self.assertFalse(keys & {"state", "state_event", "merge", "merge_when_pipeline_succeeds",
                                     "squash", "title", "target_branch", "base"}, plat)

    def test_update_5xx_and_no_answer_are_unknown(self):
        for status in (500, 503):
            self.assertEqual(delivery.update("gitlab", "g/p", 7, "b", transport=answer(status), base_url=GL)["outcome"],
                             "unknown")
        r = delivery.update("gitlab", "g/p", 7, "b", transport=raising(TimeoutError()), base_url=GL)
        self.assertEqual((r["outcome"], r["sent"]), ("unknown", True))
        self.assertEqual(delivery.update("gitlab", "g/p", 7, "b", transport=answer(422), base_url=GL)["outcome"],
                         "refused")

    def test_update_rejects_a_ref_that_is_not_a_number_before_sending(self):
        seen, transport = self.record()
        self.assertEqual(delivery.update("gitlab", "g/p", "7/merge", "b", transport=transport,
                                         base_url=GL)["outcome"], "error")
        self.assertEqual(seen, [])


def _host_of(url):
    import urllib.parse
    return urllib.parse.urlsplit(url).hostname


class TestTokenBinding(unittest.TestCase):
    """R70/R71/R78: a token goes only to a host the environment names, over https, for an origin on that host."""
    def calls_for(self, remote, env, cfg=None, argv=None):
        seen = []
        def transport(method, url, headers, body):
            seen.append((url, dict(headers)))
            return 201, {"iid": 7, "web_url": "u"}
        cli = CliMixin()
        ctx = fake_repo()
        repo = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", remote], check=True)
        if cfg:
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump(cfg, f)
        body = cli.body_file(repo)
        spec = os.path.join(repo, "in.json")
        with open(spec, "w") as f:
            json.dump({"summary": "S", "changes": "C", "verification": ["x"], "not_run": [], "issue": "5",
                       "target": "main"}, f)
        files = {"BODY": body, "INPUT": spec, "OUT": os.path.join(repo, "out.md")}
        argv = [files.get(a, a) for a in (argv or ["comment", "--issue", "5", "--body-file", body])]
        base = {"GITLAB_TOKEN": "tok-gitlab-1", "GITHUB_TOKEN": "tok-github-1", "GITLAB_HOST": "", "GH_HOST": "",
                "PLAN_GATE_LANG": "en"}
        rc, out, err = cli.cli(repo, argv, transport, dict(base, **env))
        return rc, json.loads(out), seen, out + err

    def assertNoToken(self, seen, out):
        for _, headers in seen:
            self.assertNotIn("tok-gitlab-1", json.dumps(headers))
            self.assertNotIn("tok-github-1", json.dumps(headers))

    def test_foreign_hosts_get_no_token(self):
        for remote, host in (("git@bitbucket.org:o/r.git", "bitbucket.org"),
                             ("https://codeberg.org/o/r.git", "codeberg.org"),
                             ("git@gitlab.example:g/p.git", "gitlab.example")):
            with self.subTest(remote=remote):
                rc, data, seen, out = self.calls_for(remote, {})
                self.assertNotEqual(rc, 0)
                self.assertEqual(seen, [])
                self.assertIn("token-not-bound-to-host", data["error"])
                self.assertIn(host, data["error"])

    def test_gitlab_host_binds_the_token(self):
        for value in ("git.example.org", "https://git.example.org", "https://git.example.org/"):
            with self.subTest(value=value):
                rc, data, seen, out = self.calls_for("git@git.example.org:g/p.git", {"GITLAB_HOST": value})
                self.assertEqual(rc, 0, out)
                self.assertEqual(seen[0][1]["PRIVATE-TOKEN"], "tok-gitlab-1")

    def every_subcommand(self, repo_body_holder=None):
        return [["publish", "--title", "t", "--source", "main", "--target", "main", "--body-file", "BODY"],
                ["update", "--ref", "7", "--body-file", "BODY"], ["verify", "--ref", "7", "--expected-len", "1"],
                ["comment", "--issue", "5", "--body-file", "BODY"], ["open-request", "--branch", "s"]]

    def test_r67_repo_api_url_on_a_foreign_host_never_gets_the_token(self):
        """R70: `.claude/plan-gate.json` is committed in the repo -- a cloned repo must not pick the token's host."""
        for env in ({}, {"GITLAB_HOST": "git.example.org"}):
            for cfg in ({"api_url": "https://evil.example/api/v4"},
                        {"platform": "github", "api_url": "https://evil.example/api/v3"}):
                for argv in self.every_subcommand():
                    with self.subTest(env=env, cfg=cfg, cmd=argv[0]):
                        rc, data, seen, out = self.calls_for("git@git.example.org:g/p.git", env, cfg=cfg, argv=argv)
                        self.assertNotEqual(rc, 0)
                        self.assertEqual(seen, [])
                        self.assertIn("token-not-bound-to-host: evil.example", data["error"])

    def test_r67_repo_api_url_on_a_foreign_host_and_render_or_platform(self):
        rc, data, seen, out = self.calls_for("git@git.example.org:g/p.git", {"GITLAB_HOST": "git.example.org"},
                                             cfg={"api_url": "https://evil.example/api/v4"}, argv=["platform"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(data["token_bound"])
        self.assertEqual(seen, [])

    def test_r70_repo_config_never_binds_the_origin_host(self):
        """R70: a hostile host X serving a repo whose config says `api_url: X` must not receive the token."""
        for cfg in ({"api_url": "https://code.example.org:8443/api/v4"},
                    {"api_url": "https://code.example.org/api/v4", "platform": "gitlab"},
                    {"platform": "github", "api_url": "https://code.example.org/api/v3"},
                    {"platform": "github"}):
            for argv in self.every_subcommand() + [["platform"]]:
                with self.subTest(cfg=cfg, cmd=argv[0]):
                    rc, data, seen, out = self.calls_for("git@code.example.org:g/p.git", {}, cfg=cfg, argv=argv)
                    self.assertEqual(seen, [])
                    if argv[0] == "platform":
                        self.assertFalse(data["token_bound"])
                    else:
                        self.assertNotEqual(rc, 0)
                        self.assertIn("token-not-bound-to-host: code.example.org", data["error"])

    def test_r70_repo_api_url_inside_the_trusted_set_is_used(self):
        # R78: the origin must be on the API host (or the host the environment names) -- same host, other port
        rc, data, seen, out = self.calls_for("git@code.example.org:g/p.git", {"GITLAB_HOST": "code.example.org"},
                                             cfg={"api_url": "https://code.example.org:8443/api/v4"})
        self.assertEqual(rc, 0, out)
        self.assertTrue(seen[0][0].startswith("https://code.example.org:8443/api/v4/"))
        self.assertEqual(seen[0][1]["PRIVATE-TOKEN"], "tok-gitlab-1")

    def test_r78_the_origin_host_must_be_the_api_host(self):
        """Measured: origin on a hostile host + a repo config pointing the API at gitlab.com sent the token to
        gitlab.com with the project the hostile origin named (`victim-group/secret-proj`)."""
        cases = (("git@evil.example:victim-group/secret-proj.git",
                  {"platform": "gitlab", "api_url": "https://gitlab.com/api/v4"}, {}),
                 ("git@evil.example:victim-group/secret-proj.git",
                  {"platform": "github", "api_url": "https://api.github.com"}, {}),
                 ("git@evil.example:victim-group/secret-proj.git",
                  {"api_url": "https://api.example.org/api/v4"}, {"GITLAB_HOST": "api.example.org"}))
        for remote, cfg, env in cases:
            for argv in self.every_subcommand() + [["notes", "--issue", "5"]]:
                with self.subTest(cfg=cfg, cmd=argv[0]):
                    rc, data, seen, out = self.calls_for(remote, env, cfg=cfg, argv=argv)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(seen, [])
                    self.assertIn("token-not-bound-to-host", data["error"])
                    self.assertIn("evil.example", data["error"])
            rc, data, seen, out = self.calls_for(remote, env, cfg=cfg, argv=["platform"])
            self.assertFalse(data["token_bound"])
            rc, data, seen, out = self.calls_for(remote, env, cfg=cfg,
                                                 argv=["render", "--input", "INPUT", "--out", "OUT"])
            self.assertEqual(rc, 0, out)
            self.assertEqual(seen, [])                       # render's default-branch lookup is skipped too

    def test_r78_the_origin_on_the_environment_host_is_bound(self):
        rc, data, seen, out = self.calls_for("git@gitlab.com:g/p.git", {})
        self.assertEqual(rc, 0, out)
        rc, data, seen, out = self.calls_for("git@git.example.org:g/p.git", {"GITLAB_HOST": "git.example.org"},
                                             cfg={"api_url": "https://gitlab.com/api/v4"})
        self.assertEqual(rc, 0, out)                         # the person named the origin's host

    def test_r70_gh_host_binds_the_github_token_and_derives_the_ghe_base(self):
        rc, data, seen, out = self.calls_for("git@ghe.example.org:o/r.git", {"GH_HOST": "ghe.example.org"})
        self.assertEqual(rc, 0, out)
        url, headers = seen[0]
        self.assertEqual(url, "https://ghe.example.org/api/v3/repos/o/r/issues/5/comments")
        self.assertEqual(headers["Authorization"], "Bearer tok-github-1")
        self.assertNotIn("PRIVATE-TOKEN", headers)
        rc, data, seen, out = self.calls_for("git@ghe.example.org:o/r.git", {"GH_HOST": "https://ghe.example.org"},
                                             argv=["platform"])
        self.assertEqual((data["platform"], data["api_url"], data["token_bound"]),
                         ("github", "https://ghe.example.org/api/v3", True))

    def test_r70_gh_host_does_not_bind_the_gitlab_token(self):
        rc, data, seen, out = self.calls_for("git@code.example.org:g/p.git", {"GH_HOST": "code.example.org"},
                                             cfg={"platform": "gitlab"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(seen, [])
        self.assertIn("token-not-bound-to-host: code.example.org", data["error"])

    def test_r68_a_host_with_at_sign_is_an_invalid_remote(self):
        for remote in ("git@evil.com@gitlab.com:g/p.git", "https://a@evil.com@gitlab.com/g/p.git",
                       "ssh://git@evil.com@gitlab.com/g/p.git"):
            with self.subTest(remote=remote):
                with self.assertRaises(ValueError):
                    delivery.parse_remote(remote)
                rc, data, seen, out = self.calls_for(remote, {"GITLAB_HOST": "gitlab.com"})
                self.assertNotEqual(rc, 0)
                self.assertEqual(seen, [])
                self.assertEqual(data["error"], "unrecognised-remote")

    def test_gitlab_com_is_bound(self):
        rc, data, seen, out = self.calls_for("git@gitlab.com:g/p.git", {})
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0][1]["PRIVATE-TOKEN"], "tok-gitlab-1")

    def test_github_token_only_to_api_github_com_or_gh_host(self):
        rc, data, seen, out = self.calls_for("git@github.com:o/r.git", {})
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0][1]["Authorization"], "Bearer tok-github-1")
        rc, data, seen, out = self.calls_for("git@code.example.org:o/r.git", {}, cfg={"platform": "github"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(seen, [])
        self.assertIn("code.example.org", data["error"])
        rc, data, seen, out = self.calls_for("git@code.example.org:o/r.git", {"GH_HOST": "code.example.org"},
                                             cfg={"platform": "github", "api_url": "https://code.example.org/api/v3"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0][1]["Authorization"], "Bearer tok-github-1")

    def test_gitlab_host_does_not_bind_the_github_token(self):
        rc, data, seen, out = self.calls_for("git@code.example.org:o/r.git", {"GITLAB_HOST": "code.example.org"},
                                             cfg={"platform": "github"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(seen, [])

    # ---------------------------------------------------------------- R71: a token never travels in cleartext
    def assertNotBound(self, remote, env, cfg, host):
        for argv in self.every_subcommand() + [["platform"]]:
            with self.subTest(env=env, cfg=cfg, cmd=argv[0]):
                rc, data, seen, out = self.calls_for(remote, env, cfg=cfg, argv=argv)
                self.assertEqual(seen, [])
                if argv[0] == "platform":
                    self.assertEqual(rc, 0, out)
                    self.assertFalse(data["token_bound"])
                else:
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(data["error"], f"token-not-bound-to-host: {host}")

    def test_r71_repo_http_on_gitlab_com_is_never_bound(self):
        for cfg in ({"api_url": "http://gitlab.com/api/v4"}, {"api_url": "http://gitlab.com:80/api/v4"},
                    {"api_url": "http://gitlab.com:443/api/v4"}):
            self.assertNotBound("git@gitlab.com:g/p.git", {}, cfg, "gitlab.com")

    def test_r71_repo_http_on_api_github_com_is_never_bound(self):
        self.assertNotBound("git@github.com:o/r.git", {}, {"api_url": "http://api.github.com"}, "api.github.com")

    def test_r71_an_https_or_bare_env_host_never_binds_a_repo_http_url(self):
        for env in ({"GITLAB_HOST": "https://git.example.org"}, {"GITLAB_HOST": "git.example.org"}):
            for cfg in ({"api_url": "http://git.example.org:9999/api/v4"},
                        {"api_url": "http://git.example.org/api/v4"}):
                self.assertNotBound("git@git.example.org:g/p.git", env, cfg, "git.example.org")

    def test_r71_an_http_env_host_binds_only_that_exact_host_and_port(self):
        env = {"GITLAB_HOST": "http://git.example.org"}
        for cfg in ({"api_url": "http://git.example.org:9999/api/v4"}, {"api_url": "http://gitlab.com/api/v4"}):
            self.assertNotBound("git@git.example.org:g/p.git", env, cfg, _host_of(cfg["api_url"]))
        # the GitLab variable never lets the GitHub token travel in cleartext
        self.assertNotBound("git@git.example.org:o/r.git", env,
                            {"platform": "github", "api_url": "http://git.example.org/api/v3"}, "git.example.org")

    def test_r71_an_http_env_host_is_bound_over_http(self):
        for env, cfg, base in (({"GITLAB_HOST": "http://git.example.org"}, None, "http://git.example.org/api/v4/"),
                               ({"GITLAB_HOST": "http://git.example.org/"}, None, "http://git.example.org/api/v4/"),
                               ({"GITLAB_HOST": "http://git.example.org:8080"}, None,
                                "http://git.example.org:8080/api/v4/"),
                               ({"GITLAB_HOST": "http://git.example.org"}, {"api_url": "http://git.example.org/api/v4"},
                                "http://git.example.org/api/v4/")):
            with self.subTest(env=env, cfg=cfg):
                rc, data, seen, out = self.calls_for("git@git.example.org:g/p.git", env, cfg=cfg)
                self.assertEqual(rc, 0, out)
                self.assertTrue(seen[0][0].startswith(base), seen[0][0])
                self.assertEqual(seen[0][1]["PRIVATE-TOKEN"], "tok-gitlab-1")
        rc, data, seen, out = self.calls_for("git@ghe.example.org:o/r.git", {"GH_HOST": "http://ghe.example.org"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen[0][0], "http://ghe.example.org/api/v3/repos/o/r/issues/5/comments")
        self.assertEqual(seen[0][1]["Authorization"], "Bearer tok-github-1")

    def test_r71_an_https_port_on_a_trusted_host_stays_bound(self):
        for env, remote, cfg, base in (({}, "git@gitlab.com:g/p.git", {"api_url": "https://gitlab.com:8443/api/v4"},
                                        "https://gitlab.com:8443/api/v4/"),
                                       ({"GITLAB_HOST": "http://git.example.org"}, "git@git.example.org:g/p.git",
                                        {"api_url": "https://git.example.org:8443/api/v4"},
                                        "https://git.example.org:8443/api/v4/")):
            with self.subTest(cfg=cfg):
                rc, data, seen, out = self.calls_for(remote, env, cfg=cfg)
                self.assertEqual(rc, 0, out)
                self.assertTrue(seen[0][0].startswith(base), seen[0][0])

    def test_r71_render_makes_no_request_when_the_token_is_unbound(self):
        """render's default-branch GET carries the token: an unbound base gets zero requests."""
        render = ["render", "--input", "INPUT", "--out", "OUT"]
        for remote, env, cfg in (("git@gitlab.com:g/p.git", {}, {"api_url": "http://gitlab.com/api/v4"}),
                                 ("git@git.example.org:g/p.git", {"GITLAB_HOST": "https://git.example.org"},
                                  {"api_url": "http://git.example.org:9999/api/v4"}),
                                 ("git@code.example.org:g/p.git", {}, None),
                                 ("git@code.example.org:g/p.git", {}, {"api_url": "https://evil.example/api/v4"})):
            with self.subTest(remote=remote, cfg=cfg):
                rc, data, seen, out = self.calls_for(remote, env, cfg=cfg, argv=render)
                self.assertEqual(rc, 0, out)
                self.assertEqual(seen, [])
        # the control: a bound base does make the GET, so the empty list above is not vacuous
        rc, data, seen, out = self.calls_for("git@gitlab.com:g/p.git", {}, argv=render)
        self.assertEqual(rc, 0, out)
        self.assertEqual([u for u, _ in seen], ["https://gitlab.com/api/v4/projects/g%2Fp"])

    def test_platform_reports_the_binding_without_failing(self):
        rc, data, seen, out = self.calls_for("git@bitbucket.org:o/r.git", {}, argv=["platform"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(data["token_bound"])


class TestHardening(unittest.TestCase):
    def test_r62_api_base_from_a_remote_is_always_https(self):
        self.assertEqual(delivery.api_base("gitlab", "http://git.example.org/g/p.git"), "https://git.example.org/api/v4")
        self.assertEqual(delivery.api_base("gitlab", "http://git.example.org:8080/g/p.git"),
                         "https://git.example.org/api/v4")
        self.assertEqual(delivery.api_base("github", "http://code.example.org/o/r"), "https://code.example.org/api/v3")

    def test_r71_a_repo_http_api_url_is_never_the_derived_base(self):
        """R71 (replaces the R62 config test): the repo may write an http `api_url`, but it is never bound --
        see TestTokenBinding.test_r71_*. The derived base stays https."""
        with fake_repo() as repo, mock.patch.dict(os.environ, {"GITLAB_HOST": "", "GH_HOST": ""}):
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "http://git.example.org/g/p.git"], check=True)
            self.assertEqual(delivery.target(repo)[2], "https://git.example.org/api/v4")
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"api_url": "http://git.example.org:8080/api/v4"}, f)
            self.assertEqual(delivery.target(repo)[2], "http://git.example.org:8080/api/v4")
            self.assertEqual(delivery.token_bound(repo, "gitlab", delivery.target(repo)[2])[0], False)

    def test_r63_scrub_never_touches_keys(self):
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "o"}):
            self.assertEqual(set(delivery.scrub({"outcome": "x", "error": "o"})), {"outcome", "error"})

    def test_r63_one_character_token_gives_valid_json(self):
        with temp_state() as env, fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "git@gitlab.example:g/p.git"], check=True)
            rc, out, err = run("delivery.py", "platform", cwd=repo, env=dict(no_tokens(env), GITLAB_TOKEN="o"))
            self.assertNotIn("Traceback", err)
            self.assertIn("outcome", json.loads(out))
            self.assertEqual(rc, 0, err)

    def test_r64_quick_actions_are_neutralised(self):
        free = "intro\n/close\n  /merge\n\t/label ~x\nend"
        md = delivery.render(free, free, ["make test\n/close"], ["/merge"], "|t|", None, "en")
        for line in md.splitlines():
            self.assertFalse(line.lstrip().startswith("/"), repr(line))
        self.assertIn("close", md)
        self.assertIn("merge", md)

    def test_r64_gitlab_bodies_are_neutralised_too(self):
        seen = []
        def transport(method, url, headers, body):
            seen.append(body)
            return 201, {}
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "t"}):
            delivery.publish("gitlab", "g/p", "s", "main", "t", "/close\nx", transport=transport, base_url=GL)
            delivery.update("gitlab", "g/p", 7, " /merge", transport=transport, base_url=GL)
            delivery.comment_issue("gitlab", "g/p", 5, "/close", transport=transport, base_url=GL)
        for body in seen:
            for value in body.values():
                for line in value.splitlines():
                    self.assertFalse(line.lstrip().startswith("/"), repr(line))


class TestStampsDelivered(CliMixin, unittest.TestCase):
    """R58: `delivered` is stamped by the CLI on an ok publish -- and only then."""
    def events(self, repo):
        timeline = import_bin("timeline")
        with mock.patch.dict(os.environ, {"PLAN_GATE_DIR": self.state}):
            return [e["event"] for e in timeline.events(repo, timeline.demand(repo))]

    def publish(self, repo, transport):
        return self.cli(repo, ["publish", "--title", "t", "--source", "main", "--target", "main",
                               "--body-file", self.body_file(repo)], transport,
                        {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"})

    def test_ok_publish_stamps_delivered(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        rc, out, err = self.publish(repo, answer(201, {"iid": 7, "web_url": "u"}))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.events(repo), ["delivered"])
        self.assertTrue(json.loads(out)["delivered"])

    def test_unknown_refused_and_error_stamp_nothing(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        for transport in (answer(502), raising(TimeoutError()), answer(422), answer(429), answer(600)):
            rc, out, _ = self.publish(repo, transport)
            self.assertNotEqual(rc, 0)
        self.assertEqual(self.events(repo), [])

    def test_update_cli(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url, body))
            return 200, {"iid": 7, "web_url": "u"}
        rc, out, err = self.cli(repo, ["update", "--ref", "7", "--body-file", self.body_file(repo, "B")], transport,
                                {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"})
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen, [("PUT", "https://gitlab.example/api/v4/projects/g%2Fp/merge_requests/7",
                                 {"description": "B"})])
        self.assertEqual(self.events(repo), [])          # update never stamps


class TestOneCharacterToken(CliMixin, unittest.TestCase):
    """R63: a token of one character must not rewrite the fixed fields (`outcome`, `next`, ids, numbers)."""
    def events(self, repo):
        timeline = import_bin("timeline")
        with mock.patch.dict(os.environ, {"PLAN_GATE_DIR": self.state}):
            return [e["event"] for e in timeline.events(repo, timeline.demand(repo))]

    def run_cli(self, tok, argv, transport):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        argv = [self.body_file(repo) if a == "BODY" else a for a in argv]
        rc, out, err = self.cli(repo, argv, transport, {"GITLAB_TOKEN": tok, "PLAN_GATE_LANG": "en"})
        self.assertNotIn("Traceback", err)
        return repo, rc, json.loads(out)

    def test_publish_ok_with_a_one_character_token(self):
        for tok in ("o", "k"):
            with self.subTest(tok=tok):
                repo, rc, data = self.run_cli(tok, ["publish", "--title", "t", "--source", "main", "--target", "main",
                                                    "--body-file", "BODY"], answer(201, {"iid": 7, "web_url": "u"}))
                self.assertEqual((rc, data["outcome"], data["ref"], data["delivered"]), (0, "ok", 7, True))
                self.assertEqual(self.events(repo), ["delivered"])

    def test_update_comment_and_open_request_with_a_one_character_token(self):
        for tok in ("o", "k", "e", "p"):
            with self.subTest(tok=tok):
                _, rc, data = self.run_cli(tok, ["update", "--ref", "7", "--body-file", "BODY"],
                                           answer(200, {"iid": 7}))
                self.assertEqual((rc, data["outcome"]), (0, "ok"))
                _, rc, data = self.run_cli(tok, ["comment", "--issue", "5", "--body-file", "BODY"],
                                           answer(201, {"id": 3}))
                self.assertEqual((rc, data["outcome"], data["ref"]), (0, "ok", 3))
                _, rc, data = self.run_cli(tok, ["open-request", "--branch", "s"],
                                           answer(200, [{"iid": 9, "web_url": "u"}]))
                self.assertEqual((rc, data["outcome"], data["ref"]), (0, "ok", 9))
                _, rc, data = self.run_cli(tok, ["publish", "--title", "t", "--source", "main", "--target", "main",
                                                 "--body-file", "BODY"], answer(502))
                self.assertEqual((rc, data["outcome"], data["next"], data["status"]), (5, "unknown", "open-request", 502))

    def test_library_fixed_fields_survive_and_free_text_is_scrubbed(self):
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "o"}):
            r = delivery.publish("gitlab", "g/p", "s", "main", "t", "b", transport=answer(503), base_url=GL)
            self.assertEqual((r["outcome"], r["next"], r["status"]), ("unknown", "open-request", 503))
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "tok-123456"}):
            r = delivery.publish("gitlab", "g/p", "s", "main", "t", "b",
                                 transport=raising(RuntimeError("x tok-123456")), base_url=GL)
            self.assertNotIn("tok-123456", json.dumps(r))
            r = delivery.publish("gitlab", "g/p", "s", "main", "t", "b",
                                 transport=answer(422, {"message": "tok-123456"}), base_url=GL)
            self.assertNotIn("tok-123456", json.dumps(r))

    def test_emit_never_raises_on_an_unknown_outcome(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = delivery._emit({"outcome": "***k"}, None, ())
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["message"], "***k")


class TestNoMutatingEndpoint(unittest.TestCase):
    """R55: delivery creates a request, reads it back and adds a note. Nothing else is reachable in the code."""
    def code_strings(self):
        with open(os.path.join(BIN, "delivery.py")) as f:
            tree = ast.parse(f.read())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                doc = node.body[0] if node.body else None
                if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                    docstrings.add(id(doc.value))
        strings = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + \
                [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        return strings, names

    def test_no_merge_state_event_or_push_in_the_code(self):
        strings, names = self.code_strings()
        for s in strings + names:
            cleaned = s.lower().replace("merge_requests", "").replace("merge_request", "")
            self.assertNotIn("merge", cleaned, s)
            self.assertNotIn("state_event", cleaned, s)
            self.assertNotIn("push", cleaned, s)
            self.assertNotIn("reopen", cleaned, s)

    def test_no_close_endpoint_in_the_code(self):
        """No path segment and no payload value that would close, reopen or merge: `/close`, `/merge`, `closed`,
        `state_event`… (`merge_requests` is the GitLab resource name, `Closes #` the line in the description)."""
        strings, _ = self.code_strings()
        for s in strings:
            low = s.lower().replace("merge_requests", "").replace("merge_request", "")
            if "/" in low:                         # anything that could be part of an endpoint
                self.assertNotIn("close", low, s)
                self.assertNotIn("merge", low, s)
            self.assertNotIn(low.strip(), {"close", "closed", "reopen", "reopened", "merge", "merged",
                                           "state_event", "merge_when_pipeline_succeeds"}, s)
        self.assertNotIn("DELETE", strings)
        self.assertEqual(sorted(s for s in strings if s in ("PUT", "PATCH")), ["PATCH", "PUT"],
                         "PUT and PATCH are each written exactly once: in update()")

    def test_every_request_is_on_the_allowlist(self):
        """R58: GET anything; POST only to create a request or a note; PUT only on merge_requests/<iid> and PATCH
        only on pulls/<n>, carrying the description/body and nothing else (no state, state_event or merge key)."""
        allowed = {
            "POST": (r"/repos/[^/]+/[^/]+/pulls$", r"/projects/[^/]+/merge_requests$",
                     r"/repos/[^/]+/[^/]+/issues/\d+/comments$", r"/projects/[^/]+/issues/\d+/notes$"),
            "PUT": (r"/projects/[^/]+/merge_requests/\d+$",),
            "PATCH": (r"/repos/[^/]+/[^/]+/pulls/\d+$",),
        }
        payload_keys = {"PUT": {"description"}, "PATCH": {"body"}}
        post_keys = {"title", "head", "base", "body", "source_branch", "target_branch", "description"}
        seen = []
        def transport(method, url, headers, body):
            seen.append((method, url, body))
            return 200, ([] if "?" in url else {"description": "x", "body": "x", "default_branch": "main"})
        import re
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "t", "GITHUB_TOKEN": "t"}):
            for plat, project in (("gitlab", "g/p"), ("github", "o/r")):
                kw = {"transport": transport, "base_url": "https://api.example"}
                delivery.publish(plat, project, "s", "main", "t", "b", **kw)
                delivery.update(plat, project, 7, "b", **kw)
                delivery.comment_issue(plat, project, 5, "b", **kw)
                delivery.verify(plat, project, 7, 1, **kw)
                delivery.open_request_for(plat, project, "s", **kw)
                delivery.default_branch(plat, project, **kw)
        self.assertEqual({m for m, _, _ in seen}, {"GET", "POST", "PUT", "PATCH"})
        for method, url, body in seen:
            path = url.split("?")[0][len("https://api.example"):]
            if method == "GET":
                self.assertIsNone(body)
                continue
            self.assertIn(method, allowed, url)
            self.assertTrue(any(re.search(p, path) for p in allowed[method]), (method, url))
            if method in payload_keys:
                self.assertEqual(set(body), payload_keys[method], (method, body))
            else:
                self.assertLessEqual(set(body), post_keys, (method, body))   # no state / merge key on create

class TestFinalReviewDelivery(CliMixin, unittest.TestCase):
    """B-I6 (R80), B-I7 (R81), B-M4/L218, B-M6."""
    ENV = {"GITLAB_TOKEN": "tok-abcdef", "PLAN_GATE_LANG": "en"}

    def input_file(self, repo, **extra):
        path = os.path.join(repo, "in.json")
        with open(path, "w") as f:
            json.dump(dict({"summary": "S", "changes": "C", "verification": ["x"], "not_run": [], "target": "main"},
                           **extra), f)
        return path

    def test_notes_lists_id_length_and_the_timing_heading(self):
        for remote, env, url in (
                ("git@gitlab.example:g/p.git", self.ENV,
                 "https://gitlab.example/api/v4/projects/g%2Fp/issues/5/notes?per_page=100"),
                ("git@github.com:o/r.git", {"GITHUB_TOKEN": "tok-gh", "PLAN_GATE_LANG": "en"},
                 "https://api.github.com/repos/o/r/issues/5/comments?per_page=100")):
            with self.subTest(remote=remote):
                repo = self.repo_with(remote)
                seen = []

                def transport(method, u, headers, body):
                    seen.append((method, u, body))
                    return 200, [{"id": 11, "body": "## ⏱ Development time\n\n| a |"},
                                 {"id": 12, "body": "looks good"}, {"id": 13, "body": None}]
                rc, out, err = self.cli(repo, ["notes", "--issue", "#5"], transport, env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(seen, [("GET", url, None)])
                self.assertEqual(json.loads(out)["notes"],
                                 [{"id": 11, "length": len("## ⏱ Development time\n\n| a |"), "timing": True},
                                  {"id": 12, "length": 10, "timing": False}, {"id": 13, "length": 0, "timing": False}])

    def test_notes_failures_are_errors_and_an_unbound_host_sends_nothing(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        for transport in (answer(503), answer(200, {"not": "a list"}), raising(TimeoutError())):
            rc, out, _ = self.cli(repo, ["notes", "--issue", "5"], transport, self.ENV)
            self.assertNotEqual(rc, 0)
            self.assertEqual(json.loads(out)["outcome"], "error")
        seen = []
        rc, out, _ = self.cli(repo, ["notes", "--issue", "5"], lambda *a: seen.append(a) or (200, []),
                              dict(self.ENV, GITLAB_HOST=""))
        self.assertNotEqual(rc, 0)
        self.assertEqual(seen, [])

    def test_publish_refuses_a_source_that_is_not_this_checkout(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        seen = []
        rc, out, err = self.cli(repo, ["publish", "--title", "t", "--source", "feat/9-other", "--target", "main",
                                       "--body-file", self.body_file(repo)],
                                lambda *a: seen.append(a) or (201, {"iid": 1}), self.ENV)
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(out)["error"], "source-is-not-this-checkout")
        self.assertEqual(seen, [])

    def test_render_returns_the_branch_and_the_target(self):
        repo = self.repo_with("git@gitlab.example:g/p.git")
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "feat/77-x"], check=True)
        rc, out, err = self.cli(repo, ["render", "--input", self.input_file(repo), "--out",
                                       os.path.join(repo, "o.md")], answer(200, {"default_branch": "main"}), self.ENV)
        self.assertEqual(rc, 0, out + err)
        data = json.loads(out)
        self.assertEqual((data["branch"], data["target"]), ("feat/77-x", "main"))

    def test_phase_start_issue_overrides_the_branch_number(self):
        timeline = import_bin("timeline")
        for started, expected in (("58", "58"), (None, "77")):
            with self.subTest(started=started):
                repo = self.repo_with("git@gitlab.example:g/p.git")
                subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "feat/77-x"], check=True)
                if started:
                    self.cli(repo, ["platform"], None, {})             # creates self.state
                    with mock.patch.dict(os.environ, {"PLAN_GATE_DIR": self.state}):
                        timeline.append(repo, "start", "phase", {"issue": started})
                rc, out, err = self.cli(repo, ["render", "--input", self.input_file(repo), "--out",
                                               os.path.join(repo, "o.md")],
                                        answer(200, {"default_branch": "main"}), self.ENV)
                self.assertEqual(rc, 0, out + err)
                data = json.loads(out)
                self.assertEqual(data["issue"], expected)                # the comment target
                self.assertEqual(data["closes"], f"Closes #{expected}")

    def test_github_errors_array_reaches_api_message_scrubbed(self):
        repo = self.repo_with("git@github.com:o/r.git")
        body = {"message": "Validation Failed",
                "errors": [{"resource": "PullRequest", "code": "custom",
                            "message": "A pull request already exists for o:main. tok-gh-secret"},
                           {"resource": "PullRequest", "field": "base", "code": "invalid"}]}
        rc, out, err = self.cli(repo, ["publish", "--title", "t", "--source", "main", "--target", "dev",
                                       "--body-file", self.body_file(repo)], answer(422, body),
                                {"GITHUB_TOKEN": "tok-gh-secret", "PLAN_GATE_LANG": "en"})
        self.assertEqual(rc, 3)
        msg = json.loads(out)["api_message"]
        self.assertIn("Validation Failed", msg)
        self.assertIn("A pull request already exists for o:main.", msg)
        self.assertIn("invalid", msg)
        self.assertNotIn("tok-gh-secret", out + err)


class TestHelpersStripTokens(unittest.TestCase):
    """L249: a test subprocess never inherits a token or a host binding from the developer's shell."""
    def test_run_and_temp_state_strip_tokens_and_hosts(self):
        names = ("GITLAB_TOKEN", "GITHUB_TOKEN", "GITLAB_HOST", "GH_HOST")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {n: "leak-" + n for n in names}):
            script = os.path.join(d, "env.py")
            with open(script, "w") as f:
                f.write("import json, os; print(json.dumps(dict(os.environ)))\n")
            rc, out, err = run(script)
            self.assertEqual(rc, 0, err)
            child = json.loads(out)
            with temp_state() as env:
                for n in names:
                    self.assertNotIn(n, child)
                    self.assertNotIn(n, env)


if __name__ == "__main__":
    unittest.main()
