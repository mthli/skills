---
name: base-breakout-scan
description: "Scan US large-cap equities for valid pre-breakout bases: tight consolidations after a prior advance, with squeeze + volume dry-up + rising relative strength. Use when the user wants to find stocks about to break out, identify coiled / VCP / cup-and-handle setups, or find Minervini Trend Template + base setups. Triggers on 'find breakout candidates', 'stocks setting up', 'compressed bases', 'tight ranges', 'ready to pop', 'near 52-week highs', 'VCP setups', 'coiled'. The natural complement to momentum-scan: that one finds what's already running; this finds what's about to. Do NOT use for single-ticker chart analysis, ETF screening, value/contrarian picks, or generic explanations of base-and-breakout investing."
---

# base-breakout-scan

Find US equities in **valid pre-breakout bases**: tight consolidations near 52-week highs after a prior advance, with quality metrics (Bollinger Band squeeze, volume dry-up, rising relative strength) showing the setup is "loaded". Surface a daily watchlist of which names are 🚀 breaking out today, 🔥 imminent, ⏳ coiled, or 📊 still forming, and track which setups persist across runs.

The natural sibling of `momentum-scan`: that one finds **what's already running** (high trailing return); this one finds **what's about to run** (the build-up before the move). They filter for opposite price patterns by design: momentum-scan's `--min-return-pct 30` floor would reject every name this scan surfaces, since basers are ranging, not climbing.

**The big idea**: a stock that's already up 50% in 3 months tends to be too extended to chase. A stock that ran up 50%, then spent 8-20 weeks in a tight consolidation while sellers exhausted and RS improved under the surface, is a high-quality setup *if* the trend holds; that's what this finds. The literature backing is the William O'Neil / Mark Minervini / Stan Weinstein school: their published win rates on this exact pattern are 40-50%, enough to be profitable when paired with a 7-8% stop loss (asymmetric payoff).

By default each run surfaces four entry layers on every pick: (1) a **composite Base Score** (0-100) summarizing setup quality, (2) a **Signal** classification (🚀 breakout today / 🔥 imminent / ⏳ coiled / 📊 forming), (3) the **pivot price** to break above, and (4) an **ATR-based stop loss** (2.5× ATR by default). Persistence tracking via `state/history.csv` records each ET-date snapshot once so each subsequent run can show streak, rank changes, breakouts (left because they triggered), and breakdowns (left because they failed).

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2`, `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run: bases 6-40 weeks, score ≥ 40
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

# High-vol regime: relax the smoothness band so 2-3% noise doesn't kill scores
... python <SKILL_DIR>/scripts/scan.py --smoothness-band-pct 3.0

# Disable the vol-collapse acquisition-target filter (keep buyouts in the table; see parameter table)
... python <SKILL_DIR>/scripts/scan.py --vol-collapse-ratio 0

# Outcome backtest: replay history.csv as canonical pivot buy-stop trades,
# stratified by setup attributes (see "Backtested outcomes" section)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/backtest_outcomes.py            # touch entry (buy-stop)
... python <SKILL_DIR>/scripts/backtest_outcomes.py --entry confirmed  # close>pivot on 1.5x vol

# Rebuild state/outcomes.csv from scratch (normal scans keep it current on
# their own; this is the seed / repair path, and the only one that works
# when the scan can't run). Idempotent.
... python <SKILL_DIR>/scripts/backtest_outcomes.py --write-ledger

# Render history.csv + outcomes.csv into a self-contained HTML dashboard at
# state/history.html (approach-to-pivot trajectories, watchlist tension,
# ⭐ pocket vs rest, per-sector result panel, maturity grid, roster).
# Stdlib-only, no network, no uv:
python <SKILL_DIR>/scripts/render_history_html.py   # --days 60 --out <path>
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--min-base-weeks` | 6 | Minimum base length in weeks. Lower = catch fresh consolidations earlier with more noise. The literature floor (O'Neil "minimum 5-week flat base") is around 5-6 weeks. |
| `--max-base-weeks` | 40 | Maximum base length. Very long bases (year+) often resolve down; momentum has bled out. |
| `--max-base-width` | 25 | Max width % of base (high-low) / high. Bases tighter than 15% are higher quality but rarer. Loosen if too few picks; tighten for higher conviction. |
| `--max-to-52w-high` | 15 | Max % distance from 52-week high. Closer to high = nearer to the resistance breakout. Beyond 15% means stock is in correction territory, not basing. |
| `--min-rs-rating` | 70 | Minervini's RS Rating threshold (top 30% of universe by O'Neil-style weighted 3/6/9/12-month return). Bump to 80 for top-quintile only. |
| `--min-base-score` | 40 | Composite Base Score floor (0-100) for display. See **Output shape** below for component breakdown. Bump to 60 for high-conviction only; lower to 25 in thin-cohort markets. |
| `--smoothness-band-pct` | 2.0 | Smoothness band: a base bar counts as "smooth" if it's within ±this % of the base mean (feeds the Smooth% column and the Score's smoothness component). Calibrated for liquid US large-caps. Lower to `1.0` to reward only very tight bases; raise to `3.0` in a high-vol regime so 2-3% daily noise doesn't kill scores of otherwise-valid bases. |
| `--top-n` | 30 | How many candidates to display + log to history. |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower to include small-cap setups (which are richer in this pattern but noisier). |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match the screener reports (currently ~1000 US large caps at default mcap/volume floors). The screener returns at most **250 rows per request** (Yahoo's hard cap; `yf.screen` raises `ValueError` above that), so the script paginates the universe in 250-row pages with `offset`; at default filters that's ~5 paginated requests, taking a few extra seconds, but only on cache refresh (every 7 days). Pass an explicit positive integer to cap the universe (e.g. `250` for a one-request refresh, `500` for a middle ground); argparse rejects 0 / negative values. If you raise `--universe-count` above the number of tickers already in `state/universe.txt`, the script force-refreshes the cache even within TTL; otherwise you'd keep getting the smaller cached pool with no warning. Older yfinance versions (without `offset` support) fall back to a single 250-row page. |
| `--ticker` | — | Single-ticker check mode (e.g. `--ticker AAPL`). Bypasses the universe scan; shows which funnel stage the ticker passes/fails (Trend Template → Base detection → Score → ATR stops) with all per-stage metrics. **Works for any US ticker yfinance can fetch** (large-cap, small-cap, ADR, even foreign-listed); the scan never consults the universe. ~2-5s vs ~30-60s for a full scan. Honors `--format json` for structured output. Writes no history. Mutually exclusive with `--show-history` (if you pass both, `--show-history` wins and `--ticker` is ignored). |
| `--refresh-universe` / `--no-refresh-universe` | (TTL 7d) | Force-refresh / use cache regardless of age. |
| `--show-history` | — | Print history summary, no new scan. |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--prune-non-trading-days` | — | One-shot cleanup: drop history rows whose ET-date `run_date` is not an NYSE trading day. |
| `--no-save` | — | Don't append this run to history (one-off exploration). Also skips the outcomes-ledger refresh, since both are state writes. |
| `--no-outcomes` | — | Skip the outcomes-ledger refresh. Every normal run resolves any episode whose trade has finished — reusing the bars already in memory, so no extra network — and upserts `state/outcomes.csv`, the file the HTML dashboard reads for realized results. Use this only to isolate the scan itself. |
| `--save-stale` | — | Override the non-trading-day guard. By default the script skips `append_history` on weekends / NYSE holidays so streak counts don't inflate from duplicate-data days. Pre-market runs on a real trading day still save. |
| `--allow-same-day` | — | **[advanced/debug]** Append even if a row exists for today's ET date. Default overwrites today's snapshot, the right behavior for normal use since intra-day re-runs should refresh, not duplicate. Enable only for debugging or forced multi-snapshot workflows. |
| `--format` | markdown | `markdown` or `json`. |
| `--regime-gate` | warn | `off` skips SPY trend + breadth calculation. `warn` shows the regime banner + RISK-OFF caveat but still prints top-N. `strict` suppresses top-N when RISK-OFF (history still saved). RISK-ON requires SPY > 200DMA *and* 200DMA slope over the last 20 trading days above a small `-0.05%` dead band. Base breakouts have higher failure rates in RISK-OFF markets: most bases break *down*, not up. |
| `--atr-stop-mult` | 2.5 | ATR-based stop multiplier. Computes 14-day ATR per pick and adds a `Stop@trigger` column showing `pivot - mult × ATR` (the stop level you'd set if entering at the pivot breakout, which is the canonical entry for these setups). Typical values: 2.0 tight, 2.5 standard, 3.0 loose. Pass `0` to disable the column. The underlying ATR and this multiplier are persisted per row to `state/history.csv` (raw, not as a derived stop level) so any multiplier can be replayed later; disabling the column also stops that recording. (Unlike momentum-scan there's no TrailStop: trailing is for already-running positions, and base setups haven't triggered yet.) |
| `--no-sectors` | — | Skip sector tagging. Default fetches sector/industry from yfinance for top-N picks (cached, 30-day TTL) and shows a Sector column + breakdown line. |
| `--vol-collapse-ratio` | 0.2 | Acquisition-target / lock-in filter. A locked stock (cash buyout pending shareholder vote) looks **identical to a perfect base** (tight width, vol dryup, BB squeeze, high RS, Trend Template passing), so without this filter every announced-but-not-closed merger would top the list. The check: annualized realized vol of the first vs second half of a fixed 3-month lookback; a locked stock has `v2/v1 ≈ 0.02`. **Raise** to `0.3` (hard cap `1.0`) to catch more lock-ins (more false positives); **lower** to `0.15` for a stricter collapse. Pass `0` or negative to disable. See `references/vol-collapse.md` for the MASI case, the excluded-entry JSON fields, and the gap-in-second-half blind spot. |
| `--persistent-min-streak` | 4 | Streak threshold used by the **Maturing bases** section. Default 4 ≈ "survived one trading week" for daily users, or "4 weeks" for weekly runners. A maturing base is the highest-conviction signal: the geometry has held through real market noise. |
| `--recent-breakout-days` | 10 | Lookback window for the **Recent breakouts** section. Names that triggered the pivot on volume in the last N days (but aren't currently in the watchlist) get listed in a separate section, split by whether they're still above the pivot (working) or fell back below (failed). Set to `0` to disable the section. |
| `--broke-out-pct` | 0.5 | Dropout-reason threshold: a dropped name with current price ≥ `pivot × (1 + this/100)` gets the `broke_out` label in the **Dropouts** section. Bump to 2.0 for stricter "confirmed breakout" labeling. |
| `--broke-down-pct` | -8.0 | Dropout-reason threshold: a dropped name with current price < `pivot × (1 + this/100)` gets the `broke_down` label. Tighten to -3 for earlier breakdown flagging. |
| `--verbose` | — | Full diagnostics, two effects. **Table**: restores the diagnostic columns (RS, Smooth%, BB%ile, Vol↓, RSslope%/wk, RankΔ, FirstSeen) that the slim default (2026-07-31 redesign) hides — they feed the Score rather than the entry decision, and JSON always carries every field. **Stderr**: adds the trend-template-failure breakdown to the funnel line (e.g. `fail_ma50_gt_ma150_gt_ma200=4`), useful for understanding *why* a thin cohort came back. |

## Output shape

A funnel-summary line (stderr), regime banner, sector breakdown, the **Sig** cohort strip (🚀/🔥/⏳/📊 counts across the top-N), an optional **Excluded by vol-collapse filter** section (printed between the Regime banner and the Top-N table when the filter rejects anything; see the `--vol-collapse-ratio` parameter), then an optional `🚀 Breakouts today` block, the **⭐️ Validated pocket** section (BaseWks ≥ 20; always printed, even when empty, because an empty pocket is itself the signal that today's list is all unvalidated candidates), the main top-N table, and 2-4 discovery sections (dropouts with reasons, recent breakouts split by working/failed, new setups, maturing bases). The script skips empty sections. The Dropouts section gets a fifth reason category, **Vol-collapse filtered**, when the filter excludes a prior-run pick this run; it prints first in Dropouts (above broke_out/broke_down/deduped/faded) since it's the strongest "this is not a real signal" categorization. Sample below (illustrative; picks change daily, and the exact tickers, scores, and counts will differ on your run):

```
Funnel: ~1000 → ~280 (RS≥70) → ~200 (TT) → ~130 (valid base) → ~50 (score≥40) → ~48 (after dedup, -1)

# Base-breakout scan — 2026-05-12 17:10 UTC

**Params**: base=6-40wks, max_width=25%, max_to_52w_high=15%, min_rs=70, min_score=40
**Universe**: ~1000 tickers · **Passed filter**: ~48 (vol-collapse: 0 excluded) · **Prior runs**: 1
**Regime**: SPY 733.3 vs 200DMA 671.6 (+9.2%) · 50DMA > 200DMA · 200DMA slope (20d): +1.48% · Breadth: 66% > 200DMA → **RISK-ON**
**Sectors**: Financial Services 9 · Energy 7 · Technology 5 · Basic Materials 4 · Communication Services 2 · Other 3
**Sig**: 🚀0 🔥6 ⏳14 📊10

## ⭐️ Validated pocket — BaseWks ≥ 20 (1)
_The one stratum the outcome backtest validated (+4.9%/trade, 75% win vs −0.8% baseline, in-sample). Score does not rank outcomes; base length does._
- **TD** (#4): base 21wks, width 16.7%, -1.5% to $108.60 pivot, 📊

## Top 30

| # | Ticker | Sector | BaseWks | Score | Width% | ToPivot% | Pivot | Sig | Stop@trigger | Streak |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **NEM** | Materials | 6.0 | 64 | 11.0 | -2.6 | $120.90 | 🔥 | $108.48 (-10.3%) | 2 |
| 2 | **HSBC** | Financ | 19.0 | 63 | 17.0 | -2.8 | $92.16 | 🔥 | $86.94 (-5.7%) | 2 |
| 3 | **PBR-A** _(also PBR, score 52)_ | Energy | 7.0 | 58 | 10.4 | -6.3 | $19.90 | ⏳ | $18.69 (-6.1%) | 2 |
| 4 | ⭐️ 🔒 **TD** | Financ | 21.0 | 56 | 16.7 | -1.5 | $108.60 | 📊 | $104.68 (-3.6%) | 2 |
...

_Diagnostic columns (RS, Smooth%, BB%ile, Vol↓, RSslope%/wk, RankΔ, FirstSeen): --verbose_

## Dropouts since last run (3)
**Broke out** (1):
- **BHP** (was #16, pivot $84.33) (+2.9% above pivot)
**Deduped** (1, same-issuer rule):
- **PBR** (was #11; kept PBR-A instead)
**Faded out** (1):
- **AMAT** (was #14)

## Recent breakouts (last 10 trading days, 11 working / 2 failed)
**Working** (still above pivot):
- **INTC**: broke $84.99 pivot on 2026-04-29 (10d ago, 1.8× vol). Now: $116.52 (+37.1% above pivot ↗)
- **AMD**: broke $360.54 pivot on 2026-05-06 (5d ago, 2.1× vol). Now: $433.07 (+20.1% above pivot ↗)
- **PWR**: broke $637.28 pivot on 2026-04-30 (9d ago, 2.7× vol). Now: $752.77 (+18.1% above pivot ↗)
...
**Failed** (back below pivot, top 2 worst):
- **DELL**: broke $238.80 on 2026-05-08 (3d ago), now $229.03 (-4.1% below pivot ↘)
- **NOKBF**: broke $13.54 on 2026-05-11 (2d ago), now $13.00 (-4.0% below pivot ↘)

## New setups (1)
- **VLO** at #12 (score 43, base 8wks, 📊)
```

The **Sig** strip under the banner is the cohort-level read: many 🔥/🚀 means the watchlist is loaded and time-sensitive; all 📊 means nothing is near a trigger and the list is "check back later". The table is the **slim** default (2026-07-31 redesign, decision columns only); `--verbose` restores the diagnostic columns, and JSON always carries every field.

(Things to notice in the funnel: `→ 15 (score≥40) → 14 (after dedup, -1)` shows the same-issuer dedup removed 1 candidate, so the watchlist is 14 not 15; that keeps the math transparent. PBR-A's row shows `_(also PBR, score 52)_` because PBR is its same-issuer pair that also passed but scored lower. The Dropouts section labels PBR as **Deduped** rather than **Faded** so the user doesn't misread a structural dedup as a real price-action signal.)

(The 🚀 *Breakouts today* section is shown above the main table when any name fires a same-day breakout with volume confirmation. **Most days the section is empty**: a fresh breakout-on-volume across 250 large caps on any single trading day is uncommon. When empty, the script skips the section header. The **Recent breakouts** section below the table catches the broader population of breakouts in the last N trading days; that one has entries most days.)

(The **Maturing bases** section only appears once at least one ticker has a streak ≥ `--persistent-min-streak` (default 4). On the first few runs of a fresh history it stays absent, as designed.)

Column meanings (columns marked *verbose* print only with `--verbose`; JSON always carries them):

- **Score**: composite 0-100 Base Score. Components (max points): Tightness 25 (lower width%=better, scaled 5%→25pts down to 25%→0), BB squeeze 20 (lower BB%ile=better, scaled 0pctile→20pts down to 40pctile→0), Vol dry-up 15 (0.55→15pts, 1.10→0), RS slope 20 (+2.5%/wk→20pts, -0.5%/wk→0), Pivot proximity 15 (bell curve, ideal -2% from pivot), Smoothness 10 (90%→10pts, 50%→0), Three-weeks-tight 5 bonus. Realistic top picks land 75-95; a 50-point pick is a solid setup; 70+ is high-conviction. Use it as a *priority filter*, not a deterministic ranking; treat two names within 5 points as tied. ⚠️ In the 2026-05→07 outcome backtest the Score bands did **not** discriminate post-trigger outcomes at all (see **Backtested outcomes** #3); BaseWks was the far stronger ranker.
- **RS** (*verbose*): O'Neil-style universe-relative RS Rating (1-99). Weighted average of trailing 3/6/9/12-month returns, percentile-ranked across the universe. ≥ 70 is the Minervini gate; ≥ 80 is the top quintile.
- **BaseWks**: length of the current consolidation in trading weeks. **The backtest-validated ranker**: ≥ 20 weeks is the ⭐️ validated pocket (+4.9%/trade, 75% win vs −0.8% baseline in the 2026-05→07 sample); the column leads Score in the table by design. JSON carries the pocket membership per pick as `validated_pocket: true/false`. The algorithm picks the trailing window that maximizes `days / max(width, 1)`, so a 6-week base at 5% width can beat a 30-week base at 20% width.
- **Width%**: `(base_high - base_low) / base_high × 100`. Tighter is better. Sub-15% is high quality; 20-25% is acceptable; capped by `--max-base-width`.
- **Smooth%** (*verbose*): % of base bars within ±2% of the base mean. Discriminates real horizontal consolidations (high Smooth%) from V-shapes or jagged action that happens to fit the width envelope (low Smooth%). 50%+ reads as horizontal; 70%+ is textbook smooth.
- **BB%ile** (*verbose*): percentile rank of current Bollinger Band(20) width within the last 126 trading days (~6 months). 0 = tightest in 6 months (max squeeze), 100 = widest. Below 25 is meaningful compression.
- **Vol↓** (*verbose*): volume dry-up ratio: `(last 20d avg volume) / (prior 60d avg volume)`. Below 1.0 = drying up (sellers exhausted, classic accumulation tell); below 0.75 = deep dry-up. ⚠️ The 2026-05→07 outcome backtest found this signal *inverted*: deep dry-up names failed post-trigger at nearly 2× the rate of no-dry-up names. See **Backtested outcomes** #2.
- **RSslope%/wk** (*verbose*): OLS slope of `(close / SPY)` regressed over the base period, expressed as % per trading week. Positive = stock outperforming SPY *while ranging*, which the doctrine calls the single most important pre-breakout signal — the outcome data does not back that, so don't rank on it (see **Backtested outcomes** #7). Negative = stock losing relative strength even though price hasn't dropped much; the base may be the leading edge of a downturn.
- **ToPivot%**: distance from current price to the pivot (the price to break above). 0 = at pivot; -3 = need a 3% move up to trigger. Negative is in-range; positive means we're already above (suspect; check the Signal column for breakout confirmation).
- **Pivot**: the breakout trigger price. = high of the base window. Crossing this with volume confirmation is the buy signal.
- **Sig**: entry-timing classifier:
  - **🚀 BREAKOUT**: today's close ≥ pivot AND today's volume ≥ 1.5× 20-day avg. The trigger fired today; entry decision is "now or wait for pullback".
  - **🔥 IMMINENT**: within 3% of pivot AND BB%ile < 25 AND vol_dryup_ratio < 0.95. The setup is loaded but hasn't triggered. This is the highest-information state for active traders: set a price alert at the pivot.
  - **⏳ COILED**: within 10% of pivot AND BB%ile < 30. Squeeze on but a few percent of work needed before the trigger.
  - **📊 SETUP**: valid base, but not yet near pivot or not yet showing squeeze. Watchlist candidate; check back in 1-2 weeks.
- **Stop@trigger**: pivot-anchored ATR stop = `pivot - 2.5 × ATR(14)`. The % is the distance from the pivot, **not** from current price. This is the stop level you'd set if you enter via a buy-stop at the pivot (the canonical entry for these setups). The JSON output also includes `stop_now` / `stop_now_pct` (current-price anchored) for users who want to size from spot today rather than wait for the breakout. The two numbers diverge by `to_pivot_pct`: for 🔥 names (close to pivot) they're similar; for ⏳ names farther below pivot the gap widens.
- **Streak**: consecutive prior runs this ticker has been in the top-N (1 = first appearance). Higher = base has held through multiple periods of noise; the setup is durable.
- **RankΔ** (*verbose*): change in rank vs the latest prior appearance (positive ↗ = rising, negative ↘ = slipping, 🆕 = no prior).
- **FirstSeen** (*verbose*): earliest date this base appeared in history. Combined with Streak gives the base's age in the watchlist.
- **🔒 prefix on ticker**: "three weeks tight" signal: last 3 weekly closes within 1.5% of each other. Minervini's textbook indicator that supply is absorbed; institutional selling has stopped.
- **⭐️ prefix on ticker**: validated-pocket membership (BaseWks ≥ 20). Same names as the ⭐️ section above the table; the prefix keeps them visible when scanning the full table.

The script computes the discovery sections (dropouts with reasons / recent breakouts / new setups / maturing bases) against the most recent prior run plus the last `--recent-breakout-days` of price data, and skips sections with zero entries so the output stays clean on thin days. The **Maturing bases** section (streak ≥ `--persistent-min-streak`, default 4) stays absent on the first few runs of a fresh history, as designed; it only fires once a ticker has persisted through multiple scan-days.

### Stop@trigger: worked example

For HSBC in the sample table: pivot $92.16, 14-day ATR ≈ $2.08 (computed from OHLCV). With the default `--atr-stop-mult 2.5`:

```
stop_trigger = pivot - 2.5 × ATR = 92.16 - 2.5 × 2.08 = $86.94
stop_trigger_pct = (86.94 / 92.16 - 1) × 100 = -5.7%
```

So if you enter via a buy-stop at $92.16 (the canonical entry: a market order triggered when price crosses the pivot), your initial stop sits at $86.94, a 5.7% maximum loss per share if the breakout fails and the stop hits. For dollar sizing: `shares = risk_per_trade ÷ (pivot − stop_trigger)`. If you're willing to risk $500 per trade, you'd buy ~96 shares ($500 / $5.22).

The JSON output also includes `stop_now` and `stop_now_pct` (anchored to today's close instead of the pivot) for users who want to size from spot today. The two diverge by `to_pivot_pct`: for 🔥 names close to pivot they're nearly identical; for ⏳ names a few % below pivot the gap widens.

### Sig column markers

A trailing `*` after a Sig glyph (e.g. `🚀*` or `📊*`) means the base ended within the last 3 days and today is a fresh cross above the prior range (`anchor_mode=3`). Same glyph semantics; the `*` tells you "this base's geometry resolved in the last few days; the setup is hot off the press, not still building". Most days no rows carry the asterisk.

### Single-ticker check (`--ticker AAPL`)

Bypasses the universe scan and prints a 4-stage diagnostic + ATR stop levels for one ticker. Works for any US ticker yfinance can fetch (large-cap, small-cap, ADR, foreign-listed). Runtime ~2-5 seconds (vs ~30-60s for a full scan). Honors `--format json` for structured output. Also runs the vol-collapse check (when `--vol-collapse-ratio > 0`) and surfaces a prominent `⚠️ VOL-COLLAPSE WARNING` block at the top if the ticker shows the acquisition-lock signature; the block appears even when the ticker fails earlier stages (TT-fail, no base), so a user asking "is MASI a good base?" can't miss the warning. Sample (illustrative; values will differ on your run):

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

When the vol-collapse filter triggers, the warning prints at the top of the report, before any stage analysis, so a user can't proceed to "valid base" framing without seeing the alarm:

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

The warning appears even when the ticker fails Stage 1 (TT-fail, no base) because the vol-collapse check runs early in the pipeline, before any short-circuit. The warning states its numbers with units: vol values are annualized percentages (1.9% / 98.0%); ratio and threshold are decimals (0.019 / 0.20).

The RS Rating in `--ticker` mode is an approximation (RS-vs-SPY proxy mapping weighted excess return through a ±30% band onto a 1-99 scale) rather than the exact universe-relative percentile; skipping the full 250-ticker universe fetch is the reason for the ~15× speedup. The proxy lands within ~10-15 percentile points of the true rating in practice: close enough to be a TT gate, not close enough to publish as a ranked figure. For the exact RS Rating, run the full scan (without `--ticker`) and read the RS column.

When base detection fails but the ticker broke out in the last 10 trading days, the report includes a **fallback section** with the breakout date, follow-through %, and concrete action guidance ("set a price alert at $X; if price retests from above, that's the textbook O'Neil throwback entry; if it closes below for 2 sessions, the breakout failed").

## Backtested outcomes (2026-05-14 → 2026-07-29 sample)

`scripts/backtest_outcomes.py` replays `state/history.csv` as the canonical trade: while a name stays on the watchlist, a buy-stop sits at the most recent pivot (gaps pay real slippage); dropout cancels the order. 426 testable episodes over a flat tape; re-run quarterly. The same replay with `--write-ledger` persists each finished episode to `state/outcomes.csv`, which is how the dashboard shows whether the pockets below still pay **out of sample** rather than only quoting these in-sample numbers. Findings, strongest first; full evidence, magnitudes, and caveats in `references/backtest-findings.md` (sections throughout this file reference the numbering below):

1. **Base length ≥ 20 weeks is the one big validated edge**: +4.9%/trade, 75% win vs −0.8% baseline; sub-10-week bases are where the failures live. The ⭐️ validated pocket.
2. **Volume dry-up is INVERTED vs doctrine in this sample**: deep dry-up (< 0.70) ran −8.8% with 61% stop-hit; no-dry-up was the only profitable band. **Survives the base-length control** (see #10): held to the sub-20-week cohort, so the ⭐️ pocket can't be doing the work, deep dry-up still ran −5.9%/trade on a 20% win rate and a 73% stop-hit rate against that cohort's −2.3% mean. So it carries information #1 doesn't already give you. Same ledger, not a fresh sample; re-validate on new data before hard-coding.
3. **The composite Base Score does not discriminate outcomes**: bands statistically identical; treat Score as a display floor and rank by BaseWks.
4. **The watchlist as a whole did not pay**: post-trigger avg −2.6%, 66% fall back below pivot within 5 sessions; selection (which base) is everything.
5. **Don't chase gap fills**: fills > 3% above pivot averaged −11.5%; within 1-3% were fine.
6. **Volume confirmation did not rescue entries**: it costs next-open slippage and selected no better in this tape.
7. **RS slope's "sweet spot" is mostly base length in disguise** ⚠️: the raw split reads 0-1%/wk good, > 1%/wk extended high-beta (−6.5%, 67% stop-hit). The base-length control (see #10) collapses the good half: across all trades the 0-1%/wk band runs +1.5%/trade with a 95% interval clear of the baseline, but inside the sub-20-week cohort it runs −1.2%/trade with the interval straddling it, and the spread across the four bands falls 4.9pt → 2.3pt. Long bases happen to land in that band; the band selected nothing on its own. Do not rank on RS slope. The "> 1%/wk is extended" half hasn't been put through the same control and may go the same way.
8. **First appearance far from pivot is a fade signal**: entering at −10..−3% from pivot brings a 28% trigger rate and −3.4%/trade; slow triggers (6-10 sessions) arrive exhausted.
9. **Cutting on the first close back below the pivot saves the junk and destroys the pocket**: −3.6pt on ⭐️ pocket trades (+4.45% → +0.86%/trade), +2.0pt on everything else. Falling back is a good *descriptor* of a bad trade, not an exit rule for the validated stratum.
10. **Only two attributes survive holding base length constant, and both say "don't buy" rather than "buy"** (2026-08-02 re-analysis). Method: stratify the 154 completed trades in `state/outcomes.csv` by every attribute `history.csv` records at the *first* listing day (the decision point), then re-run each split inside the sub-20-week cohort so finding #1 can't launder its edge through a correlated column. Survivors: **Vol↓ < 0.70** (#2) and **BB%ile ≥ 50** (−3.8%/trade, 27% win, 68% stop-hit) — though the second is the weaker claim, since every trade in that band came from a sub-20-week base, so the two attributes are collinear here and the control can't actually separate them. Casualties: RS slope's good band (#7). **Flat across their bands** — spread between best and worst, where #1's is 7.5pt: `rank` **0.5pt** (the top-ranked name and the #50 name returned the same, which is why the ranker matters less than the filter), Score 1.5pt (#3 again), Sig tier (🔥 −0.6% vs 📊 −0.7%, so the "loaded" glyph does not predict), width, and gap size. Stacking both exclusions lifts the whole ledger −0.95% → +0.06%/trade but leaves non-pocket trades at −1.22%: they cut losses, they don't make the rest pay. Two cautions before acting on any single line here — ~40 band comparisons ran, so at 95% roughly 2 "significant" bands are expected from noise alone; and this is a **re-analysis of the same ledger as #1-#9**, not an out-of-sample test, so it can correct what those findings *controlled for* but cannot independently confirm them.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass: apply judgment rather than reciting the principles below. **Where the backtest above and the doctrine below disagree, lead with the backtest and say so**: e.g. weight BaseWks > 20 over Score, and do not sell deep Vol↓ as a bullish tell.

Relay the script's markdown output **in full — every row of the top-N table and every section**; don't truncate to save space. Then write the interpretation for a reader with **no finance background**, in the conversation's language, translating each term the moment you use it — a "base" is "a stock that already rose a lot and has been moving sideways in a tight range while sellers run out"; "pivot $108.60" is "the price line that, once crossed, is the buy trigger"; "Stop@trigger -3.6%" is "if you bought at the trigger, the exit that caps the loss at 3.6%". Lead with the Sig strip story (is the watchlist loaded or quiet?) and the ⭐️ pocket (the only stratum with backtest-proven odds), and close with what to do — usually "set alerts, buy nothing today".

1. **Lead with the 🚀 Breakouts section if non-empty.** This is the most actionable, time-sensitive subset: a base broke today *with* volume confirmation. The decision space is narrow: enter now near pivot, or wait for the retest of the breakout level (it tends to come within 5 sessions). Quantify the per-trade risk using the Stop column; reject the entry if the per-trade loss exceeds the user's risk budget. Remind the user: most breakouts fail (40-60% by various studies). Stop discipline is non-negotiable.

2. **🔥 IMMINENT names beat 🚀 BREAKOUTs in expected value for most users.** 🚀 means the breakout already happened, so you're chasing 1-2% above the pivot with all the risk and less of the move. 🔥 means the trigger hasn't fired yet: you can set a buy-stop *at* the pivot, which removes the "chase" cost. The entry-mechanics logic holds, but don't oversell the signal itself: in the 2026 backtest 🔥-at-start episodes performed no better than 📊 ones (the 🔥 state requires deep dry-up, which was inverted; see Backtested outcomes #2), and episodes that *reached* 🔥 fell back below the pivot within 5 sessions 85% of the time. The backtest also found RSslope > +1%/wk during a base was a *negative*, not a positive — but don't flip that into "prefer the calm 0-1%/wk band": once base length is held constant that band's edge disappears (see Backtested outcomes #7).

3. **Read the Regime banner first.** Base breakouts have much higher failure rates in RISK-OFF markets: most bases break *down*, not up, when the broader tape is weak. In RISK-OFF: do NOT recommend specific buys; reframe the list as "names showing structural strength that we want to revisit when the regime turns". The skill auto-suppresses the top-N when `--regime-gate strict` and RISK-OFF, which is the safer default for cautious users.

4. **Watch sector clustering.** A list dominated by one sector (e.g., 6 of 19 Energy, 4 Financials) often means the *sector* is basing, not only individual names. The trade is then the sector (sector ETF, or a basket of the top 3 from the cluster) rather than picking the highest-Score single name. Isolated newcomers in unrelated sectors are higher single-name conviction but also higher single-name risk.

5. **Maturing bases (streak ≥ 4) are the real signals, with a nuance the backtest added.** Long persistence means the base has held through real market noise. But note what the outcome data validated: *base length in weeks* (BaseWks ≥ 20) was the strong predictor; *time sitting on the watchlist before triggering* was not protective (triggers that took 6-10 sessions to fire went 22% win). So prefer a name that's been consolidating for months over one that's been on our list for days; streak is a durability check, not an edge by itself.

6. **Read Score *components* for hidden information.** A 60-point Score is a *composite*: two names with identical scores can have very different profiles. Patterns to recognize:
   - **"All geometry, no RS"**: high tightness + BB squeeze + smoothness + pivot proximity but flat or negative RS slope. The setup *looks* perfect on the chart but the stock is losing ground to SPY under the surface. More likely to break the *wrong way*.
   - **"All RS, no geometry"**: strong positive RS slope but wide width / high BB pctile / low smoothness. Buyers are accumulating the stock but the consolidation hasn't tightened yet; expect it to need another 2-4 weeks.
   - **"Balanced quality"**: middling on every component, no standout strength. These land at 50-65 run after run, the consistent "watchlist filler": neither exciting nor disqualifying.
   - **"Loaded"**: high on every component including positive RS slope. Rare; these score 75+ and are the textbook setups.

   These four labels are for *reading* a setup — they tell you what kind of thing you're looking at — and none of them has been shown to predict the result; treat them as vocabulary, not as a ranking. In particular, the advice that used to sit here (on a Score tie, prefer the higher RS slope) is **withdrawn**: RS slope's apparent edge was base length in disguise (#7), and a "tie on Score" isn't a meaningful starting point anyway since Score doesn't discriminate outcomes at all (#3). Break ties on BaseWks, the one attribute that ranked. Cap on the score: even all-perfect + the 3-week-tight bonus sums to 110 → capped at 100, so any **90+ is exceptional**, **75+ is high-conviction**, **50-65 is solid filler**.

7. **Dropouts reveal more than the picks.** A name leaving the list with reason="broke_out" is a win; the setup did what it was supposed to do. "broke_down" is a loss; the base failed and the stop (would have) fired. "no_longer_qualifies"/"faded" is neutral noise; the score drifted below threshold for benign reasons. Surface the *broke_out* names in particular: those validate the screening logic, and sustained "broke_out" rates above ~40% on prior watchlist picks would mean the screen is working as intended.

8. **The Vol↓ ratio is more leading than BB%ile, but in the 2026 backtest its doctrine sign was WRONG.** The textbook story (dry-up = sellers exhausted = bullish) did not survive: deep dry-up (< 0.70) names lost −8.8% avg post-trigger with a 61% stop-hit rate, while no-dry-up (≥ 0.90) names were the only profitable band. It's also one of only two attributes that survived holding base length constant (**Backtested outcomes** #10), so unlike most of the diagnostic columns it carries information the ⭐️ pocket doesn't already give you — as an exclusion, not a pick. Until a trending-tape re-test says otherwise, read Vol↓ < 0.70 as "possibly dead, not coiled" and stop treating it as a bullish tell on its own. The trajectory read (watching Vol↓ and BB%ile evolve across runs via `--show-history`) is still the most informative use of the metric.

9. **Read the Recent breakouts section as the bridge from "what's setting up" to "what just broke".** The main watchlist surfaces *pre-breakout* candidates; Recent breakouts surfaces names that crossed their pivot on volume in the last 5-10 trading days (too late to enter at the pivot but still in the follow-through window). Use the split:
   - **Working** (positive % vs pivot): the breakout is *confirmed*; these names crossed and held. The actionable read is whether a *pullback to the breakout level* offers a second entry (textbook O'Neil "throwback" entry). When a working breakout is +1-5% above pivot, that retest is often imminent; when it's +15%+ above pivot, the entry window is closed and chasing is high-risk.
   - **Failed** (negative % vs pivot): the breakout *triggered then reversed*; the base failed. These also work as a *tape signal*: when the failed list runs long (more failed than working), the broader market is digesting breakouts poorly, a yellow card on initiating new entries in the main watchlist until the ratio flips.
   - When the section is missing (or has zero entries), no qualifying breakouts happened in the lookback window: a quiet stretch; focus shifts to the main watchlist alone.

10. **Never recommend specific buys.** Frame results as "names worth investigating" with specific risk parameters (pivot for entry, ATR stop for risk). Flag that base-and-breakout strategies depend on stop discipline more than entry selection. The literature win rate is 40-50%; without the stop loss, that's a money-losing strategy. *With* a 7-8% stop and asymmetric payoffs, it's profitable. Don't present the published `Stop@trigger` as the stop those odds were measured at — it's ATR-derived and can run tighter than 8%; see **Known limitations** for what is and isn't established. The skill cannot enforce the stop; only the user can.

## State files

- `state/history.csv`: one snapshot per US market day (America/New_York) × **every ticker that passed the funnel that day** (all kept picks, not only the displayed top-N; below-cutoff rows preserve near-miss context, same design as momentum-scan, and persistence stats filter to rank ≤ top-N at read time, so Streak / RankΔ only count appearances you saw). Columns: `run_id, run_date, ticker, rank, score_rank, base_score, base_weeks, width_pct, bb_pctile, vol_dryup_ratio, rs_slope_pct_per_wk, to_pivot_pct, pivot_price, signal, close, atr, atr_stop_mult`. The last three (added 2026-08-02) exist for **stop-rule research**, and store the raw ATR rather than a derived stop level on purpose: persisting `stop_trigger` would freeze whichever `--atr-stop-mult` was live that day, and the open question is which multiplier is right — including whether the ledger's fixed 8% beats any of them (see **Known limitations**). With `atr` + `pivot_price` every multiplier replays on identical episodes; `atr_stop_mult` records what the run actually displayed so that stays recoverable on a non-default run. `close` is the adjusted close the scan saw, and lands even with `--atr-stop-mult 0` since it comes off the base detection rather than the ATR pass. It normally equals the price `stop_now` is anchored to, but don't treat that as an identity: the two are computed independently, and `compute_atrs` intersects the High/Low/Close indices before taking its last bar, so a final bar carrying a Close but missing a High or Low would leave `stop_now` anchored one session earlier than this column. All three are empty for rows written before the upgrade and left unfilled by design — later corporate actions rewrite the adjusted series and delistings erase it, so a backfill wouldn't reproduce what the run saw. ATR is computed for **every** persisted pick, not just the displayed top-N: the ⭐️ pocket ignores the rank cutoff, so filling only the top-N would sample this data on exactly the wrong side of it. (The script computes `anchor_mode` but doesn't persist it to history; it only matters for the current scan's Sig column markers.) Re-running the same ET day overwrites that day's rows. Writes are atomic (.tmp + rename) so a crash mid-write can't truncate. **The skill gets more useful with each subsequent run**: the first run is only the picks; later runs add streak, RankΔ, breakout/breakdown tracking, and base-maturation signal.
- `state/universe.txt`: cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json`: per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker. Fetched on demand for top-N picks only.
- `state/outcomes.csv`: the outcomes ledger — one row per **finished episode**, keyed `(start_run_id, ticker)`, which is the unit the canonical trade actually operates on (a buy-stop living from first listing to dropout), not the run-day. Columns: `start_run_id, ticker, end_run_id, outcome, days_to_trigger, gap_pct, ret5, ret10, ret_h, trade_ret_pct, exit_day, spy_trade, qqq_trade, fellback5, stop_hit, censored, horizon, stop_pct, entry`. `exit_day` is how many sessions after the fill the trade actually ended (the stop day, or the horizon when it ran full), and `spy_trade` / `qqq_trade` are the two indices' close-to-close returns over exactly that window — the dashboard's control line, recorded here because the resolver already holds SPY when it decides the exit, so the panel needs no second fetch. Half the completed trades stop out early, so a benchmark held for the full 20 sessions would be answering a different question. `outcome` is `TRIGGERED` / `FADED` / `BROKE_DOWN`; episodes still in flight (no trigger yet *and* the watch window runs past the data edge) are deliberately left out, so a missing row means "undecided", never "failed". The last three columns record the resolution convention so a consumer can tell whether the numbers are comparable to the published findings (horizon 20 / stop 8% / touch). Written by **every scan**, which resolves the pending episodes off the 14 months of adjusted OHLCV it already downloaded — the same units the recorded pivots were computed in, so this path and the backtest's own fetch score an episode identically. Work is bounded by ledger *state*, not by a date window: a trade completes 20 sessions after its trigger, which can fall long after the name left the list, so the pending set is "no row yet, a row cut short by the data edge, or a `TRIGGERED` row whose trade hasn't completed" and drains on its own (a `FADED` / `BROKE_DOWN` row is terminal — its empty trade result *is* the outcome, so it leaves the set for good; a ticker that stops trading entirely mid-trade resolves as a **forced exit at its final bar** — real money, and a terminal row — instead of waiting forever for bars that will never print). Much of what remains is re-resolved every run and writes back identical rows; the scan reports only what actually moved. **One structural blind spot**: the scan holds bars for the *current* universe, so an episode whose ticker has since dropped out (market-cap / volume drift, or an acquisition) freezes at whatever completeness it had — and those names did not leave at random, so letting the gap grow biases the expectancies the dashboard reports. The scan names the stuck tickers on stderr; `backtest_outcomes.py --write-ledger` fetches **by ticker** rather than by universe and reaches them, which is one more reason to run it on the quarterly cadence. (Episodes whose pivot pre-dates a split/spinoff re-adjustment are named on stderr too, but are unscoreable by any path — every fetch returns the re-adjusted bars.) Both writers use a keyed upsert that preserves rows the run couldn't reach, and a failed refresh never costs the run its watchlist. Tracked in git: only regenerable while yfinance still serves the price window (~13 months) and the ticker still trades.
- `state/history.html`: self-contained HTML dashboard rendered from `history.csv` + `outcomes.csv` + `sectors.json` by `scripts/render_history_html.py`: KPI row, an approach-to-pivot chart (one line per episode, y = distance to the pivot, so the zero line *is* the trigger and a line reaching it is a breakout; filterable to ⭐ pocket / triggered / all), a watchlist-tension chart (each day's list stacked by Sig tier with the ⭐ pocket count overlaid), a ⭐-pocket-vs-rest running trade-expectancy chart against the backtest's in-sample references and dotted matched-index controls (SPY and QQQ, same trigger day, same holding length, stop-outs included), a date × ticker maturity grid (cell = that day's Sig tier, dot = ⭐ pocket day, row end = realized result), a **per-sector realized-result panel** (one bar per sector = avg %/completed trade, with its 95% interval drawn above it and a dashed all-trades average as the reference; a sector whose interval reaches that line is not distinguishable from the board and is drawn back to 42% opacity), a sortable roster, and an EN/简中/繁中/日/한 language menu. **Deliberately not the sibling scans' chart sets**: momentum-scan draws rank trajectories (persistence story) and mean-reversion-scan draws outcome grids (event story); this one's spine is distance-to-pivot over time, the one axis in the family with an absolute meaning. No external assets or network; regenerable at any time, so it's gitignored. Re-render after a scan when the user wants the visual view.

Storage growth: each run adds one row per funnel-passing name: ~50-90 rows × ~150 bytes ≈ 8-14 KB in practice. A year of daily runs ≈ 2-3.5 MB; weekly ≈ 400-700 KB. Negligible for years of typical use.

## Cadence

Cadence-agnostic by design. One snapshot per US market day (America/New_York), so streak counts **consecutive prior scan-days**; running twice on the same ET day refreshes that day's entry. Runs on weekends or NYSE holidays auto-skip from history (results still print, with nothing appended) so streak doesn't inflate from duplicate-data days. Pre-market runs on a real trading day **do** save.

Recommended cadence:
- **Daily**: streak unit is days. Finest granularity, sees breakouts the day they happen. Best for active traders.
- **Weekly (Friday close)**: streak unit is weeks. Recommended sweet spot: captures base maturation 4× faster than monthly while smoothing intra-week noise. Best for swing traders.
- **Monthly**: streak unit is months. Smoothest signal, only the longest / highest-conviction bases survive. Best for position traders.

For automatic recurring runs, use a local scheduler (macOS `launchd`, or `cron`) pointing at `scripts/scan.py`. The `schedule` skill runs *remote* agents in Anthropic-managed sandboxes that can't see this local `state/` directory.

## Known limitations

- **Base-detection is a screener heuristic, not a chart annotator.** The trailing-window approach (find the longest window where price range ≤ max_base_width) catches the same population as Minervini's VCP / O'Neil's cup-and-handle without trying to identify those exact patterns. Real chartists can find textbook bases this misses (e.g., a stair-stepping handle inside a larger cup), and vice versa: a window that meets the numerical criteria may be a *failing* base by eye (descending highs, no proper handle). Use the surfaced names as candidates to chart, not as finished analysis.
- **Survivorship bias**: the universe is current US large caps; delisted names are absent. This is fine for forward-looking screening but means any backtested win rate from this exact universe would be optimistic vs. a true point-in-time universe.
- **Yahoo data quirks**: rare missing bars, late dividend adjustments. If a single name's metrics look wrong (e.g., width% way off the chart), sanity-check via the `yfinance` skill or the underlying ticker's chart on any free site.
- **52w high reference is rolling, not the absolute high.** A stock that made an all-time high 14 months ago doesn't show that high; we only see the last 252 trading days. So a "20% from 52w high" might be a much smaller correction from a more-recent acute high vs. all-time. This seldom matters for the screen's purpose (we're looking at *recent* setups), but worth knowing when interpreting deep-base names.
- **RS Rating is universe-relative, not market-relative.** O'Neil's published RS Rating uses a much broader universe (~6000 stocks); ours uses the 250 large caps in scope. So our RS = 70 corresponds to "top 30% within S&P large caps", not "top 30% across all listed stocks". For large-cap-only investing this is the right denominator; for "is this stock outperforming the market as a whole", check RS slope (which uses SPY).
- **Base detection ignores patterns within the base.** A stock ranging 0-25% width passes width check whether the range is smoothly horizontal or jagged with V-spikes. The latter often looks "right" by the screen but fails more often in practice. Cross-checking with the BB%ile (low BB pctile = smooth) and width% (low width = smooth) filters out most of the V-spike cases, but not all.
- **`--regime-gate` reduces bear-market downside but can't catch fast regime flips.** A 200DMA slope that turns negative requires ~20 trading days to register; a 1-week crash (Aug 2024, March 2020, Feb 2018) blows through the gate before the slope flips. Treat the regime banner as helpful framing, not a defensive moat.
- **Volume dry-up ratio uses 20d/60d SMA, which lags.** Fast regime changes (M&A talk, earnings surprises) spike volume in days; our ratio takes weeks to catch up. Treat the metric as "is supply drying up over weeks" rather than "what's volume doing today".
- **The "broke_out" / "broke_down" labels in dropouts use a simple 0.5% / 8% threshold from the pivot.** A name that broke out by 0.3% and reversed isn't a confirmed breakout, but we'd label it "no_longer_qualifies" only if it's below the threshold today. Treat the dropout reasons as a heuristic, not an audit trail.
- **Vol-collapse filter has a "gap-in-second-half" blind spot**: a merger announced within the last ~6 weeks puts the gap in the second half of the 3-month lookback, ratio > 1, name passes through (late detection, not a silent miss). **This bites hardest in `--ticker` mode**: asking "is MASI a good base?" on announcement day gets no warning, and that's a high-stakes single-name verdict. Leak fingerprint: `MaxDD%` < -1% *and* `width%` < 2%; confirm via the `yfinance` skill's `sec_filings --type PREM14A,DEFM14A` before treating any recently-gapping name as a tradeable base. Both failure directions, the MASI case, and mitigations in `references/vol-collapse.md`.
- **The stop you're shown and the stop the numbers were validated at are not the same stop.** Output publishes an ATR stop (`Stop@trigger` = pivot − 2.5 × ATR); the ledger and every backtest expectancy quoted here score trades at a **fixed 8%**, which is also what the literature's 40-50% win rate assumes. SKILL.md used to call these "a 7-8% (or ATR-based) stop" as if interchangeable — they aren't necessarily. Both `Stop@trigger` values this file documents run **tighter** than 8% — `-5.7%` in the worked example above and `-3.6%` in the output-relay guidance — and a tighter stop cuts earlier, which is the direction **Backtested outcomes** #9 measured as destroying the ⭐️ pocket (+4.45% → +0.86%/trade). Whether that generalizes is unmeasured: those are two hand-picked illustrations, not a distribution. `history.csv` started recording `atr` on 2026-08-02 precisely so the question becomes answerable; until roughly a quarter of rows accumulate, treat the published expectancies as valid **at 8%** and read `Stop@trigger` as a risk budget rather than as the stop those numbers assume. When the data is there, add ATR-stop as an extra column in `backtest_outcomes.py`'s exit-rule table and compare paired on one fixed trade set (the discipline mean-reversion-scan's finding #7 used) — don't swap the ledger convention on a hunch, since that also forces a full re-resolve and breaks comparability with the published win rates.
- **No fundamental check.** The screen is 100% price/volume; it doesn't know if the company has decelerating earnings, regulatory overhang, or executive turnover during the base. The literature's 40-50% win rate is *without* a fundamental filter; adding even a simple "EPS growth not declining" check would lift it by a real margin. The user should at minimum eyeball the latest earnings before acting on any pick.
- **Pivot is the in-base high, which may be conservative.** Some chart traders prefer the high of the *handle* (a sub-pattern inside a cup-with-handle base) as the actual pivot, which tends to sit a few percent below the full cup high. Our pivot uses the full base high: a more conservative trigger, worse entries, and a lower false-breakout rate, each by a small margin.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)**: this only matters if Yahoo's response stops including the `total` field (schema drift), in which case the script falls back to per-page heuristics (short page / zero new tickers) to detect end-of-results. The 20-page cap is the absolute backstop; if it triggers you'll see `refresh_universe: hit SCREENER_MAX_PAGES=20 backstop` on stderr and the universe caps at ~5000, well above any realistic large-cap match count, so triggering it signals something wrong upstream.
- **Universe size affects historical rank comparability**: if you raise `--universe-count` (or the underlying universe grows from market-cap drift) between runs, ranks recorded in `state/history.csv` from before the change aren't comparable to ranks after. A larger universe means more names can pass the funnel, which can demote previously-high-ranked names because new entrants joined the pool, with no change in the setups themselves. `Streak` and `FirstSeen` survive (a ticker is still "in the top N" or not), but read `RankΔ` across a universe-size change with that caveat. If you want clean before/after comparison, run `--clear-history` after changing universe size.

## Tests

```bash
cd <SKILL_DIR>/scripts && uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  --with 'numpy>=1.24,<3' --with pytest pytest -q
```

Pure-logic tests (no network) cover the base-detection geometry, scoring components, signal classification, and history/persistence logic (`test_scan.py`), the vol-collapse filter's split-half math, thresholds, and exclusion behavior (`test_vol_collapse.py`), the paginated universe refresh with its fallback heuristics (`test_refresh_universe.py`), and the dashboard payload plus the ledger's episode classification and upsert (`test_render_html.py`). The last file also carries the drift guards that pin the stdlib-only renderer to `scan.py`'s `VALIDATED_BASE_WEEKS` and Sig vocabulary, and its reference lines to `backtest_outcomes.py`'s resolution defaults — the renderer duplicates those numerically because it must not import yfinance.
