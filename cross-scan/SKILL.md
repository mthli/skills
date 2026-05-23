---
name: cross-scan
description: Cross-reference outputs from momentum-scan, base-breakout-scan, mean-reversion-scan, and unusual-options-scan to find tickers appearing in 2+ scans — the highest-conviction "agreement" picks where technical + flow signals stack. Use when the user wants overlap / consensus picks across the sister scans, a high-conviction daily watchlist, or to act on the four scans together rather than separately. Triggers on "cross-scan", "consensus picks", "overlap", "agreement across scans", "what's in multiple scans", "combine/merge scan outputs". Do NOT use for single-scan re-runs, single-ticker lookups, or fundamentals questions — invoke the relevant individual scan instead.
---

# cross-scan

Aggregate the latest snapshots from the four sister scans — **momentum-scan**, **base-breakout-scan**, **mean-reversion-scan**, **unusual-options-scan** — and surface tickers appearing in **two or more** of them. The premise: each scan answers a different question (what's running / what's setting up / what's oversold but fine / where is options flow positioning), and any single signal can be a fluke or a noise pattern. When **two or three** of these independent signals fire on the same name on the same day, that's the small subset most worth a research dig.

By default this skill is purely an aggregator — it reads the four scans' existing state files. Pass `--refresh` to have it re-run any stale sister scan first (snapshots older than 3 days, including missing ones), so you can drive a "make sure everything is fresh and show me the overlap" workflow in one command. Refreshes run silently (no progress output) for ~1-3 minutes each. After each refresh cross-scan verifies that the snapshot date actually advanced — if a scan exited cleanly but didn't write new data (e.g. weekend save-skip), you get an explicit warning rather than a misleading ✓. Either way, refresh failures don't abort the report; the freshness header on the markdown output always tells you what you actually got.

**Dependencies**: Python ≥ 3.10 standard library only for the aggregation itself. `--refresh` shells out to the sister scans, which need `uv` in `PATH` (they auto-fetch `yfinance`, `pandas`, and `numpy` via `uv run --with`).

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Default — latest snapshot from each scan, min overlap 2, top 30 tickers
python <SKILL_DIR>/scripts/aggregate.py

# Auto-refresh any sister scan whose snapshot is >3 days old (or missing),
# then aggregate. The one-command "ensure fresh + show overlap" workflow.
python <SKILL_DIR>/scripts/aggregate.py --refresh

# Higher conviction only (3+ scans must agree)
python <SKILL_DIR>/scripts/aggregate.py --min-overlap 3

# Strict date alignment — only use data from a specific trading day
python <SKILL_DIR>/scripts/aggregate.py --date 2026-05-22

# Subset of scans (e.g., skip mean-reversion if its data is stale)
python <SKILL_DIR>/scripts/aggregate.py --scans momentum,base-breakout,unusual-options

# Show more rows
python <SKILL_DIR>/scripts/aggregate.py --top-n 60

# Machine-readable JSON
python <SKILL_DIR>/scripts/aggregate.py --format json
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--date` | (latest each) | If unset, each scan contributes its own most-recent snapshot (dates may differ — they're shown per scan in the report header). If set to `YYYY-MM-DD`, every scan must have data for that exact date; scans missing that date emit a warning row and are excluded. Use strict mode when all four scans have been run on the same trading day and you want clean date alignment. |
| `--min-overlap` | 2 | Minimum number of scans a ticker must appear in to be reported. 2 is the useful default; 3 is high-conviction; 4 is extremely rare in practice (the four scans target different lifecycle phases, so all-four overlap is the exception). |
| `--top-n` | 30 | Hard cap on total tickers displayed in the markdown report. Tier-3+ rows get priority; tier-2 fills the remaining budget. (The full overlap set always lands in the JSON output regardless.) |
| `--scans` | (all four) | Comma-separated subset to include. Names: `momentum`, `base-breakout`, `mean-reversion`, `unusual-options`. Skip a scan if its history is stale or missing without forcing the user to look at warnings. |
| `--format` | markdown | `markdown` or `json`. |
| `--scans-dir` | `~/.claude/skills` | Parent directory holding the four sister-scan folders. Override if the scans live elsewhere. |
| `--refresh` | off | Auto-refresh any requested scan whose latest snapshot is older than 3 days (the STALE threshold) or missing entirely. Re-runs each via `uv run`, then proceeds with aggregation. Each scan is invoked with its own "save even on non-trading day" flag (`--save-stale` for the CSV scans, `--allow-same-day` for UOA), so refresh works on weekends and holidays too. Cross-scan re-reads the snapshot date after each refresh and warns if it didn't advance (so a scan that exits cleanly without saving doesn't get a false ✓). Refresh failures don't abort the report. Ignored when `--date` is set — strict-date queries are asking about a specific historical date, so re-running a scan today wouldn't help. |

## What this skill does NOT do

- **Does not re-run any scan by default.** Run the individual scans first, or pass `--refresh` to have cross-scan re-run any stale ones for you. Without `--refresh`, a stale or missing scan shows a warning in the freshness header but won't be regenerated.
- **Does not invent a composite "alpha score" across scans.** Each scan's quality metric (momentum's score, base-breakout's base_score, mean-reversion's RSI(2), UOA's Vol/OI + notional) measures different things — combining them into a single number would be more confusing than helpful. Instead, the output shows each scan's rank/score side-by-side and lets the human read the pattern.
- **Does not recommend specific trades.** This is a *prioritized research list*, not a buy list. Tickers appearing in 3+ scans deserve a closer look — but "deserves a closer look" ≠ "buy". Confirm catalysts, check earnings calendar, look at the chart, size the position, etc.

## Output shape

```
# Cross-scan overlap — 2026-05-23 (latest each)

**Scan freshness**:
- momentum-scan: 2026-05-22 (75 tickers)
- base-breakout-scan: 2026-05-22 (55 tickers)
- mean-reversion-scan: 2026-05-14 (19 tickers) ⚠️ STALE (>3 days old)
- unusual-options-scan: 2026-05-23 (232 tickers)

**Overlap summary**: 2 in ≥3 scans · 50 in ≥2 scans

## Tickers in ≥3 scans (highest conviction)

| Ticker | Sector | # | mom | base | mr | uoa | Composite read |
|---|---|---|---|---|---|---|---|
| **CIEN** | Technology | 3 | #36 (4.1) | #11 (base 55) | — | #112 (V/O 16, $1.1M, ⚡) | base setup + options activity |
| **JBL**  | Technology | 3 | #57 (2.7) | #16 (base 52) | — | #106 (V/O 9, $753k, ⚡📊) | ⭐ pre-breakout + call flow — best pattern · trend + call-flow confirmation |

## Tickers in 2 scans (worth a look) — 28 of 50 shown

| Ticker | Sector | # | mom | base | mr | uoa | Composite read |
|---|---|---|---|---|---|---|---|
| **AMD**  | Technology | 2 | #5 (10.7) | — | — | #3 (V/O 76, $6.9M, ⚡🎯🔥) | in 2 scans |
| **TS**   | Energy     | 2 | — | #20 (base 50) | — | #59 (V/O 840, $1.9M, ⚡📊) | ⭐ pre-breakout + call flow — best pattern |
| **CCJ**  | Energy     | 2 | — | — | #13 (RSI2 8.37) | #74 (V/O 12, $63k, ⚡🎯🔥) | in 2 scans |
| ...
```

Notes on the composite read column:

- It's a one-line human-readable label, not a score. The script picks based on **which** scans the ticker appears in (and, where UOA is one of them, the call/put ratio direction):
  - `base-breakout + unusual-options` (call-heavy ratio ≥ 3.0) → `⭐ pre-breakout + call flow — best pattern`
  - `base-breakout + unusual-options` (put-heavy ratio ≤ 0.33) → `base setup but put-flow disagrees — caution`
  - `base-breakout + unusual-options` (other) → `base setup + options activity`
  - `momentum + mean-reversion` → `pullback in a leader` (classic buyable dip)
  - `momentum + unusual-options` (call-heavy) → `trend + call-flow confirmation`
  - `momentum + unusual-options` (put-heavy) → `⚠️ leader with bearish positioning`
  - `momentum + base-breakout` (alone) → `leader still consolidating`
  - `mean-reversion + base-breakout` (alone) → `oversold base candidate`
  - momentum + base + mr together → `leader, in base, pulled back hard — rare`
  - Anything else falls back to a plain `in N scans` label — the cell is still useful because the per-scan rank columns are right there.
- Multiple labels can apply to the same row (joined by ` · `).
- The reads are heuristics, not guarantees. They surface the *type* of overlap so the human can decide if it matches a thesis worth pursuing.

## How to interpret (what to do after running)

1. **`base-breakout + unusual-options` is the most interesting overlap** when it shows up — technical setup ready AND someone is positioning. This is the closest thing to a "leaked deal / catalyst" pattern surfaceable from public data. Always cross-check the earnings calendar before treating UOA-confirmed setups as informed flow (post-news call buying is normal).

2. **`momentum + mean-reversion` is the safest entry pattern** — confirmed leader that just pulled back. Lower edge but easier execution. Especially valid if the mean-reversion candidate also sits in a sector that's on momentum-scan as a sector leader.

3. **`momentum + unusual-options put-heavy` is a warning sign**, not an opportunity. A name running hard with institutional put-flow stacking up means something doesn't quite agree. Don't add to longs at extended levels when the smart-money tape disagrees.

4. **3+ scan overlaps are rare and worth full attention** — typically <5 names per day, often zero. When they appear, read the entire context (sector, recent news, earnings calendar) before forming a view.

5. **An empty overlap report is also a signal** — it usually means the market is rotating or quiet. Don't force trades when no overlap shows up. Re-run the next day.

6. **Watch for "stale" warnings in the freshness header.** If mean-reversion was last run 9 days ago, its appearance in an overlap is meaningless — it's just old data. The script flags scans older than 3 trading days; treat stale-flagged scans as informational only.

## State files

This skill keeps no state of its own. All data is read from the four sister scans' `state/` directories:

- `~/.claude/skills/momentum-scan/state/history.csv`
- `~/.claude/skills/base-breakout-scan/state/history.csv`
- `~/.claude/skills/mean-reversion-scan/state/history.csv`
- `~/.claude/skills/unusual-options-scan/state/history/YYYY-MM-DD.md`

If any of these are missing the skill prints a warning for that scan and continues — a partial cross-scan (e.g., 3 of 4) is still useful.

## Recommended cadence

Run this after running the underlying scans on the same day. A typical daily workflow:

1. Evening (after US close): run `/unusual-options-scan` (OI is EOD-refreshed)
2. Same evening or next morning: run `/mean-reversion-scan` (short horizon — refresh daily for usefulness)
3. Weekly (e.g., Friday close): refresh `/momentum-scan` and `/base-breakout-scan`
4. After the relevant scans are fresh: `/cross-scan` to surface overlaps

If `momentum-scan` and `base-breakout-scan` haven't been re-run in a few days, that's usually fine — their signals move slowly. The freshness warnings in cross-scan's output tell you whether to trust each scan's contribution.

## Known limitations

- **Date misalignment is a real thing.** In "latest each" mode (the default), each scan's most-recent snapshot may carry a different date. Most often this is harmless (e.g., momentum from Friday, UOA from Saturday using Friday's close data is fine because Saturday's UOA used Friday's close). Occasionally it bites — if you ran mean-reversion 2 weeks ago and forgot, today's report would happily overlap it with this morning's other scans. The freshness header surfaces this; pay attention to it.
- **No per-scan weighting.** A ticker that's #1 in momentum + #29 in UOA gets the same "in 2 scans" treatment as a ticker that's #15 in momentum + #2 in UOA. The output columns expose the actual ranks so the human can re-prioritize; the script does not.
- **UOA snapshot dates can be off by one calendar day from the other scans' run-id dates** because UOA uses today's ET date even on weekend runs, while the other scans only record on actual trading days. The freshness header makes this visible; the overlap join itself is purely on ticker, not date.
- **No historical persistence tracking yet.** Sister scans track streak (consecutive runs a ticker appeared in). cross-scan does not — every invocation reads only the latest snapshot of each scan. A future enhancement could surface "ticker has been in 2+ scans for N consecutive days" by reading multiple historical snapshots, but v0 is a single-day join.
- **Sector field comes from whichever scan provides it.** The sister scans each cache sector info; cross-scan picks the first non-empty value. If they ever disagree (rare — same Yahoo metadata source), the precedence is momentum → base-breakout → mean-reversion → UOA.
