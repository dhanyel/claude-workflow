import json, os, unittest
from unittest import mock
from .helpers import isolated_env, run, fake_repo, write, import_bin

state = import_bin("state")


class TestCheck(unittest.TestCase):
    def check(self, env, plan, h):
        rc, out, err = run("check.py", plan, env=dict(env, PLAN_GATE_CONTENT_HASH=h))
        return rc, json.loads(out), err

    def with_verdict(self, env, plan, h, entry):
        with mock.patch.dict(os.environ, env, clear=True):
            state.record_verdict(plan, h, entry)

    def test_no_review_fails_and_names_the_command(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual((rc, data["status"]), (1, "fail"))
            self.assertIn("review.py", json.dumps(data))

    def test_approved_for_this_hash_passes(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "APPROVED", "blockers": 0, "round": 1})
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual((rc, data), (0, {"status": "pass", "findings": []}))

    def test_approved_for_another_hash_fails(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "APPROVED", "blockers": 0, "round": 1})
            rc, data, _ = self.check(env, plan, "d" * 64)
            self.assertEqual((rc, data["status"]), (1, "fail"))

    def test_rejected_fails_with_the_review_file(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "REJECTED", "blockers": 2, "round": 1,
                                                    "review_md": "docs/superpowers/plans/reviews/x-codex-round-1.md"})
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual(rc, 1)
            self.assertIn("x-codex-round-1.md", json.dumps(data))

    def test_ceiling_reached_names_despite_check(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "REJECTED", "blockers": 1, "round": 3})
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual(rc, 1)
            self.assertIn("--despite-check adversarial-review", json.dumps(data))

    def test_printed_commands_keep_a_hostile_file_name_one_inert_word(self):   # final review I2 (R17)
        import subprocess
        from .test_round import wire
        from .helpers import BIN, GATE_BIN
        env = isolated_env()
        wire(env)
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans", "p $(touch pwned) `touch pwned` 'q'.md"), "# P\n")
            commands = {}
            for entry in ({"verdict": "REJECTED", "blockers": 1, "round": 1},
                          {"verdict": "REJECTED", "blockers": 1, "round": 3}):
                self.with_verdict(env, plan, "c" * 64, entry)
                _, data, _ = self.check(env, plan, "c" * 64)
                text = data["findings"][0]["items"][0]
                line = [l.strip() for l in text.splitlines() if l.strip().startswith("python3 ")][0]
                # the printed line, run by bash with `python3` replaced by a function that prints its argv:
                # what bash hands over is exactly what the command would get -- and nothing expanded or ran
                argv = subprocess.run(["bash", "-c", "python3() { printf '%s\\0' \"$@\"; }; " + line],
                                      capture_output=True, text=True, cwd=env["HOME"]).stdout.split("\0")[:-1]
                commands[entry["round"]] = argv
            self.assertEqual(commands[1], [os.path.join(BIN, "review.py"), plan])
            self.assertEqual(commands[3][:3], [os.path.join(GATE_BIN, "plan_gate.py"), "release", plan])
            self.assertFalse(os.path.exists(os.path.join(env["HOME"], "pwned")))

    def test_the_ceiling_without_a_reachable_gate_still_names_no_bare_script(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "REJECTED", "blockers": 1, "round": 3})
            _, data, _ = self.check(env, plan, "c" * 64)
            text = data["findings"][0]["items"][0]
            self.assertIn("<plan-gate>/bin/plan_gate.py release", text)

    def test_missing_hash_fails_closed(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            self.with_verdict(env, plan, "c" * 64, {"verdict": "APPROVED", "blockers": 0, "round": 1})
            rc, out, _ = run("check.py", plan, env=env)
            self.assertEqual((rc, json.loads(out)["status"]), (1, "fail"))

    def test_corrupt_state_fails_closed(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            with mock.patch.dict(os.environ, env, clear=True):
                os.makedirs(state.state_dir(), exist_ok=True)
                write(state.state_path(plan), "[]")
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual((rc, data["status"]), (1, "fail"))
            # the finding names the state folder: "unreadable" is told apart from "no review yet"
            self.assertIn(env["ADVERSARIAL_REVIEW_DIR"], json.dumps(data))

    def test_unparseable_state_fails_closed(self):
        env = isolated_env()
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
            with mock.patch.dict(os.environ, env, clear=True):
                os.makedirs(state.state_dir(), exist_ok=True)
                write(state.state_path(plan), "{half")
            rc, data, _ = self.check(env, plan, "c" * 64)
            self.assertEqual((rc, data["status"]), (1, "fail"))
            # the finding names the state folder: "unreadable" is told apart from "no review yet"
            self.assertIn(env["ADVERSARIAL_REVIEW_DIR"], json.dumps(data))

    def test_pass_needs_both_halves(self):
        bad = [{"verdict": "APPROVED", "blockers": 1, "round": 1}, {"verdict": "REJECTED", "blockers": 0, "round": 1},
               {"verdict": "APPROVED", "round": 1}, {"verdict": "APPROVED", "blockers": False, "round": 1},
               {"verdict": "APPROVED", "blockers": 0.0, "round": 1}, {"verdict": "APPROVED", "blockers": "0", "round": 1}]
        for entry in bad:
            with self.subTest(entry=entry):
                env = isolated_env()
                with fake_repo() as repo:
                    plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
                    self.with_verdict(env, plan, "c" * 64, entry)
                    rc, data, _ = self.check(env, plan, "c" * 64)
                    self.assertEqual((rc, data["status"]), (1, "fail"))

    def test_malformed_entries_fail_without_crashing(self):
        for entry in ("APPROVED", {"verdict": "REJECTED", "blockers": 1, "round": "two"}):
            with self.subTest(entry=entry):
                env = isolated_env()
                with fake_repo() as repo:
                    plan = write(os.path.join(repo, "docs/superpowers/plans/2026-01-01-x.md"), "# P\n")
                    self.with_verdict(env, plan, "c" * 64, entry)
                    rc, data, err = self.check(env, plan, "c" * 64)
                    self.assertEqual((rc, data["status"]), (1, "fail"))
                    self.assertNotIn("Traceback", err)
                    self.assertIn(env["ADVERSARIAL_REVIEW_DIR"], json.dumps(data))
