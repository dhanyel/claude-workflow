import json, os, re, subprocess, tempfile, unittest
from .helpers import PLUGIN, ROOT, BIN, run, isolated_env

HOOK = re.compile(r'^python3 "\$\{CLAUDE_PLUGIN_ROOT\}/bin/[a-z_]+\.py"( [a-z-]+)*$')


class TestPackaging(unittest.TestCase):
    def test_plugin_and_marketplace(self):
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["name"], "adversarial-review")
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            names = [p["name"] for p in json.load(f)["plugins"]]
        self.assertEqual(names, ["plan-gate", "adversarial-review"])

    def test_check_manifest(self):
        with open(os.path.join(PLUGIN, "plan-gate-check.json")) as f:
            [entry] = json.load(f)
        self.assertEqual((entry["id"], entry["required"], entry["applies_to"]),
                         ("adversarial-review", True, ["plan"]))
        self.assertRegex(entry["command"], HOOK)

    def test_hooks_call_python3_with_the_plugin_root(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertTrue(commands)
        for c in commands:
            self.assertRegex(c, HOOK)

    def test_every_bin_script_has_a_shebang(self):
        for name in sorted(n for n in os.listdir(BIN) if n.endswith(".py")):
            with open(os.path.join(BIN, name)) as f:
                self.assertEqual(f.readline().strip(), "#!/usr/bin/env python3", name)

    def test_every_tracked_bin_script_is_executable_in_git(self):
        # only what is already tracked: a task creating a script runs the suite BEFORE its own commit
        listed = subprocess.run(["git", "-C", ROOT, "ls-files", "-s", "plugins/adversarial-review/bin",
                                 "plugins/adversarial-review/tests/fakes"],
                                capture_output=True, text=True, check=True).stdout.splitlines()
        for line in listed:
            mode, path = line.split()[0], line.split()[-1]
            if path.endswith(".py") or "/fakes/" in path:
                self.assertEqual(mode, "100755", path)

    def test_announce_writes_the_inbox_entry_and_no_stdout(self):
        env = isolated_env(CLAUDE_PLUGIN_ROOT=PLUGIN)
        rc, out, err = run("announce.py", env=env)
        self.assertEqual((rc, out), (0, ""), err)
        with open(os.path.join(env["PLAN_GATE_DIR"], "manifests.d", "adversarial-review.json")) as f:
            self.assertEqual(json.load(f), {"manifest": os.path.join(PLUGIN, "plan-gate-check.json"),
                                            "plugin_root": PLUGIN})

    def test_announce_that_cannot_write_still_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "file")
            open(blocker, "w").close()
            env = isolated_env(PLAN_GATE_DIR=os.path.join(blocker, "sub"))
            rc, out, err = run("announce.py", env=env)
            self.assertEqual((rc, out), (0, ""))
            self.assertIn("announce", err)

    def test_skill_points_at_review_py_through_the_skill_dir(self):
        with open(os.path.join(PLUGIN, "skills", "review-plan", "SKILL.md")) as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: review-plan\n"))
        self.assertIn("${CLAUDE_SKILL_DIR}/../../bin/review.py", text)
        self.assertIn("--despite-check adversarial-review", text)

    def test_the_docs_state_the_rules_as_the_code_runs_them(self):     # final review M11
        with open(os.path.join(PLUGIN, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        with open(os.path.join(PLUGIN, "skills", "review-plan", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        for text in (readme, skill):
            self.assertNotRegex(text, r"(?i)up to \**3 rounds")                  # there is no hard cap
            self.assertIn("-spec-round-N", text)                                  # where a spec review lands
        self.assertIn("no hard cap", readme)
        self.assertRegex(readme, r"`\$PLUGIN` is this plugin's\s+folder")         # defined where it is used
        self.assertIn("## What a round refuses (every backend)", readme)
        refuses = readme.split("## What a round refuses (every backend)", 1)[1].split("\n## ", 1)[0]
        self.assertIn("exit code **3**", refuses.split("A tracked symlink")[0])   # the reviews/ symlink: exit 3
        self.assertIn("A tracked symlink (git mode `120000`)", refuses)
        self.assertIn("not a sandbox", readme)
        self.assertIn("next session start", readme)

    def test_plan_gate_is_0_2_0(self):
        with open(os.path.join(ROOT, "plugins", "plan-gate", ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["version"], "0.2.0")
