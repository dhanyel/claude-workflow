import json, os, re, stat, unittest
from .helpers import ROOT, PLUGIN, BIN

class TestPackaging(unittest.TestCase):
    def test_marketplace_lists_plan_gate(self):
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as f:
            m = json.load(f)
        self.assertEqual(m["name"], "claude-workflow")
        self.assertEqual([(p["name"], p["source"]) for p in m["plugins"]], [("plan-gate", "./plugins/plan-gate"),
                         ("adversarial-review", "./plugins/adversarial-review")])

    def test_plugin_manifest(self):
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            p = json.load(f)
        self.assertEqual(p["name"], "plan-gate")
        self.assertRegex(p["version"], r"^\d+\.\d+\.\d+$")

    def test_hooks_manifest_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN, "hooks", "hooks.json")))

    def test_every_referenced_script_runs_through_python3_and_is_executable(self):
        for rel in ("hooks/hooks.json", "plan-gate-check.json"):
            path = os.path.join(PLUGIN, rel)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                text = f.read().replace('\\"', '"')
            for cmd in re.findall(r'\$\{CLAUDE_PLUGIN_ROOT\}/bin/[\w.-]+', text):
                self.assertIn('python3 "' + cmd, text, cmd)
        if os.path.isdir(BIN):
            for name in os.listdir(BIN):
                if name.endswith(".py"):
                    path = os.path.join(BIN, name)
                    with open(path) as f:
                        self.assertTrue(f.readline().startswith("#!/usr/bin/env python3"), name)
                    self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, name + " is not executable")

    def test_readme_documents_every_config_key_and_the_check_contract(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for word in ("language", "plans_dir", "specs_dir", "checks", "mr_template",
                     "platform", "api_url", "GITLAB_HOST", "GH_HOST", "--false-positive",
                     '"status"', '"pass"', '"fail"', '"findings"', "nothing-to-check"):
            self.assertIn(word, readme)

    def test_skills_reach_the_scripts_through_the_skill_dir(self):
        """A-I2 (R83): CLAUDE_PLUGIN_ROOT is not set in the Bash tool; ${CLAUDE_SKILL_DIR}/../../bin is."""
        skills = os.path.join(PLUGIN, "skills")
        for name in sorted(os.listdir(skills)):
            with open(os.path.join(skills, name, "SKILL.md")) as f:
                text = f.read()
            with self.subTest(skill=name):
                self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)
                cmds = re.findall(r'python3 "\$\{CLAUDE_SKILL_DIR\}/\.\./\.\./bin/([\w.-]+)"', text)
                self.assertTrue(cmds, name)
                for script in cmds:
                    self.assertTrue(os.path.isfile(os.path.join(BIN, script)), script)
                # every mention of a bin script is the full form
                self.assertEqual(len(re.findall(r"\bbin/[\w-]+\.py", text)), len(cmds), name)

    def test_preflight_skills_teach_the_release(self):
        ids = {"preflight-plan": ("aliases", "invoked-symbols", "decorative-constants", "tolerant-defaults",
                                  "cited-files", "created-not-committed", "verification-gap"),
               "preflight-spec": ("guarantees", "citations", "support", "outside-repo", "quantities", "claims")}
        for skill, groups in ids.items():
            with open(os.path.join(PLUGIN, "skills", skill, "SKILL.md")) as f:
                text = f.read()
            with self.subTest(skill=skill):
                self.assertIn("## Release", text)
                section = text.split("## Release", 1)[1]
                for word in ("plan_gate.py\" release", "--reason", "--false-positive", "--no-coverage",
                             "human decision", *groups):
                    self.assertIn(word, section, word)

    def test_skills_define_no_shell_function(self):
        """B-M1: a shell function from one Bash tool call does not exist in the next; every step is a full command."""
        skills = os.path.join(PLUGIN, "skills")
        for name in sorted(os.listdir(skills)):
            with open(os.path.join(skills, name, "SKILL.md")) as f:
                text = f.read()
            self.assertIsNone(re.search(r"^\s*\w+\s*\(\)\s*\{", text, re.M), name)
        with open(os.path.join(skills, "delivery-report", "SKILL.md")) as f:
            text = f.read()
        self.assertIn('bin/delivery.py" notes --issue', text)

    def test_license_file_matches_the_declared_license(self):
        """A-M7: plugin.json and the README declare MIT; the file has to exist."""
        with open(os.path.join(ROOT, "LICENSE")) as f:
            text = f.read()
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 Dhanyel Nunes", text)
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["license"], "MIT")

    def test_ci_uses_current_actions(self):
        """L62: checkout@v4 and setup-python@v5."""
        with open(os.path.join(ROOT, ".github", "workflows", "ci.yml")) as f:
            uses = re.findall(r"uses:\s*(\S+)", f.read())
        self.assertEqual(sorted(uses), ["actions/checkout@v4", "actions/setup-python@v5"])

    def test_readme_warns_against_a_global_state_dir(self):
        """A-M5: another gate that honours PLAN_GATE_DIR would share the state folder."""
        with open(os.path.join(ROOT, "README.md")) as f:
            row = [l for l in f.read().splitlines() if l.startswith("| `PLAN_GATE_DIR`")][0]
        self.assertIn("never set it globally", row)

