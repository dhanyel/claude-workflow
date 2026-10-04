---
name: review-plan
description: Use when a plan is pending on the adversarial-review check, or before the first task of a plan that touches money, permissions or irreversible operations — sends the plan to a reviewer of another model lineage, running read-only inside the repo, checking the plan against the code that exists.
---

# Review a plan

A plan reviewed by whoever wrote it agrees with itself **by construction**, and the task tests come out of the same
plan, so they agree with it too. This skill sends the plan to a reviewer of **another model lineage**, running
**inside the repo**, that answers what only the code can answer: does the method exist? is the constant used? does the
defense reach the `raise`? does the test turn red when the behavior breaks?

## When

After the mechanical preflight is clean (the `preflight-plan` skill), and always before the first task of a plan that
touches **money, permissions or an irreversible operation**. When plan-gate says the plan is pending on
`adversarial-review`, this is the way out.

## How

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/review.py" docs/superpowers/plans/<plan>.md
python3 "${CLAUDE_SKILL_DIR}/../../bin/review.py" docs/superpowers/plans/<plan>.md --thinking
python3 "${CLAUDE_SKILL_DIR}/../../bin/review.py" docs/superpowers/plans/<plan>.md --redo
python3 "${CLAUDE_SKILL_DIR}/../../bin/review.py" docs/superpowers/specs/<spec>.md --as-spec
```

- The spec is found by convention (`specs/<same-name>-design.md`) or passed with `--spec`.
- `--redo` runs the latest round again instead of starting the next one.
- `--as-spec` reviews a **spec** against the repo. It records no verdict for the gate (a spec review approves
  nothing), and every later round reviews the whole spec again, not a diff. It is written to
  `<specs_dir>/reviews/<spec>-<backend>-spec-round-N.md`.
- `--prompt-only` prints the prompt and writes nothing; `--timeout <seconds>` changes the 15 minute ceiling.
- Which reviewer runs (Codex, opencode or an endpoint), with which model, comes only from your environment
  (`ADVERSARIAL_REVIEW_BACKEND` and friends, see the plugin README). No flag chooses it. With no backend configured
  the command refuses before sending anything.

A plan review is written to `reviews/<plan>-<backend>-round-N.md` next to the plan. **Read that `.md`** — do not paste the
plan into the conversation.

## Handle the findings

| severity | what to do |
|---|---|
| `BLOCKER` | fix it **in the plan** before any code. Not negotiable. |
| `RISK` | fix it, or write in the plan why you will not |
| `NOTE` | judge it; it does not block |

A non-empty `spec_promises_without_task` is a blocker by definition: a requirement nobody will implement, and no task
test catches it, because the test comes out of the same plan.

A finding is not revealed truth: if the reviewer is wrong, refute it with `file:line` in the plan and say so there.
And **fix the STEP, never write a table saying you fixed it** — the next round reads the diff and checks the step.

## The loop ends in one of three places

- **`APPROVED`** → the gate approves the plan by itself at the end of the round (the review re-runs the gate's
  checks). Carry on.
- **3 rounds and a blocker still open** (the check then reports the ceiling; there is no hard cap, a 4th round would
  run) → stop and call whoever decides: it is a disagreement about a decision, not about a detail. An open blocker **holds `release`**: the gate refuses even with the preflight all green. Whoever
  decides to go on anyway takes it on in writing, and that is recorded in the gate state. Only when the person tells
  you to:

  ```bash
  python3 "<plan-gate>/bin/plan_gate.py" release <plan> --reason "…" --despite-check adversarial-review="why"
  ```

- **`NO VERDICT`** (exit code 3) → the reviewer could not answer (quota, dead session, invalid JSON after the one
  repair, the state folder unreadable), or the round refused to run: a symlink in `reviews/`, a tracked symlink that
  leaves the repo, the plan edited during the round. Nothing changes: a failure never approves.

Exit code 2 means the round was **refused before spending anything** (no backend, bad settings, plan-gate not
reachable, plan outside git, another round of the same plan running). Exit code 0 means a verdict was recorded; if the summary says `"gate": "unavailable"`,
the verdict is saved but plan-gate could not re-run its checks right then, and will on the next edit of the plan or on
`plan_gate.py run-checks`.

## Thinking is off by default, and that costs

Measured on 2026-09-17: without thinking the reviewer found the same defects in about 4 minutes; with thinking it takes
about 12. The price of the faster mode is lower severities and some oscillation between rounds. Use `--thinking` in
round 1 of a plan that touches money, permissions or irreversible operations. Ceiling per round: 15 minutes.
