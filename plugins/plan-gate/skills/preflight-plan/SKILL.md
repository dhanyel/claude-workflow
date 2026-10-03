---
name: preflight-plan
description: Use when you finish writing or changing an implementation plan in docs/superpowers/plans/, and whenever the plan gate blocks a tool saying a plan is pending -- it runs the mechanical checks (symbol that does not exist, decorative constant, file created outside the commit, verification that does not cover) and reports what is left.
---

# Plan preflight

A plan reviewed by whoever wrote it agrees with itself **by construction**. This skill runs what is
mechanical -- binary-answer `grep` -- before any code.

## When

A plan was written or changed; or the gate blocked a tool.

## How

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/preflight_plan.py" docs/superpowers/plans/<plan>.md
```

Seven check groups, seconds, no model cost. Fix the findings **in the plan** -- not in a table saying
you fixed it; whoever reads the plan later is the next person, not your note.

For a machine-readable result (what the gate consumes):

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/preflight_plan.py" --json docs/superpowers/plans/<plan>.md
# {"status": "pass"|"fail", "findings": [{"group", "state", "count", "items"}]}
```

`status` is `pass` **only if** no group has findings or did not run **and at least one group is `ok`**.
A plan where every group is `not_applicable` (prose only: no code block, no `**Files:**`, nothing
verifiable) is **vacuous** -- it fails with a `nothing-to-check` entry instead of passing on nothing.

## The seven groups

| group | question it answers |
|---|---|
| `aliases` | does the aliased `import` resolve in the repo? |
| `invoked-symbols` | does the method the plan calls exist, or does an **earlier** task create it? |
| `decorative-constants` | is the constant the plan defines used anywhere? |
| `tolerant-defaults` | is there a `?? 0`, empty `catch`... that turns an unknown value into silence? |
| `cited-files` | do the paths the plan cites exist? |
| `created-not-committed` | does a task that creates a file have it in a `git add`? |
| `verification-gap` | does the task's test command cover what it created? |

## What this is NOT

The preflight is `grep`, **not an adversarial review**. It does not see composition between tasks,
reuse gates, a defense that does not reach the output path, or a test that does not prove what its
name promises. Passing the preflight is not a reviewed plan -- it is a plan without mechanical errors.

⚠️ It prints `NOT EVALUATED` when it does not know how to judge -- a plan outside the language table,
or a plan where no task was recognized. That is **not** "ok": it is the absence of a check, and it is
written in the output.

The exit codes tell the three cases:

| code | means |
|---|---|
| `0` | nothing mechanical pending |
| `1` | there is a finding -- fix it in the plan |
| `2` | some check came out `NOT EVALUATED`, that is, it **did not run** |

And the four states each group can have:

| state | means | the gate |
|---|---|---|
| `ok` | ran, examined an item, nothing wrong | allows |
| `not_applicable` | ran and there was no item in scope | allows |
| `findings` | found something | **refuses** |
| `not_evaluated` | did not run, or did not recognize what to examine | **refuses** |

`2` is not a command failure: it is missing coverage. Treating it as "passed" is the mistake the
preflight exists to keep you from making.

## Release (a human decision)

When every required check passes, the plan is **approved automatically**: there is nothing to run. Fix the
findings in the plan and save it; to run the checks now:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/plan_gate.py" run-checks docs/superpowers/plans/<plan>.md
```

`release` is a **human decision**, never a way out of a finding. Run it only when the person who decides
tells you to, with their reason:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/plan_gate.py" release docs/superpowers/plans/<plan>.md \
    --reason "<what the person decided>" \
    --false-positive invoked-symbols="<why this finding is false>" \
    --no-coverage verification-gap="<why this group could not run>"
```

- `--reason` is required; without it nothing is released.
- `release` runs the preflight on the plan as it is **now**, and refuses (exit 2) while a group that blocks
  has no escape.
- `--false-positive GROUP=text` is accepted only for a group in state `findings`; `--no-coverage GROUP=text`
  only for a group in state `not_evaluated`. Any other state is refused. Both can be repeated.
- The separator is the first `=`. `GROUP` is one of the English ids: `aliases`, `invoked-symbols`,
  `decorative-constants`, `tolerant-defaults`, `cited-files`, `created-not-committed`, `verification-gap`.
- A release is tied to the plan's content: editing the plan afterwards makes it pending again.

## Requirements

`python3` >= 3.10 and `git`. `ripgrep` is optional: without it the search runs on `git ls-files`.
