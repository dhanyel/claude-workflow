#!/usr/bin/env python3
"""SessionStart: announces this plugin's plan-gate-check.json in the plan-gate inbox (manifests.d).

⚠️ Never writes stdout (SessionStart stdout becomes model context) and always exits 0 (a hook error must not break a
session). The gate fails CLOSED on its side: an inbox entry it cannot register is a failing required check
(plan-gate checks.ingest_inbox).
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gate  # noqa: E402

PLUGIN = "adversarial-review"


def main():
    root = os.path.abspath(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HERE))
    entry = {"manifest": os.path.join(root, "plan-gate-check.json"), "plugin_root": root}
    folder = os.path.join(gate.gate_state_dir(), "manifests.d")
    target = os.path.join(folder, PLUGIN + ".json")
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, target)
    except OSError as error:
        print(f"adversarial-review announce: {error!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
