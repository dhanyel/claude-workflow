"""The two search engines agree -- and the fallback is what the container will use.

⚠️ A test that only exercises the `rg` path proves nothing about the `git` engine, and a test
written from the same premise as the implementation does not catch an error in the premise.
"""
import os, shutil, subprocess, tempfile, unittest, unittest.mock

from .helpers import import_bin

search = import_bin("search")

# One definition: the preflight's own constant (the source test imported it from the preflight too).
TEST_GLOBS = import_bin("preflight_plan").TEST_GLOBS


def fake_repo(base_dir):
    # Ported from `apoio.repo_de_mentira`: a bare `git init` (no commit), docs/superpowers/plans inside.
    repo = os.path.join(base_dir, "repo")
    os.makedirs(os.path.join(repo, "docs", "superpowers", "plans"), exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


class BaseSearch(unittest.TestCase):
    # ⚠️ `git` ALWAYS; `rg` only where the binary exists. Iterating ("rg","git") blindly would leave the
    # suite red on exactly the machine WITHOUT ripgrep -- the container of Phase 3, the environment
    # this task exists to serve.
    ENGINES = ["git"] + (["rg"] if shutil.which("rg") else [])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = fake_repo(self.tmp)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".git")),
                        "the fixture must be a git repo, otherwise the git engine lists nothing")
        # ⚠️ The two cases where fnmatch diverged from rg: a test file at the ROOT.
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        for path in ("tests/a.py", "foo_test.py", "src/production.py"):
            os.makedirs(os.path.dirname(os.path.join(self.repo, path)) or self.repo,
                        exist_ok=True)
            with open(os.path.join(self.repo, path), "w", encoding="utf-8") as f:
                f.write("def somar(a, b):\n    return a + b\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def with_engine(self, name):
        return unittest.mock.patch.object(search, "HAS_RG", name == "rg")


class TestTheTwoEnginesAgree(BaseSearch):
    CASES = [
        {"pattern": r"def somar", "exclude": ("node_modules", ".git")},
        {"pattern": r"[A-Z_]{4,}", "exclude": ("node_modules", ".git")},
        {"pattern": "somar(", "fixed": True, "exclude": ()},
        {"pattern": r"nao-existe-em-lugar-nenhum-xyz", "exclude": ()},
        # ⚠️ The case that was missing and where the divergence lived: the REAL caller passes `globs=`,
        # not only `exclude=`. Taken from the preflight itself, so the test does not measure a copy
        # that gets stale.
        {"pattern": r"def somar", "globs": tuple(TEST_GLOBS)},
        {"pattern": r"[a-z]", "exclude": ("node_modules",), "globs": tuple(TEST_GLOBS)},
    ]

    @unittest.skipUnless(shutil.which("rg"), "only compares where both engines exist")
    def test_same_SET_of_hits_in_both_engines(self):
        for case in self.CASES:
            with self.subTest(**case):
                with self.with_engine("rg"):
                    a = search.find(self.repo, **case)
                with self.with_engine("git"):
                    b = search.find(self.repo, **case)
                # ⚠️ Compare IDENTITY, not count: matching counts are not proof.
                self.assertEqual({(x.file, x.line) for x in a},
                                 {(x.file, x.line) for x in b})

    @unittest.skipUnless(shutil.which("rg"), "only compares where both engines exist")
    def test_same_file_list_in_both_engines(self):
        with self.with_engine("rg"):
            a = search.list_files(self.repo, exclude=("node_modules", ".git"))
        with self.with_engine("git"):
            b = search.list_files(self.repo, exclude=("node_modules", ".git"))
        self.assertEqual(sorted(a), sorted(b))

    @unittest.skipUnless(shutil.which("rg"), "only compares where both engines exist")
    def test_the_test_glob_excludes_the_ROOT_file_in_both(self):
        # ⚠️ The exact case fnmatch got wrong: `tests/a.py` and `foo_test.py` at the root.
        for engine in ("rg", "git"):
            with self.subTest(engine=engine), self.with_engine(engine):
                hits = search.find(self.repo, r"def somar", globs=tuple(TEST_GLOBS))
                files = {a.file for a in hits}
                self.assertNotIn(os.path.join("tests", "a.py"), files)
                self.assertNotIn("foo_test.py", files)
                self.assertIn(os.path.join("src", "production.py"), files)


class TestContract(BaseSearch):
    def test_the_returned_path_is_RELATIVE_to_the_repo(self):
        # ⚠️ The caller depends on it; the comment in preflight_plano records the damage from when it
        # was not: `../../..`, a path that exists nowhere.
        for name in self.ENGINES:
            with self.subTest(engine=name), self.with_engine(name):
                for a in search.find(self.repo, r"def somar"):
                    self.assertFalse(a.file.startswith(("/", "..")), a.file)

    def test_max_per_file_1_returns_one_line_per_file(self):
        for name in self.ENGINES:
            with self.subTest(engine=name), self.with_engine(name):
                hits = search.find(self.repo, r"[a-z]", max_per_file=1)
                files = [a.file for a in hits]
                self.assertEqual(len(files), len(set(files)))

    def test_a_binary_file_does_not_blow_up(self):
        with open(os.path.join(self.repo, "bin.dat"), "wb") as f:
            f.write(bytes(range(256)) * 20)
        for name in self.ENGINES:
            with self.subTest(engine=name), self.with_engine(name):
                search.find(self.repo, r"def somar")      # must not raise

    def test_a_non_git_dir_does_not_blow_up_in_the_git_engine(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as f:
                f.write("def somar(): pass\n")
            with self.with_engine("git"):
                hits = search.find(d, r"def somar")
            self.assertEqual([a.file for a in hits], ["a.py"])

    def test_failure_becomes_CouldNotCheck_and_not_an_empty_list(self):
        # ⚠️ "" reaches the groups indistinguishable from "searched and found nothing", and with the
        # gate reading that result, a search with a timeout would OPEN the gate.
        with self.with_engine("git"):
            with unittest.mock.patch.object(search, "_git_files",
                                            side_effect=search.CouldNotCheck("timeout")):
                with self.assertRaises(search.CouldNotCheck):
                    search.find(self.repo, r"x")


class TestGitFailureDoesNotBecomeOsWalk(BaseSearch):
    """⚠️ Regression of the worktree fix: `except CouldNotCheck: return False` swallowed EVERY git
    failure and fell into os.walk without `.gitignore` -- a rare, silent fail-open."""

    def test_a_failing_git_PROPAGATES_instead_of_scanning_everything(self):
        with tempfile.TemporaryDirectory() as d:
            repo = fake_repo(d)
            with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("ignored/\n")
            os.makedirs(os.path.join(repo, "ignored"), exist_ok=True)
            with open(os.path.join(repo, "ignored", "generated.py"), "w", encoding="utf-8") as f:
                f.write("def ghostMethod(): pass\n")
            # `dubious ownership` is the real case in a container: git exits 128.
            with unittest.mock.patch.object(
                    search, "_run", side_effect=search.CouldNotCheck("`git` saiu 128: fatal: "
                                                                     "detected dubious ownership")):
                with self.with_engine("git"):
                    with self.assertRaises(search.CouldNotCheck):
                        search.find(repo, r"ghostMethod")

    def test_a_directory_that_is_NOT_a_git_repo_still_falls_into_os_walk(self):
        # the case the fix meant to handle keeps working
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as f:
                f.write("def alvo(): pass\n")
            with self.with_engine("git"):
                self.assertEqual([a.file for a in search.find(d, r"def alvo")], ["a.py"])

    def test_in_a_WORKTREE_the_gitignore_is_respected(self):
        # ⚠️ The other tests' fixture ASSERTS isdir(.git), so the suite guaranteed the fixed case was
        # never exercised. This one uses a real worktree.
        with tempfile.TemporaryDirectory() as d:
            repo = fake_repo(d)
            with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("ignored/\n")
            with open(os.path.join(repo, "good.py"), "w", encoding="utf-8") as f:
                f.write("def alvo(): pass\n")
            for cmd in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                        "commit", "-qm", "x"]):
                subprocess.run(["git", "-C", repo] + cmd, capture_output=True)
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", wt, "-b", "branch"],
                           capture_output=True)
            self.assertFalse(os.path.isdir(os.path.join(wt, ".git")),
                             "in a worktree .git is a FILE -- and that is the defect's premise")
            os.makedirs(os.path.join(wt, "ignored"), exist_ok=True)
            with open(os.path.join(wt, "ignored", "generated.py"), "w", encoding="utf-8") as f:
                f.write("def alvo(): pass\n")
            with self.with_engine("git"):
                hits = {a.file for a in search.find(wt, r"def alvo")}
            self.assertEqual(hits, {"good.py"}, "an ignored file must not show up")


class TestPositiveGlobRefuses(BaseSearch):
    def test_find_with_a_positive_glob_REFUSES_instead_of_diverging(self):
        # ⚠️ The two engines diverge in depth with a positive glob. Half support is worse than none:
        # refusing loudly is the only answer that does not lie.
        for name in self.ENGINES:
            # R15/A-M4: the assertion reads localized text -- pin en (the developer's shell may have pt-BR)
            with self.subTest(engine=name), self.with_engine(name), \
                    unittest.mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}):
                if name == "rg":
                    continue        # rg has the native behavior; the guard belongs to the git engine
                with self.assertRaises(search.CouldNotCheck) as cm:
                    search.find(self.repo, r"def somar", globs=("*.py",))
                self.assertIn("POSITIVE", str(cm.exception))

    def test_only_NEGATIVE_globs_keep_working(self):
        for name in self.ENGINES:
            with self.subTest(engine=name), self.with_engine(name):
                search.find(self.repo, r"def somar", globs=tuple(TEST_GLOBS))
