---
name: preflight-spec
description: Use when you finish writing or changing a spec in docs/superpowers/specs/, and whenever the gate says a spec is pending -- it checks what the spec CLAIMS about the code (guarantee claim without proof, broken citation, citation that does not support the sentence) and reports what is left.
---

# Spec preflight

## Why it exists, with a number

A real adversarial reading found **10 defects** in one spec, **4 of them blocking**. Three were claims
about code read in the same session and described wrongly afterwards -- "the service already rejects
values over the cap" (it does not) and "the state stays untouched" (only the `status` does).

⚠️ **No downstream review would catch it.** Plan review checks **the plan against the spec**. If the spec
lies, both agree and pass -- by construction. It is the same root as "the task's test came from the brief
and cannot catch an error in the brief", one level up.

## The writing contract

**Every sentence that claims something about the code is born with a `file:line` checked at the moment of
writing.** Not "I remember the code does X". Opening the file is what catches the mistake; the citation
is only the proof that you opened it.

Where you cannot prove it, write **`NOT VERIFIED`** and the reason. That is not a weakness of the spec: it
is the difference between a declared doubt and a lie that looks like a fact.

```markdown
The cap does not count the incoming request (`src/limits.py:146`).
Whether the provider keeps charging when the client disconnects: **NOT VERIFIED**.
```

## Run

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/preflight_spec.py" docs/superpowers/specs/<spec>.md
```

For a machine-readable result (what the gate consumes):

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/preflight_spec.py" --json docs/superpowers/specs/<spec>.md
# {"status": "pass"|"fail", "findings": [{"group", "state", "count", "items"}]}
```

Two rules are specific to the spec:

- `status` only looks at the **blocking** groups. A non-blocking warning never makes it `fail`.
- **Vacuity.** Every group without a finding is `ok`, so "at least one `ok`" proves nothing. `pass`
  needs the checks to have **examined at least one item**: a citation that resolved to a file in this
  repo, or a sentence that got past the checks' filters. Sentences of intent ("vai", "vamos"), quoted
  or block-quoted sentences, and citations to files outside the repo are **not** examined, so a spec
  made only of those fails with a `nothing-to-check` entry instead of passing on nothing.

Exit codes: `0` nothing blocking (with `--json`: `0` only if `status` is `pass`), `1` a blocking group
has findings (with `--json`: or the spec is vacuous), `2` spec not found, no argument, or a check could not run (with `--json`: every group `not_evaluated`).

## The groups, and why only three block

The cut **came from measurement** against a corpus of 73 real specs -- not from taste:

| group | measured | blocks? |
|---|---|---|
| `guarantees` -- guarantee claim without proof | 0.65/spec, ~70% adjudicated precision | **yes** |
| `citations` -- citation that does not resolve (line past the end) | 0.08/spec, precision 100% by construction | **yes** |
| `support` -- citation that does not support the sentence | 0.09/spec, precision 100% by construction | **yes** |
| `outside-repo` -- citation to outside this repo | 0.24/spec | warns |
| `quantities` -- quantitative requirement without a value | 2.48/spec (68% of occurrences) | warns |
| `claims` -- ordinary claim without a citation | 33.45/spec | warns |

⚠️ **Why the last three do not block.** Demanding a citation on every claim would be ~33 findings per
spec: a gate that is escaped every time is dead. The **guarantee** cut ("already prevents", "guarantees",
"does not do", "untouched") isolates exactly the class that burns, in ~2 sentences per spec.

⚠️ **Bare "always" and "never" do not count.** They caught working policy ("`docs/superpowers/` is never
committed") instead of code behavior. An adverb cannot tell a claim about code from a rule about people;
a capability verb can.

⚠️ **The sentence recognizers are Portuguese regexes** (`ja impede`, `garante`, `vai`, `teto`...). The
text the plugin shows you follows your language setting, but the claims it detects are the ones written
in Portuguese prose.

## What this is NOT

The spec preflight is `grep`. It checks whether you **cited**, not whether you **understood**. Of the 10
defects that motivated it, it would catch 6; the other 4 were external knowledge, a reasoning error and a
forgotten failure mode. For those, use an adversarial review pointed at the spec.

Passing the preflight is not a reviewed spec -- it is a spec without claims from memory.

## Release (a human decision)

When every required check passes, the spec is **approved automatically**: there is nothing to run. Fix the
findings in the spec and save it; to run the checks now:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/plan_gate.py" run-checks docs/superpowers/specs/<spec>.md
```

`release` is a **human decision**, never a way out of a finding. Run it only when the person who decides
tells you to, with their reason:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/plan_gate.py" release docs/superpowers/specs/<spec>.md \
    --reason "<what the person decided>" \
    --false-positive guarantees="<why this finding is false>"
```

- `--reason` is required; without it nothing is released.
- `release` runs the spec preflight on the spec as it is **now**, refuses (exit 2) while a blocking group
  has findings without an escape, and refuses if the spec changes while it is being checked.
- `--false-positive GROUP=text` is accepted only for a **blocking** group (`guarantees`, `citations`,
  `support`) whose result is `findings`. Any other group or state is refused. It can be repeated.
- `--no-coverage` is **refused** for a spec (exit 2): it exists only for a plan group that did not run.
- The separator is the first `=`. The spec group ids are `guarantees`, `citations`, `support`,
  `outside-repo`, `quantities`, `claims` (only the first three block).
- A release is tied to the spec's content: editing the spec afterwards makes it pending again.

## Requirements

`python3` >= 3.10 and `git`. A spec outside a git work tree is checked relative to its own folder; if git itself cannot be asked (missing, timeout, "dubious ownership") the result is not-evaluated, never a pass.
