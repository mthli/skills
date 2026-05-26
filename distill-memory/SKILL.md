---
name: distill-memory
description: >
  Scan a project's `.claude/decisions/**/*.md` and the last month of `MODULE:`-tagged
  commits, then propose 0–3 candidate Claude Code memory entries for the user to review
  one-by-one — the Layer 2 → Layer 3 step in the knowledge loop. Use this skill whenever
  the user says "/distill-memory", "distill memory", "roll up decisions into memory", or
  asks to extract cross-module patterns into memory. This skill should NOT auto-trigger —
  only invoke it when the user explicitly requests it.
---

# Distill Memory

Lift signal out of `.claude/decisions/**/*.md` (and the last ~month of `MODULE:`-tagged
commit Decision blocks) into a small batch of candidate Claude Code memory entries that
the user reviews one-by-one before anything is written.

This skill is the **Layer 2 → Layer 3** step in the knowledge loop:

- **Layer 1** (immutable history): structured `## Decision` blocks inside `git log` commit
  bodies, written by `/commit-context`.
- **Layer 2** (current consensus): `.claude/decisions/<module>.md` per module, refreshed by
  `/distill-module`.
- **Layer 3** (cross-project memory): `~/.claude/projects/<slug>/memory/` — durable,
  user-curated notes whose `MEMORY.md` index is auto-loaded into every future session for
  this project; individual entries are pulled into context when their `description` field
  matches the active task.

The contract is **pull, don't push**. This skill never writes a memory entry the user
hasn't explicitly approved. Layer 3 is precious — its signal is what makes future Claude
sessions feel context-aware instead of amnesiac, and the only way to keep that signal high
is to be aggressively selective. **Zero candidates from a run is a fine outcome.** Don't
pad.

## When to run this

Pull, don't push. Run only when the user asks. Reasonable triggers:

- It's been ~1 month since the last `/distill-memory`, and several modules have had their
  `.claude/decisions/*.md` refreshed in that window.
- The user just finished a big chunk of work that spanned multiple modules and wants the
  cross-module lessons captured.
- A pattern keeps coming up in conversation ("I keep telling you to do X — can we
  remember that?") — though for a one-shot capture like that, writing the memory
  directly is usually faster than running this skill.

If the user invokes the skill without further direction, default to the standard scan
(all decisions, ~30 days of commits). Ask if they want a tighter or wider window.

## Workflow

### 1. Locate the memory directory and existing entries

1. **Slugify the project's absolute path** to find its memory dir. Claude Code's
   convention: take the working directory's absolute path, replace every `/` with `-`.
   The leading `/` becomes a leading `-`. Memory lives at
   `~/.claude/projects/<slug>/memory/`.

   Example: working dir `/Users/matthew/GitHub/gravitty` → slug
   `-Users-matthew-GitHub-gravitty` → memory dir
   `~/.claude/projects/-Users-matthew-GitHub-gravitty/memory/`.

   You can confirm the slug with `pwd | sed 's|/|-|g'`.

2. **Bail if the memory dir doesn't exist.** Tell the user the expected path and stop.
   The Claude Code harness owns the lifecycle of this directory — per the auto-memory
   system prompt, it's expected to already exist whenever memory is active for the
   project. Do **not** `mkdir` it from here. If it's genuinely missing, point the user
   at Claude Code's setup docs rather than improvising; a manually created directory
   may or may not be recognized by the harness, and guessing wrong silently loses memory.

3. **Read the existing memory inventory.** Read `MEMORY.md` (the index) and skim the
   per-entry filenames. Keep two things in working memory throughout the run:
   - The set of existing memory **slugs** (so step 4 can detect collisions).
   - The set of existing **topics covered** (so step 3 doesn't propose a candidate that
     duplicates an entry already on disk).

   If `MEMORY.md` doesn't exist yet, treat the existing-slugs set as empty and warn the
   user that the first candidate they approve will bootstrap it.

### 2. Inventory the inputs

Two independent sources, gathered in the same step:

**Source A — Layer 2 snapshots:**

```bash
find .claude/decisions -type f -name '*.md' -print
```

Read every file. For each, note the module ID (the path minus the `.claude/decisions/`
prefix and `.md` suffix), the `D<n>` active decisions, and any `Superseded` entries.

If `.claude/decisions/` does not exist, this repo hasn't been distilled yet. Mention it
to the user, offer to fall back to commit-only mode (Source B alone), or recommend they
run `/distill-module` on a few modules first. Don't proceed silently — the cross-module
signal that makes this skill valuable comes from comparing snapshots side-by-side.

**Source B — recent commits:**

```bash
git log --all --since="30 days ago" --grep="MODULE:" \
  --pretty=format:'===%H===%n%aI%n%an%n--BODY--%n%B%n--END--%n'
```

Why this format string and not `--pretty=fuller`: `--pretty=fuller` indents bodies by 4
spaces, which silently turns `## Decision` headings into Markdown code blocks. The
sentinel-delimited form emits the body raw (`%B`) so you can split reliably.

The 30-day window is a default — widen if the user asks, or if Source A coverage suggests
relevant decisions land further back. Narrow if the project's commit pace is very high
and 30 days swamps the analysis.

Parse out `MODULE:`, `WHY:`, `CHOSEN:`, `TRADEOFFS:`, `RISKS:`, `SUPERSEDES:` fields per
the `/commit-context` schema. Use commits as a freshness signal — a decision that just
landed but hasn't been distilled into `.claude/decisions/` yet is still candidate
material.

### 3. Generate candidate memories

Now sift through the inventory looking for three specific signals. **Read all three
descriptions before generating anything** — they shape what counts as a candidate.

**Signal 1 — Same pattern recurring across 3+ modules → candidate `project` memory.**

Look for: a design choice, library preference, structural convention, or process step that
shows up nearly identically in three or more `.claude/decisions/*.md` files (or recent
commits tagged for three or more different modules). The "3+ modules" threshold is the
filter — two is a coincidence, three is a pattern worth remembering.

Example shape: "every UI module's decisions mention using view binding instead of Compose;
that's a project-wide stance, not a per-module choice."

**Signal 2 — Repeated category of mistake → candidate `feedback` memory.**

Look for: `RISKS:` fields or `Superseded` entries that describe the same kind of pitfall
recurring across modules — a wrong assumption Claude (or anyone) kept making, a check that
kept getting skipped, an architectural smell that kept slipping in. Or recurring
`SUPERSEDES:` reversals of the same shape ("we keep trying X and rolling back to Y").

This is the highest-value signal because it directly prevents future repetition. But it
also requires the strongest filter: a one-time miscalibration isn't feedback material —
look for ≥2 independent occurrences before flagging.

Example shape: "two modules have decisions that initially extracted a helper class and
later inlined it back; the lesson is to prefer inline duplication over premature
abstraction until 3+ uses exist."

For `feedback` candidates, include both a **Why:** line (the underlying cause / past
incidents) and a **How to apply:** line (when this guidance should kick in). Future-you
needs both to judge edge cases.

**Signal 3 — Cross-module architectural constraint → candidate `project` memory.**

Look for: a decision whose `WHY:` or `TRADEOFFS:` references concerns that span the
codebase — performance budgets, ABI compatibility, threading model, security boundary,
compliance requirement. Even if it appears in only one decision file, if the rationale
clearly binds other modules ("we can't do X because the native layer assumes Y"), it's
worth elevating into a `project` memory so future Claude sessions touching the *other*
modules see it.

This is the only signal that can fire from a single source decision, because the
"crossness" of the constraint is the filter, not the count.

**Hard filter — drop any candidate that falls into "what NOT to save":**

The auto-memory system prompt enumerates content categories that should *never* become
memory entries, even when the user explicitly asks. Apply this filter before showing
candidates to the user:

- **Code patterns, conventions, architecture, file paths, or project structure** that are
  derivable by reading the current project state. (If a future Claude session can answer
  the question by reading the code, don't put it in memory.)
- **Git history, recent changes, who-changed-what.** `git log` / `git blame` are
  authoritative.
- **Debugging solutions or fix recipes.** The fix is in the code; the commit message has
  the context.
- **Anything already documented in `CLAUDE.md`.** Read `CLAUDE.md` once at the start of
  step 3 and skip any candidate that restates content already there.
- **Ephemeral task state**: in-progress work, current conversation context.

A signal that survives all three filters and the "what NOT to save" check is a real
candidate. **If nothing survives, the right number of candidates is 0** — tell the user
that explicitly and stop. Do not invent borderline candidates to fill a quota.

**Cap the batch at 3.** If you have more than 3 surviving candidates, rank by leverage
and present only the top 3. Default ranking when other things are equal:
**Signal 2 (recurring mistake) > Signal 3 (cross-module constraint) > Signal 1
(cross-module pattern)** — preventing repeated errors changes future behavior more than
remembering a constraint, which in turn changes more than recording a shared convention.
Override this ordering if a specific lower-tier candidate is obviously more load-bearing
(e.g., a Signal 1 pattern that touches every module beats a Signal 2 mistake that's
already mostly understood). Tell the user you trimmed and offer to re-rank.

### 4. Show the user, one candidate at a time

For each candidate, present **the full draft entry** the user would be approving, plus
the signal that triggered it. Format:

```
Candidate <i> of <N> — type: <user|feedback|project|reference>

Signal: <one-sentence summary of why this is a candidate, citing source modules / commits>

Proposed slug: <kebab-case-slug>
Proposed MEMORY.md line: - [<Title>](<slug>.md) — <one-line hook>

Proposed file content:
---
name: <slug>
description: <one-line summary used for future relevance matching>
metadata:
  type: <user|feedback|project|reference>
---

<body — for feedback/project, lead with rule/fact, then **Why:** and **How to apply:** lines.
Link related memories with [[other-slug]]. Keep tight; memory is precious.>
```

Then ask the user one of four things, per candidate:

- **Approve as-is** → mark for write in step 5.
- **Edit** → apply the user's edits, re-show, ask again.
- **Skip** → drop this candidate, move on. Don't argue.
- **Update existing instead** → if the user points at an existing memory this overlaps
  with, switch from "create new" to "edit existing": read the existing file, propose a
  merged version, ask for approval.

**Slug collision check (mandatory before showing each candidate):** look up the proposed
slug in the existing-slugs set from step 1. If it collides:
- If the existing entry is on a clearly different topic, propose a new slug.
- If it's on the same topic, default to **Update existing instead** and present the merged
  draft, not a new file.

Never silently overwrite. Never auto-rename to `slug-2`.

### 5. Write the approved entries

For each candidate the user approved in step 4:

1. **Write the per-entry file** to `~/.claude/projects/<slug-of-cwd>/memory/<slug>.md`
   with the `Write` tool, using the exact frontmatter + body format shown in step 4.
2. **Append (or update) the pointer in `MEMORY.md`.** One line per entry, under ~150
   chars, format: `- [<Title>](<slug>.md) — <one-line hook>`. Do not write a frontmatter
   block in `MEMORY.md` itself — it is a plain index. If `MEMORY.md` doesn't exist yet,
   create it as a plain markdown file with just the new pointer line.
3. If this was an **Update existing instead** flow, edit the existing file in place:
   write the merged body the user approved in step 4, preserve the frontmatter and slug,
   and update the `MEMORY.md` line if the hook changed.

After all approved entries are written, report back:

> "Wrote <N> memory entries to `<memory-dir>`. <M> candidates were skipped. The
> `MEMORY.md` index is auto-loaded in every future session for this project, and each
> entry will be pulled into context when its `description` field matches the active task."

Then stop. Don't suggest follow-ups. Don't `git add` anything (memory lives outside the
repo).

## Things this skill must not do

- **Never write a memory entry without explicit per-candidate approval.** The
  user-review gate in step 4 is mandatory. "Batch approve all" is OK if the user
  explicitly says so, but each candidate must still be *shown* before that approval is
  given — the user is auditing your proposals, not your intentions.
- **Never modify `.claude/decisions/*.md` files or commits.** This skill only reads them.
  If a decision looks wrong, surface that as an observation, and let the user fix it
  through `/commit-context` + `/distill-module`.
- **Never `mkdir` the memory directory.** If it's missing, bail with the expected path
  and point the user at Claude Code's setup docs. The harness owns this directory's
  lifecycle; a manually created one may not be recognized and silently loses memory.
- **Never invent borderline candidates to fill a 3-slot quota.** Zero is a valid
  outcome and signals a healthy filter.
- **Never propose memory content that violates the auto-memory "what NOT to save"
  rules.** Code patterns derivable from the repo, git history summaries, debugging
  recipes, `CLAUDE.md` restatements, and ephemeral task state are all out of bounds —
  the user may ask anyway, but the right move is to explain why it doesn't belong and
  suggest where it does (commit body, `.claude/decisions/`, `CLAUDE.md`).
- **Never silently overwrite an existing memory.** Slug collisions trigger an explicit
  update-vs-new prompt.
- **Never auto-trigger.** Run only on explicit user invocation.

## Edge cases

**`.claude/decisions/` doesn't exist.** Mention it; offer commit-only mode (Source B
alone), but warn the cross-module signal is much weaker without snapshots. Suggest the
user run `/distill-module` on the most-active modules first.

**Memory directory doesn't exist.** Bail with the expected path. Don't create it.

**`MEMORY.md` doesn't exist but the memory directory does.** Treat the existing-slugs set
as empty for collision detection. Bootstrap `MEMORY.md` on the first approved write.

**No commits in the last 30 days.** Source A (snapshots) alone is fine as long as the
snapshots themselves are recent. If both are stale, tell the user the loop hasn't
generated new material since the last distill and stop.

**A candidate would partially overlap an existing memory.** Default to update-existing
(merge), not new-file. Present the merged version for approval.

**The user invokes the skill but the project has only one module with decisions.**
Signals 1 and 2 require multi-module evidence and will produce nothing. Signal 3 can
still fire from a single-module decision if the rationale is genuinely cross-cutting.
Tell the user what you found and don't reach.

**An approved candidate's content links via `[[other-slug]]` to a memory that doesn't
exist yet.** That's fine — per the auto-memory conventions, an unresolved `[[name]]` is a
forward marker for a memory worth writing later, not an error. Leave it as-is.

**The user asks to "just write all of them, I trust you."** Show each candidate anyway
(briefly), then write. The "show" step is the audit trail — it ensures the user actually
saw what's going into memory, which is the safeguard against memory drift over many runs.

**Working tree has uncommitted changes.** Fine — this skill only reads `git log` and
`.claude/decisions/`, and writes outside the repo.

## Why this format

Memory is the most expensive layer to keep healthy because the `MEMORY.md` index is
always-loaded — every line sits in every future session's context, and lines past the
cap get silently truncated, so bloat costs real entries their slot in the index. The
per-entry files are pulled in by description-match rather than always-on, but a noisy or
misleading `description` field still costs every future session a wasted relevance check.
The five-step workflow (locate → inventory → generate → review-each → write) exists to
enforce one property: **a high human-judgment-per-write ratio**.

The three-signal heuristic is deliberately narrow:

- **Cross-module pattern (≥3 occurrences)** — the count threshold filters coincidence.
- **Recurring mistake (≥2 occurrences)** — failure patterns have the highest leverage but
  also the highest false-positive rate, so the count threshold matters even more.
- **Cross-module constraint (single decision, but binding on others)** — count of 1 is
  allowed only because the "crossness" is itself the filter.

Other potentially interesting signals (one-off insights, novel architectural ideas,
beautiful refactors) are deliberately excluded. They belong in commit bodies and
`.claude/decisions/`, not in memory. Memory is for things future-you will be **grateful
to be reminded of** at the start of an unrelated session — that's a much narrower bar
than "interesting."

The per-candidate review gate exists because the cost of a bad memory entry is borne by
every future session in this project, while the cost of an extra moment to confirm is
borne by one human, once. The asymmetry justifies the friction.
