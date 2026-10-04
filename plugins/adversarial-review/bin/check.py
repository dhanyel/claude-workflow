#!/usr/bin/env python3
"""The plan-gate check: PASS only with an APPROVED review of THIS content hash (adversarial-review spec §3.1).

⚠️ It never runs a review -- the gate runs checks inside a hook with a 45 s ceiling (plan-gate checks.py:25) and a
review takes 4-12 min. It reads the verdict that review.py recorded, and answers in milliseconds.
⚠️ Exit 0 only on pass: a "pass" with a failing exit is an error for the gate (plan-gate R34).
"""
import json, os, re, shlex, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gate, i18n, loop, state  # noqa: E402

CHECK_ID = "adversarial-review"


def review_command(path):
    """R17: every word through `shlex.quote` -- double quotes would still expand `$(...)` in a plan's file name."""
    return f"python3 {shlex.quote(os.path.join(HERE, 'review.py'))} {shlex.quote(path)}"


def release_command(path):
    """The human release, with the ABSOLUTE plan_gate.py (the bare name is not on PATH; plan-gate R83)."""
    try:
        gate_py = shlex.quote(gate.locate())
    except gate.GateUnavailable:
        gate_py = "<plan-gate>/bin/plan_gate.py"
    return f"python3 {gate_py} release {shlex.quote(path)}"


def decide(path, content_hash, lang):
    t = lambda key, **kw: i18n.t(key, lang, **kw)  # noqa: E731
    if not path or not os.path.isfile(path):
        return "fail", [t("check.no_file", path=path)]
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash or ""):
        return "fail", [t("check.no_hash")]
    entry, error = state.verdict_for(path, content_hash)
    if error:
        return "fail", [t("check.state_unreadable", error=error, folder=state.state_dir())]
    if entry is None:
        return "fail", [t("check.no_review", command=review_command(path))]
    b = entry.get("blockers")
    if entry.get("verdict") == "APPROVED" and type(b) is int and b == 0:
        return "pass", []
    n = entry.get("round", 0)
    if type(n) is not int:
        return "fail", [t("check.state_unreadable", error=f"round is not an integer: {n!r}",
                          folder=state.state_dir())]
    if n >= loop.MAX_ROUNDS:
        return "fail", [t("check.ceiling", n=n, md=entry.get("review_md", "?"), release=release_command(path))]
    return "fail", [t("check.rejected", n=n, blockers=entry.get("blockers", "?"), md=entry.get("review_md", "?"),
                      command=review_command(path))]


def _lang(path):
    try:
        return i18n.resolve_language(gate.repo_config(path, timeout=10).get("language"))
    except gate.GateUnavailable:
        return i18n.resolve_language(None)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = os.path.abspath(argv[-1]) if argv else ""
    status, items = decide(path, os.environ.get("PLAN_GATE_CONTENT_HASH", ""), _lang(path))
    findings = [] if status == "pass" else [{"group": CHECK_ID, "state": "findings", "count": len(items),
                                             "items": items}]
    print(json.dumps({"status": status, "findings": findings}, ensure_ascii=False))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
