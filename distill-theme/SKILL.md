---
name: distill-theme
description: >
  Aggregate every Thesis and Observation tagged with a given theme across all `positions/*.md`
  journal files (and `macro.md` for regime context) into a single theme-level snapshot at
  `.claude/decisions/themes/<theme>.md` — the view that surfaces "what do I think about this
  theme overall, and which names express it". Use this skill whenever the user says
  "/distill-theme", "distill theme", "refresh the snapshot for <theme>", or asks for a
  cross-ticker view on a theme. This skill should NOT auto-trigger — only invoke it when the
  user explicitly requests it.
---

# Distill Theme

Pull every Thesis and Observation block tagged with a given theme (across every per-ticker
journal in `positions/`, plus relevant Macro blocks in `macro.md`) into a single theme-level
snapshot at `.claude/decisions/themes/<theme>.md`. The snapshot answers two questions a
future discussion will routinely ask:

- **What's my overall view on this theme?** (sentiment, what's driving it)
- **Which names express it, and what's my current stance on each?**

This skill is the **cross-ticker** Layer 1 → Layer 2 step, complementary to
`/distill-ticker`'s per-ticker collapse. Both read the same source-of-truth journal layer
but slice it on different axes.

## When to run this

Pull, don't push. Run only when the user asks. Reasonable triggers:

- The user just landed a Thesis or Macro view that bears on a multi-ticker theme and wants
  the theme snapshot refreshed.
- Several tickers have updated their views on the same theme in a short window (e.g., 3+
  ai-infra-tagged blocks in the last month) and the cross-name picture has shifted.
- The user is about to discuss a new candidate name within the theme and wants the current
  theme-level context loaded first.

If the user invokes the skill without a target theme, list the registered themes from
`.claude/THEMES.md` with a block-count per theme (rough freshness signal) and ask. Don't
guess.

## Workflow

### 1. Identify and validate the target theme

1. **Read `.claude/THEMES.md`.**
   - **Missing** → tell the user "no theme registry yet — run `/commit-invest` once to
     bootstrap one", then stop. Theme creation lives in one workflow.
   - **Exists** → parse the registered theme IDs.
2. **Confirm the target theme is registered.**
   - If the user supplied a theme not in the registry, list the available themes and ask.
     Don't auto-create.
   - Theme IDs are ASCII, lowercase, hyphen-separated (e.g., `ai-infra`, `fed-policy`).
3. **Resolve the destination path**: `.claude/decisions/themes/<theme>.md` at the repo
   root. Create the intermediate `.claude/decisions/themes/` directory as needed.
4. **Read the existing destination if any.** If the snapshot already exists from a prior
   distill, read it for the diff in step 5 and for the "Last distilled" timestamp.

### 2. Mine the journals for blocks tagged with this theme

The lateral aggregation is what makes this skill distinct from `/distill-ticker`. Pull
blocks two ways and combine:

**Path A — scan per-ticker journal files** (most reliable, includes uncommitted blocks):

```bash
grep -lE "THEMES:.*\b<theme>\b" positions/*.md
```

The `\b` word boundaries are load-bearing — without them, grepping for `ai` would
falsely match `ai-infra`, `defense-ai`, etc., silently aggregating the wrong tickers
into the wrong theme.

For each candidate file, parse it and pull every block whose `THEMES:` field includes
the target theme as an **exact comma-separated token**. The safe extraction is: split
the `THEMES:` value on `,`, strip whitespace, exact-match against `<theme>`. Don't trust
the grep result alone — grep just filters candidate files; per-block exact-match happens
during parsing.

Per block, record: ticker, date, type (Thesis / Observation), and all relevant fields.

**Path B — scan macro.md for Macro blocks tagged with `RELATED_THEMES:`** including this
theme. Macro blocks written by `/commit-invest` include an optional `RELATED_THEMES:` field
listing the registered theme IDs the regime call bears on:

```bash
grep -nE "RELATED_THEMES:.*\b<theme>\b" macro.md
```

For each match, find the enclosing `## YYYY-MM-DD — Macro` block (scan backward from the
matched line) and parse its full field set. Apply the same exact comma-token match during
parsing — don't trust grep alone.

**Fallback for old Macro blocks** written before `RELATED_THEMES:` was introduced (or
written by hand without the field): they won't be picked up by the grep above. If the user
specifically asks for older Macro context, scan `macro.md` for blocks where the prose
(IMPLICATIONS / DRIVERS) names the theme, and flag those in step 6 for the user to confirm
relevance. Default behavior is mechanical match only — don't fuzzy-scan unless asked.

**Path C — optional fallback to git log** if the user wants to surface views that have
been superseded out of the live journal but might still inform the theme view:

```bash
git log --all -E --grep="THEMES:.*\b<theme>\b" \
  --pretty=format:'===%H===%n%aI%n%an%n--BODY--%n%B%n--END--%n'
```

The `-E` (extended regex) plus `\b` boundaries prevent the same false-positive bug as
Path A. Then per-block, apply the same exact comma-token match during parsing — don't
trust grep alone.

Default to path A (and B). Only invoke path C if path A returns very little or the user
asks for the full historical view.

**Why this format string** (Path C): `--pretty=fuller` indents commit bodies by 4 spaces,
which silently turns the ALL-CAPS field lines into Markdown code blocks and breaks naive
parsing. The format above emits the body raw (`%B`) with `===<sha>===` and `--BODY--` /
`--END--` sentinels you can split on without surprise.

For each parsed block, record: source ticker (from filename or commit), date, type, and all
relevant fields. Tag every block with which path it came from so step 5 can prioritize
correctly (live journal > git history).

**Sha resolution for Path A / Path B blocks** (needed for "Recent updates" feed in step 5,
which cites `(commit <short-sha>)`): journal blocks parsed from markdown files don't carry
a hash directly. Resolve by running:

```bash
git log --oneline -- positions/<TICKER>.md
```

(or `git log --oneline -- macro.md` for Path B). Match each parsed block's H2 date against
the commit log; the first commit whose date matches the block's date is the originating
commit. **If multiple commits share the same date for the same file** (e.g., two
`/commit-invest`s on the same day each appended a block to `positions/NVDA.md`),
disambiguate by `git show <sha>` and matching the block's TICKER / STANCE / THESIS content
against the commit body. If you still can't disambiguate, accept the latest matching
commit and flag in step 6. If a block has no matching commit (uncommitted journal edits,
or hand-edited content), record `<uncommitted>` instead of a sha — don't hallucinate.
Surface the `<uncommitted>` count in step 6 as a warning so the user knows some "recent
updates" point at unsaved changes.

### 3. Resolve currency for each ticker

For each ticker that appears in the parsed set, you need its **current view on this theme**
— not every view ever held. Two passes:

1. **Use the per-ticker snapshot** at `.claude/decisions/positions/<TICKER>.md` if it
   exists *and is current*. Compare the snapshot's `Last distilled` date to the journal's
   latest block date — you already parsed the journal in step 2's Path A, so reuse those
   parsed blocks rather than re-reading the file. The most recent block's date is the
   latest journal entry.
   - **Snapshot is current** (no journal blocks newer than `Last distilled`): trust it.
     Read STANCE, HORIZON, CONVICTION directly.
   - **Snapshot is stale** (journal has newer blocks): fall back to parsing the journal
     for the latest Thesis. Note the staleness for the warnings list in step 6.
   - **Snapshot doesn't exist**: parse the journal for the latest Thesis. **Don't run
     `/distill-ticker` from here** — it's a separate workflow.
2. **Filter the parsed blocks for this ticker** to the latest Thesis tagged with this
   theme. That's the per-ticker contribution to the theme view.
   - If a ticker only has Observation blocks for the theme (no Thesis), the ticker still
     appears in the "Names exposed" section but with `Stance: watching (no thesis)`.

A ticker whose latest Thesis on this theme has `STANCE: exit` and is recent is a **closed
position on the theme**. Surface it separately in step 4's snapshot — closed positions
within an active theme are signal about the theme's evolution.

### 4. Synthesize the theme view

Walk the parsed blocks and per-ticker currencies to derive:

**Theme stance** — the high-level posture. Use judgment, not just mode-counting. Weight
each active Thesis by CONVICTION (high > med > low) and let HORIZON inform whether
short-term and long-term views should be reconciled or kept distinct. Examples:

- 5 long + 1 watch → `Net long`
- 3 long med-conviction + 2 short low-conviction → `Net long` (conviction-weighted)
- 1 long high-conviction + 3 watching low-conviction → `Long with high conviction, others
  watching` (the high-conviction view dominates, but flag the cautious posture)
- 2 long months + 2 short days → `Long-term constructive, short-term cautious` (horizons
  diverge — don't collapse into "mixed")
- 2 long + 2 short same horizon, same conviction → `Mixed / pair-trade setup`
- 0 active Theses, 5 observations → `Watching, no positions`

Keep it to one line — a single sentence, qualitative. Two clauses joined by a comma
(e.g., "Long-term constructive, short-term cautious") still count as one line and are the
right shape when horizons genuinely diverge. The examples are illustrative — don't
mechanically pick mode when conviction or horizon clearly tilts the read.

**Drivers** — what's making this theme interesting right now, as a tight bullet list. Pull
from:
- The latest Macro blocks tagged with this theme (their DRIVERS field)
- The most common CATALYSTS across active Theses for this theme

**Risks / what could go wrong** — bullet list. Pull from:
- Reversal scenarios implicit in the latest Macro blocks' DRIVERS
- The most common INVALIDATION triggers across active Theses for this theme

**Drivers / Risks vs Macro context**: Drivers and Risks are extracted bullets, optimized
for scanning. Macro context (further down the template) is a 2-4 line narrative gloss on
the *regime backdrop* — how the macro environment frames the theme. They share source
material (the Macro blocks) but serve different reading purposes. Avoid duplicating exact
phrases between them; the bullets are the "what to watch", the narrative is the "what's
going on".

**Names exposed** — every ticker with at least one block tagged with this theme. Group by
current stance:
- Long
- Short
- Watching (Observations only, or Thesis with `STANCE: watch`)
- Closed (latest Thesis is `STANCE: exit`)

**Recent updates** — last 5 blocks tagged with this theme across all tickers, in reverse
chronological order. This is the "what's new since last distill" feed.

### 5. Draft the snapshot

Template — fill in placeholders, don't include the `<>` brackets:

```markdown
# <theme> — Theme Snapshot

> Cross-ticker view on `<theme>`. Journals: `positions/*.md`, `macro.md`
> Last distilled: <YYYY-MM-DD>

## Theme stance

<One line — e.g., "Net long, high conviction" or "Mixed, watching for resolution">

## Drivers

- <driver 1 — what's pushing this theme forward>
- <driver 2>
...

## Risks / invalidation

- <risk 1 — what would reverse the theme view>
- <risk 2>
...

## Names exposed

### Long
- **<TICKER>** (<HORIZON>, <CONVICTION>) — <one-line gist of the per-ticker thesis on this theme>
- ...

### Short
- **<TICKER>** (<HORIZON>, <CONVICTION>) — <one-line gist>

### Watching
- **<TICKER>** — <one-line on why it's tracked>

### Closed
- **<TICKER>** — exited <YYYY-MM-DD>, <one-line on outcome / why closed>

## Macro context

<If any Macro blocks were included via Path B, summarize them in 2-4 lines here.
Omit this section entirely if no Macro blocks apply.>

## Recent updates

<Last 5 theme-tagged blocks across all tickers, reverse chronological. Omit if empty.>

- <YYYY-MM-DD>: <TICKER> — <block type> — <one-line summary> (commit <short-sha>)
```

Drafting rules:

- **One line per ticker in "Names exposed"**. The reader is doing cross-name scanning, not
  reading individual theses. Anyone who wants the full thesis on a name goes to
  `.claude/decisions/positions/<TICKER>.md` (linkable mentally, even if not literally
  hyperlinked).
- **Theme stance is one line, qualitative.** Don't try to be quantitative; the user already
  knows the stances of individual names — what they want here is the gestalt.
- **Omit empty sections entirely.** A theme with no shorts just doesn't have a `### Short`
  subheading.
- **Diff against the existing snapshot** if there is one. Only surface what changed (new
  names exposed, stance changes, new drivers, newly-surfaced risks). Don't rewrite
  unchanged sections — that erases hand edits and produces noisy diffs.
- **"Macro context" is short** — 2-4 lines. The full Macro block lives in `macro.md`; this
  is just the theme-relevant excerpt.

### 6. Show the user

Present the draft (or diff against the existing snapshot) for review. Explicitly call out:

- The current theme stance and whether it changed
- New names that joined the theme since last distill
- Stance changes within existing names (e.g., a ticker that moved from Long → Watching)
- **Warnings**:
  - Tickers whose latest Theses on this theme contradict each other (rare, but worth
    surfacing — e.g., one Thesis says "long for ai-infra reasons", another says "short on
    ai-infra capex unwind")
  - Macro blocks pulled in via soft prose-match (Path B) — ask the user to confirm
    relevance
  - Blocks parsed from `git log` (Path C) that don't appear in the current journal files
    (could indicate hand-edited journals or git history out of sync with working tree)
- The destination path and whether it's a create or an update

Ask: "Write this to `<path>`?"

Three responses to handle:

- **Yes** → proceed to step 7.
- **Edits** → apply user edits, re-show, ask again.
- **No / abort** → drop the draft.

### 7. Write

Write the file with the `Write` tool. **Do not** `git add`, `git commit`, or push. The user
commits it themselves.

After writing, surface the path and tell them:

> "Wrote theme snapshot to `<path>`. Future investment discussions touching names tagged
> with `<theme>` will load this file via the Investment Knowledge Loop read-rules.
> `git add` and commit when ready."

## Things this skill must not do

- **Never modify journal files (`positions/*.md`, `macro.md`).** They're append-only. If a
  block is wrong, the user fixes it via `/commit-invest` with a new block + `SUPERSEDES:`.
- **Never modify commits.** No `--amend`, no `rebase`.
- **Never auto-create theme IDs.** If the user names a theme not in `.claude/THEMES.md`,
  send them to `/commit-invest` to register it. Theme-set mutations live in one place.
- **Never run `/distill-ticker` from inside this skill** to bring snapshots up to date.
  That's a separate workflow with its own user-review gate. If a per-ticker snapshot is
  missing or stale, fall back to parsing the journal directly and mention the staleness in
  step 6.
- **Never silently swallow malformed blocks.** Surface them as warnings.
- **Never write the snapshot without explicit user approval.** Step 6 is mandatory.
- **Never auto-trigger.** Run only on explicit user invocation.

## Edge cases

**Theme is registered but has zero matching blocks.** Tell the user: "no blocks tagged with
`<theme>` yet." Suggest either grepping for related themes (in case the registration name
diverges from how the theme has been informally referenced) or accepting that the theme is
still aspirational. Don't write an empty snapshot.

**Theme has only Observations, no Theses.** Write the snapshot with `Theme stance:
Watching, no positions formed`. The Observations + Macro context are valuable on their own
as a pre-thesis context layer.

**A ticker has a Thesis tagged with multiple themes** (e.g., NVDA tagged with both
`ai-infra` and `semis`). The thesis appears in **both** themes' snapshots. That's by
design — themes are non-exclusive lenses on the same underlying view.

**The latest per-ticker view contradicts the latest theme-level Macro view** (e.g., Macro
says "ai-infra bubble", but the latest NVDA thesis is `STANCE: long high-conviction`).
Surface this as a warning in step 6 — it's exactly the kind of contradiction the user
benefits from seeing before forming new views.

**A ticker's only block on this theme has been SUPERSEDED.** If the latest Thesis on the
ticker is on a different theme entirely (the ticker "moved off" the theme), don't list it
in "Names exposed". If the latest Thesis is still on the same theme but with a different
stance, use the latest. If the ticker has no remaining live view on the theme, optionally
list under Closed with "no longer tagged <theme>".

**`.claude/decisions/positions/<TICKER>.md` exists but is stale relative to the journal.**
Mention it in step 6 ("the per-ticker snapshot for AAPL is from 3 months ago, while the
journal has 4 newer blocks — you may want to run `/distill-ticker AAPL` first"). But don't
block — the per-ticker snapshots are convenience reads; the journals are authoritative.

**The theme snapshot was hand-edited** (user added analyst notes, reordered sections). Use
the diff approach; flag conflicts at review time.

**Working tree has uncommitted changes.** Fine — this skill reads journal files (committed
or not) and writes outside them.

## Why this format

**Theme stance is one line, qualitative.** Quantifying it ("60% long, 40% short, +0.3 net")
would be false precision — the user is forming a judgment, not measuring a portfolio. The
one-line stance is the lowest-friction useful summary; everything else in the snapshot
backs it up.

**"Names exposed" grouped by stance** because that's how the user actually thinks about a
theme: which names am I long, which am I watching, which did I exit. A flat alphabetical
list would hide the structure. Grouping by stance makes the cross-name picture legible at
a glance.

**Macro context as a separate section** because regime calls and per-ticker theses are
different units of analysis. A regime can shift without invalidating individual theses
(and vice versa); separating them lets the reader see whether a name-level concern is
ticker-specific or macro-driven.

**Recent updates as a feed** because the cross-ticker view is the most prone to going
stale. The 5-most-recent updates show, at a glance, whether the theme has been getting
attention or quietly drifting. It's also the natural entry point for "what's new since I
last looked".

**Themes are non-exclusive lenses** — a Thesis tagged with multiple themes legitimately
shows up in multiple theme snapshots. This is by design. Themes are how you slice the
portfolio, not how you partition it. A name can express multiple themes simultaneously,
and the theme snapshots should reflect that.
