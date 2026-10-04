#!/usr/bin/env python3
"""gate: how adversarial-review reaches the plan-gate (adversarial-review spec §4.1)."""
import json, os, re, subprocess, sys


def gate_state_dir():
    """The plan-gate state folder: PLAN_GATE_DIR, else ~/.claude/claude-workflow/plan-gate.

    ⚠️ The same rule as plan-gate's config.state_dir(), duplicated ON PURPOSE: it is the bootstrap -- how this plugin
    finds the gate at all (gate/pointer.json and manifests.d live there). The plan-gate README documents it as part of the
    check-plugin contract, so a change there is a contract change.
    """
    return os.environ.get("PLAN_GATE_DIR") or os.path.expanduser("~/.claude/claude-workflow/plan-gate")


class GateUnavailable(Exception):
    """The plan-gate cannot be reached or answered unusably. The review REFUSES (exit 2): the plan stays pending."""


def locate():
    # ⚠️ gate/pointer.json, in a subfolder: plan-gate 0.1.0 reads every top-level *.json of its state folder as a
    # plan state, so a top-level gate.json would be misread.
    pointer = os.path.join(gate_state_dir(), "gate", "pointer.json")
    try:
        with open(pointer, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise GateUnavailable(f"{pointer}: {e}") from e
    path = data.get("plan_gate") if isinstance(data, dict) else None
    if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isfile(path):
        raise GateUnavailable(f"{pointer}: plan_gate {path!r} does not exist")
    return path


SECRET_VARS = ("ADVERSARIAL_REVIEW_ENDPOINT_TOKEN", "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE")


def env_without_secrets():
    """The parent environment minus the endpoint credential: for every child that is not the endpoint's opencode."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_VARS}


def _child_env():
    """The environment of every plan-gate subprocess: the reviewer credential is not the gate's business."""
    return env_without_secrets()


def _call(*args, timeout=60):
    plan_gate = locate()
    try:
        return subprocess.run([sys.executable, plan_gate, *args], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=_child_env())
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:  # ValueError: undecodable output
        raise GateUnavailable(f"{plan_gate} {args[0]}: {e}") from e


def content_hash(path):
    p = _call("hash", os.path.abspath(path))
    h = p.stdout.strip()
    if p.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", h):
        raise GateUnavailable(f"hash {path}: rc {p.returncode} {p.stderr.strip()[:200]}")
    return h


def repo_config(path, timeout=60):
    p = _call("config", os.path.abspath(path), timeout=timeout)
    if p.returncode != 0:
        raise GateUnavailable(p.stderr.strip()[:300] or f"config rc {p.returncode}")
    try:
        data = json.loads(p.stdout)
    except ValueError as e:
        raise GateUnavailable(f"config: {e}") from e
    if not isinstance(data, dict):
        raise GateUnavailable("config: not an object")
    return data


def run_checks(path):
    p = _call("run-checks", os.path.abspath(path), timeout=600)
    return p.returncode, p.stdout, p.stderr


def preflight_prompt(path, incremental):
    """The plan preflight rendered for a reviewer prompt, or None when it did not run (the prompt says so)."""
    script = os.path.join(os.path.dirname(locate()), "preflight_plan.py")
    args = [sys.executable, script, "--prompt", os.path.abspath(path)] + (["--incremental"] if incremental else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
                           env=_child_env())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 and p.stdout.strip() else None
