---
name: commit-context
description: >
  Commit staged (or unstaged) changes with a git commit message automatically derived from the current
  conversation context. Use this skill whenever the user says "/commit-context", "commit with context",
  "commit context", or asks to commit using the conversation/session context as the commit message.
  This skill should NOT auto-trigger — only invoke it when the user explicitly requests it.
---

# Commit Context

Generate a rich git commit message by combining **what changed** (from `git diff`) with **why it changed**
(from the conversation context — the questions asked, decisions made, bugs discussed, features planned).

A good commit message tells a story that `git log` readers can follow months later. The diff already
shows *what*; this skill's job is to capture the *why* and *how* from the session so future readers
don't have to guess.

## Workflow

### 1. Gather the raw materials

Run these commands to understand the current state of the repo:

```bash
git status
git diff --staged
git log --oneline -5
```

- If there **are** staged changes → use those as the basis for the commit.
- If there are **no** staged changes but there **are** unstaged changes → show the user what's
  unstaged (`git diff --stat`) and ask whether to:
  - Stage everything (`git add -A`)
  - Stage specific files (let the user pick)
  - Abort
- If there are **no changes at all** → tell the user there's nothing to commit and stop.

### 2. Load the module registry & map changes

When the `# Decisions` block in Step 5 is written, every decision must be tagged with a `MODULE` that
exists in `.claude/MODULES.md` — the registry that makes `git log --grep="MODULE: <id>"` mechanical.
(The block may also be skipped entirely; see the conditions below.)

**First check whether Decisions apply at all**. Skip the entire `# Decisions` block (and the rest of
this step) when **any** of the following holds — the prose body in Step 3 still needs one line on
motivation, but no structured decisions:

- The diff is genuinely trivial: pure typo / comment-only / formatting / single-line obvious bug fix.
  Heuristic: ≤5 net lines changed in `git diff --stat`, and (for source-code files) no new imports,
  methods, or classes. For non-code files (markdown, YAML, JSON, configs) just use the line-count
  threshold.
- The repo has no `.claude/MODULES.md` and the user declines to bootstrap one (see below).

**Otherwise, work the registry**:

1. **Look for `.claude/MODULES.md` at the repo root.**
   - **Exists** → parse out the `<id>` entries (lines like `- \`<id>\` — description` under any
     H2 section, typically `## Structural modules` and `## Cross-cutting concerns`). Treat these
     as the legal module set.
   - **Missing** → tell the user "this repo has no module registry yet" and offer to bootstrap one.
     Infer 1–3 candidate modules from the current diff (group by directory + cross-cutting concern)
     and show the draft. Only write the file if the user approves. If they decline, skip the
     Decisions block for this commit (see the rule above). If they approve, the **one-time setup
     hook** at the end of this step will fire after items 2 and 3.

2. **Map the current diff to module(s):**
   - For **structural modules**, match by changed file paths under the module's real directory.
   - For **cross-cutting concerns** (`tracking`, `build`, `dependencies`, etc.), match by semantics
     of the change, not by path.
   - A single commit can touch multiple modules → multiple Decisions.

3. **If no registered module matches**, reverse-question the user:
   *"This change doesn't match any module in `.claude/MODULES.md`. What's the new module called?
   Should I add it to the registry?"*
   - Naming constraints (reject and re-ask if violated, **do not auto-fix**):
     - ASCII only, lowercase, hyphens for word separation, slashes for hierarchy
       (e.g. `live-call/role-dialog`)
     - no mixed case, no spaces, no non-ASCII characters
   - Mention the current registry size; the doc suggests keeping it to 15–25 entries.
   - On approval, append the new entry to `.claude/MODULES.md` and `git add` that file so it
     ships with this commit.

**One-time setup hook: read-rules in `CLAUDE.md`**. Fires **only** in the same `/commit-context`
invocation that just bootstrapped `.claude/MODULES.md` (item 1's "Missing" branch, above). Ask one
separate yes/no question:
*"Also install the read-rules block into the project's `CLAUDE.md` so future sessions actually
consume what gets written?"*

Show the draft block (below), tell the user whether it will be **appended to an existing
`<repo-root>/CLAUDE.md`** or **used to create a new one**, and write only on explicit approval.
After this bootstrap moment — whether the user said yes or no — **never raise this question again
for this repo**. Subsequent `/commit-context` invocations skip it entirely (heuristic: once
`.claude/MODULES.md` exists, the bootstrap moment is over). If the user approved, `git add` the
`CLAUDE.md` change alongside `.claude/MODULES.md` so both ship in this commit.

**Language handling**: if an existing `CLAUDE.md` is in a non-English language, translate the prose
of the draft block to match. But **keep these tokens verbatim** (they're literal identifiers used
by the structured-decisions parser and other tooling — translating them silently breaks future
distillation): `MODULE`, `WHY`, `ALTERNATIVES`, `CHOSEN`, `TRADEOFFS`, `RISKS`, `SUPERSEDES`,
`.claude/decisions/`, `.claude/MODULES.md`, `/commit-context`, `git log`, `git show`, and any
field name in ALL CAPS.

**Indentation note**: the fenced block below is shown at the document's left margin. The actual
content injected into `CLAUDE.md` has **no leading whitespace** — paste it flush-left.

Draft block (use a top-level H2 so it doesn't collide with existing sections):

````markdown
## Knowledge Loop Conventions

### Before editing code

1. Read `.claude/decisions/<module>.md` for any module(s) your change touches. Module IDs
   live in `.claude/MODULES.md`; `/` in IDs maps to subdirectories (e.g., `module/key` →
   `.claude/decisions/module/key.md`).
2. Run `git log --oneline -10 -- <path>` for the file(s) you're about to change.
3. If recent commits contain `MODULE: <current module>` blocks, `git show` those bodies.

### After finishing a task

- When using `/commit-context`, fill all six fields in each Decision block — don't skip
  `ALTERNATIVES` or `RISKS`.
- If this change overrides a prior decision in `.claude/decisions/`, add `SUPERSEDES:` to the
  new Decision.
````

### 3. Synthesize the commit message

Look back through the conversation and identify:

- **What task** the user was working on (bug fix, new feature, refactor, docs, tests, config, etc.)
- **Problems encountered and solved** (e.g. "race condition in the cache layer")
- **The motivation** — why the change was needed

Key decisions / tradeoffs / alternatives belong in the structured `# Decisions` block (Step 5),
**not** in the prose body. Don't duplicate them here.

Combine this with the actual diff to produce a commit message in this format:

```
<type>(<optional-scope>): <summary under 72 chars>

<body: 2-5 lines on the task and motivation — what was being worked on and why.
Imperative mood. Don't list specific decisions/tradeoffs here — those live in
the structured # Decisions block.>
```

**Type** follows Conventional Commits: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`,
`perf`, `ci`, `build`.

**Scope** is optional — use it when the change clearly belongs to one module/component.

**Summary line rules**:
- Imperative mood ("add", not "added" or "adds")
- No period at the end
- Under 72 characters total (including the `type(scope): ` prefix)

**Body guidelines**:
- Focus on *why* the change was made, not *what* files were touched (the diff handles that)
- Decisions, tradeoffs, alternatives → go in `# Decisions` (Step 5), not here. Don't duplicate.
- Length: 2-5 lines for normal commits; **1 line is fine for trivial commits** (those that skipped
  Decisions per Step 2)
- Use imperative mood

### 4. Retrieve token usage with ccusage

Use [`ccusage`](https://github.com/ryoppippi/ccusage) to get the **current session's** token usage.

**Important**: ccusage's default `session` command aggregates all sessions under the same project
path. To get the token usage for **this specific session only**, you must find the current session's
UUID and query it with the `-i` flag.

> **No pipes.** Each step below is a single Bash command (no `|`, no `&&`, no compound shell).
> Claude does the string/JSON transforms itself between steps. This keeps every call
> matching a stable Bash allowlist prefix so the user is not re-prompted for permission.

#### Step 1: Get the working directory

```bash
pwd
```

Then **Claude transforms the path itself** (do not pipe to `sed`): replace every `/`, `.`, `_`,
and space with `-` to derive `PROJECT_ID`. Example: `/Users/me/GitHub/my.app` → `-Users-me-GitHub-my-app`.

#### Step 2: List session files for this project

```bash
ls -t ~/.claude/projects/<PROJECT_ID>/
```

Substitute the `PROJECT_ID` you derived in Step 1. `-t` sorts by mtime (newest first), so the
**first `.jsonl` filename** in the output is the current session. **Claude parses the output**
and strips the `.jsonl` suffix to get `SESSION_UUID` — do not pipe through `head`/`xargs`/`sed`.

#### Step 3: Query token usage for this session (raw JSON)

```bash
npx ccusage claude session -i "<SESSION_UUID>" --json -O --no-color
```

Substitute the `SESSION_UUID` from Step 2. This prints JSON with a `sessionId`, `totalTokens`,
`totalCost`, and an `entries` array. Each entry has `inputTokens`, `outputTokens`,
`cacheCreationTokens`, `cacheReadTokens`, `model`.

#### Step 4: Summarize the token usage (in Claude, no shell)

**Claude parses the JSON from Step 3 directly** and computes the totals — do **not** pipe to
`python3` or any other interpreter. Sum across `entries`:

- `Input tokens` = sum of `inputTokens`
- `Output tokens` = sum of `outputTokens`
- `Cache read tokens` = sum of `cacheReadTokens`
- `Cache creation tokens` = sum of `cacheCreationTokens`
- `Total tokens` = `totalTokens` from the top-level JSON (fall back to the four sums above)
- `Total cost` = `totalCost` from the top-level JSON, formatted as USD with 4 decimal places (e.g. `$0.1234`). Omit this line if `totalCost` is missing or `0`.
- `Models used` = sorted unique `model` values across entries

If any step fails (ccusage not installed, no `.jsonl` files found, JSON empty, etc.),
**omit the `# Token Usage` section entirely** from the commit message — do not add a
placeholder or error note.

### 5. Append conversation log, Decisions, and metadata

After the body, add a `---` separator and the structured sections below. This makes the reasoning
permanently part of the commit history (unlike git notes which are local-only and easily lost).

If token usage was successfully retrieved in step 4, include it as the last section before the
trailer.

The full commit message format:

```
<type>(<optional-scope>): <summary under 72 chars>

<body: 2-5 lines on the task and motivation. Imperative mood.
No decisions/tradeoffs here — those live in # Decisions below.>

---

# Conversation Log

- User: <key user request or question>
- Assistant: <key response, action taken, or decision made>
- User: <follow-up or clarification>
- Assistant: <what was done next>
...
(List the core dialog nodes in chronological order. Skip pure tool-call noise
and redundant back-and-forth — focus on intent, decisions, and turning points.)

# Decisions

## Decision 1
- MODULE: <id from .claude/MODULES.md — must match exactly>
- WHY: <one-line motivation>
- ALTERNATIVES: <other approaches considered, separated by " / ">
- CHOSEN: <the approach actually taken>
- TRADEOFFS: <what was given up>
- RISKS: <what to watch out for later>
- SUPERSEDES: <OPTIONAL — only if this overrides a prior decision; format: "<old summary> (commit <hash>)">

# Files Modified

- <file path> — <one-line semantic description of what changed and why>
- ...
(Summarize each file's change in plain language — what the edit accomplishes,
not what lines were touched. Derive from the diff + conversation context.
If `.claude/MODULES.md` or `CLAUDE.md` were added/changed as part of Step 2's
one-time bootstrap, label them explicitly as such — e.g.
"`.claude/MODULES.md` — one-time knowledge-loop bootstrap (module registry)"
— so a future reader doesn't mistake the infrastructure addition for feature work.)

# Token Usage (only include if ccusage succeeds)

- Input tokens: <inputTokens>
- Output tokens: <outputTokens>
- Cache read tokens: <cacheReadTokens>
- Cache creation tokens: <cacheCreationTokens>
- Total tokens: <totalTokens>
- Total cost: <totalCost as USD, e.g. $0.1234 — omit this line if missing or 0>
- Models used: <modelsUsed>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

The `Co-Authored-By` trailer must be the **last line** of the commit message, separated
from the preceding section by a blank line. Match the model name to the actual model
running the session (check the environment block — e.g. `Claude Opus 4.7 (1M context)`,
`Claude Sonnet 4.6`, etc.).

**Conversation log guidelines**:
- Extract **key dialog nodes**, not every message verbatim. Keep it readable for someone skimming
  months later.
- **Scrub sensitive information** — if API keys, passwords, tokens, or other secrets appeared in
  the conversation, omit or redact them. Never persist credentials into version control.

**Decisions block format rules** (these enable future scripted distillation — keep them strict):
- Each Decision is a level-2 heading (`## Decision N`).
- One Decision per module touched. Cross-cutting commits get multiple Decisions.
- Field names are **ALL CAPS**, one field per line, exact prefix `- KEY: `. Required fields:
  `MODULE`, `WHY`, `ALTERNATIVES`, `CHOSEN`, `TRADEOFFS`, `RISKS`. Optional: `SUPERSEDES`.
- `MODULE` value must exactly match an `<id>` from `.claude/MODULES.md`. No paraphrasing, no
  inventing new ones inline — go back to Step 2's reverse-question flow instead.
- A value may span multiple lines, but the next field must still start with `- KEY: ` on its own line.
- Omit the whole `# Decisions` section when Step 2's skip conditions apply (trivial change or
  declined MODULES.md bootstrap) — don't leave an empty section or write "n/a".

### 6. Commit

Write the message to a temp file with the `Write` tool, then pass it to
`git commit -F`. The `Write` tool writes bytes verbatim — `$`, `` ` ``, `\`,
and quotes are preserved exactly as authored, with no shell expansion.

1. Use the `Write` tool to write the full commit message to `/tmp/commit-context-msg.txt`.
   Do not wrap the message in quotes or any other delimiter.
2. Commit and clean up in a single chained Bash call so the temp file is always removed,
   even if the commit fails:

```bash
git commit -F /tmp/commit-context-msg.txt; rm -f /tmp/commit-context-msg.txt
```

The `;` (not `&&`) ensures `rm` runs whether the commit succeeded or not. After this
step, show `git log -1 --stat` so the user sees the final commit.

## Important notes

- Never force-push or amend a previous commit unless the user explicitly asks.
- Never stage files that look like secrets (`.env`, `credentials.json`, `*.pem`, etc.) without
  warning the user first.
- If the conversation context is very short or unclear, lean more heavily on the diff to write the
  message, but still try to infer intent.
- **Module-registry discipline** — new entries in `.claude/MODULES.md` are only added after the
  user explicitly approves. Don't auto-create modules. If a proposed name violates the naming
  constraints (mixed case / spaces / non-ASCII / etc.), go back and reverse-question the user — do
  not silently rewrite it. The registry is meant to stay small (~15–25 entries); flag it when the
  user is about to push past that.
- **CLAUDE.md scope guard** — the only time this skill touches the project's `CLAUDE.md` is the
  one-shot read-rules bootstrap in Step 2, immediately after a `.claude/MODULES.md` bootstrap, on
  explicit user approval. Once that moment has passed (signal: `.claude/MODULES.md` exists), never
  re-prompt and never modify `CLAUDE.md` again. The skill's job is writing commits, not managing
  project config.
