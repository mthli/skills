---
name: distill-module
description: >
  Roll up `MODULE: <id>` Decision blocks from `git log` into a per-module
  `.claude/decisions/<id>.md` snapshot — the current-consensus view that complements the
  immutable commit history. Use this skill whenever the user says "/distill-module",
  "distill module decisions", "refresh the decisions file", or asks to consolidate a
  module's recent decisions. This skill should NOT auto-trigger — only invoke it when the
  user explicitly requests it.
---

# Distill Module

Convert the stream of `MODULE: <id>` Decision blocks scattered across `git log` commit bodies
into a single, current-consensus `.claude/decisions/<id>.md` for one module. Future readers
(and future Claude sessions reading the `Knowledge Loop Conventions` block in `CLAUDE.md`)
read this snapshot before touching the module, instead of paging through 50 commits.

This skill is the **Layer 1 → Layer 2** step in the knowledge loop:

- **Layer 1** (immutable history): structured Decision blocks inside `git log` commit bodies,
  written by `/commit-context`.
- **Layer 2** (current consensus): `.claude/decisions/<id>.md` per module — an active doc that
  gets re-distilled on a weekly-ish cadence.

`git log` is the **source of truth**. `.claude/decisions/<id>.md` is a derived, regenerable
view. This skill must never modify, amend, rebase, or `git commit --amend` existing commits —
the fix for a wrong decision is always a **new** commit that `SUPERSEDES` the old one.

## When to run this

Pull, don't push. Run only when the user asks. Reasonable triggers:

- A module has accumulated 5+ new Decision blocks since the last distill (check the
  `Last distilled` line at the top of an existing `.claude/decisions/<id>.md`, or count
  `git log --grep="MODULE: <id>" --since="<date>"`).
- The user shipped a coherent chunk of work and wants the snapshot refreshed.
- A new contributor is joining the module and needs the digest.

If the user invokes the skill without a target module, ask. Don't guess.

## Workflow

### 1. Identify the target module

1. **Read the registry**: `.claude/MODULES.md` at the repo root.
   - **Missing** → tell the user "this repo has no module registry yet — run `/commit-context`
     once to bootstrap one", then stop. Don't create the registry from here; module-set
     mutations belong to a single workflow.
2. **Pick the target module**:
   - If the user named one, look it up in the registry. If it isn't there, list the available
     IDs and ask. Don't auto-create IDs.
   - If the user said "the next due module" or similar, list candidates with their
     pending-Decision counts (see step 2's grep) and let them choose.
3. **Resolve the destination path**: always `.claude/decisions/<id>.md` at the repo root, with
   `/` in the module ID preserved as a subdirectory separator (e.g., `module/key` →
   `.claude/decisions/module/key.md`, `native/jni` → `.claude/decisions/native/jni.md`,
   `tracking` → `.claude/decisions/tracking.md`). Create intermediate directories as needed.
   Both structural and cross-cutting modules live here — one fixed location keeps the
   `CLAUDE.md` read-rule simple and avoids `.md` files sprinkled through the source tree.
4. **Read the existing destination if any**. If `.claude/decisions/<id>.md` exists from a
   prior distill, read it now and keep it in working memory — its `D<n>` IDs drive ID reuse
   (step 4) and its content drives the diff-against-existing draft (step 5). A fresh distill
   without this read will silently renumber IDs that other commits may already reference via
   `SUPERSEDES:`.

### 2. Mine the commits

Pull every commit body that tagged this module. **Don't pre-filter by path** — the
`MODULE: <id>` tag is the authoritative index, paths are noisy (a `tracking` decision can land
in any file; structural-module decisions sometimes get tagged from sibling dirs during refactors).

```bash
git log --all --grep="MODULE: <id>" \
  --pretty=format:'===%H===%n%aI%n%an%n--BODY--%n%B%n--END--%n'
```

Why this format string (not `--pretty=fuller`): `--pretty=fuller` indents commit bodies by
4 spaces, which silently turns every `## Decision` heading into a Markdown code block and
breaks naive parsers. The format above emits the body raw (`%B`) with explicit `===<sha>===`
and `--BODY--` / `--END--` sentinels you can split on without surprise.

- `--all` covers all refs, not just the current branch — picks up decisions that lived on
  feature branches that already merged. **Caveat**: it *also* picks up un-merged feature
  branches with WIP decisions that aren't shipped on main. If the user wants strict
  "what's in main", swap `--all` for the relevant branch name (e.g., `git log main --grep=...`).
  Default to `--all` and mention the alternative if you spot suspicious-looking branch-only
  decisions during review.
- `%aI` is ISO 8601 strict (timezone-aware), good for sorting chronologically.

For each commit, extract the body (everything after the subject line) and find `## Decision`
H2 blocks. Each block has these fields (per `/commit-context`'s schema):

| Field | Required? | Notes |
| --- | --- | --- |
| `MODULE` | yes | match against `<id>`; skip blocks tagged for other modules in the same commit |
| `WHY` | yes | motivation |
| `ALTERNATIVES` | yes | options considered (used at distill review time, not in the final snapshot) |
| `CHOSEN` | yes | what was picked |
| `TRADEOFFS` | yes | costs of CHOSEN |
| `RISKS` | yes | what could go wrong / what to watch |
| `SUPERSEDES` | optional | summary or `D<n>` ID of the prior decision being overridden |

If a commit body has **malformed** Decision blocks (missing required fields, wrong nesting,
unparseable formatting), surface them as **warnings** in the user-review step (step 6). Don't
silently skip — the user may want to know their commit data has gaps and fix the next
distillation's input by writing better commits going forward.

### 3. Resolve SUPERSEDES chains

Sort the parsed decisions by **commit date ascending**. Walk forward:

- If decision D has `SUPERSEDES: X`, try to resolve X against:
  1. A `D<n>` ID from an existing `.claude/decisions/<id>.md` at the destination (most common
     — the prior distill assigned the ID).
  2. A previous decision's `CHOSEN` summary (substring or fuzzy match).
- On match, mark the prior decision as superseded by D.
- A decision can be superseded multiple times across iterations — only the latest in the chain
  is "Active". The earlier ones live in the `Superseded` section.
- **Ambiguous SUPERSEDES** (matches multiple candidates): surface as a warning, let the user
  pick at review time.
- **Unresolvable SUPERSEDES** (matches nothing): surface as a warning. Don't fail the
  distillation; the user may want to accept the gap or edit the draft.

### 4. Cluster and assign IDs

Group active (non-superseded) decisions by topic. Heuristics, in order of strength:

1. Same SUPERSEDES chain → already collapsed in step 3.
2. Substantial overlap in `CHOSEN` / `WHY` text — probably the same decision restated. No
   strict threshold; "substantial" is a judgment call. When you're unsure, leave them as
   separate clusters and surface the candidate-merge as a warning in step 6 so the user can
   decide.
3. Same files repeatedly modified across the source commits (`git show --name-only <sha>`).

Within each cluster, the **latest** commit's decision wins for the "What/Why/Tradeoffs/Watch
out" content; all contributing commits go in `Source`.

Assign stable IDs `D1`, `D2`, …:

- **Existing destination file** (already read in step 1.4): **reuse its IDs**, matching by
  SUPERSEDES chain or content similarity. This is non-negotiable — IDs are referenced by
  `SUPERSEDES:` in future commits, so they must not shift across re-distills.
- **New file**: assign `D1, D2, …` by **topic-first-seen order** — the topic whose *earliest
  contributing commit* is oldest becomes `D1`, the next-earliest topic is `D2`, and so on.
  (Not strict commit-first-seen — that would interleave topics and make the document
  unreadable. Cluster first, then sort the clusters by their oldest member.)
- **Newly-added decisions** (re-distill case): get the next free ID after the existing ones.

### 5. Draft the snapshot

Template — fill in placeholders, don't include the `<>` brackets:

```markdown
# <Module display name> Decisions

> Snapshot of current consensus. Evolution: `git log --grep="MODULE: <id>"`
> Last distilled: <YYYY-MM-DD> (HEAD = <short-sha>)

## Active

### D1: <short title — paraphrase the CHOSEN, under ~60 chars>
- **What**: <one tight sentence — how we do it now>
- **Why**: <one tight sentence — the WHY from the latest contributing commit>
- **Tradeoffs**: <one tight sentence — from TRADEOFFS>
- **Watch out**: <one tight sentence — from RISKS>
- **Source**: <abbrev-sha-1>, <abbrev-sha-2>, …

### D2: …

## Superseded

- ~~<short title of old decision>~~ → replaced by **D1** in <abbrev-sha> (<YYYY-MM-DD>)
- ~~<…>~~ → replaced by **D3** in <abbrev-sha> (<YYYY-MM-DD>)
```

Drafting rules:

- **Paraphrase, don't quote**. `What` / `Why` / `Tradeoffs` / `Watch out` are one-sentence
  digests. Readers want the gist; they can `git show <sha>` for the full commit body.
- **`ALTERNATIVES` is intentionally dropped from the snapshot**. It mattered when *making* the
  decision but bloats the *current state* view. The `Source` shas are the escape hatch — anyone
  curious about alternatives reads the original commit.
- **List every contributing commit** in `Source`. That's the link back to the immutable
  history.
- **Active before Superseded.** Active is what people read; Superseded is the appendix.
- **Omit Superseded entirely** if empty — don't leave a blank section.
- **Diff against the existing file** if there is one. Only surface what changed (new Active
  entries, newly-Superseded entries, edited existing entries). Don't rewrite unchanged sections.

### 6. Show the user

Present the draft (or, if the destination exists, the diff against it) for review. Explicitly
call out:

- New active decisions added since the previous distill
- Active decisions that just moved to Superseded
- **Warnings** collected along the way:
  - Malformed Decision blocks in commits (with shas)
  - Ambiguous `SUPERSEDES` targets — ask the user to pick
  - Unresolvable `SUPERSEDES` — ask the user whether to accept the gap or edit
  - Clusters the heuristic was unsure about
- The destination path and whether it's a create or an update

Ask: "Write this to `<path>`?"

Three responses to handle:

- **Yes** → proceed to step 7.
- **Edits** → apply the user's edits to the draft, re-show, ask again.
- **No / abort** → drop the draft. Don't write anything. Leave the workspace as you found it.

### 7. Write

Write the file with the `Write` tool. **Do not** `git add`, `git commit`, or push. The user
will commit it themselves — most likely via `/commit-context`, which will produce its own
Decision block tagged with the module being distilled (the distillation *is* about that
module's current state, so it's the coherent tag — and it's guaranteed to be in
`.claude/MODULES.md`, unlike speculative tags like `docs`).

After writing, surface the path and tell them:

> "Open `<path>` to review, then commit when ready. If your `CLAUDE.md` has the centralized
> `Knowledge Loop Conventions` read-rule (`.claude/decisions/<module>.md`), future sessions
> editing this module will load this file before touching code. If your read-rule still
> points at `<module-dir>/DECISIONS.md` (the pre-centralization shape), update it — or run
> `/commit-context` once to refresh the bootstrap."

## Things this skill must not do

- **Never modify commits.** No `--amend`, no `rebase -i`, no `commit --fixup`. The git log is
  the source of truth. If a commit's Decision is wrong, the fix is a **new** commit that
  `SUPERSEDES` it.
- **Never delete history.** The `Superseded` section in `.claude/decisions/<id>.md` is a
  derived view; the raw data still lives in `git log`. Don't try to "clean up" by dropping
  commits or rewriting their Decision blocks.
- **Never auto-create module IDs.** If the user names a module not in `.claude/MODULES.md`,
  send them to `/commit-context` to register it. Module-set mutations live in one place.
- **Never silently swallow malformed Decision blocks.** Surface them as warnings so the user
  knows their commit data has gaps.
- **Never write the file without explicit user approval.** The user-review gate in step 6 is
  mandatory; this is a destructive write to a tracked file.

## Edge cases

**No commits found for the module.** Tell the user, suggest checking the spelling
(`git log --grep="MODULE:" --pretty=format:'%h %s' | head -50` shows every tag that's been
used). Stop without writing.

**Decisions for a different module that touched this module's files.** Expected during
cross-module refactors. Don't pull them in — they belong in the sibling module's
`.claude/decisions/<sibling-id>.md`. The `MODULE:` tag is authoritative, not the file paths.

**The same decision verbatim across multiple commits** (cherry-picks, merges, rebases).
Deduplicate by content similarity in step 4; cite all source commits.

**The destination file was hand-edited.** Use the diff-against-existing approach in step 5
so you don't clobber manual annotations. If a hand-edited entry conflicts with the freshly
distilled version, flag it at review time — don't silently overwrite.

**A SUPERSEDES target can't be resolved.** Don't fail. Surface a warning ("D3 supersedes 'old
auth wrapper' but no prior decision in this module matches") and let the user decide.

**Working tree has uncommitted changes.** Fine — this skill only reads `git log`. The user
may want the freshly-written `.claude/decisions/<id>.md` to land alongside other in-flight
work, which is why step 7 doesn't auto-stage anything.

**An older `<module-dir>/DECISIONS.md` exists from before the centralization.** Treat it as
a hand-edited prior snapshot: read it during step 5, fold its content in, then write the new
distillation to `.claude/decisions/<id>.md`. Don't delete the old file from this skill — but
in the user-review step (step 6), mention that they should `git rm <module-dir>/DECISIONS.md`
in their next commit, since leaving both files alive violates the "one fixed location"
principle and will confuse future readers about which is canonical.

## Why this format

The 5-field active entry (`What` / `Why` / `Tradeoffs` / `Watch out` / `Source`) is a
deliberate compression of the 6-field commit Decision. The mapping:

- `CHOSEN` → `What`
- `WHY` → `Why`
- `TRADEOFFS` → `Tradeoffs`
- `RISKS` → `Watch out`
- All contributing shas → `Source`
- `ALTERNATIVES` → **dropped**; reachable via `git show <sha>`

`ALTERNATIVES` is load-bearing when *deciding* but noise when *reading current state*. Keeping
the snapshot short is what makes it readable; the `Source` line preserves the escape hatch to
the full reasoning.
