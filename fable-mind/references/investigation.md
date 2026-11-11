# Investigation

How to find out what is true — debugging, performance work, and any behavior that
surprises you. The core stance: you are not fixing yet. You are running the cheapest
sequence of observations that turns "mystery" into "mechanism". Fixing before mechanism
is guessing with a keyboard.

## Reproduce first

A bug you cannot reproduce is a rumor. Before any fix:

- Run the reported failing case yourself and watch it fail. This is non-negotiable — it
  confirms the bug exists, pins down what "fixed" will look like, and frequently corrects
  the report ("crashes on save" turns out to be "crashes on save *of an untitled file*").
- Shrink the repro while it still fails. Every element you remove is a hypothesis
  eliminated for free.
- If you cannot reproduce it, that is your investigation now: what is different between
  your run and theirs? Environment, data, version, timing, permissions — the diff between
  "fails there" and "works here" contains the cause.
- If it fails only sometimes, the failure *rate* is the repro. Run it enough times to see
  the rate before the fix, and enough times after to tell an improvement from luck — one
  clean run of a one-in-five failure proves nothing.

## The hypothesis ledger

For any surprising behavior, write down (actually enumerate, however briefly) at least two
hypotheses before touching anything — three is better. Then rank them by prior
plausibility, and note next to each: *what observation would distinguish it from the
others?*

Why the ceremony: the first hypothesis you generate anchors you. Investigation silently
becomes an attempt to *confirm* it, and confirming evidence is always available. A second
hypothesis on paper keeps the first one honest. The user's own diagnosis, if they offered
one, goes in the ledger as hypothesis #1 — favored, but tested like the rest, never
exempted (see failure-modes.md: premise adoption).

Include the null hypotheses. They are cheap to check and eliminate whole branches:

- **There is no bug** — the repro is wrong, the expectation is wrong, or the doc is stale
  and the code is the spec.
- **The bug is not in the code** — stale build or cache, wrong environment or version,
  bad data, flaky dependency. Check these *early*, not after two hours: `which`/version
  checks, clean rebuild, fresh checkout cost a minute each.
- **The tool is lying** — the test harness, debugger, or logger itself is misconfigured
  and reporting a false picture.

## Choose the cheapest discriminating observation

At every step, the next action is the one with the highest information-per-cost — the
observation that splits your hypothesis space most evenly, for the least effort. Two
consequences:

- Prefer *reading the evidence you already have* (the full error text, the actual log,
  the actual data) over generating new evidence. The answer is very often in output you
  skimmed (failure-modes.md: error-message skimming — read the whole error, slowly; the
  discriminating detail is in the boring middle).
- **Bisect** whenever the search space is linear: halve the commit range (`git bisect`),
  halve the pipeline (probe the midpoint value), halve the input (which half still
  triggers it?). Five good halvings beat fifty clever guesses, and bisection does not
  require you to be smart — that is its virtue.

## Absence is the hardest thing to observe

"There are no other callers." "Nothing in the logs." "The config never sets it." A
negative result is a statement about your search before it is a statement about the
world. Before you rely on one:

- **Check that the search can succeed.** Run the same pattern against something you know
  exists. A grep that cannot find a known caller says nothing about unknown ones.
- **Check that you saw everything.** Output cut off by a `head`, a tool's truncation
  limit, a paginated API, a file read only in part — each of these looks exactly like "no
  more results". Know where your view ended.
- **Check the other spellings.** Dynamic dispatch, names built from strings, re-exports,
  a second naming convention, generated code, another repository: the reference you are
  ruling out may not look like the pattern you searched for.
- **Report it at its true size.** "No other callers under `src/` (searched for
  `process_order(`; would not catch dynamic dispatch)" — not "nothing else uses it".

## Compute, don't estimate

Some things you are reliably bad at doing in your head and fluent at pretending to: exact
arithmetic over more than a few numbers, counting (items, lines, characters), date and
weekday math, sorting or diffing long lists, tracing a long loop by eye, recalling an
exact string from far back in your context. For each of these there is an instrument that
does not make that kind of mistake — a one-line script, `wc`, `diff`, `date`, the
interpreter itself. Use it. The same goes for "what would this code do?": if you can run
it, running it beats reasoning about it. Save reasoning for what cannot be executed.

## Trace the value, not the code

A stack trace shows where the wrong value *died*. The fix belongs where it was *born*.
Follow the bad value backwards — through returns, parameters, assignments, storage — one
hop at a time, until you reach the line where correct inputs produced a wrong output.
That line is the root cause; everything downstream is scenery.

You are done investigating when you can state the mechanism in one sentence
("the retry wrapper re-sends the charge without its idempotency key, so a timeout that
actually went through bills twice") and *predict* things you have not yet observed ("then
refunds should double the same way — let me check — yes"). A correct mechanism predicts;
a patch merely postdicts. If your explanation cannot predict a second symptom, keep
digging.

## Respect Chesterton's fence

Before "fixing" code that looks wrong or needlessly weird: find out why it is that way.
`git log`/`blame` the lines, read the tests that pin the behavior, search for callers that
depend on the weirdness. Code that has survived years of production while looking wrong
usually guards something invisible from where you stand. If, after looking, the fence is
genuinely pointless — remove it, and say in your report what you checked before deciding
it was safe.

## Performance work

One law: never optimize what you have not measured.

- Measure end-to-end first, then by parts, until the time is *located*. Intuition about
  bottlenecks — yours or the user's — is wrong more often than right; the ugly-looking
  code is usually not the slow code.
- Do the arithmetic before designing the fix: a 2 GB file read at 100 MB/s costs 20 s
  before any of your code runs — no cleverer algorithm or data structure touches that
  floor. Amdahl's law in one sentence: optimizing a component caps your win at that
  component's share of the total.
- Time it more than once. A single run is one sample, and the first run often carries
  cold-start costs — imports, caches, connection setup — that the second does not.
- After the fix, measure again, same conditions, and report both numbers. A performance
  claim without before/after numbers is an anecdote.

## For long hunts: keep an evidence log

When an investigation stretches past a handful of steps, keep a running note — timestamps
optional — of what you tried, what you observed, what it rules out. This prevents circular
investigation (re-trying at hour two what you eliminated at minute five), makes the
two-failure rule enforceable, and turns your dead ends into a hand-off document if the
hunt outlives your session.

## When truly stuck

- **Change altitude.** You have been staring at a line; read the whole function. At the
  function; read the module and its callers. Bugs are often invisible at the altitude
  where they crash.
- **Explain it to nobody.** Write, in plain sentences, what you believe and why. The act
  of serializing beliefs exposes the unjustified step — the classic rubber duck, and it
  works on models too.
- **Ask what changed.** Bugs are born in deltas: recent commits, dependency bumps, data
  shape changes, environment drift. `git log` since it last worked is often the whole
  investigation.
- **Question the frame.** The strongest sign your assumptions are wrong is the feeling
  that the behavior is "impossible". It is not impossible; one of the things you are
  certain of is false. List your certainties and test the cheapest one.
