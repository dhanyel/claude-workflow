import json, os, sys, unittest
from .helpers import run, GOLDEN, temp_state

sys.path.insert(0, GOLDEN)
from golden_lib import materialized, hook_payload

class TestGateGolden(unittest.TestCase):
    def test_check_decides_like_the_internal_plugin(self):
        with open(os.path.join(GOLDEN, "decisions.json")) as f:
            golden = json.load(f)["check"]
        self.assertTrue(any(v["rc"] == 2 for v in golden.values()), "a golden with no denial proves nothing")
        self.assertTrue(any(v["rc"] == 0 for v in golden.values()), "a golden with no pass proves nothing")
        for hook, expected in golden.items():
            with materialized("prose_only") as (repo, plan), temp_state() as env:
                mark = json.dumps({"hook_event_name": "PostToolUse", "cwd": repo, "tool_name": "Write",
                                   "tool_input": {"file_path": plan}})
                run("plan_gate.py", "mark", env=env, stdin=mark)
                rc, _, _ = run("plan_gate.py", "check", env=env, stdin=hook_payload(hook, repo))
                self.assertEqual(rc, expected["rc"], hook)
