#!/usr/bin/env python3
"""The review loop's pure parts: build the prompt, validate, force the verdict, render the .md.

What belongs to EACH backend (how to run, how to talk to the model) lives in the backends; this module
runs nothing by itself: `run_round` drives a backend through the fixed contract below.
"""
import contextlib, dataclasses, datetime, fcntl, glob, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import i18n, state  # noqa: E402

ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(ROOT, "schema", "review.schema.json")
PROMPTS = os.path.join(ROOT, "prompts")

MAX_ROUNDS = 3
ROUND_TIMEOUT_S = 900           # 15 min, absolute deadline
NO_BYTE_S = 90                  # hang at startup (opencode#35870)
DIFF_LIMIT = 20_000             # chars

LANGUAGE_NAMES = {"en": "English", "pt-BR": "Brazilian Portuguese"}


def _prompt(name):
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as f:
        return f.read()


def honest_diff(previous, current, limit=DIFF_LIMIT):
    """Diff cut at a HUNK boundary, declaring what was left out.

    A raw cut at `d[:20000]` split the diff in the middle of a hunk and ended on orphan `-` lines, with no
    marker. The reviewer had no way to know the diff was incomplete -- and would reread the plan with `sed`,
    paying for the diff AND the reading. Silent truncation read as completeness (measured on 2026-08-25).
    """
    proc = subprocess.run(["diff", "-u", previous, current], capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        # `diff` exits 2 when a file is unreadable/missing: that is NOT "the plan did not change".
        return "(could not diff against the previous round's snapshot: treat the whole plan as new)"
    d = proc.stdout
    if not d.strip():
        return "(the plan did NOT change since the previous round)"

    lines = d.splitlines(keepends=True)
    i = next((k for k, l in enumerate(lines) if l.startswith("@@")), len(lines))
    header = "".join(lines[:i])

    hunks, current_hunk = [], None
    for l in lines[i:]:
        if l.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = [l]
        elif current_hunk is not None:
            current_hunk.append(l)
    if current_hunk:
        hunks.append(current_hunk)

    out, spent, included = [header], len(header), 0
    for h in hunks:
        text = "".join(h)
        # Always delivers at least one whole hunk, even if it overshoots the limit:
        # half a hunk is not information, it is noise.
        if included and spent + len(text) > limit:
            break
        out.append(text)
        spent += len(text)
        included += 1

    if included == len(hunks):
        return "".join(out)

    ranges = []
    for h in hunks[included:]:
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", h[0])
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count == 0:  # pure deletion: nothing of the hunk exists in the current plan
                ranges.append(f"deleted before line {start + 1}")
            else:
                ranges.append(f"{start}-{start + count - 1}")
    warning = (
        f"\n\n⚠️ **INCOMPLETE DIFF — {included} of {len(hunks)} hunks above.**\n"
        f"The {len(hunks) - included} omitted hunks touch these lines of the CURRENT plan: "
        f"{', '.join(ranges) or '(range not determined)'}.\n"
        f"Those ranges CHANGED and are not here: read them in the plan before concluding that something "
        f"was not handled. Do not assume that what is missing is the same as what came."
    )
    return "".join(out) + warning


def find_spec(plan, repo, plans_dir, specs_dir):
    """Superpowers convention: <specs_dir>/<same-name>-design.md, inside the repo.

    Not finding it, returns `None` and the round runs with "(no spec)" -- visible in the status line and in
    the review's `.md`. ⚠️ The caller does NOT abort.

    ⚠️ Never returns the plan ITSELF. Pointing the reviewer at a spec made the by-date fallback match that
    very file: a single candidate, a review against itself, in silence. (`plans_dir` is accepted so callers
    pass the whole layout from the settings; the lookup only needs the specs folder.)
    """
    base = os.path.basename(plan)
    base = base[:-3] if base.endswith(".md") else base
    specs = os.path.join(repo, specs_dir)
    if not os.path.isdir(specs):
        return None
    exact = os.path.join(specs, base + "-design.md")
    if os.path.isfile(exact):
        found = exact
    else:
        # fallback: same date, similar topic
        date = "-".join(base.split("-")[:3])
        candidates = sorted(glob.glob(os.path.join(specs, date + "*-design.md")))
        found = candidates[0] if len(candidates) == 1 else None
    if found and os.path.realpath(found) == os.path.realpath(plan):
        return None
    return found


def round_number(path):
    """The N of `...-round-N.md` (or `.json`), or 0 when the name does not match the pattern."""
    m = re.search(r"-round-(\d+)\.\w+$", path)
    return int(m.group(1)) if m else 0


def previous_rounds(rev_dir, base, namespace):
    """Previous reviews of THIS namespace, ordered by N.

    ⚠️ Namespace per TOOL: `<base>-<namespace>-round-N.md`. One tool's rounds do NOT count for another --
    otherwise round 1 of one reads round 3 of the other as its own, and "what changed since your last round"
    becomes fiction. The mtime tie-break on N is only a deterministic order (a namespace has one file per N).
    """
    found = [p for p in glob.glob(os.path.join(rev_dir, f"{base}-{namespace}-round-*.md"))
             if round_number(p)]
    return sorted(found, key=lambda p: (round_number(p), os.path.getmtime(p)))


class PreviousBlockersUnreadable(Exception):
    """The previous round's blockers could not be read, not even with `tolerant_json`.

    ⚠️ `build_prompt` reads the JSON of the previous round to list the open blockers in the incremental round.
    Swallowing a failure there (an empty list) would make round N see "(none)" open and the reviewer would stop
    rechecking what was already pointed out -- a real blocker would vanish between rounds, silently. A fail-open
    here erases exactly the information that blocks.

    CHOICE: refuse the round (raise this, which the runner turns into NO VERDICT -- exit 3, verdicts untouched)
    instead of continuing with an empty list and a warning. An incremental round that does not know what was open
    is not incremental: it is a round 1 posing as round N.
    """


def build_prompt(path, spec, repo, n, previous, snap_dir, preflight, lang, as_spec=False):
    """The prompt of round `n`. Incremental from the 2nd round, with an honest diff of the previous one.

    `previous` are the earlier rounds' `.md` paths (oldest first); `preflight` is the ready preflight text, or
    None when it did not run. `as_spec` reviews a SPEC against the repo (full contract every round).

    ⚠️ Raises `PreviousBlockersUnreadable` when the previous round's `.json` is missing, unreadable even with
    `tolerant_json`, or fails `validate` -- see the exception's docstring for why.
    """
    rel_path = os.path.relpath(path, repo)
    rel_spec = os.path.relpath(spec, repo) if spec else "(no spec)"

    if as_spec:
        prompt = _prompt("contract-spec.md").format(plan=rel_path, spec=rel_spec, n=n)
    elif not previous:
        prompt = _prompt("contract.md").format(plan=rel_path, spec=rel_spec)
    else:
        previous_md = previous[-1]
        prev_n = int(re.search(r"-round-(\d+)\.md$", previous_md).group(1))
        prev_json = previous_md[:-3] + ".json"
        snap = os.path.join(snap_dir, os.path.basename(previous_md))
        diff = honest_diff(snap, path) if os.path.isfile(snap) else \
            "(no snapshot of the previous round: treat the whole plan as new)"
        # ⚠️ The open blockers come from the previous round's JSON, not from the .md: the .md is for people,
        # and re-parsing text to find a blocker is like reading an HTTP status by substring.
        # ⚠️ The .md exists, so the round happened: its JSON MUST be there, readable AND valid. A missing or
        # shapeless one (`{"verdict": "REJECTED"}` with no findings) read as "(none)" open blockers -- a real
        # blocker vanished between rounds (final review S3).
        try:
            with open(prev_json, encoding="utf-8") as f:
                data = tolerant_json(f.read())
            problem = validate(data)
            if problem:
                raise ValueError(problem)
            previous_blockers = [a for a in data["findings"] if a["severity"] == "BLOCKER"]
        except (OSError, ValueError) as e:
            # ⚠️ The refusal writes neither .md nor .json, so `previous_rounds()` keeps seeing the same set
            # and the next attempt rereads the SAME file: the loop is stable and never leaves on its own. A
            # defense that traps without saying the way out is a new way to lose the round -- hence the message
            # carries `--redo`.
            raise PreviousBlockersUnreadable(
                f"blockers of round {prev_n} unreadable even with tolerant_json "
                f"({os.path.relpath(prev_json, repo)}): {e}\n"
                f"This repeats on every attempt until round {prev_n} is rewritten: "
                f"run again with `--redo` to redo round {prev_n}.") from e
        blockers_text = "\n\n".join(
            f"### {a['where']}\n{a['problem']}" for a in previous_blockers) or "(none)"
        prompt = _prompt("contract-incremental.md").format(plan=rel_path, spec=rel_spec, n=n)
        prompt += _prompt("previous-round.md").format(
            prev=prev_n, n_blockers=len(previous_blockers), blockers=blockers_text, diff=diff,
            prev_json=os.path.relpath(prev_json, repo))

    # ⚠️ The preflight enters AFTER the .format: its symbol table has `{}` braces that format would try to
    # substitute.
    if preflight:
        prompt += preflight
    elif not as_spec:   # a spec has no plan preflight: saying "it did NOT run" there is noise
        prompt += ("\n\n## Preflight\n\nNote: the preflight did NOT run: do the mechanical checks yourself.\n")
    prompt += (f"\n\nWrite every prose field (summary, problem, why_it_matters, fix, detail) in "
               f"{LANGUAGE_NAMES.get(lang, 'English')}. Keep enum values (APPROVED, BLOCKER, PASSED, ...) "
               f"exactly as the schema says.\n")
    return prompt


_VALID_ESCAPE = set('"\\/bfnrtu')  # JSON's escape alphabet (includes the u of \uXXXX)


def _fix_backslash_run(m):
    """Repairs ONE whole run of backslashes (and the character right after it) at once.

    ⚠️ Fixing backslash BY backslash, by position, doubled the right backslash of an ALREADY valid pair
    (`\\\\`, the escape of a literal backslash) whenever it was not followed by an escape-alphabet character,
    producing three backslashes (invalid again): `C:\\\\Users` (valid pair) in the SAME document as `C:\\Wrong`
    (stray backslash) became `C:\\\\\\Users` just because of the `U` that follows.

    The right rule treats the whole run: backslashes are consumed IN PAIRS (each pair is already a valid `\\\\`
    escape, never touched); if one is left over (odd count), only IT is in doubt, and only then does it matter
    whether the next character belongs to the JSON escape alphabet. If it does not, only that leftover
    backslash is doubled.
    """
    slashes, c = m.group(1), m.group(2) or ""
    pairs, leftover = divmod(len(slashes), 2)
    out = "\\\\" * pairs
    if not leftover:
        return out + c
    if c in _VALID_ESCAPE:
        return out + "\\" + c     # the leftover closes a valid escape -- leave it
    return out + "\\\\" + c       # the leftover is invalid -- that one, and only that one, is doubled


def tolerant_json(raw):
    """`json.loads`, and a single retry fixing an invalid escape.

    ⚠️ The retry recovered a whole file on 2026-09-18 -- 13.6 KB, verdict APPROVED, 3 real findings and 12
    mechanical checks -- that was about to become NO VERDICT because of one stray backslash. Losing a round of
    10min40s over that is too expensive not to try.

    ⚠️ SCOPED tolerance: invalid escape only. Truncated JSON still blows up -- a half verdict that "passes" is
    worse than a lost round. `_fix_backslash_run` only touches the backslash left over from an odd-length run,
    and only when the next character is NOT in the JSON escape alphabet (`"\\/bfnrtu`, including the `u` of
    `\\uXXXX` -- incomplete digits of `\\uXXXX` stay out of scope: the letter `u` is already in the alphabet, so
    the function never touches it, and guessing the missing digits would invent content the reviewer never
    wrote). It never touches a key, a closed string or structure, so it cannot turn truncated JSON into valid.
    """
    try:
        return json.loads(raw)
    except ValueError as original:
        fixed = re.sub(r'(\\+)(.)?', _fix_backslash_run, raw, flags=re.DOTALL)
        try:
            data = json.loads(fixed)
        except ValueError:
            # If the retry fails too, whoever debugs wants the error of the ORIGINAL text -- the column and
            # context of the text already rewritten by the regex may not match the writer's problem.
            raise original from None
        # Silence hides that the reviewer is producing invalid JSON -- that is information that matters.
        print("  JSON with an invalid escape; fixed automatically (regex retry)", file=sys.stderr)
        return data


FINDING_PROSE = ("why_it_matters", "evidence", "fix")
CHECK_FIELDS = ("check", "command", "result", "detail")


def validate(data):
    """None if the JSON is good; otherwise the message of what is missing.

    ⚠️ Not schema validation for elegance: it separates "the model answered" from "the model answered what we
    asked". Without it, a JSON with `verdict` missing becomes `data.get("verdict")` == None, which is not
    "REJECTED" -- and the gate opens. ⚠️ And every field `render_md` prints has the type it prints: a number where
    prose goes, or a `mechanical_checks` that is not a list of objects, was a traceback (exit 1) instead of the
    repair / NO VERDICT path (final review I4).
    """
    if not isinstance(data, dict):
        return f"the reviewer returned {type(data).__name__}, not an object"
    if data.get("verdict") not in ("APPROVED", "REJECTED"):
        return f"verdict missing or invalid: {data.get('verdict')!r}"
    if not isinstance(data.get("summary"), str):
        return "field `summary` missing or not a string"
    findings = data.get("findings")
    if not isinstance(findings, list):
        return "field `findings` missing or not a list"
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            return f"finding {i} is not an object"
        missing = [c for c in ("severity", "where", "problem") if not f.get(c)]
        if missing:
            return f"finding {i} without {', '.join(missing)}"
        if f["severity"] not in ("BLOCKER", "RISK", "NOTE"):
            return f"finding {i} with invalid severity: {f['severity']!r}"
        wrong = [c for c in ("where", "problem") + FINDING_PROSE if c in f and not isinstance(f[c], str)]
        if wrong:
            return f"finding {i}: {', '.join(wrong)} must be text"
    checks = data.get("mechanical_checks", [])
    if not isinstance(checks, list):
        return "`mechanical_checks` is not a list"
    for i, c in enumerate(checks):
        if not isinstance(c, dict):
            return f"mechanical check {i} is not an object"
        wrong = [k for k in CHECK_FIELDS if k in c and not isinstance(c[k], str)]
        if wrong:
            return f"mechanical check {i}: {', '.join(wrong)} must be text"
    promises = data.get("spec_promises_without_task", [])
    if not isinstance(promises, list):
        return "`spec_promises_without_task` is not a list"
    if not all(isinstance(p, str) for p in promises):
        return "`spec_promises_without_task` must be a list of text"
    return None


def coerce_verdict(data):
    """Forces REJECTED when there is a BLOCKER or a promise without a task. Returns the blockers.

    ⚠️ The model has already written "APPROVED" with an open blocker in the list -- so the decision is not
    its own. A spec promise without a task is a blocker by definition: it is a requirement nobody will
    implement, and no task test catches it, because the test comes out of the plan.
    """
    blockers = [f for f in data.get("findings", []) if f.get("severity") == "BLOCKER"]
    promises = data.get("spec_promises_without_task") or []
    if blockers or promises:
        data["verdict"] = "REJECTED"
    return blockers


def render_md(data, *, path, spec, repo, namespace, n, when, wall_s, telemetry, lang, as_spec=False):
    """The `.md` a person reads. The JSON is for the program; this one is for whoever decides.

    ⚠️ The title names the TOOL (`namespace`), and the model line says "model requested": the system only
    knows which model was ASKED for, never which one answered.
    """
    def T(key, **kw):
        return i18n.t(key, lang, **kw)

    findings = data.get("findings", [])
    blockers = [a for a in findings if a.get("severity") == "BLOCKER"]
    risks = [a for a in findings if a.get("severity") == "RISK"]
    notes = [a for a in findings if a.get("severity") == "NOTE"]
    promises = data.get("spec_promises_without_task") or []
    tel = telemetry or {}

    lines = [
        T("review.title_spec" if as_spec else "review.title", n=n, namespace=namespace),
        "",
        T("review.verdict", value=data.get("verdict")),
        T("review.plan", value=os.path.relpath(path, repo)),
        T("review.spec", value=os.path.relpath(spec, repo) if spec else T("review.no_spec")),
        T("review.when", when=when, wall_s=wall_s),
        T("review.reviewer", namespace=namespace,
          model=tel.get("model_requested") or T("review.not_declared")),
        T("review.route", value=tel.get("route") or T("review.route_not_declared")),
    ]
    shown = []
    for k, v in sorted((telemetry or {}).items()):
        if k == "model":
            # the model the STREAM reported (the one that answered), never to be read as the one requested -- and
            # nothing at all when the stream named none (opencode 1.18 never does)
            if v:
                shown.append(T("review.model_reported", value=v))
            continue
        shown.append(f"{k}={v}")
    if shown:
        lines.append(T("review.telemetry", value=", ".join(shown)))
    lines += ["", T("review.summary", value=data.get("summary", T("review.no_summary"))), ""]

    for title_key, items in (("review.blockers", blockers), ("review.risks", risks), ("review.notes", notes)):
        if not items:
            continue
        lines.append(T(title_key, count=len(items)))
        lines.append("")
        for a in items:
            lines.append(f"### {a['where']}")
            lines.append("")
            lines.append(a["problem"])
            for field, label_key in (("why_it_matters", "review.why"), ("evidence", "review.evidence"),
                                     ("fix", "review.fix")):
                if a.get(field):
                    lines.append("")
                    lines.append(f"**{T(label_key)}:** {a[field]}")
            lines.append("")
    if promises:
        lines += [T("review.promises", count=len(promises)), ""]
        lines += [f"- {p}" for p in promises] + [""]
    if checks := data.get("mechanical_checks"):
        lines += [T("review.checks"), ""]
        for c in checks:
            lines.append(f"- `{c.get('command', '?')}` → **{c.get('result', '?')}** "
                         f"{c.get('detail', '')}".rstrip())
        lines.append("")
    return "\n".join(lines)


# ---- the round ------------------------------------------------------------------------------------------------

BACKEND_ATTRIBUTES = ("CAN_REPAIR", "CAN_MODEL", "review", "route")


@dataclasses.dataclass
class Options:
    thinking: bool = False
    redo: bool = False
    prompt_only: bool = False
    timeout_s: int = ROUND_TIMEOUT_S
    no_byte_s: int = NO_BYTE_S
    as_spec: bool = False
    spec: str | None = None


@dataclasses.dataclass
class Result:
    """What a backend answers. `telemetry` lands VERBATIM in the review's .md: never a token, never a URL that
    carries a credential."""
    ok: bool
    reason: str = ""
    session: str = ""
    telemetry: dict = dataclasses.field(default_factory=dict)
    # False only when a failed call says a repeat cannot help (the provider refused, non-retryable): no repair turn
    repairable: bool = True


class Refused(Exception):
    """Refused before spending anything (exit 2). `key` is a locale key; `kwargs` fill its placeholders."""

    def __init__(self, key, **kwargs):
        super().__init__(key)
        self.key, self.kwargs = key, kwargs


def backend_problem(module):
    """Why `module` does not honour the backend contract, or None.

    The contract (Tasks 11-13 implement it):
      review(*, path, repo, prompt, output_json, settings, options, deadline) -> Result
      repair(*, path, repo, session, reason, output_json, settings, options, deadline) -> Result   (CAN_REPAIR only)
      route(settings) -> str
    """
    for name in BACKEND_ATTRIBUTES:
        if not hasattr(module, name):
            return f"missing {name}"
    for name in ("review", "route"):
        if not callable(getattr(module, name)):
            return f"{name} is not callable"
    if module.CAN_REPAIR and not callable(getattr(module, "repair", None)):
        return "CAN_REPAIR is set but there is no callable repair"
    return None


class StateFailed(Exception):
    """The review's state folder could not be read or written. Nothing was recorded: the round ends as NO VERDICT
    (exit 3), with the verdicts exactly as they were."""

    def __init__(self, error):
        super().__init__(str(error))
        self.error = error
        self.folder = state.state_dir()


@contextlib.contextmanager
def _state_io():
    """Every touch of the state folder (verdicts, last_failure, snapshots) goes through here: an OSError there --
    StateUnreadable included -- becomes StateFailed, which review.py reports without a traceback."""
    try:
        yield
    except OSError as e:
        raise StateFailed(e) from e


STALE_SUFFIX = ".stale"


def symlink_in(folder):
    """The first symlink at or under `folder` (path relative to its parent), or None."""
    if os.path.islink(folder):
        return os.path.basename(folder)
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                return os.path.relpath(os.path.join(root, name), os.path.dirname(folder))
    return None


def reviews_folder_problem(rev_dir, repo):
    """Why the round must not write in `rev_dir`, or None.

    ⚠️ Every backend writes there (the round itself writes the .md/.json, opencode's reviewer writes its JSON): a
    committed `reviews/<plan>-codex-round-1.md -> ~/.bashrc` was overwritten by a `--redo`. A symlink anywhere at or
    under the folder, or a folder that resolves outside the repo, refuses the round before anything is written.
    """
    link = symlink_in(rev_dir)
    if link:
        return (f"refusing to run: the reviews folder contains a symlink ({link}); the round writes there and would "
                f"write through it. Remove it and run again.")
    real_repo, real = os.path.realpath(repo), os.path.realpath(rev_dir)
    if real != real_repo and not real.startswith(real_repo.rstrip(os.sep) + os.sep):
        return f"refusing to run: the reviews folder resolves outside the repository ({real})"
    return None


def write_new(path, data):
    """Write `data` (bytes) to `path` WITHOUT following a symlink there: a link in its place is an OSError (ELOOP),
    never a write through it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _lock_round(rev_dir, base, namespace):
    """The exclusive, NON-blocking lock of one plan+namespace: an open file whose close releases it. Raises
    BlockingIOError when another round holds it. `.<base>-<namespace>.lock` is a dotfile and not `*.md`, so
    `previous_rounds` never sees it."""
    os.makedirs(rev_dir, exist_ok=True)
    fd = os.open(os.path.join(rev_dir, f".{base}-{namespace}.lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    lock = os.fdopen(fd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


def _recover_set_aside(rev_dir, base, namespace):
    """Put back every round JSON a previous attempt set aside and never restored (killed mid-round: SIGKILL, power).

    ⚠️ It runs before the previous rounds are read: otherwise round N+1 would find round N's JSON missing and list
    "(none)" open blockers. When both files exist, the set-aside one wins: it is the last answer known to be whole,
    and the worst case (a crash between the new JSON and the cleanup) re-lists OLD blockers -- never loses them.
    """
    for stale in glob.glob(os.path.join(rev_dir, f"{base}-{namespace}-round-*.json{STALE_SUFFIX}")):
        os.replace(stale, stale[:-len(STALE_SUFFIX)])


@contextlib.contextmanager
def _set_aside(output_json):
    """A JSON already at output_json (the round being redone, or one a failed attempt left) must never be read as
    THIS round's answer -- a backend that says ok without writing would re-record an old verdict for a hash nobody
    reviewed. It moves to `.stale` and comes back on EVERY exit that did not commit a new answer: NO VERDICT, an
    exception, a Ctrl-C. `box["committed"] = True` once the new JSON is written."""
    stale = output_json + STALE_SUFFIX
    if os.path.lexists(output_json):
        os.replace(output_json, stale)
    box = {"committed": False}
    try:
        yield box
    finally:
        if not os.path.lexists(stale):
            pass
        elif box["committed"]:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stale)
        else:
            os.replace(stale, output_json)


CHANGED_DURING_ROUND = "the plan changed during the round; review again"


def file_identity(path):
    """(inode, size, mtime_ns, ctime_ns) of `path`, or None when it cannot be stat'ed.

    ⚠️ Comparing BYTES at the end is not enough: a plan edited during the round and restored to the exact bytes
    hashes the same, yet the reviewer may have read the edited text. Any metadata change means "not the text that
    was hashed" (final review S4). Limit: an in-place edit and restore inside one timestamp tick is invisible.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def secret_forms(secret):
    """Every spelling of `secret` that can reach a text: raw, and JSON-escaped (a body quoted inside a JSON string)."""
    if not secret:
        return []
    return sorted({secret, json.dumps(secret)[1:-1]}, key=len, reverse=True)


def mask_secret(value, secret):
    """`value` with every form of `secret` masked, recursively (dict keys too). Mask BEFORE any cut: a cut can split
    the secret, and the halves no longer match."""
    forms = secret_forms(secret)
    if not forms:
        return value
    if isinstance(value, str):
        for form in forms:
            value = value.replace(form, "***")
        return value
    if isinstance(value, dict):
        return {mask_secret(k, secret): mask_secret(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_secret(v, secret) for v in value]
    return value


def tracked_links_outside(repo):
    """Why the reviewer must not read this repo (a tracked symlink resolving outside it), or None."""
    try:
        p = subprocess.run(["git", "-C", repo, "ls-files", "-s", "-z"], capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"could not list the repository's tracked files: {e}"
    if p.returncode != 0:
        return f"could not list the repository's tracked files: git exited {p.returncode}"
    real_repo = os.path.realpath(repo)
    for record in p.stdout.split(b"\0"):
        meta, _, name = record.partition(b"\t")
        if not meta.startswith(b"120000 "):
            continue
        rel = os.fsdecode(name)
        real = os.path.realpath(os.path.join(repo, rel))
        if real != real_repo and not real.startswith(real_repo.rstrip(os.sep) + os.sep):
            return (f"refusing to run: the tracked symlink {rel} resolves outside the repository ({real}); the "
                    f"reviewer would read it and send it to the provider. Remove it and run again.")
    return None


def _mask_file(path, secret):
    """Rewrite `path` with `secret` masked, when it holds the secret. Best effort: never raises."""
    if not secret:
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        masked = mask_secret(text, secret)
        if masked != text:
            write_new(path, masked.encode("utf-8"))
    except OSError:
        pass


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_output(output_json):
    """-> (data, error). The reviewer's own JSON, read with the scoped tolerance of `tolerant_json`."""
    if not os.path.isfile(output_json):
        return None, "the reviewer did not write the JSON"
    try:
        with open(output_json, encoding="utf-8") as f:
            data = tolerant_json(f.read())
    except (OSError, ValueError) as e:  # UnicodeDecodeError is a ValueError
        return None, f"invalid JSON: {str(e)[:120]}"
    return data, validate(data)


def run_round(path, backend, settings, options, *, gate_api, cfg=None):
    """ONE review round. Returns the exit code: 0 verdict recorded, 3 NO VERDICT. Raises `Refused` (exit 2) when it
    refuses before spending anything, and `StateFailed` (exit 3) when the state folder cannot be read or written.

    `cfg` is the repo config the caller already asked the gate for (review.py needs it for the language first); when
    None, it is asked here.

    The ORDER is part of the contract:
    ⚠️ The hash and the snapshot come from the SAME bytes, read BEFORE the backend runs (spec §3.1.4). The INTERNAL
    took the snapshot at the END, and could record as reviewed a text the reviewer never read.
    ⚠️ The deadline is ABSOLUTE and born here: `time.monotonic() + timeout_s`, inherited by the repair -- otherwise
    "15 min" becomes 15 min per attempt.
    ⚠️ A failure never approves: NO VERDICT records `last_failure` and leaves every verdict untouched.
    """
    # 1. the repo, from the gate (one reader of the repo config)
    if cfg is None:
        cfg = gate_api.repo_config(path)
    repo = cfg.get("repo")
    if not repo:
        raise Refused("review.not_in_git", path=path)
    lang = i18n.resolve_language(cfg.get("language"))

    # 2. bytes, hash, bytes again: the hash must describe exactly the bytes the snapshot will hold
    identity = file_identity(path)
    with open(path, "rb") as f:
        content = f.read()
    content_hash = gate_api.content_hash(path)
    with open(path, "rb") as f:
        if f.read() != content:
            raise Refused("review.changed_while_starting", path=path)

    # 3. where, and which round
    namespace = settings.backend + ("-spec" if options.as_spec else "")
    name = os.path.basename(path)
    base = name[:-3] if name.endswith(".md") else name
    rev_dir = os.path.join(os.path.dirname(path), "reviews")
    snap_dir = state.snapshots_dir(path)

    def which_round():
        previous = previous_rounds(rev_dir, base, namespace)
        # the number is the HIGHEST N used, not the file count (one tool's files never count for another)
        n = max((round_number(p) for p in previous), default=0) + 1
        if options.redo and previous:
            n = round_number(previous[-1])
            previous = previous[:-1]
        return previous, n

    started = time.time()
    previous, n = which_round()     # read-only: the number a refusal below is recorded under
    secret = getattr(settings, "token", "")

    def no_verdict(reason):
        """NO VERDICT: `last_failure` only (plans, and never for --prompt-only, which writes no state)."""
        reason = mask_secret(reason, secret)
        if not options.as_spec and not options.prompt_only:
            with _state_io():
                state.record_failure(path, {"round": n, "backend": settings.backend, "reason": reason,
                                            "when": _now()})
        print(i18n.t("review.no_verdict", lang, reason=reason), file=sys.stderr)
        print(json.dumps({"verdict": "NO VERDICT", "round": n, "backend": settings.backend, "reason": reason,
                          "wall_s": int(time.time() - started)}, ensure_ascii=False, indent=2))
        return 3

    # 3b. never write through a link: checked BEFORE the set-aside recovery or any makedirs touches the folder
    problem = reviews_folder_problem(rev_dir, repo)
    if problem:
        return no_verdict(problem)
    # 3c. ONE round of this plan and namespace at a time: two rounds would set aside and restore each other's JSON
    # (final review I3). Held from the recovery through the artifacts and the verdict.
    try:
        lock = None if options.prompt_only and not os.path.isdir(rev_dir) else _lock_round(rev_dir, base, namespace)
    except BlockingIOError:
        raise Refused("review.round_running", path=os.path.relpath(path, repo))
    except OSError as e:
        return no_verdict(f"could not prepare the reviews folder: {e}")

    def locked():
        nonlocal previous, n
        try:
            _recover_set_aside(rev_dir, base, namespace)
        except OSError as e:
            return no_verdict(f"could not restore a set-aside review JSON: {e}")
        previous, n = which_round()

        # 4. the prompt -- a previous round whose blockers cannot be read refuses BEFORE waking the backend
        spec = options.spec or (None if options.as_spec else
                                find_spec(path, repo, cfg["plans_dir"], cfg["specs_dir"]))
        preflight = None if options.as_spec else gate_api.preflight_prompt(path, incremental=bool(previous))
        try:
            prompt = build_prompt(path, spec, repo, n, previous, snap_dir, preflight, lang, as_spec=options.as_spec)
        except PreviousBlockersUnreadable as e:
            return no_verdict(str(e))

        # 5.
        if options.prompt_only:
            print(prompt)
            return 0

        # 5b. what the reviewer may read: a tracked symlink that resolves outside the repo hands it a file the repo
        # chose (~/.ssh/id_rsa, a token file), which then goes to the provider
        problem = tracked_links_outside(repo)
        if problem:
            return no_verdict(problem)

        # 6. a model the backend never receives is a FALSE LABEL: warn, and never declare it
        if settings.model and not backend.CAN_MODEL:
            print(i18n.t("review.model_ignored", lang, backend=settings.backend, model=settings.model),
                  file=sys.stderr)

        # 7. ABSOLUTE deadline
        deadline = time.monotonic() + options.timeout_s
        try:
            os.makedirs(rev_dir, exist_ok=True)
        except OSError as e:
            return no_verdict(f"could not prepare the reviews folder: {e}")
        output_json = os.path.join(rev_dir, f"{base}-{namespace}-round-{n}.json")
        output_md = output_json[:-5] + ".md"
        print(i18n.t("review.progress", lang, n=n, path=os.path.relpath(path, repo), namespace=namespace,
                     spec=os.path.relpath(spec, repo) if spec else i18n.t("review.no_spec", lang)), file=sys.stderr)

        try:
            with _set_aside(output_json) as aside:
                # 8. review, and at most ONE repair turn
                r = backend.review(path=path, repo=repo, prompt=prompt, output_json=output_json, settings=settings,
                                   options=options, deadline=deadline)
                data, error = _read_output(output_json) if r.ok else (None, r.reason or "the backend failed")
                slack = min(30, options.timeout_s // 4)
                # ⚠️ Three cases. (1) The reviewer ANSWERED and the JSON is unusable: repair. (2) The backend failed
                # but may recover (a stall after output): repair, as before. (3) The backend failed with
                # `repairable=False` (the provider refused, e.g. a 403 with isRetryable false): repeating the call
                # only spends quota and takes the same error, so the round ends NO VERDICT with the ORIGINAL reason.
                if error and (r.ok or r.repairable) and backend.CAN_REPAIR and r.session and deadline - time.monotonic() > slack:
                    print(i18n.t("review.repairing", lang, error=error), file=sys.stderr)
                    r2 = backend.repair(path=path, repo=repo, session=r.session, reason=error,
                                        output_json=output_json, settings=settings, options=options,
                                        deadline=deadline)
                    if r2.ok:
                        data, error = _read_output(output_json)
                        # a repair reply without telemetry must not erase what the original call declared
                        r2.telemetry = {**r.telemetry, **r2.telemetry}
                        r = r2
                    else:
                        error = f"{error}; the repair failed too: {r2.reason or 'no reason given'}"

                # 9. (leaving the `with` without committing puts the set-aside JSON back)
                if error:
                    _mask_file(output_json, secret)     # a raw answer left behind (nothing to restore): no token
                    return no_verdict(error)

                # ⚠️ The model's own text can carry the token (a provider's auth error quoted back, a prompt injection
                # that asked for it): masked in the data BEFORE anything is written from it (final review I1).
                masked = mask_secret(data, secret)
                if masked != data:
                    data = masked
                    data["findings"].append({"severity": "NOTE", "where": "review output",
                                             "problem": "the token appeared in the reviewer output and was masked"})

                # 10. force the verdict, declare, write the artifacts -- the snapshot holds the bytes of step 2
                if file_identity(path) != identity:
                    return no_verdict(CHANGED_DURING_ROUND)
                blockers = coerce_verdict(data)
                when = _now()
                wall_s = int(time.time() - started)
                route = backend.route(settings)
                model_requested = settings.model if backend.CAN_MODEL else ""
                telemetry = mask_secret(dict(r.telemetry), secret)
                telemetry["route"] = route
                if model_requested:
                    telemetry["model_requested"] = model_requested
                md = render_md(data, path=path, spec=spec, repo=repo, namespace=namespace, n=n, when=when,
                               wall_s=wall_s, telemetry=telemetry, lang=lang, as_spec=options.as_spec)
                try:
                    # the .json FIRST: a lone .json is invisible to `previous_rounds` (it globs *.md), while a lone
                    # new .md next to a missing JSON would refuse every later round
                    write_new(output_json, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
                    write_new(output_md, md.encode("utf-8"))
                except OSError as e:
                    return no_verdict(f"could not write the review: {e}")
                aside["committed"] = True
            with _state_io():
                os.makedirs(snap_dir, exist_ok=True)
                write_new(os.path.join(snap_dir, os.path.basename(output_md)), content)
        except OSError as e:
            # R19: an OSError of the set-aside / the review files is NO VERDICT (exit 3), never a traceback (exit 1)
            return no_verdict(f"could not handle the review files: {e}")

        findings = data["findings"]
        review_md = os.path.relpath(output_md, repo)
        summary = {"verdict": data["verdict"], "round": n, "backend": settings.backend, "blockers": len(blockers),
                   "risks": sum(1 for a in findings if a["severity"] == "RISK"),
                   "notes": sum(1 for a in findings if a["severity"] == "NOTE"),
                   "spec_promises_without_task": data.get("spec_promises_without_task") or [],
                   "ceiling_reached": n >= MAX_ROUNDS, "wall_s": wall_s, "review_md": review_md, "route": route}
        if model_requested:
            summary["model_requested"] = model_requested

        # 11. plans only: the verdict for THIS hash, then the gate re-runs its checks (a spec review approves nothing)
        if not options.as_spec:
            entry = {"verdict": data["verdict"], "blockers": len(blockers), "round": n, "backend": settings.backend}
            if model_requested:
                entry["model_requested"] = model_requested
            entry.update(route=route, when=when, review_md=review_md)
            if file_identity(path) != identity:
                return no_verdict(CHANGED_DURING_ROUND)
            with _state_io():
                state.record_verdict(path, content_hash, entry)
            try:
                _, out, _ = gate_api.run_checks(path)
                lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
                summary["gate"] = lines[-1] if lines else "unknown"
            except getattr(gate_api, "GateUnavailable", ()) as e:
                # The verdict IS recorded (exit 0 stays true); the gate reads it on its next mark/run-checks.
                summary["gate"] = "unavailable"
                print(i18n.t("review.gate_rerun_failed", lang, error=e), file=sys.stderr)

        # 12.
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    try:
        return locked()
    finally:
        if lock is not None:
            lock.close()
