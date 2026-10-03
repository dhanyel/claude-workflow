"""key() and content_hash() are a FORMAT, not an implementation detail.

Ported from the INTERNAL `tests/test_estado_golden.py` (loader: 5 tests). The INTERNAL compared against
a frozen copy of its legacy gate; here the expected values are FIXED literals, computed once with the
INTERNAL itself (`chave`/`sha` of `plan-gate.py` at 78c6b00b), so they do not depend on any path:

    HOME=$(mktemp -d) PLAN_GATE_DIR=$(mktemp -d) python3 -c "import importlib.util ...;
        print(g.chave('/x/docs/superpowers/plans/a.md')); print(g.sha(<each CASES text in a temp file>))"

If they change, every existing release turns invalid in silence -- or worse, stops being invalidated
when the plan changes.
"""
import os, tempfile, unittest
from .helpers import import_bin

gate = import_bin("plan_gate")

CASES = {
    "no log": ("# Plano\n\n### Task 1\n\ncorpo\n",
               "4b402291622578d123717fff067f05a10b81c642b6851fe61639e9837c95aa19"),
    "log at the end": ("# Plano\n\n### Task 1\n\ncorpo\n\n## Log de execucao\n\n| a |\n",
                       "e8d84736166ae9bb54a49cd1a9d46a6d38e7793b2ea869864d8dde8e0d155adb"),
    "log in the middle": ("# Plano\n\n## Log de execucao\n\n| a |\n\n## Depois\n\n### Task 1\n\ncorpo\n",
                          "54eec86a9cca710f17212e50dd5da38464120604c2c2479990b53fe82ee70b61"),
    "accented": ("# Plano ação\n\n### Task 1\n\nnão é só isso\n",
                 "76ed9940911fa93ea10ebb08cd89ca4dbb9c67840a96f1b45415c3ccad609f56"),
}
INVALID_BYTES = (b"# Plano\n\ncorpo\n\xff\xfe\n",
                 "a1b01075edcad8ceec03edd639bd6cdc317656059400b2d75e0f65abfe365a66")
KEY = ("/x/docs/superpowers/plans/a.md", "2711dabf928d404b")


class TestPersistedFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, content, name="plan.md"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        return p

    def test_content_hash_matches_the_internal(self):
        for name, (text, expected) in CASES.items():
            self.assertEqual(gate.content_hash(self._file(text)), expected, name)

    def test_key_matches_the_internal(self):
        self.assertEqual(gate.key(KEY[0]), KEY[1])

    def test_invalid_bytes_do_not_blow_up_and_match(self):
        self.assertEqual(gate.content_hash(self._file(INVALID_BYTES[0])), INVALID_BYTES[1])

    def test_editing_the_log_does_not_change_the_hash(self):
        # On purpose: the log is filled in AS IT HAPPENS, and each timestamp revoked the release.
        p = self._file(CASES["log at the end"][0])
        before = gate.content_hash(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write("| 09:00 |\n")
        self.assertEqual(gate.content_hash(p), before)

    def test_editing_a_TASK_changes_the_hash(self):
        # The other half: what the hash protects stays protected.
        p = self._file(CASES["log at the end"][0])
        before = gate.content_hash(p)
        with open(p, encoding="utf-8") as f:
            text = f.read().replace("corpo", "corpo diferente")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertNotEqual(gate.content_hash(p), before)

    def test_english_execution_log_heading_hashes_like_the_portuguese_one(self):
        # Deliberate difference: `## Execution log` is out of the hash too, with the same section rule.
        for name in ("log at the end", "log in the middle"):
            text, expected = CASES[name]
            english = text.replace("## Log de execucao", "## Execution log")
            self.assertNotEqual(english, text)
            self.assertEqual(gate.content_hash(self._file(english, "en.md")), expected, name)


if __name__ == "__main__":
    unittest.main()
