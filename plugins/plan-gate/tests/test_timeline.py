import json, os, shutil, subprocess, tempfile, unittest
from unittest import mock
from .helpers import run, temp_state, fake_repo, fake_plan, hook_input, import_bin, state_of
from .test_checks import fake_check, PASS

timeline = import_bin("timeline")

def ev(t, e):
    return {"t": t, "event": e, "source": "hook"}

def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)

def bash_payload(cwd, command):
    return json.dumps({"hook_event_name": "PostToolUse", "cwd": cwd, "tool_name": "Bash",
                       "tool_input": {"command": command}})

def prompt(repo, env):
    rc, out, err = run("timeline.py", "hook", "prompt", env=env,
                       stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": repo, "prompt": "go"}))
    assert (rc, out, err) == (0, "", ""), (rc, out, err)

class TestReport(unittest.TestCase):
    def test_waiting_is_the_gap_between_stop_and_the_next_prompt(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:10:00-03:00", "stop"),
            ev("2026-10-03T10:40:00-03:00", "prompt"),
            ev("2026-10-03T10:50:00-03:00", "plan_written"),
        ])
        d = rows[0]
        self.assertEqual((d["phase"], d["wall_s"], d["waiting_s"], d["work_s"]), ("discussion", 3000, 1800, 1200))

    def test_unstamped_boundary_renders_as_dash_never_a_guess(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start")])
        self.assertIsNone(rows[0]["end"])
        self.assertIn("—", timeline.render(rows, "en"))

    def test_automatic_approval_opens_implementation(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "plan_written"),
                                ev("2026-10-03T10:20:00-03:00", "approved"),
                                ev("2026-10-03T11:00:00-03:00", "first_commit")])
        impl = [r for r in rows if r["phase"] == "implementation"][0]
        self.assertEqual(impl["wall_s"], 2400)

    def test_total_closes_on_delivered(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:30:00-03:00", "plan_written"),
            ev("2026-10-03T11:00:00-03:00", "released"),
            ev("2026-10-03T12:00:00-03:00", "first_commit"),
            ev("2026-10-03T12:20:00-03:00", "delivered"),
        ])
        self.assertIn("2h 20m", timeline.render(rows, "en").splitlines()[-1])

    def test_issue_ref_from_branch(self):
        self.assertEqual(timeline.issue_ref("feat/58-card-vendas"), "58")
        self.assertIsNone(timeline.issue_ref("main"))

    # -- beyond the brief --------------------------------------------------------------------------

    def test_stop_without_a_later_prompt_is_unknown_waiting(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:10:00-03:00", "stop"),
            ev("2026-10-03T10:30:00-03:00", "plan_written"),
        ])
        d = rows[0]
        # R45: no prompt ever closed that stop -- the wait is not measured, so it is not counted either
        self.assertEqual((d["wall_s"], d["waiting_s"], d["work_s"]), (1800, None, None))
        # the plan phase has no end: neither wall nor waiting is guessed
        plan = rows[1]
        self.assertEqual((plan["phase"], plan["end"], plan["wall_s"], plan["waiting_s"], plan["work_s"]),
                         ("plan", None, None, None, None))

    def test_an_unclosed_stop_never_becomes_waiting_in_later_phases(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:05:00-03:00", "stop"),
            ev("2026-10-03T10:30:00-03:00", "plan_written"),
            ev("2026-10-03T10:40:00-03:00", "released"),
            ev("2026-10-03T10:50:00-03:00", "first_commit"),
            ev("2026-10-03T11:00:00-03:00", "delivered"),
        ])
        by = {r["phase"]: r for r in rows}
        self.assertEqual((by["discussion"]["waiting_s"], by["discussion"]["work_s"]), (None, None))
        for phase in ("plan", "implementation", "delivery"):
            self.assertEqual(by[phase]["waiting_s"], 0, phase)
        out = timeline.render(rows, "en")
        self.assertNotIn("55m", out)
        last = out.splitlines()[-1]
        self.assertIn("1h 0m", last)
        self.assertEqual(last.count("—"), 2, last)       # waiting and work of the total are unknown

    def test_a_run_of_stops_waits_from_the_last_one(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:05:00-03:00", "stop"),
            ev("2026-10-03T10:20:00-03:00", "stop"),
            ev("2026-10-03T10:25:00-03:00", "prompt"),
            ev("2026-10-03T10:30:00-03:00", "plan_written"),
        ])
        self.assertEqual((rows[0]["waiting_s"], rows[0]["work_s"]), (300, 1500))
        self.assertIn("| 5m |", timeline.render(rows, "en"))

    def test_waiting_that_spans_a_boundary_is_clipped_to_each_phase(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "start"),
            ev("2026-10-03T10:10:00-03:00", "stop"),
            ev("2026-10-03T10:20:00-03:00", "plan_written"),
            ev("2026-10-03T10:40:00-03:00", "prompt"),
            ev("2026-10-03T11:00:00-03:00", "released"),
        ])
        disc, plan = rows[0], rows[1]
        self.assertEqual((disc["wall_s"], disc["waiting_s"]), (1200, 600))
        self.assertEqual((plan["wall_s"], plan["waiting_s"], plan["work_s"]), (2400, 1200, 1200))

    def test_a_second_plan_written_does_not_reopen_the_plan_phase(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "plan_written"),
            ev("2026-10-03T10:20:00-03:00", "released"),
            ev("2026-10-03T10:30:00-03:00", "plan_written"),     # the plan edited during implementation
            ev("2026-10-03T11:00:00-03:00", "first_commit"),
            ev("2026-10-03T11:10:00-03:00", "delivered"),
        ])
        self.assertEqual([r["phase"] for r in rows], ["plan", "implementation", "delivery"])
        plan = rows[0]
        self.assertEqual((plan["start"], plan["wall_s"]), ("2026-10-03T10:00:00-03:00", 1200))
        self.assertEqual(rows[1]["wall_s"], 2400)

    def test_released_after_approved_does_not_move_implementation(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "plan_written"),
            ev("2026-10-03T10:10:00-03:00", "approved"),
            ev("2026-10-03T10:50:00-03:00", "released"),
            ev("2026-10-03T11:10:00-03:00", "first_commit"),
        ])
        impl = [r for r in rows if r["phase"] == "implementation"][0]
        self.assertEqual((impl["start"], impl["wall_s"]), ("2026-10-03T10:10:00-03:00", 3600))

    def test_an_out_of_order_boundary_is_unknown_not_negative(self):
        rows = timeline.report([
            ev("2026-10-03T10:00:00-03:00", "plan_written"),
            ev("2026-10-03T10:30:00-03:00", "spec_written"),      # a spec written after the plan
            ev("2026-10-03T11:00:00-03:00", "released"),
        ])
        spec = [r for r in rows if r["phase"] == "spec"][0]
        self.assertIsNone(spec["end"])
        self.assertIsNone(spec["wall_s"])
        for r in rows:
            self.assertTrue(r["wall_s"] is None or r["wall_s"] >= 0, r)

    def test_without_delivered_the_total_is_a_dash(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start"),
                                ev("2026-10-03T10:30:00-03:00", "plan_written")])
        last = timeline.render(rows, "en").splitlines()[-1]
        self.assertIn("—", last)
        self.assertNotRegex(last, r"\d+m")

    def test_render_in_both_locales(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start"),
                                ev("2026-10-03T10:05:00-03:00", "delivered")])
        en, pt = timeline.render(rows, "en"), timeline.render(rows, "pt-BR")
        self.assertNotEqual(en, pt)
        self.assertIn("5m", en.splitlines()[-1])
        self.assertIn("5m", pt.splitlines()[-1])

    def test_issue_ref_reads_the_leading_number_but_never_a_version_or_a_date(self):
        # R49: Task 12 comments on this issue -- a wrong number comments on someone else's issue; digits
        # later in the slug do not matter
        for name, ref in (("feat/58-card-vendas", "58"), ("feat/58", "58"), ("feat/58-utf8-support", "58"),
                          ("feat/58-onda5", "58"), ("fix/7_typo", "7"),
                          ("release/1.2.3", None), ("hotfix/2026-10-03", None), ("release/1.2", None),
                          ("main", None), ("feat/x-58", None), ("feat/58.1-x", None), ("detached", None)):
            self.assertEqual(timeline.issue_ref(name), ref, name)

    def test_durations(self):
        self.assertEqual(timeline.duration(8400), "2h 20m")
        self.assertEqual(timeline.duration(1200), "20m")
        self.assertEqual(timeline.duration(None), "—")


class TestFinalReviewReport(unittest.TestCase):
    """B-I2 (R79), B-I3 (R77), B-M3: what the report may not invent."""

    def test_without_any_prompt_or_stop_waiting_and_work_are_unknown(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start"),
                                ev("2026-10-03T10:30:00-03:00", "plan_written"),
                                ev("2026-10-03T11:00:00-03:00", "released"),
                                ev("2026-10-03T12:00:00-03:00", "delivered")])
        self.assertEqual([r["wall_s"] for r in rows], [1800, 1800, 3600])
        for r in rows:
            self.assertIsNone(r["waiting_s"], r)
            self.assertIsNone(r["work_s"], r)
        _, _, wall, waiting, work = timeline.total(rows)
        self.assertEqual((wall, waiting, work), (7200, None, None))
        self.assertTrue(timeline.render(rows, "en").splitlines()[-1].endswith("| — | — |"))

    def test_one_prompt_is_enough_to_measure_waiting(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start"),
                                ev("2026-10-03T10:10:00-03:00", "prompt"),
                                ev("2026-10-03T10:30:00-03:00", "plan_written")])
        self.assertEqual(rows[0]["waiting_s"], 0)

    MEASURED = [("2026-10-03T10:00:00-03:00", "start"), ("2026-10-03T11:00:00-03:00", "released"),
                ("2026-10-03T11:40:00-03:00", "first_commit"), ("2026-10-03T12:30:00-03:00", "commit"),
                ("2026-10-03T13:00:00-03:00", "review"), ("2026-10-03T13:30:00-03:00", "validation"),
                ("2026-10-03T14:30:00-03:00", "delivered")]

    def test_commits_during_implementation_do_not_open_delivery(self):
        rows = timeline.report([ev(t, e) for t, e in self.MEASURED])
        self.assertEqual([(r["phase"], r["start"][11:16], r["end"][11:16]) for r in rows],
                         [("discussion", "10:00", "11:00"), ("implementation", "11:00", "13:00"),
                          ("review", "13:00", "13:30"), ("validation", "13:30", "14:30")])
        for a, b in zip(rows, rows[1:]):
            self.assertEqual(a["end"], b["start"])          # no overlap: the phases tile the span
        self.assertEqual(timeline.total(rows)[2], 4 * 3600 + 1800)

    def test_delivery_opens_at_the_first_commit_after_every_earlier_phase(self):
        events = self.MEASURED[:-1] + [("2026-10-03T14:00:00-03:00", "push"),
                                       ("2026-10-03T14:30:00-03:00", "delivered")]
        rows = timeline.report([ev(t, e) for t, e in events])
        self.assertEqual([(r["phase"], r["start"][11:16], r["end"][11:16]) for r in rows][-2:],
                         [("validation", "13:30", "14:00"), ("delivery", "14:00", "14:30")])

    def test_delivered_is_the_first_one_after_the_last_phase_starts(self):
        rows = timeline.report([ev("2026-10-03T10:00:00-03:00", "start"),
                                ev("2026-10-03T10:30:00-03:00", "delivered"),      # a stray, earlier one
                                ev("2026-10-03T11:00:00-03:00", "plan_written"),
                                ev("2026-10-03T11:30:00-03:00", "released"),
                                ev("2026-10-03T13:00:00-03:00", "delivered"),
                                ev("2026-10-03T13:10:00-03:00", "delivered")])
        self.assertEqual(rows[-1]["phase"], "implementation")
        self.assertEqual(rows[-1]["end"][11:16], "13:00")
        self.assertEqual(timeline.total(rows)[2], 3 * 3600)


class TestTimelineFiles(unittest.TestCase):
    def test_append_never_rewrites(self):
        with temp_state() as env, fake_repo() as repo, mock.patch.dict(os.environ, env):
            timeline.append(repo, "start", "phase", now="2026-10-03T10:00:00-03:00")
            path = timeline.path(repo, timeline.demand(repo))
            with open(path) as f:
                first = f.read()
            timeline.append(repo, "review", "phase", now="2026-10-03T10:05:00-03:00")
            with open(path) as f:
                now = f.read()
            self.assertTrue(now.startswith(first))
            self.assertEqual(len(now.splitlines()), 2)

    def test_first_commit_after_the_plan_is_stamped_without_a_dry_call(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env,
                stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))   # plan_written records head
            subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            payload = json.dumps({"hook_event_name": "PostToolUse", "cwd": repo, "tool_name": "Bash",
                                  "tool_input": {"command": "git commit -m x"}, "tool_response": {"stdout": ""}})
            run("timeline.py", "hook", "bash", stdin=payload, env=env)
            run("timeline.py", "hook", "bash", stdin=payload, env=env)      # same HEAD again
            with mock.patch.dict(os.environ, env):
                names = [e["event"] for e in timeline.events(repo, timeline.demand(repo))]
            self.assertIn("plan_written", names)
            self.assertEqual(names.count("first_commit"), 1)
            self.assertNotIn("commit", names)

    def test_hooks_never_block_even_on_garbage(self):
        with temp_state() as env:
            rc, _, _ = run("timeline.py", "hook", "bash", stdin="{not json", env=env)
            self.assertEqual(rc, 0)

    def test_gate_emits_plan_spec_approval_and_release_events(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            for path in (plan, spec):
                run("plan_gate.py", "mark", env=env,
                    stdin=hook_input("Write", {"file_path": path}, repo, "PostToolUse"))
            run("plan_gate.py", "release", plan, "--reason", "ok", env=env)
            with mock.patch.dict(os.environ, env):
                names = [e["event"] for e in timeline.events(repo, timeline.demand(repo))]
            for event in ("plan_written", "spec_written", "released"):
                self.assertIn(event, names)

    # -- beyond the brief --------------------------------------------------------------------------

    def names(self, env, repo):
        with mock.patch.dict(os.environ, env):
            return [e["event"] for e in timeline.events(repo, timeline.demand(repo))]

    def test_run_checks_stamps_approved_for_a_plan_and_spec_approved_for_a_spec(self):
        with temp_state() as env, fake_repo() as repo, tempfile.TemporaryDirectory() as tmp:
            manifest, _ = fake_check(tmp, "a", PASS, applies_to=("plan", "spec"))
            rc, _, err = run("plan_gate.py", "register-check", manifest, env=env)
            self.assertEqual(rc, 0, err)
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            rc, out, err = run("plan_gate.py", "run-checks", spec, env=env)
            self.assertEqual((rc, state_of(spec, env)["status"]), (0, "approved"), out + err)
            self.assertEqual(self.names(env, repo), ["spec_approved"])
            plan = fake_plan(repo)
            rc, out, err = run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual((rc, state_of(plan, env)["status"]), (0, "approved"), out + err)
            self.assertEqual(self.names(env, repo), ["spec_approved", "approved"])

    def test_releasing_a_spec_stamps_spec_released_not_released(self):
        with temp_state() as env, fake_repo() as repo:
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            rc, out, err = run("plan_gate.py", "release", spec, "--reason", "ok", env=env)
            self.assertEqual((rc, state_of(spec, env)["status"]), (0, "released"), out + err)
            names = self.names(env, repo)
            self.assertIn("spec_released", names)
            self.assertNotIn("released", names)

    def test_prompt_and_stop_hooks_are_silent_inside_and_outside_a_repo(self):
        with temp_state() as env, fake_repo() as repo, \
             tempfile.TemporaryDirectory(prefix="plan-gate-norepo-") as outside:
            for cwd in (repo, os.path.realpath(outside)):
                for kind, name in (("prompt", "UserPromptSubmit"), ("stop", "Stop")):
                    payload = json.dumps({"hook_event_name": name, "cwd": cwd, "prompt": "hi"})
                    rc, out, err = run("timeline.py", "hook", kind, stdin=payload, env=env, cwd=cwd)
                    self.assertEqual((rc, out, err), (0, "", ""), (cwd, kind))
            with mock.patch.dict(os.environ, env):
                names = [e["event"] for e in timeline.events(repo, timeline.demand(repo))]
            self.assertEqual(names, ["prompt", "stop"])
            # outside a repo it is a no-op: the only timeline folder is the repo's
            self.assertEqual(len(os.listdir(os.path.join(env["PLAN_GATE_DIR"], "timeline"))), 1)

    def test_hook_errors_are_logged_never_printed(self):
        with temp_state() as env, fake_repo() as repo:
            with open(os.path.join(env["PLAN_GATE_DIR"], "timeline"), "w") as f:
                f.write("a file where the folder should be")
            payload = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": repo, "prompt": "hi"})
            rc, out, err = run("timeline.py", "hook", "prompt", stdin=payload, env=env)
            self.assertEqual((rc, out, err), (0, "", ""))
            with open(os.path.join(env["PLAN_GATE_DIR"], "errors.log")) as f:
                self.assertIn("timeline", f.read())

    def test_a_failing_timeline_never_undoes_a_release(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env,
                stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            shutil.rmtree(os.path.join(env["PLAN_GATE_DIR"], "timeline"))     # mark stamped plan_written
            with open(os.path.join(env["PLAN_GATE_DIR"], "timeline"), "w") as f:
                f.write("a file where the folder should be")
            rc, _, _ = run("plan_gate.py", "release", plan, "--reason", "ok", env=env)
            self.assertEqual(rc, 0)
            self.assertEqual(state_of(plan, env)["status"], "released")
            with open(os.path.join(env["PLAN_GATE_DIR"], "errors.log")) as f:
                self.assertIn("timeline", f.read())

    def test_push_is_stamped_only_when_the_upstream_is_head(self):
        with temp_state() as env, fake_repo() as repo, \
             tempfile.TemporaryDirectory(prefix="plan-gate-remote-") as remote:
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", remote], check=True)
            payload = json.dumps({"hook_event_name": "PostToolUse", "cwd": repo, "tool_name": "Bash",
                                  "tool_input": {"command": "git push -u origin main"}})
            run("timeline.py", "hook", "bash", stdin=payload, env=env)                  # no upstream yet
            subprocess.run(["git", "-C", repo, "push", "-q", "-u", "origin", "main"], check=True,
                           capture_output=True)
            run("timeline.py", "hook", "bash", stdin=payload, env=env)
            with mock.patch.dict(os.environ, env):
                names = [e["event"] for e in timeline.events(repo, timeline.demand(repo))]
            self.assertEqual(names, ["push"])

    def test_a_later_commit_is_commit_not_first_commit(self):
        with temp_state() as env, fake_repo() as repo:
            prompt(repo, env)
            payload = bash_payload(repo, "git add x && git commit -m y")
            for _ in range(2):
                subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
                run("timeline.py", "hook", "bash", stdin=payload, env=env)
            self.assertEqual(self.names(env, repo), ["prompt", "first_commit", "commit"])

    def test_an_amend_is_a_commit(self):
        with temp_state() as env, fake_repo() as repo:
            prompt(repo, env)
            git(repo, "commit", "--allow-empty", "-m", "x")
            run("timeline.py", "hook", "bash", stdin=bash_payload(repo, "git commit -m x"), env=env)
            git(repo, "commit", "--amend", "--allow-empty", "-m", "y")
            run("timeline.py", "hook", "bash", stdin=bash_payload(repo, "git commit --amend -m y"), env=env)
            self.assertEqual(self.names(env, repo), ["prompt", "first_commit", "commit"])

    def test_a_failed_commit_on_a_new_branch_is_not_a_first_commit(self):
        # R46 probe (a): no baseline on the new branch, and HEAD did not move
        with temp_state() as env, fake_repo() as repo:
            prompt(repo, env)
            git(repo, "checkout", "-b", "feat/9-x")
            rc = subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "nothing"], capture_output=True).returncode
            self.assertNotEqual(rc, 0)
            run("timeline.py", "hook", "bash", stdin=bash_payload(repo, "git commit -m nothing; echo done"), env=env)
            self.assertNotIn("first_commit", self.names(env, repo))
            self.assertEqual(self.names(env, repo), [])

    def test_a_reset_then_a_failed_commit_is_not_a_commit(self):
        # R46 probe (b): HEAD moved, but by a reset
        with temp_state() as env, fake_repo() as repo:
            git(repo, "commit", "--allow-empty", "-m", "second")
            prompt(repo, env)
            git(repo, "reset", "-q", "--hard", "HEAD~1")
            run("timeline.py", "hook", "bash",
                stdin=bash_payload(repo, "git reset --hard HEAD~1 && git commit -m z || true"), env=env)
            self.assertEqual(self.names(env, repo), ["prompt"])

    def test_git_dash_c_and_cd_use_that_directory(self):
        # R47: the repo of the stamp is the one the command named, relative to the hook's cwd
        with temp_state() as env, tempfile.TemporaryDirectory(prefix="plan-gate-outer-") as outer:
            outer = os.path.realpath(outer)
            repo = os.path.join(outer, "sub")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "T")
            git(repo, "commit", "--allow-empty", "-m", "init")
            prompt(repo, env)
            git(repo, "commit", "--allow-empty", "-m", "a")
            run("timeline.py", "hook", "bash", stdin=bash_payload(outer, "git -C sub commit -m a"), env=env)
            git(repo, "commit", "--allow-empty", "-m", "b")
            run("timeline.py", "hook", "bash", stdin=bash_payload(outer, "cd sub && git commit -m b"), env=env)
            with tempfile.TemporaryDirectory(prefix="plan-gate-remote-") as remote:
                git(repo, "remote", "add", "origin", remote)
                subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
                git(repo, "push", "-q", "-u", "origin", "main")
                run("timeline.py", "hook", "bash", stdin=bash_payload(outer, "cd sub && git push"), env=env)
                run("timeline.py", "hook", "bash", stdin=bash_payload(outer, "git -C sub push"), env=env)
            self.assertEqual(self.names(env, repo), ["prompt", "first_commit", "commit", "push", "push"])
            self.assertFalse(os.path.exists(os.path.join(outer, ".git")))

    def test_a_broken_sibling_module_still_exits_0_silently(self):
        # R42: an import failure of i18n/config is the hook's failure too -- rc 0, nothing printed
        from .helpers import BIN
        for broken in ("i18n.py", "config.py"):
            with temp_state() as env, fake_repo() as repo, tempfile.TemporaryDirectory() as copy:
                for name in os.listdir(BIN):
                    if name.endswith(".py"):
                        shutil.copy(os.path.join(BIN, name), copy)
                with open(os.path.join(copy, broken), "w") as f:
                    f.write("raise ImportError('broken on purpose')\n")
                for kind in ("prompt", "stop", "bash"):
                    payload = bash_payload(repo, "git commit -m x") if kind == "bash" else \
                        json.dumps({"hook_event_name": "Stop", "cwd": repo})
                    rc, out, err = run(os.path.join(copy, "timeline.py"), "hook", kind, stdin=payload, env=env)
                    self.assertEqual((rc, out, err), (0, "", ""), (broken, kind))

    def test_every_event_records_head(self):
        with temp_state() as env, fake_repo() as repo, mock.patch.dict(os.environ, env):
            timeline.append(repo, "start", "phase", now="2026-10-03T10:00:00-03:00")
            head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()
            self.assertEqual(timeline.events(repo, timeline.demand(repo))[0]["head"], head)

    def test_a_branch_with_a_slash_becomes_a_nested_folder(self):
        with temp_state() as env, fake_repo() as repo, mock.patch.dict(os.environ, env):
            subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "feat/58-x"], check=True)
            self.assertEqual(timeline.demand(repo), "feat/58-x")
            timeline.append(repo, "start", "phase")
            p = timeline.path(repo, "feat/58-x")
            self.assertTrue(p.endswith(os.path.join("feat", "58-x.jsonl")))
            self.assertTrue(os.path.isfile(p))
            folder = os.path.basename(os.path.dirname(os.path.dirname(p)))
            self.assertRegex(folder, r"^" + os.path.basename(repo) + r"-[0-9a-f]{8}$")

    def test_detached_head_is_its_own_demand(self):
        with fake_repo() as repo:
            subprocess.run(["git", "-C", repo, "checkout", "-q", "--detach"], check=True)
            self.assertEqual(timeline.demand(repo), "detached")


if __name__ == "__main__":
    unittest.main()
