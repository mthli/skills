---
name: commit-invest
description: >
  Distill an investment discussion into one or more structured blocks (Thesis / Observation /
  Macro / Lesson / Methodology), append them to per-ticker or per-topic files in an investment-notes repo, and
  commit with a rich message that preserves the full conversation. Use this skill whenever the
  user says "/commit-invest", "commit invest", "commit this discussion", or asks to persist an
  investment conversation into the notes repo. This skill should NOT auto-trigger — only invoke
  it when the user explicitly requests it.
---

# Commit Invest

Take the current investment conversation and turn it into durable, structured records:

- One or more **append-only blocks** written to per-ticker `positions/<TICKER>.md` files (or to
  `macro.md` / `methodology.md` / `postmortems.md` when no ticker is the subject).
- A **rich commit message** that preserves the conversation log and structured blocks so that
  `/distill-ticker` and `/distill-theme` can later mine them mechanically from `git log`.

Unlike `/commit-context` for code, the **conversation is the artifact** — there is no diff to
anchor on. The "what changed" is the new block you write; the "why" is the conversation that
produced the view.

## When to run this

Pull, don't push. Run only when the user explicitly asks (e.g., "/commit-invest", "save this
discussion"). Don't auto-fire after every chat — the user decides when a discussion has
solidified enough to commit.

**Repeat invocations in the same session**: if the user runs `/commit-invest` more than once
in a single conversation (commit, discuss more, commit again), each invocation only captures
material from *after the prior commit*. The marker is the assistant turn where you last ran
`git commit -F /tmp/commit-invest-msg.txt` — everything in the dialog after that point is
new material; everything before it is already journaled. If you can't clearly identify a
prior commit in this session, treat the whole conversation as fresh material.

If the working directory isn't an investment-notes repo (no `positions/`, no
`.claude/THEMES.md`, no `CLAUDE.md` mentioning the Investment Knowledge Loop), tell the user and
stop. This skill is scoped to dedicated investment-notes repos, not general code repos.

## Workflow

### 1. Sanity-check the repo

Run:

```bash
git status
git log --oneline -5
```

The working tree is normally clean before invoking this skill — there's no code editing flow
to leave behind half-staged files. If you see uncommitted changes, surface them and ask the
user whether to include them, abort, or proceed alongside (rare; usually a sign something is
off).

If `.git` doesn't exist, tell the user this isn't a git repo and stop.

### 2. Identify block type(s) and target file(s)

Read back through the conversation. Sort the content into one or more of these block types,
each of which routes to a specific destination file:

| Block type      | When it fits                                                       | Target file                  |
| --------------- | ------------------------------------------------------------------ | ---------------------------- |
| **Thesis**      | A held view on a specific ticker: long / short / watch / avoid / exit | `positions/<TICKER>.md`     |
| **Observation** | A data point or pattern noted on a ticker, not yet a thesis        | `positions/<TICKER>.md`     |
| **Macro**       | A market / regime / policy view, no specific ticker                | `macro.md`                   |
| **Lesson**      | Post-trade reflection (what worked, what didn't, general takeaway) | `postmortems.md`             |
| **Methodology** | A process / discipline rule for yourself (sizing, journaling, etc.) | `methodology.md`            |

**A single conversation can produce multiple blocks across multiple files.** Example: a
discussion that lands a NVDA long thesis, an AAPL observation, and a macro view on Fed
liquidity → three blocks, three files, one commit.

**When the block type is ambiguous, ask — don't guess.** The five types overlap in real
discussions; the user's intent decides. Common ambiguities and the disambiguating question:

- **Thesis vs Observation** — "Is this a view you'd act on if conditions were right (Thesis),
  or a data point you're tracking (Observation)?"
- **Thesis vs Macro** — a sector or theme view that doesn't name a specific ticker is
  typically Macro, even if it implies positioning. "Is there a specific ticker you'd
  express this through (Thesis), or is this a broader market call (Macro)?"
- **Lesson vs Methodology** — Lesson is anchored in a specific trade or event ("I lost on
  the TSLA short because…"). Methodology is a forward-looking rule abstracted from one or
  more lessons ("from now on, no naked shorts in momentum names"). "Is this about a
  specific event you're reflecting on (Lesson), or a rule you want to apply going forward
  (Methodology)?"
- **Macro vs Methodology** — Macro is a time-bound view on the regime (it can be wrong and
  superseded). Methodology is a durable process rule about how you operate. "Will this view
  expire when the regime changes (Macro), or is it a rule you'd hold across regimes
  (Methodology)?"

A single discussion can produce blocks of multiple types — a Lesson often pairs naturally
with a Methodology, and a Macro shift often pairs with revised Theses on affected names.
Don't force everything into one type.

### 3. Load and validate the theme registry

Every Thesis and Observation block tags one or more **themes** (e.g., `ai-infra`, `defense`,
`fed-policy`). Themes are the lateral axis that lets `/distill-theme` aggregate views across
tickers. They live in `.claude/THEMES.md` and must be registered before use.

1. **Read `.claude/THEMES.md`.**
   - **Exists** → parse the registered theme IDs (lines like `` - `<theme-id>` — description ``
     under any H2 section). Treat these as the legal theme set.
   - **Missing** → tell the user "this repo has no theme registry yet" and offer to bootstrap
     one. Infer 1–3 candidate themes from the current conversation; show the draft; write only
     on approval. If they decline, omit `THEMES:` from this commit's blocks (don't invent
     freelance themes — they'd never get distilled). The **one-time CLAUDE.md read-rules
     hook** at the end of this step fires once after a successful registry bootstrap.

2. **Map the conversation's themes** to the registered set.
   - Exact-match registered IDs → use them in the block's `THEMES:` field.
   - **Unregistered theme** → reverse-question the user:
     *"This discussion mentions `<theme>` but it's not in `.claude/THEMES.md`. Add it to the
     registry?"*
   - Naming constraints (reject and re-ask if violated, **do not auto-fix**):
     - ASCII only, lowercase, hyphens for word separation (e.g., `ai-infra`, `fed-policy`)
     - no mixed case, no spaces, no non-ASCII characters
   - Mention the current registry size; aim to keep it to ~15–25 entries. A bloated theme
     registry erodes the value of `/distill-theme`.
   - On approval, append the new entry to `.claude/THEMES.md` and `git add` that file so it
     ships with this commit.

3. **Macro / Methodology / Lesson blocks don't need themes** — they're already filed under
   topical files. Skip the theme check for those.

**One-time setup hook: CLAUDE.md read-rules.** Fires **only** in the same `/commit-invest`
invocation that just bootstrapped `.claude/THEMES.md`. Ask one separate yes/no question:

*"Also install the Investment Knowledge Loop read-rules into the project's `CLAUDE.md` so
future sessions actually load the relevant position and theme snapshots before discussion?"*

The draft block to inject (along with language-handling rules — what to translate and what
to keep verbatim) lives in `references/claude_md_template.md`. Read that file when this
branch fires, show the user the block, tell them whether it will be appended to an existing
`<repo-root>/CLAUDE.md` or used to create a new one, and write only on explicit approval.

After this bootstrap moment — whether approved or declined — **never raise this question
again for this repo**. Heuristic: once `.claude/THEMES.md` exists, the bootstrap moment is
over.

### 4. Compose the blocks

For each block identified in step 2, build it according to the per-type schema. **All field
names are ALL CAPS** and use the exact `- KEY: value` prefix — `/distill-*` parses these
mechanically.

The date in the H2 heading is today's date in ISO format (YYYY-MM-DD). Get it from the current
session context, not by guessing.

#### Thesis block (goes in `positions/<TICKER>.md`)

```markdown
## YYYY-MM-DD — Thesis

- TICKER: <SYMBOL>
- THEMES: <theme-1>, <theme-2>
- STANCE: long | short | watch | avoid | exit
- HORIZON: days | weeks | months | years
- THESIS: <one paragraph — the why, in plain language>
- CATALYSTS: <what events / data prints would make this play out>
- INVALIDATION: <what signal says you're wrong; when to exit>
- CONVICTION: low | med | high
- ENTRY_PRICE: <optional — leave blank if not entered>
- EXIT_PRICE: <optional — fill on close>
- SUPERSEDES: <optional — "YYYY-MM-DD (commit <short-sha>)" of the prior block being replaced>

### Conversation Log
- User: <key turn>
- Assistant: <key turn>
- ...
```

#### Observation block (goes in `positions/<TICKER>.md`)

```markdown
## YYYY-MM-DD — Observation

- TICKER: <SYMBOL>
- THEMES: <theme-1>, <theme-2>
- WHAT: <one-line description of the observation>
- WHY_IT_MATTERS: <why this is worth tracking>
- RELATED_THEMES: <if it bears on themes beyond the ticker's primary tags>
- RELATED_TICKERS: <if it cross-implicates other tickers>

### Conversation Log
- User: ...
- Assistant: ...
```

#### Macro block (goes in `macro.md`)

```markdown
## YYYY-MM-DD — Macro

- REGIME: <one-line regime label — e.g., "late-cycle tightening", "post-pivot easing">
- DRIVERS: <what's driving the regime call — Fed, fiscal, geopolitics, etc.>
- IMPLICATIONS: <what this means for positioning — sectors favored/avoided>
- RELATED_THEMES: <optional — comma-separated theme IDs from .claude/THEMES.md that this regime call bears on>
- HORIZON: weeks | months | quarters | years
- CONVICTION: low | med | high
- SUPERSEDES: <optional>

### Conversation Log
- User: ...
- Assistant: ...
```

`RELATED_THEMES:` is optional but **strongly preferred** when the Macro view bears on any
registered themes — without it, `/distill-theme` has to fuzzy-match the Macro prose and
will often miss the connection. If the regime call genuinely applies to no registered
themes (e.g., pure FX or rate-curve commentary), omit the field.

#### Lesson block (goes in `postmortems.md`)

```markdown
## YYYY-MM-DD — Lesson

- TICKER: <SYMBOL, if a specific trade — else omit>
- WHAT_HAPPENED: <one paragraph — the trade or event being reflected on>
- WHAT_WORKED: <what played out as expected>
- WHAT_DIDNT: <what surprised you, what you got wrong>
- GENERAL_LESSON: <the durable takeaway — the thing future-you should remember>

### Conversation Log
- User: ...
- Assistant: ...
```

#### Methodology block (goes in `methodology.md`)

```markdown
## YYYY-MM-DD — Methodology

- RULE: <one-line rule or principle>
- WHY: <the reasoning — usually a past incident or recurring failure mode>
- HOW_TO_APPLY: <when this rule kicks in, what it changes about behavior>
- SUPERSEDES: <optional>

### Conversation Log
- User: ...
- Assistant: ...
```

**Conversation log guidelines** (the conversation-log subsection in every block):

- **Subheading is always `### Conversation Log` in English** — treat it as a structural
  label like the ALL CAPS field keys (`TICKER:`, `THEMES:`, etc.), not translated content.
  This keeps `grep "### Conversation Log"` reliable across multi-language journals and
  removes a per-file language judgment call.
- **Body content follows the user's working language** — match whatever language the
  existing journal file already uses (run a quick `head` on `positions/<TICKER>.md` if
  unsure); for brand-new files, match the language the user has been speaking in the
  current session. Field values (THESIS, CATALYSTS, INVALIDATION, etc.) follow the same
  rule. Structural labels (the H1, the H2 dated headings, the `### Conversation Log`
  subheading, and the ALL CAPS field keys) stay English regardless.
- Extract **key dialog turns**, not every message verbatim. Aim for ~5–15 lines. Skip pure
  tool-call noise and redundant back-and-forth — focus on intent, decisions, and turning
  points.
- **Per-block scope for multi-ticker commits**: each per-block conversation log is filtered
  to the dialog turns relevant to *that block's* subject. If the discussion of NVDA and the
  discussion of AAPL were largely separate, the NVDA block log should contain only the NVDA
  turns. If the discussion was genuinely joint (same arguments applied to both), the logs
  may overlap substantially — that's fine. The commit-level log (in the commit message) is
  comprehensive and threads them together chronologically; the per-block logs are scoped.

**SUPERSEDES discipline**: if this block updates or reverses a prior view, fill `SUPERSEDES:`
with the date and short commit hash of the prior block. To find the prior commit:

```bash
git log --oneline -- positions/<TICKER>.md
```

The date in `SUPERSEDES:` should match the H2 heading of the prior block; the hash is the
commit that introduced it.

### 5. Append blocks to target files

For each block, append it to its target file. If the target file doesn't exist yet, create
it.

**Append, never overwrite.** This is an append-only journal. If you find yourself wanting to
edit an existing block, the right move is a new block with `SUPERSEDES:` pointing at the old
one — not editing history.

**File structure**:

Each journal file uses a top-level H1 as its subject anchor, then one or more dated blocks
beneath it as H2 headings, separated by `---` horizontal rules:

```markdown
# <subject>

## YYYY-MM-DD — Thesis
- TICKER: ...
...

---

## YYYY-MM-DD — Thesis
- TICKER: ...
...
```

The H1 subject by file:
- `positions/<TICKER>.md` → `# <TICKER>` (e.g., `# ARM`, `# NVDA`, `# BRK-B`)
- `macro.md` → `# Macro`
- `methodology.md` → `# Methodology`
- `postmortems.md` → `# Postmortems`

The `---` is a between-block separator, NOT part of the block itself. The first block in a
file has no `---` above it — the H1 provides the structural break.

**Mechanics**:

- **Brand-new files** — use `Write` with `# <subject>\n\n` + new block. No leading `---`
  — the H1 is the only thing above the first block. Critically, this avoids the dangling
  YAML-frontmatter ambiguity that markdown renderers (GitHub, Obsidian, Hugo) hit when a
  file starts with `---`.
- **Small journals (under ~500 lines)** — use the `Read` tool to load the file, then `Write`
  the full new contents: existing content + `\n\n---\n` + new block. The `\n\n---\n`
  produces the blank-line + horizontal-rule separator above the new block's H2. Make sure
  the existing content ends with a single trailing newline before concatenating; otherwise
  you'll get only one blank line where you want one.
- **Larger journals** — use the `Edit` tool to insert at end of file. Pass the last existing
  block's trailing line as `old_string` and `old_string + "\n\n---\n" + new_block` as
  `new_string`. This avoids re-sending the whole file to the model on every append.

For multi-block / multi-ticker commits, append all blocks in step 5 before running the
consistency check (step 6) and composing the commit message (step 8) — that way the check
sees every file change at once and the commit covers all of them atomically.

### 6. Self-consistency check

Before pulling token usage and composing the commit, re-read every block you just appended
and scan for self-inconsistencies. Numbers in journal blocks are load-bearing — they're
what `/distill-*` surfaces as the "current view" months from now, and they shape entry /
exit decisions. A typo that survives this skill corrupts the journal until someone catches
it by hand.

For each just-appended block, scan three severity tiers:

**🔴 Internal numeric inconsistency** (must fix before committing):

- Same indicator appears as different numbers within one block. E.g.: THESIS says
  `\$20B order` but the same block's Conversation Log says `\$2B` and `\$2 billion` —
  the structured field is the lone outlier against two agreeing log mentions.
- Same date written differently across fields (e.g., `2026-07-29` in CATALYSTS vs
  `2026-06-29` in the Conversation Log).
- Ticker mismatch (TICKER field vs prose / Conversation Log).
- Percentages / ratios / multipliers that contradict (`2× royalty` in one sentence,
  `3× royalty` in another).

**🟡 Plausibility flags** (surface to user, don't auto-block):

- A single-source large claim worth corroborating before persisting — these have a high
  typo / hallucination rate. Two signals that warrant a flag: the figure is `≥ 2× the
  company's annual revenue` (e.g., `\$X B order book`), or it's attributed to one specific
  event ("the May X earnings call") with no corroborating mention anywhere else in the
  conversation.
- An earnings date / catalyst date already in the past, written as upcoming.
- ENTRY_PRICE / EXIT_PRICE / current-price levels that conflict with the ticker's recent
  trading range as discussed in the conversation.

**🟢 Polish** (surface but don't block):

- Inconsistent unit conventions (`\$200B` vs `\$200bn` vs `\$200 billion` used
  interchangeably without anchoring on one).
- Minor formatting drift (one block uses `—` em-dash separators, the next uses ` - `).

**Output format**: list findings per file + block, severity-tagged. If zero 🔴 and zero
🟡, proceed silently to step 7. Otherwise surface them in chat like:

```
Self-consistency check found:

- 🔴 positions/ARM.md (2026-05-29 Thesis): THESIS says "\$20B order" but
  Conversation Log says "\$2B" and "\$2 billion" — the structured field disagrees
  with both log mentions. Likely correct: \$2B, based on internal majority + plausibility.
- 🟡 positions/ARM.md (2026-05-29 Thesis): "\$20B order" represents ~4× ARM's
  annual revenue — single-source claim, worth corroborating against the May 6
  earnings transcript before persisting.
```

Then wait for the user to direct: "fix all 🔴, ignore 🟡", "I've verified, proceed", or
specific picks. After applying fixes (**in-place edits** to the just-appended blocks, NOT
new SUPERSEDES blocks — these are typos caught before commit, not view changes), re-run
the scan on the corrected content. Once clean, proceed to step 7.

**Escape hatch**: if the user says "skip the check" / "just commit" / similar, proceed
directly to step 7. This is for rapid logging of known-correct material where the check
overhead isn't worth it.

**Why this is in the workflow rather than a separate `/review-iterate` invocation**: typo /
inconsistency detection on freshly-written journal content is a one-shot need with a narrow
checklist. Building it inline means future-you doesn't have to remember to chain
`/review-iterate` before `/commit-invest` — the protection is automatic, and the failure
mode this catches (Conversation Log says one thing, THESIS says another) is exactly the
kind of error that's invisible from outside the just-written content.

### 7. Retrieve token usage with ccusage

Use [`ccusage`](https://github.com/ryoppippi/ccusage) to get the **current session's** token
usage. This is identical to the `/commit-context` flow; the same constraints apply.

> **No pipes.** Each step below is a single Bash command. Claude does the string/JSON
> transforms itself between steps. This keeps every call matching a stable Bash allowlist
> prefix so the user is not re-prompted for permission.

#### Step 7.1: Get the working directory

```bash
pwd
```

Then **Claude transforms the path itself**: replace every `/`, `.`, `_`, and space with `-` to
derive `PROJECT_ID`. Example: `/Users/me/GitHub/invest-notes` →
`-Users-me-GitHub-invest-notes`.

#### Step 7.2: List session files for this project

```bash
ls -t ~/.claude/projects/<PROJECT_ID>/
```

The first `.jsonl` filename is the current session. Claude parses the output and strips the
`.jsonl` suffix to get `SESSION_UUID`.

#### Step 7.3: Query token usage for this session

```bash
npx ccusage claude session -i "<SESSION_UUID>" --json -O --no-color
```

Parse the JSON in Claude (do not pipe to `python3` or `jq`). Sum across `entries`:

- `Input tokens` = sum of `inputTokens`
- `Output tokens` = sum of `outputTokens`
- `Cache read tokens` = sum of `cacheReadTokens`
- `Cache creation tokens` = sum of `cacheCreationTokens`
- `Total tokens` = `totalTokens` (fall back to the four sums above)
- `Total cost` = `totalCost`, formatted as USD with 4 decimal places (e.g. `$0.1234`). Omit if
  missing or `0`.
- `Models used` = sorted unique `model` values across entries

If any step fails (ccusage not installed, no `.jsonl` found, JSON empty), **omit the
`# Token Usage` section entirely** from the commit message — no placeholder.

### 8. Write the commit message

The full commit message format:

```
invest(<scope>): <summary under 72 chars>

<body: 2-5 lines on what was discussed and the upshot. Imperative mood.
The structured blocks below carry the details — don't duplicate them here.>

---

# Conversation Log

- User: <key user request or question>
- Assistant: <key response, action taken, or decision made>
- User: <follow-up or clarification>
- Assistant: <what was done next>
...

# Blocks

## Block 1
- TYPE: Thesis | Observation | Macro | Lesson | Methodology
- TARGET_FILE: positions/<TICKER>.md (or macro.md / postmortems.md / methodology.md)
- <full block fields per the schema in step 4, ALL CAPS keys, one per line>

## Block 2
- ...

# Files Modified

- <file path> — <one-line semantic description of what was appended>
- ...

# Token Usage (only include if ccusage succeeds)

- Input tokens: <inputTokens>
- Output tokens: <outputTokens>
- Cache read tokens: <cacheReadTokens>
- Cache creation tokens: <cacheCreationTokens>
- Total tokens: <totalTokens>
- Total cost: <totalCost as USD, e.g. $0.1234 — omit if missing or 0>
- Models used: <modelsUsed>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

The `Co-Authored-By` trailer must be the **last line** of the commit message. Match the
model name to the actual model running the session (check the environment block — e.g.
`Claude Opus 4.8 (1M context)`, `Claude Sonnet 4.6`, etc.) rather than copying the literal
above.

#### Type and scope rules

The type is always `invest`. The scope encodes what's being committed:

- **Ticker(s)**: `invest(NVDA)` for a single ticker, `invest(NVDA,AAPL)` for multiple. Comma-
  separated, no spaces. Use this even if the commit *also* contains a macro or methodology
  block — the tickers stay in the scope, and the extra block types are visible in the
  `# Blocks` section.
- **`invest(macro)`** when the commit is pure macro (no ticker blocks).
- **`invest(methodology)`** when pure methodology.
- **`invest(postmortem)`** when pure postmortem.
- **Mixed pure-keyword cases** (e.g., a macro + a methodology, no tickers):
  `invest(macro,methodology)`.

Summary line rules:

- Imperative mood ("add", "revisit", not "added" / "adds")
- No period at the end
- Under 72 characters total (including the `invest(<scope>): ` prefix)

Body guidelines:

- Focus on *the discussion's upshot* — what was decided / concluded — in 2–5 lines
- Don't restate the structured fields; the `# Blocks` section already has them
- Use imperative mood

#### Blocks section format rules

These exact rules let `/distill-ticker` and `/distill-theme` parse mechanically. Keep them
strict:

- Each block is a level-2 heading (`## Block N`).
- Always include `TYPE:` and `TARGET_FILE:` as the first two fields — `/distill-*` uses them
  to route.
- Then list every field from the per-type schema in step 4, ALL CAPS, one per line, exact
  prefix `- KEY: `.
- `TICKER:` and `THEMES:` values must exactly match registered identifiers (the ticker as
  used in the filename, the theme(s) from `.claude/THEMES.md`).
- A value may span multiple lines, but the next field must start with `- KEY: ` on its own
  line.
- `SUPERSEDES:` is optional and only present when this block overrides a prior one.

#### Conversation log guidelines (commit-level)

Same shape as the per-block conversation log, but at the commit level it summarizes the
**whole session**, not just one block's contribution. For a single-block commit they end up
nearly identical; for multi-block commits the commit-level log threads them together
chronologically.

**Scrub sensitive information** — if API keys, broker credentials, account numbers, or
personally identifiable financial info appeared, omit or redact them. Investment notes are
generally less risky than code (no secrets *should* be in there), but err on the side of
caution.

### 9. Commit

Write the message to a temp file with the `Write` tool, then pass it to `git commit -F`.

1. Use the `Write` tool to write the full commit message to `/tmp/commit-invest-msg.txt`. Do
   not wrap the message in quotes or any other delimiter.
2. Stage all the files you appended to or created in step 5 (the positions files,
   `.claude/THEMES.md` if you bootstrapped or updated it, `CLAUDE.md` if you bootstrapped
   read-rules). Use explicit paths, not `git add -A`:

```bash
git add positions/NVDA.md positions/AAPL.md
```

3. Commit and clean up in a single chained Bash call so the temp file is always removed,
   even if the commit fails:

```bash
git commit -F /tmp/commit-invest-msg.txt; rm -f /tmp/commit-invest-msg.txt
```

The `;` (not `&&`) ensures `rm` runs whether the commit succeeded or not. After this step,
show `git log -1 --stat` so the user sees the final commit.

## Things this skill must not do

- **Never force-push or amend a previous commit** unless the user explicitly asks. The
  immutable journal is the whole point — superseded views go in *new* commits with
  `SUPERSEDES:`, not by rewriting history.
- **Never edit existing blocks in positions files.** Append-only. If a view changes, write a
  new block with `SUPERSEDES:` pointing at the old one. Editing in place erases the
  reasoning trail.
- **Never invent themes** — unregistered themes must go through the reverse-question flow in
  step 3.2. Silent freelance themes never get distilled and corrupt the cross-ticker view.
- **Never silently rewrite a malformed theme name** to satisfy the naming constraints — go
  back and ask the user. The constraints exist so distillation is mechanical; auto-fixing
  hides the friction that motivates better names.
- **Never `git add -A`.** Always stage explicit paths. An investment-notes repo may contain
  hand-edited drafts the user isn't ready to commit; sweeping everything in defeats their
  workflow.
- **Never commit secrets.** Broker keys, account numbers, real position sizes if the user
  doesn't want them recorded — omit. Investment notes is a journal, not a positions ledger.
- **Never auto-trigger.** Run only on explicit user invocation. The user decides when a
  discussion has solidified.
- **Never touch `CLAUDE.md` after the one-shot bootstrap.** Once `.claude/THEMES.md` exists,
  this skill's CLAUDE.md responsibilities are over. The skill's job is writing commits, not
  managing project config.

## Edge cases

**The conversation produced no clear view** (general market chat, half-formed musings).
Tell the user: "I don't see a crystallized thesis / observation / macro view in this
discussion. Want to keep talking, or commit it as an Observation with `WHAT: open question`
to log the thread?" Don't force a thesis out of mud.

**Multiple tickers but only one has a real thesis** (the others were just mentioned in
passing). Only write a block for the ticker that got real discussion. Mentions don't deserve
files. Use `RELATED_TICKERS:` on the main block if the others bear on the thesis.

**The conversation revises a prior view from earlier in the same session.** Write a single
block reflecting the final view; don't write two blocks "for completeness". The dialog log
will show the evolution. `SUPERSEDES:` only applies across commits, not within a session.

**The conversation includes a position close** (user mentions they exited). Add `EXIT_PRICE:`
to a new Thesis block with `STANCE: exit`, and `SUPERSEDES:` pointing at the prior open
thesis. Also propose a Lesson block for `postmortems.md` — closed positions are the highest-
leverage moments to capture a `GENERAL_LESSON:`.

**Working tree has unrelated uncommitted changes** (rare in this repo type). Show them to
the user and ask whether to include, abort, or proceed alongside. Default to abort if
unclear — investment notes commits should be tightly scoped to the discussion.

**Ticker symbol has unusual characters** (e.g., `BRK.B`, `0700.HK`). Use the symbol as-is in
the `TICKER:` field. For the file path, replace `.` with `-` (`positions/BRK-B.md`,
`positions/0700-HK.md`) since `.` in filenames can interact badly with tooling. Be
consistent — once a ticker has a file, subsequent commits must use the same filename.

**The user wants to back-date a block** (e.g., "this view actually crystallized last week").
Use the user-supplied date in the H2 heading, but record today's date in the commit's
metadata (git handles that automatically). Don't argue — the journal date is what the user
remembers, the commit date is what `git log` shows.

## Why this format

**Append-only per-ticker files** give you `git log -- positions/NVDA.md` as a complete
chronological view of every view ever held on NVDA. That's the unit of context loading for
future discussions — `tail -200 positions/NVDA.md` puts the recent history directly in front
of the model before forming new views.

**Structured ALL-CAPS fields** make `/distill-ticker` and `/distill-theme` mechanical. They
grep for `TICKER:`, `THEMES:`, `STANCE:` and collapse the journal into a current-consensus
snapshot without needing the parser to understand natural language. The format is friction
on the writing side so distillation can be cheap on the reading side.

**The commit log duplicates the block content** so that distillation can choose its source:
`/distill-ticker` mostly reads the positions file (faster, cleaner), but `/distill-theme`
benefits from `git log --grep=THEMES:.*ai-infra` to scan across all tickers at once. Both
paths work because both layers hold the same structured data.

**INVALIDATION is mandatory by convention, not by enforcement.** A Thesis without an exit
trigger is a story you'll talk yourself into holding forever. The CLAUDE.md read-rules
remind future-you to fill it; this skill won't refuse to commit if you skip it, but it
should nudge.

**SUPERSEDES is the linking primitive.** It's how `/distill-ticker` knows which old blocks
are still live and which have been overruled. Without it, distillation can't tell "I changed
my mind" from "I have two contradictory views". When in doubt about whether to add it, add
it.
