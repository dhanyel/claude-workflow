# plan-gate

Plan-first gate with registrable checks, wall-clock timing per phase and MR/PR reports, as a Claude Code plugin.

- Hooks: `hooks/hooks.json` (gate, timeline, check registration).
- Skills: `preflight-plan`, `preflight-spec`, `phase`, `delivery-report`.
- Checks it registers: `plan-gate-check.json`.

Installation, configuration (`.claude/plan-gate.json`), environment variables, the check contract and the
privacy model are documented in the [root README](../../README.md). The reasoning behind the design is in
[docs/method.md](../../docs/method.md).

## Check plugins (contract)

Another plugin adds a required check to the gate without the gate knowing it in advance. This is the contract
(plan-gate >= 0.2.0); [adversarial-review](../adversarial-review/README.md) is its first user.

- **The manifest.** `plan-gate-check.json` at the plugin root, one object or a list:
  `{"id", "command", "required", "applies_to"}`. `${CLAUDE_PLUGIN_ROOT}` in `command` is resolved at registration.
- **Announcing it.** The plugin's own `SessionStart` hook writes
  `<state>/manifests.d/<plugin>.json` containing `{"manifest": "<absolute path>", "plugin_root": "<absolute path>"}`
  (both absolute). Plan-gate registers every inbox entry when it runs `mark`, `run-checks`, `register-checks`,
  `release` or `status`, so the order in which two plugins' hooks run does not matter. An entry the gate cannot
  register (unreadable, not absolute, an invalid manifest) becomes the **failing required check `inbox-<plugin>`**,
  never a silent skip. `plan_gate.py checks prune` removes the inbox entries whose manifest is gone and their error
  records, together with the orphaned registrations.
- **The state folder** is `PLAN_GATE_DIR`, else `~/.claude/claude-workflow/plan-gate`.
- **The gate pointer.** Plan-gate keeps `<state>/gate/pointer.json` = `{"plan_gate": "<absolute path of
  plan_gate.py>", "version": "<plugin version>"}` up to date on every hook, so another plugin finds the gate without
  hard-coding where it was installed. (It lives in a subfolder because plan-gate 0.1.0 reads every top-level
  `*.json` of the state folder as the state of a plan.) A plugin that cannot find or run the gate refuses on its side.
- **The content hash.** The command runs with `PLAN_GATE_CONTENT_HASH` in its environment: the hash of the file's
  content as the gate computes it, so a verdict can be tied to exactly what was checked. `plan_gate.py hash <file>`
  prints the same value, and `plan_gate.py config <file>` prints the resolved repo config (`repo`, `plans_dir`,
  `specs_dir`, `language`, ...) as JSON, so that nobody else has to read `.claude/plan-gate.json`.
- **Release.** `plan_gate.py release` also runs every other **required, applicable** check. One that fails blocks the
  release, unless a human names it with `--despite-check <id>="why"` (repeatable); the justification is recorded in
  the state. A check that passes cannot be named. Plan-gate's own check (`preflight` for a plan, `preflight-spec`
  for a spec) is skipped when it is healthy or not registered at all (release runs the preflight itself), but a
  corrupted or contested registration of it needs `--despite-check` as well.

```bash
python3 "$PLUGIN/bin/plan_gate.py" release docs/superpowers/plans/my-plan.md \
    --reason "a human decided" --despite-check adversarial-review="why the open blocker is accepted"
```

Run the tests from the repository root:

```bash
python3 -m unittest discover -b -s plugins/plan-gate/tests -t .
```
