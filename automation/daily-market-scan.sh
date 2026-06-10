#!/usr/bin/env bash
set -euo pipefail

# Resolve everything from this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
PROMPT_FILE="$SCRIPT_DIR/daily-market-scan.md"

[ -f "$PROMPT_FILE" ] || { echo "missing prompt: $PROMPT_FILE" >&2; exit 1; }

cd "$REPO"

# Reap any orphaned session first. pm2's cron_restart can double-fire (:59:59 +
# :00:00); restarting the in-flight run SIGKILLs the tree after the 1.6s grace
# (the SIGINT lands on this wrapper, which bash won't die from while waiting on
# ccp), so ccp's cleanup trap never runs and the tmux session — hosted by the
# separate tmux server — survives. ccp then refuses the name collision forever
# after. This job owns the name and runs never overlap, so a pre-existing
# session is always such an orphan.
tmux kill-session -t '=daily-market-scan' 2>/dev/null || true # '=' = exact match, never tmux's prefix fallback

# exec so pm2's stop signal reaches ccp itself: its INT/TERM trap kills the
# tmux session and exits promptly instead of escalating to SIGKILL.
exec ccp -s daily-market-scan -e CLAUDE_VOCAB_EXTRACTING=1 "$(cat "$PROMPT_FILE")"
