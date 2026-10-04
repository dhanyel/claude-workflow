import json, os, subprocess, sys, unittest
from unittest import mock
from .helpers import GATE_BIN, isolated_env, fake_repo, write, import_bin

gate = import_bin("gate")


def wire(env):
    """Runs the REAL plan-gate once so it writes gate/pointer.json into env's PLAN_GATE_DIR."""
    subprocess.run([sys.executable, os.path.join(GATE_BIN, "plan_gate.py"), "status"], env=env,
                   capture_output=True, check=False)


class TestGate(unittest.TestCase):
    def test_no_pointer_is_gate_unavailable(self):
        env = isolated_env()
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gate.GateUnavailable):
                gate.locate()

    def test_a_pointer_to_a_missing_file_is_gate_unavailable(self):
        env = isolated_env()
        write(os.path.join(env["PLAN_GATE_DIR"], "gate", "pointer.json"),
              json.dumps({"plan_gate": "/nope/plan_gate.py"}))
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gate.GateUnavailable):
                gate.locate()

    def test_hash_and_config_come_from_the_real_gate(self):
        env = isolated_env()
        wire(env)
        with fake_repo() as repo, mock.patch.dict(os.environ, env, clear=True):
            plan = write(os.path.join(repo, "docs", "superpowers", "plans", "2026-01-01-x.md"), "# P\n")
            self.assertEqual(gate.locate(), os.path.join(GATE_BIN, "plan_gate.py"))
            expected = subprocess.run([sys.executable, os.path.join(GATE_BIN, "plan_gate.py"), "hash", plan],
                                      env=env, capture_output=True, text=True).stdout.strip()
            self.assertRegex(expected, r"^[0-9a-f]{64}$")
            self.assertEqual(gate.content_hash(plan), expected)
            cfg = gate.repo_config(plan)
            self.assertEqual((cfg["repo"], cfg["plans_dir"]), (repo, "docs/superpowers/plans"))

    def test_a_broken_repo_config_is_gate_unavailable(self):
        env = isolated_env()
        wire(env)
        with fake_repo() as repo, mock.patch.dict(os.environ, env, clear=True):
            write(os.path.join(repo, ".claude", "plan-gate.json"), "{")
            with self.assertRaises(gate.GateUnavailable):
                gate.repo_config(repo)


FAKE = """import json, os, sys
mode = os.environ.get("FAKE_MODE")
if mode == "bad_hash" and sys.argv[1] == "hash":
    print("not-a-hash")
elif mode == "fail":
    sys.stderr.write("boom"); sys.exit(1)
elif mode == "env" and sys.argv[1] == "config":
    print(json.dumps(dict(os.environ)))
"""


class TestFakeGate(unittest.TestCase):
    def setUp(self):
        self.env = isolated_env()
        fake = write(os.path.join(self.env["PLAN_GATE_DIR"], "fake", "plan_gate.py"), FAKE)
        write(os.path.join(self.env["PLAN_GATE_DIR"], "gate", "pointer.json"), json.dumps({"plan_gate": fake}))

    def patched(self, **extra):
        return mock.patch.dict(os.environ, dict(self.env, **extra), clear=True)

    def test_a_non_hex_hash_is_gate_unavailable(self):
        with self.patched(FAKE_MODE="bad_hash"), self.assertRaises(gate.GateUnavailable):
            gate.content_hash("/x/plan.md")

    def test_a_failing_gate_is_gate_unavailable(self):
        with self.patched(FAKE_MODE="fail"), self.assertRaises(gate.GateUnavailable):
            gate.repo_config("/x/plan.md")

    def test_the_reviewer_credential_never_reaches_the_gate(self):
        with self.patched(FAKE_MODE="env", ADVERSARIAL_REVIEW_ENDPOINT_TOKEN="tok",
                          ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE="/tmp/tok", KEEP_ME="1"):
            seen = gate.repo_config("/x/plan.md")
        self.assertEqual(seen.get("KEEP_ME"), "1")
        self.assertNotIn("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN", seen)
        self.assertNotIn("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE", seen)
