import json, os, tempfile, unittest
from unittest import mock
from .helpers import PLUGIN, import_bin

i18n = import_bin("i18n")

class TestI18n(unittest.TestCase):
    def test_default_is_english(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAN_GATE_LANG", None)
            self.assertEqual(i18n.language(None), "en")

    def test_env_overrides_default(self):
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "pt-BR"}):
            self.assertEqual(i18n.language(None), "pt-BR")

    def test_repo_config_wins_over_env(self):
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "pt-BR"}):
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": "en"}, f)
            self.assertEqual(i18n.language(repo), "en")

    def test_both_locales_have_the_same_keys(self):
        with open(os.path.join(PLUGIN, "locales", "en.json")) as f:
            en = json.load(f)
        with open(os.path.join(PLUGIN, "locales", "pt-BR.json")) as f:
            pt = json.load(f)
        self.assertEqual(sorted(en), sorted(pt))

    def test_unknown_key_fails_loudly(self):
        with self.assertRaises(KeyError):
            i18n.t("no.such.key")

    def test_formats_arguments_in_each_language(self):
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "en"}):
            self.assertIn("plan.md", i18n.t("gate.pending", plan="plan.md"))
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "pt-BR"}):
            self.assertIn("PENDENTE", i18n.t("gate.pending", plan="plan.md"))

    def test_unsupported_language_in_config_with_valid_env(self):
        """Unsupported config language falls through to valid env."""
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "pt-BR"}):
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": "fr"}, f)
            self.assertEqual(i18n.language(repo), "pt-BR")

    def test_unsupported_language_in_config_no_env(self):
        """Unsupported config language falls back to en."""
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAN_GATE_LANG", None)
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": "fr"}, f)
            self.assertEqual(i18n.language(repo), "en")

    def test_unsupported_env_language(self):
        """Unsupported PLAN_GATE_LANG falls back to en."""
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "fr"}):
            self.assertEqual(i18n.language(None), "en")

    def test_non_string_config_language(self):
        """Non-string config language falls through without raising."""
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAN_GATE_LANG", None)
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": ["x"]}, f)
            self.assertEqual(i18n.language(repo), "en")

    def test_t_works_with_unsupported_config_language(self):
        """Translation works when config has unsupported language."""
        with tempfile.TemporaryDirectory() as repo, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAN_GATE_LANG", None)
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": "fr"}, f)
            # Should fall back to en and work
            self.assertIn("PENDING", i18n.t("gate.pending", repo=repo, plan="test.md"))

    def test_t_works_with_unsupported_env_language(self):
        """Translation works when PLAN_GATE_LANG is unsupported."""
        with mock.patch.dict(os.environ, {"PLAN_GATE_LANG": "fr"}):
            # Should fall back to en and work
            self.assertIn("PENDING", i18n.t("gate.pending", plan="test.md"))
