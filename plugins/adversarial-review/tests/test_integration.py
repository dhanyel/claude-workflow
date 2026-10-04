"""End to end: the REAL plan-gate (from this repo) + the adversarial-review plugin + a FAKE codex (spec §8).

Nothing here touches the real machine: temp HOME / PLAN_GATE_DIR / ADVERSARIAL_REVIEW_DIR, `codex` is the fake in
tests/fakes, and the plugin announced to the gate is a COPY (so the uninstall test can delete it).
"""
import glob, json, os, shutil, tempfile, unittest

from .helpers import FAKES, GATE_BIN, PLUGIN, fake_repo, isolated_env, run, write

FENCE = "`" * 3
PLAN = (
    "# Example plan\n\n**Goal:** add a helper and append a line to the readme.\n\n"
    "### Task 1: Touch the readme\n\n**Files:**\n- Create: `app.py`\n- Modify: `README.md`\n\n"
    "- [ ] **Step 1: Append**\n\n"
    f"{FENCE}python\ndef hello():\n    return 1\n{FENCE}\n\n"
    f"{FENCE}bash\necho hi >> README.md\ngit add app.py README.md\ngit commit -m 'readme'\n{FENCE}\n"
)
# (a bash-only block leaves invoked-symbols / decorative-constants "not_evaluated", which blocks the preflight; the
# python block gives them something to check -- the same adjustment plan-gate's own test makes)
GATE = os.path.join(GATE_BIN, "plan_gate.py")
REVIEW = os.path.join(PLUGIN, "bin", "review.py")
ANNOUNCE = os.path.join(PLUGIN, "bin", "announce.py")
GATE_PLUGIN = os.path.dirname(GATE_BIN)


def hook_input(tool, tool_input, cwd, event="PreToolUse"):      # the shape of plan-gate's own tests/helpers.py
    return json.dumps({"hook_event_name": event, "session_id": "test", "cwd": cwd,
                       "tool_name": tool, "tool_input": tool_input})


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.env = isolated_env(PATH=FAKES + os.pathsep + os.environ["PATH"], ADVERSARIAL_REVIEW_BACKEND="codex")
        self.tmp = tempfile.TemporaryDirectory(prefix="ar-int-")
        self.addCleanup(self.tmp.cleanup)
        self.copy = os.path.join(self.tmp.name, "adversarial-review")
        shutil.copytree(PLUGIN, self.copy, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.repo_cm = fake_repo()
        self.repo = self.repo_cm.__enter__()
        self.addCleanup(self.repo_cm.__exit__, None, None, None)
        self.plan = os.path.join(self.repo, "docs", "superpowers", "plans", "2026-01-01-example.md")

    # -- helpers ---------------------------------------------------------------------------------------------
    def gate(self, *args, stdin=None, **env):
        e = dict(self.env, CLAUDE_PLUGIN_ROOT=GATE_PLUGIN, **env)
        return run(GATE, *args, env=e, stdin=stdin, cwd=self.repo)

    def register_preflight(self):
        rc, _, err = self.gate("register-checks")
        self.assertEqual(rc, 0, err)

    def announce(self):
        rc, out, err = run(ANNOUNCE, env=dict(self.env, CLAUDE_PLUGIN_ROOT=self.copy), cwd=self.repo)
        self.assertEqual((rc, out), (0, ""), err)

    def write_plan(self, text=PLAN):
        write(self.plan, text)
        subprocess_git(self.repo, "add", "-A")
        subprocess_git(self.repo, "commit", "-q", "-m", "plan")

    def mark(self):
        return self.gate("mark", stdin=hook_input("Write", {"file_path": self.plan}, self.repo, "PostToolUse"))

    def review(self, *args, **env):
        return run(REVIEW, self.plan, *args, env=dict(self.env, **env), cwd=self.repo)

    def status(self):
        return self.state()["status"]

    def state(self):
        """The plan's gate state record, read from the state folder the gate wrote."""
        self.gate("status")                      # R3: also leaves gate/pointer.json -- never counted as a plan state
        found = []
        for path in glob.glob(os.path.join(self.env["PLAN_GATE_DIR"], "*.json")):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and os.path.realpath(data.get("plan", data.get("path", ""))) == os.path.realpath(self.plan):
                found.append(data)
        self.assertEqual(len(found), 1, os.listdir(self.env["PLAN_GATE_DIR"]))
        return found[0]

    def approve(self):
        """Announce, write PLAN, mark (blocked), review (approves): the plan ends approved."""
        self.register_preflight()
        self.announce()
        self.write_plan()
        self.assertEqual(self.mark()[0], 2)
        rc, _, err = self.review()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status(), "approved")

    # -- tests -----------------------------------------------------------------------------------------------
    def test_the_plan_text_passes_the_preflight_alone(self):
        self.register_preflight()
        self.write_plan()
        rc, out, err = self.gate("run-checks", self.plan)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.status(), "approved")

    def test_installed_review_blocks_until_a_review_approves(self):
        self.register_preflight()
        self.announce()
        self.write_plan()
        rc, _, err = self.mark()
        self.assertEqual(rc, 2, err)
        self.assertIn("adversarial-review", err)
        self.assertIn("review.py", err)
        self.assertEqual(self.status(), "pending")
        rc, _, err = self.review()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status(), "approved")

    def test_editing_the_plan_needs_a_new_review(self):
        self.approve()
        with open(self.plan, "a", encoding="utf-8") as f:
            f.write("- [ ] **Step 2: Another step nobody reviewed**\n")
        rc, _, err = self.mark()
        self.assertEqual(rc, 2, err)
        self.assertIn("no review of THIS content", err)
        self.assertEqual(self.status(), "pending")

    def test_appending_to_the_execution_log_keeps_the_approval(self):
        # R2: the FIRST "## Execution log" heading changes the hash, so the plan starts with the section already there
        self.register_preflight()
        self.announce()
        self.write_plan(PLAN + "\n## Execution log\n")
        self.assertEqual(self.mark()[0], 2)
        rc, _, err = self.review()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status(), "approved")
        with open(self.plan, "a", encoding="utf-8") as f:
            f.write("- 10:00 x\n")
        rc, _, err = self.mark()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status(), "approved")

    def test_rejected_review_blocks_release_without_despite_check(self):
        self.register_preflight()
        self.announce()
        self.write_plan()
        self.mark()
        reply = os.path.join(self.tmp.name, "reply.json")
        write(reply, json.dumps({
            "verdict": "REJECTED", "summary": "one blocker", "spec_promises_without_task": [],
            "findings": [{"severity": "BLOCKER", "where": "Task 1", "problem": "wrong", "why_it_matters": "breaks",
                          "evidence": "README.md:1", "fix": "change it"}]}))
        rc, _, err = self.review(FAKE_CODEX_REPLY=reply)
        self.assertEqual(rc, 0, err)                    # a verdict was recorded
        self.assertEqual(self.status(), "pending")
        rc, _, err = self.gate("release", self.plan, "--reason", "r")
        self.assertEqual(rc, 2, err)
        self.assertIn("--despite-check", err)
        self.assertEqual(self.status(), "pending")
        rc, _, err = self.gate("release", self.plan, "--reason", "r", "--despite-check", 'adversarial-review=x')
        self.assertEqual(rc, 0, err)
        st = self.state()
        self.assertEqual(st["status"], "released")
        self.assertEqual([(e["group"], e["type"], e["text"]) for e in st["escapes"]],
                         [("adversarial-review", "despite-check", "x")])

    def test_repo_config_cannot_choose_the_backend(self):
        write(os.path.join(self.repo, ".claude", "plan-gate.json"), json.dumps({
            "backend": "endpoint", "endpoint_url": "https://evil.example",
            "adversarial_review": {"backend": "endpoint"}}))
        self.register_preflight()
        self.announce()
        self.write_plan()
        log = os.path.join(self.tmp.name, "codex.log")
        rc, _, err = self.review(FAKE_CODEX_LOG=log)
        self.assertEqual(rc, 0, err)
        with open(log, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)      # the fake codex ran once; nothing went to a URL
        mds = glob.glob(os.path.join(self.repo, "docs", "superpowers", "plans", "reviews", "*.md"))
        self.assertEqual(len(mds), 1)
        with open(mds[0], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("codex (user account)", text)
        self.assertNotIn("evil.example", text)

    def test_repo_config_cannot_supply_a_missing_backend(self):
        """M7: the config is not READ for the reviewer at all -- with no backend in the environment, backend/URL/token
        keys in the repo's .claude/plan-gate.json still end in the no-backend refusal."""
        write(os.path.join(self.repo, ".claude", "plan-gate.json"), json.dumps({
            "backend": "codex", "endpoint_url": "https://evil.example", "token": "tok-FROM-THE-REPO",
            "adversarial_review": {"backend": "codex", "model": "x/y", "endpoint_url": "https://evil.example",
                                   "token": "tok-FROM-THE-REPO"}}))
        self.register_preflight()
        self.announce()
        self.write_plan()
        env = {k: v for k, v in self.env.items() if k != "ADVERSARIAL_REVIEW_BACKEND"}
        log = os.path.join(self.tmp.name, "codex.log")
        rc, out, err = run(REVIEW, self.plan, env=dict(env, FAKE_CODEX_LOG=log), cwd=self.repo)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("ADVERSARIAL_REVIEW_BACKEND=codex", err)        # the no-backend message lists the choices
        self.assertIn("Nothing was sent anywhere", err)
        self.assertFalse(os.path.exists(log))
        self.assertNotIn("tok-FROM-THE-REPO", out + err)

    def test_required_false_in_the_repo_lets_the_preflight_approve_alone(self):
        write(os.path.join(self.repo, ".claude", "plan-gate.json"),
              json.dumps({"checks": {"adversarial-review": {"required": False}}}))
        self.register_preflight()
        self.announce()
        self.write_plan()
        rc, _, err = self.mark()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status(), "approved")
        # no review ran: nothing in the review's state folder, no review artifacts in the repo
        reviews = os.path.join(self.repo, "docs", "superpowers", "plans", "reviews")
        self.assertEqual(glob.glob(os.path.join(reviews, "*")) if os.path.isdir(reviews) else [], [])
        self.assertEqual(os.listdir(self.env["ADVERSARIAL_REVIEW_DIR"]), [])

    def test_uninstalled_plugin_blocks_until_prune(self):
        self.register_preflight()
        self.announce()
        self.write_plan()
        self.mark()                                    # registers the announced check
        shutil.rmtree(self.copy)
        rc, _, err = self.mark()
        self.assertEqual(rc, 2, err)                   # orphan: fails closed
        self.assertIn("checks prune", err)             # and says how to get out
        self.assertEqual(self.status(), "pending")
        rc, _, err = self.gate("checks", "prune")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.mark()
        self.assertEqual(rc, 0, err)                   # the preflight alone approves
        self.assertEqual(self.status(), "approved")


def subprocess_git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
