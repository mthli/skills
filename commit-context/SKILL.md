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

### 2. Synthesize the commit message

Look back through the conversation and identify:

- **What task** the user was working on (bug fix, new feature, refactor, docs, tests, config, etc.)
- **Key decisions** made during the session (e.g. "chose JWT over session cookies", "switched from
  REST to GraphQL")
- **Problems encountered and solved** (e.g. "race condition in the cache layer")
- **The motivation** — why the change was needed

Combine this with the actual diff to produce a commit message in this format:

```
<type>(<optional-scope>): <summary under 72 chars>

<body: 2-5 lines summarizing the session context — the why, key decisions,
and any non-obvious choices. Written in imperative mood.>
```

**Type** follows Conventional Commits: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`,
`perf`, `ci`, `build`.

**Scope** is optional — use it when the change clearly belongs to one module/component.

**Summary line rules:**
- Imperative mood ("add", not "added" or "adds")
- No period at the end
- Under 72 characters total (including the `type(scope): ` prefix)

**Body guidelines:**
- Focus on *why* the change was made, not *what* files were touched (the diff handles that)
- Mention key decisions or tradeoffs from the conversation
- Keep it to 2-5 lines — concise but informative
- Use imperative mood

### 3. Retrieve token usage with ccusage

Use [`ccusage`](https://github.com/ryoppippi/ccusage) to get the **current session's** token usage.

**Important:** ccusage's default `session` command aggregates all sessions under the same project
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
npx ccusage session -i "<SESSION_UUID>" --json -O --no-color
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

### 4. Append conversation log to the commit message

After the body, add a `---` separator and a structured conversation log that captures the full
session context. This makes the reasoning permanently part of the commit history (unlike git notes
which are local-only and easily lost).

If token usage was successfully retrieved in step 3, include it as the last section.

The full commit message format:

```
<type>(<optional-scope>): <summary under 72 chars>

<body: 2-5 lines summarizing the session context — the why, key decisions,
and any non-obvious choices. Written in imperative mood.>

---

# Conversation Log

- User: <key user request or question>
- Assistant: <key response, action taken, or decision made>
- User: <follow-up or clarification>
- Assistant: <what was done next>
...
(List the core dialog nodes in chronological order. Skip pure tool-call noise
and redundant back-and-forth — focus on intent, decisions, and turning points.)

# Key Decisions

- <decision 1 and its rationale>
- <decision 2 and its rationale>

# Files Modified

- <file path> — <one-line semantic description of what changed and why>
- ...
(Summarize each file's change in plain language — what the edit accomplishes,
not what lines were touched. Derive from the diff + conversation context.)

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

**Conversation log guidelines:**
- Extract **key dialog nodes**, not every message verbatim. Keep it readable for someone skimming
  months later.
- **Scrub sensitive information** — if API keys, passwords, tokens, or other secrets appeared in
  the conversation, omit or redact them. Never persist credentials into version control.

### 5. Commit

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
- The conversation log in the commit message should extract **key dialog nodes**, not copy every
  message verbatim. Keep it readable and useful for someone skimming months later.
- **Scrub sensitive information** — if API keys, passwords, tokens, or other secrets appeared in
  the conversation, omit or redact them. Never persist credentials into version control.
