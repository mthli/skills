#!/usr/bin/env bash
# vocab/extract.sh — Stop hook: extract one "worth learning" English word
# from the latest assistant turn and save to ~/.claude/vocab/current.json.
# The statusline reads that file and renders the word inline.
#
# Installed by the statusline-vocab skill (https://github.com/.../skills).
set -uo pipefail

# --- Knobs ---------------------------------------------------------------
COOLDOWN=180                       # seconds; skip if current.json is fresher than this
MODEL="claude-haiku-4-5"           # model used to pick the word
DEFAULT_LANG="Chinese"             # target language for the meaning field; override in $VOCAB_DIR/config
# -------------------------------------------------------------------------

VOCAB_DIR="$HOME/.claude/vocab"
CURRENT="$VOCAB_DIR/current.json"
HISTORY="$VOCAB_DIR/history.jsonl"
LOG="$VOCAB_DIR/extract.log"
CONFIG="$VOCAB_DIR/config"
mkdir -p "$VOCAB_DIR"

# Re-entry guard: this hook spawns `claude -p`, which starts a nested Claude
# Code session. When that session ends it would re-fire the Stop hook, which
# would call this script again — infinite loop. The env var breaks the cycle.
# Log the skip so debugging "why isn't this firing?" has a breadcrumb.
if [ "${CLAUDE_VOCAB_EXTRACTING:-}" = "1" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] skipped: re-entry guard (nested claude -p)" >>"$LOG" 2>/dev/null
  exit 0
fi
export CLAUDE_VOCAB_EXTRACTING=1

# Target language for the `meaning` field. Read from $CONFIG (key=value, one
# per line) so the user can swap languages without editing this script.
# Strip only CR/LF then trim edge whitespace — multi-word language names
# (e.g. "Brazilian Portuguese") must keep their internal spaces.
TARGET_LANG="$DEFAULT_LANG"
if [ -f "$CONFIG" ]; then
  cfg_lang=$(grep '^lang=' "$CONFIG" 2>/dev/null | head -1 | cut -d= -f2- \
    | tr -d '\r\n' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
  [ -n "$cfg_lang" ] && TARGET_LANG="$cfg_lang"
fi

# Cooldown: skip if current.json was updated very recently. Stop fires on
# every assistant turn, but the user only wants the word to change every
# few minutes — not on every short reply within a conversation.
if [ -f "$CURRENT" ]; then
  last=$(stat -f %m "$CURRENT" 2>/dev/null || stat -c %Y "$CURRENT" 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $((now - last)) -lt "$COOLDOWN" ] && exit 0
fi

input=$(cat)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)
[ -z "$transcript" ] && exit 0
[ ! -f "$transcript" ] && exit 0

# Pull plain text from the last ~30 transcript entries. Each JSONL line has
# .message.content as either a string or an array of content blocks (text,
# tool_use, tool_result, …); flatten both shapes into text and ignore the rest.
excerpt=$(tail -n 30 "$transcript" 2>/dev/null \
  | jq -r '
      (.message.content // empty) as $c
      | if ($c | type) == "array" then
          ($c | map(.text? // "") | join(" "))
        elif ($c | type) == "string" then
          $c
        else empty end
    ' 2>/dev/null \
  | tr '\r\n' '  ' \
  | head -c 6000 \
  | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null)  # drop a partial multibyte char if head -c cut mid-character

[ -z "$excerpt" ] && exit 0

prompt='You are an English vocabulary tutor for a '"$TARGET_LANG"'-speaking learner.

From the conversation excerpt below, pick ONE English word that is worth learning. Criteria:
- The word MUST appear in the excerpt
- Skip trivially common words (the, use, make, thing, get, do, have, want)
- Favor words a fluent learner might recognize but not actively own — interesting, vivid, or precise
- Use the lemma / base form (e.g. "ephemeral" not "ephemerally", "stride" not "strode")

Output ONLY valid JSON, no markdown fence, no prose. Exact schema:
{"word":"...","ipa":"/...US IPA.../","pos":"n.|v.|adj.|adv.|prep.|conj.","meaning":"<concise translation in '"$TARGET_LANG"', under ~15 characters or 3-4 words>","emoji":"<a single emoji that best represents the word'\''s meaning>"}

If no suitable word exists, output exactly: {"word":null}

Excerpt:
'"$excerpt"

result=$(printf '%s' "$prompt" | claude -p --model "$MODEL" 2>>"$LOG")

# The model may wrap output in ```json fences despite the instruction; strip them defensively.
clean=$(printf '%s' "$result" | sed -E 's/^```(json)?//; s/```$//' | tr -d '\r')

# Validate: must parse as JSON, .word must be a non-empty string.
if ! printf '%s' "$clean" | jq -e '.word and (.word | type == "string") and (.word | length > 0)' >/dev/null 2>&1; then
  printf '[%s] skipped (no valid word): %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(printf '%s' "$result" | head -c 200)" >>"$LOG"
  exit 0
fi

# Stamp with a timestamp and write atomically so the statusline never sees a partial file.
stamped=$(printf '%s' "$clean" | jq -c --arg ts "$(date '+%Y-%m-%dT%H:%M:%S')" '. + {ts: $ts}')
printf '%s\n' "$stamped" > "$CURRENT.tmp" && mv "$CURRENT.tmp" "$CURRENT"
printf '%s\n' "$stamped" >> "$HISTORY"

# Cap history.jsonl size with hysteresis: when it exceeds 10000 entries, trim to
# the most recent 5000. Keeps the file bounded without trimming on every run.
hist_lines=$(wc -l <"$HISTORY" 2>/dev/null | tr -d ' ')
if [ "${hist_lines:-0}" -gt 10000 ] 2>/dev/null; then
  tail -n 5000 "$HISTORY" > "$HISTORY.tmp" && mv "$HISTORY.tmp" "$HISTORY"
fi
