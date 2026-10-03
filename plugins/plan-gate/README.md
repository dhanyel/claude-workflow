# plan-gate

Plan-first gate with registrable checks, wall-clock timing per phase and MR/PR reports, as a Claude Code plugin.

- Hooks: `hooks/hooks.json` (gate, timeline, check registration).
- Skills: `preflight-plan`, `preflight-spec`, `phase`, `delivery-report`.
- Checks it registers: `plan-gate-check.json`.

Installation, configuration (`.claude/plan-gate.json`), environment variables, the check contract and the
privacy model are documented in the [root README](../../README.md). The reasoning behind the design is in
[docs/method.md](../../docs/method.md).

Run the tests from the repository root:

```bash
python3 -m unittest discover -b -s plugins/plan-gate/tests -t .
```
