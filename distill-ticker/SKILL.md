---
name: distill-ticker
description: >
  Roll up the append-only Thesis / Observation / Lesson blocks in `positions/<TICKER>.md`
  into a single current-consensus snapshot at `.claude/snapshots/positions/<TICKER>.md` —
  the view that future investment discussions load before forming new opinions. Use this
  skill whenever the user says "/distill-ticker", "distill ticker", "refresh the snapshot
  for <TICKER>", or asks to consolidate a ticker's recent views. This skill should NOT
  auto-trigger — only invoke it when the user explicitly requests it.
---

# Distill Ticker

Convert the chronological journal in `positions/<TICKER>.md` (and its mirror in
`git log -- positions/<TICKER>.md`) into a single current-consensus snapshot at
`.claude/snapshots/positions/<TICKER>.md`. Future investment discussions load that snapshot
before forming new views, so they pick up the current stance without having to page through
every block ever written about the ticker.

This skill is the **Layer 1 → Layer 2** step in the investment knowledge loop:

- **Layer 1** (immutable journal): structured blocks inside `positions/<TICKER>.md`, written
  by `/commit-invest` and committed to git.
- **Layer 2** (current consensus): `.claude/snapshots/positions/<TICKER>.md` — an active doc
  that gets re-distilled every few new blocks.

The append-only journal is the **source of truth**. The snapshot is a derived, regenerable
view. This skill must never edit existing blocks, amend commits, or rewrite history — the
fix for a wrong view is always a **new** block with `SUPERSEDES:`.

## When to run this

Pull, don't push. Run only when the user asks. Reasonable triggers:

- A ticker has accumulated 3–5 new blocks since the last distill (check the
  `Last distilled` line at the top of an existing snapshot, or count blocks in the journal
  newer than the snapshot's timestamp).
- The user just had a substantive discussion that landed a new view and wants the snapshot
  refreshed for future sessions.
- A position was just closed and the user wants the snapshot to reflect the exit before
  the next round of thinking about the name.

If the user invokes the skill without a target ticker, ask. Don't guess.

## Workflow

### 1. Identify the target ticker

1. **Verify the journal file exists**: `positions/<TICKER>.md` (with `.` → `-` in the
   filename for tickers like `BRK.B` → `positions/BRK-B.md`).
   - **Missing** → tell the user "no journal for <TICKER> yet — has it been discussed and
     committed via `/commit-invest`?" and stop. Don't create the file from here.
2. **Resolve the destination path**: always `.claude/snapshots/positions/<TICKER>.md` at the
   repo root, mirroring the journal filename. Create intermediate directories
   (`.claude/snapshots/positions/`) as needed.
3. **Read the existing destination if any.** If the snapshot already exists from a prior
   distill, read it now and keep it in working memory — it drives the diff-against-existing
   draft in step 5 and gives you the date of the last distill (which scopes "new since
   last").

### 2. Parse the journal (and postmortems for this ticker)

Read **two sources**:

1. `positions/<TICKER>.md` end-to-end — Thesis and Observation blocks live here.
2. `postmortems.md` end-to-end if it exists — Lesson blocks live here, but only those
   tagged with `TICKER: <TICKER>` belong to this ticker's snapshot. Skip Lesson blocks
   without a TICKER field (those are general lessons, not per-ticker) or with a different
   ticker.

Both files share the same block format: a sequence of blocks, **each identified by its H2
`## YYYY-MM-DD — <Type>` heading** (this is the parser anchor — locate blocks by scanning
for H2, not by splitting on `---`). Between adjacent blocks: a `---` horizontal-rule
separator. The first block in a file sits directly under the file's H1 (e.g. `# ARM`,
`# Postmortems`) with no leading `---`. Parse out:

| Block type      | Source file              | Required fields                                                              |
| --------------- | ------------------------ | ---------------------------------------------------------------------------- |
| **Thesis**      | positions/<TICKER>.md    | TICKER, THEMES, STANCE, HORIZON, THESIS, CATALYSTS, INVALIDATION, CONVICTION |
| **Observation** | positions/<TICKER>.md    | TICKER, THEMES, WHAT, WHY_IT_MATTERS                                         |
| **Lesson**      | postmortems.md           | TICKER, WHAT_HAPPENED, WHAT_WORKED, WHAT_DIDNT, GENERAL_LESSON               |

Optional fields per type: Thesis may have `ENTRY_PRICE`, `EXIT_PRICE`, `SUPERSEDES`;
Observation may have `RELATED_THEMES`, `RELATED_TICKERS`.

For Lessons, the "required fields" above are for **malformation detection only** — the
snapshot surfaces only `GENERAL_LESSON` (see step 5). The other fields are read so you
can flag blocks that are missing required content, but their values don't appear in the
final snapshot.

A quick grep helps locate Lesson candidates without reading all of `postmortems.md`:

```bash
grep -nE "TICKER:\s*<TICKER>\b" postmortems.md
```

Then scan backward from each match to find the enclosing `## YYYY-MM-DD — Lesson`
heading and parse the block.

For each parsed block, also record:
- The H2 date (parse `## YYYY-MM-DD — <Type>` → date)
- The block's position in the file (so ties break by latest)
- Any `SUPERSEDES:` value (date + commit hash of the prior block)

**If a block has malformed fields** (missing required keys, wrong nesting, fields outside
ALL-CAPS), surface them as warnings in step 6. Don't silently skip — the user may want to
fix the next round's input by writing better blocks going forward.

**Cross-reference with git log** for hash resolution (needed both for validating
SUPERSEDES values and for the `(commit <short-sha>)` citations in the snapshot template):

```bash
git log --oneline -- positions/<TICKER>.md
git log --oneline -- postmortems.md
```

The first command gives you `<short-sha> <subject>` pairs for Thesis / Observation blocks;
the second covers Lesson blocks (if you found any in postmortems for this ticker). Use
them two ways:

- **Validate SUPERSEDES values**: the hash inside `SUPERSEDES: YYYY-MM-DD (commit <sha>)`
  should exist in the relevant log. Flag mismatches as warnings.
- **Resolve shas for snapshot citations**: each parsed block needs a sha for the
  `(commit <short-sha>)` cites in step 5's "Open questions", "Lessons", and "Superseded
  theses" sections. Match each block's H2 date against the commit log; the first commit
  whose date matches is the originating commit.

**Tie-breaking when multiple commits share a date** (rare but possible — e.g., two
`/commit-invest`s on the same day each appended a block): disambiguate by `git show <sha>`
and matching the block's TICKER / STANCE / THESIS content against the commit body. If you
still can't disambiguate, accept the latest matching commit and flag in step 6. If a
block has no matching commit (uncommitted journal edits, or hand-edited content), record
`<uncommitted>` instead of a sha — don't hallucinate.

### 3. Resolve SUPERSEDES chains

Sort the parsed blocks by **date ascending** (use file-position as a tiebreaker for blocks
written on the same day). Walk forward through Thesis blocks:

- If block T has `SUPERSEDES: YYYY-MM-DD (commit <sha>)`, find the prior Thesis at that
  date in this file. **The commit sha disambiguates same-day cases**: if multiple Theses
  share the SUPERSEDES date, the sha pins down which one. On match, mark the prior thesis
  as **superseded by T**.
- A thesis can be superseded multiple times across iterations — only the latest in the
  chain is "Active". Earlier ones live in the `Superseded` section.
- **Ambiguous SUPERSEDES** (multiple prior blocks match the date AND the sha is missing
  or also matches multiple): surface as a warning, let the user pick at review time.
- **Unresolvable SUPERSEDES** (date doesn't match any prior block): surface as a warning.
  Don't fail; the user may want to accept the gap or edit.
- A Thesis with `STANCE: exit` and `EXIT_PRICE:` filled is treated as **closing the
  position** — the prior Thesis is superseded, and the exit Thesis itself is filed under
  Active but flagged as a closed position (see step 5).

**Observation and Lesson blocks don't use SUPERSEDES** in the same chain-collapsing way:
- **Observations** are surfaced in the snapshot only when they're newer than the latest
  Thesis (see step 4's "Open questions"). Older observations stay in the journal as the
  historical record but don't appear in the snapshot — anyone wanting them can
  `git log -p -- positions/<TICKER>.md` or just read the file.
- **Lessons** are post-trade reflections; they're always retained verbatim in the
  snapshot, since the point of a lesson is to be remembered.

### 4. Determine current stance

Walk the resolved chain of Thesis blocks to derive the **current stance**:

- The **latest non-superseded Thesis** is the live view. Its STANCE, HORIZON, CONVICTION,
  THESIS, CATALYSTS, INVALIDATION fields drive the snapshot's "Current stance" section.
- If the latest Thesis has `STANCE: exit` with `EXIT_PRICE:` filled → snapshot Stance is
  `Closed` (the "Current stance" section's Stance line may add detail like "Closed at
  <EXIT_PRICE> on <date>" beneath, but the canonical label is `Closed`).
- If the latest Thesis has `STANCE: watch` → snapshot Stance is `Watching`.
- If the latest Thesis has `STANCE: long` / `short` / `avoid` → use the title-cased
  STANCE value directly (`Long`, `Short`, `Avoid`).
- If no Thesis blocks exist (only Observations) → snapshot Stance is `No thesis`.

These labels match the enum in the snapshot template's Stance line —
`<Long | Short | Watching | Avoid | Closed | No thesis>` — so the snapshot is consistent
with downstream `/distill-theme` parsing that looks for these exact strings.

**Open questions**: scan Observations newer than the latest Thesis. Any Observation that
hasn't been folded into a subsequent Thesis is an "open question" — a data point the user
flagged but hasn't committed to a view on. Surface them.

**Catalysts and invalidation triggers**: take from the latest Thesis. If multiple recent
Theses agree on catalysts/invalidation, merge non-duplicatively. Don't pull from superseded
Theses unless the user asks.

### 5. Draft the snapshot

Template — fill in placeholders, don't include the `<>` brackets:

```markdown
# <TICKER> — Current Consensus

> Snapshot of current view on <TICKER>. Journal: `positions/<TICKER>.md`
> Last distilled: <YYYY-MM-DD> (HEAD = <short-sha>)

## Current stance

- **Stance**: <Long | Short | Watching | Avoid | Closed | No thesis>
- **Horizon**: <days | weeks | months | years>
- **Conviction**: <low | med | high>
- **As of**: <YYYY-MM-DD of latest Thesis> (commit <short-sha>)

## Live thesis

<One tight paragraph paraphrasing the THESIS field of the latest non-superseded Thesis.
Plain language, present tense.>

## Catalysts to watch

- <catalyst 1>
- <catalyst 2>
...

## Invalidation triggers

- <trigger 1 — what would mean we're wrong / exit signal>
- <trigger 2>
...

## Open questions

<Observations newer than the latest Thesis that haven't been folded into a view yet.
One line each, dated. Omit this section entirely if empty.>

- <YYYY-MM-DD>: <observation summary> (commit <short-sha>)

## Lessons

<All Lesson blocks for this ticker, verbatim GENERAL_LESSON field. One line each.
Omit this section entirely if no lessons exist.>

- <YYYY-MM-DD>: <general lesson> (commit <short-sha>)

## Superseded theses

<Compact history of prior Theses that were SUPERSEDED. One line each.
Omit this section entirely if empty.>

- ~~<YYYY-MM-DD>: <one-line summary of prior thesis>~~ → replaced by view on <YYYY-MM-DD> (commit <short-sha>)
```

Drafting rules:

- **Header derivation**: `Last distilled` is today's date in ISO format. `HEAD` is the
  current commit's short sha — get it via `git rev-parse --short HEAD` at distill time.
- **Paraphrase, don't quote**. The live thesis paragraph is a one-paragraph digest, not a
  copy-paste of the THESIS field. Readers want the gist; they can `cat positions/<TICKER>.md`
  or `git show <sha>` for the full original.
- **Omit empty sections entirely** rather than leaving them blank. A snapshot with no open
  questions just doesn't have an "Open questions" heading.
- **Use the latest Thesis's date** as the "As of" date — that's when the current view was
  last refreshed, not when this distillation ran.
- **Themes** are not surfaced in the per-ticker snapshot (they're the lateral axis for
  `/distill-theme`). The reader of this file is already in ticker-mode; theme aggregation
  is a different document.
- **Diff against the existing snapshot** if there is one. Only surface what changed (new
  live thesis, newly superseded, new open questions, new lessons) when presenting to the
  user in step 6. Don't rewrite unchanged sections from scratch — that produces a
  meaningless diff and erases hand edits.

### 6. Show the user

Present the draft (or, if the destination exists, the diff against it) for review.
Explicitly call out:

- The current stance and whether it changed since the prior snapshot
- New open questions added since prior snapshot
- Theses that moved into the Superseded section
- **Warnings** collected along the way:
  - Malformed blocks in the journal (with dates)
  - Ambiguous or unresolvable `SUPERSEDES` targets
  - Cases where Observations newer than the latest Thesis hint that the live thesis may be
    stale (e.g., 3 contradictory observations sitting unaddressed)
- The destination path and whether it's a create or an update

Ask: "Write this to `<path>`?"

Three responses to handle:

- **Yes** → proceed to step 7.
- **Edits** → apply the user's edits to the draft, re-show, ask again.
- **No / abort** → drop the draft. Don't write anything.

### 7. Write

Write the file with the `Write` tool. **Do not** `git add`, `git commit`, or push. The
user commits it themselves.

After writing, surface the path and tell them:

> "Wrote snapshot to `<path>`. Future investment discussions touching <TICKER> will load
> this file via the Investment Knowledge Loop read-rules. `git add` and commit when ready."

## Things this skill must not do

- **Never modify the journal file.** `positions/<TICKER>.md` is append-only. If a block is
  wrong, the user fixes it by writing a new block with `SUPERSEDES:` via `/commit-invest`.
  Editing the journal here breaks the audit trail and silently changes what future distills
  will produce.
- **Never modify commits.** No `--amend`, no `rebase`. The git log is authoritative.
- **Never delete history.** The `Superseded theses` section in the snapshot is a derived
  summary; the raw blocks still live in the journal. Don't "clean up" by deleting blocks.
- **Never auto-create a journal file.** If `positions/<TICKER>.md` doesn't exist, send the
  user to `/commit-invest`. Journal creation is one workflow.
- **Never silently swallow malformed blocks.** Surface them as warnings.
- **Never write the snapshot without explicit user approval.** Step 6 is mandatory; this is
  a destructive write to a tracked file.
- **Never auto-trigger.** Run only on explicit user invocation.

## Edge cases

**Journal exists but has only Observation blocks (no Thesis).** That's fine — the snapshot
will say "No thesis formed" and list the observations as open questions. The reader still
benefits from seeing the data points that have been logged.

**The latest Thesis is months old and many recent Observations contradict it.** Surface
this in step 6 as a warning: "The live thesis is from <date>, but <N> observations since
then suggest reconsideration." Don't rewrite the thesis — that's the user's job. Just
flag.

**The journal has Thesis blocks across multiple themes that have diverged** (e.g., long
for ai-infra reasons but the discussion has moved to data-center power constraints). The
snapshot reflects the **latest** Thesis only; cross-theme reconciliation is the user's job
when writing the next Thesis. Note in step 6 if the themes have drifted.

**SUPERSEDES references a commit that doesn't exist in git log** (typo, wrong hash, or a
commit that got squashed). Surface as a warning. Don't fail the distillation — the user
can either accept the gap, fix the journal with a corrective Thesis, or amend the snapshot
manually after writing.

**The same date has multiple blocks of the same type.** Treat them as ordered by file
position. If they're substantively duplicative (e.g., two Theses written 5 minutes apart
in one session), the later one wins; the earlier becomes part of the journal's history but
doesn't appear in the snapshot. Don't try to merge them.

**The destination snapshot was hand-edited** (the user added a personal note, reordered
sections). Use the diff-against-existing approach in step 5 so manual annotations survive.
If a hand-edit conflicts with the freshly distilled content, flag at review time — don't
silently overwrite.

**Working tree has uncommitted changes.** Fine — this skill only reads the journal and the
existing snapshot, and writes to `.claude/snapshots/positions/<TICKER>.md`. The user
decides when to stage and commit.

**A ticker has been renamed or merged with another** (e.g., FB → META). Treat them as
separate tickers — each gets its own journal and snapshot. If the user wants to migrate,
that's a manual step: rename the file, fix the H2 headings via a new committed block, and
re-run distill.

## Why this format

**The current stance is at the top** because it's what the reader wants first. A future
discussion about NVDA opens with "what's the current view?" — putting the answer in the
first paragraph (literally) means even a glance at the snapshot anchors the next discussion
correctly.

**Catalysts and invalidation triggers as bullet lists** because they're checklists, not
prose. A reader scanning for "what would change my view" wants to see them itemized, not
buried in a paragraph.

**Open questions as a distinct section** because un-folded observations are the
**friction** in the loop — they're the gaps where a Thesis hasn't yet caught up to the
data. Surfacing them at distill time means the next discussion has a natural agenda.

**Lessons retained verbatim** because their value is in the original phrasing. Unlike
Theses (which get paraphrased into a digest), a `GENERAL_LESSON:` line is already the
distilled version. Compressing it further loses signal.

**Superseded theses compacted into one-liners** because they're an appendix — the reader
visiting the snapshot rarely needs them, but they're how the next distill resolves
SUPERSEDES chains. Keeping them in the snapshot avoids re-parsing the entire journal each
time.

**Themes deliberately absent** from this per-ticker view because they're the unit of
`/distill-theme`. Mixing the two axes here would make each snapshot do two jobs and degrade
both. The reader of `positions/<TICKER>.md` is in ticker-mode; theme aggregation is a
different document at a different path.
