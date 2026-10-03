---
name: phase
description: Use at the first message of a demand (`/phase start`), and when a demand enters review, validation or is delivered (`/phase review`, `/phase validation`, `/phase delivered`) -- it stamps the boundaries that the hooks cannot see, so the phase table has real times.
---

# Phase stamps

The hooks stamp what they can see (plan written, first commit, a human waiting). Three boundaries
are invisible to them, and only you know when they happen. This skill stamps them.

## When

- **`/phase start`** -- at the **first message of a demand**, before anything else. It prints the
  date with offset, the branch and how many commits it is ahead of the base, and stamps `start`.
  Pass the issue number when the branch does not carry it: `start #58`. When the branch of the demand is not
  checked out yet (it will be created later, or lives in another worktree), pass it: `start --branch feat/58-x`
  -- the stamp goes to that branch's demand, at the real time. It fetches the base first (`git fetch origin
  <base>`, never prompting); if the fetch fails, the count shows `— (not fetched: <reason>)` instead of a
  stale number.
- **`/phase review`** -- when the implementation is done and the review begins.
- **`/phase validation`** -- when the review passed and the validation begins (tests, the screen).
- **`/phase delivered`** -- when the work is delivered (MR opened, handed over). It closes the table.
  The `delivery-report` skill stamps it itself on an `ok` publish: run it by hand only when the delivery
  happened another way, or when that skill tells you to (after an `unknown` publish found by `open-request`).
- **`/phase report`** -- prints the table at any moment.

## How

```bash
python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" start [#N] [--branch B]
python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" review
python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" validation
python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" delivered
python3 "${CLAUDE_SKILL_DIR}/../../bin/phase.py" report
```

A name is lowercase letters and `_`; anything else exits 2 and writes nothing. The names the hooks and the
gate stamp themselves (`released`, `approved`, `spec_released`, `spec_approved`, `first_commit`, `commit`,
`push`, `plan_written`, `spec_written`, `prompt`, `stop`) are refused too: a hand-made one would forge that
boundary.

## Never reconstruct a time

A timestamp is read from the clock **at the moment it happens**, never rebuilt from memory. If you
forgot a stamp, it stays missing and the table shows `—`: an empty cell is honest, a plausible
guess is indistinguishable from a measurement a week later. Stamping late is worse than not
stamping, because the time would be the time of the command, not of the boundary.
