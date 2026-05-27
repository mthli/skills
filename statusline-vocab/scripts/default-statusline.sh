#!/usr/bin/env bash
# Claude Code statusLine — minimal default bundled with the statusline-vocab skill.
# Installed only when no existing ~/.claude/statusline-command.sh is present.
# Users who already have a statusline keep theirs; the skill prints a snippet for
# manual integration in that case.

input=$(cat)

cwd=$(echo "$input"  | jq -r '.workspace.current_dir // .cwd // "."')
dir=$(basename "$cwd")
model=$(echo "$input" | jq -r '.model.display_name // ""')
used=$(echo "$input"  | jq -r '.context_window.used_percentage // empty')

line="$dir"
[ -n "$model" ] && line="${line} · ${model}"
if [ -n "$used" ]; then
  used_int=$(printf "%.0f" "$used")
  line="${line} · ctx:${used_int}%"
fi

# vocab:start — statusline-vocab skill
vocab_file="$HOME/.claude/vocab/current.json"
if [ -f "$vocab_file" ]; then
  v_word=$(jq -r '.word // empty' "$vocab_file" 2>/dev/null)
  if [ -n "$v_word" ] && [ "$v_word" != "null" ]; then
    v_emoji=$(jq -r   '.emoji // "📖"' "$vocab_file" 2>/dev/null)
    v_ipa=$(jq -r     '.ipa // ""'      "$vocab_file" 2>/dev/null)
    v_pos=$(jq -r     '.pos // ""'      "$vocab_file" 2>/dev/null)
    v_meaning=$(jq -r '.meaning // ""'  "$vocab_file" 2>/dev/null)
    line="${line} · ${v_emoji} \033[1;94m${v_word}\033[0m \033[2m${v_ipa}\033[0m \033[0;33m${v_pos}\033[0m ${v_meaning}"
  fi
fi
# vocab:end

# %b interprets the \033 escape sequences embedded above into ANSI color codes.
printf '%b\n' "$line"
