"""Ported from the INTERNAL test_preflight.py, test_pre_voo.py, test_preflight_humano.py and
test_nao_avaliado.py (apoio.py helpers become the local ones below).

Two source tests are NOT ported by name -- both compared against the frozen `bin/_legado/` copy;
their guarantee moved to the golden (test_golden_preflight.py):
  - test_o_legado_falhava_neste_caso            (test_preflight.py)
  - test_cada_grupo_acha_o_que_o_legado_achava  (test_preflight_humano.py)

In-process tests that assert localized text pin PLAN_GATE_LANG=en (R15); subprocess tests run
through helpers.run, which already strips the language switches (default is en).
"""
import os, shutil, stat, subprocess, tempfile, unittest, unittest.mock

from .helpers import import_bin, run

PF = import_bin("preflight_plan")
FENCE = chr(96) * 3


def fake_repo(base_dir):
    # Ported from `apoio.repo_de_mentira`: a bare `git init` (no commit), docs/superpowers/plans inside.
    repo = os.path.join(base_dir, "repo")
    os.makedirs(os.path.join(repo, "docs", "superpowers", "plans"), exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


MINIMAL_PLAN = """# Fake plan

### Task 1: do it

**Files:**
- Create: `src/novo.py`

- [ ] **Step 1: Write**

```python
def somar(a, b):
    return a + b
```
"""


def fake_plan(repo, name="2026-09-18-plan.md", text=MINIMAL_PLAN):
    p = os.path.join(repo, "docs", "superpowers", "plans", name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def write_plan(repo, text, name="p.md"):
    return fake_plan(repo, name=name, text=text)


class TestMethodWithoutKeyword(unittest.TestCase):
    def _repo_with(self, content, file="src/servico.ts"):
        d = tempfile.mkdtemp(); repo = fake_repo(d)
        target = os.path.join(repo, file); os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return repo

    def test_class_method_without_keyword_is_found(self):
        repo = self._repo_with("class Servico {\n  reconciliar(pedido) {\n    return 1;\n  }\n}\n")
        definition, _ = PF.check_in_batch(repo, ["reconciliar"])["reconciliar"]
        self.assertTrue(definition, "class method without keyword was not found")

    def test_async_method_without_keyword_is_found(self):
        repo = self._repo_with("class Servico {\n  async enviarLote(itens) {\n    return itens;\n  }\n}\n")
        self.assertTrue(PF.check_in_batch(repo, ["enviarLote"])["enviarLote"][0])

    def test_call_is_not_mistaken_for_definition(self):
        # If a call became a definition, the preflight would say EVERY invoked method exists --
        # and then it never finds anything, which is worse than finding too much.
        repo = self._repo_with("function main() {\n  outro(1, 2);\n}\n")
        definition, uses = PF.check_in_batch(repo, ["outro"])["outro"]
        self.assertEqual(definition, ""); self.assertGreaterEqual(uses, 1)


class TestDottedSymbol(unittest.TestCase):
    def test_dotted_symbol_is_not_a_file(self):
        for s in ("Pedido.reconciliar", "config.debug", "resposta.statusCode"):
            self.assertFalse(PF.looks_like_file(s), s)

    def test_real_file_is_still_a_file(self):
        for s in ("src/servico.ts", "composer.json", "README.md", "Dockerfile", "Makefile",
                  "app/code/Vendor/Modulo/etc/di.xml", "tests/test_x.py"):
            self.assertTrue(PF.looks_like_file(s), s)

    def test_exotic_extension_with_slash_is_still_a_file(self):
        self.assertTrue(PF.looks_like_file("config/nginx/site.vhost"))


class TestReceiverRule(unittest.TestCase):
    """Calibrated on 13 real plans (18/09): 42 findings -> 19, zero true ones lost."""

    def _findings(self, body):
        d = tempfile.mkdtemp(); repo = fake_repo(d)
        text = (f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a.ts`\n\n"
                f"{FENCE}typescript\n{body}\n{FENCE}\n")
        return set(PF.check_symbols(text, repo, PF.tasks_of_plan(text)))

    def test_library_method_on_lowercase_receiver_is_dropped(self):
        # `page` is not created by the plan: nothing coming from it is a promise of the plan.
        self.assertEqual(self._findings(
            "test('x', async ({ page }) => {\n"
            "  const card = page.getByTestId('remessa');\n"
            "  await expect(card.getByRole('button')).toBeVisible();\n"
            "});"), set())

    def test_invented_method_on_OUR_object_still_reported(self):
        # The case the check exists to catch. If the rule kills this one, it is useless.
        findings = self._findings("class Servico {\n  usar() {\n    return this.reconciliarXyz();\n  }\n}")
        self.assertTrue(any("reconciliarXyz" in a for a in findings), findings)

    def test_receiver_created_by_the_plan_still_counts(self):
        findings = self._findings("class Pedido {}\nconst pedido = new Pedido();\npedido.calcularTotalXyz();")
        self.assertTrue(any("calcularTotalXyz" in a for a in findings), findings)

    def test_framework_fixture_is_not_creation(self):
        # `({ page }) =>` names, it does not create. Counting it as creation voids the propagation.
        self.assertNotIn("page", PF.created_by_plan("test('x', async ({ page }) => {});"))

    def test_chain_head_rules(self):
        # `expect(x).toBeVisible()`: the receiver is `)`. What counts is who opens the chain.
        self.assertEqual(self._findings("await expect(algo).toBeVisivelXyz();"), set())


class TestFieldIsNotACall(unittest.TestCase):
    """`travados: ObjetoInelegivel[]` was read as `travados()` in a real plan."""

    def test_interface_field_does_not_become_method(self):
        d = tempfile.mkdtemp(); repo = fake_repo(d)
        text = (f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a.ts`\n\n"
                f"{FENCE}typescript\ninterface Resumo {{\n  travadosXyz: string[];\n}}\n{FENCE}\n")
        findings = PF.check_symbols(text, repo, PF.tasks_of_plan(text))
        self.assertFalse(any("travadosXyz" in a for a in findings), findings)


class TestRepoUnderExcludedPath(unittest.TestCase):
    """The repo may live under a directory whose name EXCLUDE cuts.

    ⚠️ Measured on 18/09: rg globs are matched against the WHOLE path, so `--glob '!**/tmp/**'`
    with the repo at `/tmp/xxx` excludes EVERYTHING -- and the preflight answers "does not exist in
    the repo" for every symbol, silently. This test exists because it went green for the wrong
    reason: the tests use `mkdtemp()`, which lives in /tmp, and the old copy found nothing either.
    """
    def _repo_under(self, ancestor_name):
        base = tempfile.mkdtemp()
        root = os.path.join(base, ancestor_name)
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        subprocess.run(["git", "init", "-q", root], check=True)
        with open(os.path.join(root, "src", "servico.ts"), "w", encoding="utf-8") as f:
            f.write("export function reconciliarPedido(p) {\n  return p;\n}\n")
        return root

    def test_ancestor_with_excluded_name_does_not_hide_the_repo(self):
        for ancestor in ("tmp", "docs", "build", "dist", "coverage"):
            repo = self._repo_under(ancestor)
            definition, _ = PF.check_in_batch(repo, ["reconciliarPedido"])["reconciliarPedido"]
            self.assertTrue(definition, f"repo under '{ancestor}/' became invisible")

    def test_node_modules_INSIDE_the_repo_is_still_excluded(self):
        # The fix must not loosen what EXCLUDE exists to cut.
        repo = self._repo_under("tmp")
        os.makedirs(os.path.join(repo, "node_modules", "x"), exist_ok=True)
        with open(os.path.join(repo, "node_modules", "x", "a.ts"), "w", encoding="utf-8") as f:
            f.write("export function soNoVendorXyz() {}\n")
        definition, _ = PF.check_in_batch(repo, ["soNoVendorXyz"])["soNoVendorXyz"]
        self.assertEqual(definition, "", "node_modules inside the repo is being scanned again")


class TestPythonNoise(unittest.TestCase):
    """Python entered the language table in wave 1.5; the extractor was calibrated for TS/JS.

    ⚠️ Measured on the wave 2 plan (18/09): 12 of 15 findings were stdlib, unittest or comment
    text. `self.assertX()` is the case the receiver rule does NOT cut -- `self` is in ALWAYS_COUNTS
    on purpose, to catch `this.methodThatDoesNotExist()`.
    """
    def _findings(self, body, lang="python"):
        d = tempfile.mkdtemp(); repo = fake_repo(d)
        text = (f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a.py`\n\n"
                f"{FENCE}{lang}\n{body}\n{FENCE}\n")
        return set(PF.check_symbols(text, repo, PF.tasks_of_plan(text)))

    def test_unittest_assert_is_not_an_invented_method(self):
        self.assertEqual(self._findings(
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.assertIsNone(None)\n"
            "        self.assertGreater(2, 1)\n"), set())

    def test_builtin_and_stdlib_method_are_not_invented(self):
        self.assertEqual(self._findings(
            "def usar(xs):\n"
            "    for i, x in enumerate(xs):\n"
            "        print(float(x), str(x).startswith('a'))\n"), set())

    def test_trailing_comment_does_not_become_a_call(self):
        # `# travamento na inicializacao (issue)` became an invented `inicializacao()`.
        self.assertEqual(self._findings(
            "SEM_BYTE_S = 90  # travamento na inicializacao (opencode#35870)\n"), set())

    def test_really_invented_method_is_still_reported(self):
        # The rule must not become "never finds anything": this is the case it exists to catch.
        findings = self._findings(
            "class Servico:\n"
            "    def usar(self):\n"
            "        return self.reconciliar_xyz()\n")
        self.assertTrue(any("reconciliar_xyz" in a for a in findings), findings)


class TestCountOccurrences(unittest.TestCase):
    """⚠️ `_count` feeds the 'uses in the repo' column of the decorative-constant check (rule 17b).
    It counted OCCURRENCES (`rg --count-matches`) and became a LINE count when swapped for the
    search module -- `usa(X, X, X)` became 1 instead of 3. No test touched it."""

    def test_three_occurrences_on_the_SAME_line_count_three(self):
        with tempfile.TemporaryDirectory() as d:
            repo = fake_repo(d)
            with open(os.path.join(repo, "a.py"), "w", encoding="utf-8") as f:
                f.write("XPTO = 1\nusa(XPTO, XPTO, XPTO)\n")
            self.assertEqual(PF._count(repo, r"\bXPTO\b"), 4)

    def test_absent_pattern_counts_zero(self):
        with tempfile.TemporaryDirectory() as d:
            repo = fake_repo(d)
            self.assertEqual(PF._count(repo, r"\bNAO_EXISTE_XYZ\b"), 0)


class TestDeclaredIsTheEngineThatRan(unittest.TestCase):
    """⚠️ This text goes into the reviewer PROMPT. Saying "ripgrep" when the `git` engine ran is
    lying to whoever reviews -- declare what is known, not what is presumed."""

    def block(self, with_rg):
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}):       # R15
            repo = fake_repo(d)
            plan = fake_plan(repo)
            with unittest.mock.patch.object(PF.search, "engine",
                                            lambda: "rg" if with_rg else "git"):
                return PF.preflight(plan, repo) or ""

    def test_with_rg_declares_ripgrep(self):
        self.assertIn("with ripgrep", self.block(True))

    def test_without_rg_declares_git_and_does_NOT_say_ripgrep(self):
        text = self.block(False)
        self.assertIn("git ls-files", text)
        self.assertNotIn("with ripgrep", text)


# ---------------------------------------------------------------- test_pre_voo.py

# ⚠️ TypeScript, not Python: `check_symbols` only evaluates TS/JS/PY/PHP; see the language table.
BODY = "__FENCE__typescript\nexport function usar() {\n  return metodoQueNaoExisteNoRepoXyz();\n}\n__FENCE__\n".replace(
    "__FENCE__", FENCE)
BODY_PY = "__FENCE__python\ndef usar():\n    return metodo_que_nao_existe_xyz()\n__FENCE__\n".replace(
    "__FENCE__", FENCE)


def plan_with(heading):
    return f"# Plan\n\n{heading}\n\n**Files:**\n- Create: `src/a.ts`\n\n{BODY}"


class TestTaskHeading(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = fake_repo(self.d)

    def _run(self, text):
        p = write_plan(self.repo, text)
        return run("preflight_plan.py", "--human", p, cwd=self.repo)

    def test_the_three_real_formats_are_recognized(self):
        # Measured on real delivered plans: all three formats appear.
        for heading in ("### Task 1: fazer a coisa",
                        "## Task 1: fazer a coisa",
                        "## Task 1 — fazer a coisa"):
            rc, out, _ = self._run(plan_with(heading))
            self.assertIn("1 tasks", out, heading)

    def test_without_any_task_the_report_declares_it_did_not_evaluate(self):
        """The defect that costs the most: with zero tasks it reported with confidence.

        ⚠️ Without a recognized task, "does an earlier task create this symbol?" has nothing to
        consult, and every library symbol becomes "invented method" -- 10 false findings in a
        real plan. The right thing is to say it did NOT evaluate.
        """
        rc, out, _ = self._run("# Plan without task\n\n## Problem\n\nsomething\n\n" + BODY)
        self.assertIn("NOT EVALUATED", out)
        self.assertIn("no task", out.lower())
        self.assertNotIn("Invented method", out)
        self.assertEqual(rc, 2, "code 2 = not evaluated, never 0")

    def test_with_a_task_the_real_finding_still_comes_out(self):
        rc, out, _ = self._run(plan_with("### Task 1: do"))
        self.assertIn("metodoQueNaoExisteNoRepoXyz", out)
        self.assertEqual(rc, 1)

    def test_language_outside_the_table_exits_2(self):
        """The other path to NOT EVALUATED: a language the extractor cannot read (Ruby stays out)."""
        body = BODY_PY.replace("python", "ruby").replace(
            "def usar():\n    return metodo_que_nao_existe_xyz()",
            "def usar\n  metodo_que_nao_existe_xyz\nend")
        text = f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a.rb`\n\n{body}"
        rc, out, _ = self._run(text)
        self.assertEqual(rc, 2, out)
        self.assertIn("NOT EVALUATED", out)
        self.assertIn("language in the table", out)
        self.assertNotIn("no task", out.lower())

    def test_python_plan_with_task_is_NOW_evaluated(self):
        # The other half of the change: what used to exit 2 by language now runs.
        text = f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a.py`\n\n{BODY_PY}"
        rc, out, _ = self._run(text)
        self.assertNotEqual(rc, 2, out)
        self.assertIn("metodo_que_nao_existe_xyz", out)

    def test_usage_documents_the_three_exit_codes(self):
        rc, out, _ = run("preflight_plan.py", "--help")
        for c in ("0 =", "1 =", "2 ="):
            self.assertIn(c, out, c)
        self.assertIn("NOT EVALUATED", out)


# ---------------------------------------------------------------- test_preflight_humano.py

# ⚠️ Every line of this fixture must PRODUCE a finding. A fixture that exercises nothing agrees
# with any implementation -- hence the assertTrue(...) in the tests that read it.
PLAN_OF_THE_UNION = f"""# Plan

### Task 1: do

**Files:**
- Create: `src/novo.ts`
- Modify: `src/inexistente-de-verdade.ts`

{FENCE}typescript
class Servico {{
  usar() {{
    return this.reconciliarXyz();
  }}
}}
{FENCE}
"""


class TestUnion(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = fake_repo(self.d)
        self.plan = os.path.join(self.repo, "docs", "superpowers", "plans", "p.md")
        with open(self.plan, "w", encoding="utf-8") as f:
            f.write(PLAN_OF_THE_UNION)

    def test_human_report_has_the_exact_signature(self):
        # ⚠️ `parameters[:2]` would pass with (plan, repo, spec, promises). The advisory must stay
        # OUTSIDE this function, and only the whole signature proves that.
        import inspect
        self.assertEqual(list(inspect.signature(PF.human_report).parameters), ["plan", "repo"])

    def test_the_seven_groups_appear_in_the_report(self):
        rc, out, _ = run("preflight_plan.py", "--human", self.plan, cwd=self.repo)
        for g in ("Aliases", "Invoked symbols", "Decorative constants",
                  "Tolerant defaults", "Cited files",
                  "File created that does not enter a commit", "Verification that does not cover"):
            self.assertIn(g, out, g)

    def test_invalid_bytes_are_NOT_EVALUATED_and_exit_2(self):
        # ⚠️ Accepting 1 here would let "invalid byte became a finding" through. A decoding error
        # is not a finding: it is not having been able to read.
        p = os.path.join(self.repo, "docs", "superpowers", "plans", "ruim.md")
        with open(p, "wb") as f:
            f.write("# Plan\n\n### Task 1: x\n".encode() + b"\xff\xfe")
        rc, out, err = run("preflight_plan.py", "--gate", p, cwd=self.repo)
        self.assertEqual(rc, 2, out + err)
        text, findings, not_evaluated = PF.human_report(p, self.repo)
        self.assertEqual(findings, [])
        self.assertTrue(not_evaluated)
        self.assertTrue(all("UTF-8" in m for _, m in not_evaluated), not_evaluated)

    def test_gate_is_silent(self):
        rc, out, err = run("preflight_plan.py", "--gate", self.plan, cwd=self.repo)
        self.assertEqual(out.strip(), "")
        self.assertEqual(err.strip(), "")
        self.assertEqual(rc, 1)          # the fixture has a finding

    def test_prompt_returns_the_block(self):
        rc, out, err = run("preflight_plan.py", "--prompt", self.plan, cwd=self.repo)
        self.assertEqual(rc, 0, err)
        self.assertIn("Mechanical preflight", out)

    def test_promises_never_changes_the_exit_code(self):
        without = run("preflight_plan.py", "--human", self.plan, cwd=self.repo)[0]
        with_ = run("preflight_plan.py", "--human", "--promises", self.plan, cwd=self.repo)[0]
        self.assertEqual(without, with_)

    def test_check_7_orphan_fields_survived(self):
        # Written on 17/09; it was the only check that existed in the preflight and not in the report.
        self.assertTrue(callable(PF.orphan_fields))
        self.assertTrue(PF.RE_FIELD.pattern)


# ---------------------------------------------------------------- test_nao_avaliado.py

NO_BLOCK = """# Plan

### Task 1: prose only

**Files:**
- Create: `src/a.py`

- [ ] **Step 1:** write the sum function.
"""

NO_TASK = """# Plan

This is a document without any recognized task.
"""

LANGUAGE_OUTSIDE = f"""# Plan

### Task 1: do

**Files:**
- Create: `src/a.rb`

{FENCE}ruby
def somar(a, b)
  a + b
end
{FENCE}
"""

ODD_FENCE = f"""# Plan

### Task 1: do

**Files:**
- Create: `src/a.ts`

{FENCE}typescript
export function x() {{ return 1; }}
"""


class TestStates(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.repo = fake_repo(self.d)

    def _states(self, text):
        p = write_plan(self.repo, text)
        with open(p, encoding="utf-8") as _f:
            t = _f.read()
        return {k: v.state for k, v in PF.run_groups(t, self.repo, PF.tasks_of_plan(t)).items()}

    def test_plan_without_code_block(self):
        e = self._states(NO_BLOCK)
        self.assertEqual(e["simbolos"], "not_applicable")
        self.assertEqual(e["constantes"], "not_applicable")
        self.assertEqual(e["fail_open"], "not_applicable")
        self.assertIn(e["arquivos"], ("ok", "findings"))
        # ⚠️ There is `Create: src/a.py`, so the group APPLIES: with no `git add` in the plan every
        # created file is outside the commit -- and that is the FINDING, which is what the INTERNAL
        # always did. Classifying as not_applicable here would loosen the check silently.
        self.assertEqual(e["commits"], "findings")

    def test_plan_without_recognized_task(self):
        self.assertEqual(self._states(NO_TASK)["cobertura"], "not_evaluated")

    def test_language_outside_the_table_is_not_evaluated_not_not_applicable(self):
        # There is a fenced block: there was something to examine and we did not know how. That REFUSES.
        self.assertEqual(self._states(LANGUAGE_OUTSIDE)["simbolos"], "not_evaluated")

    def test_fence_the_extractor_does_not_close_is_not_evaluated(self):
        # ⚠️ There is code in the plan; if RE_BLOCK does not match, WE could not read it.
        # Saying not_applicable ("no code") here RELEASES, and it is a lie.
        self.assertEqual(self._states(ODD_FENCE)["simbolos"], "not_evaluated")

    def test_python_and_php_are_evaluated_and_find(self):
        for lang, target, body in (
                ("python", "metodo_que_nao_existe_xyz", "def usar():\n    return metodo_que_nao_existe_xyz()\n"),
                ("php", "metodoQueNaoExisteXyz", "<?php\nfunction usar() {\n  return metodoQueNaoExisteXyz();\n}\n")):
            text = (f"# Plan\n\n### Task 1: do\n\n**Files:**\n- Create: `src/a`\n\n"
                    f"{FENCE}{lang}\n{body}{FENCE}\n")
            p = write_plan(self.repo, text, name=f"p-{lang}.md")
            with open(p, encoding="utf-8") as _f:
                t = _f.read()
            res = PF.run_groups(t, self.repo, PF.tasks_of_plan(t))
            self.assertEqual(res["simbolos"].state, "findings", lang)
            self.assertTrue(any(target in x for x in res["simbolos"].findings), res["simbolos"].findings)

    def test_broken_alias_config_does_not_become_absence(self):
        # ⚠️ Three distinct answers: path (exists and parses), None (does not exist), False (exists
        # and does NOT parse). Without the third, a broken config becomes "has no alias", which RELEASES.
        with open(os.path.join(self.repo, "tsconfig.json"), "w") as f:
            f.write('{"compilerOptions": {"paths": {')     # truncated json
        self.assertIs(PF.alias_config(self.repo), False)
        self.assertEqual(self._states(NO_BLOCK)["aliases"], "not_evaluated")


class TestOperationalFailureIsNeverGreen(unittest.TestCase):
    """rg that fails, hangs or answers garbage must NOT become 'nothing found'."""

    def setUp(self):
        self.d = tempfile.mkdtemp(); self.repo = fake_repo(self.d)
        self.plan = write_plan(self.repo, f"# Plan\n\n### Task 1: x\n\n**Files:**\n- Create: `src/a.ts`\n\n"
                                          f"{FENCE}typescript\nexport function x() {{ return y(); }}\n{FENCE}\n",
                               name="op.md")

    def _fake_rg(self, body):
        d = tempfile.mkdtemp()
        rg = os.path.join(d, "rg")
        with open(rg, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(rg, os.stat(rg).st_mode | stat.S_IEXEC)
        return {"PATH": d + os.pathsep + os.environ["PATH"]}

    def test_rg_that_exits_2(self):
        # ⚠️ A PRESENT and broken `rg` is still an operational failure: the chosen engine is its
        # own, and it lied. Only the ABSENCE of the binary stopped being a failure (git engine).
        rc, out, err = run("preflight_plan.py", "--gate", self.plan, env=self._fake_rg("exit 2\n"), cwd=self.repo)
        self.assertEqual(rc, 2, out + err)

    def test_rg_that_hangs_until_the_timeout(self):
        rc, _, _ = run("preflight_plan.py", "--gate", self.plan, env=self._fake_rg("sleep 60\n"), cwd=self.repo)
        self.assertEqual(rc, 2)

    def _path_without_rg(self):
        """PATH with git (the preflight needs it to find the repo) and WITHOUT rg.

        ⚠️ An empty PATH takes `git` away too, and then the script exits 1 for "the plan must be
        inside a git repo" -- which is not what this test asserts. It would pass for the wrong reason.
        """
        d = tempfile.mkdtemp()
        os.symlink(shutil.which("git"), os.path.join(d, "git"))
        return {"PATH": d}

    def test_without_rg_on_the_path_the_preflight_RUNS_on_the_git_engine(self):
        """Without `rg` the preflight used to skip the whole symbol table ("could not check"), which
        OPENS the gate, the forbidden direction. Now the `git` engine needs no external binary.

        What did NOT change, and what the two broken-`rg` tests above protect: a search failure is
        still never green.
        """
        without = run("preflight_plan.py", "--human", self.plan, env=self._path_without_rg(), cwd=self.repo)
        with_ = run("preflight_plan.py", "--human", self.plan, cwd=self.repo)
        # ⚠️ The assertion that matters is not the exit code: it is the EQUIVALENCE.
        self.assertEqual(without[0], with_[0], without[1] + without[2])
        lines = lambda r: [l for l in r[1].splitlines() if l.startswith("[")]
        self.assertEqual(lines(without), lines(with_))
        self.assertNotIn("apt install ripgrep", without[1])

    def test_unreadable_plan(self):
        p = write_plan(self.repo, "# Plan\n", name="closed.md")
        os.chmod(p, 0)
        try:
            rc, _, _ = run("preflight_plan.py", "--gate", p, cwd=self.repo)
            self.assertEqual(rc, 2)
        finally:
            os.chmod(p, 0o644)

class TestAlwaysFlagIsKeyedNotWorded(unittest.TestCase):
    """A-M1: whether a fail-open pattern is flagged in any context depends on the label KEY, never on the
    words of its translation."""

    def test_translation_words_do_not_decide(self):
        plan = (f"### Task 1: x\n\n{FENCE}ts\nconst a = b ?? 0;\n"
                f"try {{ f(); }} catch (e) {{}}\n{FENCE}\n")
        swapped = {"preflight.fo.nullish_zero": "catch substring", "preflight.fo.empty_catch": "swallowed"}
        real = PF._t

        def fake(key, **kw):
            return swapped.get(key) or real(key, **kw)
        with unittest.mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}), \
                unittest.mock.patch.object(PF, "_t", side_effect=fake):
            found = PF.check_fail_open(plan, PF.tasks_of_plan(plan))
        self.assertEqual(len(found), 1, found)
        self.assertIn("swallowed", found[0])        # empty catch: always, whatever its label says

