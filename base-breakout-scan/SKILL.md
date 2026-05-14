---
name: base-breakout-scan
description: "Scan US large-cap equities for stocks in valid pre-breakout bases — tight consolidations after a prior advance, with squeeze + volume dry-up + relative strength building. Use when the user wants to find stocks about to break out, identify coiled / VCP / cup-and-handle setups, look for 'what's about to run' rather than 'what's running', spot names that have been ranging for weeks/months and may be ready to move, or find Minervini Trend Template + base setups. Triggers on 'find breakout candidates', 'stocks setting up', 'compressed bases', 'tight ranges', 'ready to pop', 'near 52-week highs', 'VCP setups', 'coiled'. Also covers re-runs and parameter tweaks. The natural complement to momentum-scan — that one finds what's already running; this finds what's about to. Do NOT use for single-ticker chart analysis, ETF screening, value/contrarian picks, or generic explanations of base-and-breakout investing."
---

# base-breakout-scan

Find US equities in **valid pre-breakout bases** — tight consolidations near 52-week highs after a prior advance, with quality metrics (Bollinger Band squeeze, volume dry-up, rising relative strength) showing the setup is "loaded". Surface a daily watchlist of which names are 🚀 breaking out today, 🔥 imminent, ⏳ coiled, or 📊 still forming, and track which setups persist across runs.

The natural sibling of `momentum-scan`: that one finds **what's already running** (high trailing return); this one finds **what's about to run** (the build-up before the move). They filter for opposite price patterns by design — momentum-scan's `--min-return-pct 30` floor would reject every name this scan surfaces, since basers are ranging, not climbing.

**The big idea**: a stock that's already up 50% in 3 months is usually too extended to chase. A stock that ran up 50%, then consolidated tightly for 8-20 weeks while sellers exhausted and RS quietly improved, is a high-quality setup *if* the trend holds — that's what this finds. The literature backing is the William O'Neil / Mark Minervini / Stan Weinstein school: their published win rates on this exact pattern are 40-50%, which is enough to be profitable when paired with a 7-8% stop loss (asymmetric payoff).

By default each run surfaces four entry layers on every pick: (1) a **composite Base Score** (0-100) summarizing setup quality, (2) a **Signal** classification (🚀 breakout today / 🔥 imminent / ⏳ coiled / 📊 forming), (3) the **pivot price** to break above, and (4) an **ATR-based stop loss** (2.5× ATR by default). Persistence tracking via `state/history.csv` records each ET-date snapshot once so each subsequent run can show streak, rank changes, breakouts (left because they triggered), and breakdowns (left because they failed).

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2`, `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run — bases 6-40 weeks, score ≥ 40
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/scan.py

# Tighter quality filter (high-conviction only)
... python <SKILL_DIR>/scripts/scan.py --min-base-score 60

# Longer bases only (multi-month consolidations are higher conviction)
... python <SKILL_DIR>/scripts/scan.py --min-base-weeks 10

# Loosen the screen if today's market doesn't have many setups
... python <SKILL_DIR>/scripts/scan.py --min-base-weeks 4 --min-base-score 25

# Top RS-quintile only (Minervini's stricter cutoff)
... python <SKILL_DIR>/scripts/scan.py --min-rs-rating 80

# Inspect the run history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# Diagnose why few/no picks came back
... python <SKILL_DIR>/scripts/scan.py --verbose

# Machine-readable JSON
... python <SKILL_DIR>/scripts/scan.py --format json

# Strict gate: suppress top-N when SPY below rising 200DMA (bases fail more in bear tapes)
... python <SKILL_DIR>/scripts/scan.py --regime-gate strict

# Single-ticker check: "is AAPL in a tradeable base right now?" Bypasses the
# universe scan; prints which funnel stage AAPL passes/fails and all metrics.
... python <SKILL_DIR>/scripts/scan.py --ticker AAPL

# Tighter dropout-reason thresholds (only call something "broke_out" at +2% above pivot)
... python <SKILL_DIR>/scripts/scan.py --broke-out-pct 2.0

# Disable the Recent breakouts section
... python <SKILL_DIR>/scripts/scan.py --recent-breakout-days 0

# High-vol regime — relax the smoothness band so 2-3% noise doesn't kill scores
... python <SKILL_DIR>/scripts/scan.py --smoothness-band-pct 3.0

# Disable the vol-collapse acquisition-target filter (keep buyouts in the table — see parameter table)
... python <SKILL_DIR>/scripts/scan.py --vol-collapse-ratio 0
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--min-base-weeks` | 6 | Minimum base length in weeks. Lower = catch fresh consolidations earlier with more noise. The literature floor (O'Neil "minimum 5-week flat base") is around 5-6 weeks. |
| `--max-base-weeks` | 40 | Maximum base length. Very long bases (year+) often resolve down — momentum has bled out. |
| `--max-base-width` | 25 | Max width % of base (high-low) / high. Bases tighter than 15% are higher quality but rarer. Loosen if too few picks; tighten for higher conviction. |
| `--max-to-52w-high` | 15 | Max % distance from 52-week high. Closer to high = nearer to the resistance breakout. Beyond 15% means stock is in correction territory, not basing. |
| `--min-rs-rating` | 70 | Minervini's RS Rating threshold (top 30% of universe by O'Neil-style weighted 3/6/9/12-month return). Bump to 80 for top-quintile only. |
| `--min-base-score` | 40 | Composite Base Score floor (0-100) for display. See **Output shape** below for component breakdown. Bump to 60 for high-conviction only; lower to 25 in thin-cohort markets. |
| `--top-n` | 30 | How many candidates to display + log to history. |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower to include small-cap setups (which are richer in this pattern but noisier). |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match the screener reports (currently ~1000 US large caps at default mcap/volume floors). The screener returns at most **250 rows per request** (Yahoo's hard cap; `yf.screen` raises `ValueError` above that), so the universe is paginated automatically in 250-row pages with `offset` — at default filters that's ~5 paginated requests, taking a few extra seconds, but only on cache refresh (every 7 days). Pass an explicit positive integer to cap the universe (e.g. `250` for a one-request refresh, `500` for a middle ground); 0 / negative values are rejected by argparse. If you raise `--universe-count` above the number of tickers already in `state/universe.txt`, the cache is force-refreshed even within TTL — otherwise you'd silently get the smaller cached pool. Older yfinance versions (without `offset` support) fall back to a single 250-row page. |
| `--ticker` | — | Single-ticker check mode (e.g. `--ticker AAPL`). Bypasses the universe scan; shows which funnel stage the ticker passes/fails (Trend Template → Base detection → Score → ATR stops) with all per-stage metrics. **Works for any US ticker yfinance can fetch** — large-cap, small-cap, ADR, even foreign-listed — the universe is not consulted. ~2-5s vs ~30-60s for a full scan. Honors `--format json` for structured output. No history is written. Mutually exclusive with `--show-history` (if both are passed, `--show-history` wins and `--ticker` is silently ignored). |
| `--refresh-universe` / `--no-refresh-universe` | (TTL 7d) | Force-refresh / use cache regardless of age. |
| `--show-history` | — | Print history summary, no new scan. |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--prune-non-trading-days` | — | One-shot cleanup: drop history rows whose ET-date `run_date` is not an NYSE trading day. |
| `--no-save` | — | Don't append this run to history (one-off exploration). |
| `--save-stale` | — | Override the non-trading-day guard. By default the script skips `append_history` on weekends / NYSE holidays so streak counts don't inflate from duplicate-data days. Pre-market runs on a real trading day are still saved. |
| `--allow-same-day` | — | **[advanced/debug]** Append even if a row exists for today's ET date. Default overwrites today's snapshot — the right behavior for normal use since intra-day re-runs should refresh, not duplicate. Only enable for specific debugging or forced multi-snapshot workflows. |
| `--format` | markdown | `markdown` or `json`. |
| `--regime-gate` | warn | `off` skips SPY trend + breadth calculation. `warn` shows the regime banner + RISK-OFF caveat but still prints top-N. `strict` suppresses top-N when RISK-OFF (history still saved). RISK-ON requires SPY > 200DMA *and* 200DMA slope over the last 20 trading days above a small `-0.05%` dead band. Base breakouts have higher failure rates in RISK-OFF markets — most bases break *down*, not up. |
| `--atr-stop-mult` | 2.5 | ATR-based stop multiplier. Computes 14-day ATR per pick and adds a `Stop@trigger` column showing `pivot - mult × ATR` (the stop level you'd set if entering at the pivot breakout, which is the canonical entry for these setups). Typical values: 2.0 tight, 2.5 standard, 3.0 loose. Pass `0` to disable the column. (Unlike momentum-scan there's no TrailStop — trailing is for already-running positions; base setups haven't even triggered yet.) |
| `--no-sectors` | — | Skip sector tagging. Default fetches sector/industry from yfinance for top-N picks (cached, 30-day TTL) and shows a Sector column + breakdown line. |
| `--vol-collapse-ratio` | 0.2 | Acquisition-target / lock-in filter. A locked stock (cash buyout pending shareholder vote) looks **identical to a perfect base** — tight width, vol dryup, BB squeeze, RS rating high (post-gap), Trend Template passes (MAs all aligned below the gapped-up price). Without this filter, base-breakout-scan would flag every announced-but-not-closed merger as a top breakout candidate. Verified empirically: MASI on 2026-05-14 scored 88 (would be #1) but is correctly excluded by the default ratio. The check: annualized realized vol of the **first half** vs. **second half** of a fixed 3-month lookback. A locked stock has `v2/v1 ≈ 0.02`; a real base has both halves similar. **Raise** to `0.3` (hard cap `1.0`) to catch more lock-ins (more false positives on calmly-drifting earnings-pop names); **lower** to `0.15` to require a more dramatic collapse. Pass `0` or negative to disable. Excluded names print in a dedicated section above the Top-N table with their pre/post vol ratio; excluded entries carry `rank: null`, `pre_filter_rank`, `score_rank`, `vol_first_pct`, `vol_second_pct`, `vol_ratio`. The filter has the same "gap-in-second-half" failure mode as in momentum-scan — see Known limitations. |
| `--persistent-min-streak` | 4 | Streak threshold used by the **Maturing bases** section. Default 4 ≈ "survived one trading week" for daily users, or "4 weeks" for weekly runners. A maturing base is the highest-conviction signal — it means the geometry has held through actual market noise. |
| `--recent-breakout-days` | 10 | Lookback window for the **Recent breakouts** section. Names that triggered the pivot on volume in the last N days (but aren't currently in the watchlist) get listed in a separate section, split by whether they're still above the pivot (working) or fell back below (failed). Set to `0` to disable the section. |
| `--broke-out-pct` | 0.5 | Dropout-reason threshold: a dropped name with current price ≥ `pivot × (1 + this/100)` is labeled `broke_out` in the **Dropouts** section. Bump to 2.0 for stricter "confirmed breakout" labeling. |
| `--broke-down-pct` | -8.0 | Dropout-reason threshold: a dropped name with current price < `pivot × (1 + this/100)` is labeled `broke_down`. Tighten to -3 for earlier breakdown flagging. |
| `--verbose` | — | Print the funnel **plus** the trend-template-failure breakdown to stderr. By default only the one-line funnel prints; `--verbose` adds the top failure reasons (e.g. `fail_ma50_gt_ma150_gt_ma200=4`). Useful for understanding *why* a thin cohort came back. |

## Output shape

A funnel-summary line (stderr), regime banner, sector breakdown, an optional **Excluded by vol-collapse filter** section (printed between the Regime banner and the Top-N table when the filter rejects anything — see the `--vol-collapse-ratio` parameter), then an optional `🚀 Breakouts today` block, the main top-N table, and 2-4 discovery sections (dropouts with reasons, recent breakouts split by working/failed, new setups, maturing bases). Empty sections are skipped entirely. The Dropouts section gets a fifth reason category — **Vol-collapse filtered** — when a prior-run pick is excluded by the filter this run; it prints first in Dropouts (above broke_out/broke_down/deduped/faded) since it's the strongest "this is not a real signal" categorization. Sample below (illustrative — picks change daily; the exact tickers, scores, and counts will be different on your run):

```
Funnel: ~1000 → ~280 (RS≥70) → ~200 (TT) → ~130 (valid base) → ~50 (score≥40) → ~48 (after dedup, -1)

# Base-breakout scan — 2026-05-12 17:10 UTC

**Params**: base=6-40wks, max_width=25%, max_to_52w_high=15%, min_rs=70, min_score=40
**Universe**: ~1000 tickers · **Passed filter**: ~48 (vol-collapse: 0 excluded) · **Prior runs**: 1
**Regime**: SPY 733.3 vs 200DMA 671.6 (+9.2%) · 50DMA > 200DMA · 200DMA slope (20d): +1.48% · Breadth: 66% > 200DMA → **RISK-ON**
**Sectors**: Financial Services 9 · Energy 7 · Technology 5 · Basic Materials 4 · Communication Services 2 · Other 3

## Top 30

| # | Ticker | Sector | Score | RS | BaseWks | Width% | Smooth% | BB%ile | Vol↓ | RSslope%/wk | ToPivot% | Pivot | Sig | Stop@trigger | Streak | RankΔ | FirstSeen |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **NEM** | Materials | 64 | 82 | 6.0 | 11.0 | 37 | 8 | 0.67 | -2.22 | -2.6 | $120.90 | 🔥 | $108.48 (-10.3%) | 2 | +1 ↗ | 2026-05-12 |
| 2 | **HSBC** | Financ | 63 | 73 | 19.0 | 17.0 | 23 | 2 | 0.70 | +0.57 | -2.8 | $92.16 | 🔥 | $86.94 (-5.7%) | 2 | -1 ↘ | 2026-05-12 |
| 3 | **PBR-A** _(also PBR, score 52)_ | Energy | 58 | 85 | 7.0 | 10.4 | 63 | 18 | 0.69 | -1.38 | -6.3 | $19.90 | ⏳ | $18.69 (-6.1%) | 2 | +2 ↗ | 2026-05-12 |
| 4 | 🔒 **TD** | Financ | 56 | 76 | 21.0 | 16.7 | 27 | 26 | 0.71 | +0.55 | -1.5 | $108.60 | 📊 | $104.68 (-3.6%) | 2 | -1 ↘ | 2026-05-12 |
...

## Dropouts since last run (3)
**Broke out** (1):
- **BHP** (was #16, pivot $84.33) (+2.9% above pivot)
**Deduped** (1, same-issuer rule):
- **PBR** (was #11; kept PBR-A instead)
**Faded out** (1):
- **AMAT** (was #14)

## Recent breakouts (last 10 trading days, 11 working / 2 failed)
**Working** (still above pivot):
- **INTC** — broke $84.99 pivot on 2026-04-29 (10d ago, 1.8× vol). Now: $116.52 (+37.1% above pivot ↗)
- **AMD** — broke $360.54 pivot on 2026-05-06 (5d ago, 2.1× vol). Now: $433.07 (+20.1% above pivot ↗)
- **PWR** — broke $637.28 pivot on 2026-04-30 (9d ago, 2.7× vol). Now: $752.77 (+18.1% above pivot ↗)
...
**Failed** (back below pivot, top 2 worst):
- **DELL** — broke $238.80 on 2026-05-08 (3d ago), now $229.03 (-4.1% below pivot ↘)
- **NOKBF** — broke $13.54 on 2026-05-11 (2d ago), now $13.00 (-4.0% below pivot ↘)

## New setups (1)
- **VLO** at #12 (score 43, base 8wks, 📊)
```

(Things to notice in the funnel: `→ 15 (score≥40) → 14 (after dedup, -1)` shows the same-issuer dedup removed 1 candidate, so the watchlist is 14 not 15 — keeps the math transparent. PBR-A's row shows `_(also PBR, score 52)_` because PBR is its same-issuer pair that also passed but scored lower. The Dropouts section labels PBR as **Deduped** rather than **Faded** so the user doesn't misread a structural dedup as a real price-action signal.)

(The 🚀 *Breakouts today* section is shown above the main table when any name fires a same-day breakout with volume confirmation. **Most days the section is empty** — a fresh breakout-on-volume across 250 large caps on any single trading day is genuinely uncommon. When empty, the section header is skipped entirely. The **Recent breakouts** section below the table catches the broader population of breakouts in the last N trading days; that one is usually populated.)

(The **Maturing bases** section only appears once at least one ticker has a streak ≥ `--persistent-min-streak` (default 4). On the first few runs of a fresh history it's correctly omitted.)

Column meanings:

- **Score** — composite 0-100 Base Score. Components (max points): Tightness 25 (lower width%=better, scaled 5%→25pts down to 25%→0), BB squeeze 20 (lower BB%ile=better, scaled 0pctile→20pts down to 40pctile→0), Vol dry-up 15 (0.55→15pts, 1.10→0), RS slope 20 (+2.5%/wk→20pts, -0.5%/wk→0), Pivot proximity 15 (bell curve, ideal -2% from pivot), Smoothness 10 (90%→10pts, 50%→0), Three-weeks-tight 5 bonus. Realistic top picks land 75-95; a 50-point pick is a solid setup; 70+ is high-conviction. Use as a *priority filter*, not a deterministic ranking — two names within 5 points are effectively tied.
- **RS** — O'Neil-style universe-relative RS Rating (1-99). Weighted average of trailing 3/6/9/12-month returns, percentile-ranked across the universe. ≥ 70 is the Minervini gate; ≥ 80 is the top quintile.
- **BaseWks** — length of the current consolidation in trading weeks. Longer bases (15+) tend to fuel bigger moves when they break, at the cost of more time waiting. The algorithm picks the trailing window that maximizes `days / max(width, 1)` — so a 6-week base at 5% width can beat a 30-week base at 20% width.
- **Width%** — `(base_high - base_low) / base_high × 100`. Tighter is better. Sub-15% is high quality; 20-25% is acceptable; capped by `--max-base-width`.
- **Smooth%** — % of base bars within ±2% of the base mean. Discriminates real horizontal consolidations (high Smooth%) from V-shapes or jagged action that happens to fit the width envelope (low Smooth%). 50%+ is meaningfully horizontal; 70%+ is textbook smooth.
- **BB%ile** — percentile rank of current Bollinger Band(20) width within the last 126 trading days (~6 months). 0 = tightest in 6 months (max squeeze), 100 = widest. Below 25 is meaningful compression.
- **Vol↓** — volume dry-up ratio: `(last 20d avg volume) / (prior 60d avg volume)`. Below 1.0 = drying up (sellers exhausted, classic accumulation tell); below 0.75 = deep dry-up.
- **RSslope%/wk** — OLS slope of `(close / SPY)` regressed over the base period, expressed as % per trading week. Positive = stock outperforming SPY *while ranging* (the single most important pre-breakout signal). Negative = stock losing relative strength even though price hasn't dropped much — base may be the leading edge of a downturn.
- **ToPivot%** — distance from current price to the pivot (the price to break above). 0 = at pivot; -3 = need a 3% move up to trigger. Negative is in-range; positive means we're already above (suspect — check the Signal column for breakout confirmation).
- **Pivot** — the breakout trigger price. = high of the base window. Crossing this with volume confirmation is the buy signal.
- **Sig** — entry-timing classifier:
  - **🚀 BREAKOUT** — today's close ≥ pivot AND today's volume ≥ 1.5× 20-day avg. The trigger fired today; entry decision is "now or wait for pullback".
  - **🔥 IMMINENT** — within 3% of pivot AND BB%ile < 25 AND vol_dryup_ratio < 0.95. The setup is fully loaded but hasn't triggered. This is the highest-information state for active traders: set a price alert at the pivot.
  - **⏳ COILED** — within 10% of pivot AND BB%ile < 30. Squeeze on but a few percent of work needed before the trigger.
  - **📊 SETUP** — valid base, but not yet near pivot or not yet showing squeeze. Watchlist candidate; check back in 1-2 weeks.
- **Stop@trigger** — pivot-anchored ATR stop = `pivot - 2.5 × ATR(14)`. The % is the distance from the pivot, **not** from current price. This is the stop level you'd set if you enter via a buy-stop at the pivot (the canonical entry for these setups). The JSON output also includes `stop_now` / `stop_now_pct` (current-price anchored) for users who want to size from spot today rather than wait for the breakout. The two numbers diverge by `to_pivot_pct` — for 🔥 names (close to pivot) they're similar; for ⏳ names farther below pivot they differ meaningfully.
- **Streak** — consecutive prior runs this ticker has been in the top-N (1 = first appearance). Higher = base has held through multiple periods of noise; the setup is durable.
- **RankΔ** — change in rank vs the latest prior appearance (positive ↗ = rising, negative ↘ = slipping, 🆕 = no prior).
- **FirstSeen** — earliest date this base appeared in history. Combined with Streak gives the base's age in the watchlist.
- **🔒 prefix on ticker** — "three weeks tight" signal: last 3 weekly closes within 1.5% of each other. Minervini's textbook indicator that supply has been fully absorbed — institutional selling has stopped.

Discovery sections (dropouts with reasons / recent breakouts / new setups / maturing bases) are computed against the most recent prior run + the last `--recent-breakout-days` of price data. Sections with zero entries are skipped entirely so the output stays clean on thin days. The **Maturing bases** section (streak ≥ `--persistent-min-streak`, default 4) is correctly omitted on the first few runs of a fresh history — it only fires once a ticker has actually persisted through multiple scan-days.

### Stop@trigger — worked example

For HSBC in the sample table: pivot $92.16, 14-day ATR ≈ $2.08 (computed from OHLCV). With the default `--atr-stop-mult 2.5`:

```
stop_trigger = pivot - 2.5 × ATR = 92.16 - 2.5 × 2.08 = $86.94
stop_trigger_pct = (86.94 / 92.16 - 1) × 100 = -5.7%
```

So if you enter via a buy-stop at $92.16 (the canonical entry: a market order triggered when price crosses the pivot), your initial stop sits at $86.94 — a 5.7% maximum loss per share if the breakout fails and the stop hits. For dollar sizing: `shares = risk_per_trade ÷ (pivot − stop_trigger)`. If you're willing to risk $500 per trade, you'd buy ~96 shares ($500 / $5.22).

The JSON output also includes `stop_now` and `stop_now_pct` (anchored to today's close instead of the pivot) for users who want to size from spot today. The two diverge by `to_pivot_pct` — for 🔥 names close to pivot they're nearly identical; for ⏳ names a few % below pivot they diverge meaningfully.

### Sig column markers

A trailing `*` after a Sig glyph (e.g. `🚀*` or `📊*`) means the base ended within the last 3 days and today is a fresh cross above the prior range (`anchor_mode=3`). Same glyph semantics — `*` just tells you "this base's geometry resolved very recently; the setup is hot off the press, not still building". Most days no rows have the asterisk.

### Single-ticker check (`--ticker AAPL`)

Bypasses the universe scan and prints a 4-stage diagnostic + ATR stop levels for one ticker. Works for any US ticker yfinance can fetch (large-cap, small-cap, ADR, foreign-listed). Runtime ~2-5 seconds (vs ~30-60s for a full scan). Honors `--format json` for structured output. Also runs the vol-collapse check (when `--vol-collapse-ratio > 0`) and surfaces a prominent `⚠️ VOL-COLLAPSE WARNING` block at the top if the ticker shows the acquisition-lock signature — appears even when the ticker fails earlier stages (TT-fail, no base), so a user asking "is MASI a good base?" can't accidentally miss the warning. Sample (illustrative; values will differ on your run):

```
# Single-ticker check: HSBC
_(using fast RS-vs-SPY proxy in place of universe-relative RS Rating)_

**RS proxy (vs SPY)**: ~76/99 (approximate)

## Stage 1: Minervini Trend Template
✅ **PASS** — all 7 criteria met
- Close: $89.49
- 50DMA: $85.99, 150DMA: $78.77, 200DMA: $75.08
- 200DMA slope (21d): +4.48%
- 52w high: $92.16 (-2.9% from current)
- ...

## Stage 2: Base detection
✅ **Valid base** detected
- Length: 19.0 weeks (95 trading days)
- Width: 17.0% ($76.52 – $92.16)
- Smoothness: 23% of bars within ±2% of mean
- Pivot price: $92.16
- ...

## Stage 3: Quality metrics
- BB(20) squeeze percentile (6mo): 2
- RS slope vs SPY during base: +0.57%/wk
- Three-weeks-tight (🔒): no

## Stage 4: Composite Base Score & Signal
- **Base Score: 62/100**
- **Signal: 🔥**

## Stage 5: Risk levels (ATR-based stops, 2.5× ATR)
- 14-day ATR: $2.08 (2.3% of price)
- **Stop @ trigger** (if entering at pivot): $86.94 (-5.7% from pivot)
- Stop @ now (if entering at current price): $84.29 (-5.8% from spot)
- For $500 max risk per trade: ~95 shares (risk-per-share = pivot − stop = $5.22)

→ **HSBC would appear in the standard watchlist.**
```

When the vol-collapse filter triggers, the warning prints at the top of the report — before any stage analysis — so a user can't accidentally proceed to "valid base" framing without seeing the alarm:

```
# Single-ticker check: MASI _(Healthcare / Medical Devices)_
_(using fast RS-vs-SPY proxy in place of universe-relative RS Rating; run the full scan if you need the exact percentile)_

> ⚠️ **VOL-COLLAPSE WARNING**: 2nd-half annualized vol = 1.9% (1st-half was 98.0%); ratio = 0.019, below the 0.20 threshold.
>
> This is the canonical signature of an acquisition target locked at a cash offer price — the chart will look like a perfect base, but the stock won't actually move.
>
> Verify via `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun for a pending merger. Treat any 'valid base' framing below with skepticism until verified.

**RS proxy (vs SPY)**: ~65/99 (approximate)

## Stage 1: Minervini Trend Template
❌ **FAIL** — reason: `fail_rs_rating`
...
```

The warning appears even when the ticker fails Stage 1 (TT-fail, no base) because the vol-collapse check runs early in the pipeline, before any short-circuit. The numbers in the warning are stated unambiguously: vol values are annualized percentages (1.9% / 98.0%); ratio and threshold are decimals (0.019 / 0.20).

The RS Rating in `--ticker` mode is an approximation (RS-vs-SPY proxy mapping weighted excess return through a ±30% band onto a 1-99 scale) rather than the exact universe-relative percentile — this avoids fetching the full 250-ticker universe and is the reason for the ~15× speedup. The proxy is within ~10-15 percentile points of the true rating in practice — close enough to be a TT gate, not close enough to publish as a ranked figure. For the exact RS Rating, run the full scan (without `--ticker`) and read the RS column.

When base detection fails but the ticker broke out in the last 10 trading days, the report includes a **fallback section** with the breakout date, follow-through %, and concrete action guidance ("set a price alert at $X — if price retests from above, that's the textbook O'Neil throwback entry; if it closes below for 2 sessions, the breakout failed").

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly.

1. **Lead with the 🚀 Breakouts section if non-empty.** This is the most actionable, time-sensitive subset — a base broke today *with* volume confirmation. The decision space is narrow: enter now near pivot, or wait for the inevitable retest of the breakout level (usually within 5 sessions). Quantify the per-trade risk using the Stop column; reject the entry if the per-trade loss exceeds the user's risk budget. Always remind: most breakouts fail (40-60% by various studies). Stop discipline is non-negotiable.

2. **🔥 IMMINENT names beat 🚀 BREAKOUTs in expected value for most users.** Counterintuitively: 🚀 means the breakout already happened, so you're chasing 1-2% above the pivot with all the risk and less of the move. 🔥 means the trigger hasn't fired yet — you can set a buy-stop *at* the pivot, eliminating the "chase" cost. Lead with 🔥 names if any exist. Especially flag them when RS slope is also strongly positive (RSslope > +1%/wk during the base = the stock is being accumulated under the surface even as price ranges).

3. **Read the Regime banner first.** Base breakouts have much higher failure rates in RISK-OFF markets — most bases break *down*, not up, when the broader tape is weak. In RISK-OFF: do NOT recommend specific buys; reframe the list as "names showing structural strength that we want to revisit when the regime turns". The skill auto-suppresses the top-N when `--regime-gate strict` and RISK-OFF, which is the safer default for cautious users.

4. **Watch sector clustering.** A list dominated by one sector (e.g., 6 of 19 Energy, 4 Financials) often means the *sector* is basing, not just individual names. The trade is then the sector (sector ETF, or a basket of the top 3 from the cluster) rather than picking the highest-Score single name. Isolated newcomers in unrelated sectors are higher single-name conviction but also higher single-name risk.

5. **Maturing bases (streak ≥ 4) are the real signals.** Long persistence means the base has held through actual market noise — these are the highest-conviction setups by definition. A stock appearing for the first time has a thin signal (could be noise; the consolidation might break either direction). A stock with streak 6-10 has earned the watchlist slot. Combine maturing-base names with 🔥 IMMINENT status: when those overlap, that's the textbook setup.

6. **Read Score *components* for hidden information.** A 60-point Score is a *composite* — two names with identical scores can have very different profiles. Some patterns to recognize:
   - **"All geometry, no RS"**: high tightness + BB squeeze + smoothness + pivot proximity but flat or negative RS slope. The setup *looks* perfect on the chart but the stock is quietly losing ground to SPY. More likely to break the *wrong way*.
   - **"All RS, no geometry"**: strong positive RS slope but wide width / high BB pctile / low smoothness. Stock is being accumulated but the consolidation hasn't tightened yet — typically 2-4 weeks early.
   - **"Balanced quality"**: middling on every component, no standout strength. These score 50-65 reliably across many runs and are the consistent "watchlist filler" — neither exciting nor disqualifying.
   - **"Loaded"**: high on every component including positive RS slope. Rare; usually scores 75+. These are the textbook setups.

   When two picks tie on Score, prefer the one with higher RS slope — it's the most leading of the components and the one where deception (a stock geometrically tightening on the way to a downside break) is least common. Cap on the score: even all-perfect + the 3-week-tight bonus sums to 110 → capped at 100, so any **90+ is exceptional**, **75+ is high-conviction**, **50-65 is solid filler**.

7. **Dropouts reveal more than the picks.** A name leaving the list with reason="broke_out" is a win — the setup did what it was supposed to do. "broke_down" is a loss — the base failed and the stop (would have) fired. "no_longer_qualifies"/"faded" is neutral noise — the score drifted below threshold for benign reasons. Surface the *broke_out* names in particular: those validate the screening logic, and persistent "broke_out" rates above ~40% on prior watchlist picks would mean the screen is working as intended.

8. **The Vol↓ ratio is more leading than BB%ile.** Vol dry-up usually precedes the price compression by 2-4 weeks: institutions are done selling first, *then* the BB tightens. A name with vol ratio = 0.60 but BB%ile = 50 may be early in the setup; same name 2 weeks later with vol 0.65 and BB%ile = 15 is the textbook "fully loaded" state. Look at multi-run trajectories using `--show-history` to spot this evolution.

9. **Read the Recent breakouts section as the bridge from "what's setting up" to "what just broke".** The main watchlist surfaces *pre-breakout* candidates; Recent breakouts surfaces names that crossed their pivot on volume in the last 5-10 trading days — too late to enter at the pivot but still in the follow-through window. Use the split:
   - **Working** (positive % vs pivot): the breakout is *confirmed* — these names crossed and held. The actionable read is whether a *pullback to the breakout level* offers a second entry (textbook O'Neil "throwback" entry). When a working breakout is +1-5% above pivot, that retest is often imminent; when it's +15%+ above pivot, the entry window is closed and chasing is high-risk.
   - **Failed** (negative % vs pivot): the breakout *triggered then reversed* — the base failed. These are also informative as a *tape signal*: when the failed list is unusually long (more failed than working), it means the broader market is digesting breakouts poorly — a yellow card on initiating new entries in the main watchlist until the ratio flips.
   - When the section is missing (or has zero entries), no qualifying breakouts happened in the lookback window — quiet stretch, focus shifts to the main watchlist alone.

10. **Never recommend specific buys.** Frame results as "names worth investigating" with specific risk parameters (pivot for entry, ATR stop for risk). Always flag: base-and-breakout strategies depend on stop discipline more than entry selection. The literature win rate is 40-50%; without the stop loss, that's a money-losing strategy. *With* a 7-8% (or ATR-based) stop and asymmetric payoffs, it's profitable. The skill cannot enforce the stop; only the user can.

## State files

- `state/history.csv` — one snapshot per US market day (America/New_York) × every top-N ticker. Columns: `run_id, run_date, ticker, rank, base_score, base_weeks, width_pct, bb_pctile, vol_dryup_ratio, rs_slope_pct_per_wk, to_pivot_pct, pivot_price, signal`. (The `anchor_mode` field is computed but not persisted to history — it's only relevant for the current scan's Sig column markers.) Re-running the same ET day overwrites that day's rows. Writes are atomic (.tmp + rename) so a crash mid-write can't truncate. **The skill gets more useful with each subsequent run** — first run is just the picks; later runs add streak, RankΔ, breakout/breakdown tracking, and base-maturation signal.
- `state/universe.txt` — cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json` — per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker. Fetched lazily on top-N picks only.

Storage growth: at default `--top-n 30`, each run adds ~30 rows × ~150 bytes ≈ 4.5 KB. A year of daily runs ≈ 1.6 MB; weekly ≈ 230 KB. Negligible for years of typical use.

## Cadence

Cadence-agnostic by design. One snapshot per US market day (America/New_York), so streak counts **consecutive prior scan-days** — running multiple times on the same ET day refreshes that day's entry. Runs on weekends or NYSE holidays auto-skip from history (results still print, just nothing is appended) so streak doesn't inflate from duplicate-data days. Pre-market runs on a real trading day **do** save.

Recommended cadence:
- **Daily**: streak unit is days. Finest granularity, sees breakouts the day they happen. Best for active traders.
- **Weekly (Friday close)**: streak unit is weeks. Recommended sweet spot — captures base maturation 4× faster than monthly while smoothing intra-week noise. Best for swing traders.
- **Monthly**: streak unit is months. Smoothest signal, only the longest / highest-conviction bases survive. Best for position traders.

For automatic recurring runs, use a local scheduler (macOS `launchd`, or `cron`) pointing at `scripts/scan.py`. The `schedule` skill runs *remote* agents in Anthropic-managed sandboxes that can't see this local `state/` directory.

## Known limitations

- **Base-detection is a screener heuristic, not a chart annotator.** The trailing-window approach (find the longest window where price range ≤ max_base_width) catches the same population as Minervini's VCP / O'Neil's cup-and-handle without trying to identify those exact patterns. Real chartists can find textbook bases this misses (e.g., a stair-stepping handle inside a larger cup), and vice versa — a window that meets the numerical criteria may be a *failing* base by eye (e.g., descending highs, no proper handle). Use the surfaced names as candidates to chart, not as finished analysis.
- **Survivorship bias** — the universe is current US large caps; delisted names are absent. This is fine for forward-looking screening but means any backtested win rate from this exact universe would be optimistic vs. a true point-in-time universe.
- **Yahoo data quirks** — rare missing bars, late dividend adjustments. If a single name's metrics look wrong (e.g., width% way off the chart), sanity-check via the `yfinance` skill or the underlying ticker's chart on any free site.
- **52w high reference is rolling, not the absolute high.** A stock that made an all-time high 14 months ago doesn't show that high — we only see the last 252 trading days. So a "20% from 52w high" might be a much smaller correction from a more-recent acute high vs. all-time. Usually doesn't matter for the screen's purpose (we're looking at *recent* setups), but worth knowing when interpreting deep-base names.
- **RS Rating is universe-relative, not market-relative.** O'Neil's published RS Rating uses a much broader universe (~6000 stocks); ours uses the 250 large caps in scope. So our RS = 70 corresponds to "top 30% within S&P large caps", not "top 30% across all listed stocks". For large-cap-only investing this is the right denominator; for "is this stock outperforming the market as a whole", check RS slope (which uses SPY).
- **Base detection ignores patterns within the base.** A stock ranging 0-25% width passes width check whether the range is smoothly horizontal or jagged with V-spikes. The latter often looks "right" by the screen but fails more often in practice. Cross-checking with the BB%ile (low BB pctile = smooth) and width% (low width = smooth) filters out most of the V-spike cases, but not all.
- **`--regime-gate` reduces bear-market downside but can't catch fast regime flips.** A 200DMA slope that turns negative requires ~20 trading days to register; a 1-week crash (Aug 2024, March 2020, Feb 2018) blows through the gate before the slope flips. Treat the regime banner as helpful framing, not a defensive moat.
- **Volume dry-up ratio uses 20d/60d SMA, which lags.** Fast regime changes (M&A talk, earnings surprises) spike volume in days; our ratio takes weeks to fully reflect. Treat the metric as "is supply slowly drying up" rather than "what's volume doing today".
- **The "broke_out" / "broke_down" labels in dropouts use a simple 0.5% / 8% threshold from the pivot.** A name that broke out by 0.3% and reversed isn't a confirmed breakout — but we'd label it "no_longer_qualifies" only if it's below the threshold today. Treat the dropout reasons as a heuristic, not as an audit trail.
- **Vol-collapse filter has a "gap-in-second-half" blind spot.** `--vol-collapse-ratio` compares the realized vol of two halves of a fixed 3-month lookback. It catches lock-ins when the announcement gap falls in the **first** half (typical case: gap happened 1-3 months ago). When the gap day lands in the **second** half (announcement was within the last ~6 weeks), the gap inflates `v2` above `v1`, ratio > 1, name passes through. Failure mode is **late detection, not silent miss**: by a few weeks later the gap drifts into the first half and the filter catches it. The 5%-annualized minimum first-half vol guard prevents already-low-vol names from being flagged on noise. False-positive direction: names that gapped on an earnings beat and then drifted calmly higher with very orderly daily ranges can compress 2nd-half vol enough to trip the filter; lower `--vol-collapse-ratio` to 0.15 to reduce these. The filter doesn't catch tender-offer situations that don't involve a single-day gap (slow accumulation of shares at small premiums) — those produce a normal-looking price chart with no anomaly to detect. **Identification heuristic for a leak**: any base with `MaxDD%` improbably small (< -1%) *and* `width%` < 2% is suspect. Cross-check with `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun (proxy filings relating to a merger); `8-K` / `DEFA14A` is suggestive but not conclusive.
- **Single-ticker mode (`--ticker MASI`) has the same gap-in-second-half blind spot, with worse practical impact.** Asking the scanner "is MASI a good base?" on the day a merger is announced will pass through without the warning — the gap is at the end of the window, inflates `v2`, ratio > 1, no trigger. The universe scan is forgiving (many tickers, signal redundancy) but single-ticker mode answers a high-stakes "should I trade this name today?" question. By the time the gap drifts into the first half (~2-3 weeks later) the warning fires, but day-1 false positive is the worst failure mode. There's no clean fix using only the split-half geometry — a complementary "did the most recent N days have a single move > 20%?" check would catch announcement-day gaps, but adds a separate detection pattern. For now, when running `--ticker` on any recently-gapping name (visible from a quick `history` chart check), manually verify via `sec_filings --type PREM14A,DEFM14A` before treating it as a tradeable base.
- **No fundamental check.** The screen is 100% price/volume; it doesn't know if the company has decelerating earnings, regulatory overhang, or executive turnover during the base. The literature's 40-50% win rate is *without* a fundamental filter — adding even a simple "EPS growth not declining" check would lift it materially. The user should at minimum eyeball the latest earnings before acting on any pick.
- **Pivot is the in-base high, which may be conservative.** Some chart traders prefer the high of the *handle* (a sub-pattern inside a cup-with-handle base) as the actual pivot, which is typically a few percent below the full cup high. Our pivot uses the full base high — slightly more conservative trigger, slightly worse entries, slightly lower false-breakout rate.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)** — this only matters if Yahoo's response stops including the `total` field (schema drift), in which case the script falls back to per-page heuristics (short page / zero new tickers) to detect end-of-results. The 20-page cap is the absolute backstop; if it ever triggers you'll see `refresh_universe: hit SCREENER_MAX_PAGES=20 backstop` on stderr and the universe will be capped at ~5000 — well above any realistic large-cap match count, so triggering it is a strong signal something is wrong upstream.
- **Universe size affects historical rank comparability** — if you raise `--universe-count` (or the underlying universe grows from market-cap drift) between runs, ranks recorded in `state/history.csv` from before the change aren't directly comparable to ranks after. A larger universe means more names can pass the funnel, which can demote previously-high-ranked names not because their setups weakened but because new entrants were added to the pool. `Streak` and `FirstSeen` survive (a ticker is still "in the top N" or not), but `RankΔ` across a universe-size change should be read with that caveat. If you want clean before/after comparison, run `--clear-history` after changing universe size.
