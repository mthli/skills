---
name: sparse-checkout
description: Personally hide files or directories from a git repo's working tree via git sparse-checkout — no .gitignore changes, invisible to teammates. Use whenever the user wants to ignore team-committed paths just for themselves (e.g. .claude/, dist/, generated docs), restore a hidden path, list what's hidden, or undo all hiding. Triggers on "hide X locally", "ignore X locally", "make git stop showing X", "restore hidden files", "what did I hide". Distinct from .gitignore (committed, team-wide) — this is per-clone, reversible, stays private.
---

# sparse-checkout

Locally hide files and directories that teammates committed, without modifying any tracked file. The exclusion lives in `.git/info/sparse-checkout`, which is per-clone state — your teammates never see it, and `git push` cannot carry it.

## When to use

- Teammates committed personal config / generated output that you don't want in your tree (e.g. `.claude/`, `dist/`, `vendor/`)
- You want a private exclusion without proposing a `.gitignore` change to the team
- The user asks to restore a previously hidden path, or to see what's currently hidden
- The user wants to undo all local hiding

## Mental model

Behind the scenes this uses `git sparse-checkout` in **non-cone mode**, which accepts gitignore-style patterns. A typical patterns file looks like:

```
/*
!.claude/
!docs/private/
```

Read as: "include everything at the root, except `.claude/` and `docs/private/`." The patterns file is `.git/info/sparse-checkout`; it lives inside `.git/`, so it's never pushed and never affects anyone else.

`git sparse-checkout disable` blows the whole thing away and the clone behaves like a normal one again.

## Operations

Use the helper script `scripts/sparse_checkout.py` (relative to this SKILL.md). Resolve its absolute path and run it from anywhere inside the target repo — the script discovers the repo root itself, and path arguments are interpreted relative to that root (not your cwd).

| Goal | Command |
|---|---|
| Hide one or more paths | `python <skill-dir>/scripts/sparse_checkout.py hide <path> [path...]` |
| Restore one or more paths | `python <skill-dir>/scripts/sparse_checkout.py restore <path> [path...]` |
| List what's hidden in this repo | `python <skill-dir>/scripts/sparse_checkout.py list` |
| Show status (enabled? what's hidden?) | `python <skill-dir>/scripts/sparse_checkout.py status` |
| Disable entirely (bring all files back) | `python <skill-dir>/scripts/sparse_checkout.py disable` |

The script auto-detects whether a path is a directory and appends a trailing `/` so the pattern unambiguously matches the whole subtree.

## Safety checks — perform these BEFORE running `hide`

Sparse-checkout removes **tracked** files in the excluded paths from the working tree, but leaves **untracked** files alone. Both behaviors can surprise users. Walk through this checklist:

1. **Check for uncommitted modifications under the target path.** Run `git status --short -- <path>`. If anything tracked shows up (`M` / `A` / `D` / `R`), stop and report — the script enforces this and will refuse the hide. (Background: `git sparse-checkout reapply` is permissive and would warn-but-succeed, leaving the dirty files on disk while the patterns file commits to hiding them — a silent half-applied state that risks losing the modifications on the next checkout. So the script does a hard pre-flight check.) Ask the user whether to commit, stash, or skip.

2. **Note untracked personal files under the target path.** Run `git ls-files --others --exclude-standard -- <path>`. If anything is listed, warn the user: those files **stay on disk** after hiding, so the directory won't fully disappear — it will contain only their personal junk. Usually fine, but they may want to move that content out first.

3. **Confirm it's a git repo.** The script bails with a clear error if not, but it's nicer to know upfront.

For `restore`, no special checks are needed — restored content comes from the git index and is canonical.

## Workflow

When the user asks to hide something:

1. Identify the target path(s). If ambiguous (e.g. "hide the claude stuff"), confirm the exact path.
2. Run the safety checks above and report any findings.
3. Run the `hide` command.
4. Confirm with the user by reading back the resulting state (the script prints it automatically).
5. Mention the inverse command they can use to undo (`restore <path>` or `disable`).

When the user asks to restore or undo:

1. Run `list` first if the path isn't clear, so you can show them options.
2. Run `restore <path>` (specific) or `disable` (everything).
3. Confirm the new state.

## Example interactions

**Example 1 — hide a noisy directory**

User: "hide `.claude/`, teammates keep committing stuff into it"

1. Run `git status --short -- .claude/` and `git ls-files --others --exclude-standard -- .claude/` to check the state.
2. Report findings — e.g. "you have 2 untracked files under `.claude/` (`agents/my-foo.md`, `settings.local.json`). They'll stay on disk after hiding; the directory will only contain those. Proceed?"
3. After confirmation, run `python <skill-dir>/scripts/sparse_checkout.py hide .claude/`.
4. Echo the resulting hidden list.
5. Mention `restore .claude/` undoes it.

**Example 2 — list and partial restore**

User: "I hid a few directories before — list them, then restore the docs one"

1. Run `... list` to show what's hidden.
2. From the list pick the `docs/*` entry the user means; if multiple, ask which.
3. Run `... restore docs/internal/` (or whichever).
4. Echo the new state.

**Example 3 — full undo**

User: "restore everything, I don't want sparse-checkout anymore"

1. Run `... disable`.
2. Confirm it's off; all files are back.

## Edge cases

- **Sparse-checkout already initialized**: the script reads the existing pattern file and merges new exclusions without duplicating.
- **Hiding a path that's already hidden**: no-op, the script reports `(already hidden: <path>)`.
- **Restoring a path that wasn't hidden**: no-op, reports `(not hidden: <path>)`.
- **Restoring the last remaining exclusion**: the script runs `git sparse-checkout disable` for a clean state instead of leaving a no-op patterns file.
- **Not a git repo**: the script exits with a clear error; surface it to the user.
- **Path doesn't exist in HEAD**: still allow hiding — it may exist in other branches the user will check out later.

## What not to do

- Don't edit `.git/info/sparse-checkout` by hand from the model. Use the script — it writes the patterns file and calls `git sparse-checkout reapply`, with a rollback on failure so the patterns file never diverges from the actual working tree.
- Don't propose adding the path to `.gitignore` instead. That's a different problem (preventing new commits of the path) and affects the whole team. This skill is for the opposite case: the path is already tracked, and you personally don't want it in your tree.
- Don't use this in CI / shared scripts. It's a developer-machine convenience.
