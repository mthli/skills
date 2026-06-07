#!/usr/bin/env bash
set -euo pipefail

# Resolve everything from this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
PROMPT_FILE="$SCRIPT_DIR/daily-market-scan.md"

[ -f "$PROMPT_FILE" ] || { echo "missing prompt: $PROMPT_FILE" >&2; exit 1; }

cd "$REPO"
ccp -s daily-market-scan -e CLAUDE_VOCAB_EXTRACTING=1 "$(cat "$PROMPT_FILE")"
