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
# state/history.html (breadth × outcome columns, outcome grid, 📝 paper-track
# vs rest expectancy, per-sector result panel, roster table). Stdlib-only, no
# network, no uv needed:
python <SKILL_DIR>/scripts/render_history_html.py   # --days 60 --out <path>

# Matched-horizon index reference for the 📝 paper-track panel (each resolved signal
# vs buying SPY/QQQ the same day and holding the same number of sessions). The
# scan does this after each ledger write; run it by hand after --no-benchmark.
... python <SKILL_DIR>/scripts/compute_benchmark.py   # --refresh-prices

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
# stratified by signal attributes, plus the exit-horizon tables (every
# holding period paired against the live rule on one fixed trade set —
# this is where "should I cut it early?" gets answered, NOT from the
# ledger's days_to_resolve). See "Backtested outcomes" section.
... python <SKILL_DIR>/scripts/backtest_outcomes.py
# --target-window resets the live rule the exit-horizon tables compare to
... python <SKILL_DIR>/scripts/backtest_outcomes.py --target-window 10
# Realistic execution variant: enter at the first open after the scan
# published instead of the signal-day close (skips signals that gap past
# target/stop before the fill)
... python <SKILL_DIR>/scripts/backtest_outcomes.py --entry next-open

# Multi-year replay: rebuild the pick list for every session since
# --eval-start on a point-in-time universe (no survivorship from today's
# large caps) and score signals and portfolios against SPY. The first run
# downloads ~3000 tickers x the span (a few minutes; cached after).
# --universe-file ../state/universe.txt measures survivorship.
... python <SKILL_DIR>/scripts/replay_signals.py --eval-start 2021-01-04
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
| `--persistent-min-streak` | 3 | Streak threshold for the **Stuck oversold** section. **In mean reversion, a long streak is a yellow flag, not green**: the bounce hasn't materialized after multiple runs, which suggests something structural rather than a noise overreaction. Default 3 surfaces these for review. The live sample put a cliff at the 3rd consecutive listing; the six-year replay didn't (day 3 ran the same as day 1), so this is a review threshold, not a measured edge. |
| `--target-window-days` | 5 | Number of trading days within which the bounce-to-5DMA must occur for an outcome to count as WON. Connors's canonical exit is "first close ≥ 5DMA"; we use intraday high to be charitable. After this many days without target or stop hit, outcome is EXPIRED. Tested in both directions on the live sample: shortening to 1-2 days and stretching to 7-10 both cost expectancy on identical trades (live-sample finding #7; the replay didn't re-test exits), so don't move it without re-running that table. |

## Output shape

A regime banner (including a **Signal breadth** tier line; see below), sector breakdown, the **Sig** cohort strip (🟢/🔵/🟡/🔴 counts across the top-N), an optional **Excluded by vol-collapse filter** section, the **📝 Paper-track** section (Score ≥ 70, the one stratum the six-year replay kept, tracked on paper; always printed when there are picks, because it doubles as the daily reminder that nothing else on the list has a measured edge over SPY), the main top-N table, and 2-3 discovery sections (recently-resolved picks with running win rate, stuck-oversold leaders). The script skips sections with zero entries. The table is the **slim** default (2026-07-31 redesign, decision columns only); `--verbose` restores the 5DMA%/50DMA%/200DMA%/Freq60d diagnostics, and JSON always carries every field (including `paper_track` per pick). Sample (illustrative; picks change daily):

```
# Mean-reversion scan — 2026-05-14 16:32 UTC

**Params**: rsi2_threshold=5.0, target_window=5d, mcap>5e+09
**Universe**: 1035 tickers · **Passed filter**: 18 (vol-collapse: 0 excluded) · **Prior runs**: 12
**Data**: daily bars through 2026-05-11
**Signal breadth**: 18 → **THIN** (<30). ⚠️ Isolated oversold: in the 2021→2026 replay, signals on days like this trailed SPY over the same days. Treat today's list as research-only.
**Regime**: SPY 742.3 vs 200DMA 672.2 (+10.4%) · 50DMA > 200DMA · 200DMA slope (20d): +1.50% · Breadth: 58% > 200DMA → **RISK-ON**
**Win rate** (last 30d, 47 resolved): 73% (34W / 13L) · avg days to target: 1.9
**Sectors**: Tech 5 · Health 4 · Financ 3 · Cons Cyc 2 · Energy 2 · Other 2
**Sig**: 🟢8 🔵3 🟡7 🔴0

## 📝 Paper-track — Score ≥ 70 (1)
_The one stratum the 2021→2026 replay kept: +1.06%/signal over SPY, but −2.7% in 2022. Hot names after a violent drop; tracked on paper, not a buy list._
- **AAPL** (#1): score 72, day 2, RSI(2) 1.8, 🔵

## Top 18

| # | Ticker | Sector | RSI(2) | Score | Sig | Streak | Stop | Target |
|---|---|---|---|---|---|---|---|---|
| 1 | 📝 **AAPL** | Tech | 1.8 | 72 | 🔵 | 2 | $228.40 (-2.7%) | $237.55 (+1.2%) |
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

Column meanings (columns marked *verbose* print only with `--verbose`; JSON always carries them; a 📝 ticker prefix marks paper-track membership, same names as the 📝 section):

- **RSI(2)**: 2-period RSI using Wilder's smoothing. Connors's canonical signal. Below threshold = oversold; lower = more oversold.
- **5DMA%** (*verbose*): `(last_close / SMA(5) - 1) × 100`. Negative = price below 5-day average (the canonical Connors target for the bounce). The reversion target is the 5DMA itself; this column shows how far you are from it.
- **50DMA%, 200DMA%** (*verbose*): distance from the longer averages. Both should be positive for a healthy "MR inside uptrend" setup. If 50DMA% goes negative, the trend is wobbling and the MR signal is lower-conviction.
- **Score**: composite 0-100 Reversion Score. All components are *variable* (no constant offsets; the trend filter is a hard gate before scoring). Components: RSI(2) depth (40pts: rsi2=0 → 40, rsi2=threshold → 0), 5DMA pullback magnitude (30pts: dist_5dma=-15% → 30), trend buffer quality (15pts: dist_200dma=+30% → 15, rewards "MR inside a real uptrend, not a borderline one"), frequency uniqueness (15pts: never-fired → 15, freq=8 → 0). Realistic calibration: textbook 🟢 picks land 50-65, 🔵 with buffer + low freq lands 70-85, 90+ is rare. ⚠️ In the six-year replay Score below 70 carries no edge over SPY in any band; only **Score ≥ 70** does (+1.06%/signal, failing in 2022), which in practice means a name far above its 200DMA (≥ 50%) that just dropped hard. The 2026-05→07 sample's "Score ≥ 40" filter didn't replicate. See **Backtested outcomes**.
- **Sig**: entry classifier:
  - **🟢 fresh trigger**: RSI(2) below threshold AND price > 200DMA AND trend healthy. The canonical Connors setup; act today or set a price-improvement limit.
  - **🔵 deep oversold**: RSI(2) below half-threshold (default 2.5). In past data, the deeper the panic, the more reliable the bounce, but also the higher the chance of a real news driver. Verify there's no major catalyst. The 2026-05→07 sample had 🔵 *underperforming* 🟢 (+0.37% vs +0.78%/signal); over six years the two tiers are the same against SPY (−0.05% vs −0.10%). Depth alone doesn't rank.
  - **🟡 setup forming**: RSI(2) within `[threshold, threshold × 2]`. Approaching trigger; monitor for a further selloff to confirm.
  - **🔴 too late**: RSI(2) > 50 (already bouncing). Don't initiate; part of the move is already gone.
- **Streak**: consecutive prior runs this ticker has appeared. **In MR, high streak is a warning, not a confirmation**; see the "Stuck oversold" section. The live sample's day-3 cliff (−0.12% vs +0.76-0.93% for days 1-2) didn't replicate over six years, so read streak as a prompt to check the news, not as a measured penalty.
- **Freq60d** (*verbose*): number of times this ticker triggered RSI(2) < threshold in the last 60 trading days. Lower = more idiosyncratic event = better signal. Higher = noisy name where this signal fires often and carries less information.
- **Stop**: ATR-based stop level: `last_close - mult × ATR(14)`. Format `$price (-%)`. The persisted-to-CSV stop level used for outcome resolution.
- **Target**: `5DMA × 1.0` (the canonical Connors exit). Format `$price (+%)`. Persisted to CSV alongside Stop for outcome resolution.

The `Win rate` line in the banner aggregates **resolved** outcomes across all history (only WON or LOST count toward the rate; OPEN and EXPIRED stay out of it but count in the resolved total). It becomes meaningful after ~10 resolved picks and reliable after ~30.

### Signal breadth line

The script tiers the emitted-signal count (the post-vol-collapse "Passed filter" number) against cutoffs chosen on the 2026-05→07 sample: `thin` (< 30), `normal` (30-60), `washout` (> 60). That sample made the dial look strong (thin −1.39%/signal, washout +1.29%); the six-year replay shrank it to a tilt against SPY (thin −0.12%, t −1.6; washout +0.10%, t +1.3), so thin days still get the research-only warning and washout days get "the bounce is mostly the index's". **The washout framing is regime-conditional**: a washout on a RISK-OFF day prints a disaster-case warning instead (broad capitulation in a weak tape = 2008 H2; the regime gate overrides the breadth dial). JSON carries the same data as `signal_breadth: {n_signals, tier, thin_max, washout_min}` so downstream consumers (conviction-funnel, snapback-scan, premarket-brief) can read the dial without re-deriving it; pair it with the `regime` field: the tier is regime-agnostic data, the interpretation isn't. Both cutoffs come from in-sample choices; re-check them when the replay is re-run.

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

## Backtested outcomes

Two tests, and they disagree; the longer one decides. Full evidence and caveats in `references/backtest-findings.md`.

### Six-year replay (2021-01 → 2026-09): the verdict

`scripts/replay_signals.py` rebuilds the pick list for every session from raw prices on a point-in-time universe (today's US equities above $500M, filtered each day by market cap at the time, so names that shrank since stay in) and trades each signal by the scan's own convention. Over the 56 run-days both cover it matches the live ledger: pick lists 92% the same, outcomes 99% the same, +0.98%/signal both ways. 57k signals. The measure is the excess over SPY across the same holding span at next-open fills (what an evening scan can execute), averaged within each signal day first and then across days: a washout day lists 100+ names that bounce together, and a plain per-signal average overweights it.

1. **The bounce is the market's.** Every signal averaged +0.31% with an 84% win rate, and −0.04% against SPY over the same days (t −0.8). The old ⭐ pocket (Score ≥ 40 on a 1st-2nd-day listing, +1.83% in the live sample) ran −0.14% (t −1.2), negative in 2021 and 2025.
2. **Score ≥ 70 is the one stratum that survived, and it isn't safe**: +1.06%/signal over SPY (t +2.7), positive 2023-26 (+2.3 / +0.5 / +2.2 / +2.0) but −2.7% in 2022 and flat in 2021. Bands below 70 show nothing; the jump comes from names ≥ 50% above their 200DMA after a violent drop, i.e. buying the first crack in the market's hottest names. That pays while the theme lasts and failed when 2022's theme (energy) broke in June. As a portfolio (10 slots, one position per ticker, idle slots in SPY, next-open, 10bp): CAGR 29.5% vs SPY 15.1%, but 2022 −31% vs −18% and max drawdown −37% vs −25% (alpha t 1.8). Hence **📝 paper-track**, not a buy list.
3. **The live sample's other findings don't replicate**: no 3rd-listing-day cliff, 🔵 no worse than 🟢, the ≤ −7% washout premium is market beta (−0.12% vs SPY), the frequency penalty reverses. Breadth survives only as a tilt (thin days −0.12%, t −1.6; washout days +0.10%, t +1.3).
4. **Bias**: restricting the pool to today's large caps barely moves the result (survivorship is small for this scan). Today's share count admits heavy issuers to the universe early; dropping them takes Score ≥ 70 from +1.76% to +1.42% over 2023-26, and since dropping future issuers is itself look-ahead, the truth sits between. The pool can't hold 2021-22's busted SPACs and meme names, so the real 2022 was worse.

### Live-history backtest (2026-05-14 → 2026-07-29): in-sample, superseded

`scripts/backtest_outcomes.py` replays `state/history.csv` (1,584 resolved signals, one mostly RISK-ON tape). Its findings, each with the replay's verdict:

1. Win rate is structural, expectancy is the KPI: 92% of decisive signals won while netting +0.68%/signal (+0.95% after the 2026-10-05 ledger fix). ✓ The replay agrees on the structure (84% / +0.31%), and adds that the expectancy is the index's.
2. Score ≥ 40 on a 1st-2nd-day listing, +1.83%/signal. ✗ −0.14% vs SPY over six years.
3. Expectancy collapses at the 3rd consecutive listing (−0.12%). ✗ Day 3 ran the same as day 1.
4. Pullback depth bimodal: ≤ −7% +2.28%, −7..−4% the trap. ✗ Monotonic in absolute terms, nothing against SPY.
5. 🔵 underperforms 🟢 (+0.37% vs +0.78%). ✗ No difference.
6. Breadth dial: thin −1.39%, washout +1.29%. ◐ Same direction, a fraction of the size.
7. Exit horizon: target, stop, or the 5th close; cutting at day 1-2 or stretching to 7-10 both cost money on identical trades. Not re-tested by the replay; still the rule.
8. Frequency penalty (Freq60d 3-5 +0.13% vs +0.84% for 1-2). ✗ Reversed.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass: apply judgment rather than reciting the principles below.

Relay the script's markdown output **in full — every row of the top-N table and every section**; don't truncate to save space. Then write the interpretation for a reader with **no finance background**, in the conversation's language, translating each term the moment you use it — "mean reversion" is "strong stocks that just took a short, sharp hit and tend to snap back"; "RSI(2) 1.8" is "a 0-100 panic meter for the last two days; under 5 is heavily oversold"; the ⭐️ pocket is "the only slice with backtest-proven odds: high score AND on the list no more than 2 days"; "Win rate 94%" needs its caveat in the same breath (the high rate is structural — the KPI is the per-signal expectancy, not the rate). Say once, plainly, what the six-year replay found: these bounces have paid about what holding SPY over the same days paid, so the list is a watchlist of strong stocks that just got hit, not a source of extra return. Lead with the breadth tier, then the 📝 paper-track names ("stocks that ran far above their long-term average and just dropped hard; the one slice that beat the index over six years, except in 2022's crash, so we are only tracking it on paper"), flag stuck-oversold names as warnings not bargains, and close with what to do — usually "nothing".

Before the numbered points, check the `**Data**` line: it names the session the numbers reflect. A `⚠️ Stale data` warning means Yahoo hadn't published the last session's daily bars and the intraday rebuild failed too, so every signal, target and stop is a session old; for a 1–5 day trade that is a large share of the holding period. Open the summary with it, in plain words ("these numbers are from Thursday's close, not Friday's"). A `rebuilt from 30m intraday bars` note is the workaround succeeding (closes within ~0.1% of the official ones) and needs no mention.

1. **Lead with the regime banner.** Mean reversion has its worst regime in confirmed bear markets; this is non-negotiable. If RISK-OFF, the recommendation should be "wait" or "paper-trade only", not "here's a name to buy". Even in `--regime-gate warn` mode where the table still prints, frame the names as research-only when SPY is below a falling 200DMA.

2. **Lead with the running win rate, calibrated against ~90%, not 70%.** Once history has ≥ 10 resolved picks, the `Win rate` line tells the user whether this system has been working *recently in this market*. Note the resolver's conventions (intraday-high touch, 2.5-ATR-wide stop) run structurally hot: the 2026-05→07 backtest measured 92% across 983 decisive signals while netting only +0.68%/signal: each stop-out gives back ~2.7 average wins, and the 38% expired bucket drifts −2.4% besides. So read 85% as "below par", not "great", and never quote the win rate without the expectancy framing, and the expectancy without the index's over the same days (**Backtested outcomes**, replay #1).

3. **No filter on this list has a measured edge over SPY; say so before any name.** The 2026-05→07 sample's Score ≥ 40 / day ≤ 2 filter didn't survive six years (**Backtested outcomes**, replay #1). The one stratum that did, 📝 Score ≥ 70, lost 2.7%/signal in 2022, so it is paper-tracked: report its names and the dashboard's running paper-track line, never size them. Read the Sig tiers as description:
   - **🟢 fresh trigger** is the canonical Connors setup.
   - **🔵 deep oversold**: RSI(2) < 2.5 often means a real news driver, so the catalyst check matters even more here. Over six years it ran the same as 🟢.
   - **🟡 setup forming** is research, not action: set a limit order at a price that would pull RSI(2) below threshold.
   - **🔴 too late** is "missed the bus this time"; note it for the next occurrence.

4. **Frequency is a first-class tiebreaker.** A name with `Freq60d = 1` (the only RSI(2) panic in the last 3 months) is a much higher-conviction signal than a name with `Freq60d = 8` (a noisy stock where the signal fires almost weekly and means little). Rank two names with similar Scores but different Freq60d by Freq60d (lower = better) before any other tiebreaker.

5. **The Stuck oversold section is more important than the top-N.** Names appearing here have failed the mean-reversion thesis; the bounce didn't come. The live sample's day-3 cliff didn't replicate over six years, so this is a research prompt rather than a measured penalty, and the research is the point: the natural next step is to investigate *why*: news search, earnings calendar, sector ETF check. That digging often surfaces information the broader market hasn't priced yet. **Never recommend buying these.** The scan flags them for analysis, not action.

6. **Stop discipline is non-negotiable for this style.** The 70% win rate only turns into profit if you cap the losses. Connors-style MR is asymmetric in the wrong direction: many small wins, occasional larger losses. Without the stop, one breakdown trade can wipe out 5-10 winners. The Stop column is the hard floor; Target is the take-profit. Both persist to history so the win-rate stat reflects realistic execution.

   **Say the exit rule as three levels, never as a holding period.** "5 days" is a deadline, not a plan: whichever of target, stop, or the 5th session's close comes first. In-sample, 57% of trades left at the target (avg +3.71%), 5% at the stop (−9.98%), 38% at the deadline (−2.44% drift), average time in the trade **3.35 sessions** — 23% are out on day 1. The temptation the expired bucket creates is to cut the dead ones early; the live sample's exit-horizon test (**Backtested outcomes**, live #7) measures that and it loses money, because the loss is already taken by day 2 and a quarter of the winners land on days 3-5. Two traps to name if the user raises them: the ledger's `days_to_resolve` cannot be used to derive an exit rule (bucketing trades by how long they took conditions on the future — it says holding past day 2 averages −1.5%, and the paired test says the opposite), and a shorter rule's higher return *per day held* (2-day: +0.27%/day vs +0.20% at 5) is only real capital efficiency if the freed money has another signal to take. Mid-trade fiddling doesn't improve the P&L, and over six years no entry filter below Score 70 did either.

7. **Sector clustering means less here than in momentum; total breadth means a lot.** A momentum scan with 16/30 Tech tells you AI infra is the cluster trade. A mean-reversion scan with 8/30 Tech in the same week probably means the Nasdaq had a bad day: a market-wide event, not a sector edge. The banner's **Signal breadth** line tiers this (thin / normal / washout); lead with it. Over six years it is a tilt, not an edge: quiet-day (<30) signals trailed SPY by 0.12%/signal, washout-day (>60) signals beat it by 0.10% (**Backtested outcomes**, replay #3). A THIN banner still overrides the high-score names below it: an oversold name when nothing else is oversold usually has its own bad news. Diversification still applies: pick each name from a different sector where possible, so the stops don't all fire together on one bad SPY day. **Don't confuse this with the dashboard's per-sector result panel** — that one is longitudinal (how a sector's signals have actually resolved over the whole ledger), this one is same-day cross-section (how concentrated today's list is). They answer different questions, and the same-day reading is the weaker of the two: measured across the ledger, a day's sector concentration has no monotonic relationship to outcome (mid-concentration days ran ahead of the most concentrated ones), which is exactly why the breadth tier, not the sector mix, is what the banner leads with.

8. **Never recommend specific buys, least of all for this style.** The 70% win rate is a population statistic; any individual trade is a coin flip with 70% bias. Frame results as "names where the Connors RSI(2) setup triggered today, with entry/stop/target levels", not "buy this". Flag that mean-reversion strategies produce spectacular tail-risk events when you misread a fundamental selloff as panic; the 2008 H2 case study is the canonical lesson.

## State files

- `state/history.csv`: one snapshot per US market day (America/New_York) × **every ticker that passed the filter that day** (all emitted signals, not only the displayed top-N; the row count doubles as the signal-breadth record, and Streak counts *any* filter-passing appearance by design, so it reads "consecutive days oversold", which is what Stuck oversold needs). Columns: `run_id, run_date, ticker, rank, score_rank, score, rsi2, dist_5dma_pct, dist_50dma_pct, dist_200dma_pct, last_close, target_price, stop_price, signal, freq_60d, data_asof`. `data_asof` (added 2026-10-05) is the session the run's daily bars reached, as `YYYYMMDD`. Since 2026-09-02 Yahoo has often not published the just-closed session's daily bar by the 20:00 ET scan (its row comes back with a NaN close); the scan now rebuilds that session from 30-minute intraday bars and says so on the `**Data**` line, and a run whose rebuild fails too still saves, with `data_asof` older than `run_id`. Rows from 2026-09-02 to 10-02 predate the column and 84% of them carry the prior session's close; the resolvers find their signal bar from `dist_5dma_pct` instead (see `outcomes.csv`). Re-running the same ET day overwrites that day's rows. Writes are atomic (.tmp + rename). The `target_price` and `stop_price` columns are the load-bearing fields for outcome resolution; without them the script can't compute win-rate stats.
- `state/outcomes.csv`: the outcomes ledger — one row per **resolved** past signal (`run_id, ticker, outcome, days_to_resolve, result_pct`; OPEN signals stay out until they resolve). Written by every scan via keyed upsert of the resolver's ~15-day lookback window, so it accumulates the full outcome history that any single run can't see; `(run_id, ticker)` joins back into `history.csv` for everything known at signal time. Each signal enters at its signal bar's close (the bar `data_asof` names, or for older rows the run-day bar or the one before it, whichever matches the recorded `dist_5dma_pct`) and is watched from the next session, with target and stop rescaled into the price series' current units so later dividends don't move them. Until 2026-10-05 the watch was keyed off the run's UTC timestamp, which for the 00:00 UTC nightly job landed after the next session's bar and started every watch a session late; the whole ledger was re-resolved that day (pre-09-02 expectancy +0.68% → +0.95%/signal, now matching `backtest_outcomes.py`). Tracked in git: only regenerable (`--backfill-outcomes`) while yfinance still serves the price window (~13 months) and the ticker still trades.
- `state/benchmark.json`: matched-horizon index returns written by `scripts/compute_benchmark.py` — for every resolved signal, what SPY and QQQ paid from the same signal-day close over the same number of sessions the signal took, averaged per signal day. Feeds the 📝 paper-track panel's two dotted reference lines, which answer whether the expectancy is an edge or just the market being up over those windows (as of 2026-10-05: +0.36%/signal for SPY and +0.60% for QQQ over the ledger). Matched horizon rather than a buy-and-hold curve on purpose: the panel's axis is %/signal, and an equity curve there would be two measures sharing one scale. `scan.py` rebuilds it after each ledger write (skip with `--no-benchmark`; failures warn and keep the previous file). Tracked in git, written one value per line so the daily rewrite reads as an append.
- `state/history.html`: self-contained HTML dashboard rendered from `history.csv` + `outcomes.csv` + `sectors.json` (+ `benchmark.json` when present) by `scripts/render_history_html.py`: KPI row, a per-day signal-breadth column chart stacked by eventual outcome (with the thin/washout cutoffs drawn in), a date × ticker outcome grid (📝 paper-track days dotted; long unbroken rows = stuck oversold), a 📝-paper-track-vs-rest running-expectancy chart against the six-year replay's references (+2.49% for Score ≥ 70, +0.29% for the rest, close fills like the ledger) and the matched-horizon SPY / QQQ lines, a **per-sector realized-result panel** (one bar per sector = avg %/resolved signal, with its 95% interval drawn above it and a dashed all-signals average as the reference; a sector whose interval reaches that line is not distinguishable from the board and is drawn back to 42% opacity — most currently aren't), a sortable roster, and an EN/简中/繁中/日/한 language menu. **Deliberately not momentum-scan's chart set**: no rank trajectories (MR daily ranks are noise; outcomes are the story). No external assets or network; regenerable at any time, so it's gitignored. Re-render after a scan when the user wants the visual view of the outcome history.
- `state/universe.txt`: cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json`: per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker.

Storage growth: one row per emitted signal: ~20 rows on quiet days to 120+ on washout days, × ~180 bytes ≈ 4-22 KB/day. A year of daily runs ≈ 2-5 MB. Negligible.

## Cadence

Cadence-agnostic by design. One snapshot per US market day (ET); intraday re-runs refresh the snapshot rather than appending. Runs on weekends or NYSE holidays auto-skip from history.

**Recommended cadence**: daily, after the close. Mean-reversion is a short-time-frame signal: RSI(2) moves a lot day-to-day, and the 5-day target window means a signal goes stale within a trading week. Weekly cadence misses 80% of the signals; monthly is useless for this style. If you want lower-effort monitoring, set up a `cron` or `launchd` job to run after the 4pm ET close.

## Known limitations

- **No edge over the index**: over 2021→2026 the signals as a whole earned what SPY did over the same days. The running win rate and expectancy measure the system, not an advantage over holding the index; the dashboard's dotted SPY / QQQ lines are the comparison that matters.
- **Survivorship bias**: the live universe is current US large caps; delisted names are absent. The replay measures it as small for this scan, but its pool (today's names above $500M) still misses names that collapsed or delisted, most of all 2021-22's busted SPACs and meme stocks.
- **Pre-cost**: no transaction costs, slippage, or taxes modeled. Mean-reversion's many small trades make it more cost-sensitive than momentum: 0.5% round-trip on a 1.5% target gain is a 33% haircut to expected return. Real execution shaves more off this style than any other.
- **Connors RSI(2) is a published, well-known system**: traders have arbitraged away some of its edge since the original 2008 publication. Modern win rates on liquid US large caps run 65-75% rather than the 75-80% in the original studies. The 5DMA target is conservative enough that the system still profits, but calibrate expectations to "good", not "great".
- **`MaxDD%` too small to be natural + RSI(2) < 5 is a buyout fingerprint**: same vol-collapse blind spot as the sister skills. The default `--vol-collapse-ratio 0.2` catches the canonical signature, but a deal announced in the last ~6 weeks (gap day in second half of the 3-month vol window) can still leak through. Cross-check via `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun for a pending merger. The `--ticker` mode also surfaces a vol-collapse warning at the top when triggered.
- **The 5DMA target is a moving goalpost.** As price drops, the 5DMA drops too: the target measured at signal time uses *today's* 5DMA, but the price needed to hit "above 5DMA" 3 days later may differ. We persist `target_price` at signal time and resolve against that fixed value, the cleanest definition for out-of-sample win-rate stats though stricter than "first close above 5DMA" using the live 5DMA.
- **Running win rate ignores fees, slippage, and execution gaps.** A "WON" trade where price spiked through the target intraday and closed below it still counts as WON in our resolver (we use the high). In live trading without a take-profit limit order sitting right at the target, you might miss the wick. Treat the historical rate as an upper bound on what you'd realize.
- **Yahoo's daily bar can be late**: since 2026-09-02 the just-closed session's bar often isn't out by the evening scan. The scan rebuilds it from intraday bars (the `**Data**` line says so); when that fails too, the run is a session stale and `history.csv`'s `data_asof` records it.
- **Regime gate uses 200DMA + slope; doesn't catch fast regime flips.** A 1-week selloff (Aug 2024, March 2020) blows through the 200DMA before the slope flips. Mean-reversion signals fired into the start of such a crash are the canonical disaster case.
- **Trend Template is lite by design.** We only check `price > 200DMA`, `200DMA slope positive`, and `50DMA > 200DMA`, three of Minervini's 8 criteria. The omitted ones (RS Rating, distance from 52w low/high, etc.) would over-restrict a mean-reversion universe that benefits from including names that are out of favor for now. If you want a stricter trend filter, run `base-breakout-scan --ticker NAME` first to see if it passes the full Trend Template.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)**: only matters if Yahoo's response stops including the `total` field (schema drift). Same backstop as the sister skills.
- **History csv schema must include `target_price` and `stop_price`**: outcome resolution depends on them. If an old history file from an early version lacks these columns, the resolver skips those rows from win-rate stats (no crash, no stat contribution). Run `--clear-history` to start fresh if you want clean stats.

## Tests

```bash
cd <SKILL_DIR>/scripts && uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  --with 'numpy>=1.24,<3' --with pytest pytest -q
```

Pure-logic tests (no network) cover the signal-breadth dial, the Sig classifier, the Reversion Score components, Wilder RSI, the lite trend filter, trigger-frequency crossing counts, the vol-collapse halves + exclusion filter (low-vol floor, re-ranking), the NYSE trading-day guard, streak/persistence enrichment, outcome resolution with WON / LOST / EXPIRED / OPEN plus the win-rate aggregator, and the outcomes ledger (full-history resolve + keyed upsert) (`test_classify.py`), the backtester's history/spell parsing and canonical / gap-aware / next-open fill conventions (`test_backtest_outcomes.py`), and the HTML renderer's payload building (streak/pocket flags, outcome categories incl. OPEN-vs-UNRESOLVED, breadth stacks, cumulative expectancy lines, windowing, and the per-sector panel's means / 95% intervals / thin-sector folding / untagged exclusion) plus drift guards pinning its mirrored constants to scan.py (`test_render_html.py`), and the replay's per-session pick list pinned to scan.score_tickers + filter_vol_collapse + attach_atr_stops_all session by session, plus its trade resolution and slot portfolio (`test_replay_signals.py`).
