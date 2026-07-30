---
name: cross-scan
description: Cross-reference outputs from momentum-scan, base-breakout-scan, mean-reversion-scan, and unusual-options-scan to find tickers appearing in 2+ scans — the "agreement" watchlist where technical + flow signals stack (backtested — read as attention-crowding to research or de-risk, not stacked conviction). Use when the user wants overlap / consensus picks across the sister scans, a daily overlap watchlist, or to act on the four scans together rather than separately. Triggers on "cross-scan", "consensus picks", "overlap", "agreement across scans", "what's in multiple scans", "combine/merge scan outputs". Do NOT use for single-scan re-runs, single-ticker lookups, or fundamentals questions — invoke the relevant individual scan instead.
---

# cross-scan

Aggregate the latest snapshots from the four sister scans — **momentum-scan**, **base-breakout-scan**, **mean-reversion-scan**, **unusual-options-scan** — and surface tickers appearing in **two or more** of them. The premise: each scan answers a different question (what's running / what's setting up / what's oversold but fine / where is options flow positioning), and any single signal can be a fluke or a noise pattern. When **two or three** of these independent signals fire on the same name on the same day, that's the small subset most worth a research dig.

⚠️ **What the 2026-05→07 overlap backtest actually found** (see **Backtested outcomes**): the conviction premise ran **backwards** in-sample — the more scans agreed, the worse the forward returns (3-scan names −4.5% excess at T+10 vs −1.5% for single-scan). The main driver is UOA: co-appearing in the options scan was a **froth/crowding tell, not confirmation** (consistent with UOA's own backtest). Technical+technical overlaps were merely neutral. Read the output as an **attention-crowding map and de-risking prompt**, with tech+tech cells as research candidates — never as a stacked-conviction buy list.

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

# Only 3+ scan overlaps (⚠️ backtest: the most-crowded names, not the best)
python <SKILL_DIR>/scripts/aggregate.py --min-overlap 3

# Strict date alignment — only use data from a specific trading day
python <SKILL_DIR>/scripts/aggregate.py --date 2026-05-22

# Subset of scans (e.g., skip mean-reversion if its data is stale)
python <SKILL_DIR>/scripts/aggregate.py --scans momentum,base-breakout,unusual-options

# Show more rows
python <SKILL_DIR>/scripts/aggregate.py --top-n 60

# Machine-readable JSON
python <SKILL_DIR>/scripts/aggregate.py --format json

# Outcome backtest: reconstruct daily overlap sets from the four scans'
# histories and measure forward excess returns (see "Backtested outcomes")
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/backtest_overlap.py
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--date` | (latest each) | If unset, each scan contributes its own most-recent snapshot (dates may differ — they're shown per scan in the report header). If set to `YYYY-MM-DD`, every scan must have data for that exact date; scans missing that date emit a warning row and are excluded. Use strict mode when all four scans have been run on the same trading day and you want clean date alignment. |
| `--min-overlap` | 2 | Minimum number of scans a ticker must appear in to be reported. 2 is the useful default. ⚠️ 3+ was assumed "high-conviction" but the backtest inverted that — 3-scan names ran −4.5% xT+10 (**Backtested outcomes** #1); use `--min-overlap 3` to find the most *crowded* names, not the best. 4 is extremely rare (5 ticker-days in 2.3 months). |
| `--top-n` | 30 | Hard cap on total tickers displayed in the markdown report. Tier-3+ rows get priority; tier-2 fills the remaining budget. (The full overlap set always lands in the JSON output regardless.) |
| `--scans` | (all four) | Comma-separated subset to include. Names: `momentum`, `base-breakout`, `mean-reversion`, `unusual-options`. Skip a scan if its history is stale or missing without forcing the user to look at warnings. |
| `--format` | markdown | `markdown` or `json`. |
| `--scans-dir` | `~/.claude/skills` | Parent directory holding the four sister-scan folders. Override if the scans live elsewhere. |
| `--refresh` | off | Auto-refresh any requested scan whose latest snapshot is older than 3 days (the STALE threshold) or missing entirely. Re-runs each via `uv run`, then proceeds with aggregation. Each scan is invoked with its own "save even on non-trading day" flag (`--save-stale` for the CSV scans, `--allow-same-day` for UOA), so refresh works on weekends and holidays too. Cross-scan re-reads the snapshot date after each refresh and warns if it didn't advance (so a scan that exits cleanly without saving doesn't get a false ✓). Refresh failures don't abort the report. Ignored when `--date` is set — strict-date queries are asking about a specific historical date, so re-running a scan today wouldn't help. |

## What this skill does NOT do

- **Does not re-run any scan by default.** Run the individual scans first, or pass `--refresh` to have cross-scan re-run any stale ones for you. Without `--refresh`, a stale or missing scan shows a warning in the freshness header but won't be regenerated.
- **Does not invent a composite "alpha score" across scans.** Each scan's quality metric (momentum's score, base-breakout's base_score, mean-reversion's RSI(2), UOA's Vol/OI + notional) measures different things — combining them into a single number would be more confusing than helpful. Instead, the output shows each scan's rank/score side-by-side and lets the human read the pattern.
- **Does not recommend specific trades.** This is a *research and de-risking list*, not a buy list. Tickers appearing in 3+ scans deserve a closer look — and per the backtest, that look most often resolves to "crowded", not "confirmed". Confirm catalysts, check earnings calendar, look at the chart, size the position, etc.

## Output shape

```
# Cross-scan overlap — 2026-05-23 (latest each)

**Scan freshness**:
- momentum-scan: 2026-05-22 (75 tickers)
- base-breakout-scan: 2026-05-22 (55 tickers)
- mean-reversion-scan: 2026-05-14 (19 tickers) ⚠️ STALE (>3 days old)
- unusual-options-scan: 2026-05-23 (232 tickers)

**Overlap summary**: 2 in ≥3 scans · 50 in ≥2 scans

## Tickers in ≥3 scans

_⚠️ Backtest (2026-05→07): overlap count ranked conviction BACKWARDS — 3-scan names ran −4.5% xT+10 vs −1.5% for single-scan. Read as attention-crowding, not conviction (see SKILL.md Backtested outcomes)._

| Ticker | Sector | # | mom | base | mr | uoa | Composite read |
|---|---|---|---|---|---|---|---|
| **CIEN** | Technology | 3 | #36 (4.1) | #11 (base 55) | — | #112 (V/O 16, $1.1M, ⚡) | base setup + options attention |
| **JBL**  | Technology | 3 | #57 (2.7) | #16 (base 52) | — | #106 (V/O 9, $753k, ⚡📊) | base + hot call tape — ⚠️ froth tell, not confirmation · leader + hot call tape — ⚠️ crowding tell |

## Tickers in 2 scans — 28 of 50 shown

| Ticker | Sector | # | mom | base | mr | uoa | Composite read |
|---|---|---|---|---|---|---|---|
| **AMD**  | Technology | 2 | #5 (10.7) | — | — | #3 (V/O 76, $6.9M, ⚡🎯🔥) | in 2 scans |
| **TS**   | Energy     | 2 | — | #20 (base 50) | — | #59 (V/O 840, $1.9M, ⚡📊) | base + hot call tape — ⚠️ froth tell, not confirmation |
| **CCJ**  | Energy     | 2 | — | — | #13 (RSI2 8.37) | #74 (V/O 12, $63k, ⚡🎯🔥) | in 2 scans |
| ...
```

Notes on the composite read column:

- It's a one-line human-readable label, not a score. The script picks based on **which** scans the ticker appears in (and, where UOA is one of them, the call/put ratio direction). Labels were re-worded after the 2026-05→07 backtest — no UOA-involving label reads as bullish anymore:
  - `base-breakout + unusual-options` (call-heavy ratio ≥ 3.0) → `base + hot call tape — ⚠️ froth tell, not confirmation` (backtest: −2.1% xT+10, 37% Beat10)
  - `base-breakout + unusual-options` (put-heavy ratio ≤ 0.33) → `base setup but put-flow disagrees — caution`
  - `base-breakout + unusual-options` (other) → `base setup + options attention`
  - `momentum + mean-reversion` → `pullback in a leader` (the least-bad research label: median +0.8% xT+10, mean dragged negative by tails)
  - `momentum + unusual-options` (call-heavy) → `leader + hot call tape — ⚠️ crowding tell` (backtest: −3.0% xT+10)
  - `momentum + unusual-options` (put-heavy) → `⚠️ leader with bearish positioning` (backtest-validated: −5.3% xT+10)
  - `momentum + base-breakout` (alone) → `leader still consolidating`
  - `mean-reversion + base-breakout` (alone) → `oversold base candidate`
  - momentum + base + mr together → `leader, in base, pulled back hard — ⚠️ backtest-worst cell` (n=15, −8.0% xT+10, 10% Beat10)
  - Anything else falls back to a plain `in N scans` label — the cell is still useful because the per-scan rank columns are right there.
- Multiple labels can apply to the same row (joined by ` · `).
- The reads are heuristics with in-sample numbers attached, not guarantees. They surface the *type* of overlap so the human can decide if it matches a thesis worth pursuing.

## Backtested outcomes (2026-05-22 → 2026-07-29 sample)

`scripts/backtest_overlap.py` reconstructs the daily overlap sets from the four sister scans' state histories (37 joined sessions, 9,847 resolved ticker-days) and measures the underlying's forward **excess** returns vs the equal-weight universe mean at T+5/T+10/T+20. Findings, strongest first:

1. **Overlap count ranked conviction backwards.** 1 scan: −1.50% xT+10 · 2 scans: −2.36% · 3 scans: **−4.46% (Beat10 36%)** · all 4: −10.8% (n=5). The "highest conviction" tier was the worst bucket, stable across both halves of the sample. (Composition note: the 1-scan row is dominated by UOA-only names; the within-scan tables below are the like-for-like comparison.)
2. **UOA co-membership is contamination, not confirmation.** Within each technical scan's own pool (xT+10): momentum alone −1.52% → +UOA **−3.54%**; base-breakout alone **+0.39%** → +UOA −1.48%; mean-reversion alone −0.15% → +UOA **−3.59%**. Adding UOA on top of a tech+tech overlap made every cell worse still (−4.6/−5.2/−3.6%). This is exactly what UOA's own backtest predicts: its flags mark attention froth that underperforms. **UOA presence in an overlap is a red flag, not a green one.**
3. **Tech+tech overlap is the only defensible cell — and it's merely neutral.** momentum + another technical (no UOA): −0.54% xT+10, Beat10 55% (vs −1.52% alone); base+tech −0.53%/54%; mr+tech −0.64%/55%. Not an edge; a "least-bad" pocket that earns the research dig the skill was designed for.
4. **The old ⭐ label was a lemon; the ⚠️ was right.** "base + call-heavy UOA — best pattern": −2.07% xT+10, Beat10 37%. "momentum + put-heavy UOA — warning": −5.28% xT+10 — directionally correct, though note *everything* UOA-touched was bad; put-heavy was just worse. The rarest label, mom+base+mr ("rare"): n=15, **−7.95% xT+10, Beat10 10%** — the single worst labeled cell.
5. **"Pullback in a leader" is the least-bad label but not "safest entry".** Mean −1.55% xT+10 (dragged by tails), median **+0.77%**, Beat10 52% — a coin-flip with negative skew, not a validated entry pattern.
6. **Better rank does not rescue overlaps.** Top-10-ranked overlap names: −3.27% xT+10 — *worse* than rank 11-30 (−1.91%). The top-ranked names in an overlap are usually the most extended ones. Rank-weighting the consensus would not have helped in this window.
7. **Fresh overlaps beat stale ones.** 1st session of an overlap: +0.12% xT+5 / −1.60% xT+10; by the 4th+ consecutive session: −1.38% / **−4.37%**. If the list is used at all, it's a day-it-forms signal; names that keep re-appearing are crowding, not confirming.

**Caveats**: one ~2-month RISK-ON window; ticker-days cluster within episodes (spell table row "1st session" is the per-episode view); membership sizes are wildly asymmetric (momentum ~87/day, UOA ~157/day, base ~37, MR ~37), so overlap with UOA is cheap by construction — judge via within-scan comparisons; the universe baseline is the current UOA universe (survivorship, contains the scanned names); scan pools skew high-beta in different degrees, so part of every gap vs universe is style. Re-run quarterly — especially #2 in a weaker tape, where hedging-driven put flow may behave differently.

## How to interpret (what to do after running)

**Where the backtest above and the old doctrine disagree, lead with the backtest and say so.**

1. **Frame the report as an attention-crowding map, not a conviction list.** More agreement ≠ more edge — in-sample it meant *less* (Backtested outcomes #1). The practical uses: tech+tech overlaps as research candidates (#3), and UOA-touched overlaps as de-risking prompts on names the user holds (#2).

2. **Treat the uoa column as a red flag on any overlap row.** Every technical scan's names did worse when UOA co-flagged them (#2). If the user holds a name showing `⚠️ froth tell` / `⚠️ crowding tell` / `⚠️ bearish positioning`, that's the actionable output — not a new-buy candidate. The put-heavy warning is the one label the backtest validated (−5.3% xT+10).

3. **The research-worthy cells are tech+tech, on the day they form.** momentum+base / momentum+mr / base+mr without UOA ran neutral (#3), and fresh overlaps beat persistent ones (#7) — prioritize names in their 1st-2nd overlap session, and drop names that have been overlapping for a week (crowding, not confirmation). For `pullback in a leader`, note the shape: median mildly positive, mean dragged by tails (#5) — a stop matters more than usual.

4. **3+ scan overlaps deserve attention as risk, not as opportunity.** Typically <5 names per day. In-sample they ran −4.5% xT+10 (Beat10 36%), and the mom+base+mr triple was the worst cell of all (#4). Read the full context (sector, news, earnings) — the most common resolution is "everyone already piled in".

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
- **No per-scan weighting.** A ticker that's #1 in momentum + #29 in UOA gets the same "in 2 scans" treatment as a ticker that's #15 in momentum + #2 in UOA. The output columns expose the actual ranks so the human can re-prioritize; the script does not. The 2026-05→07 backtest suggests this limitation is *not* worth fixing: top-10-ranked overlap names ran **worse** than rank-11-30 ones (−3.27% vs −1.91% xT+10) — rank-weighting the consensus would have hurt (**Backtested outcomes** #6).
- **UOA snapshot dates can be off by one calendar day from the other scans' run-id dates** because UOA uses today's ET date even on weekend runs, while the other scans only record on actual trading days. The freshness header makes this visible; the overlap join itself is purely on ticker, not date.
- **No historical persistence tracking yet.** Sister scans track streak (consecutive runs a ticker appeared in). cross-scan does not — every invocation reads only the latest snapshot of each scan. The backtest measured what this hides: fresh overlaps (1st session) ran +0.12% xT+5 while 4th+-session overlaps ran −4.37% xT+10 (**Backtested outcomes** #7) — making an overlap-spell column the highest-value future enhancement, as a *staleness discount* rather than a persistence bonus.
- **Sector field comes from whichever scan provides it.** The sister scans each cache sector info; cross-scan picks the first non-empty value. If they ever disagree (rare — same Yahoo metadata source), the precedence is momentum → base-breakout → mean-reversion → UOA.
