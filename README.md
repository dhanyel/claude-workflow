# claude-workflow

A Claude Code marketplace with two plugins: **plan-gate**, a plan-first, measured workflow, and
**adversarial-review**, a plan review by a model of another lineage that plan-gate enforces as a required check.

- **Plan gate.** While a plan (or the spec behind it) has not passed its checks, the tools that change
  the repository are refused. The hooks do it, so it holds even when nobody remembers to ask.
- **Mechanical preflights.** `grep`-grade checks of a plan and of a spec, with no model in the loop.
- **Registrable checks.** Any plugin can add a check that has to pass before a plan is approved.
- **Adversarial review (`adversarial-review`).** Sends the plan to a reviewer of another model lineage (Codex,
  opencode or an OpenAI-compatible endpoint) that runs read-only inside the repo and checks the plan against the
  code that exists. A plan is not approved without an `APPROVED` review of its current content. See
  [plugins/adversarial-review](plugins/adversarial-review/README.md).
- **Wall-clock timeline.** Each phase of a demand is stamped from the clock, never estimated.
- **Delivery report.** Writes the MR/PR description from the real diff, publishes it on GitHub or
  GitLab, reads it back to verify it, and comments the timing table on the issue.

Why each piece exists: [docs/method.md](docs/method.md).

## Install

```
/plugin marketplace add dhanyel/claude-workflow
/plugin install plan-gate@claude-workflow
/plugin install adversarial-review@claude-workflow   # optional; needs plan-gate >= 0.2.0
```

To try a local clone instead, add its path as the marketplace:

```
/plugin marketplace add /path/to/claude-workflow
/plugin install plan-gate@claude-workflow
```

Requirements: `python3` >= 3.10 (standard library only) and `git`. `ripgrep` is optional (without it the search runs on `git ls-files`). `delivery-report` needs no CLI, only a token (see Environment).

## Quick start

1. Open a session in a git repository. A plan is any `.md` file under `docs/superpowers/plans/`
   (a spec: under `docs/superpowers/specs/`); both folders are configurable.
2. Write the plan. The `PostToolUse` hook runs the registered checks on it. Until they pass, the plan is
   `pending`, and these are refused (exit code 2, reason on stderr): `Write`, `Edit`, `MultiEdit`,
   `NotebookEdit`, `Task`/`Agent`, and any `Bash` command not recognised as read-only. Writing the spec,
   and anything under the repo's own `docs/superpowers/` (at the repo root), stays allowed. Writing the plan
   stays allowed too, unless a spec is still pending: then the plan write is refused as well. A gate state
   file that cannot be read counts as pending, and a file tool aimed at the state folder is always refused
   (approval comes from the checks, release from a human). While anything is pending, a file tool aimed
   anywhere under `~/.claude/claude-workflow/` (the state of every check plugin, such as adversarial-review's
   verdicts), `PLAN_GATE_DIR` or `ADVERSARIAL_REVIEW_DIR` is refused too.

   ⚠️ **The gate is not a sandbox.** It stops a forgetful agent from implementing before the plan is approved; it
   does not contain a malicious one. A command it does not read as a write (`python3 -c "open(...).write(...)"`,
   for one) still writes, wherever it points.
3. Fix the findings in the plan and save it again. When every required check passes, it is `approved`.
4. See where things stand, and release by hand when a finding is a false positive:

```bash
python3 "$PLUGIN/bin/plan_gate.py" status
python3 "$PLUGIN/bin/plan_gate.py" release docs/superpowers/plans/my-plan.md \
    --reason "why a human is overriding the gate" \
    --false-positive invoked-symbols="the symbol is created by a generator" \
    --no-coverage verification-gap="the suite needs a browser"
```

`$PLUGIN` is the plugin folder (`${CLAUDE_PLUGIN_ROOT}` inside Claude Code).

- `--reason` is required.
- For a **plan**, `--false-positive GROUP=text` is accepted only for a group in state `findings`, and
  `--no-coverage GROUP=text` only for a group in state `not_evaluated`. Both can be repeated.
- For a **spec**, `--false-positive GROUP=text` is accepted only for a blocking group (`guarantees`,
  `citations`, `support`) in state `findings`; `--no-coverage` is refused. A spec that changes while
  `release` checks it is not released.
- The separator is `=` (the first one; the text may contain more). Group ids are the English ones:
  - plan: `aliases`, `invoked-symbols`, `decorative-constants`, `tolerant-defaults`, `cited-files`,
    `created-not-committed`, `verification-gap`;
  - spec: `guarantees`, `citations`, `support`, `outside-repo`, `quantities`, `claims`.
- A release is tied to the file's content: edit the plan afterwards and it is `pending` again.
- `release` also runs every other required check that applies; one that fails blocks the release unless you name
  it with `--despite-check <id>="why"`, which is recorded.
- There is no release that skips the check: `release` runs the preflight on the file as it is now, and
  refuses while a group that blocks has no escape.

Other commands: `plan_gate.py run-checks <file>` (run the required checks now), `plan_gate.py checks list`,
`plan_gate.py checks prune` (removes orphaned registrations), `plan_gate.py register-check <manifest>`, `plan_gate.py hash <file>` and `plan_gate.py config <file>` (what check plugins use).

Skills: `preflight-plan`, `preflight-spec`, `phase` (`/phase start|review|validation|delivered|report`) and
`delivery-report`.

## Configuration: `.claude/plan-gate.json`

Optional, per repository. Missing file = defaults. Unknown keys print one warning and are ignored.
A broken file (bad JSON, not an object, a symbolic link, not a regular file, larger than 1 MiB, or a
wrong value) is **not** silent defaults: the gate refuses tool calls until it is fixed, except for the edit
of the config file itself.

| key | type | default | validation |
|---|---|---|---|
| `language` | string or null | `null` | `en` or `pt-BR`; any other value (a non-string included) only warns and falls through to `PLAN_GATE_LANG`, then `en` |
| `plans_dir` | string | `docs/superpowers/plans` | relative to the repo; no `~`, no `..`, not empty or the root, no leading or trailing whitespace |
| `specs_dir` | string | `docs/superpowers/specs` | same as `plans_dir` |
| `checks` | object of objects | `{}` | `{"<check id>": {"required": true\|false}}`; overrides the check's own `required` |
| `mr_template` | string or null | `null` (the plugin's template) | the config only checks string or null; the path rules (relative, no `..`, a readable file inside the repo) are enforced when `delivery-report` builds the description, and a bad path fails that report, not the gate |
| `platform` | `"github"`, `"gitlab"` or null | `null` (from the `origin` remote) | anything else is refused |
| `api_url` | string or null | `null` (derived from the remote) | `http(s)://host[:port][/path]`; no credentials, query or fragment, no whitespace |

```json
{
  "language": "en",
  "plans_dir": "docs/plans",
  "checks": {"preflight-spec": {"required": false}, "my-lint": {"required": true}}
}
```

Language order: `language` in the repo config, then `PLAN_GATE_LANG`, then `en`.

## Environment

| variable | effect |
|---|---|
| `PLAN_GATE` | `off` turns the gate off (hooks that stamp the timeline keep running; a file tool aimed at the state folder is still refused) |
| `PLAN_GATE_LANG` | `en` or `pt-BR`, used when the repo config has no `language` |
| `PLAN_GATE_DIR` | state folder; default `~/.claude/claude-workflow/plan-gate/`. Meant for tests: **never set it globally** (e.g. in the `env` block of `~/.claude/settings.json`) while another plan gate that honours the same variable is installed, or both gates share one state folder and read each other's states |
| `GITLAB_TOKEN` | token for GitLab (`delivery-report`) |
| `GITHUB_TOKEN` | token for GitHub (`delivery-report`) |
| `GITLAB_HOST` | the self-hosted GitLab the token may be sent to |
| `GH_HOST` | the GitHub Enterprise host the token may be sent to |

### Where a token may go

The repo travels with `git clone`, so the repo config can never decide where your token is sent.

- `GITLAB_TOKEN` goes only to `gitlab.com` or the host named in **your** `GITLAB_HOST`.
- `GITHUB_TOKEN` goes only to `api.github.com` or the host named in **your** `GH_HOST`.
- `api_url` in the repo config only picks the API base for a host you already named; it never adds one.
- The project name comes from `origin`, so `origin` must live on that same API host (`github.com` counts as
  `api.github.com`) -- strictly: naming the origin's host in `GITLAB_HOST` / `GH_HOST` does not pair it with an
  API on another host.
- The token travels over **https**. Plain http is used only when you wrote `http://` yourself in
  `GITLAB_HOST` / `GH_HOST`, and only to that exact host and port.
- Anything else is answered with `token-not-bound-to-host` and nothing is sent.
- The token is sent in a header only, never in a URL, a file or the output.

## Checks: the contract

A check is a command that decides whether a plan or a spec may be approved. A plugin registers its
checks in a `plan-gate-check.json` at its root (one object or a list):

```json
{"id": "my-lint", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bin/lint.py\" --json",
 "required": true, "applies_to": ["plan"]}
```

At `SessionStart`, plan-gate registers **its own** `plan-gate-check.json`, copying each entry to
`<state dir>/checks.d/<id>.json` with `${CLAUDE_PLUGIN_ROOT}` resolved. There is no directory scan: another plugin
announces its manifest in the inbox `<state dir>/manifests.d/<plugin>.json` (or calls
`plan_gate.py register-check <manifest>`), and an inbox entry the gate cannot register is a failing required check.
The details, including the gate pointer, are in the [plan-gate README](plugins/plan-gate/README.md#check-plugins-contract).

- **Invocation.** `shlex.split(command)`, run **without a shell**, with the absolute path of the checked
  file appended as the **last argument**, a 45 second timeout, no stdin.
- **Answer.** On stdout, one JSON object:

  ```json
  {"status": "pass", "findings": []}
  {"status": "fail", "findings": [{"group": "invoked-symbols", "state": "findings", "count": 1, "items": ["..."]}]}
  ```

  `"status"` is `"pass"` or `"fail"`; `"findings"` is a list. **stdout decides, not the exit code**:
  the preflights exit 1 or 2 on a fail and their findings must survive. But a `"pass"` with a non-zero exit
  is a fail.
- **Anything else is a fail** carrying a `check-error` group with the reason: stdout that is not that JSON,
  an invalid status, a command that cannot start, a timeout.
- **Approval.** A file is `approved` only when it has **at least one** required applicable check and
  **every** one passes at its current content. Zero checks approve nothing.
- **Vacuity.** A check that examined nothing must not pass:
  1. the plan preflight is vacuous when no group is `ok` (all `not_applicable`, e.g. prose with no code
     block and no `**Files:**`);
  2. the spec preflight is vacuous when it examined no item (no citation resolved in this repo and no
     sentence got past its filters).

  Both fail with a `nothing-to-check` entry.
- **Required.** `required` in the manifest can be overridden by `checks.<id>.required` in the repo config.
  A non-boolean value counts as required. `applies_to` is `["plan"]`, `["spec"]` or both; a missing or
  unknown value means "applies to everything".
- **A check the repo requires but nobody registered blocks.** `"checks": {"my-lint": {}}` without a
  registered `my-lint` is a failing required check. Only `"required": false` lets it be absent.
- **Ownership.** An `id` is one file. Re-registering by the same plugin replaces it in place. Two
  manifests with the same `id` from different plugins become a **conflict** that fails as a required check,
  naming both manifests; nothing is replaced silently. A registration whose plugin folder is gone is an
  **orphan** (listed by `checks list`, removed by `checks prune`) and fails when run. An entry that cannot
  be read counts as a failing required check and is kept for a human to look at.

The two checks plan-gate ships are `preflight` (plans) and `preflight-spec` (specs); see the
`preflight-plan` and `preflight-spec` skills for their groups.

## Timing

Hooks stamp what they can see (`prompt`, `stop`, plan or spec written, approved or released, the first
commit, a push); `/phase start|review|validation|delivered` stamps the boundaries only you know. Stamps go
to an append-only file per demand (branch) under the state folder. `/phase start --branch B` stamps the demand
of a branch that is not checked out yet; `/phase start` fetches the base before counting the commits ahead of it,
and says `— (not fetched: <reason>)` when it cannot. The names the hooks own (`released`, `first_commit`,
`prompt`, ...) cannot be stamped by hand.

`/phase report` renders a table of the phases `discussion`, `spec`, `plan`, `implementation`, `review`,
`validation` and `delivery`, with wall time, time **waiting for a human** (from the last `stop` before a
prompt to that prompt) and the difference. A boundary without a stamp shows `—`: **nothing is estimated**.
A demand with no `prompt` and no `stop` at all shows waiting as `—`, never `0`. Delivery opens at the first
commit or push that comes after every earlier phase started (commits made during the implementation stay in
it); without one there is no delivery row. The table closes at the first `delivered` after the last phase
started.

## Delivery report

The `delivery-report` skill drives `bin/delivery.py`: `platform`, `render`, `publish`, `open-request`, `update`,
`verify`, `comment` and `notes` (the id, length and ⏱ heading of an issue's notes, read before commenting again
after an `unknown`). Run it from the checkout of the delivered branch: `publish --source` must be that branch,
and `render` returns `branch` and `target` to repeat in `publish`. It stamps `delivered`, rewrites the description with the closed timing table, reads
it back to verify its length, and comments the table on the issue. It never pushes, merges or closes an
issue. The description comes from `templates/<language>/mr.md`, or from the file named in `mr_template`
(placeholders `{{summary}}`, `{{changes}}`, `{{verification}}`, `{{timing}}`, `{{closes}}`).

## Privacy

- Gate state, checks and the timeline are **local files** under the state folder (`PLAN_GATE_DIR`). Nothing
  is uploaded, and no hook makes a network call.
- `delivery-report` talks to a network, and only when you ask for it, with the token you provided and only
  to the hosts described above. The one other lookup is `/phase start`: a single read-only query for the
  open MR/PR of the branch, under the same token and host rules, with a 10 s total deadline; it is skipped
  when there is no token or the host is not bound.
  `/phase start` also runs `git fetch -q <remote> <base>` (`plugins/plan-gate/bin/phase.py:165`) to measure
  the branch against its base; that uses your own git credentials and remote, like any fetch.
- `adversarial-review` sends the plan and what its agent reads in the repo to the backend you chose -- and nothing
  without one.
- Plans, specs and reviews are your files; the default `docs/superpowers/` folder is something your own
  `.gitignore` decides whether to version.

## Development

```bash
python3 -m unittest discover -b -s plugins/plan-gate/tests -t .
```

Tests use a temporary `HOME` and `PLAN_GATE_DIR` and never touch the real state.

## License

MIT -- see [LICENSE](LICENSE).
