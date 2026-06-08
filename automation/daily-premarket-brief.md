Run the /premarket-brief skill now — it is built to run ~30 min before the US
cash open, which is exactly when this fires. Follow the skill end to end
(reconcile the prior briefing, build + sanity-check the packet, synthesize,
archive).

It REUSES the regime-scan + cross-scan caches that the post-close
daily-market-scan writes — never recompute them. If those caches are stale
(`regime.stale_days > 1`), say so in the briefing and lean on the live tape.

The skill writes files only — no git. After it finishes, check git status: if
there are changes, commit them on the current branch and push; otherwise do
nothing.
