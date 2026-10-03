"""Warning when the INTERNAL gate (`revisao-de-plano@pentagrama`) is still enabled and blocking (R41)."""
import json, os, tempfile, unittest
from unittest import mock
from .helpers import import_bin, run, temp_state, fake_repo, fake_plan, hook_input, denied

gate = import_bin("plan_gate")


def write_settings(base, enabled=True, env=None, name="settings.json", raw=None):
    os.makedirs(os.path.join(base, ".claude"), exist_ok=True)
    path = os.path.join(base, ".claude", name)
    with open(path, "w") as f:
        if raw is not None:
            f.write(raw)
        else:
            d = {"enabledPlugins": {"revisao-de-plano@pentagrama": enabled}}
            if env is not None:
                d["env"] = env
            json.dump(d, f)
    return path


class TestCoexistence(unittest.TestCase):
    def _settings(self, home, enabled, env=None):
        write_settings(home, enabled, env)

    def test_enabled_and_blocking_is_reported(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAO_DE_PLANO", None)
            self._settings(home, True)
            self.assertIsNotNone(gate.internal_gate_active(home=home))

    def test_enabled_but_switched_off_in_settings_env_is_fine(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAO_DE_PLANO", None)
            self._settings(home, True, {"PORTAO_DE_PLANO": "off"})
            self.assertIsNone(gate.internal_gate_active(home=home))

    def test_switched_off_in_the_environment_is_fine(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"PORTAO_DE_PLANO": "off"}):
            self._settings(home, True)
            self.assertIsNone(gate.internal_gate_active(home=home))

    def test_disabled_is_fine(self):
        with tempfile.TemporaryDirectory() as home:
            self._settings(home, False)
            self.assertIsNone(gate.internal_gate_active(home=home))

    def test_project_level_settings_are_reported(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAO_DE_PLANO", None)
            path = write_settings(repo, True, name="settings.local.json")
            self.assertIsNone(gate.internal_gate_active(home=home))
            self.assertEqual(gate.internal_gate_active(home=home, repo=repo), path)

    def test_off_in_any_file_env_wins_over_an_enabling_file(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAO_DE_PLANO", None)
            write_settings(home, True)
            write_settings(repo, False, {" PORTAO_DE_PLANO": "x"}, name="settings.local.json")
            write_settings(repo, False, {"PORTAO_DE_PLANO": " OFF "})
            self.assertIsNone(gate.internal_gate_active(home=home, repo=repo))

    def test_broken_settings_are_ignored_without_exception(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAO_DE_PLANO", None)
            write_settings(home, raw="{not json")
            write_settings(home, raw="[1, 2]", name="settings.local.json")
            self.assertIsNone(gate.internal_gate_active(home=home))
            write_settings(home, raw='{"enabledPlugins": [], "env": 3}')
            self.assertIsNone(gate.internal_gate_active(home=home))


class TestWarningOutput(unittest.TestCase):
    def _denied_repo(self, env):
        """A repo with a pending plan, so that an edit is denied."""
        return fake_repo()

    def test_denial_includes_the_warning_when_internal_is_active(self):
        with temp_state() as env, fake_repo() as repo:
            env["PLAN_GATE_LANG"] = "en"
            settings = write_settings(env["HOME"], True)
            plan = fake_plan(repo)
            rc, out, err = run("plan_gate.py", "mark", env=env,
                               stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            rc, out, err = run("plan_gate.py", "check", env=env,
                               stdin=hook_input("Edit", {"file_path": os.path.join(repo, "x.py")}, repo))
            self.assertTrue(denied(rc), err)
            self.assertEqual(out, "")
            self.assertIn(settings, err)
            self.assertIn("PORTAO_DE_PLANO", err)

    def test_denial_without_internal_has_no_warning(self):
        with temp_state() as env, fake_repo() as repo:
            env["PLAN_GATE_LANG"] = "en"
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env,
                stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            rc, out, err = run("plan_gate.py", "check", env=env,
                               stdin=hook_input("Edit", {"file_path": os.path.join(repo, "x.py")}, repo))
            self.assertTrue(denied(rc), err)
            self.assertNotIn("PORTAO_DE_PLANO", err)

    def test_allowed_check_is_silent_even_with_internal_active(self):
        with temp_state() as env, fake_repo() as repo:
            write_settings(env["HOME"], True)
            rc, out, err = run("plan_gate.py", "check", env=env,
                               stdin=hook_input("Edit", {"file_path": os.path.join(repo, "x.py")}, repo))
            self.assertEqual((rc, out, err), (0, "", ""))

    def test_status_always_warns_on_stderr_only(self):
        with temp_state() as env, fake_repo() as repo:
            env["PLAN_GATE_LANG"] = "en"
            settings = write_settings(env["HOME"], True)
            rc, out, err = run("plan_gate.py", "status", env=env, cwd=repo)
            self.assertEqual(rc, 0)
            self.assertIn(settings, err)
            self.assertNotIn(settings, out)


if __name__ == "__main__":
    unittest.main()
