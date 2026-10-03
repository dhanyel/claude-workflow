import json, os, sys, tempfile, unittest
from .helpers import run, GOLDEN

sys.path.insert(0, GOLDEN)
from golden_lib import materialized

class TestSpecPreflightGolden(unittest.TestCase):
    def test_same_exit_code_as_the_internal_plugin(self):
        with open(os.path.join(GOLDEN, "decisions.json")) as f:
            golden = json.load(f)["preflight_spec"]
        self.assertTrue(golden, "an empty golden proves nothing")
        self.assertTrue(len({v["rc"] for v in golden.values()}) > 1, "a golden with one outcome does not discriminate")
        for case, expected in golden.items():
            with materialized(case) as (repo, spec):
                rc, _, _ = run("preflight_spec.py", spec, cwd=repo)
                self.assertEqual(rc, expected["rc"], case)


class TestSpecVacuity(unittest.TestCase):
    def test_empty_spec_does_not_pass(self):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n")
            _, out, _ = run("preflight_spec.py", "--json", spec, cwd=d)
            result = json.loads(out)
            self.assertEqual(result["status"], "fail")
            self.assertIn("nothing-to-check", [g["group"] for g in result["findings"]])


GROUP_IDS = ["guarantees", "citations", "support", "outside-repo", "quantities", "claims"]


class TestSpecJson(unittest.TestCase):
    """--json: every group is listed; status looks only at the BLOCKING groups."""

    def check(self, text, files=None):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (files or {}).items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            spec = os.path.join(d, "s.md")
            with open(spec, "w") as f:
                f.write(text)
            rc, out, _ = run("preflight_spec.py", "--json", spec, cwd=d)
            return rc, json.loads(out)

    def test_lists_all_six_groups_with_the_stable_shape(self):
        _, result = self.check("# Spec\n")
        groups = [g for g in result["findings"] if g["group"] != "nothing-to-check"]
        self.assertEqual([g["group"] for g in groups], GROUP_IDS)
        for g in groups:
            self.assertEqual({"group", "state", "count", "items"} - set(g), set())

    def test_non_blocking_warning_does_not_fail(self):
        rc, result = self.check("O `servico.py` le o arquivo e devolve o valor.\n")
        states = {g["group"]: g["state"] for g in result["findings"]}
        self.assertEqual(states["claims"], "findings")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(rc, 0)

    def test_blocking_finding_fails_with_rc_one(self):
        rc, result = self.check("O `servico.py` ja impede o estouro do teto.\n")
        states = {g["group"]: g["state"] for g in result["findings"]}
        self.assertEqual(states["guarantees"], "findings")
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("nothing-to-check", states)
        self.assertEqual(rc, 1)

    def test_one_resolved_citation_is_enough_to_pass(self):
        rc, result = self.check("A funcao esta em `a.py:1`.\n", files={"a.py": "x = 1\n"})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(rc, 0)


class TestVacuityFromWhatWasExamined(unittest.TestCase):
    """`pass` needs something the checks actually EXAMINED, not just a symbol in a sentence."""

    def status_of(self, text):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, "s.md")
            with open(spec, "w") as f:
                f.write(text)
            rc, out, _ = run("preflight_spec.py", "--json", spec, cwd=d)
            result = json.loads(out)
            return rc, result["status"], [g["group"] for g in result["findings"]]

    def assertVacuous(self, text):
        rc, status, groups = self.status_of(text)
        self.assertEqual(status, "fail", text)
        self.assertIn("nothing-to-check", groups, text)
        self.assertEqual(rc, 1, text)

    def test_intent_only_spec_is_vacuous(self):
        self.assertVacuous("Vamos criar `modelo_upstream` e o servico vai ler `a_b.py`.\n")

    def test_quoted_only_spec_is_vacuous(self):
        self.assertVacuous('Ele diz "o `a_b.py` ja impede".\n')

    def test_outside_repo_citation_only_spec_is_vacuous(self):
        self.assertVacuous("Ver `other/x.php:10`.\n")

    def test_an_examined_guarantee_is_enough(self):
        rc, status, groups = self.status_of("O `a_b.py` ja impede o estouro (`a_b.py:1`).\n")
        # the citation points at a file that does not exist here: outside-repo, but the
        # guarantee sentence WAS examined (it carries a citation) -> not vacuous
        self.assertNotIn("nothing-to-check", groups)


FAKE_GIT = "#!/bin/sh\necho 'fatal: detected dubious ownership in repository' >&2\nexit 128\n"


class TestFallbackOnlyOutsideGit(unittest.TestCase):
    """A spec inside a git repo whose git cannot be asked is NOT EVALUATED, never a silent pass."""

    def run_json(self, path_dirs, repo, spec):
        env = {k: v for k, v in os.environ.items() if k not in ("PATH", "HOME", "PLAN_GATE_DIR")}
        env["PATH"] = os.pathsep.join(path_dirs)
        return run("preflight_spec.py", "--json", spec, cwd=repo, env=env)

    def check(self, path_dirs):
        with materialized("spec_test_spec_limpa_sai_zero") as (repo, spec):
            rc, out, err = self.run_json(path_dirs, repo, spec)
        result = json.loads(out)
        self.assertEqual(result["status"], "fail")
        self.assertEqual([g["state"] for g in result["findings"]], ["not_evaluated"] * 6)
        self.assertNotIn("nothing-to-check", [g["group"] for g in result["findings"]])
        self.assertEqual(rc, 2)

    def test_git_that_fails_with_another_message_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "git")
            with open(fake, "w") as f:
                f.write(FAKE_GIT)
            os.chmod(fake, 0o755)
            self.check([d])

    def test_git_absent_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            self.check([d])


if __name__ == "__main__":
    unittest.main()
