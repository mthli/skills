---
name: statusline-vocab
description: >
  Manage the statusline-vocab feature — a `Stop` hook picks one English word worth learning from each
  conversation and renders `{emoji} {word} /IPA/ pos. {translation}` on the Claude Code statusline
  (translation language configurable; Chinese by default).
  Trigger on "/statusline-vocab" or when the user asks to install / configure / check / uninstall /
  switch language / debug the vocab statusline. Do NOT auto-trigger — explicit invocation only.
---

# statusline-vocab

Put a "word of the conversation" segment on the Claude Code statusline so the user passively learns
English vocabulary from their own work. Three moving parts:

1. **`~/.claude/hooks/vocab/extract.sh`** — `Stop` hook. Each time the assistant finishes a turn,
   this hook reads the last ~30 transcript entries, calls `claude -p --model claude-haiku-4-5`
   to pick one English word worth learning (lemma + US IPA + POS + translation + emoji; target
   language configurable), and writes JSON to `~/.claude/vocab/current.json`. A 3-minute cooldown
   prevents the word from churning every single turn within one conversation.
2. **`~/.claude/vocab/current.json`** + **`~/.claude/vocab/history.jsonl`** — current word plus an
   append-only wordbook history of everything that's been picked.
3. **`~/.claude/statusline-command.sh`** — reads `current.json` and appends a vocab segment to the
   statusline output.

Rendered example (with default Chinese; swap to any language via `--lang`):

```
➜ skills git:(develop) · Opus 4.7 ctx:12% · 📝 transcript /ˈtrænskrɪpt/ n. 誊本，逐字记录
```

## When to use this skill

The user has typed `/statusline-vocab` or otherwise referenced this feature. Likely intents:

- **Install** on a fresh machine — set everything up (optionally with `--lang`)
- **Status / diagnose** — "why isn't a word showing up?", "what's installed?", "what language am I on?"
- **Switch language** — "translate to Spanish", "use Japanese definitions" → `install.py config --lang …`
- **Reconfigure** other knobs — cooldown, model, selection criteria (edit `extract.sh` directly)
- **Uninstall** — remove the hook and tell the user how to clean the statusline

Pick the right subcommand below based on which of these the user wants. If unclear, run
`status` first so the conversation has a shared baseline.

## Workflow

### Step 1 — Detect environment

Before doing anything destructive, verify the prerequisites. The installer script does this too,
but it's worth knowing what it checks so you can give a useful error if something is missing:

- `claude` CLI on `PATH` (the hook shells out to `claude -p`)
- `jq` on `PATH` (both the hook and the statusline parse JSON with it)
- `~/.claude/` exists (true on any machine where Claude Code has been launched)

### Step 2 — Run the installer

```bash
python <skill-dir>/scripts/install.py install
```

The installer is **idempotent** — running it twice does nothing the second time. It:

1. Creates `~/.claude/hooks/vocab/` and `~/.claude/vocab/` if missing.
2. Copies the canonical `extract.sh` from the skill into `~/.claude/hooks/vocab/extract.sh`
   (overwrites the file each run — this is how the user picks up skill updates).
3. Registers the hook under `hooks.Stop` in `~/.claude/settings.json`. Skips if the exact
   command path is already present. Makes a `.bak-vocab` backup before writing.
4. Handles the statusline based on its current state — see Step 3.

### Step 3 — Statusline integration

The installer **does not** auto-edit a custom statusline. It branches into three cases and prints
which one it took:

**Case A — no statusline exists** (`~/.claude/statusline-command.sh` missing or empty)
  → installer writes the bundled `default-statusline.sh` verbatim. The default is intentionally
  plain (dir · model · ctx · vocab); users who want a richer look can edit freely after.

**Case B — already integrated** (statusline contains the `# vocab:start` marker)
  → installer skips, prints "already integrated".

**Case C — existing custom statusline without markers**
  → installer prints the snippet below and **refuses to auto-modify**. Your job in the
  conversation: read the user's `~/.claude/statusline-command.sh`, understand how it builds
  its output line, and insert the snippet so the vocab segment lands at the end of the line
  with a ` · ` separator. Always show the diff before writing so the user can sanity-check.

Insertion snippet (always surround with the markers so a future uninstall can find it):

```bash
# vocab:start — statusline-vocab skill
vocab_file="$HOME/.claude/vocab/current.json"
vocab_part=""
if [ -f "$vocab_file" ]; then
  v_word=$(jq -r '.word // empty' "$vocab_file" 2>/dev/null)
  if [ -n "$v_word" ] && [ "$v_word" != "null" ]; then
    v_emoji=$(jq -r '.emoji // "📖"' "$vocab_file" 2>/dev/null)
    v_ipa=$(jq -r '.ipa // ""' "$vocab_file" 2>/dev/null)
    v_pos=$(jq -r '.pos // ""' "$vocab_file" 2>/dev/null)
    v_meaning=$(jq -r '.meaning // ""' "$vocab_file" 2>/dev/null)
    vocab_part=" · ${v_emoji} \033[1;34m${v_word}\033[0m \033[2m${v_ipa}\033[0m \033[0;33m${v_pos}\033[0m ${v_meaning}"
  fi
fi
# vocab:end
```

How to wire `${vocab_part}` into the final output depends on how the statusline is structured:

- **Statusline builds a single `line` variable and prints once at the end** (most common pattern,
  matches the bundled default): append `${vocab_part}` to that variable just before the final
  `printf` / `echo`.
- **Statusline calls `printf` multiple times inline**: refactor lightly so the last `printf`
  gets `${vocab_part}` concatenated onto its argument. Don't add a separate `printf` call after,
  because Claude Code's statusline keeps trailing newlines literal.
- **Statusline outputs multiple lines on purpose** (e.g. uses `printf '%b\n'` twice): pick the
  line you want vocab on, and append there.

After inserting, run `Step 4` to confirm.

### Step 4 — Verify

```bash
python <skill-dir>/scripts/install.py status
```

Reports: hook script present? hook registered? statusline integrated (markers found)? what word
is current? how many history entries?

To preview rendering without waiting for a real `Stop`, pipe a stub event:

```bash
echo '{"workspace":{"current_dir":"'"$PWD"'"},"cwd":"'"$PWD"'","model":{"display_name":"Test"},"context_window":{"used_percentage":0}}' \
  | bash ~/.claude/statusline-command.sh
```

The vocab segment only appears once a word has been extracted — that happens after the next real
`Stop` hook fires in a Claude Code session. To force an extract right now, point the hook script
at an existing transcript:

```bash
latest=$(ls -t ~/.claude/projects/*/*.jsonl | head -1)
printf '{"transcript_path":"%s","hook_event_name":"Stop"}' "$latest" \
  | ~/.claude/hooks/vocab/extract.sh
cat ~/.claude/vocab/current.json
```

## Configuration

### Translation language

The `meaning` field is translated into a target language — Chinese by default, but switchable. The
language is stored in `~/.claude/vocab/config` so it survives across upgrades and doesn't require
editing `extract.sh`. Two ways to set it:

```bash
# At install time:
python <skill-dir>/scripts/install.py install --lang Spanish

# Anytime after, without reinstalling:
python <skill-dir>/scripts/install.py config --lang Japanese

# Inspect current:
python <skill-dir>/scripts/install.py config
```

The value is passed verbatim to the prompt (`"…tutor for a {LANG}-speaking learner. … concise
translation in {LANG}, under ~15 characters or 3-4 words…"`), so any language the model knows
works: `Chinese`, `Japanese`, `Korean`, `Spanish`, `French`, `German`, `Portuguese`, `Italian`,
`Russian`, `Vietnamese`, `Thai`, …

The change takes effect on the **next** `Stop` hook fire. The currently-displayed word
(`current.json`) is not retroactively re-translated — wait for the cooldown to expire or
`rm ~/.claude/vocab/current.json` to force a fresh extract.

### Other knobs

For knobs that don't change often, edit `~/.claude/hooks/vocab/extract.sh` directly. They live
at the top under `# --- Knobs ---`:

| Knob | Default | Effect |
|---|---|---|
| `COOLDOWN` | `180` (seconds) | Skip re-extraction if `current.json` is fresher than this. Higher = more stable word; lower = updates more often. Raising to 600 gives roughly one word per "real" conversation. |
| `MODEL` | `claude-haiku-4-5` | Swap for `claude-sonnet-4-6` if you want richer choices. Cost goes up; latency too (still async, so doesn't block). |
| `DEFAULT_LANG` | `Chinese` | Only used when no config file exists. The config file always wins. |
| The prompt body | see file | Edit the skip-list ("the", "use", "make"…), the difficulty bias, or the JSON schema. After editing, the next `Stop` picks up the new prompt — no restart needed. |

If the user asks for a tunable that requires more than a one-line edit (e.g. "only extract on
weekdays", "rotate through three difficulty levels"), edit the script in place.

## Uninstall

```bash
python <skill-dir>/scripts/install.py uninstall
```

This:

1. Removes the hook entry from `~/.claude/settings.json` (backup at `.json.bak-vocab-uninstall`).
2. Deletes `~/.claude/hooks/vocab/extract.sh`.
3. Leaves `~/.claude/vocab/` intact so the user's `history.jsonl` survives.
4. Leaves the statusline alone, but prints the marker lines so the user (or Claude) can strip
   them. If the statusline was the bundled default and the user wants a clean slate, they can
   just `rm ~/.claude/statusline-command.sh`.

## Troubleshooting

- **No word ever appears.** Check `~/.claude/vocab/extract.log` for errors. The most common cause
  is `claude` not found from the hook's non-interactive shell — Claude Code hooks inherit a
  minimal PATH. Fix by symlinking `claude` into `/usr/local/bin/` or by editing the hook to use
  the absolute path. Verify manually with the "force an extract" snippet above.
- **Same word keeps showing.** Cooldown is doing its job; either wait it out or
  `rm ~/.claude/vocab/current.json` to force a re-extract on the next `Stop`.
- **Statusline renders broken / garbled.** `jq` is probably missing. The snippet swallows individual
  `jq` errors but ends up with an empty vocab segment, which usually looks fine. If color codes are
  leaking as raw `\033[...` text, the user's terminal doesn't render `printf '%b'` — switch the
  insertion to `echo -e` or drop the colors.
- **Model picks bad words ("the", "use", "make").** Strengthen the skip-list in the prompt inside
  `extract.sh`. Haiku follows the skip-list reliably.
- **Hook fires twice on every turn.** Look for duplicate entries in `~/.claude/settings.json` under
  `hooks.Stop` — possible if the file was hand-edited between installer runs. Either re-run the
  installer (it dedupes) or remove the duplicate manually.

## Mental model

Three boundaries, each swappable without touching the others:

- **Trigger** — when to think about updating the word. Currently `Stop` (every assistant turn,
  rate-limited by `COOLDOWN`). Could be moved to `SessionEnd` for "one word per session" semantics.
- **Storage** — `current.json` (the visible word) plus `history.jsonl` (the running wordbook).
- **Render** — how the statusline displays the word. The snippet only reads JSON; you can rewrite
  the format without ever touching the hook.

If the user wants a richer experience later (Anki export, daily review prompt, frequency-based
spaced repetition over `history.jsonl`), build it as a fourth piece that consumes `history.jsonl` —
don't entangle it with the trigger or render layers.
