---
name: mean-reversion-scan
description: "Scan US large-cap equities for short-term oversold reversals: Connors-style RSI(2) setups inside confirmed long-term uptrends. Use when the user wants oversold bounces, mean-reversion entries, short-term pullbacks in strong stocks, or a 'buy the dip' watchlist. Triggers on 'find oversold bounces', 'RSI(2) setup', 'mean reversion candidates', 'short-term pullback', 'buy the dip', 'bounce candidates', 'panic sellers', 'overdone sell-off'. The complement to momentum-scan and base-breakout-scan: those find what's running and what's about to run; this finds what just got punched in the face but is structurally fine. Do NOT use for single-ticker chart analysis (use yfinance), value/contrarian long-term picks, ETF screening, or generic explanations of mean-reversion theory."
---

# mean-reversion-scan

Find US equities that are **short-term oversold inside a confirmed long-term uptrend**: Larry Connors's canonical RSI(2) setup, augmented with persistence tracking and per-name outcome resolution so each subsequent run shows you the **running win rate** of past picks.

The core bet: in a healthy uptrend, a stock whose 2-day RSI dives below 5 is likely panicking on a short-term overreaction (margin calls, ETF rebalancing, headline noise) rather than starting a real breakdown. The mean-reversion edge is **the bounce back to the 5-day average within 1-5 trading days**. Connors's published win rates on this exact setup are 70-75% on liquid US large-caps in RISK-ON regimes; the 25-30% losing trades tend to be small-to-moderate but include occasional gap-down disasters when the trend was breaking for real.

The natural complement to `momentum-scan` and `base-breakout-scan`:
- `momentum-scan` finds **what's already running** (trailing return + low drawdown)
- `base-breakout-scan` finds **what's about to run** (compressed pre-breakout bases)
- `mean-reversion-scan` finds **what just got punched in the face but is structurally fine** (oversold inside an uptrend)

The three skills filter for non-overlapping price patterns by design; you wouldn't want any of them to surface the same name on the same day.

By default each run surfaces:
1. A **regime gate** (SPY > 200DMA + rising, Connors's hard filter; mean-reversion longs in a confirmed downtrend is the classic "catching falling knives" trap)
2. A per-ticker **uptrend filter** (price > 200DMA, 200DMA slope positive; same logic at the name level)
3. The **RSI(2) trigger** (default < 5; deep tier < 2 fires the 🔵 signal)
4. A **composite Reversion Score** (0-100) combining RSI depth, trend health, pullback magnitude, and frequency-of-trigger uniqueness
5. **ATR-based stop loss** (per-name max-loss anchor)
6. **Outcome resolution on past picks**: for every signal in the last ~30 trading days, did price reach the 5DMA target within 5 days (won), hit the stop (lost), or expire flat? The result is a running win-rate stat that grows more reliable as history accumulates.
7. The **vol-collapse filter** (same M&A-arb defense as the sister skills; without it, an acquisition-target with a post-deal price-pin can satisfy "RSI(2) low" without being tradable)

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2`, `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run: RSI(2) < 5, top 30
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/scan.py

# Tighter trigger (only deep oversold)
... python <SKILL_DIR>/scripts/scan.py --rsi2-threshold 2

# Looser trigger when the market hasn't been giving signals
... python <SKILL_DIR>/scripts/scan.py --rsi2-threshold 10

# Inspect history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# One-shot: resolve EVERY reachable history signal into state/outcomes.csv
# (normal runs only look back ~15 days; run this once to seed the ledger,
# or again after a scanning gap)
... python <SKILL_DIR>/scripts/scan.py --backfill-outcomes

# Render history.csv + outcomes.csv into a self-contained HTML dashboard at
# state/history.html (breadth × outcome columns, outcome grid, ⭐ pocket vs
# rest expectancy, per-sector result panel, roster table). Stdlib-only, no
# network, no uv needed:
python <SKILL_DIR>/scripts/render_history_html.py   # --days 60 --out <path>

# Single-ticker diagnostic: "is AAPL set up for a bounce right now?"
... python <SKILL_DIR>/scripts/scan.py --ticker AAPL

# Machine-readable JSON
... python <SKILL_DIR>/scripts/scan.py --format json

# Strict regime gate: suppress top-N when SPY < 200DMA
... python <SKILL_DIR>/scripts/scan.py --regime-gate strict

# Override ATR stop multiplier (default 2.5; pass 0 to disable)
... python <SKILL_DIR>/scripts/scan.py --atr-stop-mult 3.0

# Disable sector tagging (faster first run, no Sector column)
... python <SKILL_DIR>/scripts/scan.py --no-sectors

# Disable vol-collapse acquisition-target filter
... python <SKILL_DIR>/scripts/scan.py --vol-collapse-ratio 0

# Outcome backtest: replay history.csv with the canonical target/stop trade,
# stratified by signal attributes (see "Backtested outcomes" section)
... python <SKILL_DIR>/scripts/backtest_outcomes.py
... python <SKILL_DIR>/scripts/backtest_outcomes.py --target-window 10
# Realistic execution variant: enter at the NEXT session's open instead of
# the signal-day close (skips signals that gap past target/stop overnight)
... python <SKILL_DIR>/scripts/backtest_outcomes.py --entry next-open
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--rsi2-threshold` | 5.0 | RSI(2) ceiling for the 🟢 fresh-trigger signal. Connors's published value is 5; raise to 10 in thin tapes (more candidates, shallower oversold), lower to 2 to catch only deep panics. The 🔵 deep tier is hardcoded at half the threshold (default 2.5). |
| `--top-n` | 30 | How many candidates to display + log to history. |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower to include small-cap reversals (richer in this pattern but with much higher tail risk: a small-cap "RSI < 5 inside an uptrend" can still be a CEO-leaving-tomorrow situation). |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match (~1000 US large caps at default mcap/volume floors). The screener returns at most 250 rows per request, so the script paginates larger values with `offset`. Pass an explicit positive integer to cap. If you raise above the cached size, the script force-refreshes the cache. |
| `--refresh-universe` / `--no-refresh-universe` | (TTL 7d) | Force refresh / use cache regardless of age. |
| `--ticker` | — | Single-ticker diagnostic mode (e.g. `--ticker AAPL`). Bypasses universe scan; shows trend template pass/fail, RSI(2), 5DMA distance, signal classification, ATR stop, and historical reliability over the last ~60 trading days for this name. ~2-5s vs ~30-60s for full scan. Honors `--format json`. Writes no history. |
| `--show-history` | — | Print history summary including running win rate; no new scan. |
| `--backfill-outcomes` | — | One-shot: resolve every history signal the price data can reach (no ~15-day lookback cutoff) and merge into `state/outcomes.csv`, then exit without scanning. Idempotent. Needed once to seed the ledger, and again after any scanning gap longer than the lookback (an unresolved hole never self-heals otherwise). |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--prune-non-trading-days` | — | One-shot cleanup: drop history rows whose ET-date `run_date` is not an NYSE trading day. |
| `--no-save` | — | Don't append this run to history. |
| `--save-stale` | — | Override the non-trading-day guard. By default the script skips `append_history` on weekends / NYSE holidays so streak counts and outcome resolution don't double-count duplicate-data days. Pre-market runs on a real trading day still save. |
| `--allow-same-day` | — | Append even if a row exists for today's ET date. Default overwrites today's snapshot. |
| `--format` | markdown | `markdown` or `json`. |
| `--verbose` | — | Restore the diagnostic columns (5DMA%, 50DMA%, 200DMA%, Freq60d) to the top-N table. The default slim table (2026-07-31 redesign) keeps the decision columns — RSI(2), Score, Sig, Streak, Stop, Target — since the Target column already encodes the 5DMA distance and the rest feed the Score. JSON always carries every field. |
| `--regime-gate` | warn | `off` skips SPY trend calc. `warn` shows banner + RISK-OFF caveat but still prints top-N. `strict` suppresses top-N when RISK-OFF (history still saved). RISK-ON requires SPY > 200DMA AND 200DMA slope (20 trading days) above a small `-0.05%` dead band. **Mean-reversion longs are at their most dangerous in RISK-OFF**: the canonical failure mode is "every oversold bounce is followed by more selling" (2008 H2, 2020 March, 2022 H1). Use the strict gate for live trading. |
| `--atr-stop-mult` | 2.5 | ATR-based stop multiplier. Computes 14-day ATR and adds a `Stop` column showing `last_close - mult × ATR`. Typical: 2.0 tight, 2.5 standard, 3.0 loose. Pass `0` or negative to disable the column. The script **also persists the stop to history.csv** so outcome resolution can check whether price hit the stop between signal and target. |
| `--no-sectors` | — | Disable sector tagging. Default fetches sector/industry from yfinance for top-N picks (cached, 30-day TTL) and shows a Sector column + breakdown line. |
| `--vol-collapse-ratio` | 0.2 | Acquisition-target / lock-in filter. Same logic as the sister skills: a stock pinned at a cash buyout offer satisfies "low RSI(2)" without being tradable as mean reversion. Excludes names where 2nd-half realized vol over a 3-month window is < ratio × 1st-half vol. Default 0.2; raise to 0.3 for more aggressive exclusion (more false positives), lower to 0.15 for stricter. Hard cap 1.0. Pass `0` or negative to disable. |
| `--persistent-min-streak` | 3 | Streak threshold for the **Stuck oversold** section. **In mean reversion, a long streak is a yellow flag, not green**: the bounce hasn't materialized after multiple runs, which suggests something structural rather than a noise overreaction. Default 3 surfaces these for review. Backtest-validated: expectancy collapses at the 3rd consecutive listing (**Backtested outcomes** #3), so don't lower this to 2. |
| `--target-window-days` | 5 | Number of trading days within which the bounce-to-5DMA must occur for an outcome to count as WON. Connors's canonical exit is "first close ≥ 5DMA"; we use intraday high to be charitable. After this many days without target or stop hit, outcome is EXPIRED. |

## Output shape

A regime banner (including a **Signal breadth** tier line; see below), sector breakdown, the **Sig** cohort strip (🟢/🔵/🟡/🔴 counts across the top-N), an optional **Excluded by vol-collapse filter** section, the **⭐️ Validated pocket** section (Score ≥ 40 on a day-1/2 listing — the funnel's validated stratum; always printed when there are picks, because an empty pocket is itself the signal), the main top-N table, and 2-3 discovery sections (recently-resolved picks with running win rate, stuck-oversold leaders). The script skips sections with zero entries. The table is the **slim** default (2026-07-31 redesign, decision columns only); `--verbose` restores the 5DMA%/50DMA%/200DMA%/Freq60d diagnostics, and JSON always carries every field (including `validated_pocket` per pick). Sample (illustrative; picks change daily):

```
# Mean-reversion scan — 2026-05-14 16:32 UTC

**Params**: rsi2_threshold=5.0, target_window=5d, mcap>5e+09
**Universe**: 1035 tickers · **Passed filter**: 18 (vol-collapse: 0 excluded) · **Prior runs**: 12
**Signal breadth**: 18 → **THIN** (<30) — ⚠️ isolated oversold: on days like this the backtest ran −1.39%/signal and Score ≥ 40 did not rescue it. Treat today's list as research-only.
**Regime**: SPY 742.3 vs 200DMA 672.2 (+10.4%) · 50DMA > 200DMA · 200DMA slope (20d): +1.50% · Breadth: 58% > 200DMA → **RISK-ON**
**Win rate** (last 30d, 47 resolved): 73% (34W / 13L) · avg days to target: 1.9
**Sectors**: Tech 5 · Health 4 · Financ 3 · Cons Cyc 2 · Energy 2 · Other 2
**Sig**: 🟢8 🔵3 🟡7 🔴0

## ⭐️ Validated pocket — Score ≥ 40 & listing day ≤ 2 (1)
_The stratum the outcome backtest validated (+1.83%/signal, ~3× baseline, in-sample); 3rd-day-plus listings ran negative._
- **AAPL** (#1): score 65, day 1, RSI(2) 1.8, 🔵

## Top 18

| # | Ticker | Sector | RSI(2) | Score | Sig | Streak | Stop | Target |
|---|---|---|---|---|---|---|---|---|
| 1 | ⭐️ **AAPL** | Tech | 1.8 | 65 | 🔵 | 1 | $228.40 (-2.7%) | $237.55 (+1.2%) |
| 2 | **JNJ** | Health | 4.2 | 35 | 🟢 | 1 | $158.20 (-4.3%) | $164.10 (+0.9%) |
...

_Diagnostic columns (5DMA%, 50DMA%, 200DMA%, Freq60d): --verbose_

## Recently resolved (last 10 days, 6 picks)
**Won** (4): avg +1.7% in 1.5 day(s) · best NVDA +2.0%, MSFT +1.3%, JPM +1.2%
**Lost** (1, worst first):
- **XYZ**: signaled 2026-05-10 @ $52.80, stopped at $48.10 (-8.9%) in 3 day(s)
**Expired** (1): drifted -0.4% on average; neither target nor stop hit within the window

## Stuck oversold (streak ≥ 3 runs: REVIEW for structural break)
_The bounce hasn't materialized across multiple runs. Usual causes: real breakdown, missed news catalyst, or sector-wide pressure; a warning list, not a bargain bin._
- **GHI**: streak 4, first seen 2026-05-08, RSI(2) trajectory: 4.5 → 2.8 → 3.6 → 3.1
```

Column meanings (columns marked *verbose* print only with `--verbose`; JSON always carries them; a ⭐️ ticker prefix marks validated-pocket membership, same names as the ⭐️ section):

- **RSI(2)**: 2-period RSI using Wilder's smoothing. Connors's canonical signal. Below threshold = oversold; lower = more oversold.
- **5DMA%** (*verbose*): `(last_close / SMA(5) - 1) × 100`. Negative = price below 5-day average (the canonical Connors target for the bounce). The reversion target is the 5DMA itself; this column shows how far you are from it.
- **50DMA%, 200DMA%** (*verbose*): distance from the longer averages. Both should be positive for a healthy "MR inside uptrend" setup. If 50DMA% goes negative, the trend is wobbling and the MR signal is lower-conviction.
- **Score**: composite 0-100 Reversion Score. All components are *variable* (no constant offsets; the trend filter is a hard gate before scoring). Components: RSI(2) depth (40pts: rsi2=0 → 40, rsi2=threshold → 0), 5DMA pullback magnitude (30pts: dist_5dma=-15% → 30), trend buffer quality (15pts: dist_200dma=+30% → 15, rewards "MR inside a real uptrend, not a borderline one"), frequency uniqueness (15pts: never-fired → 15, freq=8 → 0). Realistic calibration: textbook 🟢 picks land 50-65, 🔵 with buffer + low freq lands 70-85, 90+ is rare. ⚠️ The 2026-05→07 outcome backtest validated **absolute Score ≥ 40** as the strongest entry filter (+1.29%/signal vs +0.33% below) but found the RSI-depth component (40pts) anti-predictive in-sample; the edge lives in the pullback/trend/frequency components. See **Backtested outcomes** #2 and #5.
- **Sig**: entry classifier:
  - **🟢 fresh trigger**: RSI(2) below threshold AND price > 200DMA AND trend healthy. The canonical Connors setup; act today or set a price-improvement limit.
  - **🔵 deep oversold**: RSI(2) below half-threshold (default 2.5). In past data, the deeper the panic, the more reliable the bounce, but also the higher the chance of a real news driver. Verify there's no major catalyst. ⚠️ The 2026-05→07 backtest found 🔵 *underperforms* 🟢 on expectancy (+0.37% vs +0.78%/signal): the extra depth bought a higher win rate but smaller wins and more flat expiries. Deeper ≠ better; see **Backtested outcomes** #5.
  - **🟡 setup forming**: RSI(2) within `[threshold, threshold × 2]`. Approaching trigger; monitor for a further selloff to confirm.
  - **🔴 too late**: RSI(2) > 50 (already bouncing). Don't initiate; part of the move is already gone.
- **Streak**: consecutive prior runs this ticker has appeared. **In MR, high streak is a warning, not a confirmation**; see the "Stuck oversold" section. Backtest-validated: day 1-2 listings ran +0.76-0.93%/signal, day 3+ ran −0.12% (**Backtested outcomes** #3).
- **Freq60d** (*verbose*): number of times this ticker triggered RSI(2) < threshold in the last 60 trading days. Lower = more idiosyncratic event = better signal. Higher = noisy name where this signal fires often and carries less information.
- **Stop**: ATR-based stop level: `last_close - mult × ATR(14)`. Format `$price (-%)`. The persisted-to-CSV stop level used for outcome resolution.
- **Target**: `5DMA × 1.0` (the canonical Connors exit). Format `$price (+%)`. Persisted to CSV alongside Stop for outcome resolution.

The `Win rate` line in the banner aggregates **resolved** outcomes across all history (only WON or LOST count toward the rate; OPEN and EXPIRED stay out of it but count in the resolved total). It becomes meaningful after ~10 resolved picks and reliable after ~30.

### Signal breadth line

**Backtested outcomes** #6 promoted from interpretation doctrine to output: the script tiers the emitted-signal count (the post-vol-collapse "Passed filter" number) against the backtest's in-sample cutoffs: `thin` (< 30: −1.39%/signal, unrescued by Score ≥ 40), `normal` (30-60), `washout` (> 60: +1.29%, and +2.27% on Score ≥ 40). Thin days get a research-only warning in the banner. **The washout framing is regime-conditional**: finding #6 comes from *inside RISK-ON*, so on RISK-ON (or gate-off) days the banner reads "best regime in-sample (RISK-ON tape)", while a washout on a RISK-OFF day flips to a disaster-case warning (broad capitulation in a weak tape = 2008 H2; the regime gate overrides the breadth dial). JSON carries the same data as `signal_breadth: {n_signals, tier, thin_max, washout_min}` so downstream consumers (conviction-funnel, snapback-scan, premarket-brief) can read the dial without re-deriving it; pair it with the `regime` field: the tier is regime-agnostic data, the interpretation isn't. Both cutoffs come from in-sample choices; re-validate quarterly alongside the backtest re-run.

### Recently resolved section

For each pick from the last `--target-window-days × 2` calendar days, the script looks at the price action since the signal date and classifies:

- **WON**: high reached `target` within `--target-window-days` (default 5). Aggregated to one line (count, avg %, avg days, best 3) — a washout week can push 300+ resolved picks through the display window, and itemizing every +1.x% bounce buried the losses. Per-outcome detail stays in JSON `outcomes`.
- **LOST**: low touched `stop` before the target was hit. Itemized worst-first (capped at 10, remainder counted) — each stop-out is worth an individual look.
- **EXPIRED**: neither target nor stop hit within the window. Aggregated to one line (count, avg drift).
- **OPEN**: fewer than `--target-window-days` trading days have passed since signal. Not displayed (still in flight).

Resolution is **deterministic from history.csv plus current price data**: no separate outcome ledger to maintain. Each run re-resolves the relevant prior signals using fresh price data.

### Stuck oversold section

Names with streak ≥ `--persistent-min-streak` (default 3). The interpretation flips vs. the trend-following sister skills:

- In `momentum-scan`: high streak = durable winner = more conviction
- In `base-breakout-scan`: high streak = base maturing = more conviction
- In `mean-reversion-scan`: **high streak = bounce never came = LESS conviction**

The mean-reversion thesis is "panic + healthy trend → quick bounce". When the bounce doesn't happen for 3+ runs, something is sustaining the panic, and that tends to mean a real driver (news, sector rotation, broken trend) the price isn't telling you about yet. The scan flags these names for review, not for buying.

### Single-ticker diagnostic

`--ticker AAPL` produces a multi-stage report:

```
# Single-ticker check: AAPL (Technology / Consumer Electronics)

## Stage 1: Long-term trend (regime + per-name)
✅ Price > 200DMA (+14.2%)
✅ 200DMA slope positive (+1.85% over 20d)
✅ 50DMA > 200DMA

## Stage 2: Short-term oversold metrics
- RSI(2): 1.8
- Distance from 5DMA: -3.2%
- Distance from 50DMA: +4.1%
- Last close: $234.70

## Stage 3: Reversion Score & Signal
- Score: 65/100
- Signal: 🔵 (deep oversold)

## Stage 4: Risk levels (ATR-based, 2.5×)
- 14-day ATR: $2.45 (1.0% of price)
- Stop: $228.40 (-2.7% from spot)
- Target (5DMA): $242.40 (+3.3% from spot)
- Risk/reward at current price: 1.22

## Stage 5: Historical reliability (last 60 trading days)
- Triggers: 3 (last on 2026-04-22)
- Resolved: 3 — 2 won (avg 1.5 days), 1 lost
- Win rate: 67% (n=3 — small sample, treat as directional only)
```

The historical reliability section is unique to single-ticker mode: it scans the last 60 trading days of price data for past instances of this exact setup on this exact ticker and resolves their outcomes. That builds confidence (or skepticism) about applying the system to this specific name.

## Backtested outcomes (2026-05-14 → 2026-07-29 sample)

`scripts/backtest_outcomes.py` replays `state/history.csv` with the exact convention the running win-rate stat uses (entry at the signal-day close, limit at the 5DMA target, stop at the ATR level, 5-day window) and stratifies by everything recorded at signal time. 1,584 resolved signals over a mostly RISK-ON tape; re-run quarterly. Findings, strongest first; full evidence, magnitudes, and caveats in `references/backtest-findings.md` (sections throughout this file reference the numbering below):

1. **The win rate is real but not the point; expectancy is**: 92% of decisive signals win yet the system nets +0.68%/signal (stops and flat expiries give most of it back); judge every filter by expectancy. Next-open entry trims it only to +0.61%, so the edge is not an entry-timing artifact.
2. **Score ≥ 40 plus fresh-or-second-day listing is the validated entry filter**: +1.83%/signal (95% win rate), ~3× the unfiltered baseline; it's the *absolute* score that matters, not the daily rank.
3. **"Stuck oversold" is data, not doctrine**: expectancy collapses at the 3rd consecutive listing (−0.12% vs +0.76–0.93% for day 1-2); the `--persistent-min-streak 3` default sits on the cliff.
4. **Pullback depth is bimodal**: ≤−7% washouts pay +2.28%; the −7..−4% middle zone is the trap (−0.22%, worst pocket); shallow dips are the one pocket where the canonical exit costs money.
5. **Deeper RSI(2) is NOT better: 🔵 underperforms 🟢** (+0.37% vs +0.78%); the Score's RSI-depth component is dead weight in-sample, and the pullback/trend/frequency components carry the edge.
6. **Signal breadth is a regime dial**: <30 emitted signals: −1.39%/signal, unrescued by Score ≥ 40; >60 (washout): +1.29%, and +2.27% on Score ≥ 40. Idiosyncratic oversold doesn't mean-revert.
7. **The 5-day window is the right default**: the bounce edge is spent within a week; 10-day windows only add the losses back.
8. **Frequency penalty confirmed**: Freq60d 3-5: +0.13% vs +0.84% for 1-2; the sector skew in-sample is tape-driven, not structural.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass: apply judgment rather than reciting the principles below.

Relay the script's markdown output **in full — every row of the top-N table and every section**; don't truncate to save space. Then write the interpretation for a reader with **no finance background**, in the conversation's language, translating each term the moment you use it — "mean reversion" is "strong stocks that just took a short, sharp hit and tend to snap back"; "RSI(2) 1.8" is "a 0-100 panic meter for the last two days; under 5 is heavily oversold"; the ⭐️ pocket is "the only slice with backtest-proven odds: high score AND on the list no more than 2 days"; "Win rate 94%" needs its caveat in the same breath (the high rate is structural — the KPI is the per-signal expectancy, not the rate). Lead with the breadth tier and the ⭐️ pocket, flag stuck-oversold names as warnings not bargains, and close with what to do — usually "nothing; if researching, start and stop at the ⭐️ names".

1. **Lead with the regime banner.** Mean reversion has its worst regime in confirmed bear markets; this is non-negotiable. If RISK-OFF, the recommendation should be "wait" or "paper-trade only", not "here's a name to buy". Even in `--regime-gate warn` mode where the table still prints, frame the names as research-only when SPY is below a falling 200DMA.

2. **Lead with the running win rate, calibrated against ~90%, not 70%.** Once history has ≥ 10 resolved picks, the `Win rate` line tells the user whether this system has been working *recently in this market*. Note the resolver's conventions (intraday-high touch, 2.5-ATR-wide stop) run structurally hot: the 2026-05→07 backtest measured 92% across 983 decisive signals while netting only +0.68%/signal: each stop-out gives back ~2.7 average wins, and the 38% expired bucket drifts −2.4% besides. So read 85% as "below par", not "great", and never quote the win rate without the expectancy framing (**Backtested outcomes** #1).

3. **Filter on Score ≥ 40 and day-of-spell first; read Sig as context, not ranking.** The backtest's validated entry filter is *absolute Score ≥ 40 on a name in its 1st-2nd consecutive listing*: +1.83%/signal vs +0.68% baseline (**Backtested outcomes** #2). Within that, the Sig tiers:
   - **🟢 fresh trigger** is the meat of the system, and in-sample the best-paying tier (+0.78%/signal).
   - **🔵 deep oversold** is *not* the highest-EV bucket despite doctrine: +0.37%/signal, smaller wins, more flat expiries (**Backtested outcomes** #5). RSI(2) < 2.5 often means a real news driver, so the catalyst check matters even more here.
   - **🟡 setup forming** is research, not action: set a limit order at a price that would pull RSI(2) below threshold.
   - **🔴 too late** is "missed the bus this time"; note it for the next occurrence.

4. **Frequency is a first-class tiebreaker.** A name with `Freq60d = 1` (the only RSI(2) panic in the last 3 months) is a much higher-conviction signal than a name with `Freq60d = 8` (a noisy stock where the signal fires almost weekly and means little). Rank two names with similar Scores but different Freq60d by Freq60d (lower = better) before any other tiebreaker.

5. **The Stuck oversold section is more important than the top-N.** Names appearing here have failed the mean-reversion thesis; the bounce didn't come. The backtest puts numbers on it: 3rd+ consecutive listings ran −0.12%/signal vs +0.76-0.93% for day 1-2, and the worst single pocket in the whole sample was stuck names in the −7..−4% pullback zone at −1.79% (**Backtested outcomes** #3, #4). The natural next step is to investigate *why*: news search, earnings calendar, sector ETF check. That digging often surfaces information the broader market hasn't priced yet. **Never recommend buying these.** The scan flags them for analysis, not action.

6. **Stop discipline is non-negotiable for this style.** The 70% win rate only turns into profit if you cap the losses. Connors-style MR is asymmetric in the wrong direction: many small wins, occasional larger losses. Without the stop, one breakdown trade can wipe out 5-10 winners. The Stop column is the hard floor; Target is the take-profit. Both persist to history so the win-rate stat reflects realistic execution.

7. **Sector clustering means less here than in momentum; total breadth means a lot.** A momentum scan with 16/30 Tech tells you AI infra is the cluster trade. A mean-reversion scan with 8/30 Tech in the same week probably means the Nasdaq had a bad day: a market-wide event, not a sector edge. And market-wide is what you want: the backtest found signals on broad-washout days (>60 emitted) ran +1.29%/signal while signals on quiet days (<30) ran **−1.39%, unrescued by high scores** (**Backtested outcomes** #6). The banner's **Signal breadth** line now tiers this for you (thin / normal / washout); lead with it: a THIN banner overrides everything below it, including high-score names. Diversification still applies: pick each name from a different sector where possible, so the stops don't all fire together on one bad SPY day. **Don't confuse this with the dashboard's per-sector result panel** — that one is longitudinal (how a sector's signals have actually resolved over the whole ledger), this one is same-day cross-section (how concentrated today's list is). They answer different questions, and the same-day reading is the weaker of the two: measured across the ledger, a day's sector concentration has no monotonic relationship to outcome (mid-concentration days ran ahead of the most concentrated ones), which is exactly why the breadth tier, not the sector mix, is what the banner leads with.

8. **Never recommend specific buys, least of all for this style.** The 70% win rate is a population statistic; any individual trade is a coin flip with 70% bias. Frame results as "names where the Connors RSI(2) setup triggered today, with entry/stop/target levels", not "buy this". Flag that mean-reversion strategies produce spectacular tail-risk events when you misread a fundamental selloff as panic; the 2008 H2 case study is the canonical lesson.

## State files

- `state/history.csv`: one snapshot per US market day (America/New_York) × **every ticker that passed the filter that day** (all emitted signals, not only the displayed top-N; the row count doubles as the signal-breadth record, and Streak counts *any* filter-passing appearance by design, so it reads "consecutive days oversold", which is what Stuck oversold needs). Columns: `run_id, run_date, ticker, rank, score_rank, score, rsi2, dist_5dma_pct, dist_50dma_pct, dist_200dma_pct, last_close, target_price, stop_price, signal, freq_60d`. Re-running the same ET day overwrites that day's rows. Writes are atomic (.tmp + rename). The `target_price` and `stop_price` columns are the load-bearing fields for outcome resolution; without them the script can't compute win-rate stats.
- `state/outcomes.csv`: the outcomes ledger — one row per **resolved** past signal (`run_id, ticker, outcome, days_to_resolve, result_pct`; OPEN signals stay out until they resolve). Written by every scan via keyed upsert of the resolver's ~15-day lookback window, so it accumulates the full outcome history that any single run can't see; `(run_id, ticker)` joins back into `history.csv` for everything known at signal time. Tracked in git: only regenerable (`--backfill-outcomes`) while yfinance still serves the price window (~13 months) and the ticker still trades.
- `state/history.html`: self-contained HTML dashboard rendered from `history.csv` + `outcomes.csv` + `sectors.json` by `scripts/render_history_html.py`: KPI row, a per-day signal-breadth column chart stacked by eventual outcome (with the thin/washout cutoffs drawn in), a date × ticker outcome grid (⭐ pocket days dotted; long unbroken rows = stuck oversold), a ⭐-pocket-vs-rest running-expectancy chart against the backtest's in-sample references, a **per-sector realized-result panel** (one bar per sector = avg %/resolved signal, with its 95% interval drawn above it and a dashed all-signals average as the reference; a sector whose interval reaches that line is not distinguishable from the board and is drawn back to 42% opacity — most currently aren't), a sortable roster, and an EN/简中/繁中/日/한 language menu. **Deliberately not momentum-scan's chart set**: no rank trajectories (MR daily ranks are noise; outcomes are the story). No external assets or network; regenerable at any time, so it's gitignored. Re-render after a scan when the user wants the visual view of the outcome history.
- `state/universe.txt`: cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json`: per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker.

Storage growth: one row per emitted signal: ~20 rows on quiet days to 120+ on washout days, × ~180 bytes ≈ 4-22 KB/day. A year of daily runs ≈ 2-5 MB. Negligible.

## Cadence

Cadence-agnostic by design. One snapshot per US market day (ET); intraday re-runs refresh the snapshot rather than appending. Runs on weekends or NYSE holidays auto-skip from history.

**Recommended cadence**: daily, after the close. Mean-reversion is a short-time-frame signal: RSI(2) moves a lot day-to-day, and the 5-day target window means a signal goes stale within a trading week. Weekly cadence misses 80% of the signals; monthly is useless for this style. If you want lower-effort monitoring, set up a `cron` or `launchd` job to run after the 4pm ET close.

## Known limitations

- **Survivorship bias**: the universe is current US large caps; delisted names are absent. The historical win rate can't account for catastrophic past events that wiped a name from the universe.
- **Pre-cost**: no transaction costs, slippage, or taxes modeled. Mean-reversion's many small trades make it more cost-sensitive than momentum: 0.5% round-trip on a 1.5% target gain is a 33% haircut to expected return. Real execution shaves more off this style than any other.
- **Connors RSI(2) is a published, well-known system**: traders have arbitraged away some of its edge since the original 2008 publication. Modern win rates on liquid US large caps run 65-75% rather than the 75-80% in the original studies. The 5DMA target is conservative enough that the system still profits, but calibrate expectations to "good", not "great".
- **`MaxDD%` too small to be natural + RSI(2) < 5 is a buyout fingerprint**: same vol-collapse blind spot as the sister skills. The default `--vol-collapse-ratio 0.2` catches the canonical signature, but a deal announced in the last ~6 weeks (gap day in second half of the 3-month vol window) can still leak through. Cross-check via `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun for a pending merger. The `--ticker` mode also surfaces a vol-collapse warning at the top when triggered.
- **The 5DMA target is a moving goalpost.** As price drops, the 5DMA drops too: the target measured at signal time uses *today's* 5DMA, but the price needed to hit "above 5DMA" 3 days later may differ. We persist `target_price` at signal time and resolve against that fixed value, the cleanest definition for out-of-sample win-rate stats though stricter than "first close above 5DMA" using the live 5DMA.
- **Running win rate ignores fees, slippage, and execution gaps.** A "WON" trade where price spiked through the target intraday and closed below it still counts as WON in our resolver (we use the high). In live trading without a take-profit limit order sitting right at the target, you might miss the wick. Treat the historical rate as an upper bound on what you'd realize.
- **Regime gate uses 200DMA + slope; doesn't catch fast regime flips.** A 1-week selloff (Aug 2024, March 2020) blows through the 200DMA before the slope flips. Mean-reversion signals fired into the start of such a crash are the canonical disaster case.
- **Trend Template is lite by design.** We only check `price > 200DMA`, `200DMA slope positive`, and `50DMA > 200DMA`, three of Minervini's 8 criteria. The omitted ones (RS Rating, distance from 52w low/high, etc.) would over-restrict a mean-reversion universe that benefits from including names that are out of favor for now. If you want a stricter trend filter, run `base-breakout-scan --ticker NAME` first to see if it passes the full Trend Template.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)**: only matters if Yahoo's response stops including the `total` field (schema drift). Same backstop as the sister skills.
- **History csv schema must include `target_price` and `stop_price`**: outcome resolution depends on them. If an old history file from an early version lacks these columns, the resolver skips those rows from win-rate stats (no crash, no stat contribution). Run `--clear-history` to start fresh if you want clean stats.

## Tests

```bash
cd <SKILL_DIR>/scripts && uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  --with 'numpy>=1.24,<3' --with pytest pytest -q
```

Pure-logic tests (no network) cover the signal-breadth dial, the Sig classifier, the Reversion Score components, Wilder RSI, the lite trend filter, trigger-frequency crossing counts, the vol-collapse halves + exclusion filter (low-vol floor, re-ranking), the NYSE trading-day guard, streak/persistence enrichment, outcome resolution with WON / LOST / EXPIRED / OPEN plus the win-rate aggregator, and the outcomes ledger (full-history resolve + keyed upsert) (`test_classify.py`), the backtester's history/spell parsing and canonical / gap-aware / next-open fill conventions (`test_backtest_outcomes.py`), and the HTML renderer's payload building (streak/pocket flags, outcome categories incl. OPEN-vs-UNRESOLVED, breadth stacks, cumulative expectancy lines, windowing, and the per-sector panel's means / 95% intervals / thin-sector folding / untagged exclusion) plus drift guards pinning its mirrored constants to scan.py (`test_render_html.py`).
