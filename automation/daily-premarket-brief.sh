#!/usr/bin/env bash
set -euo pipefail

# Resolve everything from this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
PROMPT_FILE="$SCRIPT_DIR/daily-premarket-brief.md"

[ -f "$PROMPT_FILE" ] || {
	echo "missing prompt: $PROMPT_FILE" >&2
	exit 1
}

# We want to run at 09:00 America/New_York — 30 min before the 09:30 cash open.
# The machine runs on Beijing time, so pm2 fires this at 21:00 Beijing, which is
# 09:00 EDT in summer but only 08:00 EST in winter. Rather than hard-code a
# Beijing time that drifts an hour with US daylight-saving, fire early and sleep
# the remaining gap to 09:00 ET. All clock math is done in TZ=America/New_York,
# so DST is handled for free.

# Read the ET clock once — day-of-week plus seconds-of-day — so the weekend gate
# and the sleep math share a single atomic timestamp. 10# forces decimal below,
# so a zero-padded hour like "08" is not misread as octal.
read -r et_dow et_h et_m et_s <<<"$(TZ=America/New_York date '+%u %H %M %S')"
et_now="${et_h}:${et_m}:${et_s}"

# US markets are closed on weekends. cron already restricts to Beijing Mon–Fri
# (= ET Mon–Fri, since 21:00 Beijing is the same calendar weekday's morning in
# ET), but gate on the ET weekday too as a cheap belt-and-suspenders.
# (Market holidays are NOT detected here — on a holiday the brief still builds,
# but the skill flags it as a non-trading-day and degrades gracefully, same
# spirit as the post-close scan. Acceptable for an automated best-effort job.)
if [ "$et_dow" -gt 5 ]; then
	echo "ET is a weekend (dow=$et_dow) — skipping premarket brief" >&2
	exit 0
fi

# Seconds until our 09:00 ET target. Done in seconds-of-day (not epoch) to stay
# portable across BSD/GNU date and sidestep date-string parsing.
et_now_secs=$((10#$et_h * 3600 + 10#$et_m * 60 + 10#$et_s))
sleep_secs=$((9 * 3600 - et_now_secs))
open_secs=$((9 * 3600 + 30 * 60)) # 09:30 ET cash open = first tick past the pre-open window.

if [ "$sleep_secs" -gt 7200 ]; then
	# Never legitimately more than ~1h early (08:00 EST is the worst case). A gap
	# this large means a misfired cron or a bad clock — skip rather than run hours
	# off-target.
	echo "ET now $et_now — ${sleep_secs}s until 09:00 ET is implausibly early; skipping" >&2
	exit 0
elif [ "$et_now_secs" -ge "$open_secs" ]; then
	# At/after the 09:30 cash open — the pre-open window is over (09:30:00 is the
	# open, matching the skill's `mins < 09:30` pre-open boundary). This fires when pm2
	# runs the one-shot immediately on `pm2 start`/restart/boot-resurrect at an
	# arbitrary wall-clock time (e.g. mid-session). A brief built intraday has no
	# valid overnight-gap data — the packet's session.valid would be false — so
	# skip rather than archive a void briefing. The scheduled 21:00-Beijing cron
	# fire still lands inside the window and runs normally.
	echo "ET now $et_now — past the 09:30 open; pre-open window over, skipping" >&2
	exit 0
elif [ "$sleep_secs" -gt 0 ]; then
	echo "ET now $et_now — sleeping ${sleep_secs}s until 09:00 ET" >&2
	sleep "$sleep_secs"
else
	echo "ET now $et_now — at/after 09:00 ET, running now" >&2
fi

cd "$REPO"

# Reap any orphaned session first. pm2's cron_restart can double-fire (:59:59 +
# :00:00); restarting the in-flight run SIGKILLs the tree after the 1.6s grace
# (the SIGINT lands on this wrapper, which bash won't die from while waiting on
# ccp), so ccp's cleanup trap never runs and the tmux session — hosted by the
# separate tmux server — survives. ccp then refuses the name collision forever
# after. This job owns the name and runs never overlap, so a pre-existing
# session is always such an orphan.
tmux kill-session -t '=daily-premarket-brief' 2>/dev/null || true # '=' = exact match, never tmux's prefix fallback

# exec so pm2's stop signal reaches ccp itself: its INT/TERM trap kills the
# tmux session and exits promptly instead of escalating to SIGKILL.
exec ccp -s daily-premarket-brief -e CLAUDE_VOCAB_EXTRACTING=1 "$(cat "$PROMPT_FILE")"
