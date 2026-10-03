import json, os, tempfile, unittest
from .helpers import run, temp_state, fake_repo, fake_plan, hook_input, state_of, import_bin, denied, PLUGIN

gate = import_bin("plan_gate")
PASS = '{"status":"pass","findings":[]}'
FAIL = '{"status":"fail","findings":[{"group":"x","state":"findings","count":1,"items":["y"]}]}'

def fake_check(directory, check_id, output, required=True, applies_to=("plan",)):
    """A check script printing `output` - only when it receives an existing file as its last argument, so a
    check that is not handed the path fails. Returns (manifest, script)."""
    script = os.path.join(directory, f"{check_id}.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\n[ -f \"$1\" ] || { echo 'no path given'; exit 1; }\nprintf '%s' '" + output + "'\n")
    os.chmod(script, 0o755)
    manifest = os.path.join(directory, f"{check_id}.json")
    with open(manifest, "w") as f:
        json.dump({"id": check_id, "command": script, "required": required, "applies_to": list(applies_to)}, f)
    return manifest, script

class TestChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def register(self, env, manifest):
        rc, _, err = run("plan_gate.py", "register-check", manifest, env=env)
        self.assertEqual(rc, 0, err)

    def write_denied(self, env, repo):
        rc, _, _ = run("plan_gate.py", "check", env=env,
                       stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
        return denied(rc)

    def test_register_is_idempotent_and_resolves_the_command(self):
        with temp_state() as env:
            m = os.path.join(self.tmp.name, "m.json")
            with open(m, "w") as f:
                json.dump({"id": "c1", "command": "${CLAUDE_PLUGIN_ROOT}/bin/x --json",
                           "required": True, "applies_to": ["plan"]}, f)
            env = dict(env, CLAUDE_PLUGIN_ROOT="/opt/p")
            self.register(env, m)
            self.register(env, m)
            d = os.path.join(env["PLAN_GATE_DIR"], "checks.d")
            self.assertEqual(os.listdir(d), ["c1.json"])
            with open(os.path.join(d, "c1.json")) as f:
                self.assertEqual(json.load(f)["command"], "/opt/p/bin/x --json")

    def test_session_start_registers_the_plugin_preflights(self):
        with temp_state() as env:
            env = dict(env, CLAUDE_PLUGIN_ROOT=PLUGIN)
            rc, _, err = run("plan_gate.py", "register-checks", env=env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(sorted(os.listdir(os.path.join(env["PLAN_GATE_DIR"], "checks.d"))),
                             ["preflight-spec.json", "preflight.json"])

    def test_plan_is_approved_only_when_every_required_check_passes(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            self.register(env, fake_check(self.tmp.name, "b", FAIL)[0])
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")
            fake_check(self.tmp.name, "b", PASS)          # same script path, now passing
            run("plan_gate.py", "run-checks", plan, env=env)
            st = state_of(plan, env)
            self.assertEqual(st["status"], "approved")
            self.assertEqual(st["plan_hash"], gate.content_hash(plan))
            self.assertFalse(self.write_denied(env, repo))

    def test_no_checks_means_no_automatic_approval(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")
            self.assertTrue(self.write_denied(env, repo))

    def test_real_preflight_does_not_approve_a_prose_only_plan(self):
        with temp_state() as env, fake_repo() as repo:
            env = dict(env, CLAUDE_PLUGIN_ROOT=PLUGIN)
            run("plan_gate.py", "register-checks", env=env)
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertEqual(state_of(plan, env)["status"], "pending")
            self.assertTrue(self.write_denied(env, repo))

    def test_editing_the_plan_revokes_the_approval(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertFalse(self.write_denied(env, repo))
            with open(plan, "a") as f:
                f.write("\n### Task 99: added after approval\n")
            self.assertTrue(self.write_denied(env, repo))

    def test_execution_log_edits_do_not_revoke(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            plan = fake_plan(repo, extra="\n## Execution log\n")
            run("plan_gate.py", "run-checks", plan, env=env)
            with open(plan, "a") as f:
                f.write("| 10:00 | Task 1 | started |\n")
            self.assertFalse(self.write_denied(env, repo))

    def test_check_that_prints_garbage_fails_closed(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", "ok")[0])
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            st = state_of(plan, env)
            self.assertEqual(st["status"], "pending")
            self.assertEqual(st["checks"]["a"]["status"], "fail")

    def test_optional_by_repo_config_does_not_count(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            self.register(env, fake_check(self.tmp.name, "b", FAIL)[0])
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"checks": {"b": {"required": False}}}, f)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")

    def test_spec_is_approved_by_spec_checks_only(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "plan-only", FAIL, applies_to=("plan",))[0])
            self.register(env, fake_check(self.tmp.name, "spec-only", PASS, applies_to=("spec",))[0])
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            run("plan_gate.py", "run-checks", spec, env=env)
            self.assertEqual(state_of(spec, env)["status"], "approved")

    def test_real_preflight_approves_a_plan_with_something_to_check(self):
        with temp_state() as env, fake_repo() as repo:
            env = dict(env, CLAUDE_PLUGIN_ROOT=PLUGIN)
            run("plan_gate.py", "register-checks", env=env)
            with open(os.path.join(repo, "app.py"), "w") as f:
                f.write("def hello():\n    return 1\n")
            fence = "`" * 3                          # a literal fence would close this Markdown block
            # Test-plan adjustment (brief's warning): with ONLY the bash block the real preflight reports
            # invoked-symbols and decorative-constants as not_evaluated ("a code block, none in a language in
            # the table"), rc 2. A python block with the code the task creates gives them something to check.
            plan = fake_plan(repo, extra="\n**Files:**\n- Create: `app.py`\n\n"
                             + fence + "python\ndef hello():\n    return 1\n" + fence + "\n\n"
                             + fence + "bash\ngit add app.py\n" + fence + "\n")
            run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertEqual(state_of(plan, env)["status"], "approved")

    def test_orphan_check_blocks_and_is_reported(self):
        with temp_state() as env, fake_repo() as repo:
            gone = os.path.join(self.tmp.name, "gone.py")
            manifest = os.path.join(self.tmp.name, "gone.json")
            with open(manifest, "w") as f:          # python3 exists; the script it would run does not
                json.dump({"id": "gone", "command": f'python3 "{gone}"', "required": True,
                           "applies_to": ["plan"]}, f)
            self.register(env, manifest)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")
            rc, _, err = run("plan_gate.py", "check", env=env,
                             stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))
            self.assertIn("prune", err)
            _, out, _ = run("plan_gate.py", "checks", "list", env=env)
            self.assertIn("gone", out)

    def test_prune_removes_only_orphans(self):
        with temp_state() as env:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            manifest, script = fake_check(self.tmp.name, "gone", PASS)
            self.register(env, manifest)
            os.remove(script)
            run("plan_gate.py", "checks", "prune", env=env)
            self.assertEqual(os.listdir(os.path.join(env["PLAN_GATE_DIR"], "checks.d")), ["a.json"])

    def test_human_release_still_works_without_checks(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertTrue(self.write_denied(env, repo))
            rc, _, err = run("plan_gate.py", "release", plan, "--reason", "approved in chat", env=env)
            self.assertEqual(rc, 0, err)
            self.assertFalse(self.write_denied(env, repo))


# ---------------------------------------------------------------------------------------------------
# Controller rulings R17, R31, R32, R33 (beyond the brief's 14 tests).
# ---------------------------------------------------------------------------------------------------
import shutil, signal, subprocess, sys, time
from unittest import mock
from .helpers import BIN, _isolated



class _LazyChecks:
    """bin/checks.py, imported on first use (so a missing module fails each test, not the whole file)."""
    def __getattr__(self, name):
        return getattr(import_bin("checks"), name)


checks = _LazyChecks()


def write_manifest(directory, check_id, command, **extra):
    manifest = os.path.join(directory, f"{check_id}.json")
    with open(manifest, "w") as f:
        json.dump(dict({"id": check_id, "command": command}, **extra), f)
    return manifest


def script(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


class TestCheckRulings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def register(self, env, manifest):
        rc, _, err = run("plan_gate.py", "register-check", manifest, env=env)
        self.assertEqual(rc, 0, err)

    def checks_dir(self, env):
        return os.path.join(env["PLAN_GATE_DIR"], "checks.d")

    # R17 -- the findings of a failing check survive its non-zero exit
    def test_failing_check_keeps_its_findings_whatever_the_exit_code(self):
        s = script(self.tmp.name, "f.sh", "printf '%s' '" + FAIL + "'\nexit 1\n")
        target = os.path.join(self.tmp.name, "plan.md")
        open(target, "w").close()
        res = checks.run({"id": "f", "command": s}, target)
        self.assertEqual(res["status"], "fail")
        self.assertEqual([g["group"] for g in res["findings"]], ["x"])

    def test_real_preflight_fail_keeps_its_groups_not_a_check_error(self):
        with fake_repo() as repo:
            plan = fake_plan(repo)
            check = {"id": "preflight",
                     "command": f'python3 "{os.path.join(BIN, "preflight_plan.py")}" --json'}
            res = checks.run(check, plan)
            groups = [g["group"] for g in res["findings"]]
            self.assertEqual(res["status"], "fail")
            self.assertIn("nothing-to-check", groups)
            self.assertNotIn("check-error", groups)

    def test_invalid_status_and_missing_command_are_check_errors(self):
        target = os.path.join(self.tmp.name, "plan.md")
        open(target, "w").close()
        bad = script(self.tmp.name, "bad.sh", "printf '%s' '{\"status\":\"maybe\",\"findings\":[]}'\n")
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}):
            for check in ({"id": "bad", "command": bad},
                          {"id": "nope", "command": "plan-gate-no-such-command-xyz --json"}):
                res = checks.run(check, target)
                self.assertEqual(res["status"], "fail", check)
                self.assertEqual(res["findings"][0]["group"], "check-error", check)
                self.assertTrue(res["findings"][0]["items"][0], check)

    # R31 -- timeout, and pending written BEFORE the checks run
    def test_check_past_the_timeout_fails_closed(self):
        self.assertEqual(checks.CHECK_TIMEOUT, 45)
        with temp_state() as env, fake_repo() as repo:
            slow = script(self.tmp.name, "slow.sh", "sleep 5\nprintf '%s' '" + PASS + "'\n")
            self.register(env, write_manifest(self.tmp.name, "slow", slow, required=True, applies_to=["plan"]))
            plan = fake_plan(repo)
            # PLAN_GATE_TEST_CHECK_TIMEOUT: test-only, honored only when SHORTER than CHECK_TIMEOUT
            run("plan_gate.py", "run-checks", plan, env=dict(env, PLAN_GATE_TEST_CHECK_TIMEOUT="1"))
            st = state_of(plan, env)
            self.assertEqual(st["status"], "pending")
            self.assertEqual(st["checks"]["slow"]["status"], "fail")

    def test_test_timeout_override_can_only_shorten(self):
        with mock.patch.dict(os.environ, {"PLAN_GATE_TEST_CHECK_TIMEOUT": "999"}):
            self.assertEqual(checks.timeout(), checks.CHECK_TIMEOUT)
        with mock.patch.dict(os.environ, {"PLAN_GATE_TEST_CHECK_TIMEOUT": "2"}):
            self.assertEqual(checks.timeout(), 2)

    def test_mark_killed_during_a_check_leaves_the_plan_pending(self):
        with temp_state() as env, fake_repo() as repo:
            started = os.path.join(self.tmp.name, "started")
            slow = script(self.tmp.name, "hang.sh", f"touch '{started}'\nsleep 20\nprintf '%s' '{PASS}'\n")
            self.register(env, write_manifest(self.tmp.name, "hang", slow, required=True, applies_to=["plan"]))
            plan = fake_plan(repo)                      # never marked before: no state file at all
            p = subprocess.Popen([sys.executable, os.path.join(BIN, "plan_gate.py"), "mark"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=_isolated(env), start_new_session=True)
            p.stdin.write(hook_input("Write", {"file_path": plan}, repo, "PostToolUse").encode())
            p.stdin.close()
            deadline = time.monotonic() + 15
            while not os.path.exists(started) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(started), "the check never started")
            os.killpg(p.pid, signal.SIGKILL)            # what a hook timeout does to the hook
            p.wait()
            self.assertEqual(state_of(plan, env)["status"], "pending")
            rc, _, _ = run("plan_gate.py", "check", env=env,
                           stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))

    def test_hooks_give_mark_a_timeout_above_the_checks_and_register_on_session_start(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        mark = [h for group in hooks["PostToolUse"] for h in group["hooks"] if h["command"].endswith(" mark")]
        self.assertEqual(len(mark), 1)
        self.assertGreaterEqual(mark[0]["timeout"], 3 * checks.CHECK_TIMEOUT)
        start = [h["command"] for group in hooks["SessionStart"] for h in group["hooks"]]
        self.assertEqual(start, ['python3 "${CLAUDE_PLUGIN_ROOT}/bin/plan_gate.py" register-checks'])

    # R32 -- a corrupted manifest is a required check that fails, never skipped
    def test_corrupted_manifests_fail_closed(self):
        for name, content in (("broken", "{not json"),
                              ("no-command", json.dumps({"id": "no-command", "required": True})),
                              ("no-id", json.dumps({"command": "/bin/true"}))):
            with self.subTest(name), temp_state() as env, fake_repo() as repo:
                self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
                with open(os.path.join(self.checks_dir(env), name + ".json"), "w") as f:
                    f.write(content)
                plan = fake_plan(repo)
                run("plan_gate.py", "run-checks", plan, env=env)
                st = state_of(plan, env)
                self.assertEqual(st["status"], "pending")
                self.assertEqual(st["checks"][name]["status"], "fail")
                self.assertEqual(st["checks"]["a"]["status"], "pass")

    def test_corrupted_manifest_cannot_be_made_optional_by_the_repo(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            with open(os.path.join(self.checks_dir(env), "broken.json"), "w") as f:
                f.write("{not json")
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"checks": {"broken": {"required": False}}}, f)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_unknown_or_missing_applies_to_counts_as_applicable(self):
        for applies_to in (None, "everything", ["something-else", 3]):
            with self.subTest(applies_to), temp_state() as env, fake_repo() as repo:
                self.register(env, fake_check(self.tmp.name, "a", PASS, applies_to=("spec",))[0])
                fail = script(self.tmp.name, "u.sh", "printf '%s' '" + FAIL + "'\n")
                extra = {} if applies_to is None else {"applies_to": applies_to}
                self.register(env, write_manifest(self.tmp.name, "u", fail, required=True, **extra))
                spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
                with open(spec, "w") as f:
                    f.write("# Spec\n")
                run("plan_gate.py", "run-checks", spec, env=env)
                self.assertEqual(state_of(spec, env)["status"], "pending")
                self.assertEqual(state_of(spec, env)["checks"]["u"]["status"], "fail")

    def test_list_shows_corrupted_and_prune_keeps_them_saying_so(self):
        with temp_state() as env:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            manifest, gone = fake_check(self.tmp.name, "gone", PASS)
            self.register(env, manifest)
            os.remove(gone)
            with open(os.path.join(self.checks_dir(env), "broken.json"), "w") as f:
                f.write("{not json")
            env = dict(env, PLAN_GATE_LANG="en")
            rc, out, _ = run("plan_gate.py", "checks", "list", env=env)
            self.assertEqual(rc, 0)
            self.assertIn("broken", out)
            self.assertIn("gone", out)
            rc, out, _ = run("plan_gate.py", "checks", "prune", env=env)
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(os.listdir(self.checks_dir(env))), ["a.json", "broken.json"])
            self.assertIn("gone", out)
            self.assertIn("broken", out)            # named as NOT removed
            self.assertIn("not removed", out)

    # R33 -- exit codes and silent stdout
    def test_mark_exits_0_when_approved_and_2_when_pending(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            plan = fake_plan(repo)
            rc, out, err = run("plan_gate.py", "mark", env=env,
                               stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertEqual((rc, out), (0, ""), err)
            self.assertEqual(state_of(plan, env)["status"], "approved")
            fake_check(self.tmp.name, "a", FAIL)
            with open(plan, "a") as f:
                f.write("\n### Task 2: more\n")
            rc, out, err = run("plan_gate.py", "mark", env=dict(env, PLAN_GATE_LANG="en"),
                               stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("PENDING", err)

    def test_register_checks_never_writes_stdout_even_on_error(self):
        with temp_state() as env:
            env = dict(env, CLAUDE_PLUGIN_ROOT=PLUGIN)
            rc, out, _ = run("plan_gate.py", "register-checks", env=env)
            self.assertEqual((rc, out), (0, ""))
            shutil.rmtree(self.checks_dir(env))
            with open(os.path.join(env["PLAN_GATE_DIR"], "checks.d"), "w") as f:
                f.write("a file where the folder should be")
            rc, out, err = run("plan_gate.py", "register-checks", env=env)
            self.assertEqual((rc, out), (0, ""))
            self.assertTrue(err.strip())

    def test_mark_of_an_approved_spec_with_the_same_hash_keeps_it(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "s", PASS, applies_to=("spec",))[0])
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            rc, _, err = run("plan_gate.py", "mark", env=env,
                             stdin=hook_input("Write", {"file_path": spec}, repo, "PostToolUse"))
            self.assertEqual(rc, 0, err)
            self.assertEqual(state_of(spec, env)["status"], "approved")
            plan = fake_plan(repo)
            rc, _, _ = run("plan_gate.py", "check", env=env,
                           stdin=hook_input("Write", {"file_path": plan}, repo))
            self.assertFalse(denied(rc))           # an approved spec does not block writing the plan


# ---------------------------------------------------------------------------------------------------
# Fix round 1: duplicate ids, pass with a non-zero exit (R34), required-but-unregistered (R35),
# silent register-check (R37).
# ---------------------------------------------------------------------------------------------------
class TestCheckFixRound1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def register(self, env, manifest):
        rc, out, err = run("plan_gate.py", "register-check", manifest, env=env)
        self.assertEqual((rc, out), (0, ""), err)
        return err

    def checks_dir(self, env):
        return os.path.join(env["PLAN_GATE_DIR"], "checks.d")

    def sub(self, name):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d, exist_ok=True)
        return d

    def test_duplicate_id_inside_one_manifest_is_refused(self):
        with temp_state() as env, fake_repo() as repo:
            fail = script(self.tmp.name, "f.sh", "printf '%s' '" + FAIL + "'\n")
            ok = script(self.tmp.name, "p.sh", "printf '%s' '" + PASS + "'\n")
            manifest = os.path.join(self.tmp.name, "dup.json")
            with open(manifest, "w") as f:
                json.dump([{"id": "x", "command": fail, "required": True, "applies_to": ["plan"]},
                           {"id": "x", "command": ok, "required": True, "applies_to": ["plan"]}], f)
            err = self.register(env, manifest)
            self.assertIn("x", err)
            self.assertFalse(os.path.exists(os.path.join(self.checks_dir(env), "x.json")))
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_same_id_from_another_manifest_is_a_failing_conflict(self):
        with temp_state() as env, fake_repo() as repo:
            first, _ = fake_check(self.sub("one"), "x", FAIL)
            second, _ = fake_check(self.sub("two"), "x", PASS)
            self.register(env, first)
            self.register(env, second)
            self.register(env, first)               # a later SessionStart does not undo the conflict
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            st = state_of(plan, env)
            self.assertEqual(st["status"], "pending")
            self.assertEqual(st["checks"]["x"]["status"], "fail")
            _, out, _ = run("plan_gate.py", "checks", "list", env=dict(env, PLAN_GATE_LANG="en"))
            self.assertIn(first, out)
            self.assertIn(second, out)
            rc, _, err = run("plan_gate.py", "check", env=dict(env, PLAN_GATE_LANG="en"),
                             stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))
            self.assertIn(first, err)

    def test_same_manifest_registered_again_stays_idempotent_and_records_its_source(self):
        with temp_state() as env, fake_repo() as repo:
            manifest, _ = fake_check(self.tmp.name, "x", PASS)
            self.register(env, manifest)
            self.register(env, manifest)
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                entry = json.load(f)
            self.assertEqual(entry["source"], os.path.abspath(manifest))
            self.assertNotIn("conflict", entry)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")

    # R34
    def test_pass_with_a_non_zero_exit_is_a_check_error(self):
        target = os.path.join(self.tmp.name, "plan.md")
        open(target, "w").close()
        s = script(self.tmp.name, "incoherent.sh", "printf '%s' '" + PASS + "'\nexit 1\n")
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}):
            res = checks.run({"id": "i", "command": s}, target)
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["findings"][0]["group"], "check-error")
        with temp_state() as env, fake_repo() as repo:
            self.register(env, write_manifest(self.tmp.name, "i", s, required=True, applies_to=["plan"]))
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    # R35
    def test_check_required_by_the_repo_but_not_registered_blocks_and_is_named(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"checks": {"adversarial-review": {"required": True}}}, f)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            st = state_of(plan, env)
            self.assertEqual(st["status"], "pending")
            self.assertEqual(st["checks"]["adversarial-review"]["status"], "fail")
            rc, _, err = run("plan_gate.py", "check", env=env,
                             stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))
            self.assertIn("adversarial-review", err)
            _, out, _ = run("plan_gate.py", "status", env=env, cwd=repo)
            self.assertIn("adversarial-review", out)

    def test_optional_override_without_a_manifest_is_ignored(self):
        with temp_state() as env, fake_repo() as repo:
            self.register(env, fake_check(self.tmp.name, "a", PASS)[0])
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"checks": {"adversarial-review": {"required": False}}}, f)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")

    # R37
    def test_register_check_never_writes_stdout(self):
        with temp_state() as env:
            rc, out, _ = run("plan_gate.py", "register-check", fake_check(self.tmp.name, "a", PASS)[0], env=env)
            self.assertEqual((rc, out), (0, ""))
            broken = os.path.join(self.tmp.name, "broken.json")
            with open(broken, "w") as f:
                f.write("{not json")
            rc, out, err = run("plan_gate.py", "register-check", broken, env=env)
            self.assertEqual((rc, out), (0, ""))
            self.assertTrue(err.strip())
            self.assertIn("broken", open(os.path.join(env["PLAN_GATE_DIR"], "errors.log")).read())

    def test_registering_over_a_corrupted_file_leaves_it_failing(self):
        with temp_state() as env, fake_repo() as repo:
            manifest, _ = fake_check(self.tmp.name, "x", PASS)
            os.makedirs(self.checks_dir(env))
            with open(os.path.join(self.checks_dir(env), "x.json"), "w") as f:
                f.write("{not json")
            err = self.register(env, manifest)
            self.assertIn("x.json", err)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    # R38: the owner is the plugin's version folder's PARENT, never the declared name.
    def plugin(self, *parts, name="plan-gate"):
        d = os.path.join(self.tmp.name, *parts)
        os.makedirs(os.path.join(d, ".claude-plugin"))
        with open(os.path.join(d, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": name, "version": parts[-1]}, f)
        return d

    def test_new_version_folder_under_the_same_parent_replaces_its_own_check(self):
        with temp_state() as env, fake_repo() as repo:
            old, _ = fake_check(self.plugin("plugins", "cache", "mk", "plan-gate", "1.0.0"), "x", FAIL)
            new, _ = fake_check(self.plugin("plugins", "cache", "mk", "plan-gate", "1.1.0"), "x", PASS)
            self.register(env, old)
            self.register(env, new)               # upgrade: same parent folder, new version folder
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                entry = json.load(f)
            self.assertNotIn("conflict", entry)
            self.assertEqual(entry["source"], new)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")

    def test_same_declared_name_under_another_parent_is_a_conflict(self):       # probe A
        with temp_state() as env, fake_repo() as repo:
            real, _ = fake_check(self.plugin("plugins", "cache", "mkA", "plan-gate", "1.0.0"), "x", FAIL)
            copy, _ = fake_check(self.plugin("plugins", "cache", "mkB", "plan-gate", "1.0.0"), "x", PASS)
            self.register(env, real)
            self.register(env, copy)
            self.register(env, real)              # hook order does not pick a winner (probe B)
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                self.assertEqual(json.load(f)["conflict"], [real, copy])
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_fake_plugin_json_elsewhere_is_a_conflict(self):                    # probe C
        with temp_state() as env, fake_repo() as repo:
            real, _ = fake_check(self.plugin("plugins", "cache", "mk", "plan-gate", "1.0.0"), "x", FAIL)
            fake, _ = fake_check(self.plugin("elsewhere", "1.0.0"), "x", PASS)
            self.register(env, real)
            self.register(env, fake)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")
            self.assertEqual(state_of(plan, env)["checks"]["x"]["status"], "fail")

    def test_register_check_with_wrong_arguments_exits_0_silently_and_logs(self):
        with temp_state() as env:
            for args in ((), ("a.json", "b.json")):
                rc, out, err = run("plan_gate.py", "register-check", *args, env=env)
                self.assertEqual((rc, out), (0, ""), args)
                self.assertTrue(err.strip(), args)
            with open(os.path.join(env["PLAN_GATE_DIR"], "errors.log")) as f:
                lines = f.read().splitlines()
            self.assertEqual(len([l for l in lines if " register-check: " in l]), 2, lines)

    # R39: outside plugins/cache/<marketplace>/<plugin>/<version> the owner is the plugin folder itself
    def test_sibling_dev_plugins_with_the_same_id_conflict_in_both_orders(self):
        for order in ((0, 1), (1, 0)):
            with self.subTest(order), temp_state() as env, fake_repo() as repo:
                base = f"dev{order[0]}"
                dirs = [self.plugin(base, "plugins", "plan-gate"), self.plugin(base, "plugins", "other")]
                manifests = [fake_check(dirs[0], "x", FAIL)[0], fake_check(dirs[1], "x", PASS)[0]]
                for i in order:
                    self.register(env, manifests[i])
                with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                    self.assertIn("conflict", json.load(f))
                plan = fake_plan(repo)
                run("plan_gate.py", "run-checks", plan, env=env)
                self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_dev_plugin_registered_again_is_idempotent(self):
        with temp_state() as env, fake_repo() as repo:
            manifest, _ = fake_check(self.plugin("dev", "plugins", "plan-gate"), "x", PASS)
            self.register(env, manifest)
            self.register(env, manifest)
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                self.assertNotIn("conflict", json.load(f))
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")

    def test_cache_segments_are_matched_whole(self):
        _owner = import_bin("checks")._owner
        upgraded = [fake_check(self.plugin("myplugins", "cache", "mk", "p", v), "x", PASS)[0] for v in ("1", "2")]
        self.assertNotEqual(_owner(upgraded[0]), _owner(upgraded[1]))
        cached = [fake_check(self.plugin("plugins", "cache", "mk2", "p", v), "x", PASS)[0] for v in ("1", "2")]
        self.assertEqual(_owner(cached[0]), _owner(cached[1]))

    # R40: a gone manifest is replaceable by another owner only when its command is gone too
    def test_manifest_deleted_but_script_kept_is_a_conflict(self):
        with temp_state() as env, fake_repo() as repo:
            real, _ = fake_check(self.plugin("plugins", "cache", "mk", "plan-gate", "1.0.0"), "x", FAIL)
            impostor, _ = fake_check(self.plugin("elsewhere", "p", "1.0.0"), "x", PASS)
            self.register(env, real)
            os.remove(real)                        # only the manifest; the failing script stays
            self.register(env, impostor)
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                self.assertIn("conflict", json.load(f))
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_whole_plugin_folder_deleted_lets_the_new_owner_replace(self):
        with temp_state() as env, fake_repo() as repo:
            folder = self.plugin("plugins", "cache", "mk", "plan-gate", "1.0.0")
            real, _ = fake_check(folder, "x", FAIL)
            other, _ = fake_check(self.plugin("elsewhere", "p", "1.0.0"), "x", PASS)
            self.register(env, real)
            shutil.rmtree(folder)                  # uninstall: manifest AND command gone
            self.register(env, other)
            with open(os.path.join(self.checks_dir(env), "x.json")) as f:
                self.assertEqual(json.load(f)["source"], other)
            plan = fake_plan(repo)
            run("plan_gate.py", "run-checks", plan, env=env)
            self.assertEqual(state_of(plan, env)["status"], "approved")
