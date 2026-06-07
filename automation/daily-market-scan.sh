#!/usr/bin/env bash
set -euo pipefail

REPO="$HOME/GitHub/skills"
PROMPT_FILE="$REPO/automation/daily-market-scan.md"

[ -f "$PROMPT_FILE" ] || { echo "missing prompt: $PROMPT_FILE" >&2; exit 1; }

cd "$REPO"
ccp -p allow -e CLAUDE_VOCAB_EXTRACTING=1 "$(cat "$PROMPT_FILE")"
