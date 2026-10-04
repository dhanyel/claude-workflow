"""The `opencode`/`endpoint` backend, RUNNER half (adversarial-review spec §5.2, §5.3), and `zai_proxy.py`.

Ported from the INTERNAL test_backend_opencode.py (origin names in each docstring):
  TestTelemetria (2), TestModeloDoStream (2), TestContratoDoBackend (2 of 5), TestProxyNaRotaDireta (1 of 3), and,
  carried from Task 12, TestProvedorDerivado.test_preparar_nao_deixa_modelo_malformado_virar_traceback and the two of
  TestChecagemDeOpencodeNaProducao.
Dropped (no route axis, no gateway, no config.json, no REVISOR_* here):
  test_consome_o_eixo_rota, test_sem_credencial_devolve_motivo_e_NAO_roda_nada,
  test_sem_credencial_a_mensagem_diz_quais_arquivos_foram_lidos, test_config_global_sem_opt_in_aborta_em_vez_de_gastar,
  test_config_global_COM_opt_in_segue_em_frente; and the ROTULO half of test_repara_e_expoe_o_rotulo.

⚠️ The REAL opencode never runs here: every test that reaches the runner puts tests/fakes on the front of PATH and
asserts that `shutil.which("opencode")` is the fake.
"""
import gc, hashlib, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error
import urllib.request, warnings
from unittest import mock
from .helpers import BIN, FAKES, FakeBackend, contract_of, fake_repo, import_bin, isolated_env, run, write
from .test_round import BASE, PLAN, RoundCase, wire

loop = import_bin("loop")
settings = import_bin("settings")
state = import_bin("state")
bo = import_bin("backend_opencode")
backend_opencode = bo

TOKEN = "tok-SECRET-123"
ENDPOINT_SETTINGS = settings.Settings("endpoint", model="glm-5.3", endpoint_url="https://gw.example", token=TOKEN)
ZAI = settings.Settings("opencode", model="zai-coding-plan/glm-5.3")
OTHER = settings.Settings("opencode", model="deepseek/deepseek-v3")
FAKE_OPENCODE = os.path.join(FAKES, "opencode")
# 64 chars with a `"` and a `\` (json.dumps escapes both): the fake tiles it so EVERY truncation cuts through one
NASTY = 'k"' + hashlib.sha256(b"x").hexdigest()[:30] + "\\" + hashlib.sha256(b"y").hexdigest()[:31]
NASTY_ENDPOINT = settings.Settings("endpoint", model="glm-5.3", endpoint_url="https://gw.example", token=NASTY)


def strings(value):
    """Every string inside `value` (keys included), decoded -- no JSON escaping to hide a fragment behind."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in strings(k) + strings(v)]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in strings(v)]
    return []


class NoSlice:
    def assertNoSlice(self, text, token=NASTY, where=""):
        for i in range(len(token) - 7):
            self.assertNotIn(token[i:i + 8], text, f"{where}: a fragment of the token leaked")


def fake_env(**extra):
    env = isolated_env(**extra)
    env["PATH"] = FAKES + os.pathsep + env["PATH"]
    return env


class RunCase(unittest.TestCase):
    """bo.review / bo.repair called directly, with the fake opencode first on PATH."""

    def setUp(self):
        self.log = os.path.join(tempfile.mkdtemp(prefix="ar-oc-log-"), "calls.jsonl")
        patcher = mock.patch.dict(os.environ, fake_env(FAKE_OPENCODE_LOG=self.log), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(shutil.which("opencode"), FAKE_OPENCODE)
        repo_cm = fake_repo()
        self.repo = repo_cm.__enter__()
        self.addCleanup(repo_cm.__exit__, None, None, None)
        self.plan = write(os.path.join(self.repo, "docs", "superpowers", "plans", BASE + ".md"), PLAN)
        self.rev_dir = os.path.join(os.path.dirname(self.plan), "reviews")
        os.makedirs(self.rev_dir)
        self.out = os.path.join(self.rev_dir, BASE + "-opencode-round-1.json")
        self.cfg = os.path.join(tempfile.mkdtemp(prefix="ar-oc-cfg-"), "opencode.json")

    def mode(self, mode):
        os.environ["FAKE_OPENCODE_MODE"] = mode

    def options(self, **kw):
        return loop.Options(**kw)

    def review(self, s=OTHER, **kw):
        options = self.options(**kw)
        return bo.review(path=self.plan, repo=self.repo, prompt="PROMPT", output_json=self.out, settings=s,
                         options=options, deadline=time.monotonic() + options.timeout_s)

    def repair(self, s=OTHER, **kw):
        options = self.options(**kw)
        return bo.repair(path=self.plan, repo=self.repo, session="ses-prev", reason="invalid JSON: x",
                         output_json=self.out, settings=s, options=options,
                         deadline=time.monotonic() + options.timeout_s)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


# ---- ported ------------------------------------------------------------------------------------------------------

class TestTelemetry(unittest.TestCase):
    def test_reads_steps_tools_and_session(self):
        """INTERNAL TestTelemetria.test_le_passos_ferramentas_e_sessao."""
        p = os.path.join(tempfile.mkdtemp(), "ev.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for e in ({"sessionID": "ses-9", "part": {"type": "step-start"}},
                      {"part": {"type": "tool", "tool": "grep"}},
                      {"part": {"type": "tool", "tool": "grep"}},
                      {"part": {"type": "step-finish", "tokens": {"input": 10}}},
                      {"part": {"type": "text", "text": "the end"}}):
                f.write(json.dumps(e) + "\n")
        tel, session, text = bo.telemetry_from_stream(p)
        self.assertEqual((tel["steps"], tel["tools"]["grep"], tel["tokens"], session), (1, 2, {"input": 10}, "ses-9"))
        self.assertIn("the end", text)

    def test_an_unreadable_stream_does_not_blow_up(self):
        """INTERNAL TestTelemetria.test_stream_ilegivel_nao_estoura."""
        tel, session, text = bo.telemetry_from_stream("/does/not/exist.jsonl")
        self.assertEqual((tel, session, text), ({}, None, ""))

    def test_garbage_lines_are_skipped_and_errors_become_text(self):
        p = write(os.path.join(tempfile.mkdtemp(), "ev.jsonl"),
                  "not json\n" + json.dumps({"type": "error", "error": {"message": "quota"}}) + "\n")
        tel, session, text = bo.telemetry_from_stream(p)
        self.assertEqual(tel["steps"], 0)
        self.assertIn("quota", text)


    def test_a_json_escaped_token_inside_an_event_is_masked_too(self):
        token = "tok-\u00e9-SECRET"
        escaped = json.dumps(token)[1:-1]
        body = '{"error": "bad key ' + escaped + '"}'          # a provider body quoted inside the event text
        p = write(os.path.join(tempfile.mkdtemp(), "ev.jsonl"),
                  json.dumps({"part": {"type": "text", "text": body}}) + "\n"
                  + json.dumps({"type": "error", "error": {"message": token}}) + "\n")
        tel, _session, text = bo.telemetry_from_stream(p, token)
        self.assertNotIn(escaped, text)
        self.assertNotIn(token, text)


class TestModelFromTheStream(unittest.TestCase):
    """The model that ANSWERED is not necessarily the one asked for; opencode does not emit it today."""

    def test_reads_the_model_from_the_stream(self):
        """INTERNAL TestModeloDoStream.test_le_o_modelo_do_stream."""
        p = os.path.join(tempfile.mkdtemp(), "ev.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"sessionID": "s1", "part": {"type": "step-start"}}) + "\n")
            f.write(json.dumps({"sessionID": "s1", "modelID": "glm-5.3", "providerID": "zai-coding-plan"}) + "\n")
        tel, _session, _text = bo.telemetry_from_stream(p)
        self.assertEqual(tel["model"], "zai-coding-plan/glm-5.3")

    def test_a_stream_without_a_model_gives_empty_and_does_not_blow_up(self):
        """INTERNAL TestModeloDoStream.test_stream_sem_modelo_devolve_vazio_e_nao_explode."""
        p = os.path.join(tempfile.mkdtemp(), "ev.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"sessionID": "s1", "part": {"type": "step-start"}}) + "\n")
        tel, _s, _t = bo.telemetry_from_stream(p)
        self.assertEqual(tel["model"], "")


class TestProviderRefusalIsNotRepairable(RunCase):
    """`Result.repairable`: False only when the provider's own error event says a repeat cannot help. The event is
    read as JSON (opencode `--format json`: {"type": "error", "error": {"name": "APIError", "data": {...}}}, the shape
    measured against an endpoint that answered 403), never by substring."""

    def test_a_403_that_is_not_retryable_is_not_repairable(self):
        self.mode("api_403")
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIs(r.repairable, False)
        self.assertIn("403", r.reason)

    def test_a_retryable_500_stays_repairable(self):
        self.mode("api_500")
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIs(r.repairable, True)

    def test_a_failure_without_an_api_event_stays_repairable(self):
        self.mode("exit1")
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIs(r.repairable, True)

    def test_a_failed_repair_reports_it_too(self):
        self.mode("api_403")
        r = self.repair()
        self.assertFalse(r.ok)
        self.assertIs(r.repairable, False)

    def test_the_decision_reads_the_fields_not_the_text(self):
        p = write(os.path.join(tempfile.mkdtemp(), "ev.jsonl"),
                  json.dumps({"part": {"type": "text", "text": '"statusCode": 403, "isRetryable": false'}}) + "\n")
        self.assertFalse(bo.provider_refused(p))
        p = write(os.path.join(tempfile.mkdtemp(), "ev2.jsonl"),
                  json.dumps({"type": "error", "error": {"name": "APIError", "data": {"statusCode": 429,
                                                                                  "isRetryable": True}}}) + "\n")
        self.assertFalse(bo.provider_refused(p))


class TestBackendContract(unittest.TestCase):
    def test_repairs(self):
        """INTERNAL TestContratoDoBackend.test_repara_e_expoe_o_rotulo (the ROTULO half is dropped)."""
        self.assertTrue(bo.CAN_REPAIR)

    def test_consumes_the_model_axis(self):
        """INTERNAL TestContratoDoBackend.test_consome_o_eixo_modelo: asserted on the REAL module."""
        self.assertTrue(bo.CAN_MODEL)

    def test_meets_the_backend_contract(self):
        self.assertIsNone(loop.backend_problem(backend_opencode))

    def test_the_round_tests_fake_is_shaped_like_this_backend(self):
        self.assertEqual(contract_of(FakeBackend.like(backend_opencode, [])), contract_of(backend_opencode))

    def test_routes(self):
        self.assertEqual(backend_opencode.route(ENDPOINT_SETTINGS), "endpoint gw.example")
        self.assertEqual(backend_opencode.route(settings.Settings("opencode", model="x/y")),
                         "opencode (user provider)")


class TestPrepareRefusesReadably(RunCase):
    def test_a_malformed_model_does_not_become_a_traceback(self):
        """INTERNAL TestProvedorDerivado.test_preparar_nao_deixa_modelo_malformado_virar_traceback."""
        r = self.review(settings.Settings("opencode", model="deepseek/"))
        self.assertFalse(r.ok)
        self.assertIn("deepseek/", r.reason)
        self.assertEqual(self.calls(), [])

    def test_review_refuses_and_teaches_when_opencode_is_missing(self):
        """INTERNAL TestChecagemDeOpencodeNaProducao.test_revisar_recusa_e_ensina_quando_opencode_ausente."""
        with mock.patch("shutil.which", return_value=None):
            r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("opencode.ai/install", r.reason)
        self.assertEqual(self.calls(), [])

    def test_repair_refuses_and_teaches_when_opencode_is_missing(self):
        """INTERNAL TestChecagemDeOpencodeNaProducao.test_reparar_recusa_e_ensina_quando_opencode_ausente."""
        with mock.patch("shutil.which", return_value=None):
            r = self.repair()
        self.assertFalse(r.ok)
        self.assertIn("opencode.ai/install", r.reason)
        self.assertEqual(self.calls(), [])

    def test_an_old_opencode_is_refused_before_running(self):
        os.environ["FAKE_OPENCODE_VERSION"] = "1.17.9"
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("1.17.9", r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_symlink_inside_the_reviews_folder_refuses_the_round(self):
        # the edit permission is a path glob: a committed symlink there pointing into src/ would be written through
        src = os.path.join(self.repo, "src")
        os.makedirs(src)
        os.symlink(os.path.join(src, "app.py"), os.path.join(self.rev_dir, "trap.json"))
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("symlink", r.reason)
        self.assertIn("trap.json", r.reason)
        self.assertEqual(self.calls(), [])
        r = self.repair()
        self.assertFalse(r.ok)
        self.assertIn("symlink", r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_symlink_deeper_in_the_reviews_folder_refuses_too(self):
        os.makedirs(os.path.join(self.rev_dir, "old"))
        os.symlink(self.repo, os.path.join(self.rev_dir, "old", "up"))
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("symlink", r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_reviews_folder_that_is_itself_a_symlink_refuses_the_round(self):
        shutil.rmtree(self.rev_dir)
        elsewhere = os.path.join(self.repo, "src")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.rev_dir)
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("symlink", r.reason)
        self.assertEqual(self.calls(), [])


class TestProxyOnTheDirectRoute(RunCase):
    """INTERNAL TestProxyNaRotaDireta: z.ai hangs the SECOND call on a reused connection; the local proxy avoids it."""

    def test_repair_also_ensures_the_proxy(self):
        """INTERNAL test_reparar_tambem_garante_o_proxy: the repair spends quota like the review."""
        calls = []
        with mock.patch.object(bo, "ensure_proxy", side_effect=lambda d: calls.append(d) or True):
            r = self.repair(ZAI)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(len(calls), 1, "the repair ran without ensuring the proxy")

    def test_review_ensures_the_proxy_for_zai(self):
        with mock.patch.object(bo, "ensure_proxy", return_value=True) as ensure:
            r = self.review(ZAI)
        self.assertTrue(r.ok, r.reason)
        ensure.assert_called_once()

    def test_no_proxy_for_another_provider_or_for_the_endpoint(self):
        with mock.patch.object(bo, "ensure_proxy", return_value=True) as ensure:
            self.assertTrue(self.review(OTHER).ok)
            self.assertTrue(self.review(ENDPOINT_SETTINGS).ok)
            self.assertTrue(self.review(settings.Settings("endpoint", model="zai-coding-plan/glm-5.3",
                                                          endpoint_url="https://gw.example", token=TOKEN)).ok)
        ensure.assert_not_called()

    def test_a_proxy_that_does_not_come_up_refuses_with_the_port(self):
        with mock.patch.object(bo, "ensure_proxy", return_value=False):
            r = self.review(ZAI)
        self.assertFalse(r.ok)
        self.assertIn(str(bo.ZAI_PROXY_PORT), r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_wildcard_in_the_file_name_refuses_before_opencode_runs(self):
        out = os.path.join(self.rev_dir, "a*b-opencode-round-1.json")
        with mock.patch.object(bo, "opencode_version", return_value=(1, 18, 32)):
            r = bo.review(path=self.plan, repo=self.repo, prompt="p", output_json=out, settings=OTHER,
                          options=loop.Options(timeout_s=20), deadline=time.monotonic() + 20)
        self.assertFalse(r.ok)
        self.assertIn("wildcard", r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_temp_folder_that_cannot_be_created_is_a_reason(self):     # I6 (R19)
        with mock.patch.object(bo, "opencode_version", return_value=(1, 18, 32)), \
                mock.patch.object(bo.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("temporary folder", r.reason)
        self.assertEqual(self.calls(), [])

    def test_a_missing_schema_is_a_reason(self):     # I6 (R19)
        with mock.patch.object(bo, "opencode_version", return_value=(1, 18, 32)), \
                mock.patch.object(bo.loop, "SCHEMA", os.path.join(self.repo, "no-such-schema.json")):
            r, r2 = self.review(), self.repair()
        self.assertFalse(r.ok)
        self.assertIn("review schema", r.reason)
        self.assertFalse(r2.ok)
        self.assertIn("review schema", r2.reason)
        self.assertEqual(self.calls(), [])

    def test_proxy_port_matches_the_generated_config(self):
        cfg = bo.generate_config(self.repo, self.out, settings.Settings("opencode", model="zai-coding-plan/glm-5.3"),
                                 False, self.cfg)
        self.assertEqual(cfg["provider"]["zai-coding-plan"]["options"]["baseURL"],
                         f"http://127.0.0.1:{bo.ZAI_PROXY_PORT}")


class TestEnsureProxy(unittest.TestCase):
    def test_already_listening_starts_nothing(self):
        with mock.patch.object(bo, "_listening", return_value=True), \
             mock.patch.object(bo.subprocess, "Popen") as popen:
            self.assertTrue(bo.ensure_proxy(time.monotonic() + 5))
        popen.assert_not_called()

    def test_starts_the_proxy_with_this_python_in_a_new_session_and_without_the_token(self):
        answers = iter([False, False, True])
        env = dict(os.environ, ADVERSARIAL_REVIEW_ENDPOINT_TOKEN=TOKEN)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(bo, "_listening", side_effect=lambda *a, **k: next(answers)), \
             mock.patch.object(bo.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            self.assertTrue(bo.ensure_proxy(time.monotonic() + 5))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, bo.PROXY, str(bo.ZAI_PROXY_PORT)])
        self.assertNotIn("setsid", args[0])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN", kwargs["env"])
        self.assertEqual(bo.PROXY, os.path.join(BIN, "zai_proxy.py"))
        self.assertTrue(os.access(bo.PROXY, os.X_OK))

    def _fake_proxy(self, listen):
        d = tempfile.mkdtemp(prefix="ar-proxy-")
        pidfile = os.path.join(d, "pid")
        script = write(os.path.join(d, "proxy.py"),
                       "import os, socket, sys, time\n"
                       f"open({pidfile!r}, 'w').write(str(os.getpid()))\n"
                       + ("s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n" if listen else "")
                       + "time.sleep(30)\n")
        return script, pidfile

    def _alive(self, pidfile):
        with open(pidfile) as f:
            pid = int(f.read())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_a_proxy_that_never_listens_is_terminated_and_reaped(self):
        script, pidfile = self._fake_proxy(listen=False)
        with mock.patch.object(bo, "PROXY", script), mock.patch.object(bo, "ZAI_PROXY_PORT", free_port()), \
             mock.patch.object(bo, "PROXY_START_S", 1), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertFalse(bo.ensure_proxy(time.monotonic() + 30))
            gc.collect()
        self.assertFalse(self._alive(pidfile), "the proxy that never listened was left running")
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_a_proxy_that_listens_is_kept_referenced(self):
        script, pidfile = self._fake_proxy(listen=True)
        port = free_port()
        with mock.patch.object(bo, "PROXY", script), mock.patch.object(bo, "ZAI_PROXY_PORT", port), \
             warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                self.assertTrue(bo.ensure_proxy(time.monotonic() + 10))
                gc.collect()
            finally:
                proc = getattr(bo, "_proxy_proc", None)
                if proc is not None:
                    proc.kill()
                    proc.wait()
        self.assertIsNotNone(proc)
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_gives_up_when_the_proxy_dies(self):
        with mock.patch.object(bo, "_listening", return_value=False), \
             mock.patch.object(bo.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = 1
            t0 = time.monotonic()
            self.assertFalse(bo.ensure_proxy(time.monotonic() + 30))
        self.assertLess(time.monotonic() - t0, 5)


# ---- the runner ----------------------------------------------------------------------------------------------------

class TestRunner(NoSlice, RunCase):
    def test_endpoint_run_passes_the_token_only_through_the_env(self):
        r = self.review(ENDPOINT_SETTINGS)
        self.assertTrue(r.ok, r.reason)
        [call] = self.calls()
        self.assertEqual(call["token_in_env"], TOKEN)
        self.assertIsNotNone(call["config"])
        self.assertNotIn(TOKEN, call["config"])
        self.assertNotIn(TOKEN, call["config_content"])
        self.assertEqual(call["disable_project_config"], "1")
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "adversarial-endpoint/glm-5.3")
        self.assertEqual(argv[argv.index("--agent") + 1], bo.AGENT_NAME)
        self.assertEqual(argv[argv.index("--dir") + 1], self.repo)

    def test_a_user_provider_run_gets_no_token_even_if_the_parent_has_one(self):
        os.environ["ADVERSARIAL_REVIEW_ENDPOINT_TOKEN"] = TOKEN
        r = self.review(OTHER)
        self.assertTrue(r.ok, r.reason)
        [call] = self.calls()
        self.assertIsNone(call["token_in_env"])
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "deepseek/deepseek-v3")

    def test_the_prompt_carries_the_delivery_with_the_schema(self):
        self.review()
        [call] = self.calls()
        prompt = call["argv"][-1]
        self.assertTrue(prompt.startswith("PROMPT"))
        self.assertIn(f"Write the review JSON to `{self.out}`", prompt)
        self.assertIn('"spec_promises_without_task"', prompt)
        self.assertIn("BLOCKER", prompt)

    def test_success_returns_the_session_and_the_stream_telemetry(self):
        r = self.review(thinking=True)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.session, "ses-fake-1")
        self.assertEqual(r.telemetry["steps"], 1)
        self.assertEqual(r.telemetry["tokens"], {"input": 10, "output": 5})
        self.assertEqual(r.telemetry["thinking"], "on")
        with open(self.out) as f:
            self.assertEqual(json.load(f)["verdict"], "APPROVED")

    def test_the_repair_runs_in_the_same_session_with_the_reason(self):
        r = self.repair()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.session, "ses-prev")
        [call] = self.calls()
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], "ses-prev")
        self.assertIn("invalid JSON: x", argv[-1])
        self.assertIn(f"Write the review JSON to `{self.out}`", argv[-1])

    def test_silence_is_killed_by_the_no_byte_watchdog(self):
        self.mode("silent")
        t0 = time.monotonic()
        r = self.review(no_byte_s=1, timeout_s=20)
        self.assertFalse(r.ok)
        self.assertIn("1s", r.reason)
        self.assertLess(time.monotonic() - t0, 10)

    def test_a_startup_stall_gets_exactly_one_retry(self):
        self.mode("silent")
        r = self.review(no_byte_s=1, timeout_s=20)
        self.assertFalse(r.ok)
        self.assertEqual(len(self.calls()), 2)

    def test_a_stall_after_output_is_not_retried(self):
        # the review already spent quota: re-running it would pay twice; the loop's repair turn covers it
        self.mode("talk_then_silent")
        t0 = time.monotonic()
        r = self.review(no_byte_s=1, timeout_s=20)
        self.assertFalse(r.ok)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(r.session, "ses-fake-1")
        self.assertLess(time.monotonic() - t0, 10)

    def test_the_repair_is_never_retried(self):
        self.mode("silent")
        r = self.repair(no_byte_s=1, timeout_s=20)
        self.assertFalse(r.ok)
        self.assertEqual(len(self.calls()), 1)

    def test_bytes_flowing_do_not_beat_the_absolute_deadline(self):
        self.mode("chatty")
        t0 = time.monotonic()
        r = self.review(no_byte_s=1, timeout_s=2)
        self.assertFalse(r.ok)
        self.assertIn("2s", r.reason)
        self.assertIn("time limit", r.reason)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(len(self.calls()), 1)

    def test_a_nonzero_exit_without_the_json_is_a_readable_failure(self):
        self.mode("exit1")
        r = self.review()
        self.assertFalse(r.ok)
        self.assertIn("exited 1", r.reason)

    def test_an_error_quoting_the_token_never_reaches_the_reason_or_the_telemetry(self):
        self.mode("echo_token")
        r = self.review(ENDPOINT_SETTINGS)
        self.assertFalse(r.ok)
        self.assertIn("401", r.reason)
        self.assertNotIn(TOKEN, r.reason)
        self.assertNotIn(TOKEN, json.dumps(r.telemetry))

    def test_a_token_cut_by_the_truncations_never_reaches_the_reason_or_the_telemetry(self):
        self.assertEqual(len(NASTY), 64)
        self.mode("echo_token")
        r = self.review(NASTY_ENDPOINT)
        self.assertFalse(r.ok)
        self.assertIn("401", r.reason)
        self.assertNoSlice(r.reason, where="reason")
        for text in strings(r.telemetry):
            self.assertNoSlice(text, where="telemetry")

    def test_the_version_is_read_from_the_same_executable_the_runner_runs(self):
        seen = []
        with mock.patch.object(bo, "opencode_version", side_effect=lambda exe="opencode": seen.append(exe) or (1, 18, 32)):
            r = self.review()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(seen, [FAKE_OPENCODE])

    def test_a_config_that_cannot_be_written_is_a_readable_refusal_and_cleans_up(self):
        made = []
        real = tempfile.mkdtemp

        def spy(*a, **k):
            made.append(real(*a, **k))
            return made[-1]
        with mock.patch.object(bo, "PROMPTS", os.path.join(self.repo, "no-such-prompts")), \
             mock.patch.object(bo.tempfile, "mkdtemp", side_effect=spy):
            r = self.review()
            r2 = self.repair()
        self.assertFalse(r.ok)
        self.assertIn("could not write", r.reason)
        self.assertFalse(r2.ok)
        self.assertEqual(len(made), 2)
        for d in made:
            self.assertFalse(os.path.exists(d), d)
        self.assertEqual(self.calls(), [])

    def test_the_round_temp_folder_is_removed_on_success_and_on_failure(self):
        self.review()
        self.mode("silent")
        self.review(no_byte_s=1, timeout_s=20)
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertFalse(os.path.exists(os.path.dirname(call["config_path"])), call["config_path"])

    def test_the_round_temp_folder_is_removed_when_the_runner_raises(self):
        seen = []
        real = bo.generate_config

        def spy(repo, output_json, s, thinking, cfg_path):
            seen.append(cfg_path)
            return real(repo, output_json, s, thinking, cfg_path)
        with mock.patch.object(bo, "generate_config", side_effect=spy), \
             mock.patch.object(bo, "opencode_version", return_value=(1, 18, 32)), \
             mock.patch.object(bo.subprocess, "Popen", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.review()
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(os.path.dirname(seen[0])))


class FakeOpencodeRound(RoundCase):
    """loop.run_round driving the REAL backend module, with the fake opencode on PATH."""

    def setUp(self):
        super().setUp()
        self.log = os.path.join(tempfile.mkdtemp(prefix="ar-oc-log-"), "calls.jsonl")
        os.environ["PATH"] = FAKES + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_OPENCODE_LOG"] = self.log
        self.assertEqual(shutil.which("opencode"), FAKE_OPENCODE)

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TestRoundWithTheFakeOpencode(FakeOpencodeRound):
    def test_invalid_json_gets_one_repair_in_the_same_session(self):
        os.environ["FAKE_OPENCODE_MODE"] = "invalid_once"
        rc = self.round(bo, s=OTHER)
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(self.printed()["verdict"], "APPROVED")
        [entry] = self.state_of()["reviews"].values()
        self.assertEqual(entry["verdict"], "APPROVED")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--session", calls[0]["argv"])
        self.assertIn("--session", calls[1]["argv"])
        self.assertEqual(calls[1]["argv"][calls[1]["argv"].index("--session") + 1], "ses-fake-1")

    def test_a_repair_that_fails_again_is_no_verdict_after_exactly_two_runs(self):
        os.environ["FAKE_OPENCODE_MODE"] = "invalid_always"
        rc = self.round(bo, s=OTHER)
        self.assertEqual(rc, 3, self.err)
        self.assertEqual(len(self.calls()), 2)

    def test_the_token_never_reaches_stdout_stderr_md_json_or_state(self):
        rc = self.round(bo, s=ENDPOINT_SETTINGS)
        self.assertEqual(rc, 0, self.err)
        [call] = self.calls()
        self.assertEqual(call["token_in_env"], TOKEN)   # it did travel, through the child's env only
        self.assertNotIn(TOKEN, self.out)
        self.assertNotIn(TOKEN, self.err)
        md = os.path.join(self.rev, BASE + "-endpoint-round-1.md")
        js = md[:-3] + ".json"
        for p in (md, js, state.state_path(self.plan)):
            with open(p, encoding="utf-8") as f:
                self.assertNotIn(TOKEN, f.read(), p)
        with open(md, encoding="utf-8") as f:
            self.assertIn("endpoint gw.example", f.read())
        for folder in (self.rev, os.environ["ADVERSARIAL_REVIEW_DIR"]):
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                        self.assertNotIn(TOKEN, f.read(), os.path.join(root, name))
        self.assertFalse(os.path.exists(os.path.dirname(call["config_path"])))

    def test_a_failure_quoting_the_token_leaves_it_out_of_stderr_and_last_failure(self):
        os.environ["FAKE_OPENCODE_MODE"] = "echo_token"
        rc = self.round(bo, s=ENDPOINT_SETTINGS)
        self.assertEqual(rc, 3, self.err)
        self.assertNotIn(TOKEN, self.out + self.err)
        with open(state.state_path(self.plan), encoding="utf-8") as f:
            self.assertNotIn(TOKEN, f.read())


class TestTokenCutByTruncation(NoSlice, FakeOpencodeRound):
    """A provider error quoting the key, tiled so the 160/300 cuts land INSIDE a copy of it, with `"` and `\\`."""

    def test_on_no_verdict_no_fragment_reaches_stdout_stderr_or_the_state(self):
        os.environ["FAKE_OPENCODE_MODE"] = "echo_token"
        rc = self.round(bo, s=NASTY_ENDPOINT)
        self.assertEqual(rc, 3, self.err)
        self.assertNoSlice(self.out, where="stdout")
        self.assertNoSlice(self.err, where="stderr")
        with open(state.state_path(self.plan), encoding="utf-8") as f:
            raw = f.read()
        self.assertNoSlice(raw, where="state file")
        for text in strings(json.loads(raw)):
            self.assertNoSlice(text, where="state value")

    def test_on_a_verdict_no_fragment_reaches_the_md_the_json_or_the_state(self):
        os.environ["FAKE_OPENCODE_MODE"] = "echo_token_ok"
        rc = self.round(bo, s=NASTY_ENDPOINT)
        self.assertEqual(rc, 0, self.err)
        self.assertNoSlice(self.out + self.err, where="stdout/stderr")
        md = os.path.join(self.rev, BASE + "-endpoint-round-1.md")
        for p in (md, md[:-3] + ".json", state.state_path(self.plan)):
            with open(p, encoding="utf-8") as f:
                self.assertNoSlice(f.read(), where=p)
        with open(md, encoding="utf-8") as f:
            # the stream's modelID (the token, in this fake) was there, masked -- and labelled as the STREAM's (M8)
            self.assertIn("model reported by the stream: ***", f.read())


class TestReviewPyEndToEnd(unittest.TestCase):
    def test_review_py_with_the_endpoint_backend_and_the_fake_opencode(self):
        log = os.path.join(tempfile.mkdtemp(), "calls.jsonl")
        env = fake_env(ADVERSARIAL_REVIEW_BACKEND="endpoint", ADVERSARIAL_REVIEW_MODEL="glm-5.3",
                       ADVERSARIAL_REVIEW_ENDPOINT_URL="https://gw.example",
                       ADVERSARIAL_REVIEW_ENDPOINT_TOKEN=TOKEN, FAKE_OPENCODE_LOG=log)
        wire(env)
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs", "superpowers", "plans", BASE + ".md"), PLAN)
            rc, out, err = run("review.py", plan, env=env)
            self.assertEqual(rc, 0, err)
            self.assertNotIn(TOKEN, out + err)
            self.assertEqual(json.loads(out)["route"], "endpoint gw.example")
            md = os.path.join(repo, "docs", "superpowers", "plans", "reviews", BASE + "-endpoint-round-1.md")
            with open(md, encoding="utf-8") as f:
                self.assertNotIn(TOKEN, f.read())
        with open(log) as f:
            self.assertEqual(json.loads(f.readline())["token_in_env"], TOKEN)


class TestFakeOpencode(unittest.TestCase):
    def test_the_fake_is_executable_and_prints_its_version(self):
        self.assertTrue(os.access(FAKE_OPENCODE, os.X_OK))
        p = subprocess.run([FAKE_OPENCODE, "--version"], capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "1.18.32")


# ---- zai_proxy -----------------------------------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    """A local stand-in for z.ai: records what reached it; /fail answers 401."""
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                              "body": body})
        code, data = (401, b'{"error":"bad key"}') if self.path.endswith("/fail") else (200, b'{"ok":true}')
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestZaiProxy(unittest.TestCase):
    """The proxy runs on a free loopback port against a local upstream (ZAI_UPSTREAM): no network."""

    def setUp(self):
        Upstream.seen = []
        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.port = free_port()
        env = isolated_env(ZAI_UPSTREAM=f"http://127.0.0.1:{self.upstream.server_address[1]}/api/v4")
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([sys.executable, os.path.join(BIN, "zai_proxy.py"), str(self.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.stderr)
        self.addCleanup(self.stderr.close)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)
        deadline = time.monotonic() + 10
        while not bo._listening("127.0.0.1", self.port):
            self.assertIsNone(self.proc.poll(), "the proxy died")
            self.assertLess(time.monotonic(), deadline, "the proxy never listened")
            time.sleep(0.1)

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST",
                                     headers={"Authorization": "Bearer k-123", "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10)

    def proxy_stderr(self):
        self.proc.kill()
        self.proc.wait()
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", "replace")

    def test_forwards_with_a_fresh_connection_and_the_authorization(self):
        r = self.post("/chat/completions", b'{"model": "glm-5.3"}')
        self.assertEqual((r.status, r.read()), (200, b'{"ok":true}'))
        [seen] = Upstream.seen
        self.assertEqual(seen["path"], "/api/v4/chat/completions")
        self.assertEqual(seen["headers"]["authorization"], "Bearer k-123")
        self.assertEqual(seen["headers"]["connection"].lower(), "close")
        self.assertEqual(seen["body"], b'{"model": "glm-5.3"}')

    def test_an_upstream_error_keeps_its_status_and_body(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/fail", b"{}")
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.read(), b'{"error":"bad key"}')

    def test_never_logs_the_body_or_the_authorization(self):
        self.post("/chat/completions", b'{"private_field_name": "private value"}').read()
        err = self.proxy_stderr()
        self.assertNotIn("private_field_name", err)
        self.assertNotIn("private value", err)
        self.assertNotIn("k-123", err)


if __name__ == "__main__":
    unittest.main()
