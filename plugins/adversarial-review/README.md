# adversarial-review

An adversarial review of your implementation plan by **a model of another lineage**, run inside your repository and
enforced as a required check of [plan-gate](../plan-gate/README.md): while the plan has no `APPROVED` review of its
current content, the gate keeps implementation closed.

A plan reviewed by its author agrees with itself by construction. The reviewer checks the plan against the code that
exists: does the method it calls exist, is the constant it defines used, does the defense reach the `raise`.

**Requires `plan-gate` >= 0.2.0** (the check-plugin contract: gate pointer, inbox, `release --despite-check`) and
`python3` >= 3.10. Standard library only.

## Install

```
/plugin marketplace add dhanyel/claude-workflow
/plugin install plan-gate@claude-workflow
/plugin install adversarial-review@claude-workflow
```

At every `SessionStart` this plugin announces its check to plan-gate (`<plan-gate state>/manifests.d/adversarial-review.json`);
plan-gate registers it the next time it runs. A plugin installed in the middle of a session is therefore enforced from
the **next session start** on. Run the review with the `review-plan` skill, or directly (`$PLUGIN` is this plugin's
folder, `${CLAUDE_PLUGIN_ROOT}` inside Claude Code):

```bash
python3 "$PLUGIN/bin/review.py" docs/superpowers/plans/my-plan.md [--thinking] [--redo] [--as-spec] [--spec FILE] [--timeout SECONDS] [--prompt-only]
```

## Choose a reviewer (nothing is sent until you do)

There is **no default backend**. With `ADVERSARIAL_REVIEW_BACKEND` unset the command refuses and sends nothing. The
backend, model, URL and token are read **only from your environment** (for instance the `env` block of
`~/.claude/settings.json`), never from the repository's `.claude/plan-gate.json`: a repository you cloned must not be
able to choose where your plan and your key go.

**1. `codex`** — your own Codex login (the `codex` CLI, run read-only):

```json
{"env": {"ADVERSARIAL_REVIEW_BACKEND": "codex"}}
```

`codex exec` has no model flag: `ADVERSARIAL_REVIEW_MODEL` is ignored with a warning and is not written in the review.

**2. `opencode`** — a provider you already configured in opencode (`opencode` >= 1.18.0):

```json
{"env": {"ADVERSARIAL_REVIEW_BACKEND": "opencode",
         "ADVERSARIAL_REVIEW_MODEL": "zai-coding-plan/glm-5.3"}}
```

The model is `<provider>/<model>`; the provider's credential comes from your own opencode setup.

**3. `endpoint`** — any OpenAI-compatible URL, driven through opencode:

```json
{"env": {"ADVERSARIAL_REVIEW_BACKEND": "endpoint",
         "ADVERSARIAL_REVIEW_MODEL": "glm-5.3",
         "ADVERSARIAL_REVIEW_ENDPOINT_URL": "https://llm.example.com/v1",
         "ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE": "/home/you/.config/adversarial-review/token"}}
```

| variable | meaning |
|---|---|
| `ADVERSARIAL_REVIEW_BACKEND` | `codex`, `opencode` or `endpoint`. Required. |
| `ADVERSARIAL_REVIEW_MODEL` | `opencode`: `<provider>/<model>`. `endpoint`: the endpoint's model name. `codex`: ignored (warns). |
| `ADVERSARIAL_REVIEW_ENDPOINT_URL` | `endpoint` only. `https://`, or `http://` only for `127.0.0.1`, `::1`, `localhost`; no credentials, query or fragment. |
| `ADVERSARIAL_REVIEW_ENDPOINT_TOKEN_FILE` | `endpoint` only. An **absolute** path (a leading `~` is accepted) to a regular file with mode `600` (no access for group or others), at most 4096 bytes. |
| `ADVERSARIAL_REVIEW_ENDPOINT_TOKEN` | `endpoint` only: the token itself. Set this **or** the file, not both. |
| `ADVERSARIAL_REVIEW_THINKING` | `on` turns the model's thinking on for every round (same as `--thinking`). Off by default: about 4 min a round against about 12. |
| `ADVERSARIAL_REVIEW_DIR` | where the verdicts live; default `~/.claude/claude-workflow/adversarial-review/`. Meant for tests. |

The token is never printed, never written to a file (not the review, not the state, not the generated
`opencode.json`, which only holds an `{env:...}` reference) and never given to plan-gate. It exists only in the
environment of the one `opencode` child process, and only for the `endpoint` backend. It is sent only to the host of
`ADVERSARIAL_REVIEW_ENDPOINT_URL`. Put the raw token in `..._TOKEN`, never in `..._TOKEN_FILE`: the file variable must
be a path, and a value that is not one is refused (the message never echoes it).

⚠️ opencode starts the MCP servers and plugins of **your own global opencode config**, and they inherit the child's
environment: in `endpoint` mode they can read the token. Keep that config to things you trust.

## What the reviewer can do (opencode and endpoint)

The reviewer is an opencode agent with **no shell at all**: it can read, grep, glob and list files, and it can write
exactly one file: this round's JSON in the `reviews/` folder next to the plan (not the earlier rounds, not the
override trail). A plan whose file name holds a wildcard character (`*`, `?`, `[`) is refused, since the write
permission is a glob. Everything else is denied (the permission base is deny-all, so a
tool you added to your global opencode config, such as an MCP server that writes, stays off). A web fetch, a sub-agent
and any directory outside the repo are denied too. The generated config turns `lsp` and `formatter` off, because they
would run the reviewed repository's own tools with the token in the environment. It also disables opencode's
title generator (`agent.title.disable`), whose extra request cost about 3k tokens per round for a title nobody reads.

The reviewed repository cannot reconfigure the reviewer: the child runs with `OPENCODE_DISABLE_PROJECT_CONFIG=1`, and
the round's config is also passed in `OPENCODE_CONFIG_CONTENT`, which loads last. A repo's `opencode.json` or
`.opencode/` therefore cannot change the endpoint, the permissions or load a plugin. The price: the reviewer ignores
the reviewed project's own opencode configuration, on purpose.

## What a round refuses (every backend)

- **A symlink at or under the `reviews/` folder**, or a `reviews/` folder that resolves outside the repository: the
  round writes there (and the opencode reviewer writes its JSON there), and a committed link would be written through.
  The round ends as NO VERDICT, exit code **3**. Review files are also opened with `O_NOFOLLOW`.
- **A tracked symlink (git mode `120000`) that resolves outside the repository**: the reviewer would read the file it
  points to (`~/.ssh/id_rsa`, a token file) and send it to the provider. NO VERDICT, exit code **3**.
- **A plan edited during the round**, even when restored to the exact bytes: NO VERDICT; review it again.
- **A second round of the same plan with the same backend** while one is running: refused, exit code **2**.

⚠️ plan-gate is **not a sandbox**: while a plan is pending it refuses file tools aimed at the review state folders, but
a command it does not read as a write (`python3 -c "..."`) still writes. Its threat model is a forgetful agent, not a
malicious one.

## Privacy

- **The plan, the spec and whatever the reviewer reads in your repository go to the provider you chose.** The
  reviewer reads freely: that includes files such as `.env*` if it opens them. Do not run a review on a repository
  whose files you do not want that provider to see.
- Nothing is sent without `ADVERSARIAL_REVIEW_BACKEND`.
- The token goes only to the host of `ADVERSARIAL_REVIEW_ENDPOINT_URL` (and only for `endpoint`).
- Verdicts and snapshots are local files under `ADVERSARIAL_REVIEW_DIR`. Reviews are written to `reviews/` next to the
  plan (`<plans_dir>/reviews/`): add it to your `.gitignore` if you do not want them versioned.

## The z.ai proxy

For the `opencode` backend **and only with a model whose provider is `zai-coding-plan`**, the plugin starts a tiny
local proxy (`bin/zai_proxy.py`) and points that provider at it. It exists for one measured reason: opencode reuses
HTTP connections, z.ai drops an idle one after 30-60 seconds, and the second request of a review then hangs
([opencode#15350](https://github.com/anomalyco/opencode/issues/15350)). The proxy opens a fresh upstream connection
for every request and relays the stream. It listens on `127.0.0.1:8788` only, passes your `Authorization` header
through, and never logs a body or a header.

It keeps running after the round (the next round reuses it). ⚠️ Anything already listening on `127.0.0.1:8788` is
trusted to be the proxy and receives your z.ai `Authorization` header: on a machine shared with other users, another
user could listen there first. Avoid this provider on such a machine.

## Rounds, verdicts, and the gate

- Rounds have **no hard cap**. From round 2 on they are incremental: the reviewer gets a diff against the previous
  round and the blockers still open. Plan reviews are `<plans_dir>/reviews/<plan>-<backend>-round-N.md` and `.json`.
- The verdict is recorded **per content hash** of the plan: edit the plan and the approval no longer applies.
- `APPROVED` with no blocker passes the check; the review then asks plan-gate to re-run its checks, so the plan
  becomes approved at the end of the round. From round 3 on, a still-rejected plan makes the check report the
  **ceiling**: the convention is that a human decides now (a 4th round still runs, and can still approve):
  `python3 "<plan-gate>/bin/plan_gate.py" release <plan> --reason "…" --despite-check adversarial-review="why"`.
- `--as-spec` reviews a spec. It records no verdict for the gate, and every round re-reviews the whole spec. Spec
  reviews are `<specs_dir>/reviews/<spec>-<backend>-spec-round-N.md` and `.json`.
- Exit codes of `review.py`: **0** a verdict was recorded (also when plan-gate could not re-run its checks right
  afterwards: the summary then says `"gate": "unavailable"`); **2** refused before spending anything (settings, the
  gate not reachable, a bad `--timeout`, a file outside git, another round of the same plan running); **3** NO VERDICT
  (backend failure, a refused symlink, the plan changed during the round, or a review file or the review's state
  folder could not be read or written): every verdict is left as it was. `--prompt-only` prints the prompt and writes
  nothing.

## Make it optional in one repository

```json
{"checks": {"adversarial-review": {"required": false}}}
```

in `.claude/plan-gate.json`.

## Uninstall

Uninstall the plugin, then run `plan_gate.py checks prune`: it removes the registration of a check whose plugin is
gone, the leftover inbox entry (`manifests.d/adversarial-review.json`) and any error record of it. Until you do, the
gate treats the orphan as a failing required check.

## Development

```bash
python3 -m unittest discover -b -s plugins/adversarial-review/tests -t .
```

Tests use fake `codex` and `opencode` binaries and temporary `HOME`, `PLAN_GATE_DIR` and `ADVERSARIAL_REVIEW_DIR`;
they never touch the network.
