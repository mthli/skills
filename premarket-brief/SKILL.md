---
name: premarket-brief
description: Pre-open US market briefing ~30 min before the bell — today's character, overnight tape, catalysts (econ calendar + earnings + headlines), sentiment, sectors, index levels, and an event-gated game plan tied to YOUR positions. Reuses regime-scan (structural backdrop) and cross-scan (consensus names), adding the overnight/event layer they miss; each run reconciles the prior briefing and archives it so the regime call calibrates over time. Triggers on "premarket", "morning brief", "daily game plan", "today's catalysts / economic calendar / earnings", "what to watch before the open", "premarket movers". Do NOT use for: a pure market-health read (use regime-scan), scanning for names to buy (momentum-scan / cross-scan), single-ticker price or fundamentals (yfinance), or a post-close recap.
---

# premarket-brief

The **pre-open event + overnight overlay** — the layer that answers *"what is
today's character and how do I play the open?"* It runs ~30 minutes before the
US open and is deliberately **fast-moving and event-driven**, which is exactly
what the structural scans miss:

- `regime-scan` answers *"is the market structurally healthy?"* — slow, computed
  from end-of-day internals (breadth, VIX term structure, credit, rotation).
- `premarket-brief` answers *"what changed overnight and what fires today?"* —
  the futures gap, Asia/Europe, the econ calendar, who reports, the headline
  sentiment gauge. None of that lives in any sister scan.

So this skill **reuses, never recomputes**: it reads regime-scan's latest state
as the structural backdrop and cross-scan's overlap names as the watchlist, then
layers the overnight tape + catalysts + your positions on top. The output is a
9-section briefing, archived daily, with a built-in expected-vs-actual feedback
loop so the regime call gets calibrated instead of evaporating.

`<SKILL_DIR>` below is the directory containing this file.

## Dependencies

`build_packet.py` auto-fetches its deps via `uv` — `yfinance>=1.3,<2` and
`pandas>=2` (yfinance powers the overnight tape, premarket movers, the live VIX
term structure, and analyst rating changes). The HTTP data sources — ForexFactory
econ calendar, Nasdaq earnings, Nasdaq economic-events backup, CNN Fear & Greed,
TradingView premarket gappers, and CNBC headline RSS — use the Python stdlib, no
API keys. The two web-scanner/RSS sources (TradingView, CNBC) are unofficial
endpoints; like every source here they're best-effort — any one failing is
recorded in `errors` and the briefing degrades gracefully. Reading the
sister-scan caches needs them to exist (run the post-close `daily-market-scan`
first ideally); the briefing says so if they're stale.

---

## The run — six steps, in order

### 1. Reconcile the previous briefing first (the feedback loop)

Before building today's, close the loop on the last one. Look in `archive/` for
the most recent `YYYY-MM-DD.md` that has **no `## Reconciliation` section yet**
and whose date is a *past* trading day. If you find one:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/build_packet.py --actuals <that-date> --tickers <names the briefing flagged>
```

Compare what that briefing **called** (regime direction, which sectors, whether
the index key levels held/broke, the focus names) against what the tape
**actually did**. Append a `## Reconciliation` section to that archive file with:
expected vs actual in 3–5 lines, then **one honest calibration line**
— what the call got right, what it missed, and *why* (e.g. "over-weighted a VIX
spike that futures never confirmed"). Also append a one-row summary to
`calibration.md` (create it if missing) so the hit/miss pattern is scannable
over time. This is the whole point of archiving — a briefing nobody grades is
just a horoscope.

If there's no un-reconciled briefing (first run, or already caught up), skip to step 2.

### 2. Build the data packet

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/build_packet.py
```

It prints the packet JSON to stdout and — **only when the run is in-window** —
saves a copy to `state/packets/<today>.json` (an out-of-window run prints but is
not saved; see step 3). **First check `session.valid` in the stdout (step 3).**
If valid, **read the saved file** — it's the single source of facts for
everything below. It already contains: overnight tape (incl. live VIX
term structure — VIX9D/VIX/VIX3M levels plus the VIX/VIX3M ratio & `shape`),
premarket movers, market-wide premarket gappers (`premarket_gappers` — top
large-cap movers outside your book, via TradingView), today's econ calendar +
earnings, recent analyst rating changes on your names + watchlist
(`rating_changes`), overnight macro headlines (`headlines`, CNBC RSS), Fear &
Greed, the regime-scan state row, the cross-scan overlap names, your parsed
positions (with `reports_today` / `in_overlap` joins), special-day flags, and an
`errors` list.

### 3. Gate on the run window — STOP if out-of-window

Check `session.valid` first. The skill is built for the 04:00–09:30 ET pre-open
window. If `session.valid` is **false** (phase `intraday` / `after-hours` /
`pre-dawn` / `overnight` / `non-trading-day` / `date-override`), the `premkt`
fields are **not** overnight gaps — they're live RTH / after-hours / stale prices,
and there is no valid pre-open tape to brief on. In that case **do not build,
synthesize, or archive a briefing**:

- Report `session.warning` to the user in 1–2 lines (which phase, why it's being
  skipped), and **STOP** — skip steps 4–6 entirely.
- The packet is intentionally **not saved** to `state/packets/` (the script
  enforces this), so there's nothing to clean up.
- Step 1's reconciliation still stands — grading a *past* briefing is independent
  of today's window, so keep that result; only today's forward briefing is skipped.
- **Override only on an explicit user request** for an out-of-window read. Then
  say so plainly, lead with `session.warning`, treat **all** premarket blocks as
  void, lean on futures + the overnight tape — and still **do not** archive it as
  the day's official briefing.

If `session.valid` is **true**, continue — the packet is raw facts and your job is
to not get fooled by them. Check these:

- **Internal consistency.** The classic trap: a big `vix.pct` move with the
  futures barely budging. ^VIX overnight/early prints are thin and can be stale
  or spike on low volume — if VIX says +40% but ES/NQ are ±0.3% and Europe is
  flat, the VIX print is suspect; flag it, don't headline it. Let the *futures
  gap and Europe* lead the risk read; treat a lone VIX number as corroboration,
  not the driver.
- **Staleness.** `regime.stale_days > 1` means the structural backdrop predates
  recent action — say so and lean on the live tape. (A "RISK-ON" cache captured
  before a −4% day is worse than no read if quoted uncritically.)
- **Degradation.** If `errors` is non-empty or the calendar source is
  `unavailable`, the briefing must *say which inputs were missing* rather than
  silently omit them. Honest gaps beat invisible ones.

### 4. Enrich positions (only if positions.md is non-empty)

For each position, the packet already flags earnings-today and watchlist
overlap. If you can locate the user's investment-notes repo, read the relevant
`distill-ticker` snapshot for thesis/conviction context (this skill's
`positions.md` deliberately holds *facts only*). If you can't find it, proceed
with packet facts — don't block on it.

### 5. Synthesize the briefing

Read **`references/briefing-template.md`** and follow it. It holds the 9-section
structure, the game-plan framing (section 9 is the one that can do harm if
written lazily — conditional/event-gated, tied to positions, never a directional
call), and the output honesty rules (don't pad, check the premarket `as_of`
date, state what was missing, times in ET).

### 6. Archive it

Only reached when `session.valid` was true (step 3 stops out-of-window runs
before here). Write the finished briefing to `archive/<today>.md`. That file is
the durable, git-tracked record the next run reconciles against. A same-day
re-run simply overwrites it — one briefing per day, last run wins.
