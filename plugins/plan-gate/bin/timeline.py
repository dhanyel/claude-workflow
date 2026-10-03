#!/usr/bin/env python3
"""Timeline: one append-only file of stamps per demand, and the phase report built from it (spec §5).

  timeline.py hook prompt   UserPromptSubmit -> `prompt`
  timeline.py hook stop     Stop             -> `stop`
  timeline.py hook bash     PostToolUse(Bash): `git commit` that moved HEAD -> `first_commit` / `commit`;
                            `git push` that left `@{u}` == HEAD -> `push`

The gate stamps `plan_written`, `spec_written`, `released` and `approved` itself (plan_gate._timeline_event).

⚠️ The core rule: NEVER estimate. A boundary without a stamp is `None` and renders as `—`; a number that was not
read from a clock is worse than no number, because a week later it cannot be told apart from a measured one.

⚠️ R42: the hooks run on EVERY prompt and every stop. They always exit 0, never write stdout (UserPromptSubmit
stdout becomes model context) nor stderr, make no network call, and are a no-op outside a git repository.
Errors go to <state_dir()>/errors.log.
"""
import contextlib, datetime, hashlib, io, json, os, re, shlex, subprocess, sys

# ⚠️ Bootstrap of the sibling modules: the test helper `import_bin` does not touch sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# ⚠️ R42: the sibling modules (config, i18n -- never plan_gate, which imports us) are imported INSIDE the
# functions, so a broken one fails inside main's guard: logged, exit 0, nothing printed. A module-level import
# would print a traceback and exit 1 on every prompt.


def state_dir():
    from config import state_dir as real
    return real()


def _fallback_state_dir():
    """Only for logging when config.py itself cannot be imported: the same rule as config.state_dir()."""
    return os.environ.get("PLAN_GATE_DIR") or os.path.expanduser("~/.claude/claude-workflow/plan-gate")

DASH = "—"

# (phase, events that open it) in the fixed order of the report; the end of the last phase is `delivered`.
# ⚠️ R77: `delivery` is special -- see DELIVERY_OPENERS and report().
PHASES = (
    ("discussion", ("start",)),
    ("spec", ("spec_written",)),
    ("plan", ("plan_written",)),
    ("implementation", ("released", "approved")),
    ("review", ("review",)),
    ("validation", ("validation",)),
    ("delivery", ("first_commit",)),
)
END = "delivered"
# R77: delivery opens at the first of these that comes at or after the start of EVERY earlier phase present -- a
# commit made during the implementation (one commit per task) is implementation, not delivery.
DELIVERY_OPENERS = ("first_commit", "commit", "push")

# git global options that take a separate value (`git -c k=v commit`); `-C` is handled apart (R47).
_GIT_OPTS_WITH_VALUE = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
# Shell operators that end a simple command (newline included: a multi-line command is several commands).
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"}


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _git(repo, *args):
    """stdout of `git -C repo …`, stripped, or None when git fails. Output is always captured (R42)."""
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def repo_root(cwd):
    """The top of the git repository holding `cwd`, or None (outside a repo the hooks are a no-op)."""
    if not cwd or not os.path.isdir(cwd):
        return None
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return os.path.realpath(top) if top else None


def demand(repo):
    """The current branch; `"detached"` without one."""
    return _git(repo, "symbolic-ref", "--short", "-q", "HEAD") or "detached"


# R49: the number right after the type prefix, followed by `-`, `_` or the end of the name; digits later in the
# slug do not matter (`feat/58-onda5` -> 58). None when that leading part is a version or a date
# (`release/1.2.3`, `hotfix/2026-10-03`): Task 12 comments on this number, and a wrong one comments on someone
# else's issue.
RE_ISSUE = re.compile(r"^(?:[A-Za-z][A-Za-z_-]*/)?(?P<rest>.*)$", re.S)
RE_LEADING_NUMBER = re.compile(r"^(\d+)(?:[-_]|$)")
RE_VERSION_OR_DATE = re.compile(r"^\d+(?:[.-]\d+)+")


def issue_ref(demand):
    """`feat/58-x` / `feat/58` / `feat/58-onda5` -> `"58"`; None for a version or a date (`release/1.2.3`,
    `hotfix/2026-10-03`) and for a name that does not start with the number (`feat/x-58`, `main`)."""
    rest = RE_ISSUE.match(demand or "").group("rest")
    if RE_VERSION_OR_DATE.match(rest):
        return None
    m = RE_LEADING_NUMBER.match(rest)
    return m.group(1) if m else None


def path(repo, demand):
    """<state_dir()>/timeline/<repo name>-<sha1(realpath(repo))[:8]>/<demand>.jsonl.

    R8: a branch with `/` becomes nested folders. Git forbids `..` and components starting with `.`, but the
    demand is still checked: a name that would leave the repo's folder is replaced by its hash.
    """
    real = os.path.realpath(repo)
    folder = os.path.join(state_dir(), "timeline",
                          f"{os.path.basename(real)}-{hashlib.sha1(real.encode()).hexdigest()[:8]}")
    parts = [p for p in (demand or "").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        parts = ["demand-" + hashlib.sha1((demand or "").encode()).hexdigest()[:8]]
    return os.path.join(folder, *parts[:-1], parts[-1] + ".jsonl")


def append(repo, event, source, detail=None, now=None, branch=None):
    """Appends one event to the demand `branch` of `repo` -- the current branch when None (R81: `phase.py start
    --branch B` stamps a branch that is not checked out yet). Never rewrites: the file is opened in append mode."""
    record = {"t": now or _now(), "event": event, "source": source, "detail": detail,
              "head": _git(repo, "rev-parse", "--verify", "-q", "HEAD")}
    target = path(repo, branch or demand(repo))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def events(repo, demand):
    """Every event of the demand, in file order; a line that is not a JSON object is skipped."""
    try:
        with open(path(repo, demand), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# ---------------------------------------------------------------------------------------------------- report

def _when(item):
    try:
        t = datetime.datetime.fromisoformat(item.get("t"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None          # a stamp without an offset cannot be compared: it is not a stamp


def _waiting_intervals(timed):
    """([(last stop, the prompt that closed it)], unclosed stop or None).

    R45: only CLOSED pairs are waiting, and each runs from the LAST stop before the prompt -- an earlier stop of
    the run was followed by more work, so it was not the human's turn. A stop no prompt ever closed is returned
    apart: how long the human took is unknown, so it is never counted.
    """
    out, last_stop = [], None
    for t, name in timed:
        if name == "stop":
            last_stop = t
        elif name == "prompt" and last_stop is not None:
            out.append((last_stop, t))
            last_stop = None
    return out, last_stop


def _waiting_in(waits, start, end):
    """Seconds of waiting inside [start, end] -- the closed pairs clipped to it. None when the end is unknown,
    when an unclosed stop falls inside the window, or when nothing measured waiting at all (`waits` None, R79):
    never guessed (spec §5.3)."""
    if waits is None:
        return None
    intervals, unclosed = waits
    if end is None or (unclosed is not None and start <= unclosed < end):
        return None
    total = 0
    for a, b in intervals:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


def _row(phase, start, end, waits):
    wall = int((end[0] - start[0]).total_seconds()) if end else None
    waiting = _waiting_in(waits, start[0], end[0] if end else None)
    return {"phase": phase, "start": start[1], "end": end[1] if end else None, "wall_s": wall,
            "waiting_s": waiting, "work_s": wall - waiting if wall is not None and waiting is not None else None}


def report(events):
    """[{phase, start, end, wall_s, waiting_s, work_s}] for the phases that have an opening stamp, in the fixed
    order. A phase opens at the FIRST event that opens it (a second `plan_written` does not reopen it) -- except
    delivery, which opens at the first commit/push after every earlier phase started (R77) -- and ends at the start
    of the next phase present, or at the first `delivered` after it started for the last one (B-M3). Without either -- or when that
    next start is EARLIER than this one (stamps out of order) -- the end is None, and so are wall, waiting and
    work. Waiting = the closed `stop` -> `prompt` pairs (from the LAST stop before each prompt) clipped to the
    phase; a `stop` no prompt closed makes that phase's waiting (and work) None (R45). Only the last phase
    present can end at `delivered`, so the total is derived from the rows by `total()`/`render`.
    """
    timed = sorted(((w, e.get("event"), e.get("t")) for e in events
                    if isinstance(e.get("event"), str) and (w := _when(e)) is not None),
                   key=lambda x: x[0])
    first = {}
    for w, name, raw in timed:
        first.setdefault(name, (w, raw))
    # R79: with no `prompt` and no `stop` at all, nothing watched the human's turns -- waiting is unknown, not 0.
    names = {name for _, name, _ in timed}
    waits = (_waiting_intervals([(w, name) for w, name, _ in timed])
             if names & {"prompt", "stop"} else None)
    opened = []
    for phase, openers in PHASES:
        if phase == "delivery":
            # R77: the first commit/push at or after the start of every earlier phase present; none -> no row
            after = max((s[0] for _, s in opened), default=None)
            stamps = [(w, raw) for w, name, raw in timed if name in DELIVERY_OPENERS and (after is None or w >= after)]
        else:
            stamps = [first[o] for o in openers if o in first]
        if stamps:
            opened.append((phase, min(stamps, key=lambda s: s[0])))
    # B-M3: the `delivered` that closes the table is the first one at or after the start of the LAST phase present
    # -- an earlier stray one would leave the table open (or end a phase before it started).
    last_start = opened[-1][1][0] if opened else None
    delivered = next(((w, raw) for w, name, raw in timed
                      if name == END and (last_start is None or w >= last_start)), None)
    rows = []
    for i, (phase, start) in enumerate(opened):
        end = opened[i + 1][1] if i + 1 < len(opened) else delivered
        if end is not None and end[0] < start[0]:
            end = None                       # out of order: unknown, never a negative wall
        rows.append(_row(phase, start, end, waits))
    return rows


def total(rows):
    """(start, end, wall_s, waiting_s, work_s) from the earliest phase start until `delivered` -- the end of the
    last row, the only one `delivered` closes. None when there is no `delivered` (or it precedes a start).
    Waiting is the sum of the phases' -- they tile the span -- and None if any of them is unknown."""
    if not rows or rows[-1]["end"] is None:
        return None
    starts = [(_when({"t": r["start"]}), r["start"]) for r in rows]
    start = min(starts, key=lambda s: s[0])
    end = _when({"t": rows[-1]["end"]})
    if end < start[0]:
        return None
    wall = int((end - start[0]).total_seconds())
    waits = [r["waiting_s"] for r in rows]
    waiting = None if any(w is None for w in waits) else sum(waits)
    return start[1], rows[-1]["end"], wall, waiting, (wall - waiting if waiting is not None else None)


def duration(seconds):
    """8400 -> `2h 20m`; 1200 -> `20m`; under a minute -> `Ns`; None -> `—`."""
    if seconds is None:
        return DASH
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _stamp(raw):
    return raw[:16].replace("T", " ") if raw else DASH


def _t(key, lang, **kwargs):
    """i18n.t in an explicit language: i18n reads the language from the repo config or PLAN_GATE_LANG, and
    with no repo the variable is what decides -- set for the call, then restored."""
    import i18n
    lang = lang if lang in ("en", "pt-BR") else "en"
    old = os.environ.get("PLAN_GATE_LANG")
    os.environ["PLAN_GATE_LANG"] = lang
    try:
        return i18n.t(key, None, **kwargs)
    finally:
        if old is None:
            os.environ.pop("PLAN_GATE_LANG", None)
        else:
            os.environ["PLAN_GATE_LANG"] = old


def render(rows, lang):
    """A Markdown table. None -> `—`. The last line is always the total until `delivered` (or `—`)."""
    cols = ("phase", "start", "end", "wall", "waiting", "work")
    lines = ["| " + " | ".join(_t("timeline.col." + c, lang) for c in cols) + " |",
             "|" + "---|" * len(cols)]
    for r in rows:
        lines.append(f"| {_t('timeline.phase.' + r['phase'], lang)} | {_stamp(r['start'])} | {_stamp(r['end'])} | "
                     f"{duration(r['wall_s'])} | {duration(r['waiting_s'])} | {duration(r['work_s'])} |")
    label = f"**{_t('timeline.total', lang)}**"
    whole = total(rows)
    if whole:
        start, end, wall, waiting, work = whole
        lines.append(f"| {label} | {_stamp(start)} | {_stamp(end)} | **{duration(wall)}** | {duration(waiting)} | "
                     f"{duration(work)} |")
    else:
        lines.append(f"| {label} | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} |")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------------- hooks

def _resolve(base, target):
    return os.path.normpath(os.path.join(base, os.path.expanduser(target)))


def git_targets(command, cwd):
    """[(verb, directory)] for every `git commit` / `git push` in `command`, in order.

    R47: the directory follows the command -- `cd <dir> && git …` and `git -C <dir> …` (several `-C` compose,
    as in git), each resolved relative to the hook's cwd. Quoted text is an argument, never a command
    (`echo "git commit"` is not a commit). A command shlex cannot split yields nothing: no stamp, never a guess.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []
    out, here, simple = [], cwd, []
    for tok in tokens + ["\n"]:
        if tok not in _SEPARATORS:
            simple.append(tok)
            continue
        if simple and simple[0] == "cd":
            here = _resolve(here, simple[1]) if len(simple) > 1 else os.path.expanduser("~")
        elif simple and simple[0] == "git":
            where, i = here, 1
            while i < len(simple) and simple[i].startswith("-"):
                opt = simple[i]
                if opt == "-C" and i + 1 < len(simple):
                    where = _resolve(where, simple[i + 1])
                    i += 2
                elif opt in _GIT_OPTS_WITH_VALUE:
                    i += 2
                else:
                    i += 1
            if i < len(simple) and simple[i] in ("commit", "push"):
                out.append((simple[i], where))
        simple = []
    return out


def _commit(repo):
    """R46: a commit is stamped only with a baseline (the demand's last event has a `head`), a HEAD that moved
    from it, AND a reflog saying the move was a commit (`commit:`, `commit (amend):`, `commit (initial):`).
    A checkout, a reset or a failed commit is none of these. No baseline -> nothing: never a guess."""
    past = events(repo, demand(repo))
    last = past[-1].get("head") if past else None
    if not last:
        return
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    if not head or head == last:
        return
    if not (_git(repo, "reflog", "-1", "--format=%gs") or "").startswith("commit"):
        return
    name = "commit" if any(e.get("event") == "first_commit" for e in past) else "first_commit"
    append(repo, name, "hook")


def _push(repo):
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    upstream = _git(repo, "rev-parse", "--verify", "-q", "@{u}")
    if head and upstream == head:
        append(repo, "push", "hook")


def _bash(cwd, entry):
    command = (entry.get("tool_input") or {}).get("command") or ""
    if not isinstance(command, str):
        return
    seen = set()
    for verb, where in git_targets(command, cwd):
        repo = repo_root(where)
        if repo is None or (verb, repo) in seen:
            continue
        seen.add((verb, repo))
        if verb == "commit":
            _commit(repo)
        else:
            _push(repo)


def hook(kind, raw):
    entry = json.loads(raw or "{}")
    if not isinstance(entry, dict):
        raise ValueError(f"hook input is not an object: {type(entry).__name__}")
    cwd = entry.get("cwd") or os.getcwd()
    if kind == "bash":
        _bash(cwd, entry)          # the repo comes from each git command (R47), not only from the cwd
        return
    if kind not in ("prompt", "stop"):
        raise ValueError(f"unknown hook kind: {kind!r}")
    repo = repo_root(cwd)
    if repo is not None:
        append(repo, kind, "hook")


def _log(where, error):
    try:
        try:
            folder = state_dir()
        except Exception:  # noqa: BLE001 -- config.py itself is broken: log where it would have pointed
            folder = _fallback_state_dir()
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{_now()} timeline {where}: {error}\n")
    except Exception:  # noqa: BLE001 -- the log is the last resort; it cannot raise either
        pass


def main(argv):
    if len(argv) >= 1 and argv[0] == "hook":
        kind = argv[1] if len(argv) > 1 else ""
        # R42: nothing reaches stdout or stderr, whatever any callee prints; what it printed goes to the log.
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                hook(kind, sys.stdin.read())
        except BaseException as error:      # noqa: BLE001 -- a hook of every prompt can never block one
            _log(f"hook {kind}", repr(error))
        if sink.getvalue():
            _log(f"hook {kind} output", sink.getvalue().strip())
        return 0
    print("usage: timeline.py hook prompt|stop|bash", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
