---
name: commit-context
description: Create a Git commit that records the staged diff, relevant current-conversation context, and optional structured module decisions. Use only when the user explicitly invokes `$commit-context`; do not select it from natural-language commit requests alone.
---

# Commit Context

Create one commit that records both what changed and why the current task required it. Treat the diff as the source of truth for the implementation and the user-visible conversation as the source of truth for intent.

## Inspect the repository

Run these commands from the repository root:

```bash
git status --short
git diff --staged --stat
git diff --staged
git log --oneline -5
```

Follow these rules:

1. Use staged changes as the commit boundary when any exist. Do not include unrelated unstaged changes.
2. If nothing is staged but tracked or untracked changes exist, show the unstaged summary and ask whether to stage all changes or named paths. Do not assume `git add -A`.
3. Warn before staging files that may contain secrets, including `.env`, credential files, private keys, and generated authentication material.
4. Stop when no changes exist.
5. Preserve user-authored changes. Do not edit implementation files merely to improve the commit narrative.

Re-run `git diff --staged` after any staging operation. Base every later section on that final staged diff.

## Decide whether structured decisions apply

Omit the entire `# Decisions` section when either condition holds:

- The change is genuinely trivial: typo-only, comment-only, formatting-only, or a single obvious fix with at most five net changed lines and no new imports, methods, classes, or configuration behavior.
- The repository lacks `.codex/MODULES.md` and the user declines to create it.

Keep one motivation line in the prose body even when omitting Decisions.

For nontrivial changes, use `.codex/MODULES.md` as the module registry:

1. Read IDs from entries shaped like ``- `<id>` — description`` under any level-two section.
2. Treat only those exact IDs as legal `MODULE` values.
3. Match structural modules by changed paths and cross-cutting modules by change semantics.
4. Write one Decision per matched module.

If `.codex/MODULES.md` is missing:

1. Explain that the repository has no Codex module registry and propose one to three entries inferred
   from the staged diff.
2. Show the complete draft and write it only after explicit approval.
3. Stage the approved `.codex/MODULES.md`.
4. If the user declines, omit Decisions for this commit.

If no existing module matches, ask the user for the new module name and whether to add it. Require ASCII lowercase letters and digits, hyphens between words, and slashes for hierarchy, such as `live-call/role-dialog`. Reject invalid names without silently rewriting them. Report the current registry size and note that keeping roughly 15–25 entries improves usability. Append and stage an approved entry.

## Reconcile Codex knowledge-loop rules

Run this reconciliation when the final staged diff creates or modifies `.codex/MODULES.md`, a file
under `.codex/maps/`, or a file under `.codex/decisions/`. This covers both first-time bootstrap and
later rule upgrades without prompting during unrelated commits. Do not run it merely because a
registry already exists.

1. Select the root instruction file Codex actually reads:
   - Use `AGENTS.override.md` when a non-empty root-level override exists.
   - Otherwise use root-level `AGENTS.md`, creating it if needed.
2. If the selected file already contains equivalent rules for resolving affected modules, reading
   their map and decision files before editing, progressively expanding source context with
   truncation safeguards, and conditionally maintaining maps afterward, do not duplicate them.
3. Otherwise, ask separately whether to add or upgrade the block below. Show the exact target file
   and proposed diff.
4. Write and stage the approved instruction-file change.
5. If the user declines, continue the commit without changing project instructions and warn that
   module maps will not be consumed or maintained automatically.

Use this block, translating its prose to match an existing instruction file when appropriate. Keep
literal identifiers and all-uppercase field names unchanged, including `.codex/` paths and
`$commit-context` / `$map-module` invocations. Paste it flush-left under its own top-level H2 so it
does not collide with existing sections.

````markdown
## Knowledge Loop Conventions

### Before editing code

1. Resolve every module the change touches through `.codex/MODULES.md`; map `/` in an ID to subdirectories.
2. Read `.codex/maps/<module>.md` and `.codex/decisions/<module>.md` when present for every affected module. Do not load unrelated module files; if output is truncated, continue with focused ranges until each required file is complete.
3. Expand source context only to answer a named unresolved question. Stop when the behavior, change boundary, constraints, and verification path are clear.
4. Run `git log --oneline -10 -- <path>` for files about to change. Use `git show` only for recent commits relevant to the current behavior, including those with matching `MODULE: <current module>` Decision blocks.

### After finishing a task

- If implementation changes invalidate an existing module map, use `$map-module`: targeted refresh when the impact is bounded, otherwise full refresh. If unavailable, report the map as stale.
- In every `$commit-context` Decision, fill `MODULE`, `WHY`, `ALTERNATIVES`, `CHOSEN`, `TRADEOFFS`, and `RISKS`; add `SUPERSEDES` when replacing a prior Decision.
````

Do not otherwise edit `AGENTS.md` or `AGENTS.override.md`. This skill owns only the Knowledge Loop
Conventions block; it does not manage general repository instructions.

## Compose the message

Derive the message from:

- The final staged diff.
- The task, motivation, constraints, and user-visible decisions in the current conversation.
- Recent commit style when it does not conflict with this format.

Never include hidden system or developer instructions, internal reasoning, raw tool diagnostics, approval metadata, credentials, tokens, or other sensitive content. Summarize relevant dialog nodes instead of copying the transcript verbatim.

Use this header:

```text
<type>(<optional-scope>): <imperative summary under 72 characters>

<one to five imperative lines describing the task and motivation>
```

Choose a Conventional Commit type from `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`, `perf`, `ci`, or `build`. Omit the scope unless one component clearly owns the change. Do not end the subject with a period.

Keep implementation choices, alternatives, and tradeoffs in `# Decisions`; do not duplicate them in the prose body.

Append the following sections after a `---` separator:

```text
---

# Conversation Log

- User: <key request, constraint, or clarification>
- Assistant: <key action or user-visible result>

# Decisions

## Decision 1
- MODULE: <exact ID from .codex/MODULES.md>
- WHY: <one-line motivation>
- ALTERNATIVES: <considered approaches separated by " / ">
- CHOSEN: <implemented approach>
- TRADEOFFS: <what the choice gives up>
- RISKS: <what to monitor>
- SUPERSEDES: <optional prior summary and commit hash>

# Files Modified

- <path> — <semantic description of the staged change and its purpose>

# Token Usage

- Since: <abbreviated hash of the previous commit counted in this session, or "session start">
- Input tokens: <inputTokens>
- Output tokens: <outputTokens>
- Reasoning output tokens: <reasoningOutputTokens>
- Cache read tokens: <cacheReadTokens>
- Cache creation tokens: <cacheCreationTokens>
- Total tokens: <totalTokens>
- Total cost: <USD formatted to four decimal places; omit when absent or zero>
- Models used: <sorted model names>
```

Apply these section rules:

- Keep `# Conversation Log` concise and chronological. Include intent and turning points, not every exchange.
- Omit `# Decisions` under the skip conditions above. Never leave it empty or write `n/a`.
- Use one level-two Decision heading per module.
- Keep Decision field names uppercase with the exact `- KEY: ` prefix. A value may span lines,
  but the next field must start with `- KEY: ` on its own line.
- Use exact registry IDs; never invent an ID inside the message.
- List every committed path under `# Files Modified`, including registry or instruction bootstrap
  files. Label those as knowledge-loop bootstrap so they are not mistaken for feature work.
- Include `# Token Usage` only when the current Codex session can be identified reliably.
- Do not add a `Co-Authored-By` trailer for Codex or a model unless the user or repository explicitly requires one.

## Retrieve Codex token usage since the previous commit

Treat token usage as optional metadata. Do not block the commit when it is unavailable.

Report the session's usage since the previous commit this skill made in the same session, rather
than counting early work again in every later commit. Use the current thread's own usage; do not
aggregate unrelated sessions or invent usage for subagents.

1. Run `printenv CODEX_THREAD_ID`.
2. Resolve the Codex home directory from `CODEX_HOME` when set; otherwise use `~/.codex`.
3. Locate the one rollout file whose filename ends with `-<CODEX_THREAD_ID>.jsonl` under `<codex-home>/sessions/`. Do not select a session merely because it has the newest modification time.
4. Run:

   ```bash
   npx ccusage codex session --id <CODEX_THREAD_ID> --json -O --no-color
   ```

5. Parse the JSON directly. The ID-filtered result is one session object; if the installed version
   returns a `sessions` array, select exactly one matching entry. Require `sessionFile` to match
   the identified rollout filename, with or without its `.jsonl` extension.
6. Read the running totals `inputTokens`, `outputTokens`, `reasoningOutputTokens`, `cacheReadTokens`,
   `cacheCreationTokens`, and `totalTokens`. Use `totalCost` for USD cost (`costUSD` in older
   versions), when present. Preserve the `models` map's per-model counters and `lastActivity`.
   Use reported totals directly: reasoning and cached tokens may overlap other counters, so do
   not sum all fields to reconstruct `totalTokens`.
7. Read `commit-context-usage.json` in the session scratchpad directory when one is provided;
   otherwise use `/tmp/commit-context-<CODEX_THREAD_ID>.json`. It records the previous successful
   commit's running totals, not its reported differences:

   ```json
   {
     "threadId": "<CODEX_THREAD_ID>",
     "sessionFile": "<rollout filename without .jsonl>",
     "commit": "<abbreviated hash>",
     "lastActivity": "<latest usage timestamp>",
     "inputTokens": 0,
     "outputTokens": 0,
     "reasoningOutputTokens": 0,
     "cacheReadTokens": 0,
     "cacheCreationTokens": 0,
     "totalTokens": 0,
     "costUSD": 0,
     "models": {}
   }
   ```

8. If the baseline identifies the same thread and rollout, contains all required token counters,
   and none has decreased, subtract each baseline counter from its running total and set `Since`
   to the baseline's `commit`. Report cost only when both samples have comparable numeric costs
   and the difference is positive; omit unavailable cost from the baseline instead of storing
   zero. For `Models used`, include only models whose counters increased since the baseline
   (a new model has a zero baseline). If per-model deltas are unavailable, omit that line rather
   than presenting all session models as this interval's models.
9. If there is no usable baseline, report running totals with `Since: session start` and all
   session models. Explain when an existing baseline was invalid or counters reset.

Omit `# Token Usage` if the thread ID is absent, the rollout file is ambiguous, `ccusage` fails,
required counters are missing or invalid, or no exact session entry matches. Never report totals
for the newest or entire project session as a fallback. Sample once before composing the message;
work after that sample is counted at the next successful commit.

## Commit safely

1. Show the complete proposed message before committing.
2. Treat the explicit `$commit-context` request as authorization to create the commit after any required staging or registry questions are resolved. Do not ask for a redundant final confirmation unless the proposed commit boundary changed materially.
3. Create a unique temporary directory with `mktemp -d`, write the message to a file inside it using an available file-editing tool, and pass that explicit file to `git commit -F`.
4. Remove only that temporary directory after the commit attempt.
5. Run:

   ```bash
   git log -1 --stat
   git status --short
   ```

6. If the commit succeeded and its message includes `# Token Usage`, replace the baseline with the
   sampled running totals, per-model counters, session identity, and the new commit's hash. Keep
   this file outside the repository. Leave it unchanged when the commit failed or usage was
   omitted, so the next commit still covers that work. Report a baseline-write failure without
   retrying the commit.
7. Report the new commit hash and any remaining changes.

Never amend, force-push, push, or modify previous commits unless the user explicitly requests it.
