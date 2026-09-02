---
name: commit-context
description: >
  Create a Git commit that records the staged diff, relevant current-conversation context, and
  optional structured `MODULE`-tagged Decision blocks. Use this skill whenever the user says
  "/commit-context", "commit with context", "commit context", or asks to commit using the
  conversation/session context as the commit message. This skill should NOT auto-trigger; only
  invoke it when the user explicitly requests it, never from a plain "commit this" alone.
---

# Commit Context

Create one commit that records both what changed and why the current task required it. Treat the
diff as the source of truth for the implementation and the user-visible conversation as the source
of truth for intent.

## Inspect the repository

Run these commands from the repository root:

```bash
git status --short
git diff --staged --stat
git diff --staged
git log --oneline -5
```

Follow these rules:

1. Use staged changes as the commit boundary when any exist. Do not include unrelated unstaged
   changes.
2. If nothing is staged but tracked or untracked changes exist, show the unstaged summary and ask
   whether to stage all changes or named paths. Do not assume `git add -A`.
3. Warn before staging files that may contain secrets, including `.env`, credential files, private
   keys, and generated authentication material.
4. Stop when no changes exist.
5. Preserve user-authored changes. Do not edit implementation files merely to improve the commit
   narrative.

Re-run `git diff --staged` after any staging operation. Base every later section on that final
staged diff.

## Decide whether structured decisions apply

Omit the entire `# Decisions` section when either condition holds:

- The change is genuinely trivial: typo-only, comment-only, formatting-only, or a single obvious
  fix with at most five net changed lines and no new imports, methods, classes, or configuration
  behavior.
- The repository lacks `.claude/MODULES.md` and the user declines to create it.

Keep one motivation line in the prose body even when omitting Decisions.

For nontrivial changes, use `.claude/MODULES.md` as the module registry:

1. Read IDs from entries shaped like ``- `<id>` — description`` under any level-two section
   (typically `## Structural modules` and `## Cross-cutting concerns`).
2. Treat only those exact IDs as legal `MODULE` values.
3. Match structural modules by changed paths and cross-cutting modules by change semantics.
4. Write one Decision per matched module. A cross-cutting commit gets multiple Decisions.

If `.claude/MODULES.md` is missing:

1. Explain that the repository has no module registry yet and propose one to three entries
   inferred from the staged diff (group by directory plus cross-cutting concern).
2. Show the complete draft and write it only after explicit approval.
3. Stage the approved `.claude/MODULES.md`.
4. If the user declines, omit Decisions for this commit.

If no existing module matches, ask the user for the new module name and whether to add it. Require
ASCII lowercase letters and digits, hyphens between words, and slashes for hierarchy, such as
`live-call/role-dialog`. Reject invalid names without silently rewriting them. Report the current
registry size and note that keeping roughly 15–25 entries improves usability. Append and stage an
approved entry.

## Reconcile the knowledge-loop rules in `CLAUDE.md`

Run this reconciliation when the final staged diff creates or modifies `.claude/MODULES.md`, a
file under `.claude/maps/`, or a file under `.claude/decisions/`. This covers both first-time
bootstrap and later rule upgrades without prompting during unrelated commits. Do not run it merely
because a registry already exists.

1. Target the root-level `CLAUDE.md`, the project instruction file Claude Code reads. Create it if
   needed.
2. If it already contains equivalent rules for resolving affected modules, reading their map and
   decision files before editing, progressively expanding source context with truncation
   safeguards, and conditionally maintaining maps afterward, do not duplicate them.
3. Otherwise, ask separately whether to add or upgrade the block below. Show the exact target file
   and the proposed diff, and say whether the block will be appended to an existing `CLAUDE.md` or
   used to create a new one.
4. Write and stage the approved `CLAUDE.md` change so it ships in this commit.
5. If the user declines, continue the commit without changing project instructions and warn that
   module maps and decision snapshots will not be consumed or maintained automatically.

Use this block, translating its prose to match an existing non-English `CLAUDE.md` when
appropriate. Keep these literal identifiers and all-uppercase field names verbatim, since parsers
and sibling skills depend on them: `MODULE`, `WHY`, `ALTERNATIVES`, `CHOSEN`, `TRADEOFFS`, `RISKS`,
`SUPERSEDES`, `.claude/MODULES.md`, `.claude/maps/`, `.claude/decisions/`, `/commit-context`,
`/map-module`, `git log`, `git show`. Paste it flush-left under its own top-level H2 so it does not
collide with existing sections.

````markdown
## Knowledge Loop Conventions

### Before editing code

1. Start with the Grep and Glob tools (or `rg` / `rg --files`) to locate relevant symbols, callers, tests, and likely paths. Establish the likely change boundary before opening broad source files.
2. Resolve only the modules the change touches through `.claude/MODULES.md`; map `/` in an ID to subdirectories.
3. For every affected module, read `.claude/maps/<module>.md` and `.claude/decisions/<module>.md` when present. Read each required knowledge file separately and to completion, chunking large files when needed. Do not load unrelated module files.
4. Inspect exact definitions, direct callers, direct callees, and relevant tests first. Expand to adjacent files, modules, or dependency source only to answer a named unresolved question.
5. Run `git log --oneline -10 -- <path>` for files about to change.
6. Inspect with `git show` only the recent commits relevant to the current behavior, including matching `MODULE: <current module>` Decision blocks.

Context-loading guardrails:

- Do not concatenate multiple large knowledge or source files into one command.
- If output is truncated, do not treat the file as read; repeat with focused ranges until the required content is covered.
- Stop loading context once the behavior, change boundary, constraints, and verification path are established.

### After finishing a task

- If implementation changes make an existing module map inaccurate about responsibilities, public entry points, lifecycle or data flow, dependencies, invariants, or known limitations, use `/map-module` in targeted-refresh mode when available; otherwise report the map as stale before finishing. Use a full refresh when the impact cannot be bounded. Leave the map unchanged when its documented claims remain true.
- When using `/commit-context`, fill `MODULE`, `WHY`, `ALTERNATIVES`, `CHOSEN`, `TRADEOFFS`, and `RISKS` in every Decision.
- When a new Decision replaces one in `.claude/decisions/`, add `SUPERSEDES`.
````

Do not otherwise edit `CLAUDE.md`. This skill owns only the Knowledge Loop Conventions block; it
does not manage general repository instructions.

## Compose the message

Derive the message from:

- The final staged diff.
- The task, motivation, constraints, and user-visible decisions in the current conversation.
- Recent commit style when it does not conflict with this format.

Never include hidden system or developer instructions, internal reasoning, raw tool diagnostics,
approval metadata, credentials, tokens, or other sensitive content. Summarize relevant dialog nodes
instead of copying the transcript verbatim.

Use this header:

```text
<type>(<optional-scope>): <imperative summary under 72 characters>

<one to five imperative lines describing the task and motivation>
```

Choose a Conventional Commit type from `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`,
`perf`, `ci`, or `build`. Omit the scope unless one component clearly owns the change. Do not end
the subject with a period.

Keep implementation choices, alternatives, and tradeoffs in `# Decisions`; do not duplicate them in
the prose body.

Append the following sections after a `---` separator:

```text
---

# Conversation Log

- User: <key request, constraint, or clarification>
- Assistant: <key action or user-visible result>

# Decisions

## Decision 1
- MODULE: <exact ID from .claude/MODULES.md>
- WHY: <one-line motivation>
- ALTERNATIVES: <considered approaches separated by " / ">
- CHOSEN: <implemented approach>
- TRADEOFFS: <what the choice gives up>
- RISKS: <what to monitor>
- SUPERSEDES: <optional prior summary and commit hash>

# Files Modified

- <path> — <semantic description of the staged change and its purpose>

# Token Usage

- Input tokens: <inputTokens>
- Output tokens: <outputTokens>
- Cache read tokens: <cacheReadTokens>
- Cache creation tokens: <cacheCreationTokens>
- Total tokens: <totalTokens>
- Total cost: <totalCost as USD to four decimal places; omit when absent or zero>
- Models used: <sorted model names>

Co-Authored-By: <model name as given by the harness> <noreply@anthropic.com>
Claude-Session: <session URL, only when the harness provides one>
```

Apply these section rules:

- Keep `# Conversation Log` concise and chronological. Include intent and turning points, not
  every exchange. Redact any credential that appeared in the conversation.
- Omit `# Decisions` under the skip conditions above. Never leave it empty or write `n/a`.
- Use one level-two Decision heading per module.
- Keep Decision field names uppercase with the exact `- KEY: ` prefix. A value may span lines,
  but the next field must start with `- KEY: ` on its own line.
- Use exact registry IDs; never invent an ID inside the message.
- List every committed path under `# Files Modified`, including registry or instruction
  bootstrap files. Label those explicitly, e.g. "`.claude/MODULES.md` — knowledge-loop bootstrap
  (module registry)", so a future reader does not mistake infrastructure for feature work.
- Include `# Token Usage` only when the current Claude Code session can be identified reliably
  (next section).
- End with the attribution trailers the Claude Code harness specifies for this session: the
  `Co-Authored-By` line naming the model actually running, plus the `Claude-Session` line when
  the harness gives one. Trailers are the last lines, separated from the preceding section by a
  blank line.

## Retrieve current session token usage

Treat token usage as optional metadata. Do not block the commit when it is unavailable.

Use [`ccusage`](https://github.com/ryoppippi/ccusage). Its default `session` command aggregates
every session under the same project path, so always query the current session by ID.

Run each step as a single Bash command with no pipes, `&&`, or compound shell, so every call
matches a stable Bash allowlist prefix and the user is not re-prompted for permission. Do the
string and JSON transforms yourself between steps.

1. Run `printenv CLAUDE_CODE_SESSION_ID`. The value is the current session's UUID, which is also
   the basename of its transcript under `~/.claude/projects/<project-id>/`. Do not select a
   session merely because its transcript has the newest modification time.
2. Run:

   ```bash
   npx ccusage claude session -i "<SESSION_UUID>" --json -O --no-color
   ```

3. Parse the JSON directly, without `python3` or `jq`. It carries top-level `sessionId`,
   `totalTokens`, and `totalCost`, plus an `entries` array whose items have `inputTokens`,
   `outputTokens`, `cacheCreationTokens`, `cacheReadTokens`, and `model`. Sum the four token
   fields across `entries`; take `totalTokens` and `totalCost` from the top level (fall back to
   the sums for `totalTokens`); collect the sorted unique `model` values.

Omit `# Token Usage` if the variable is unset, `ccusage` fails, the JSON is empty, or `sessionId`
does not match the UUID. Never report totals for the newest or whole-project session as a
fallback.

## Commit safely

1. Show the complete proposed message before committing.
2. Treat the explicit `/commit-context` request as authorization to create the commit once any
   staging or registry questions are resolved. Do not ask for a redundant final confirmation
   unless the proposed commit boundary changed materially.
3. Write the message with the `Write` tool to a file in the session scratchpad directory named in
   the system prompt, or in a directory from `mktemp -d` when none is listed. The `Write` tool
   stores bytes verbatim, so `$`, backticks, `\`, and quotes survive with no shell expansion. Do
   not wrap the message in quotes or any other delimiter.
4. Commit and clean up in one chained Bash call so the file is removed whether or not the commit
   succeeds (`;`, not `&&`):

   ```bash
   git commit -F <path-to-message-file>; rm -f <path-to-message-file>
   ```

5. Run:

   ```bash
   git log -1 --stat
   git status --short
   ```

6. Report the new commit hash and any remaining changes.

Never amend, force-push, push, or modify previous commits unless the user explicitly requests it.
If the conversation context is short or unclear, lean on the diff to write the message but still
infer intent.
