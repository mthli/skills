# Execution

How to change things without breaking things: the discipline between "I know what to do"
and "it is done and I can prove it".

## Read before you write

Before editing anything, establish a read radius around the target:

- **The function itself** — all of it, not the six lines around the change.
- **Its callers** — most "locally correct" edits fail at a caller you did not read.
  Changing a return shape, an error behavior, or a mutation pattern changes a contract;
  the callers are where the contract lives.
- **Its tests** — they tell you which behaviors are pinned on purpose, and they are where
  your change will first be contradicted.
- **One level of data flow** — where do the inputs come from, where do the outputs go.

This costs minutes and routinely saves the whole afternoon. Editing code you have not
read is the confident form of guessing.

## The minimal diff

Write code that looks like it grew there: match the file's naming, error handling,
comment density, and idiom — even where your taste differs. A change that is 10 lines of
fix and 90 lines of taste is a 100-line review burden and a 10-line fix.

Comments: add one only to state a constraint the code cannot show ("must run before the
cache warms"). Never narrate what the next line does, and never explain why your change
is correct — that argument belongs in your report, not in the file.

Adjacent problems you discover — the deprecated call two functions down, the test that
tests nothing — go on your "also noticed" list for the report. Fixing them silently
entangles your diff; ignoring them silently wastes what you saw. The only exception:
something adjacent that actually blocks your change gets the smallest unblocking fix,
flagged loudly.

## Act in verified steps

Advance in increments small enough that when something breaks, the cause is the last
step. After each coherent unit — a function extracted, an endpoint added, a bug fixed —
verify *something*: compile it, run the relevant test, probe the behavior. Five stacked
unverified changes that then fail cost more than the five verifications would have,
because now the failure is unattributable.

Keep the working tree clean as you go: debug prints removed, temp scripts in a scratch
directory (not the repo), no commented-out corpses, no accidental reformatting of lines
you never meant to touch. Debris is not neutral — every stray artifact is a thing the
reviewer must decide about.

## The verification pyramid

Rungs, from cheapest to most conclusive:

1. **It parses / typechecks** — necessary, proves almost nothing.
2. **Unit tests pass** — the changed behavior, in isolation.
3. **The suite passes** — you did not break the neighbors.
4. **The real thing runs** — the actual command, app, or pipeline, end to end, doing the
   actual task.

Climb as high as the change warrants, and *report the highest rung you reached* — "suite
green, and I ran the repro script: output now correct" is a different claim from
"typechecks". Never let the reader assume a higher rung than you climbed.

What to feed it (in this order, from failure-modes.md #13):
1. the exact case from the bug report — the fix's one non-negotiable;
2. one hostile input — malformed, unexpected type, wrong order;
3. the boundaries — empty, zero, one, max, duplicate;
4. then the happy path.

## Regression tests

A fix without a regression test is a fix that can quietly un-happen.

- The test lives at the **root-cause level**, not the symptom level: if the bug was a
  shared mutable default, the test asserts non-shared state — not that one particular
  screen renders nicely.
- Its name states the behavior it protects (`test_subscribers_do_not_share_tag_lists`),
  so its future failure is self-explaining.
- **Prove the test can fail**: run it against the pre-fix code (mentally or actually) —
  if it would have passed on the buggy code, it tests nothing. A test born green has
  never earned its place.

## Dangerous operations

For deletes, overwrites, migrations, bulk operations, and anything outward-facing:

- **Look first.** List what is actually at the target. If reality contradicts the
  description you were given ("delete the temp table" — it has 40M rows and a foreign
  key), stop and surface it.
- **Rehearse.** Dry-run flags, `--limit 1`, a copy of the data, a branch. Do the
  operation small before doing it large.
- **Preserve a way back.** Backup before rewrite; branch before force; export before
  drop. The cost is seconds; the alternative is explaining why there is no way back.
- Constraints written at the top of a file ("generated — do not edit", "third-party —
  replaced on deploy") are load-bearing. Working *around* a constraint you find
  inconvenient — stubbing the latency out, editing the generated file "just this once" —
  is not cleverness; it is a defect with extra steps.

## Long tasks

Past a handful of steps, working memory needs external support:

- Keep the **ledger**: the parts of the ask, decisions made, constraints given, steps
  done/pending. Update it as you go; re-read it at phase boundaries (context rot,
  failure-modes.md #9, is beaten by exactly this).
- Prefer finishing one thread before opening the next. Half-done threads multiply the
  state you must carry and are where deliverables get lost.
- If the session may end before the work does, keep the work in a hand-off-able state:
  the ledger current, the next step written down, nothing existing only in your head.
