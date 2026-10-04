"""The `opencode`/`endpoint` backend, configuration half (adversarial-review spec §5.2, §5.3).

Ported from the INTERNAL test_backend_opencode.py (18 of 22 tests, some merged, across TestConfigGerado, TestProvedorDerivado,
TestOpencodeAusente, TestLeituraDeVersaoQueFalhaNaoBloqueia; see the task report for the dropped ones).
"""
import json, os, shutil, subprocess, tempfile, unittest
from unittest import mock
from .helpers import import_bin

settings = import_bin("settings")
bo = import_bin("backend_opencode")

TOKEN = "tok-SECRET-123"
ENDPOINT_SETTINGS = settings.Settings("endpoint", model="glm-5.3", endpoint_url="https://gw.example", token=TOKEN)
ZAI = settings.Settings("opencode", model="zai-coding-plan/glm-5.3")
OTHER = settings.Settings("opencode", model="deepseek/deepseek-v3")
ENV_TOKEN = "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ar-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.rev_dir = os.path.join(self.repo, "docs", "superpowers", "plans", "reviews")
        os.makedirs(self.rev_dir)
        self.cfg = os.path.join(self.tmp, "opencode.json")
        self.out = os.path.join(self.rev_dir, "2026-01-01-x-endpoint-round-2.json")

    def gen(self, s=ENDPOINT_SETTINGS, thinking=False):
        return bo.generate_config(self.repo, self.out, s, thinking, self.cfg)


class TestGeneratedConfig(Base):
    # from TestConfigGerado.test_nenhuma_permissao_em_ask
    def test_no_permission_is_ask(self):
        perm = self.gen()["agent"]["plan-reviewer"]["permission"]
        self.assertNotIn('"ask"', json.dumps(perm))
        for key in ("external_directory", "question", "webfetch", "websearch", "task", "skill"):
            self.assertEqual(perm[key], "deny", key)
        self.assertEqual(perm["doom_loop"], "allow")

    # from TestConfigGerado.test_thinking_desligado_por_padrao_e_ligado_so_explicitamente
    def test_thinking_is_off_by_default_and_on_only_explicitly(self):
        self.assertEqual(self.gen(thinking=False)["agent"]["plan-reviewer"]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", self.gen(thinking=True)["agent"]["plan-reviewer"])

    def test_the_reviewer_has_no_shell_and_the_permissions_are_pinned_exactly(self):
        agent = self.gen()["agent"]["plan-reviewer"]
        self.assertIs(agent["tools"]["bash"], False)
        self.assertEqual(agent["permission"]["bash"], "deny")
        self.assertEqual(list(agent["permission"])[0], "*")
        self.assertEqual(agent["permission"], {
            "*": "deny", "edit": {"*": "deny", os.path.relpath(self.out, self.repo): "allow", self.out: "allow"},
            "bash": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
            "webfetch": "deny", "websearch": "deny", "task": "deny", "skill": "deny",
            "external_directory": "deny", "question": "deny", "invalid": "allow", "doom_loop": "allow"})

    # from TestConfigGerado.test_so_pode_escrever_a_review
    def test_edit_only_this_rounds_output_json(self):
        """R18 (final review I5): not `reviews/*` -- earlier rounds' JSON and the -override.md stay out of reach."""
        perm = self.gen()["agent"]["plan-reviewer"]["permission"]["edit"]
        self.assertEqual(perm["*"], "deny")
        self.assertEqual(sorted(k for k, v in perm.items() if v == "allow"),
                         sorted(["docs/superpowers/plans/reviews/2026-01-01-x-endpoint-round-2.json", self.out]))
        self.assertFalse(any("*" in k for k, v in perm.items() if v == "allow"))

    # from TestConfigGerado.test_config_global_nao_emite_apiKey (+ TestProvedorDerivado proxy-only-for-zai)
    def test_zai_gets_the_local_proxy_and_no_key(self):
        opts = self.gen(ZAI)["provider"]["zai-coding-plan"]["options"]
        self.assertEqual(opts, {"baseURL": f"http://127.0.0.1:{bo.ZAI_PROXY_PORT}"})
        self.assertEqual(bo.ZAI_PROXY_PORT, 8788)

    # from TestProvedorDerivado.test_config_declara_o_provedor_do_modelo / provedor_que_nao_e_zai / proxy_local_SO_para_zai
    def test_another_provider_gets_no_provider_block_at_all(self):
        data = self.gen(OTHER)
        self.assertNotIn("provider", data)
        self.assertEqual(data["agent"]["plan-reviewer"]["model"], "deepseek/deepseek-v3")
        text = json.dumps(data)
        self.assertNotIn("ZAI_API_KEY", text)
        self.assertNotIn("8788", text)

    def test_the_token_never_lands_in_the_generated_file(self):
        s = settings.Settings("endpoint", model="glm-5.3", endpoint_url="https://gw.example", token=TOKEN)
        bo.generate_config(self.repo, self.out, s, False, self.cfg)
        with open(self.cfg) as f:
            text = f.read()
        self.assertNotIn(TOKEN, text)
        self.assertIn("{env:ADVERSARIAL_REVIEW_ENDPOINT_TOKEN}", text)

    def test_the_endpoint_provider_is_the_only_one_with_a_key(self):
        data = self.gen()
        keyed = [p for p, v in data["provider"].items() if "apiKey" in v.get("options", {})]
        self.assertEqual(keyed, ["adversarial-endpoint"])
        self.assertEqual(bo.run_model(ENDPOINT_SETTINGS), "adversarial-endpoint/glm-5.3")
        self.assertEqual(bo.run_model(ZAI), "zai-coding-plan/glm-5.3")
        prov = data["provider"]["adversarial-endpoint"]
        self.assertEqual(prov["options"]["baseURL"], "https://gw.example")
        self.assertEqual(prov["models"], {"glm-5.3": {"name": "glm-5.3"}})
        self.assertEqual(data["agent"]["plan-reviewer"]["model"], "adversarial-endpoint/glm-5.3")
        for s in (ZAI, OTHER):
            self.assertNotIn("apiKey", json.dumps(self.gen(s)))

    def test_the_file_on_disk_is_what_is_returned(self):
        data = self.gen()
        with open(self.cfg) as f:
            self.assertEqual(json.load(f), data)

    def test_the_agent_prompt_comes_from_the_plugin_prompt_file(self):
        with open(os.path.join(bo.PROMPTS, "agent.md"), encoding="utf-8") as f:
            self.assertEqual(self.gen()["agent"]["plan-reviewer"]["prompt"], f.read())


class TestChildEnv(Base):
    def test_child_env_carries_the_token_only_for_endpoint(self):
        self.gen()
        with mock.patch.dict(os.environ, {ENV_TOKEN: "leftover"}):
            self.assertEqual(bo.child_env(ENDPOINT_SETTINGS, self.cfg)[ENV_TOKEN], TOKEN)
            self.assertNotIn(ENV_TOKEN, bo.child_env(settings.Settings("opencode", model="x/y"), self.cfg))

    def test_child_env_points_opencode_at_the_round_config(self):
        self.gen()
        self.assertEqual(bo.child_env(ENDPOINT_SETTINGS, self.cfg)["OPENCODE_CONFIG"], self.cfg)

    def test_project_config_is_disabled_and_the_content_is_the_generated_config_without_the_token(self):
        for s in (ENDPOINT_SETTINGS, ZAI, OTHER):
            data = self.gen(s)
            env = bo.child_env(s, self.cfg)
            self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), data)
            self.assertNotIn(TOKEN, env["OPENCODE_CONFIG_CONTENT"])


class TestModelParts(unittest.TestCase):
    # from TestProvedorDerivado
    def test_splits_provider_from_model(self):
        self.assertEqual(bo.model_parts("zai-coding-plan/glm-5.3"), ("zai-coding-plan", "glm-5.3"))
        self.assertEqual(bo.model_parts("deepseek/deepseek-v3"), ("deepseek", "deepseek-v3"))

    def test_no_slash_means_no_provider(self):
        self.assertEqual(bo.model_parts("glm-5.3"), ("", "glm-5.3"))

    def test_only_the_first_slash_splits(self):
        self.assertEqual(bo.model_parts("a/b/c"), ("a", "b/c"))

    def test_an_empty_side_is_malformed(self):
        for bad in ("deepseek/", "/glm-5.3"):
            with self.assertRaises(ValueError) as cm:
                bo.model_parts(bad)
            self.assertIn(bad, str(cm.exception))


class TestNoLspNoFormatter(Base):
    def test_lsp_and_formatter_are_off_in_the_file_and_in_the_inline_content(self):
        # with them on in the user's GLOBAL config, opencode runs the REPO's eslint/prettier with the whole env
        for s in (ENDPOINT_SETTINGS, ZAI, OTHER):
            data = self.gen(s)
            self.assertIs(data["lsp"], False)
            self.assertIs(data["formatter"], False)
            content = json.loads(bo.child_env(s, self.cfg)["OPENCODE_CONFIG_CONTENT"])
            self.assertIs(content["lsp"], False)
            self.assertIs(content["formatter"], False)


class TestOpencodeAbsent(unittest.TestCase):
    # from TestOpencodeAusente
    def test_without_the_binary_the_message_teaches_the_install(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIn("opencode.ai/install", bo.check_opencode("en"))
            self.assertIn("não está instalado", bo.check_opencode("pt-BR"))

    def test_an_old_version_names_both_numbers(self):
        with mock.patch("shutil.which", return_value="/usr/bin/opencode"), \
             mock.patch.object(bo, "opencode_version", return_value=(1, 2, 0)):
            msg = bo.check_opencode("en")
        self.assertIn("1.18", msg)
        self.assertIn("1.2.0", msg)
        self.assertIn("opencode.ai/install", msg)

    def test_a_good_version_does_not_complain(self):
        with mock.patch("shutil.which", return_value="/usr/bin/opencode"), \
             mock.patch.object(bo, "opencode_version", return_value=(1, 18, 27)):
            self.assertIsNone(bo.check_opencode("en"))

    def test_the_floor_is_exactly_the_floor(self):
        with mock.patch("shutil.which", return_value="/usr/bin/opencode"):
            for v, expected_none in (((1, 18, 0), True), ((1, 17, 99), False)):
                with mock.patch.object(bo, "opencode_version", return_value=v):
                    self.assertEqual(bo.check_opencode("en") is None, expected_none, v)


class TestVersionReadThatFailsDoesNotBlock(unittest.TestCase):
    # from TestLeituraDeVersaoQueFalhaNaoBloqueia (3 tests; measured by EFFECT, check_opencode stays None)
    def _check(self, **run_kw):
        with mock.patch("shutil.which", return_value="/usr/bin/opencode"), mock.patch("subprocess.run", **run_kw):
            return bo.check_opencode("en")

    def test_a_system_error_does_not_block(self):
        self.assertIsNone(self._check(side_effect=OSError("Permission denied")))

    def test_a_timeout_does_not_block(self):
        self.assertIsNone(self._check(side_effect=subprocess.TimeoutExpired(cmd=["opencode", "--version"], timeout=10)))

    def test_output_without_a_version_number_does_not_block(self):
        out = subprocess.CompletedProcess(["opencode", "--version"], 0, stdout="opencode: unknown\n", stderr="")
        self.assertIsNone(self._check(return_value=out))

    def test_the_version_is_read_from_the_resolved_executable(self):
        out = subprocess.CompletedProcess(["x"], 0, stdout="1.18.27\n", stderr="")
        with mock.patch("shutil.which", return_value="/opt/oc/bin/opencode"), \
             mock.patch("subprocess.run", return_value=out) as run_:
            self.assertIsNone(bo.check_opencode("en"))
        self.assertEqual(run_.call_args.args[0], ["/opt/oc/bin/opencode", "--version"])

    def test_a_real_read_parses_the_number(self):
        out = subprocess.CompletedProcess(["opencode", "--version"], 0, stdout="1.18.27\n", stderr="")
        with mock.patch("subprocess.run", return_value=out):
            self.assertEqual(bo.opencode_version(), (1, 18, 27))


if __name__ == "__main__":
    unittest.main()
