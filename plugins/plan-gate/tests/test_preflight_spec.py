"""Spec preflight.

Ported from the INTERNAL test_preflight_spec.py (18 tests, all kept, names translated 1:1;
apoio.carregar / apoio.repo_de_mentira become import_bin / the local fake_repo).

⚠️ The example SENTENCES the tests feed to the preflight stay in Portuguese on purpose: the
recognizers (GUARANTEE, INTENT, QUANTITATIVE) are regexes over pt-BR prose and the port keeps them
byte for byte -- an English sentence would not trigger them and the tests would prove nothing.

The tests assert the EFFECT ("this sentence blocks", "this one does not"), never the call. Every
number quoted in the module came from running against a corpus of 73 real specs.
"""
import contextlib, io, os, subprocess, tempfile, unittest
from unittest import mock

from .helpers import import_bin

ps = import_bin("preflight_spec")


def fake_repo(base_dir):
    repo = os.path.join(base_dir, "repo")
    os.makedirs(os.path.join(repo, "docs", "superpowers", "specs"), exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


def spec_with(text, repo):
    d = os.path.join(repo, "docs", "superpowers", "specs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "2026-09-18-test-design.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"})   # R15
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = tempfile.mkdtemp()
        self.repo = fake_repo(self.home)
        with open(os.path.join(self.repo, "servico.py"), "w") as f:
            # ⚠️ File BIGGER than the +-12 line window on purpose: with 6 lines, citing line 1
            # would "support" any symbol in the file and the test would prove nothing.
            f.write("def carregar(caminho):\n    return 1\n")
            f.write("# enchimento\n" * 40)
            # ⚠️ Name WITH an underscore on purpose: `LOOKS_LIKE_CODE` demands () or _ or an
            # extension, and that filter is what keeps `status` (prose in backticks) from
            # becoming a symbol.
            f.write("def gravar_no_disco(x):\n    return x\n")

    def evaluate(self, text):
        return ps.evaluate(spec_with(text, self.repo), self.repo)

    def blocks(self, text):
        res = self.evaluate(text)
        return any(state == "findings" for _gid, _title, blocking in ps.GROUPS
                   for state, _items, _b in [res[_gid]] if blocking)


class TestGuarantee(Base):
    """The group that blocks: ~0.65 per spec in the 73 real ones, ~70% adjudicated precision."""

    def test_guarantee_without_citation_BLOCKS(self):
        self.assertTrue(self.blocks(
            "O `servico.py` ja impede que o valor passe do teto, entao nao precisamos mexer."))

    def test_the_SAME_sentence_with_citation_passes(self):
        self.assertFalse(self.blocks(
            "O `servico.py` ja impede que o valor passe do teto (`servico.py:1`)."))

    def test_intent_is_not_a_claim(self):
        # ⚠️ "vai impedir" talks about the future, and the future is not cited.
        self.assertFalse(self.blocks("O `servico.py` vai impedir que o valor passe do teto."))

    def test_policy_about_PEOPLE_does_not_block(self):
        # ⚠️ 2 of the first 3 false positives were these: bare `sempre`/`nunca` caught working
        # rules ("`docs/superpowers/` nunca e commitado") instead of code behavior.
        self.assertFalse(self.blocks(
            "O diretorio `docs/superpowers/` nunca e commitado, em nenhum projeto."))
        self.assertFalse(self.blocks(
            "A config em `config/app.json` e sempre gerada, nunca digitada a mao."))

    def test_quoted_sentence_does_not_block(self):
        # ⚠️ Documenting your own mistake must not be punished.
        self.assertFalse(self.blocks(
            'Eu tinha escrito que "o `servico.py` ja impede o estouro", e estava errado.'))

    def test_block_quote_does_not_block(self):
        self.assertFalse(self.blocks("> O `servico.py` ja impede o estouro do teto."))


class TestCitation(Base):
    """100% precision by construction: either the line exists in the file, or it does not."""

    def test_line_past_the_end_BLOCKS(self):
        self.assertTrue(self.blocks("A trava mora em `servico.py:900`, no comeco do modulo."))

    def test_line_that_exists_passes(self):
        self.assertFalse(self.blocks("A funcao `carregar` esta em `servico.py:1`."))

    def test_file_of_ANOTHER_system_only_warns(self):
        # ⚠️ Adjudicated in 4 of 6 samples: legitimate references to another system, outside
        # the repo. Accusing as wrong what cannot be checked is the vice to fight.
        res = self.evaluate("Ver `other/files/export.php:1430`, que monta o payload.")
        self.assertEqual(res["citations"][0], "ok")
        self.assertEqual(res["outside-repo"][0], "findings")
        self.assertFalse(self.blocks("Ver `other/files/export.php:1430`."))


class TestSupport(Base):
    """Replaced the 'symbol exists in the repo' check, which was conceptually wrong: a plan
    CREATES things, a spec PROPOSES things. Measured: 7 of 7 accused were false."""

    def test_citation_that_does_not_mention_the_symbol_BLOCKS(self):
        self.assertTrue(self.blocks(
            "A funcao `gravar_no_disco` trata o erro de disco — ver `servico.py:1`."))

    def test_citation_that_mentions_it_passes(self):
        self.assertFalse(self.blocks("A funcao `gravar_no_disco` esta em `servico.py:43`."))

    def test_name_the_spec_PROPOSES_does_not_block(self):
        # without a citation there is nothing to support; and proposing a new name is a spec's job
        self.assertFalse(self.blocks(
            "Vamos criar `modelo_upstream` para separar as duas representacoes."))


class TestWarnings(Base):
    def test_quantity_without_value_warns_but_does_not_block(self):
        res = self.evaluate("O teto diario protege a equipe de um laco maluco.")
        self.assertEqual(res["quantities"][0], "findings")
        self.assertFalse(self.blocks("O teto diario protege a equipe de um laco maluco."))

    def test_quantity_with_value_is_ok(self):
        res = self.evaluate("O teto diario e de 9000000 tokens.")
        self.assertEqual(res["quantities"][0], "ok")

    def test_plain_claim_warns_but_does_not_block(self):
        # ⚠️ 33.45 per spec in the 73 real ones. Blocking on that would be 33 escapes per spec: a dead gate.
        res = self.evaluate("O `servico.py` le o arquivo e devolve o valor.")
        self.assertEqual(res["claims"][0], "findings")
        self.assertFalse(self.blocks("O `servico.py` le o arquivo e devolve o valor."))


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class TestOutput(Base):
    def test_clean_spec_exits_zero(self):
        path = spec_with("# Spec\n\nVamos construir um servico novo.\n", self.repo)
        with quiet():
            self.assertEqual(ps.main([path]), 0)

    def test_spec_with_a_block_exits_one(self):
        path = spec_with("# Spec\n\nO `servico.py` ja impede o estouro.\n", self.repo)
        with quiet():
            self.assertEqual(ps.main([path]), 1)

    def test_missing_spec_exits_two(self):
        with quiet():
            self.assertEqual(ps.main([os.path.join(self.repo, "nao-existe.md")]), 2)


if __name__ == "__main__":
    unittest.main()
