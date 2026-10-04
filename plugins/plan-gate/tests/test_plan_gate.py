"""The generic plan gate (`bin/plan_gate.py`), ported from the INTERNAL `tests/test_plan_gate.py` and
`tests/test_liberar.py`.

Source counts (loader, INTERNAL): test_plan_gate.py 14, test_liberar.py 41.
Removed by name:
  - test_plan_gate.py: test_checar_decide_igual_ao_legado, test_marcar_decide_igual_ao_legado
    (the golden in test_golden_gate.py replaces the comparison with the frozen copy);
  - test_liberar.py: the classes TestBloqueioDoRevisor, TestBloqueioComPlanoEditado,
    TestEscapeDeReviewNoLog, TestDegradacaoDeclarada, TestRotaDegradadaDeclarada,
    TestRotaDegradadaGuardaDeTipo (model reviewer and its routing -- stage 3).
Ported: 12 + 14. Then the tests of the deliberate differences and of the broken config (R21).

Ported name table (INTERNAL -> here):
  TestEstado -> TestState            test_plan_gate_dir_e_respeitado -> test_plan_gate_dir_is_respected
    test_sem_a_variavel_o_estado_nasce_no_default -> test_without_the_variable_the_state_is_born_in_the_default
  TestMensagens -> TestMessages
    test_sem_home_e_relativo_ao_home_de_quem_roda -> test_without_home_is_relative_to_the_runners_home
    test_nenhuma_saida_cita_<vendor>_nem_vaza_o_home -> test_no_output_leaks_the_home (the vendor-name
      assertions are not ported: naming a reviewer vendor is out of the public port)
    test_bloqueio_ensina_o_caminho_e_declara_o_limite -> test_denial_teaches_the_way_and_declares_the_limit
  TestBypassDeBash -> TestBashBypass
    test_leitura_continua_passando -> test_reading_still_passes
    test_nao_existe_comando_privilegiado -> test_there_is_no_privileged_command
    test_invocacao_do_portao_nao_libera_a_linha_inteira -> test_invoking_the_gate_does_not_release_the_whole_line
    test_git_de_leitura_so_como_comando_inteiro -> test_reading_git_only_as_a_whole_command
    test_mutacao_dentro_de_expansao_e_bloqueada -> test_mutation_inside_expansion_is_denied
    test_interpretador_em_qualquer_posicao_e_reconhecido -> test_interpreter_in_any_position_is_recognized
    test_mutacao_dentro_de_interpretador_nao_se_esconde_nas_aspas -> test_mutation_inside_interpreter_does_not_hide_in_quotes
  TestLiberar -> TestRelease
    test_pre_voo_limpo_libera -> test_clean_preflight_releases
    test_nunca_grava_aprovado -> test_never_writes_approved
    test_achado_recusa_e_estado_fica_byte_a_byte_igual -> test_finding_refuses_and_state_stays_byte_for_byte
    test_sem_motivo_recusa -> test_no_reason_refuses
    test_motivo_vazio_ou_so_espacos_recusa -> test_empty_or_blank_reason_refuses
    test_escape_exige_grupo_no_estado_certo -> test_escape_requires_group_in_the_right_state
    test_sem_cobertura_para_grupo_nao_avaliado -> test_no_coverage_for_a_not_evaluated_group
    test_log_guarda_motivo_e_triplas -> test_log_keeps_reason_and_escapes
    test_status_conta_liberacoes_do_repo_com_escape -> test_status_counts_repo_releases_with_escape
    test_corrida_aborta_mesmo_quando_so_o_log_de_execucao_muda -> test_race_aborts_even_when_only_the_execution_log_changes
    test_excecao_no_liberar_falha_fechado -> test_exception_in_release_fails_closed
  TestStatusNaoMorreNoMeio -> TestStatusDoesNotDieHalfway
    test_estado_sem_status_nao_derruba_a_listagem -> test_state_without_status_does_not_break_the_listing
    test_status_nao_string_nao_derruba_a_listagem -> test_non_string_status_does_not_break_the_listing
    test_arquivo_corrompido_vira_uma_linha_e_os_outros_aparecem -> test_corrupted_file_becomes_one_line_and_the_others_show
"""
import json, os, subprocess, sys, tempfile, unittest
from unittest import mock
from . import helpers
from .helpers import (BIN, SWITCHES, run, temp_state, fake_repo, fake_plan, hook_input, state_of, denied,
                      import_bin)

gate = import_bin("plan_gate")
FENCE = chr(96) * 3

# Plan that PASSES the whole preflight: the success path of `release` needs it (since the release
# runs the preflight, a plan with no commit and no verification is refused -- correctly).
CLEAN_TS_PLAN = f"""# Plan

### Task 1: do

**Files:**
- Create: `src/novo.ts`

- [ ] **Step 1: Write**

{FENCE}typescript
export function somar(a, b) {{
  return a + b;
}}
{FENCE}

- [ ] **Step 2: Verify**

Run: `npm test` — covers `src/novo.ts`.

- [ ] **Step 3: Commit**

{FENCE}bash
git add src/novo.ts && git commit -m "feat: somar"
{FENCE}
"""

PLAN_WITH_FINDING = CLEAN_TS_PLAN.replace("return a + b;", "return inexistenteNoRepoXyz(a, b);")


def write_plan(repo, text, name="2026-09-18-plan.md"):
    path = os.path.join(repo, "docs", "superpowers", "plans", name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def repo_in(base):
    """A git repo INSIDE `base` -- for the tests that need the repo under the test's HOME."""
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "docs", "superpowers", "plans"), exist_ok=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


def raw_run(args, env, stdin=None, cwd=None, timeout=None):
    """Runs the gate with EXACTLY `env` -- for the tests the helper's isolation would defeat (the default
    state dir, the PLAN_GATE switch, which `helpers.run` strips on purpose) and for the ones that need a
    `timeout` (a hang must fail the test, not stall the suite)."""
    p = subprocess.run([sys.executable, os.path.join(BIN, "plan_gate.py"), *args], input=stdin,
                       capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def bare_env(home):
    env = {k: v for k, v in os.environ.items() if k not in SWITCHES + ("PLAN_GATE_DIR",)}
    env["HOME"] = home
    return env


def mark(env, path, repo):
    return run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": path}, repo, "PostToolUse"))


def write_code(env, repo):
    rc, _, err = run("plan_gate.py", "check", env=env,
                     stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
    return rc, err


class TestState(unittest.TestCase):
    def test_plan_gate_dir_is_respected(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            rc, _, err = mark(env, plan, repo)
            self.assertEqual(rc, 2, err)
            self.assertEqual(state_of(plan, env)["status"], "pending")

    def test_without_the_variable_the_state_is_born_in_the_default(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            raw_run(["mark"], bare_env(env["HOME"]),
                    stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            gate = import_bin("plan_gate")
            ours = os.path.join(env["HOME"], ".claude", "claude-workflow", "plan-gate", gate.key(plan) + ".json")
            self.assertTrue(os.path.isfile(ours))
            # deliberate difference: never the INTERNAL's folder, which keeps writing there in stage 2
            self.assertFalse(os.path.exists(os.path.join(env["HOME"], ".claude", "plan-gate")))


class TestMessages(unittest.TestCase):
    def test_without_home_is_relative_to_the_runners_home(self):
        gate = import_bin("plan_gate")
        with mock.patch.dict(os.environ, {"HOME": "/home/someone"}):
            self.assertEqual(gate.without_home("/home/someone/projects/x/p.md"), "~/projects/x/p.md")
            self.assertEqual(gate.without_home("/home/someone"), "~")
            # echo of what the person typed: rewriting it would confuse instead of protecting
            self.assertEqual(gate.without_home("/home/other/x.md"), "/home/other/x.md")
            self.assertEqual(gate.without_home("/opt/x.md"), "/opt/x.md")

    def test_no_output_leaks_the_home(self):
        with temp_state() as env:
            repo = repo_in(env["HOME"])
            plan = write_plan(repo, CLEAN_TS_PLAN)
            m = mark(env, plan, repo)
            c = run("plan_gate.py", "check", env=env,
                    stdin=hook_input("Write", {"file_path": os.path.join(repo, "s.py")}, repo))
            r = run("plan_gate.py", "release", plan, "--reason", "decided in a meeting", env=env, cwd=repo)
            self.assertEqual(r[0], 0, r[1] + r[2])
            # the ERROR path is the one that prints most paths: a missing plan lists candidates
            e = run("plan_gate.py", "release", os.path.join(env["HOME"], "missing.md"), "--reason", "x",
                    env=env, cwd=repo)
            self.assertNotEqual(e[0], 0)
            for out in (m[2], c[2], r[1], r[2], e[1], e[2]):
                self.assertNotIn(env["HOME"], out)       # THIS one can fail: the HOME is in the real paths
                self.assertNotIn("~/.claude/bin", out)

    def test_without_home_covers_the_literal_and_the_resolved_home(self):
        gate = import_bin("plan_gate")
        with tempfile.TemporaryDirectory(prefix="plan-gate-sym-") as d:
            real = os.path.realpath(os.path.join(d, "real"))
            os.makedirs(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            with mock.patch.dict(os.environ, {"HOME": link}):
                self.assertEqual(gate.without_home(link + "/p/x.md"), "~/p/x.md")
                self.assertEqual(gate.without_home(real + "/p/x.md"), "~/p/x.md")
                self.assertEqual(gate.without_home(link), "~")
                self.assertEqual(gate.without_home(real), "~")
                # not a path prefix: a sibling that merely starts with the same characters
                self.assertEqual(gate.without_home(link + "x/p.md"), link + "x/p.md")
                self.assertEqual(gate.without_home(real + "x/p.md"), real + "x/p.md")

    def test_no_output_leaks_a_home_that_is_a_symlink(self):
        # macOS: HOME=/var/folders/.. while getcwd()/realpath say /private/var/folders/..
        with temp_state() as env, tempfile.TemporaryDirectory(prefix="plan-gate-sym-") as d:
            real = os.path.realpath(os.path.join(d, "real"))
            os.makedirs(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            env = dict(env, HOME=link)
            repo = repo_in(real)
            e = run("plan_gate.py", "release", os.path.join(link, "missing.md"), "--reason", "x",
                    env=env, cwd=repo)
            self.assertNotEqual(e[0], 0)
            for out in (e[1], e[2]):
                self.assertNotIn(link, out)
                self.assertNotIn(real, out)

    def test_denial_teaches_the_way_and_declares_the_limit(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            rc, err = write_code(env, repo)
            self.assertTrue(denied(rc))
            self.assertIn("preflight", err.lower())
            self.assertIn("release", err)
            self.assertIn("not an adversarial review", err.lower())


class TestBashBypass(unittest.TestCase):
    """The holes of releasing by substring: a gate that switches off when someone writes its name is
    not a gate."""
    def _check(self, command):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            rc, _, _ = run("plan_gate.py", "check", env=env, stdin=hook_input("Bash", {"command": command}, repo))
            return rc

    def test_reading_still_passes(self):
        for c in ("git status", "git log --oneline -5", "cat file.py",
                  "grep -rn foo src/ 2>/dev/null", "ls -la > /dev/null", "plan_gate.py status",
                  'plan_gate.py release p.md --reason "fixed > before release"'):
            self.assertEqual(self._check(c), 0, c)

    def test_there_is_no_privileged_command(self):
        # The redirection rules, even in a "reading" command and in the gate itself.
        for c in ("git status > x.py", "git log >> x.py", "plan_gate.py status > x.py",
                  "preflight_plan.py p.md > report.txt"):
            self.assertTrue(denied(self._check(c)), c)

    def test_invoking_the_gate_does_not_release_the_whole_line(self):
        self.assertTrue(denied(self._check("sed -i 's/a/b/' src/x.py && plan_gate.py status")))
        self.assertTrue(denied(self._check("plan_gate.py status; echo hi > src/x.py")))

    def test_reading_git_only_as_a_whole_command(self):
        self.assertTrue(denied(self._check("sed -i s/a/b/ x.py; git status")))
        self.assertTrue(denied(self._check("echo 'git status' > x.py")))

    def test_mutation_inside_expansion_is_denied(self):
        self.assertTrue(denied(self._check("echo $(sed -i s/a/b/ src/x.py)")))
        self.assertTrue(denied(self._check("echo `tee src/x.py`")))

    def test_interpreter_in_any_position_is_recognized(self):
        """The defect class is not `bash`, nor `^`: it is having a LIST of prefixes."""
        for c in ('MODE=x bash -c "sed -i s/a/b/ src/x.py"',
                  'command bash -c "echo hi > src/x.py"',
                  'env FOO=1 sh -c "tee src/x.py"',
                  'sudo bash -c "truncate -s0 src/x.py"',
                  'sudo -u root bash -c "echo hi > src/x.py"',
                  'timeout 5 bash -c "echo hi > src/x.py"',
                  'timeout --signal=TERM 5 bash -c "echo hi > src/x.py"',
                  'nohup sh -c "echo hi >> src/x.py"',
                  'nice -n 10 ionice -c2 bash -c "tee src/x.py"'):
            self.assertTrue(denied(self._check(c)), c)
        self.assertEqual(self._check('MODE=x bash -c "cat src/x.py"'), 0)
        self.assertEqual(self._check('sudo -u root bash -c "git status"'), 0)

    def test_mutation_inside_interpreter_does_not_hide_in_quotes(self):
        for c in ('bash -c "sed -i s/a/b/ src/x.py"',
                  "sh -c 'echo hi > src/x.py'",
                  'xargs -I{} sh -c "truncate -s0 {}"'):
            self.assertTrue(denied(self._check(c)), c)
        self.assertEqual(self._check('plan_gate.py release p.md --reason "before > after"'), 0)


class TestRelease(unittest.TestCase):
    def setUp(self):
        self._ctx = temp_state()
        self.env = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def prepare(self, text):
        ctx = fake_repo()
        repo = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        plan = write_plan(repo, text)
        run("plan_gate.py", "mark", env=self.env, cwd=repo,
            stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
        return repo, plan

    def release(self, plan, *args, repo, env=None):
        return run("plan_gate.py", "release", plan, *args, env=env or self.env, cwd=repo)

    def _bytes(self, plan):
        gate = import_bin("plan_gate")
        c = os.path.join(self.env["PLAN_GATE_DIR"], gate.key(plan) + ".json")
        if not os.path.isfile(c):
            return b""
        with open(c, "rb") as f:
            return f.read()

    def test_clean_preflight_releases(self):
        repo, plan = self.prepare(CLEAN_TS_PLAN)
        rc, out, err = self.release(plan, "--reason", "reviewed by four hands", repo=repo)
        self.assertEqual(rc, 0, out + err)
        e = state_of(plan, self.env)
        self.assertEqual(e["status"], "released")
        self.assertEqual(e["override_reason"], "reviewed by four hands")

    def test_never_writes_approved(self):
        # `approved` is reserved for the registrable checks; a human release is not one
        repo, plan = self.prepare(CLEAN_TS_PLAN)
        self.release(plan, "--reason", "decided", repo=repo)
        self.assertNotEqual(state_of(plan, self.env)["status"], "approved")

    def test_finding_refuses_and_state_stays_byte_for_byte(self):
        repo, plan = self.prepare(PLAN_WITH_FINDING)
        before = self._bytes(plan)
        rc, out, err = self.release(plan, "--reason", "I want to go on like this", repo=repo)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self._bytes(plan), before)

    def test_no_reason_refuses(self):
        repo, plan = self.prepare(CLEAN_TS_PLAN)
        self.assertNotEqual(self.release(plan, repo=repo)[0], 0)

    def test_empty_or_blank_reason_refuses(self):
        repo, plan = self.prepare(CLEAN_TS_PLAN)
        for m in ("", "   "):
            self.assertNotEqual(self.release(plan, "--reason", m, repo=repo)[0], 0, repr(m))

    def test_escape_requires_group_in_the_right_state(self):
        repo, plan = self.prepare(PLAN_WITH_FINDING)
        base = ("--reason", "decided in a meeting")
        # unknown group, refused with the list of valid ids
        rc, _, err = self.release(plan, *base, "--false-positive", "invented=just because", repo=repo)
        self.assertEqual(rc, 2)
        self.assertIn("invoked-symbols", err)
        # the INTERNAL short key is not the vocabulary (R16)
        self.assertEqual(self.release(plan, *base, "--false-positive", "simbolos=x", repo=repo)[0], 2)
        # wrong flag: the group is in findings, not in not_evaluated
        self.assertEqual(self.release(plan, *base, "--no-coverage", "invoked-symbols=did not run",
                                      repo=repo)[0], 2)
        # empty text
        self.assertEqual(self.release(plan, *base, "--false-positive", "invoked-symbols=   ", repo=repo)[0], 2)
        # right
        rc, out, err = self.release(plan, *base, "--false-positive",
                                    "invoked-symbols=comes from the framework, vendor/X.ts:40", repo=repo)
        self.assertEqual(rc, 0, out + err)
        e = state_of(plan, self.env)
        self.assertEqual(e["escapes"][0]["group"], "invoked-symbols")
        self.assertEqual(e["escapes"][0]["type"], "false-positive")
        self.assertTrue(e["escapes"][0]["when"])
        self.assertEqual(e["override_reason"], "decided in a meeting")

    def test_no_coverage_for_a_not_evaluated_group(self):
        # A language outside the table takes down TWO groups (symbols and constants). Escaping one
        # still refuses: each check that did not run is a separate admission.
        repo, plan = self.prepare(CLEAN_TS_PLAN.replace("typescript", "ruby"))
        partial = self.release(plan, "--reason", "plan in ruby",
                               "--no-coverage", "invoked-symbols=we have no ruby extractor yet", repo=repo)
        self.assertEqual(partial[0], 2, "escaping one group cannot release the others")
        rc, out, err = self.release(plan, "--reason", "plan in ruby",
                                    "--no-coverage", "invoked-symbols=no ruby extractor",
                                    "--no-coverage", "decorative-constants=same", repo=repo)
        self.assertEqual(rc, 0, out + err)
        # and the wrong verb for the same case refuses
        repo2, plan2 = self.prepare(CLEAN_TS_PLAN.replace("typescript", "ruby"))
        self.assertEqual(self.release(plan2, "--reason", "same", "--false-positive",
                                      "invoked-symbols=no extractor", repo=repo2)[0], 2)

    def test_log_keeps_reason_and_escapes(self):
        repo, plan = self.prepare(PLAN_WITH_FINDING)
        self.release(plan, "--reason", "decided in a meeting",
                     "--false-positive", "invoked-symbols=comes from the framework", repo=repo)
        with open(os.path.join(self.env["PLAN_GATE_DIR"], "releases.log"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        d = json.loads(lines[-1])
        self.assertEqual(d["reason"], "decided in a meeting")
        self.assertEqual(d["escapes"][0]["group"], "invoked-symbols")
        self.assertTrue(d["when"])

    def test_status_counts_repo_releases_with_escape(self):
        repo, plan = self.prepare(PLAN_WITH_FINDING)
        self.release(plan, "--reason", "decided",
                     "--false-positive", "invoked-symbols=comes from the framework", repo=repo)
        clean = write_plan(repo, CLEAN_TS_PLAN, name="2026-09-18-other.md")
        run("plan_gate.py", "mark", env=self.env, cwd=repo,
            stdin=hook_input("Write", {"file_path": clean}, repo, "PostToolUse"))
        self.release(clean, "--reason", "this one passed clean", repo=repo)
        _, out, _ = run("plan_gate.py", "status", plan, env=self.env, cwd=repo)
        self.assertIn("2 release", out)
        self.assertIn("1 with escape", out)

    def test_race_aborts_even_when_only_the_execution_log_changes(self):
        """The guard uses the RAW digest, not the normalized content_hash(): an edit inside the log
        section would otherwise pass and the release would hold for bytes nobody examined."""
        repo, plan = self.prepare(CLEAN_TS_PLAN + "\n## Execution log\n\n| a | b |\n")
        trigger = os.path.join(self.env["HOME"], "edit.py")
        with open(trigger, "w") as f:
            f.write(f"open({plan!r}, 'a').write('\\n| 09:00 | 09:05 |\\n')\n")
        rc, out, err = self.release(plan, "--reason", "fine", repo=repo,
                                    env=dict(self.env, PLAN_GATE_TEST_EDIT=trigger))
        self.assertEqual(rc, 2, out + err)
        self.assertNotEqual(state_of(plan, self.env)["status"], "released")

    def test_exception_in_release_fails_closed(self):
        """A manual command that blows up cannot return 0: for `release`, 0 reads as 'released'."""
        repo, plan = self.prepare(CLEAN_TS_PLAN)
        before = self._bytes(plan)
        rc, out, err = self.release(plan, "--reason", "fine", repo=repo,
                                    env=dict(self.env, PLAN_GATE_TEST_BREAK="1"))
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self._bytes(plan), before)


class TestStatusDoesNotDieHalfway(unittest.TestCase):
    """A state file the gate did not write (edited by hand, older version) cannot take the whole
    listing down: one bad file used to hide all the others."""
    def setUp(self):
        self._ctx = temp_state()
        self.env = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def _state(self, name, data):
        with open(os.path.join(self.env["PLAN_GATE_DIR"], name), "w") as f:
            json.dump(data, f)

    def _status(self):
        return run("plan_gate.py", "status", env=self.env, cwd=self.env["HOME"])

    def test_state_without_status_does_not_break_the_listing(self):
        self._state("aaa.json", {"plan": "/x/a.md"})
        self._state("bbb.json", {"plan": "/x/b.md", "status": "approved"})
        rc, out, err = self._status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("/x/a.md", out)
        self.assertIn("/x/b.md", out)      # the GOOD one stays visible

    def test_non_string_status_does_not_break_the_listing(self):
        self._state("aaa.json", {"plan": "/x/a.md", "status": 123})
        self._state("bbb.json", {"plan": "/x/b.md", "status": "approved"})
        rc, out, err = self._status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("/x/b.md", out)

    def test_corrupted_file_becomes_one_line_and_the_others_show(self):
        with open(os.path.join(self.env["PLAN_GATE_DIR"], "aaa.json"), "w") as f:
            f.write("{this is not json")
        self._state("bbb.json", {"plan": "/x/b.md", "status": "approved"})
        rc, out, err = self._status()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("UNREADABLE", out)
        self.assertIn("/x/b.md", out)


class TestDeliberateDifferences(unittest.TestCase):
    """What changes from the INTERNAL on purpose (spec §4.1), each with the test that fails without it."""

    def test_released_plan_edited_outside_the_editor_is_pending_again(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            run("plan_gate.py", "release", plan, "--reason", "ok", env=env)
            with open(plan, "a") as f:              # what a `sed -i` does: no Write/Edit hook fires
                f.write("\n### Task 2: sneaked in\n")
            rc, _, _ = run("plan_gate.py", "check", env=env,
                           stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))

    def test_custom_plans_dir_is_watched_and_the_default_is_not(self):
        with temp_state() as env, fake_repo() as repo:
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"plans_dir": "plans"}, f)
            os.makedirs(os.path.join(repo, "plans"))
            custom = os.path.join(repo, "plans", "p.md")
            with open(custom, "w") as f:
                f.write("# P\n")
            run("plan_gate.py", "mark", env=env, stdin=hook_input("Write", {"file_path": custom}, repo, "PostToolUse"))
            rc, _, _ = run("plan_gate.py", "check", env=env,
                           stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))      # the custom folder is a plan folder

    def test_released_plan_still_released_while_unchanged(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo, extra="\n## Execution log\n\n| when | task |\n")
            mark(env, plan, repo)
            rc, out, err = run("plan_gate.py", "release", plan, "--reason", "ok", env=env)
            self.assertEqual(rc, 0, out + err)
            self.assertFalse(denied(write_code(env, repo)[0]))
            with open(plan, "a") as f:              # filling the execution log stays out of the hash
                f.write("| 10:00 | Task 1 |\n")
            self.assertFalse(denied(write_code(env, repo)[0]))

    def test_default_plans_dir_is_not_a_plan_when_another_is_configured(self):
        with temp_state() as env, fake_repo() as repo:
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"plans_dir": "plans"}, f)
            plan = fake_plan(repo)                  # under docs/superpowers/plans
            rc, _, _ = mark(env, plan, repo)
            self.assertEqual(rc, 0)
            self.assertFalse(denied(write_code(env, repo)[0]))

    def test_gate_off_disables_the_denial_never_the_recording(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            off = dict(bare_env(env["HOME"]), PLAN_GATE_DIR=env["PLAN_GATE_DIR"], PLAN_GATE="off")
            raw_run(["mark"], off, stdin=hook_input("Write", {"file_path": plan}, repo, "PostToolUse"))
            self.assertEqual(state_of(plan, env)["status"], "pending")      # recorded with the switch on
            rc, _, _ = raw_run(["check"], off,
                               stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertEqual(rc, 0)
            # the INTERNAL's switch is not this plugin's switch
            old = dict(off, PLAN_GATE="", PORTAO_DE_PLANO="off")
            rc, _, _ = raw_run(["check"], old,
                               stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertTrue(denied(rc))

    def test_missing_plan_hint_searches_the_current_repo(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            rc, out, err = run("plan_gate.py", "release", os.path.basename(plan), "--reason", "x",
                               env=env, cwd=env["HOME"])
            self.assertNotEqual(rc, 0)
            self.assertNotIn(repo, err)              # HOME is not a repo: nothing to suggest
            rc, out, err = run("plan_gate.py", "release", os.path.basename(plan), "--reason", "x",
                               env=env, cwd=os.path.join(repo, "docs"))
            self.assertNotEqual(rc, 0)
            self.assertIn(plan, err)                 # relative to docs/, so missing; the repo has it

    def test_spec_escape_takes_the_spec_ids(self):
        with temp_state() as env, fake_repo() as repo:
            with open(os.path.join(repo, "servico.py"), "w") as f:
                f.write("def x():\n    return 1\n")
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("O `servico.py` ja impede que o valor passe do teto, entao nao precisamos mexer.")
            rc, _, err = mark(env, spec, repo)
            self.assertEqual(rc, 2, err)
            self.assertEqual(run("plan_gate.py", "release", spec, "--reason", "x", env=env)[0], 2)
            rc, _, err = run("plan_gate.py", "release", spec, "--reason", "x",
                             "--false-positive", "invoked-symbols=a plan id", env=env)
            self.assertEqual(rc, 2)
            self.assertIn("guarantees", err)         # refused with the list of the SPEC ids
            rc, out, err = run("plan_gate.py", "release", spec, "--reason", "x",
                               "--false-positive", "guarantees=checked by hand", env=env)
            self.assertEqual(rc, 0, out + err)
            st = state_of(spec, env)
            self.assertEqual(st["status"], "released")
            self.assertEqual(st["escapes"][0]["group"], "guarantees")


class TestBrokenConfig(unittest.TestCase):
    """R21: a broken `.claude/plan-gate.json` does not become silent defaults -- a gate that opens on
    a broken config is fail-open."""
    def setUp(self):
        self._s = temp_state()
        self.env = self._s.__enter__()
        self.addCleanup(self._s.__exit__, None, None, None)
        self._r = fake_repo()
        self.repo = self._r.__enter__()
        self.addCleanup(self._r.__exit__, None, None, None)
        os.makedirs(os.path.join(self.repo, ".claude"))
        self.config = os.path.join(self.repo, ".claude", "plan-gate.json")
        with open(self.config, "w") as f:
            f.write("{broken")

    def _check(self, tool, tool_input):
        rc, _, err = run("plan_gate.py", "check", env=self.env, stdin=hook_input(tool, tool_input, self.repo))
        return rc, err

    def test_check_denies_with_the_error_and_the_path(self):
        rc, err = self._check("Write", {"file_path": os.path.join(self.repo, "app.py")})
        self.assertTrue(denied(rc))
        self.assertIn(self.config, err)
        rc, _ = self._check("Bash", {"command": "echo hi > app.py"})
        self.assertTrue(denied(rc))

    def test_the_config_file_itself_can_be_fixed_from_the_session(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            self.assertEqual(self._check(tool, {"file_path": self.config})[0], 0, tool)
        # NotebookEdit names its file in `notebook_path`
        self.assertEqual(self._check("NotebookEdit", {"notebook_path": self.config})[0], 0)
        rc, err = self._check("Write", {"file_path": os.path.join(self.repo, "app.py")})
        self.assertTrue(denied(rc))
        self.assertIn(BROKEN, err)

    def test_mark_returns_2_with_the_message(self):
        plan = fake_plan(self.repo)
        rc, out, err = mark(self.env, plan, self.repo)
        self.assertEqual(rc, 2)
        self.assertIn(self.config, err)
        self.assertEqual(out, "")

    def test_a_comment_naming_the_config_does_not_open_bash(self):
        rc, err = self._check("Bash", {"command": "sed -i s/a/b/ app.py  # plan-gate.json"})
        self.assertTrue(denied(rc))
        self.assertIn(BROKEN, err)

    def test_bash_writing_elsewhere_too_is_denied(self):
        for command in ("echo x > app.py; printf '{}' > .claude/plan-gate.json",
                        "tee app.py .claude/plan-gate.json",
                        "sed -i s/a/b/ app.py .claude/plan-gate.json",
                        'bash -c "echo {} > .claude/plan-gate.json"',
                        # a `#` inside a word is literal for bash, a comment for shlex
                        "printf hacked > .claude/plan-gate.json x#y > app.py",
                        # read-write redirection
                        "printf hacked > .claude/plan-gate.json; echo x 1<>app.py"):
            rc, err = self._check("Bash", {"command": command})
            self.assertTrue(denied(rc), command)
            self.assertIn(BROKEN, err, command)

    def test_no_bash_passes_under_a_broken_config_not_even_onto_the_config(self):
        # R26: the only way out is Write/Edit of the config file itself
        for command in ("printf '{}' > .claude/plan-gate.json",
                        "git status",                  # not even what bash_is_safe calls harmless
                        "cat > .claude/plan-gate.json <<'EOF'\n{}\nEOF",
                        "sed -i 's/broken/{}/' .claude/plan-gate.json",
                        f"printf '{{}}' > {self.config}"):
            rc, err = self._check("Bash", {"command": command})
            self.assertTrue(denied(rc), command)
            self.assertIn(BROKEN, err, command)


BROKEN = "configuration is broken"


class TestConfigProblemsCloseTheGate(unittest.TestCase):
    """Fix round 1 (R21/R25): every config problem is a broken config -- `mark` exits 2 and `check`
    denies Write and Bash. None of them may fall into `main`'s fail-open rc 0."""
    def setUp(self):
        self._s = temp_state()
        self.env = self._s.__enter__()
        self.addCleanup(self._s.__exit__, None, None, None)
        self._r = fake_repo()
        self.repo = self._r.__enter__()
        self.addCleanup(self._r.__exit__, None, None, None)
        os.makedirs(os.path.join(self.repo, ".claude"))
        self.config = os.path.join(self.repo, ".claude", "plan-gate.json")
        self.plan = fake_plan(self.repo)

    def _write_config(self, data):
        with open(self.config, "w") as f:
            json.dump(data, f)

    def assertClosed(self, label):
        rc, out, err = mark(self.env, self.plan, self.repo)
        self.assertEqual(rc, 2, label)
        self.assertIn(BROKEN, err, label)
        self.assertIn(self.config, err, label)
        rc, err = write_code(self.env, self.repo)
        self.assertTrue(denied(rc), label)
        self.assertIn(BROKEN, err, label)
        rc, _, err = run("plan_gate.py", "check", env=self.env,
                         stdin=hook_input("Bash", {"command": "echo hi > app.py"}, self.repo))
        self.assertTrue(denied(rc), label)
        self.assertIn(BROKEN, err, label)

    def test_wrong_typed_folder(self):
        for value in (5, None, ["x"]):
            with self.subTest(value=value):
                self._write_config({"plans_dir": value})
                self.assertClosed(repr(value))

    def test_empty_folder(self):
        self._write_config({"plans_dir": ""})
        self.assertClosed("empty")

    def test_folders_that_never_match_or_match_everything(self):
        for value in ("/", ".", "/abs/plans", "../plans"):
            with self.subTest(value=value):
                self._write_config({"specs_dir": value})
                self.assertClosed(value)

    def test_folder_turning_into_a_list_with_a_plan_pending(self):
        rc, _, err = mark(self.env, self.plan, self.repo)
        self.assertEqual(rc, 2, err)
        self._write_config({"plans_dir": ["x"]})
        rc, err = write_code(self.env, self.repo)
        self.assertTrue(denied(rc))
        self.assertIn(BROKEN, err)

    def test_a_directory_in_place_of_the_config(self):
        os.makedirs(self.config)
        self.assertClosed("directory")

    def test_a_dangling_link_in_place_of_the_config(self):
        os.symlink(os.path.join(self.repo, "gone.json"), self.config)
        self.assertClosed("dangling link")

    def test_a_link_to_a_valid_config_is_still_broken(self):
        real = os.path.join(self.repo, "real.json")
        with open(real, "w") as f:
            json.dump({}, f)
        os.symlink(real, self.config)
        self.assertClosed("link to valid JSON")

    def test_linking_the_config_to_a_source_file_does_not_open_that_file(self):
        # R30, the reviewer's sequence: valid config, plan pending
        app = os.path.join(self.repo, "app.py")
        with open(app, "w") as f:
            f.write("x = 1\n")
        mark(self.env, self.plan, self.repo)
        rc, err = write_code(self.env, self.repo)
        self.assertTrue(denied(rc))
        self.assertIn("BLOCKED BY THE PLAN GATE", err)
        # `ln` is not a mutation for bash_is_safe (inherited), so the link itself gets through
        os.symlink("../app.py", self.config)
        for tool in ("Write", "Edit"):
            rc, _, err = run("plan_gate.py", "check", env=self.env,
                             stdin=hook_input(tool, {"file_path": app}, self.repo))
            self.assertTrue(denied(rc), tool)
            self.assertIn(BROKEN, err, tool)
        # nor the config path itself, while it is a link
        rc, _, err = run("plan_gate.py", "check", env=self.env,
                         stdin=hook_input("Write", {"file_path": self.config}, self.repo))
        self.assertTrue(denied(rc))
        self.assertIn(BROKEN, err)

    def test_an_alias_link_to_the_config_does_not_open_it(self):
        with open(self.config, "w") as f:
            f.write("{broken")
        alias = os.path.join(self.repo, "cfglink.json")
        os.symlink(self.config, alias)
        rc, _, err = run("plan_gate.py", "check", env=self.env,
                         stdin=hook_input("Write", {"file_path": alias}, self.repo))
        self.assertTrue(denied(rc))
        self.assertIn(BROKEN, err)
        # the real path is still the way out
        rc, _, _ = run("plan_gate.py", "check", env=self.env,
                       stdin=hook_input("Write", {"file_path": self.config}, self.repo))
        self.assertEqual(rc, 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_an_unreadable_config(self):
        self._write_config({})
        os.chmod(self.config, 0)
        self.addCleanup(os.chmod, self.config, 0o644)
        self.assertClosed("chmod 000")

    def assertClosedWithin(self, label, seconds=10):
        """Like assertClosed, but a hang fails the test (TimeoutExpired) instead of stalling the suite."""
        rc, _, err = raw_run(["mark"], self.env, timeout=seconds,
                             stdin=hook_input("Write", {"file_path": self.plan}, self.repo, "PostToolUse"))
        self.assertEqual(rc, 2, label)
        self.assertIn(BROKEN, err, label)
        for tool, ti in (("Write", {"file_path": os.path.join(self.repo, "app.py")}),
                         ("Bash", {"command": "echo hi > app.py"})):
            rc, _, err = raw_run(["check"], self.env, timeout=seconds, stdin=hook_input(tool, ti, self.repo))
            self.assertTrue(denied(rc), f"{label} {tool}")
            self.assertIn(BROKEN, err, f"{label} {tool}")

    def test_a_fifo_config_does_not_hang_the_hook(self):
        os.mkfifo(self.config)
        self.assertClosedWithin("fifo")

    def test_a_config_linked_to_dev_zero_does_not_hang_the_hook(self):
        os.symlink("/dev/zero", self.config)
        self.assertClosedWithin("/dev/zero")

    def test_an_oversized_config(self):
        with open(self.config, "w") as f:
            f.write('{"mr_template": "' + "a" * (2 << 20) + '"}')
        self.assertClosedWithin("oversized")

    def test_folders_with_surrounding_spaces_or_a_tilde(self):
        for value in (" docs/superpowers/plans", "docs/superpowers/plans ", "~/plans", "~"):
            with self.subTest(value=value):
                self._write_config({"plans_dir": value})
                self.assertClosed(repr(value))

    def test_mkdir_over_the_config_does_not_switch_the_gate_off(self):
        rc, _, err = mark(self.env, self.plan, self.repo)
        self.assertEqual(rc, 2, err)
        for command in ("mkdir -p .claude/plan-gate.json", "chmod 000 .claude/plan-gate.json"):
            with self.subTest(command=command):
                if os.path.isdir(self.config):
                    os.rmdir(self.config)
                with open(self.config, "w") as f:
                    f.write("{}")
                os.chmod(self.config, 0o644)
                if command.startswith("mkdir"):
                    os.remove(self.config)
                    os.makedirs(self.config)
                else:
                    if hasattr(os, "geteuid") and os.geteuid() == 0:
                        continue
                    os.chmod(self.config, 0)
                rc, err = write_code(self.env, self.repo)
                self.assertTrue(denied(rc), command)
                self.assertIn(BROKEN, err, command)
        os.chmod(self.config, 0o644)


class TestMovedTargetsAndNewFolders(unittest.TestCase):
    def test_a_moved_or_deleted_released_spec_does_not_lock_the_repo(self):
        for how in ("move", "delete"):
            with self.subTest(how=how), temp_state() as env, fake_repo() as repo:
                specs = os.path.join(repo, "docs", "superpowers", "specs")
                spec = os.path.join(specs, "s.md")
                with open(spec, "w") as f:
                    f.write("# Spec\n\nVamos construir um servico novo.\n")
                mark(env, spec, repo)
                rc, out, err = run("plan_gate.py", "release", spec, "--reason", "ok", env=env)
                self.assertEqual(rc, 0, out + err)
                if how == "move":
                    os.makedirs(os.path.join(specs, "adiados"))
                    os.rename(spec, os.path.join(specs, "adiados", "s.md"))
                else:
                    os.remove(spec)
                self.assertFalse(denied(write_code(env, repo)[0]), how)
                plan = os.path.join(repo, "docs", "superpowers", "plans", "p.md")
                rc, _, err = run("plan_gate.py", "check", env=env,
                                 stdin=hook_input("Write", {"file_path": plan}, repo))
                self.assertEqual(rc, 0, how + ": " + err)

    def test_write_into_a_new_folder_is_denied_while_a_plan_is_pending(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            rc, _, err = run("plan_gate.py", "check", env=env,
                             stdin=hook_input("Write", {"file_path": os.path.join(repo, "src", "new", "x.py")}, repo))
            self.assertTrue(denied(rc))
            self.assertIn("BLOCKED BY THE PLAN GATE", err)

    def test_dot_dot_through_a_missing_folder_does_not_leave_the_repo(self):
        # R28: `..` is normalized before walking up, or the walk climbs out through `/nonexistent`
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            for rel in (("src", "new", "x.py"), ("app.py",)):
                target = "/nonexistent/.." + os.path.join(repo, *rel)
                rc, _, err = run("plan_gate.py", "check", env=env,
                                 stdin=hook_input("Write", {"file_path": target}, repo))
                self.assertTrue(denied(rc), target)
                self.assertIn("BLOCKED BY THE PLAN GATE", err, target)


class TestCorruptStateFailsClosed(unittest.TestCase):
    """A-I1: a corrupt or unreadable state file must never open the gate, and editing the plan rewrites it."""

    def _corrupt(self, path, env, payload=b"{not json"):
        gate = import_bin("plan_gate")
        st = os.path.join(env["PLAN_GATE_DIR"], gate.key(path) + ".json")
        with open(st, "wb") as f:
            f.write(payload)
        return st

    def test_write_state_does_not_share_a_fixed_tmp_name(self):
        gate = import_bin("plan_gate")
        with temp_state() as env, fake_repo() as repo, mock.patch.dict(os.environ, env):
            plan = fake_plan(repo)
            # a concurrent writer's fixed-name temp file in the way must not break (or be overwritten by) this one
            os.makedirs(gate.state_path(plan) + ".tmp")
            gate.write_state(plan, status="pending")
            self.assertEqual(state_of(plan, env)["status"], "pending")
            left = [n for n in os.listdir(env["PLAN_GATE_DIR"]) if n.endswith(".tmp") and
                    not os.path.isdir(os.path.join(env["PLAN_GATE_DIR"], n))]
            self.assertEqual(left, [])

    def test_a_corrupt_state_of_a_released_plan_denies(self):
        for payload in (b"{not json", b"[]", b'"x"', b'{"status": "released"}'):
            with self.subTest(payload=payload), temp_state() as env, fake_repo() as repo:
                plan = fake_plan(repo)
                mark(env, plan, repo)
                rc, out, err = run("plan_gate.py", "release", plan, "--reason", "ok", env=env)
                self.assertEqual(rc, 0, out + err)
                self.assertFalse(denied(write_code(env, repo)[0]))
                st = self._corrupt(plan, env, payload)
                rc, err = write_code(env, repo)
                self.assertTrue(denied(rc), err)
                self.assertIn(os.path.basename(plan), err)
                self.assertIn(os.path.basename(st), err)

    def test_a_corrupt_state_of_an_unknown_target_denies_naming_the_file(self):
        with temp_state() as env, fake_repo() as repo:
            st = os.path.join(env["PLAN_GATE_DIR"], "0123456789abcdef.json")
            with open(st, "w") as f:
                f.write("{half a write")
            rc, err = write_code(env, repo)
            self.assertTrue(denied(rc), err)
            self.assertIn("0123456789abcdef.json", err)

    def test_a_corrupt_spec_state_denies_writing_the_plan(self):
        with temp_state() as env, fake_repo() as repo:
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("# Spec\n\nVamos construir um servico novo.\n")
            mark(env, spec, repo)
            rc, out, err = run("plan_gate.py", "release", spec, "--reason", "ok", env=env)
            self.assertEqual(rc, 0, out + err)
            self._corrupt(spec, env)
            plan = os.path.join(repo, "docs", "superpowers", "plans", "p.md")
            rc, _, err = run("plan_gate.py", "check", env=env, stdin=hook_input("Write", {"file_path": plan}, repo))
            self.assertTrue(denied(rc), err)
            self.assertIn("s.md", err)

    def test_editing_the_plan_rewrites_a_corrupt_state_as_pending(self):
        for payload in (b"{not json", b"[]"):
            with self.subTest(payload=payload), temp_state() as env, fake_repo() as repo:
                plan = fake_plan(repo)
                self._corrupt(plan, env, payload)
                rc, _, err = mark(env, plan, repo)
                self.assertEqual(rc, 2, err)          # pending reminder, not the fail-open handler
                self.assertEqual(state_of(plan, env)["status"], "pending")


class TestStateDirIsOffLimits(unittest.TestCase):
    """A-I4 (R82): a file tool aimed (after realpath) under the state folder is denied, whatever the repo."""

    def _check(self, env, tool, tool_input, cwd):
        return run("plan_gate.py", "check", env=env, stdin=hook_input(tool, tool_input, cwd))

    def test_forging_the_state_or_a_check_manifest_is_denied(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            gate = import_bin("plan_gate")
            state = os.path.join(env["PLAN_GATE_DIR"], gate.key(plan) + ".json")
            manifest = os.path.join(env["PLAN_GATE_DIR"], "checks.d", "preflight.json")
            for tool, ti in (("Write", {"file_path": state, "content": "{}"}),
                             ("Edit", {"file_path": state, "old_string": "pending", "new_string": "approved"}),
                             ("MultiEdit", {"file_path": manifest, "edits": []}),
                             ("Write", {"file_path": manifest, "content": "{}"}),
                             ("NotebookEdit", {"notebook_path": os.path.join(env["PLAN_GATE_DIR"], "x.ipynb")})):
                for cwd in (repo, env["HOME"]):
                    with self.subTest(tool=tool, target=ti, cwd=cwd):
                        rc, _, err = self._check(env, tool, ti, cwd)
                        self.assertTrue(denied(rc), err)

    def test_denied_even_with_no_plan_pending_and_through_a_link(self):
        with temp_state() as env, fake_repo() as repo:
            link = os.path.join(repo, "innocent.json")
            os.symlink(os.path.join(env["PLAN_GATE_DIR"], "0123456789abcdef.json"), link)
            for target in (os.path.join(env["PLAN_GATE_DIR"], "0123456789abcdef.json"), link):
                with self.subTest(target=target):
                    rc, _, err = self._check(env, "Write", {"file_path": target, "content": "{}"}, repo)
                    self.assertTrue(denied(rc), err)
            rc, _, err = self._check(env, "Write", {"file_path": os.path.join(repo, "app.py")}, repo)
            self.assertEqual(rc, 0, err)          # nothing pending: ordinary code still passes

    def test_denied_even_with_the_gate_switched_off(self):
        # a state forged while PLAN_GATE=off would hold once the gate is switched back on
        with temp_state() as env, fake_repo() as repo:
            off = dict(bare_env(env["HOME"]), PLAN_GATE_DIR=env["PLAN_GATE_DIR"], PLAN_GATE="off")
            target = os.path.join(env["PLAN_GATE_DIR"], "0123456789abcdef.json")
            rc, _, err = raw_run(["check"], off, stdin=hook_input("Write", {"file_path": target}, repo))
            self.assertTrue(denied(rc), err)
            rc, _, err = raw_run(["check"], off,
                                 stdin=hook_input("Write", {"file_path": os.path.join(repo, "app.py")}, repo))
            self.assertEqual(rc, 0, err)


class TestReviewStateIsOffLimitsWhilePending(unittest.TestCase):
    """S2 (R16): while a plan is pending, a file tool cannot forge adversarial-review's verdict (or anything else under
    ~/.claude/claude-workflow/, PLAN_GATE_DIR, ADVERSARIAL_REVIEW_DIR) -- that would be self-approval."""

    FORGED = {"file_path": "", "content": '{"reviews": {"x": {"verdict": "APPROVED", "blockers": 0}}}'}

    def _write(self, env, target, cwd):
        return run("plan_gate.py", "check", env=env, stdin=hook_input("Write", dict(self.FORGED, file_path=target), cwd))

    def test_forging_the_review_state_is_denied_while_a_plan_is_pending(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            self.assertEqual(mark(env, plan, repo)[0], 2)          # pending
            default = os.path.join(env["HOME"], ".claude", "claude-workflow", "adversarial-review", "x.json")
            elsewhere = os.path.join(tempfile.mkdtemp(prefix="ar-state-"), "x.json")
            for target, extra in ((default, {}), (elsewhere, {"ADVERSARIAL_REVIEW_DIR": os.path.dirname(elsewhere)})):
                for cwd in (repo, env["HOME"]):
                    with self.subTest(target=target, cwd=cwd):
                        rc, _, err = self._write(dict(env, **extra), target, cwd)
                        self.assertTrue(denied(rc), err)
                        self.assertIn("self-approval", err)
            for tool, ti in (("Edit", {"file_path": default, "old_string": "a", "new_string": "b"}),
                             ("MultiEdit", {"file_path": default, "edits": []}),
                             ("NotebookEdit", {"notebook_path": default[:-5] + ".ipynb"})):
                with self.subTest(tool=tool):
                    rc, _, err = run("plan_gate.py", "check", env=env, stdin=hook_input(tool, ti, env["HOME"]))
                    self.assertTrue(denied(rc), err)

    def test_a_shell_redirect_into_the_review_state_is_denied_while_pending(self):
        with temp_state() as env, fake_repo() as repo:
            mark(env, fake_plan(repo), repo)
            # `~` / `$HOME`, as an agent writes it (the test HOME lives under /tmp, which the gate treats as scratch)
            for target in ("~/.claude/claude-workflow/adversarial-review/x.json",
                           '"$HOME/.claude/claude-workflow/adversarial-review/x.json"'):
                with self.subTest(target=target):
                    rc, _, err = run("plan_gate.py", "check", env=env,
                                     stdin=hook_input("Bash", {"command": f"echo '{{}}' > {target}"}, repo))
                    self.assertTrue(denied(rc), err)

    def test_a_quoted_redirect_target_is_still_a_write(self):
        gate = import_bin("plan_gate")
        for command in ('echo x > "app.py"', "echo x >> 'app.py'", 'echo x >"$HOME/.claude/claude-workflow/x"'):
            with self.subTest(command=command):
                self.assertFalse(gate.bash_is_safe(command))
        for command in ('git commit -m "a > b"', 'grep -n "x" app.py 2>/dev/null', "jq '.a' f.json"):
            with self.subTest(command=command):
                self.assertTrue(gate.bash_is_safe(command))

    def test_allowed_when_nothing_is_pending(self):
        with temp_state() as env, fake_repo() as repo:
            target = os.path.join(env["HOME"], ".claude", "claude-workflow", "adversarial-review", "x.json")
            rc, _, err = self._write(env, target, env["HOME"])
            self.assertEqual(rc, 0, err)
            elsewhere = os.path.join(tempfile.mkdtemp(prefix="ar-state-"), "x.json")
            rc, _, err = self._write(dict(env, ADVERSARIAL_REVIEW_DIR=os.path.dirname(elsewhere)), elsewhere, repo)
            self.assertEqual(rc, 0, err)


class TestOverrideTrailNeverFollowsALink(unittest.TestCase):
    """S1: the release's `-override.md` append never writes through a committed symlink."""

    def test_a_linked_override_file_is_refused_and_the_plan_stays_unreleased(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            target = os.path.join(env["HOME"], ".bashrc")
            with open(target, "w") as f:
                f.write("# the user's own file\n")
            rev = os.path.join(os.path.dirname(plan), "reviews")
            os.makedirs(rev)
            os.symlink(target, os.path.join(rev, os.path.basename(plan)[:-3] + "-override.md"))
            rc, out, err = run("plan_gate.py", "release", plan, "--reason", "decided", env=env, cwd=repo)
            self.assertEqual(rc, 2, out + err)
            with open(target) as f:
                self.assertEqual(f.read(), "# the user's own file\n")
            self.assertNotEqual(state_of(plan, env).get("status"), "released")


class TestNotebookAndAnchoredExemption(unittest.TestCase):
    def test_notebook_path_is_the_target_of_notebook_edit(self):
        # A-M2: NotebookEdit sends `notebook_path`; with cwd outside the repo, `file_path` found no repo
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            rc, _, err = run("plan_gate.py", "check", env=env, stdin=hook_input(
                "NotebookEdit", {"notebook_path": os.path.join(repo, "nb.ipynb"), "new_source": "x"}, env["HOME"]))
            self.assertTrue(denied(rc), err)

    def test_docs_superpowers_exemption_is_anchored_at_the_repo_root(self):
        # A-M3: `src/docs/superpowers/x.py` is code, not the planning folder
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            mark(env, plan, repo)
            nested = os.path.join(repo, "src", "docs", "superpowers", "x.py")
            rc, _, err = run("plan_gate.py", "check", env=env,
                             stdin=hook_input("Write", {"file_path": nested}, repo))
            self.assertTrue(denied(rc), err)
            review = os.path.join(repo, "docs", "superpowers", "plans", "reviews", "r.md")
            rc, _, err = run("plan_gate.py", "check", env=env,
                             stdin=hook_input("Write", {"file_path": review}, repo))
            self.assertEqual(rc, 0, err)


class TestMessagesNameARunnableCommand(unittest.TestCase):
    """A-I2 (R83): the gate's messages cite the command with the absolute plugin path (the bare name is not on
    PATH, and CLAUDE_PLUGIN_ROOT is not set in the Bash tool), and never steer the model to release itself."""

    GATE = 'python3 "' + os.path.join(os.path.dirname(BIN), "bin", "plan_gate.py") + '"'
    BARE = r"(?<![/\w])(plan_gate|preflight_spec|preflight_plan)\.py"

    def _messages(self, lang):
        env_extra = {"PLAN_GATE_LANG": lang}
        with temp_state() as env, fake_repo() as repo:
            env = dict(env, **env_extra)
            out = {}
            spec = os.path.join(repo, "docs", "superpowers", "specs", "s.md")
            with open(spec, "w") as f:
                f.write("O `servico.py` ja impede que o valor passe do teto.\n")
            out["mark_spec"] = mark(env, spec, repo)[2]
            plan = os.path.join(repo, "docs", "superpowers", "plans", "p.md")
            out["deny_spec"] = run("plan_gate.py", "check", env=env,
                                   stdin=hook_input("Write", {"file_path": plan}, repo))[2]
            os.remove(spec)
            out["mark_plan"] = mark(env, fake_plan(repo), repo)[2]
            out["deny_plan"] = write_code(env, repo)[1]
            return out

    def test_every_gate_message_cites_the_absolute_command(self):
        import re
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(BIN), "bin", "plan_gate.py")))
        for lang in ("en", "pt-BR"):
            for name, err in self._messages(lang).items():
                with self.subTest(lang=lang, message=name):
                    self.assertIn(self.GATE + " run-checks", err)
                    self.assertIn(self.GATE + " release", err)
                    self.assertIsNone(re.search(self.BARE, err), err)

    @staticmethod
    def _command_lines(err):
        import shlex
        out = {}
        for line in err.splitlines():
            line = line.strip()
            if line.startswith("python3 "):
                words = shlex.split(line)
                out[words[2] if words[1].endswith("plan_gate.py") else os.path.basename(words[1])] = words
        return out

    def test_mark_messages_quote_every_path(self):                # R90, now R17: shlex, round-trip exact
        for lang in ("en", "pt-BR"):
            for name in ("mark_plan", "mark_spec"):
                with self.subTest(lang=lang, message=name):
                    lines = self._command_lines(self._messages(lang)[name])
                    self.assertTrue(lines["run-checks"][3].endswith(".md"), lines)
                    self.assertEqual(lines["release"][4], "--reason")
                    if name == "mark_spec":
                        self.assertEqual(lines["preflight_spec.py"][2], lines["run-checks"][3])

    def test_a_path_under_the_home_is_tilde_outside_the_quotes(self):
        with temp_state() as env:
            repo = repo_in(env["HOME"])
            plan = os.path.join(repo, "docs", "superpowers", "plans", "p $(touch pwned) 'q'.md")
            with open(plan, "w") as f:
                f.write(helpers.CLEAN_PLAN)
            err = mark(env, plan, repo)[2]
            self.assertNotIn(env["HOME"], err)
            line = [l.strip() for l in err.splitlines() if " run-checks " in l][0]
            out = subprocess.run(["bash", "-c", "set -- " + line.split(" run-checks ", 1)[1] + "; printf '%s' \"$1\""],
                                 capture_output=True, text=True, cwd=env["HOME"],
                                 env=dict(os.environ, HOME=env["HOME"])).stdout
            self.assertEqual(out, plan)
            self.assertFalse(os.path.exists(os.path.join(env["HOME"], "pwned")))

    def test_a_hostile_file_name_stays_one_inert_word(self):  # final review I2 (R17)
        for lang in ("en", "pt-BR"):
            with self.subTest(lang=lang), temp_state() as env, fake_repo() as repo:
                env = dict(env, PLAN_GATE_LANG=lang)
                canary = os.path.join(env["HOME"], "pwned")
                plan = os.path.join(repo, "docs", "superpowers", "plans", "p $(touch pwned) `touch pwned` 'q'.md")
                with open(plan, "w") as f:
                    f.write(helpers.CLEAN_PLAN)
                spec = os.path.join(repo, "docs", "superpowers", "specs", "s $(echo x).md")
                with open(spec, "w") as f:
                    f.write("O `servico.py` ja impede que o valor passe do teto.\n")
                for path, err in ((plan, mark(env, plan, repo)[2]), (spec, mark(env, spec, repo)[2])):
                    lines = self._command_lines(err)
                    self.assertEqual(lines["run-checks"][3], path, err)
                    self.assertEqual(lines["release"][3], path, err)
                    if path == spec:
                        self.assertEqual(lines["preflight_spec.py"][2], path, err)
                    # and bash really reads it as one inert word: nothing expands, nothing runs
                    run_checks = [l.strip() for l in err.splitlines() if " run-checks " in l][0]
                    out = subprocess.run(["bash", "-c", "set -- " + run_checks.split(" run-checks ", 1)[1]
                                          + "; printf '%s' \"$1\""], capture_output=True, text=True,
                                         cwd=env["HOME"]).stdout
                    self.assertEqual(out, path)
                    self.assertFalse(os.path.exists(canary))

    def test_deny_plan_option_one_is_fix_the_plan_not_release(self):
        err = self._messages("en")["deny_plan"]
        one = err.split("1.", 1)[1].split("2.", 1)[0]
        self.assertIn("IN THE PLAN", one)
        self.assertIn("run-checks", one)
        self.assertNotIn(" release ", one)
        self.assertIn("human decision", err)


class TestSpecReleaseParity(unittest.TestCase):
    """A-I3 (R84): the spec release refuses --no-coverage, takes --false-positive only for a blocking group in
    `findings`, and refuses when the spec changed while it was being checked -- parity with the plan."""

    SPEC = ("O `servico.py` ja impede que o valor passe do teto, entao nao precisamos mexer.\n\n"
            "O servico calcula o frete e devolve o valor final ao cliente.\n")   # guarantees + quantities

    def setUp(self):
        self._s = temp_state()
        self.env = dict(self._s.__enter__(), PLAN_GATE_LANG="en")
        self.addCleanup(self._s.__exit__, None, None, None)
        self._r = fake_repo()
        self.repo = self._r.__enter__()
        self.addCleanup(self._r.__exit__, None, None, None)
        with open(os.path.join(self.repo, "servico.py"), "w") as f:
            f.write("def x():\n    return 1\n")

    def spec(self, text):
        path = os.path.join(self.repo, "docs", "superpowers", "specs", "s.md")
        with open(path, "w") as f:
            f.write(text)
        mark(self.env, path, self.repo)
        return path

    def release(self, spec, *args, env=None):
        return run("plan_gate.py", "release", spec, "--reason", "decided", *args, env=env or self.env)

    def assertNotReleased(self, spec, rc, out, err):
        self.assertEqual(rc, 2, out + err)
        self.assertNotEqual(state_of(spec, self.env).get("status"), "released")

    def test_no_coverage_is_refused_for_a_spec(self):
        spec = self.spec(self.SPEC)
        rc, out, err = self.release(spec, "--false-positive", "guarantees=checked by hand",
                                    "--no-coverage", "guarantees=could not run")
        self.assertNotReleased(spec, rc, out, err)
        self.assertIn("--no-coverage", err)
        self.assertIn("spec", err)
        rc, out, err = self.release(spec, "--false-positive", "guarantees=checked by hand")
        self.assertEqual(rc, 0, out + err)            # the same release without it goes through

    def test_false_positive_needs_a_blocking_group_in_findings(self):
        spec = self.spec(self.SPEC)
        for extra in ("citations=ok group", "quantities=a non-blocking group with findings"):
            with self.subTest(extra=extra):
                rc, out, err = self.release(spec, "--false-positive", "guarantees=checked by hand",
                                            "--false-positive", extra)
                self.assertNotReleased(spec, rc, out, err)
                self.assertIn(extra.split("=")[0], err)

    def test_false_positive_on_a_clean_spec_group_does_not_talk_about_no_coverage(self):   # R88
        spec = self.spec("# Spec\n\nPlain intent.\n")
        rc, out, err = self.release(spec, "--false-positive", "guarantees=x")
        self.assertNotReleased(spec, rc, out, err)
        self.assertNotIn("--no-coverage", err)
        self.assertIn("spec", err.lower())

    def test_a_spec_changed_during_the_check_is_not_released(self):
        spec = self.spec("# Spec\n\nVamos construir um servico novo.\n")
        trigger = os.path.join(self.env["HOME"], "edit.py")
        with open(trigger, "w") as f:
            f.write(f"open({spec!r}, 'a').write('\\nO servico.py ja garante tudo.\\n')\n")
        rc, out, err = self.release(spec, env=dict(self.env, PLAN_GATE_TEST_EDIT=trigger))
        self.assertNotReleased(spec, rc, out, err)
        self.assertIn("spec changed", err)
        self.assertNotIn("plan changed", err)



class TestHashConfigHelp(unittest.TestCase):
    def test_hash_prints_the_content_hash(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo)
            rc, out, err = run("plan_gate.py", "hash", plan, env=env)
            self.assertEqual((rc, out.strip()), (0, gate.content_hash(plan)), err)

    def test_hash_ignores_the_execution_log(self):
        with temp_state() as env, fake_repo() as repo:
            plan = fake_plan(repo, extra="\n## Execution log\n\n- 09:00 opened\n")
            _, before, _ = run("plan_gate.py", "hash", plan, env=env)
            with open(plan, "a") as f:
                f.write("- 10:00 started\n")
            _, after, _ = run("plan_gate.py", "hash", plan, env=env)
            self.assertEqual(before, after)

    def test_hash_of_a_missing_file_is_rc_2(self):
        with temp_state() as env:
            rc, out, _ = run("plan_gate.py", "hash", "/nonexistent/plan.md", env=env)
            self.assertEqual((rc, out), (2, ""))

    def test_config_prints_the_resolved_config(self):
        with temp_state() as env, fake_repo() as repo:
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                json.dump({"language": "pt-BR", "plans_dir": "plans"}, f)
            rc, out, err = run("plan_gate.py", "config", repo, env=env)
            self.assertEqual(rc, 0, err)
            data = json.loads(out)
            self.assertEqual((data["repo"], data["language"], data["plans_dir"], data["specs_dir"]),
                             (repo, "pt-BR", "plans", "docs/superpowers/specs"))

    def test_config_of_a_broken_config_is_rc_2(self):
        with temp_state() as env, fake_repo() as repo:
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                f.write("{")
            rc, out, _ = run("plan_gate.py", "config", repo, env=env)
            self.assertEqual((rc, out), (2, ""))

    def test_help_lists_every_manual_subcommand(self):
        for flag in ("-h", "--help"):
            rc, out, _ = run("plan_gate.py", flag)
            self.assertEqual(rc, 0)
            for sub in ("status", "release", "run-checks", "hash", "config", "checks list|prune"):
                self.assertIn(sub, out)

PASS = '{"status":"pass","findings":[]}'
FAIL = '{"status":"fail","findings":[{"group":"x","state":"findings","count":1,"items":["y"]}]}'


class TestDespiteCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def other_check(self, env, output, applies_to=("plan",)):
        script = os.path.join(self.tmp.name, "other.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nprintf '%s' '" + output + "'\n")
        os.chmod(script, 0o755)
        manifest = os.path.join(self.tmp.name, "other.json")
        with open(manifest, "w") as f:
            json.dump({"id": "other", "command": script, "required": True, "applies_to": list(applies_to)}, f)
        rc, _, err = run("plan_gate.py", "register-check", manifest, env=env)
        self.assertEqual(rc, 0, err)

    def test_a_failing_other_check_blocks_a_release_with_only_a_reason(self):
        with temp_state() as env, fake_repo() as repo:
            plan = write_plan(repo, CLEAN_TS_PLAN)
            self.other_check(env, FAIL)
            rc, _, err = run("plan_gate.py", "release", plan, "--reason", "ship it", env=env, cwd=repo)
            self.assertEqual(rc, 2)
            self.assertIn("other", err)
            self.assertIn("--despite-check", err)
            try:        # a refused release of a never-marked plan leaves no state file at all
                status = state_of(plan, env).get("status")
            except FileNotFoundError:
                status = None
            self.assertNotEqual(status, "released")

    def test_despite_check_releases_and_leaves_a_trail(self):
        with temp_state() as env, fake_repo() as repo:
            plan = write_plan(repo, CLEAN_TS_PLAN)
            self.other_check(env, FAIL)
            rc, out, err = run("plan_gate.py", "release", plan, "--reason", "ship it",
                               "--despite-check", "other=the reviewer misread the schema", env=env, cwd=repo)
            self.assertEqual(rc, 0, err)
            self.assertIn("other (despite-check)", out)
            escape = [e for e in state_of(plan, env)["escapes"] if e["type"] == "despite-check"]
            self.assertEqual([(e["group"], e["text"]) for e in escape],
                             [("other", "the reviewer misread the schema")])
            with open(os.path.join(env["PLAN_GATE_DIR"], "releases.log")) as f:
                self.assertIn("despite-check", f.read())
            override = os.path.join(os.path.dirname(plan), "reviews", os.path.basename(plan)[:-3] + "-override.md")
            with open(override) as f:
                self.assertIn("the reviewer misread the schema", f.read())

    def test_despite_check_on_a_passing_check_is_refused(self):
        with temp_state() as env, fake_repo() as repo:
            plan = write_plan(repo, CLEAN_TS_PLAN)
            self.other_check(env, PASS)
            rc, _, err = run("plan_gate.py", "release", plan, "--reason", "r", "--despite-check", "other=x",
                             env=env, cwd=repo)
            self.assertEqual(rc, 2, err)

    def test_despite_check_on_an_unknown_check_is_refused(self):
        with temp_state() as env, fake_repo() as repo:
            plan = write_plan(repo, CLEAN_TS_PLAN)
            rc, _, err = run("plan_gate.py", "release", plan, "--reason", "r", "--despite-check", "nope=x",
                             env=env, cwd=repo)
            self.assertEqual(rc, 2)
            self.assertIn("nope", err)

    def test_a_spec_release_also_needs_despite_check(self):
        with temp_state() as env, fake_repo() as repo:
            spec = os.path.join(repo, "docs", "superpowers", "specs", "2026-01-01-x-design.md")
            with open(spec, "w") as f:
                f.write("# Spec\n\nPlain intent, no claims about the repo.\n")
            self.other_check(env, FAIL, applies_to=("spec",))
            rc, _, err = run("plan_gate.py", "release", spec, "--reason", "r", env=env, cwd=repo)
            self.assertEqual(rc, 2)
            self.assertIn("other", err)
            self.assertIn("--despite-check", err)
            rc, _, err = run("plan_gate.py", "release", spec, "--reason", "r", "--despite-check", "other=y",
                             env=env, cwd=repo)
            self.assertEqual(rc, 0, err)

    def test_a_duplicate_despite_check_is_refused(self):
        with temp_state() as env, fake_repo() as repo:
            plan = write_plan(repo, CLEAN_TS_PLAN)
            self.other_check(env, FAIL)
            rc, _, err = run("plan_gate.py", "release", plan, "--reason", "r", "--despite-check", "other=a",
                             "--despite-check", "other=b", env=env, cwd=repo)
            self.assertEqual(rc, 2, err)
            self.assertIn("other", err)

    def test_despite_check_on_a_passing_check_is_refused_for_a_spec(self):
        with temp_state() as env, fake_repo() as repo:
            spec = os.path.join(repo, "docs", "superpowers", "specs", "2026-01-01-x-design.md")
            with open(spec, "w") as f:
                f.write("# Spec\n\nPlain intent, no claims about the repo.\n")
            self.other_check(env, PASS, applies_to=("spec",))
            rc, _, err = run("plan_gate.py", "release", spec, "--reason", "r", "--despite-check", "other=x",
                             env=env, cwd=repo)
            self.assertEqual(rc, 2, err)

    def test_a_broken_own_registration_must_be_despited(self):
        # R32: a corrupted checks.d/preflight*.json is a required, failing entry -- not "the preflight, skip it"
        for kind, own in (("plan", "preflight"), ("spec", "preflight-spec")):
            with self.subTest(kind=kind), temp_state() as env, fake_repo() as repo:
                if kind == "plan":
                    path = write_plan(repo, CLEAN_TS_PLAN)
                else:
                    path = os.path.join(repo, "docs", "superpowers", "specs", "2026-01-01-x-design.md")
                    with open(path, "w") as f:
                        f.write("# Spec\n\nPlain intent, no claims about the repo.\n")
                folder = os.path.join(env["PLAN_GATE_DIR"], "checks.d")
                os.makedirs(folder)
                with open(os.path.join(folder, own + ".json"), "w") as f:
                    f.write("{not json")
                rc, _, err = run("plan_gate.py", "release", path, "--reason", "r", env=env, cwd=repo)
                self.assertEqual(rc, 2, err)
                self.assertIn(own, err)
                self.assertIn("--despite-check", err)
                rc, _, err = run("plan_gate.py", "release", path, "--reason", "r",
                                 "--despite-check", own + "=known corrupt", env=env, cwd=repo)
                self.assertEqual(rc, 0, err)

    def test_a_required_but_unregistered_own_check_does_not_block(self):
        # R35: the repo requires `preflight`, nobody registered it -- release runs the preflight itself
        for kind, own in (("plan", "preflight"), ("spec", "preflight-spec")):
            with self.subTest(kind=kind), temp_state() as env, fake_repo() as repo:
                if kind == "plan":
                    path = write_plan(repo, CLEAN_TS_PLAN)
                else:
                    path = os.path.join(repo, "docs", "superpowers", "specs", "2026-01-01-x-design.md")
                    with open(path, "w") as f:
                        f.write("# Spec\n\nPlain intent, no claims about the repo.\n")
                os.makedirs(os.path.join(repo, ".claude"))
                with open(os.path.join(repo, ".claude", "plan-gate.json"), "w") as f:
                    json.dump({"checks": {own: {"required": True}}}, f)
                rc, _, err = run("plan_gate.py", "release", path, "--reason", "r", env=env, cwd=repo)
                self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
