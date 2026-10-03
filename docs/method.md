# The method behind plan-gate

Each piece of the plugin exists because of a specific way a demand goes wrong. This page says which.

## Plan before code

A model that starts typing code decides the design with its fingers. The plan is the cheapest place to be
wrong: a wrong sentence in a plan costs a re-read, a wrong task in code costs a rewrite. So the gate
refuses the tools that change the repository while a plan is pending, rather than asking nicely.

The same one level up: a plan is reviewed **against its spec**, so a spec that misdescribes the code passes
by construction, because the plan agrees with it. The spec has its own gate, and a pending spec blocks
writing the plan.

## Mechanical preflight

What can be answered with a binary `grep` should not wait for a reviewer: does the method the plan calls
exist, is the constant it defines used, does a task that creates a file put it in a commit, does the test
command cover what the task creates. These checks run in seconds and cost no model.

They are not a review. They do not see composition between tasks or a test that does not prove what its name
says. Passing the preflight means "no mechanical errors", and the skills say so.

A check that does not know how to judge says so (`not_evaluated`) instead of passing. The preflight's exit code 2
means "did not run"; the gate reads the verdict on stdout, where a `not_evaluated` group fails the check.

## Vacuity: a check that examined nothing is not a pass

If every group of a preflight comes back "nothing in scope", the result is not "ok", it is the absence of a
check. Counting it as a pass would open the gate on nothing, for example for a plan that is only prose. So
a result with no `ok` group (plan) or no examined item (spec) fails with `nothing-to-check`.

The same rule applies to the set of checks: zero required checks approve nothing, and a check the repo
requires but nobody registered blocks. Otherwise requiring it would be decoration.

## Fail-closed gate, fail-open hook

Two different failures, two different defaults:

- **The gate is fail-closed.** A broken config, a registration that cannot be read, a conflict between two
  plugins claiming the same check id, an orphaned command: each one counts as a required check that fails.
  A plan whose content changed after its release is pending again, and a state file that cannot be read
  counts as pending. Skipping a broken thing turns a failing required check into an absent one.
- **The state is not writable through a tool.** Approval comes from the checks and release from a person, so a
  file tool aimed at the state folder is refused in any repository, even with the gate switched off. The state
  folder is the gate's own: `PLAN_GATE_DIR` exists for tests, and setting it globally while another gate that
  reads the same variable is installed would make both gates share, and trust, one folder.
- **The hook is fail-open, with a trail.** A bug in the gate's own code must not lock every project, so a
  hook that crashes exits 0 and writes the error to a log. A manual command, such as `release`, fails
  closed: one that blew up and printed "released" would be lying.

Human release exists because a finding can be false. It requires a reason, names the group it overrides
with `GROUP=text`, is recorded in the plan's log and in a side file, and holds only for the bytes that were
examined.

## Hooks, not skills

A skill is advice the model may or may not follow, and the failure it guards against is the model forgetting
or rationalising. Whatever must hold every time (refuse a write, stamp a time, run the checks when a plan
is saved) is a hook. Skills carry what needs judgment: how to fix a finding, when to stamp a phase, what to
write in a report.

A hook exits 2 with the reason on stderr to refuse. It is invoked as `python3 script.py`, not as the script
itself, because a file created by a tool has no execute bit and a hook that cannot start is a non-blocking
error, which would switch the gate off silently.

## Wall-clock timing, with no estimates

Time is read from the clock at the moment a boundary happens, and written at once to an append-only file. A
boundary that was not stamped stays empty and renders as `—`. A reconstructed number looks like a measured
one a week later, and it poisons whatever someone later builds from those numbers, such as estimates.

Hence the rules: no stamp is invented, none is rewritten later, and the phases come from the stamps in a
fixed order. Stamping late is worse than not stamping, because the number would be the time of the command,
not of the boundary.

## Waiting for a human

Wall time includes the minutes a person spent reading and deciding, which is not the work. The table
separates them: a **waiting** interval runs from the last `stop` before a prompt to that prompt, and only
closed pairs count. A `stop` that no prompt ever closed has an unknown wait, so it is never counted and the
cell is empty; a demand with no `prompt` and no `stop` at all has no measured wait either, so its waiting is
empty, not zero. Work is wall minus waiting.

Phases tile the span and never overlap: a commit made during the implementation belongs to it, and delivery
opens only at the first commit or push after every earlier phase started.

## Registrable checks

The gate does not know what a good plan is for your repository. It knows how to ask. A plugin registers a
command, the gate runs it on the file, and it reads a JSON verdict from stdout. The command runs without a
shell, with an owner (the plugin folder, not a name anyone can declare), so one plugin cannot silently
replace another's failing check with a passing one. The bundled preflights are the first two users of the
contract, not a special case.

## Delivery report

The description of a merge request is written from the diff and the commands that actually ran, and what did
not run is declared. The timing table is rendered from the timeline, never typed. After publishing, the
skill reads the description back and compares its length: a platform can accept a request and drop the
body, and "opened" is not "described". Tokens leave your machine only for hosts that **your** environment
names, and only for a project whose `origin` lives on that host, because a repository you cloned must not be
able to choose where your credentials go, nor which project they act on.

## What this is not

It is not a code review, it does not run your tests, and it does not decide for you. It makes the cheap
mistakes impossible to make quietly, and records, from the clock, where the time went.

## Numbers

The plan preflight has seven groups and the spec preflight six, of which three block (see the
`GROUPS` tables in `bin/preflight_plan.py` and `bin/preflight_spec.py`). Anything else on this page is
described, not counted; run the test suite to see how many tests there are now.
