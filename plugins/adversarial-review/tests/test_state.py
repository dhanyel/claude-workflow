import glob, json, os, unittest
from unittest import mock
from .helpers import isolated_env, import_bin

state = import_bin("state")
H1, H2 = "a" * 64, "b" * 64


class TestState(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, isolated_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_verdicts_are_kept_per_hash(self):
        state.record_verdict("/r/p.md", H1, {"verdict": "REJECTED", "blockers": 2, "round": 1})
        state.record_verdict("/r/p.md", H2, {"verdict": "APPROVED", "blockers": 0, "round": 2})
        self.assertEqual(state.verdict_for("/r/p.md", H1)[0]["verdict"], "REJECTED")
        self.assertEqual(state.verdict_for("/r/p.md", H2)[0]["verdict"], "APPROVED")

    def test_a_failure_leaves_the_verdicts_untouched(self):
        state.record_verdict("/r/p.md", H1, {"verdict": "APPROVED", "blockers": 0, "round": 1})
        before = state.load("/r/p.md")[0]["reviews"]
        state.record_failure("/r/p.md", {"round": 2, "reason": "timeout"})
        data = state.load("/r/p.md")[0]
        self.assertEqual((data["reviews"], data["last_failure"]["reason"]), (before, "timeout"))

    def test_a_new_verdict_clears_the_last_failure(self):
        state.record_failure("/r/p.md", {"round": 1, "reason": "x"})
        state.record_verdict("/r/p.md", H1, {"verdict": "APPROVED", "blockers": 0, "round": 1})
        self.assertNotIn("last_failure", state.load("/r/p.md")[0])

    def test_a_corrupt_state_is_an_error_not_an_absence(self):
        os.makedirs(state.state_dir(), exist_ok=True)
        with open(state.state_path("/r/p.md"), "w") as f:
            f.write("{half")
        entry, error = state.verdict_for("/r/p.md", H1)
        self.assertIsNone(entry)
        self.assertTrue(error)

    def test_a_non_object_entry_is_an_error(self):
        state.record_verdict("/r/p.md", H1, "APPROVED")
        entry, error = state.verdict_for("/r/p.md", H1)
        self.assertIsNone(entry)
        self.assertIn("not an object", error)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_an_unreadable_state_is_never_set_aside(self):
        state.record_verdict("/r/p.md", H1, {"verdict": "APPROVED", "blockers": 0, "round": 1})
        target = state.state_path("/r/p.md")
        os.chmod(target, 0)
        self.addCleanup(os.chmod, target, 0o600)
        with self.assertRaises(OSError):
            state.record_verdict("/r/p.md", H2, {"verdict": "REJECTED", "blockers": 1, "round": 2})
        with self.assertRaises(OSError):
            state.record_failure("/r/p.md", {"round": 2, "reason": "x"})
        self.assertTrue(os.path.exists(target))
        self.assertEqual(glob.glob(target + ".corrupt-*"), [])
        self.assertTrue(state.verdict_for("/r/p.md", H1)[1])

    def test_a_failed_write_leaves_no_tmp_file(self):
        with mock.patch.object(state.json, "dump", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                state.record_verdict("/r/p.md", H1, {"verdict": "APPROVED", "blockers": 0, "round": 1})
        self.assertEqual(glob.glob(os.path.join(state.state_dir(), "*.tmp")), [])
