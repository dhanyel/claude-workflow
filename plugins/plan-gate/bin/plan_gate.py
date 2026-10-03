#!/usr/bin/env python3
"""Gate: an implementation plan nobody released does not become code.

Why a hook and not a skill: a skill is invoked by a model, that is, it can be forgotten -- and
"having the rule and not applying it" is the diagnosed failure, not the lack of the rule. A hook is
deterministic.

Subcommands (all read the hook JSON on stdin, except the manual ones):
  mark     PostToolUse: plan written/edited -> pending state + reminder
  check    PreToolUse:  if there is a pending plan in this repo, DENIES implementation
  status   [plan]       shows the state
  release  <plan> --reason R    human override, with a trail
  run-checks <path>     runs the registered checks; approves when every required one passes
  register-check <manifest>     registers a plugin's plan-gate-check.json
  register-checks       SessionStart: registers this plugin's own plan-gate-check.json (never stdout)
  checks list|prune     lists the registered checks / removes the orphans
"""
# Name table, pt (INTERNAL plan-gate.py) -> en. Literal one-to-one translation; the only changes beyond
# the translation are the deliberate differences listed at the end, and what is "out of the port".
#
#   constants / patterns
#     ESTADO_DIR -> state_dir() (config.py)  LOG -> log_path()
#     DIR_DE_PLANOS / DIR_DE_SPECS -> config.load(repo)["plans_dir" | "specs_dir"]
#     FERRAMENTAS_DE_IMPLEMENTACAO -> IMPLEMENTATION_TOOLS   MUTACAO_EM_BASH -> BASH_MUTATION
#     REDIRECT_INOCENTE -> INNOCENT_REDIRECT  RE_EXPANSAO -> RE_EXPANSION   RE_ASPAS -> RE_QUOTES
#     INTERPRETADORES -> INTERPRETERS         RE_LOG_DE_EXECUCAO -> RE_EXECUTION_LOG
#     CHAVES_DE_GRUPO -> GROUP_IDS (English ids from preflight_plan.GROUPS) + INTERNAL_KEY (id -> key)
#     TITULO_DO_GRUPO -> (not ported: unused in the INTERNAL; titles come from i18n "preflight.group.<id>")
#     SUBCOMANDOS_DE_HOOK -> HOOK_SUBCOMMANDS EscapeInvalido -> InvalidEscape
#     env PORTAO_DE_PLANO -> PLAN_GATE        PLAN_GATE_TESTE_EDITAR / _QUEBRAR -> PLAN_GATE_TEST_EDIT / _BREAK
#     files erros.log / liberacoes.log -> errors.log / releases.log
#   functions
#     sem_home -> without_home               _comandos -> _commands        _tem_interpretador -> _has_interpreter
#     bash_e_seguro -> bash_is_safe          chave -> key                  caminho_do_estado -> state_path
#     gravar_estado -> write_state           portao_desligado -> gate_off  sha -> content_hash
#     eh_plano -> is_plan                    eh_spec -> is_spec            specs_pendentes_do_repo -> pending_specs_in_repo
#     repo_de -> repo_of                     pendentes_do_repo -> pending_in_repo
#     cmd_marcar -> cmd_mark                 _marcar_spec -> _mark_spec    cmd_checar -> cmd_check
#     cmd_status -> cmd_status               _digest_bruto -> _raw_digest  _par -> _pair
#     registrar_no_log -> record_in_log      liberacoes_do_repo -> releases_of_repo
#     _liberar_spec -> _release_spec         cmd_liberar -> cmd_release    main -> main
#   NEW (not in the INTERNAL): _inside (configurable folders), _broken_config (R21/R26: broken config denies),
#     _unknown_group (R16: the escape id is validated against the kind being released);
#     Task 8 (spec §4.2): run_checks, _timeline_event, _checks_hint, _failed_checks, cmd_run_checks,
#     cmd_register_check(s), cmd_checks -- approval by registrable checks (bin/checks.py).
#   subcommands marcar checar status liberar -> mark check status release
#   flags --motivo --pre-voo-falso --sem-cobertura -> --reason --false-positive --no-coverage
#   states pendente aprovado liberado -> pending approved released
#   state fields plano hash_do_plano motivo escritor atualizado spec_do_portao escapes liberado_em
#                motivo_do_override -> plan plan_hash reason writer updated gate_spec escapes released_at
#                override_reason
#   escape fields grupo tipo texto quando -> group type text when;
#     types pre-voo-falso sem-cobertura -> false-positive no-coverage
#   log fields quando plano motivo escapes escritor -> when plan reason escapes writer
#
# Deliberate differences from the INTERNAL (spec §4.1), and only these:
#   - state_dir(): PLAN_GATE_DIR or ~/.claude/claude-workflow/plan-gate/ (never the INTERNAL's ~/.claude/plan-gate);
#   - opt-out PLAN_GATE=off;
#   - the log section out of the hash is `## Log de execução` OR `## Execution log`;
#   - `released` is tied to the hash: a released file whose content changed is pending again
#     (in the INTERNAL `liberado` is permanent, plan-gate.py:325,355);
#   - is_plan/is_spec read the folders from config.load(repo), keeping the exclusions;
#   - the "did you mean" hint of `release` searches the current repo (the INTERNAL globbed a fixed folder under the home);
#   - escapes take the English group ids (R16), validated against the kind being released (plan or spec);
#   - a broken `.claude/plan-gate.json` denies `check` and makes `mark` exit 2 (R21) -- any config problem:
#     bad JSON, a symbolic link (R30), not a regular file or too big (R27), wrong-typed values, unusable
#     folders (R25, R29); the only tool call let through is a Write/Edit/MultiEdit/NotebookEdit whose
#     abspath is the config path itself (R26, R30);
#   - repo_of normalizes the path and walks up to the nearest existing folder (R24, R28: a Write into a
#     new folder, or through `/missing/..`, found no repo).
#
# Out of the port (stage 3 / the INTERNAL's old install): every model-reviewer field and message (and its routing)
# (rodadas, bloqueios_abertos, apesar_da_review, ultima_falha, rota, revisor, --apesar-da-review,
# eh_rota_degradada, the "so pre-voo" / "rota direta" marks), and migrar / hook_legado /
# origens_de_settings / ARQUIVOS_DE_SETTINGS / _aponta_para_outro_portao / AVISO_DE_MIGRACAO.
import argparse, datetime, glob, hashlib, json, os, re, shlex, subprocess, sys, tempfile

# ⚠️ Bootstrap of the sibling modules: the test helper `import_bin` does not touch sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import checks        # noqa: E402
import config        # noqa: E402
import i18n          # noqa: E402
from config import state_dir   # noqa: E402

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def command(script):
    """R83: how a message names a script -- `python3 "<absolute plugin dir>/bin/<script>"`. The bare name is not
    on PATH, and CLAUDE_PLUGIN_ROOT is not set in the Bash tool's environment. Never without_home(): `~`
    does not expand inside the quotes."""
    return f'python3 "{os.path.join(PLUGIN_DIR, "bin", script)}"'


# ⚠️ PLAN_GATE_DIR exists so the TESTS do not dirty the real state of whoever runs the suite, and so the
# state lives OUTSIDE the plugin: it has to survive uninstalling and reinstalling, otherwise an update
# turns a released plan back to pending. Read on every call (state_dir() is never cached).
def log_path():
    return os.path.join(state_dir(), "errors.log")


def without_home(path):
    """/home/someone/projects/x -> ~/projects/x. Useful for who reads, anonymous for who receives.

    ⚠️ Declared limit: replaces ONLY the home of whoever is running. A path under somebody else's home
    stays intact -- it is not ours to rewrite.
    """
    # Both spellings: HOME as set AND as resolved (macOS: /var/folders/.. comes back as /private/var/..
    # from getcwd()/realpath). Longest first, each one only at a separator boundary.
    home = os.path.expanduser("~")
    for h in sorted({home, os.path.realpath(home)}, key=len, reverse=True):
        if path == h:
            return "~"
        if path.startswith(h + os.sep):
            return "~" + path[len(h):]
    return path


# Tools that mean "I started implementing".
IMPLEMENTATION_TOOLS = {"Task", "Agent", "Write", "Edit", "NotebookEdit", "MultiEdit"}

# Bash also writes files (`sed -i`, heredoc, `>`). Covering only Write/Edit would leave the gate
# decorative exactly on the path it exists to close.
BASH_MUTATION = re.compile(
    r"(^|[;&|]|\s)(sed\s+-[a-zA-Z]*i|tee\b|patch\b|dd\b|truncate\b|install\b)|>>?\s*\S|<<\s*['\"]?\w+",
)
# ⚠️ `2>/dev/null`, `2>&1` and writing to /tmp show up in almost every READING command. Treating that as
# a mutation filled the gate with false positives -- and a gate that cries wolf is a gate its owner
# turns off. It vanishes from the text before the check.
INNOCENT_REDIRECT = re.compile(r">>?\s*(/dev/null|/tmp/\S*|&\d)")
# ⚠️ An earlier version released the WHOLE LINE when the gate's name or a reading `git` showed up
# ANYWHERE in it (`re.search` on an alternation). That is: `sed -i ... && plan_gate.py status` passed.
# The release is now per simple command.
RE_EXPANSION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
# Text between quotes is an ARGUMENT, not syntax: `--reason "fixed > before"` does not redirect.
RE_QUOTES = re.compile(r"'[^']*'|\"[^\"]*\"")
# ⚠️ ... EXCEPT for whoever interprets the argument as a PROGRAM. For `bash -c`, `sh -c`, `eval` and
# `xargs`, what is between quotes is code, and erasing the quotes before looking for a mutation would
# hide `bash -c "sed -i s/a/b/ x.py"` entirely.
INTERPRETERS = re.compile(r"^(?:\S*/)?(?:bash|sh|zsh|dash|eval|xargs|env)\b")


def _commands(text):
    """Splits the line into simple commands (`;`, `&&`, `||`, `|`, newline)."""
    return [p.strip() for p in re.split(r"\|\||&&|[;&|\n]", text) if p.strip()]


def _has_interpreter(part):
    """Is some TOKEN of the simple command an interpreter?

    ⚠️ The question is not "does the command START with bash?", nor is there a list of wrappers.
    Anchoring on `^` let `MODE=x bash -c` through; listing wrappers let `sudo -u root bash -c` through
    (the option has an argument) and `timeout --signal=TERM 5 bash -c`. Listing prefixes is a game the
    one getting around always wins: it is enough to find the prefix missing from the list. The right
    question has no list.

    The price is being conservative: `echo bash -c "a > b"` also falls here and is denied. In a gate,
    erring on the side of denying is the right side to err on.
    """
    try:
        tokens = shlex.split(part)
    except ValueError:
        tokens = part.split()
    return any(INTERPRETERS.match(tok) for tok in tokens)


def bash_is_safe(command, depth=0):
    """True when NO simple command of the line writes a file.

    ⚠️ There is NO privileged command. An earlier version had a list of released prefixes
    (`git status`, `plan_gate.py ...`) and checked the prefix BEFORE the mutation: `git status > x.py`
    and `plan_gate.py status > x.py` wrote a file and passed. The redirection rules, always; what
    vanishes before the check are the QUOTES (argument) and the innocent redirections (`2>/dev/null`,
    `/tmp/...`), not a command name.

    Three rules, each from a real bypass:
    1. the text of the `$(...)` and backtick expansions is evaluated APART -- an expansion is
       executable content and inherits nothing from what contains it;
    2. each simple command answers for itself -- `echo 'git status' > x` does not become a git read
       just because the words appear in it;
    3. whoever INTERPRETS the argument (`bash -c`, `sh -c`, `eval`, `xargs`) has the quoted content
       analyzed from inside: there quotes delimit a program, not text -- and that holds with an
       assignment or a wrapper in front (`MODE=x bash -c`, `sudo sh -c`).
    """
    if depth > 3:
        return False          # absurd nesting: refuse instead of giving up looking
    clean = INNOCENT_REDIRECT.sub("", command)
    parts = []
    for a, b in RE_EXPANSION.findall(clean):
        parts.extend(_commands(a or b))
    parts.extend(_commands(RE_EXPANSION.sub(" ", clean)))
    for p in parts:
        if _has_interpreter(p):
            # what is between quotes here is a PROGRAM: analyze it from inside, one level
            for m in RE_QUOTES.finditer(p):
                if not bash_is_safe(m.group(0)[1:-1], depth + 1):
                    return False
            if BASH_MUTATION.search(RE_QUOTES.sub(" ", p)):
                return False   # and the rest of the command counts too
            continue
        if BASH_MUTATION.search(RE_QUOTES.sub(" ", p)):
            return False
    return True


def key(plan):
    return hashlib.sha256(os.path.abspath(plan).encode()).hexdigest()[:16]


def state_path(plan):
    return os.path.join(state_dir(), key(plan) + ".json")


def load_state(path):
    """-> (state, None) or (None, error). A state is a JSON object whose `plan` is a string.

    ⚠️ Anything else -- unreadable, half-written, a list, an object without `plan` -- is CORRUPT, and the
    callers fail closed on it (A-I1): a reader that skipped it let a released plan's corruption open the gate.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            e = json.load(fh)
    except (OSError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"
    if not isinstance(e, dict) or not isinstance(e.get("plan"), str):
        return None, "not a gate state"
    return e, None


def _old_state(st):
    """The current state of a plan for whoever is about to rewrite it: a corrupt one counts as none."""
    if not os.path.isfile(st):
        return {}
    e, _ = load_state(st)
    return e or {}


def write_state(plan_path, **fields):
    """Updates the plan's state preserving what we do not know.

    ⚠️ UPDATES, does not replace: later stages add fields, and an old copy of the gate that rewrote the
    whole dictionary would erase them. And it writes through tmp + os.replace because `pending_in_repo`
    reads these files on every check: a reader that catches a half-done write falls into the `except`
    and the plan vanishes from the pending list -- fail-open exactly at the moment of most writing.
    ⚠️ The temp file has a UNIQUE name (mkstemp): a fixed `<state>.tmp` let two concurrent writers
    interleave in the same file and publish a mix of both (A-I1).
    """
    st = state_path(plan_path)
    data = _old_state(st)
    data.update(fields)
    # ⚠️ `None` is the signal for "remove this key". Without it, the key would stay written as `null`
    # forever -- json does not tell absent from null, and whoever looks at the key directly (`in`/truth)
    # would go on acting on a dead value.
    for k in [k for k, v in data.items() if v is None]:
        del data[k]
    # ⚠️ `plan` is a state field AND the parameter's name: passing both collides. The path ALWAYS comes
    # from the positional, and callers never repeat it in `fields`.
    data["plan"] = os.path.abspath(plan_path)
    data["writer"] = os.path.realpath(__file__)
    data["updated"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(st), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(st), prefix=os.path.basename(st) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, st)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def gate_off():
    """Opt-out: switches off the DENIAL, never the recording -- and never the protection of the state folder.

    ⚠️ `mark` keeps running with the opt-out on, and a file tool aimed at the state folder is denied even
    with the opt-out on (R82): a state forged while the gate was off would hold once it is switched back on. Switching off both would be a persistent bypass: it
    would be enough to edit a released plan with the opt-out on so that, when it is switched off, the
    gate stayed open over content nobody checked -- the invalidation by change lives in `mark` and in
    the hash comparison of `pending_in_repo`.
    """
    return os.environ.get("PLAN_GATE", "").strip().lower() == "off"


# ⚠️ The execution log does NOT enter the hash.
#
# The timing rule says to fill the log AS IT HAPPENS, task by task. Since the release expires when the
# file changes, each timestamp written down revoked the release and forced a new one -- ten times in a
# ten-task plan. Two rules fighting each other, and the one losing was the timing one, which is the one
# that produces measured numbers.
#
# What the hash protects stays protected: changing a task, code or a constraint invalidates the
# release. Only the time record is neutral.
#
# ⚠️ The strip is LIMITED to the section (up to the next "## " or the end of the file). An earlier
# version (`.*` with re.S) cut from the heading to EOF: in a plan with the log in the MIDDLE, everything
# below it was out of the hash -- tasks could be edited after the release without the gate noticing.
# Whoever writes and whoever checks use the SAME function.
#
# Deliberate difference: `## Execution log` is accepted besides `## Log de execução`.
RE_EXECUTION_LOG = re.compile(
    r"^##\s+(?:Log de execu[çc][ãa]o|Execution log).*?(?=^##\s|\Z)", re.S | re.M)


def content_hash(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(
        RE_EXECUTION_LOG.sub("", text).encode("utf-8")).hexdigest()


def _inside(path, repo, folder):
    """NEW: is `path` inside the configured `folder`?

    Inside a repo, the folder is relative to the repo root (that is what the config means). Outside git
    there is no root to anchor on, and the INTERNAL rule holds: the folder as a substring of the path.
    """
    folder = folder.replace("\\", "/").strip("/")
    if not folder:
        return False
    if repo:
        base = os.path.realpath(os.path.join(repo, folder)).replace("\\", "/")
        return os.path.realpath(path).replace("\\", "/").startswith(base + "/")
    return folder in path.replace("\\", "/")


def is_plan(path, repo):
    c = os.path.abspath(path or "")
    # A file with a dot in front is a draft (`.tmp-new-task5.md`), not a plan: the hook registered them
    # as pending and they piled up in `status`.
    if os.path.basename(c).startswith("."):
        return False
    return (_inside(c, repo, config.load(repo)["plans_dir"]) and c.endswith(".md")
            and "/reviews/" not in c and "/.snapshots/" not in c)


def is_spec(path, repo):
    """A spec has its own gate, one level above the plan.

    ⚠️ Why: the plan review checks the plan AGAINST the spec, so a spec error passes by construction --
    the two agree. A pending spec blocks WRITING THE PLAN, just as a pending plan blocks writing code.
    """
    c = os.path.abspath(path or "")
    if os.path.basename(c).startswith("."):
        return False
    return (_inside(c, repo, config.load(repo)["specs_dir"]) and c.endswith(".md")
            and "/reviews/" not in c and "/adiados/" not in c and "/.snapshots/" not in c)


def _gated_files_by_key(repo):
    """{state key: path} of every plan and spec of this repo -- to name the target of a corrupt state file."""
    out = {}
    cfg = config.load(repo)
    for folder in (cfg["plans_dir"], cfg["specs_dir"]):
        for f in glob.glob(os.path.join(repo, folder, "**", "*.md"), recursive=True):
            if is_plan(f, repo) or is_spec(f, repo):
                out[key(f)] = os.path.abspath(f)
    return out


def _corrupt_entries(repo, corrupt):
    """A-I1: corrupt state files [(file, error)] -> pending entries. Fail closed.

    A corrupt state whose key is a plan/spec of THIS repo counts as that file pending. One whose target
    cannot be identified counts as pending too (`unknown`): the gate cannot tell it is not one of ours.
    """
    if not corrupt:
        return []
    by_key = _gated_files_by_key(repo)
    out = []
    for f, error in corrupt:
        target = by_key.get(os.path.basename(f)[:-len(".json")])
        out.append({"plan": target or f, "status": "pending", "corrupt": f, "error": error,
                    "unknown": target is None,
                    "gate_spec": target if target and is_spec(target, repo) else None})
    return out


def _corrupt_lines(entries, repo):
    """The warning lines for the corrupt entries among `entries` (empty string when none)."""
    lines = []
    for e in entries:
        if not e.get("corrupt"):
            continue
        if e.get("unknown"):
            lines.append(i18n.t("gate.corrupt_state_unknown", repo=repo, file=without_home(e["corrupt"]),
                                error=e["error"]))
        else:
            lines.append(i18n.t("gate.corrupt_state", repo=repo, file=without_home(e["corrupt"]),
                                error=e["error"], target=without_home(e["plan"])))
    return "\n".join(lines)


def _states():
    """-> ([state], [(file, error)]): the readable states and the corrupt files of the state folder."""
    good, corrupt = [], []
    for f in glob.glob(os.path.join(state_dir(), "*.json")):
        e, error = load_state(f)
        if e is None:
            corrupt.append((f, error))
        else:
            good.append(e)
    return good, corrupt


def pending_specs_in_repo(repo):
    out = []
    states, corrupt = _states()
    # A-I1: a corrupt state of a spec of this repo is a pending spec (fail closed)
    out.extend(e for e in _corrupt_entries(repo, corrupt) if e["gate_spec"])
    for e in states:
        target = e.get("gate_spec") or ""
        if not isinstance(target, str):
            target = ""
        if not target.startswith(repo.rstrip("/") + "/"):
            continue
        # Deliberate difference: no `released -> continue` -- a released spec is tied to the hash too.
        if e.get("plan_hash") and content_hash(target) != e.get("plan_hash"):
            e["status"] = "pending"      # the spec changed after it was released
        # Fix round 1: same guard as `pending_in_repo` -- a released spec moved or deleted gets hash None
        # and, without it, stayed pending forever, denying every plan with no way to release it.
        if e.get("status") == "pending" and os.path.isfile(target):
            out.append(e)
    return out


def repo_of(path):
    try:
        # Deliberate difference (R24, R28): a Write into a folder that does not exist yet (`src/new/x.py`)
        # ran git with a missing cwd, got no repo and let the write through. Normalize `..` FIRST (or the
        # walk climbs out through `/missing/..`), then walk up to an existing folder.
        path = os.path.abspath(path)
        d = path if os.path.isdir(path) else os.path.dirname(path)
        while d and not os.path.isdir(d):
            d = os.path.dirname(d)
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=d or ".",
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def pending_in_repo(repo):
    """Pending plans whose file lives inside this repo -- plus every corrupt state file (A-I1, fail closed)."""
    states, corrupt = _states()
    out = _corrupt_entries(repo, corrupt)
    for e in states:
        if not e["plan"].startswith(repo.rstrip("/") + "/"):
            continue
        # ⚠️ An approval -- and, deliberate difference, a release -- holds for the plan THAT WAS checked.
        # If the plan changed afterwards, it expired -- otherwise approving and rewriting was enough.
        # The release is caught here too because an edit outside the editor (`sed -i`) fires no hook.
        if e.get("status") in ("approved", "released") and content_hash(e["plan"]) != e.get("plan_hash"):
            reason_key = "gate.changed_after_approval" if e["status"] == "approved" else "gate.changed_after_release"
            e = write_state(e["plan"], status="pending", reason=i18n.t(reason_key, repo=repo))
        if e.get("status") == "pending" and os.path.isfile(e["plan"]):
            out.append(e)
    return out


# The tools that can fix a broken config: they name the file they write.
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _target_of(tool, ti):
    """The file a file tool writes: NotebookEdit names it `notebook_path`, the others `file_path` (A-M2, R30)."""
    if tool not in FILE_TOOLS:
        return ""
    fp = ti.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    return fp if isinstance(fp, str) else ""


def _in_state_dir(path):
    """R82: is `path`, after realpath, the state folder or under it? A link pointing in counts as in."""
    folder = os.path.realpath(state_dir())
    p = os.path.realpath(path)
    return p == folder or p.startswith(folder.rstrip(os.sep) + os.sep)


def _broken_config(repo, tool, ti):
    """NEW (R21/R26): the exit code of `check` when the repo's config is broken, or None when it loads.

    A gate that falls back to defaults on a broken config is fail-open. The one exception is a file tool
    whose path IS the config: otherwise nobody can fix it from inside the session. Bash never passes
    (R26): telling from the text which files a command line writes is a game the gate loses (`x#y`,
    `1<>` beat every token rule tried). The path is compared after `abspath` only, never `realpath`,
    and never while the config is a link (R30): `ln -sf ../app.py .claude/plan-gate.json` would
    otherwise make app.py "the config".
    """
    try:
        config.load(repo)
        return None
    except Exception as error:      # anything that slips past config's own validation is broken too
        path = os.path.join(repo, ".claude", "plan-gate.json")
        fp = _target_of(tool, ti)
        names_it = (tool in FILE_TOOLS and bool(fp) and not os.path.islink(path)
                    and os.path.abspath(fp) == path)
        if names_it:
            return 0
        print(i18n.t("gate.config_broken", error=error, path=without_home(path)), file=sys.stderr)
        return 2


def cmd_mark(entry):
    path = (entry.get("tool_input") or {}).get("file_path", "")
    if not path:
        return 0
    repo = repo_of(path)
    try:
        config.load(repo)
    except Exception as error:      # R21: a broken config is not silent defaults, whatever the error
        print(i18n.t("gate.config_broken", error=error,
                     path=without_home(os.path.join(repo, ".claude", "plan-gate.json"))), file=sys.stderr)
        return 2
    if is_spec(path, repo):
        return _mark_spec(path, repo)
    if not is_plan(path, repo):
        return 0
    # A-I1: a corrupt state counts as none, so this edit rewrites it as pending (a crash here was rc 0)
    old = _old_state(state_path(path))
    if old.get("status") in ("approved", "released") and old.get("plan_hash") == content_hash(path):
        return 0  # nothing changed, the approval still holds
    # R31: pending is written BEFORE any check runs -- a hook killed by its timeout leaves the plan pending,
    # never approved and never without a state.
    write_state(path, status="pending", plan_hash=content_hash(path))
    _timeline_event(repo, "plan_written")     # after the state: the timeline never decides the gate
    status, results = run_checks(path)
    if status == "approved":
        return 0          # R33: approved by the checks -- nothing to send back to the model
    print(i18n.t("gate.mark_plan", repo=repo, name=os.path.basename(path), path=without_home(os.path.abspath(path)),
                 gate=command("plan_gate.py")) + _failed_checks(results, repo),
          file=sys.stderr)
    return 2  # exit 2 sends the stderr back to the model


def _mark_spec(path, repo):
    old = _old_state(state_path(path))
    if old.get("status") in ("approved", "released") and old.get("plan_hash") == content_hash(path):
        return 0
    write_state(path, status="pending", gate_spec=os.path.abspath(path),
                plan_hash=content_hash(path))     # R31: before the checks
    _timeline_event(repo, "spec_written")
    status, results = run_checks(path)
    if status == "approved":
        return 0
    print(i18n.t("gate.mark_spec", repo=repo, name=os.path.basename(path), path=without_home(os.path.abspath(path)),
                 gate=command("plan_gate.py"), spec_preflight=command("preflight_spec.py"))
          + _failed_checks(results, repo),
          file=sys.stderr)
    return 2


class NotGated(Exception):
    """run-checks on a file that is neither a plan nor a spec."""


def _timeline_event(repo, event):
    """Records `event` in the repo's timeline (spec §5.2). It must never raise.

    ⚠️ Called only AFTER the gate state is written: a timeline that fails (a full disk, a folder that is a
    file) is logged and nothing more -- it can never undo or block a release or an approval. It writes
    neither stdout nor stderr: `mark` runs as a hook.
    """
    if not repo:
        return None
    try:
        import timeline
        timeline.append(repo, event, "hook")
    except Exception as error:  # noqa: BLE001
        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now().isoformat()} timeline {event}: {error!r}\n")
        except OSError:
            pass
    return None


def run_checks(path):
    """Runs the required applicable checks on `path` and writes the state -> (status, {id: result}).

    Approves (`approved`, `plan_hash`) only with AT LEAST ONE required applicable check AND every one of
    them passing at the CURRENT hash (spec §4.2, vacuity level 1): zero checks approve nothing.

    ⚠️ R31: the state is `pending` BEFORE the first check runs, so a run killed half-way leaves it pending.
    ⚠️ A human release of THIS content stays a release: the checks are recorded beside it, not over it.
    ⚠️ If the file changed while the checks ran, their verdict is about bytes that no longer exist: no
    approval, and no final write -- the pending written at the start stands, and a newer run (the mark of
    that edit) owns the state.
    """
    path = os.path.abspath(path)
    repo = repo_of(path)
    if is_spec(path, repo):
        kind, extra = "spec", {"gate_spec": path}
    elif is_plan(path, repo):
        kind, extra = "plan", {}
    else:
        raise NotGated(path)
    h = content_hash(path)
    old = _old_state(state_path(path))
    held = old.get("status") == "released" and old.get("plan_hash") == h
    if not held:
        write_state(path, status="pending", plan_hash=h, **extra)
    required = [c for c in checks.applicable(kind, repo) if c["required"]]
    results = {c["id"]: checks.run(c, path) for c in required}
    if content_hash(path) != h:
        return "pending", results
    recorded = {cid: {"status": r["status"], "hash": h} for cid, r in results.items()}
    if held:
        write_state(path, checks=recorded)
        return "released", results
    if required and all(r["status"] == "pass" for r in results.values()):
        write_state(path, status="approved", plan_hash=h, checks=recorded, reason=None, **extra)
        # ⚠️ `approved`/`released` open the implementation phase: a SPEC approval must not.
        _timeline_event(repo, "approved" if kind == "plan" else "spec_approved")
        return "approved", results
    write_state(path, status="pending", plan_hash=h, checks=recorded, **extra)
    return "pending", results


def _failed_checks(results, repo):
    """The lines naming each check that did not pass (empty when no check ran)."""
    lines = []
    for cid, r in results.items():
        if r["status"] == "pass":
            continue
        groups = []
        for g in r.get("findings") or []:
            if not isinstance(g, dict) or g.get("state") in ("ok", "not_applicable"):
                continue
            if g.get("group") == checks.ERROR_GROUP:
                groups.append(f"{checks.ERROR_GROUP}: " + "; ".join(str(i) for i in g.get("items") or []))
            else:
                groups.append(str(g.get("group")))
        lines.append(i18n.t("gate.check_failed_line", repo=repo, id=cid, groups=", ".join(groups) or "fail"))
    if not lines:
        return ""
    return "\n" + i18n.t("gate.checks_failed", repo=repo) + "\n" + "\n".join(lines)


def _checks_hint(repo):
    """Orphan / corrupted checks, for whoever is blocked. ⚠️ Never raises: it runs AFTER a denial was decided,
    and an exception here would reach main's fail-open handler and turn the denial into rc 0."""
    try:
        lines = []
        gone = checks.orphans()
        if gone:
            lines.append(i18n.t("gate.orphan_checks", repo=repo, ids=", ".join(gone), gate=command("plan_gate.py")))
        missing = checks.missing_required(repo) if repo else []
        if missing:
            lines.append(i18n.t("gate.missing_checks", repo=repo, ids=", ".join(missing)))
        bad = checks.corrupted()
        if bad:
            lines.append(i18n.t("gate.corrupted_checks", repo=repo, ids=", ".join(c["id"] for c in bad),
                                folder=without_home(checks.checks_dir())))
            lines.extend("   " + c["error"] for c in bad if "conflict" in c)
        return "\n".join(lines)
    except Exception:       # noqa: BLE001
        return ""


INTERNAL_PLUGIN = "revisao-de-plano@pentagrama"


def _settings_files(home, repo):
    files = [os.path.join(home, ".claude", n) for n in ("settings.json", "settings.local.json")]
    if repo:
        files += [os.path.join(repo, ".claude", n) for n in ("settings.json", "settings.local.json")]
    return files


def _read_settings(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def internal_gate_active(home=None, repo=None):
    """Path of the settings file that enables the INTERNAL gate while it is not switched off, else None.

    Same opt-out parse as the INTERNAL (`plan-gate.py:251`: `.strip().lower() == "off"`), read from the
    process environment or from the `env` block of ANY of the files. Never raises: a missing, unreadable or
    broken file is skipped, and this is only a warning -- it never changes a gate decision.
    """
    try:
        def off(value):
            return isinstance(value, str) and value.strip().lower() == "off"
        if off(os.environ.get("PORTAO_DE_PLANO", "")):
            return None
        loaded = [(f, _read_settings(f)) for f in _settings_files(home or os.path.expanduser("~"), repo)]
        for _, data in loaded:
            env = data.get("env")
            if isinstance(env, dict) and off(env.get("PORTAO_DE_PLANO")):
                return None
        for path, data in loaded:
            enabled = data.get("enabledPlugins")
            if isinstance(enabled, dict) and enabled.get(INTERNAL_PLUGIN) is True:
                return path
    except Exception:       # noqa: BLE001
        return None
    return None


def _warn_two_gates(repo):
    """Warning on STDERR only (hooks never write stdout)."""
    settings = internal_gate_active(repo=repo)
    if settings:
        print(i18n.t("gate.two_gates", settings=settings), file=sys.stderr)


def cmd_check(entry):
    rc = _check(entry)
    if rc == 2:
        try:
            ti = entry.get("tool_input") or {}
            _warn_two_gates(repo_of(_target_of(entry.get("tool_name", ""), ti) or entry.get("cwd") or os.getcwd()))
        except Exception:       # noqa: BLE001
            pass
    return rc


def _check(entry):
    tool = entry.get("tool_name", "")
    ti = entry.get("tool_input") or {}
    fp = _target_of(tool, ti)

    # R82: nothing legitimate writes the gate's own state through a tool -- a forged `approved` state or check
    # manifest is self-approval. Denied whatever the repo, and before the opt-out (see gate_off).
    if fp and _in_state_dir(fp):
        print(i18n.t("gate.deny_state_dir", path=without_home(os.path.abspath(fp)),
                     folder=without_home(state_dir())), file=sys.stderr)
        return 2
    if gate_off():
        return 0

    if tool == "Bash":
        target = entry.get("cwd") or os.getcwd()
    elif tool in IMPLEMENTATION_TOOLS:
        target = fp or entry.get("cwd") or os.getcwd()
    else:
        return 0

    # R26: the config comes BEFORE the Bash safety test -- under a broken config no Bash passes, not even
    # one `bash_is_safe` reads as harmless (`> /tmp/...` is "innocent", and the repo may live in /tmp).
    repo = repo_of(target)
    denial = _broken_config(repo, tool, ti) if repo else None
    if denial is not None:
        return denial
    if tool == "Bash" and bash_is_safe(ti.get("command", "")):
        return 0

    if tool in IMPLEMENTATION_TOOLS:
        # Touching the SPEC itself is what is expected while it is pending.
        if fp and is_spec(target, repo):
            return 0
        # ⚠️ Writing the PLAN with a pending spec is the hole this gate closes: the plan review checks the
        # plan AGAINST the spec, so a spec defect passes by construction.
        if fp and is_plan(target, repo):
            pend_spec = pending_specs_in_repo(repo) if repo else []
            if pend_spec:
                names = ", ".join(os.path.basename(e["gate_spec"]) for e in pend_spec)
                print(i18n.t("gate.deny_spec", repo=repo, names=names, gate=command("plan_gate.py"),
                             spec_preflight=command("preflight_spec.py")), file=sys.stderr)
                for text in (_corrupt_lines(pend_spec, repo), _checks_hint(repo)):
                    if text:
                        print(text, file=sys.stderr)
                return 2
            return 0
        # A-M3: the planning folder is `docs/superpowers` AT THE REPO ROOT -- `src/docs/superpowers/x.py` is code.
        if fp and repo and _inside(target, repo, "docs/superpowers"):
            return 0

    if not repo:
        return 0
    pend = pending_in_repo(repo)
    if not pend:
        return 0

    names = ", ".join(os.path.basename(e["plan"]) for e in pend)
    print(i18n.t("gate.deny_plan", repo=repo, names=names, gate=command("plan_gate.py")), file=sys.stderr)
    for text in (_corrupt_lines(pend, repo), _checks_hint(repo)):
        if text:
            print(text, file=sys.stderr)
    return 2


def cmd_status(args):
    current_repo = repo_of(os.getcwd())
    _warn_two_gates(current_repo)
    if current_repo:
        everything = releases_of_repo(current_repo)
        with_escape = [d for d in everything if d.get("escapes")]
        if everything:
            print(i18n.t("gate.status_releases", repo=current_repo, n=len(everything), k=len(with_escape)))
            if len(with_escape) > len(everything) / 2:
                print(i18n.t("gate.status_escape_warning", repo=current_repo))
    if gate_off():
        print(i18n.t("gate.status_off", repo=current_repo))
    hint = _checks_hint(current_repo)
    if hint:
        print(hint)
    target = args[0] if args else None
    if target:
        st = state_path(target)
        if os.path.isfile(st):
            with open(st, encoding="utf-8") as fh:
                print(fh.read())
        else:
            print(i18n.t("gate.status_no_state", repo=current_repo))
        return 0
    found = False
    for f in sorted(glob.glob(os.path.join(state_dir(), "*.json"))):
        try:
            e, error = load_state(f)
            if e is None:
                raise ValueError(error)
            # ⚠️ `str()` here, not only in the `print` below -- a non-string `status` (JSON edited by hand,
            # or written by an old version) makes `f"{state:9s}"` raise `ValueError` OUTSIDE this `try`,
            # and takes the whole listing down from that file on. Same for a MISSING `status`.
            state = str(e.get("status") or "?")
            # Deliberate difference: a `released` whose content changed is expired too.
            expired = (e.get("status") in ("approved", "released")
                       and e.get("plan_hash") is not None
                       and content_hash(e["plan"]) != e.get("plan_hash"))
        except (OSError, ValueError, KeyError) as err:
            # corrupted file, or a plan that no longer exists on disk
            unreadable = i18n.t("gate.status_unreadable", repo=current_repo)
            print(f"{unreadable:9s} {os.path.basename(f)}: {type(err).__name__}: {err}")
            found = True
            continue
        print(f"{state:9s} {i18n.t('gate.status_expired', repo=current_repo) if expired else ''}"
              f"{e.get('plan')}")
        found = True
    if not found:
        print(i18n.t("gate.status_none", repo=current_repo))
    return 0


import preflight_plan   # noqa: E402  (same directory; the plugin is one for that reason)

# R16: the escape vocabulary is the stable English id; run_groups is keyed by the internal key.
GROUP_IDS = [gid for _, gid in preflight_plan.GROUPS]
INTERNAL_KEY = {gid: k for k, gid in preflight_plan.GROUPS}


def _raw_digest(data):
    """sha256 of the WHOLE content -- answers "did I examine exactly these bytes?".

    ⚠️ Not to be confused with content_hash(): that one is the PERSISTED format and ignores the execution
    log section on purpose. Here ignoring anything would be the hole.
    """
    return hashlib.sha256(data).hexdigest()


class InvalidEscape(Exception):
    """⚠️ An escape refusal exits with code 2, like every refusal of `release`. A `sys.exit(msg)` would
    exit 1, which is the "finding" code -- two different reasons with the same answer."""


def _pair(text):
    """`group=justification` -> (group, justification), validating the shape.

    The group id is validated by the caller, against the ids of the kind being released (plan or spec):
    at parse time it is not known yet which one it is.
    """
    group, sep, just = text.partition("=")
    group, just = group.strip(), just.strip()
    if not sep or not just:
        raise InvalidEscape(i18n.t("gate.escape_malformed", received=repr(text)))
    return group, just


def _unknown_group(pairs, valid, repo):
    """The refusal text for the first escape whose group is not one of `valid`, or None."""
    for group, _ in pairs:
        if group not in valid:
            return i18n.t("gate.escape_unknown_group", repo=repo, group=repr(group), groups=", ".join(valid))
    return None


def record_in_log(plan, reason, escapes, when):
    """One JSON line per release, appended.

    ⚠️ The state keeps the NOW of each plan; the log keeps the HISTORY. Without it, releasing again erases
    the previous release and nobody answers "how many times did this plan go out by escape" -- which is
    the number that says whether the check is badly calibrated.
    """
    line = json.dumps({"when": when, "plan": os.path.abspath(plan), "reason": reason,
                       "escapes": escapes, "writer": os.path.realpath(__file__)},
                      ensure_ascii=False)
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), "releases.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def releases_of_repo(repo):
    path = os.path.join(state_dir(), "releases.log")
    if not os.path.isfile(path) or not repo:
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("plan", "").startswith(repo.rstrip("/") + "/"):
                out.append(d)
    return out


def _release_spec(spec, a):
    """Releases a SPEC: runs the spec preflight right then and refuses if there is a blocking finding.

    ⚠️ There is no "release without checking", same as the plan: the report is about the bytes of that
    instant. The escape here is the same `--false-positive <group>="…"`, because a spec finding can be
    false too -- with the SPEC group ids.
    """
    repo = repo_of(spec)
    if not repo:
        print(i18n.t("gate.spec_not_in_git"), file=sys.stderr)
        return 2
    import preflight_spec as psp
    spec_ids = [gid for gid, _title, _blocks in psp.GROUPS]
    refusal = _unknown_group(a.false_positives + a.no_coverage, spec_ids, repo)
    if refusal:
        print(i18n.t("gate.not_released", repo=repo, reason=refusal), file=sys.stderr)
        return 2
    # R84 (a): `--no-coverage` is for a plan group that did not run; a spec release refuses it instead of
    # silently ignoring it.
    if a.no_coverage:
        print(i18n.t("gate.no_coverage_not_for_spec", repo=repo), file=sys.stderr)
        return 2
    # R84 (c): snapshot -- the verdict is about THESE bytes, and only they will be released
    with open(spec, "rb") as f:
        raw = _raw_digest(f.read())
    res = psp.evaluate(spec, repo)
    escaped = {g: j for g, j in (a.false_positives or [])}
    # R84 (b): an escape is for a BLOCKING group whose result is `findings`, as for the plan
    blocks_of = {gid: blocks for gid, _title, blocks in psp.GROUPS}
    for group in escaped:
        state = res[group][0]
        if not blocks_of[group] or state != "findings":
            shown = state if blocks_of[group] else i18n.t("gate.state_not_blocking", repo=repo, state=state)
            print(i18n.t("gate.false_positive_wrong_state", repo=repo, group=group, state=shown), file=sys.stderr)
            return 2
    blocking = []
    for gid, _title, blocks in psp.GROUPS:
        state, findings, _ = res[gid]
        if not blocks or state != "findings":
            continue
        if gid in escaped:
            continue
        blocking.append((gid, i18n.t("spec.group." + gid, repo=repo), findings))
    if blocking:
        print(i18n.t("gate.spec_blocking_groups", repo=repo, groups=", ".join(g for g, _t, _f in blocking)),
              file=sys.stderr)
        for gid, title, findings in blocking:
            print(f"  - {gid}: {title} ({len(findings)})", file=sys.stderr)
            for x in findings[:5]:
                print(f"      {x}", file=sys.stderr)
            print(i18n.t("gate.spec_fix_or_escape", repo=repo, group=gid), file=sys.stderr)
        print(i18n.t("gate.spec_memory_warning", repo=repo), file=sys.stderr)
        return 2
    trigger = os.environ.get("PLAN_GATE_TEST_EDIT")      # race test hook (only when the variable is set)
    if trigger:
        subprocess.run([sys.executable, trigger], check=False)
    with open(spec, "rb") as f:
        if _raw_digest(f.read()) != raw:
            print(i18n.t("gate.changed_during_check", repo=repo), file=sys.stderr)
            return 2
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    write_state(spec, status="released", gate_spec=os.path.abspath(spec),
                plan_hash=content_hash(spec), override_reason=a.reason.strip(),
                released_at=now,
                escapes=[{"group": g, "type": "false-positive", "text": j, "when": now}
                         for g, j in escaped.items()])
    record_in_log(spec, a.reason.strip(),
                  [{"group": g, "type": "false-positive", "text": j, "when": now}
                   for g, j in escaped.items()], now)
    _timeline_event(repo, "spec_released")      # not `released`: that one opens the implementation phase
    print(i18n.t("gate.spec_released", repo=repo, path=without_home(spec)))
    print(i18n.t("gate.reason_line", repo=repo, reason=a.reason.strip()))
    if escaped:
        print(i18n.t("gate.with_escape", repo=repo, escapes=", ".join(f"{g} (false-positive)" for g in escaped)))
    return 0


def cmd_release(args):
    p = argparse.ArgumentParser(prog="plan_gate.py release")
    p.add_argument("plan")
    p.add_argument("--reason", default="")
    p.add_argument("--false-positive", action="append", default=[], type=_pair, dest="false_positives")
    p.add_argument("--no-coverage", action="append", default=[], type=_pair, dest="no_coverage")
    try:
        a = p.parse_args(args)
    except InvalidEscape as e:
        print(i18n.t("gate.not_released", reason=e), file=sys.stderr)
        return 2

    plan = os.path.abspath(a.plan)
    if not os.path.isfile(plan):
        # Deliberate difference: the hint searches the CURRENT repo (the INTERNAL globbed a fixed folder under the home).
        here = repo_of(os.getcwd())
        candidates = sorted(glob.glob(os.path.join(
            here, "**", config.load(here)["plans_dir"], os.path.basename(plan)), recursive=True)) if here else []
        hint = (i18n.t("gate.did_you_mean", repo=here) + "\n  ".join(without_home(c) for c in candidates)
                if candidates else "")
        sys.exit(i18n.t("gate.plan_missing", repo=here, path=without_home(plan), cwd=without_home(os.getcwd()))
                 + hint)
    repo = repo_of(plan)
    if not a.reason.strip():
        print(i18n.t("gate.reason_required", repo=repo), file=sys.stderr)
        return 2

    if is_spec(plan, repo):
        return _release_spec(plan, a)

    if not repo:
        sys.exit(i18n.t("gate.plan_not_in_git"))
    refusal = _unknown_group(a.false_positives + a.no_coverage, GROUP_IDS, repo)
    if refusal:
        print(i18n.t("gate.not_released", repo=repo, reason=refusal), file=sys.stderr)
        return 2

    # 1. snapshot: the report runs over THESE bytes, and only they will be released
    with open(plan, "rb") as f:
        bytes_read = f.read()
    raw = _raw_digest(bytes_read)
    try:
        content = bytes_read.decode("utf-8")
    except UnicodeDecodeError as e:
        print(i18n.t("gate.plan_not_utf8", repo=repo, byte=e.start), file=sys.stderr)
        return 2

    text, findings, not_evaluated = preflight_plan.human_report(plan, repo)
    results = preflight_plan.run_groups(
        content, repo, preflight_plan.tasks_of_plan(content))

    escaped = {}
    for group, just in a.false_positives:
        state = results[INTERNAL_KEY[group]].state
        if state != "findings":
            print(i18n.t("gate.false_positive_wrong_state", repo=repo, group=group, state=state), file=sys.stderr)
            return 2
        escaped[group] = {"group": group, "type": "false-positive", "text": just}
    for group, just in a.no_coverage:
        state = results[INTERNAL_KEY[group]].state
        if state != "not_evaluated":
            print(i18n.t("gate.no_coverage_wrong_state", repo=repo, group=group, state=state), file=sys.stderr)
            return 2
        escaped[group] = {"group": group, "type": "no-coverage", "text": just}

    hanging = [gid for k, gid in preflight_plan.GROUPS if not results[k].allows() and gid not in escaped]
    if hanging:
        print(text)
        print(i18n.t("gate.blocking_groups", repo=repo, groups=", ".join(hanging)), file=sys.stderr)
        for gid in hanging:
            r = results[INTERNAL_KEY[gid]]
            verb = "--false-positive" if r.state == "findings" else "--no-coverage"
            print(i18n.t("gate.blocking_line", repo=repo, group=gid, state=r.state,
                         reason=(" — " + r.reason if r.reason else ""), verb=verb), file=sys.stderr)
        return 2

    # 2. race test hook (exists only when the variable is set)
    trigger = os.environ.get("PLAN_GATE_TEST_EDIT")
    if trigger:
        subprocess.run([sys.executable, trigger], check=False)
    if os.environ.get("PLAN_GATE_TEST_BREAK"):
        raise RuntimeError("fault injected by the test, between the check and the write")

    # 3. is the file still the one that was examined?
    with open(plan, "rb") as f:
        if _raw_digest(f.read()) != raw:
            print(i18n.t("gate.changed_during_check", repo=repo), file=sys.stderr)
            return 2

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for d in escaped.values():
        d["when"] = now
    escapes = list(escaped.values())
    write_state(plan, status="released", plan_hash=content_hash(plan),
                override_reason=a.reason.strip(), released_at=now, escapes=escapes)
    record_in_log(plan, a.reason.strip(), escapes, now)
    _timeline_event(repo, "released")           # after the state and the log: it can never undo them
    rev = os.path.join(os.path.dirname(plan), "reviews")
    os.makedirs(rev, exist_ok=True)
    with open(os.path.join(rev, os.path.basename(plan)[:-3] + "-override.md"), "a", encoding="utf-8") as fh:
        fh.write(i18n.t("gate.override_entry", repo=repo, when=now, reason=a.reason.strip()))
        for d in escaped.values():
            fh.write(i18n.t("gate.override_escape", repo=repo, group=d["group"], type=d["type"], text=d["text"]))
    print(i18n.t("gate.released", repo=repo, path=without_home(plan), reason=a.reason.strip()))
    if escaped:
        print(i18n.t("gate.with_escape", repo=repo,
                     escapes=", ".join(f"{d['group']} ({d['type']})" for d in escaped.values())))
    return 0


def cmd_run_checks(args):
    if len(args) != 1:
        print(i18n.t("gate.usage"), file=sys.stderr)
        return 2
    try:
        status, results = run_checks(args[0])
    except NotGated:
        print(i18n.t("gate.not_gated", path=without_home(os.path.abspath(args[0]))), file=sys.stderr)
        return 2
    repo = repo_of(args[0])
    for cid, r in results.items():
        print(f"{r['status']:5s} {cid}")
    if not results:
        print(i18n.t("gate.no_required_checks", repo=repo))
    print(status)
    if status == "pending":
        failed = _failed_checks(results, repo)
        if failed:
            print(failed.lstrip("\n"), file=sys.stderr)
        return 2
    return 0



def cmd_register_check(args):
    if len(args) != 1:
        # a hook subcommand: the error goes through main's handler (logged, stderr, exit 0 -- R37)
        raise ValueError(i18n.t("gate.usage"))
    manifest = os.path.abspath(args[0])
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(manifest)
    ids = checks.register(manifest, root)
    # R37: stderr only -- another plugin's SessionStart calls this, and SessionStart stdout is model context.
    print(i18n.t("gate.checks_registered", ids=", ".join(ids)), file=sys.stderr)
    return 0


def cmd_register_checks():
    """SessionStart. ⚠️ R33: NEVER writes stdout -- SessionStart stdout becomes model context. Errors go to
    stderr and errors.log, and the exit is 0 (main's hook handler)."""
    checks.register(os.path.join(PLUGIN_DIR, "plan-gate-check.json"), PLUGIN_DIR)
    return 0


def cmd_checks(args):
    if args == ["list"]:
        return checks.cmd_list()
    if args == ["prune"]:
        return checks.cmd_prune()
    print(i18n.t("gate.usage"), file=sys.stderr)
    return 2


HOOK_SUBCOMMANDS = {"mark", "check", "register-check", "register-checks"}


def main():
    if len(sys.argv) < 2:
        sys.exit(i18n.t("gate.usage"))
    sub = sys.argv[1]
    try:
        os.makedirs(state_dir(), exist_ok=True)
        if sub in ("mark", "check"):
            raw = sys.stdin.read() or "{}"
            entry = json.loads(raw)
            return cmd_mark(entry) if sub == "mark" else cmd_check(entry)
        if sub == "status":
            return cmd_status(sys.argv[2:])
        if sub == "release":
            return cmd_release(sys.argv[2:])
        if sub == "run-checks":
            return cmd_run_checks(sys.argv[2:])
        if sub == "register-check":
            return cmd_register_check(sys.argv[2:])
        if sub == "register-checks":
            return cmd_register_checks()
        if sub == "checks":
            return cmd_checks(sys.argv[2:])
        sys.exit(i18n.t("gate.unknown_subcommand", sub=sub))
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.datetime.now().isoformat()} {sub}: {error!r}\n")
        except OSError:
            pass
        if sub in HOOK_SUBCOMMANDS:
            if sub in ("register-check", "register-checks"):
                print(f"plan-gate {sub}: {error!r}", file=sys.stderr)    # R33/R37: stderr, never stdout
            # ⚠️ Fail-OPEN ON PURPOSE, and with a trail: a bug in the gate cannot lock everybody's work
            # in every project. The price is the gate vanishing in silence -- that is why the log, and why
            # it is tested by trying to get around it.
            return 0
        # ⚠️ A manual command fails CLOSED: a `release` that blows up and returns 0 says "released" to
        # whoever is reading the screen, with nothing written.
        print(i18n.t("gate.manual_error", sub=sub, error=repr(error), log=without_home(log_path())),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
