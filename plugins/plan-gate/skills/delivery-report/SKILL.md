---
name: delivery-report
description: Use when a demand is ready to be delivered as a merge request / pull request -- builds the description from the real diff and the commands that actually ran, publishes it on GitHub or GitLab (which stamps `delivered`), rewrites it with the closed ⏱ table, reads it back to verify it, and comments the table on the issue.
---

# Delivery report

One command line per step. Every command prints one JSON object on stdout and exits 0 only when its
`outcome` is `ok`. Read the JSON; never assume a step worked.

Write the full command in every step, as shown: a shell function defined in one Bash call does not exist in
the next one.

**Run every step from the checkout of the branch being delivered** (the worktree where that branch is checked
out). The timeline, the issue and the `delivered` stamp belong to the branch of the current checkout, and
`publish` refuses a `--source` that is not that branch (`source-is-not-this-checkout`).

The platform comes from the `origin` remote (`github.com`, or the host named in `GH_HOST` -> GitHub; anything
else -> GitLab); the repo config (`.claude/plan-gate.json`) may override it with `platform` and `api_url`. The
token is read from `GITHUB_TOKEN` or `GITLAB_TOKEN`. Never print it, never put it in a file, never paste it in a
command.

A token is only sent to a host **the person's environment** names: `GITLAB_TOKEN` to `gitlab.com` or the host
of `GITLAB_HOST`; `GITHUB_TOKEN` to `api.github.com` or the host of `GH_HOST` (GitHub Enterprise). The `origin`
remote must also live on that very API host (strictly; naming the origin's host in the environment is not enough), because the project name comes
from it. The repo config never adds a host -- it travels with the repo, so a cloned repo could otherwise send the token to its
own server. On a self-hosted GitLab the person sets `GITLAB_HOST`; on GitHub Enterprise, `GH_HOST`. The token
travels only over https; plain http only when the person wrote the scheme there (`GITLAB_HOST=http://host`), and
only to that exact host and port -- a repo `api_url` with `http://` is never used with the token. Any other host,
or http that the environment did not name, gives `token-not-bound-to-host: <host>`. That is a decision for the person: never work around it, and
never set those variables yourself.

## Never

- **Never push, merge, or close or reopen an issue.** This skill opens the MR/PR, rewrites its description,
  reads it back and comments. Pushing the branch happens before it, by the person or by you on request;
  merging and closing belong to the person.
- **Never invent a verification.** A command goes under Verification only if it ran in this session and
  you saw its result. What did not run is declared under "Not run" -- it is not omitted.
- **Never type the ⏱ table.** `render` reads it from the timeline. A number nobody read from a clock is
  not a number.

## Steps, in this order

1. **Render the description from the real diff.** Read `git diff <base>...HEAD` and `git log <base>..HEAD`
   (not your memory of the session). Write an input file:

   ```json
   {"summary": "...", "changes": "...", "verification": ["python3 -m unittest ..."],
    "not_run": ["the e2e suite (no browser here)"], "target": "main", "issue": "58"}
   ```

   `issue` is optional (default: the last `/phase start #N`, else the number in the branch name). Then:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" render --input <in.json> --out <mr.md>
   ```

   The JSON returns `branch` (this checkout's branch) and `target`: repeat them as `--source` and `--target` in
   step 2.

   `closes` is `Closes #N` only when `target` is the project's default branch. When it is null, `warnings`
   says so: write the issue link in the summary yourself, and tell the person the issue will have to be
   **closed manually** after the merge -- the platform will not close it. A line of yours that starts with
   `/` gets a zero-width space so GitLab never runs it as a quick action.

2. **Publish.**

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" publish --title "<title>" --source <branch from render> --target <target from render> --body-file <mr.md>
   ```

   | outcome | what it means | what you do |
   |---|---|---|
   | `ok` | created; `ref` and `url` returned; **`delivered` is already stamped** (`"delivered": true`) | go to step 3 |
   | `refused`, status 409 or 422, because the MR/PR **already exists** (e.g. opened by a push option) | the request is there, just not by this call | run `python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" open-request --branch <branch>`. If it finds the request, stamp `python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" delivered` and continue with its `ref` from step 3 (render -> update -> verify -> comment). Never publish again |
   | the `open-request` after a 409/422 answers `"request": null`, or is not `ok` | the platform says the request exists, and the lookup did not find it or failed | **STOP.** Report to the person: the publish outcome, its `api_message`, and what `open-request` returned. Never publish again, and do not stamp `delivered` |
   | `refused`, any other 4xx | bad branch, no permission, invalid field | read `api_message`, fix the cause; do not resend blindly |
   | `retryable` | 429 | wait, then publish again |
   | `unknown` | sent, but 5xx or no answer: it **may exist**; nothing was stamped | **NEVER publish again before** `python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" open-request --branch <branch>`. If it found the request, run `python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" delivered` and continue with its `ref`; publish again only if the lookup answered `"request": null` |
   | `error` | nothing usable (no token, unbound host, bad remote, bad URL) | fix `error`; nothing was created |

   If `ok` came with `"delivered": false`, the stamp failed: run `python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" delivered` yourself.

3. **Render again: the ⏱ table is now closed.** With `delivered` stamped, the Delivery end and the Total
   have real values. Add `"request_url": "<url>"` to the input so the issue comment links the MR/PR:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" render --input <in.json> --out <mr.md> --comment-out <comment.md>
   ```

   Note `length` from the JSON.

4. **Update the description** with the closed table (the description only -- nothing else changes):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" update --ref <ref> --body-file <mr.md>
   ```

   On `unknown`, go to step 5: the read-back says whether it landed. Same outcome table otherwise.

5. **Verify by reading it back.**

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" verify --ref <ref> --expected-len <length from step 3>
   ```

   Not `ok` means the description did not land in full (platforms can drop a description). Do not report
   the MR/PR as described until `verify` passes; update again, then verify again.

6. **Comment the final ⏱ table on the issue** (only when there is an issue):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" comment --issue <N> --body-file <comment.md>
   ```

   `<N>` is the `issue` that `render` returned. The same outcome table applies. On `unknown`, read the issue's
   notes before commenting again -- a second identical note is the failure mode:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../../bin/delivery.py" notes --issue <N>
   ```

   It lists each note's `id`, `length` and whether it has the ⏱ heading (`timing`). A note with `timing: true`
   posted now means the comment landed: do not comment again. Never use `curl`, `gh` or `glab` for this.

## Report to the person

The MR/PR URL, whether `verify` passed (with the length read back), whether the issue comment was posted,
and -- when `closes` was null -- that the issue must be closed by hand after the merge.
