---
name: distill-module
description: >
  Roll up `MODULE: <id>` Decision blocks from `git log` into a per-module
  `.claude/decisions/<id>.md` snapshot, the current-consensus view that complements the
  immutable commit history. Use this skill whenever the user says "/distill-module",
  "distill module decisions", "refresh the decisions file", or asks to consolidate a
  module's recent decisions. This skill should NOT auto-trigger; only invoke it when the
  user explicitly requests it.
---

# Distill Module

Distill immutable Decision records from `git log` into one reviewable snapshot for a registered
module. Treat Git history as the source of truth and `.claude/decisions/<id>.md` as a derived view.
Future sessions read that snapshot before touching the module, through the `Knowledge Loop
Conventions` block in `CLAUDE.md`, instead of paging through dozens of commits.

Never amend, rebase, delete, or otherwise rewrite commits. Record corrections in new commits with
`SUPERSEDES`.

## Identify the module

1. Run from the repository root.
2. Read `.claude/MODULES.md`.
   - Do not silently create the registry when it is missing.
   - Stop and tell the user to use `/commit-context` to create the registry; module-set mutations
     belong to one workflow.
3. Resolve the target:
   - Use an exact registered ID when the user supplied one.
   - If the ID is unknown, list the registered IDs and ask the user to choose.
   - If no target was supplied, ask for one instead of guessing.
   - For requests such as "the next due module," calculate pending Decision counts and let the
     user choose from the candidates.
4. Map the ID to `.claude/decisions/<id>.md`, preserving `/` as a subdirectory separator. For
   example, map `native/jni` to `.claude/decisions/native/jni.md`.
5. Read the existing destination before mining history. Preserve its `D<n>` identifiers and manual
   annotations; a fresh distill without this read silently renumbers IDs that other commits may
   already reference via `SUPERSEDES`.

## Mine Decision blocks

Collect every commit whose body mentions the target tag. Do not pre-filter by changed path because
`MODULE` is the authoritative index.

```bash
git log --all --fixed-strings --grep="MODULE: <id>" \
  --pretty=format:'===%H===%n%aI%n%an%n--BODY--%n%B%n--END--%n'
```

Use `--all` by default. If branch-only work appears suspicious, note that the user can request a
specific integration branch instead. Use the explicit sentinels rather than `--pretty=fuller`, which
indents bodies and can obscure Markdown headings.

For each commit:

1. Split the record on the sentinels.
2. Find each `## Decision` block.
3. Parse these fields:

| Field | Requirement | Purpose |
| --- | --- | --- |
| `MODULE` | Required | Match exactly to the target ID |
| `WHY` | Required | Capture motivation |
| `ALTERNATIVES` | Required | Inform review; omit from the snapshot |
| `CHOSEN` | Required | Capture the selected approach |
| `TRADEOFFS` | Required | Capture accepted costs |
| `RISKS` | Required | Capture monitoring concerns |
| `SUPERSEDES` | Optional | Link to a replaced decision |

Skip well-formed blocks for other modules in the same commit. Retain malformed target blocks as
warnings with their commit hashes; never discard them silently.

If no matching decisions remain, report that result and stop without writing. Suggest:

```bash
git log --all --fixed-strings --grep="MODULE:" --pretty=format:'%h %s'
```

## Resolve supersession

Sort parsed decisions by author date ascending. Walk forward and resolve each `SUPERSEDES` value
against:

1. A `D<n>` entry from the existing snapshot.
2. A prior decision's `CHOSEN` text using conservative substring or semantic matching.

Mark a matched predecessor as superseded. Keep only the latest member of a supersession chain
active.

Do not guess when multiple targets match or no target matches. Preserve the decision as unresolved
and add a warning for user review.

## Cluster decisions and preserve IDs

Group active decisions by topic using these signals in order:

1. Membership in the same supersession chain.
2. Strong overlap in `CHOSEN` and `WHY`.
3. Repeatedly modified files from `git show --name-only <sha>`.

When uncertain, keep decisions separate and report the possible merge as a warning. Deduplicate
cherry-picked or repeated blocks by content while retaining every source hash.

Use the latest decision in a cluster for `What`, `Why`, `Tradeoffs`, and `Watch out`. List every
contributing commit under `Source`.

Assign stable IDs:

- Reuse matching `D<n>` IDs from an existing snapshot.
- Treat IDs in both `Constraint index (compressed active decisions)` and `Active (full detail)` as
  current decisions when resolving `SUPERSEDES` and reusing IDs. Snapshots split under the earlier
  wording still carry the headings `Constraint index (compressed decisions)` and `Active`; treat
  them as the same two sections and do not rename them as part of a minimal diff.
- Never renumber an existing ID.
- For a new snapshot, order clusters by their earliest contributing commit and assign `D1`, `D2`,
  and so on.
- For new clusters in an existing snapshot, continue after the highest assigned ID.

## Draft the snapshot

For an uncompressed snapshot, use this shape:

```markdown
# <Module display name> Decisions

> Snapshot of current consensus. Evolution: `git log --grep="MODULE: <id>"`
> Last distilled: <YYYY-MM-DD> (HEAD = <short-sha>)

## Active

### D1: <short paraphrased title>

- **What**: <current approach in one sentence>
- **Why**: <motivation in one sentence>
- **Tradeoffs**: <accepted costs in one sentence>
- **Watch out**: <risks in one sentence>
- **Source**: <abbrev-sha-1>, <abbrev-sha-2>

## Superseded

- ~~<old decision>~~ → replaced by **D1** in <abbrev-sha> (<YYYY-MM-DD>)
```

Apply these rules:

- Paraphrase instead of quoting commit bodies.
- Keep active fields to one concise sentence each.
- Omit `ALTERNATIVES`; preserve access to them through source hashes.
- List all contributing hashes.
- Put current decisions before Superseded: when compression is present, place `Constraint index
  (compressed active decisions)` before `Active (full detail)` so readers see settled constraints
  before nuanced entries.
- Omit the Superseded section when empty.
- Preserve non-conflicting manual annotations.
- For an existing destination, prepare a minimal diff and avoid rewriting unchanged entries.
- Flag conflicts between manual annotations and newly distilled content.

## Compress oversized snapshots

When a snapshot grows past roughly 30 full-detail active entries, or is otherwise too long to serve
as a practical pre-edit briefing, propose a constraint-index split during review:

- Insert `## Constraint index (compressed active decisions)` before `## Active (full detail)`. Move
  settled decisions there as one-line bullets shaped like
  `- **D<n>** — <surviving constraint, trap, or contract> (<source-sha>)`.
- Keep IDs unchanged. Indexed decisions remain active and continue to participate in ID reuse and
  `SUPERSEDES` resolution; the two sections together are the complete current consensus, while
  `Active (full detail)` contains the recent, in-flight, or nuanced entries that still need full
  detail.
- Do not compress an entry when an actionable qualification or manual annotation cannot be
  preserved safely in one sentence.
- Keep each compressed batch recoverable from Git. Record a batch-specific reference in the index
  preamble, such as `D1–D20: git show <full-sha>:.claude/decisions/<id>.md`, and retain earlier
  references on later rounds.
- Before proposing compression, verify that the referenced reachable commit contains the full text
  of every entry in that batch. If it does not, leave those entries uncompressed and warn the user.
- Do not create a sibling archive file. The pinned snapshot and per-entry source commits provide
  the detailed history without duplicating stale text in the working tree.

When re-distilling an already split snapshot, append new decisions to `Active (full detail)` and
move them to the index only after they settle. Keep only the latest `Last distilled` line, and
propose another compression round if the full-detail section becomes oversized again.

## Review before writing

Show the full draft for a new file or the proposed diff for an update. Include:

- The exact destination and whether it will be created or updated.
- New active decisions.
- Decisions moving to Superseded.
- Decisions moving to the constraint index and the Git references that recover their full text.
- Malformed blocks and their hashes.
- Ambiguous or unresolved `SUPERSEDES` values.
- Uncertain clusters.

Ask for explicit approval to write the proposed snapshot. Apply requested edits, show the revised
draft or diff, and ask again. If the user declines, discard the draft and leave the workspace
unchanged.

## Write the approved snapshot

Write only the approved content to `.claude/decisions/<id>.md` with the `Write` tool, creating
parent directories when needed. Do not stage, commit, or push.

After writing:

1. Show the destination path.
2. Summarize what changed and repeat any accepted warnings.
3. Tell the user to review the file and commit it when ready, optionally with `/commit-context`,
   whose own Decision block is then tagged with the module being distilled.
4. If the root `CLAUDE.md` does not direct Claude Code to read relevant module maps and decisions,
   mention that `/commit-context` will propose the canonical knowledge-loop rules when the snapshot
   is staged for commit. Do not edit `CLAUDE.md` from this skill.

## Guardrails

- Never modify Git history or create corrective fixup commits.
- Never create or mutate module IDs or registries.
- Never use file paths as a substitute for exact `MODULE` tags.
- Never hide malformed input, ambiguous links, or clustering uncertainty.
- Never overwrite a snapshot without showing the proposed content and receiving approval.
- Never stage, commit, or push.
