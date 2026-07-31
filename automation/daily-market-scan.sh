#!/usr/bin/env bash
set -euo pipefail

# Resolve everything from this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
PROMPT_FILE="$SCRIPT_DIR/daily-market-scan.md"

[ -f "$PROMPT_FILE" ] || { echo "missing prompt: $PROMPT_FILE" >&2; exit 1; }

cd "$REPO"

# Headless claude, exec'd so pm2's stop signal reaches it directly instead of
# escalating to SIGKILL. --dangerously-skip-permissions auto-approves every
# tool call — required: a detached run has nobody to answer a permission
# prompt. CLAUDE_VOCAB_EXTRACTING=1 silences the global statusline-vocab Stop
# hook, which would otherwise spawn a nested claude run after the turn.
exec env CLAUDE_VOCAB_EXTRACTING=1 \
  claude -p --dangerously-skip-permissions "$(cat "$PROMPT_FILE")"
