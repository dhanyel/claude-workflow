import json, os, subprocess, sys, tempfile, unittest
from .helpers import BIN, import_bin

config = import_bin("config")

def write(repo, data):
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))

class TestConfig(unittest.TestCase):
    def test_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as repo:
            c = config.load(repo)
            self.assertEqual((c["plans_dir"], c["specs_dir"], c["checks"]),
                             ("docs/superpowers/plans", "docs/superpowers/specs", {}))

    def test_overrides(self):
        with tempfile.TemporaryDirectory() as repo:
            write(repo, {"plans_dir": "plans", "checks": {"preflight": {"required": False}}})
            c = config.load(repo)
            self.assertEqual(c["plans_dir"], "plans")
            self.assertFalse(c["checks"]["preflight"]["required"])

    def test_broken_json_fails_loudly(self):
        with tempfile.TemporaryDirectory() as repo:
            write(repo, "{not json")
            with self.assertRaises(ValueError):
                config.load(repo)


class TestConfigValidation(unittest.TestCase):
    """Fix round 1 of Task 7 (R21/R25): a config the gate cannot use is a BROKEN config -- ValueError naming
    the path -- never a value that crashes later or a folder that never (or always) matches."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.path = os.path.join(self.repo, ".claude", "plan-gate.json")

    def assertBroken(self, data):
        write(self.repo, data)
        with self.assertRaises(ValueError) as ctx:
            config.load(self.repo)
        self.assertIn(self.path, str(ctx.exception))

    def test_wrong_typed_values_are_broken(self):
        for data in ({"plans_dir": 5}, {"plans_dir": None}, {"plans_dir": ["x"]}, {"specs_dir": 5},
                     {"checks": []}, {"checks": "x"}, {"checks": {"a": 1}}, {"mr_template": 5}):
            with self.subTest(data=data):
                self.assertBroken(data)

    def test_folders_that_never_match_or_match_everything_are_broken(self):
        for key in ("plans_dir", "specs_dir"):
            for value in ("", "/", ".", "./", "/abs/plans", "..", "../plans", "a/../../b", "a/..",
                          " docs/superpowers/plans", "plans ", "plans\n", "~/plans", "~"):
                with self.subTest(key=key, value=value):
                    self.assertBroken({key: value})

    def test_valid_values_still_load(self):
        write(self.repo, {"plans_dir": "plans/", "specs_dir": "docs/x/specs", "mr_template": None,
                          "checks": {"preflight": {"required": False}}})
        c = config.load(self.repo)
        self.assertEqual((c["plans_dir"], c["specs_dir"]), ("plans/", "docs/x/specs"))
        write(self.repo, {"mr_template": "t.md"})
        self.assertEqual(config.load(self.repo)["mr_template"], "t.md")

    def test_platform_and_api_url_default_to_null(self):
        c = config.load(self.repo)
        self.assertEqual((c["platform"], c["api_url"]), (None, None))

    def test_platform_and_api_url_overrides_load(self):
        write(self.repo, {"platform": "github", "api_url": "https://ghe.example.org/api/v3"})
        c = config.load(self.repo)
        self.assertEqual((c["platform"], c["api_url"]), ("github", "https://ghe.example.org/api/v3"))
        write(self.repo, {"platform": "gitlab", "api_url": "http://gitlab.local:8080/api/v4"})
        self.assertEqual(config.load(self.repo)["platform"], "gitlab")

    def test_bad_platform_is_broken(self):
        for value in ("GitHub", "bitbucket", "", 1, True, ["github"], {"x": 1}):
            with self.subTest(value=value):
                self.assertBroken({"platform": value})

    def test_bad_api_url_is_broken(self):
        # R57: a URL the request could not use, or one carrying credentials (they would end up in a log line)
        for value in (5, True, [], "", "gitlab.example/api/v4", "ftp://x.example/api", "https://", "https:///api",
                      " https://x.example/api", "https://x.example/api ", "https://user:pw@x.example/api",
                      "https://tok@x.example/api", "https://x.example/api?private_token=x", "https://x.example/a#f",
                      "https://x.example/a b"):
            with self.subTest(value=value):
                self.assertBroken({"api_url": value})

    def test_a_directory_in_place_of_the_file_is_broken(self):
        os.makedirs(self.path)
        with self.assertRaises(ValueError) as ctx:
            config.load(self.repo)
        self.assertIn(self.path, str(ctx.exception))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_an_unreadable_file_is_broken(self):
        write(self.repo, {})
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o644)
        with self.assertRaises(ValueError) as ctx:
            config.load(self.repo)
        self.assertIn(self.path, str(ctx.exception))


class TestConfigReadsOnlyRegularFiles(unittest.TestCase):
    """R27: a FIFO or a link to /dev/zero made the hook hang until it timed out -- and a hook timeout does not
    block the tool. load() runs in a subprocess with a timeout, so a hang FAILS instead of stalling."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        os.makedirs(os.path.join(self.repo, ".claude"))
        self.path = os.path.join(self.repo, ".claude", "plan-gate.json")

    def load_in_child(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import config\n"
                "try:\n    config.load(sys.argv[2])\nexcept ValueError as e:\n    print(e); sys.exit(3)\n")
        p = subprocess.run([sys.executable, "-c", code, BIN, self.repo], capture_output=True, text=True,
                           timeout=10)
        return p.returncode, p.stdout

    def assertBroken(self):
        rc, out = self.load_in_child()
        self.assertEqual(rc, 3, out)
        self.assertIn(self.path, out)
        return out

    def test_fifo(self):
        os.mkfifo(self.path)
        self.assertIn("not a regular file", self.assertBroken())

    def test_link_to_dev_zero(self):
        os.symlink("/dev/zero", self.path)
        self.assertIn("symbolic link", self.assertBroken())

    def test_oversized_file(self):
        with open(self.path, "w") as f:
            f.write('{"mr_template": "' + "a" * (config.MAX_CONFIG_BYTES + 1) + '"}')
        self.assertBroken()

    def test_a_link_to_a_valid_file_is_broken(self):
        # R30: a link lets the config "be" any file -- the exception for writing the config would follow it
        target = os.path.join(self.repo, "real.json")
        with open(target, "w") as f:
            json.dump({"plans_dir": "plans"}, f)
        os.symlink(target, self.path)
        self.assertIn("symbolic link", self.assertBroken())

    def test_a_dangling_link_is_broken_not_absent(self):
        os.symlink(os.path.join(self.repo, "gone.json"), self.path)
        self.assertIn("symbolic link", self.assertBroken())
