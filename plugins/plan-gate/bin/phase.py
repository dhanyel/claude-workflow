#!/usr/bin/env python3
"""/phase: stamps the boundaries of a demand that the hooks cannot see (start, review, validation, delivered).

A CLI, not a hook: it may print to stdout. Every text goes through i18n.t. The timeline itself (file format,
report, rendering) lives in timeline.py; this script only decides WHICH event to append.

    phase.py start [#N] [--branch B]   stamp `start` (into demand B when given, R81); fetch the base, then print
                                       date, branch, commits ahead of the base and the open MR/PR
    phase.py <name>        stamp the event `<name>` (`[a-z][a-z_]*`; never `start` nor a name the hooks own)
    phase.py report        print the phase table
"""
import os, re, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import delivery, i18n, timeline

DASH = timeline.DASH
RE_NAME = re.compile(r"[a-z][a-z_]*")
RE_ISSUE = re.compile(r"#?(\d+)")
MAX_REASON = 160
# B-M2: the events the hooks and the gate stamp themselves -- a hand-made one would forge a boundary
# (`released` opens the implementation, `first_commit` the delivery...). `start` goes through its subcommand.
HOOK_OWNED = {"released", "approved", "spec_released", "spec_approved", "first_commit", "commit", "push",
              "plan_written", "spec_written", "prompt", "stop", "start"}


class Usage(Exception):
    """A bad `start` argument: (locale key, the value to show)."""


def language(repo):
    """R72: (language, config problem or None), resolved ONCE per command. A config that cannot be read must not
    take `start` down after it stamped: the language then comes from PLAN_GATE_LANG, else en."""
    try:
        return i18n.language(repo), None
    except Exception as e:                 # noqa: BLE001 -- ValueError from config.load, or an OSError race
        return i18n.language(None), config_problem(repo, e)


def config_problem(repo, exc):
    """The short reason a config could not be read: first line, no absolute path, no token, at most MAX_REASON."""
    text = (str(exc) or type(exc).__name__).splitlines()[0]
    path = os.path.join(repo, ".claude", "plan-gate.json")
    text = delivery.scrub(text.replace(f" in {path}", "").replace(path, ".claude/plan-gate.json"))
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 1] + "…"


def base_ref(repo):
    """`origin/HEAD` resolved, else `origin/main` when it exists, else None."""
    head = timeline._git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head and head.startswith("refs/remotes/"):
        return head[len("refs/remotes/"):]
    if timeline._git(repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/main"):
        return "origin/main"
    return None


def commits_ahead(repo, base, ref="HEAD"):
    if not base:
        return None
    return timeline._git(repo, "rev-list", "--count", f"{base}..{ref}")


def _consult(repo, branch, lang):
    """The line for `open_request`, without the deadline. Never a guess."""
    try:
        plat, project, base = delivery.target(repo)
        if not delivery.token(plat):
            return i18n.translate("phase.request_no_token", lang)
        bound, host = delivery.token_bound(repo, plat, base)    # R70/R71/R78: no token to an unbound host/origin
        if not bound:
            return i18n.translate("phase.request_not_bound", lang, host=host)
        found = delivery.open_request_for(plat, project, branch, base_url=base, timeout=delivery.LOOKUP_TIMEOUT)
    except delivery.DeliveryError as e:
        if e.code == "no-remote":
            return i18n.translate("phase.request_no_remote", lang)
        reason = e.code
    except delivery.RequestFailed as e:
        reason = e.reason
    except Exception as e:
        reason = type(e).__name__
    else:
        if found is None:
            return i18n.translate("phase.request_none", lang)
        ref, url = delivery.describe_request(plat, found)
        return i18n.translate("phase.request_found", lang, ref=ref, url=url)
    return i18n.translate("phase.request_not_consulted", lang, reason=reason)


def open_request(repo, branch, lang=None, problem=None, deadline=None):
    """R56/R65: the open MR/PR of `branch`, consulted on the remote -- or why it was not.

    The whole consultation (git, config, DNS, connect, read) has ONE deadline, LOOKUP_TIMEOUT: it runs in a daemon
    thread, and when the deadline passes the answer is "not consulted (timeout)" and the thread is abandoned --
    `start` never waits for it. "none open" only when the remote answered an empty list. `lang`/`problem` come from `language(repo)`. R72: a
    config that cannot be read gives "not consulted (config: <reason>)", and nothing is looked up.
    """
    if lang is None:
        lang, problem = language(repo)
    if problem:
        return i18n.translate("phase.request_not_consulted", lang, reason=f"config: {problem}")
    if branch == "detached":
        return i18n.translate("phase.request_not_consulted", lang, reason="detached HEAD")
    box = {}

    def run():
        try:
            box["line"] = _consult(repo, branch, lang)
        except BaseException as e:          # an i18n/config failure in the thread must still give a line
            box["line"] = None
            box["error"] = type(e).__name__

    worker = threading.Thread(target=run, name="phase-open-request", daemon=True)
    worker.start()
    worker.join(delivery.LOOKUP_TIMEOUT if deadline is None else max(0.0, deadline - time.monotonic()))
    if worker.is_alive() or "line" not in box:
        return i18n.translate("phase.request_timeout", lang)
    if box["line"] is None:
        return i18n.translate("phase.request_not_consulted", lang, reason=box["error"])
    return box["line"]


def _start_args(repo, argv):
    """`[#N] [--branch B | --branch=B]`, in any order -> (issue or None, branch or None). Raises Usage."""
    issue = branch = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--branch" or a.startswith("--branch="):
            if branch is not None:
                raise Usage("phase.usage", None)
            if a == "--branch":
                if i + 1 >= len(argv):
                    raise Usage("phase.usage", None)
                branch, i = argv[i + 1], i + 2
            else:
                branch, i = a.split("=", 1)[1], i + 1
            ok = branch and subprocess.run(["git", "-C", repo, "check-ref-format", "--branch", branch],
                                           capture_output=True, text=True, timeout=10).returncode == 0
            if not ok:
                raise Usage("phase.bad_branch", branch)
            continue
        m = RE_ISSUE.fullmatch(a)
        if issue is not None or not m:
            raise Usage("phase.bad_issue", " ".join(argv))
        issue, i = m.group(1), i + 1
    return issue, branch


def _short(text):
    text = (text or "").strip()
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 1] + "…"


def fetch_base(repo, base, deadline):
    """B-I5 (R81): `git fetch -q origin <branch>` of `base` (`origin/main`), never prompting, inside the deadline.
    None when it worked; otherwise the short reason -- the caller shows it instead of counting a stale base."""
    remote, _, name = base.partition("/")
    left = deadline - time.monotonic()
    if left <= 0:
        return "timeout"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    try:
        p = subprocess.run(["git", "-C", repo, "fetch", "-q", remote, name], capture_output=True, text=True,
                           env=env, timeout=left)
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as e:
        return type(e).__name__
    if p.returncode == 0:
        return None
    first = next((line.strip() for line in (p.stderr or "").splitlines() if line.strip()), f"exit {p.returncode}")
    url = timeline._git(repo, "remote", "get-url", remote) or ""
    return _short(delivery.scrub(first, delivery._remote_secrets(url)))     # a remote URL may carry a credential


def start(repo, argv):
    lang, problem = language(repo)             # R72: once, and never raising on a broken config
    try:
        issue, branch = _start_args(repo, argv)
    except Usage as e:
        key, value = e.args
        print(i18n.translate(key, lang, value=value) if value is not None else i18n.translate(key, lang),
              file=sys.stderr)
        return 2
    branch = branch or timeline.demand(repo)
    issue = issue or timeline.issue_ref(branch)
    # R81: stamped FIRST, at the real time -- the fetch and the lookup that follow may take seconds
    record = timeline.append(repo, "start", "phase", {"issue": issue} if issue else None, branch=branch)
    deadline = time.monotonic() + delivery.LOOKUP_TIMEOUT    # one deadline for the fetch AND the lookup (R65)
    base = base_ref(repo)
    ahead = None
    if base:
        failed = fetch_base(repo, base, deadline)
        if failed:
            ahead = i18n.translate("phase.not_fetched", lang, reason=failed)
        else:
            base = base_ref(repo) or base
            ref = branch if timeline._git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + branch) else "HEAD"
            ahead = commits_ahead(repo, base, ref)
    request = open_request(repo, branch, lang, problem, deadline=deadline)
    print(i18n.translate("phase.start", lang, date=record["t"], branch=branch, issue=issue or DASH,
                 base=base or DASH, ahead=ahead if ahead is not None else DASH, request=request))
    return 0


def main(argv):
    if not argv:
        print(i18n.t("phase.usage"), file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd != "start" and cmd != "report" and not RE_NAME.fullmatch(cmd):
        print(i18n.t("phase.bad_name", name=cmd), file=sys.stderr)
        return 2
    if cmd != "start" and cmd in HOOK_OWNED:
        print(i18n.t("phase.reserved_name", name=cmd), file=sys.stderr)
        return 2
    if cmd not in ("start", "report") and rest:
        print(i18n.t("phase.usage"), file=sys.stderr)
        return 2
    repo = timeline.repo_root(os.getcwd())
    if cmd == "start" or cmd == "report":
        if repo is None:
            if cmd == "start":
                print(i18n.t("phase.no_repo_start", date=timeline._now(), dash=DASH))
            else:
                print(timeline.render(timeline.report([]), i18n.language(None)))
            return 0
        if cmd == "start":
            return start(repo, rest)
        lang, problem = language(repo)         # L255/R72: a broken config never takes the report down
        _warn_config(lang, problem)
        print(timeline.render(timeline.report(timeline.events(repo, timeline.demand(repo))), lang))
        return 0
    if repo is None:
        print(i18n.t("phase.no_repo"), file=sys.stderr)
        return 2
    lang, problem = language(repo)             # L255/R72: resolved once; the stamp does not need the config
    timeline.append(repo, cmd, "phase")
    _warn_config(lang, problem)
    print(i18n.translate("phase.stamped", lang, name=cmd, branch=timeline.demand(repo)))
    return 0


def _warn_config(lang, problem):
    if problem:
        print(i18n.translate("phase.config_warning", lang, reason=problem), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
