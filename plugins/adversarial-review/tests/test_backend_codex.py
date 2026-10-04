"""The `codex` backend (adversarial-review spec §5.1).

Ported from the INTERNAL test_backend_codex.py (7 of 8; the dropped one is test_nao_consome_o_eixo_rota -- there is
no route axis here). Origin names are in each docstring.
"""
import json, os, stat, tempfile, time, unittest
from unittest import mock
from .helpers import FAKES, FakeBackend, contract_of, fake_repo, import_bin, isolated_env, run, write
from .test_round import BASE, PLAN, wire

loop = import_bin("loop")
settings = import_bin("settings")
backend_codex = import_bin("backend_codex")


def fake_codex(body):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "codex")
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


class TestBackendCodex(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "r.json")

    def _run(self, body, limit=30):
        path = fake_codex(body)
        with mock.patch.object(backend_codex, "find_codex", lambda: path):
            return backend_codex.review(path="plan.md", repo=self.d, prompt="prompt", output_json=self.out,
                                        settings=settings.Settings("codex"), options=loop.Options(timeout_s=limit),
                                        deadline=time.monotonic() + limit)

    def test_does_not_repair(self):
        """INTERNAL test_nao_repara: `codex exec` cannot resume a session."""
        self.assertFalse(backend_codex.CAN_REPAIR)
        self.assertFalse(hasattr(backend_codex, "repair"))

    def test_does_not_consume_the_model_axis(self):
        """INTERNAL test_nao_consome_o_eixo_modelo: a True here would bring back a false model label."""
        self.assertFalse(backend_codex.CAN_MODEL)

    def test_success(self):
        """INTERNAL test_sucesso."""
        r = self._run("echo ok; exit 0\n")
        self.assertTrue(r.ok, r.reason)

    def test_success_telemetry_is_only_wall_seconds(self):
        r = self._run("exit 0\n")
        self.assertEqual(list(r.telemetry), ["wall_s"])

    def test_nonzero_exit_becomes_a_readable_reason(self):
        """INTERNAL test_exit_nao_zero_vira_motivo_legivel."""
        r = self._run('echo "exploded" >&2; exit 4\n')
        self.assertFalse(r.ok)
        self.assertIn("4", r.reason)
        self.assertIn("exploded", r.reason)

    def test_timeout_respects_the_loops_deadline(self):
        """INTERNAL test_timeout_respeita_o_deadline_do_laco: the limit is the LOOP's, not the attempt's."""
        t0 = time.monotonic()
        r = self._run("sleep 30\n", limit=2)
        self.assertFalse(r.ok)
        self.assertIn("time limit", r.reason)
        self.assertLess(time.monotonic() - t0, 10)

    def test_a_missing_binary_does_not_blow_up(self):
        """INTERNAL test_binario_inexistente_nao_estoura."""
        with mock.patch.object(backend_codex, "find_codex", lambda: "/does/not/exist/codex"):
            r = backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out,
                                     settings=settings.Settings("codex"), options=loop.Options(),
                                     deadline=time.monotonic() + 5)
        self.assertFalse(r.ok)
        self.assertIn("could not run", r.reason)

    def test_find_codex_prefers_the_PATH(self):
        """INTERNAL test_achar_codex_prefere_o_PATH."""
        d = tempfile.mkdtemp()
        target = write(os.path.join(d, "codex"), "#!/bin/sh\n")
        os.chmod(target, 0o755)
        with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(backend_codex.find_codex(), target)

    def test_find_codex_falls_back_to_the_highest_nvm_install_then_to_the_bare_name(self):
        home = tempfile.mkdtemp()
        for v in ("v9.11.2", "v18.0.0", "v20.1.0", "junk"):
            p = write(os.path.join(home, ".nvm", "versions", "node", v, "bin", "codex"), "x")
            os.chmod(p, 0o755)
        empty = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"PATH": empty, "HOME": home}):
            self.assertEqual(backend_codex.find_codex(), os.path.join(home, ".nvm/versions/node/v20.1.0/bin/codex"))
        with mock.patch.dict(os.environ, {"PATH": empty, "HOME": tempfile.mkdtemp()}):
            self.assertEqual(backend_codex.find_codex(), "codex")

    def test_nvm_versions_compare_numerically_not_lexicographically(self):
        home = tempfile.mkdtemp()
        for v in ("v9.11.2", "v18.0.0", "garbage"):
            os.chmod(write(os.path.join(home, ".nvm", "versions", "node", v, "bin", "codex"), "x"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": tempfile.mkdtemp(), "HOME": home}):
            self.assertEqual(backend_codex.find_codex(), os.path.join(home, ".nvm/versions/node/v18.0.0/bin/codex"))

    def test_non_utf8_stderr_becomes_a_reason_not_an_exception(self):
        env = dict(os.environ, FAKE_CODEX_MODE="badutf8")
        with mock.patch.dict(os.environ, env), mock.patch.object(backend_codex, "find_codex",
                                                                 lambda: os.path.join(FAKES, "codex")):
            r = backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out,
                                     settings=settings.Settings("codex"), options=loop.Options(),
                                     deadline=time.monotonic() + 5)
        self.assertFalse(r.ok)
        self.assertIn("exited 1", r.reason)

    def test_an_expired_deadline_still_gives_a_timeout_of_at_least_one_second(self):
        with mock.patch.object(backend_codex.subprocess, "run") as run_:
            run_.return_value = mock.Mock(returncode=0, stderr="")
            backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out,
                                 settings=settings.Settings("codex"), options=loop.Options(),
                                 deadline=time.monotonic() - 100)
        self.assertGreaterEqual(run_.call_args.kwargs["timeout"], 1)

    def test_never_reads_the_model(self):
        class Boom:
            @property
            def model(self):
                raise AssertionError("settings.model was read")
        path = fake_codex("exit 0\n")
        with mock.patch.object(backend_codex, "find_codex", lambda: path):
            r = backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out, settings=Boom(),
                                     options=loop.Options(), deadline=time.monotonic() + 5)
        self.assertTrue(r.ok, r.reason)

    def test_the_endpoint_token_never_reaches_the_codex_child(self):
        log = os.path.join(self.d, "env.log")
        token_file = write(os.path.join(self.d, "token"), "tok-FILE\n")
        env = {"FAKE_CODEX_LOG": log, "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN": "tok-SECRET-123",
               "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE": token_file}
        with mock.patch.dict(os.environ, env), mock.patch.object(backend_codex, "find_codex",
                                                                 lambda: os.path.join(FAKES, "codex")):
            r = backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out,
                                     settings=settings.Settings("codex"), options=loop.Options(),
                                     deadline=time.monotonic() + 10)
        self.assertTrue(r.ok, r.reason)
        with open(log) as f:
            seen = json.loads(f.readline())["env"]
        self.assertIn("FAKE_CODEX_LOG", seen)          # the env really is the parent's, minus the credential
        self.assertNotIn("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN", seen)
        self.assertNotIn("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE", seen)

    def test_the_token_is_masked_in_the_stderr_excerpt_before_the_cut(self):
        token = "tok-SECRET-123"
        for stderr in (f"auth failed for {token}", "x" * 190 + token, json.dumps({"k": f'a"{token}'})):
            with self.subTest(stderr=stderr[-40:]):
                env = {"FAKE_CODEX_MODE": "stderr", "FAKE_CODEX_STDERR": stderr,
                       "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN": token}
                with mock.patch.dict(os.environ, env), mock.patch.object(backend_codex, "find_codex",
                                                                         lambda: os.path.join(FAKES, "codex")):
                    r = backend_codex.review(path="p", repo=self.d, prompt="p", output_json=self.out,
                                             settings=settings.Settings("codex"), options=loop.Options(),
                                             deadline=time.monotonic() + 10)
                self.assertFalse(r.ok)
                self.assertNotIn(token, r.reason)
                self.assertNotIn(token[:8], r.reason)
                self.assertIn("exited 1", r.reason)

    def test_meets_the_backend_contract(self):
        self.assertIsNone(loop.backend_problem(backend_codex))

    def test_the_round_tests_fake_is_shaped_like_this_backend(self):
        self.assertEqual(contract_of(FakeBackend.like(backend_codex, [])), contract_of(backend_codex))

    def test_route_names_the_user_account(self):
        self.assertEqual(backend_codex.route(settings.Settings("codex")), "codex (user account)")

    def test_the_fake_codex_is_executable_and_prints_its_version(self):
        p = os.path.join(FAKES, "codex")
        self.assertTrue(os.access(p, os.X_OK))
        import subprocess
        self.assertEqual(subprocess.run([p, "--version"], capture_output=True, text=True).stdout.strip(),
                         "codex-cli 0.0.0-fake")

    def test_review_py_end_to_end_with_the_fake_codex(self):
        log = os.path.join(tempfile.mkdtemp(), "argv.log")
        env = isolated_env(ADVERSARIAL_REVIEW_BACKEND="codex", FAKE_CODEX_LOG=log)
        env["PATH"] = FAKES + os.pathsep + env["PATH"]
        wire(env)
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs", "superpowers", "plans", BASE + ".md"), PLAN)
            rc, out, err = run("review.py", plan, env=env)
            self.assertEqual(rc, 0, err)
            md = os.path.join(repo, "docs", "superpowers", "plans", "reviews", BASE + "-codex-round-1.md")
            self.assertTrue(os.path.exists(md), os.listdir(os.path.dirname(md)))
            with open(md, encoding="utf-8") as f:
                self.assertNotIn(env["HOME"], f.read())
        with open(log) as f:
            call = json.loads(f.readline())
        argv = call["argv"]
        self.assertEqual(call["stdin"], "")
        self.assertEqual(len(argv), 10)   # exec -C repo -s read-only --output-schema S -o OUT PROMPT
        self.assertIn("MANDATORY mechanical checks", argv[-1])   # the whole prompt is the single last item
        self.assertEqual(argv[0], "exec")
        i = argv.index("-s")
        self.assertEqual(argv[i + 1], "read-only")
        self.assertEqual(argv[argv.index("--output-schema") + 1], loop.SCHEMA)
        self.assertEqual(argv[argv.index("-C") + 1], repo)


if __name__ == "__main__":
    unittest.main()
