---
name: mean-reversion-scan
description: "Scan US large-cap equities for short-term oversold reversals — Connors-style RSI(2) setups inside confirmed long-term uptrends. Use when the user wants oversold bounces, mean-reversion entries, short-term pullbacks in strong stocks, or a 'buy the dip' watchlist. Triggers on 'find oversold bounces', 'RSI(2) setup', 'mean reversion candidates', 'short-term pullback', 'buy the dip', 'bounce candidates', 'panic sellers', 'overdone sell-off'. The complement to momentum-scan and base-breakout-scan: those find what's running and what's about to run; this finds what just got punched in the face but is structurally fine. Do NOT use for single-ticker chart analysis (use yfinance), value/contrarian long-term picks, ETF screening, or generic explanations of mean-reversion theory."
---

# mean-reversion-scan

Find US equities that are **short-term oversold inside a confirmed long-term uptrend** — Larry Connors's canonical RSI(2) setup, augmented with persistence tracking and per-name outcome resolution so each subsequent run shows you the **running win rate** of past picks.

The core bet: in a healthy uptrend, a stock whose 2-day RSI dives below 5 is likely panicking on a short-term overreaction (margin calls, ETF rebalancing, headline noise) rather than starting a real breakdown. The mean-reversion edge is **the bounce back to the 5-day average within 1-5 trading days**. Connors's published win rates on this exact setup are 70-75% on liquid US large-caps in RISK-ON regimes; the 25-30% losing trades are usually small-to-moderate but include occasional gap-down disasters when the trend was actually breaking.

The natural complement to `momentum-scan` and `base-breakout-scan`:
- `momentum-scan` finds **what's already running** (trailing return + low drawdown)
- `base-breakout-scan` finds **what's about to run** (compressed pre-breakout bases)
- `mean-reversion-scan` finds **what just got punched in the face but is structurally fine** (oversold inside an uptrend)

The three skills filter for non-overlapping price patterns by design — you wouldn't want any of them to surface the same name on the same day.

By default each run surfaces:
1. A **regime gate** (SPY > 200DMA + rising — Connors's hard filter; mean-reversion longs in a confirmed downtrend is the classic "catching falling knives" trap)
2. A per-ticker **uptrend filter** (price > 200DMA, 200DMA slope positive — same logic, applied at the name level)
3. The **RSI(2) trigger** (default < 5; deep tier < 2 fires the 🔵 signal)
4. A **composite Reversion Score** (0-100) combining RSI depth, trend health, pullback magnitude, and frequency-of-trigger uniqueness
5. **ATR-based stop loss** (per-name max-loss anchor)
6. **Outcome resolution on past picks** — for every signal in the last ~30 trading days, did price reach the 5DMA target within 5 days (won), hit the stop (lost), or expire flat? Yields a running win-rate stat that grows more reliable as history accumulates.
7. The **vol-collapse filter** (same M&A-arb defense as the sister skills — without it, an acquisition-target with a post-deal price-pin can satisfy "RSI(2) low" trivially while not being tradable)

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2`, `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run — RSI(2) < 5, top 30
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/scan.py

# Tighter trigger (only deep oversold)
... python <SKILL_DIR>/scripts/scan.py --rsi2-threshold 2

# Looser trigger when the market hasn't been giving signals
... python <SKILL_DIR>/scripts/scan.py --rsi2-threshold 10

# Inspect history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# Single-ticker diagnostic — "is AAPL set up for a bounce right now?"
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
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--rsi2-threshold` | 5.0 | RSI(2) ceiling for the 🟢 fresh-trigger signal. Connors's published value is 5; raise to 10 in thin tapes (more candidates, shallower oversold), lower to 2 to catch only deep panics. The 🔵 deep tier is hardcoded at half the threshold (default 2.5). |
| `--top-n` | 30 | How many candidates to display + log to history. |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower to include small-cap reversals (richer in this pattern but with much higher tail risk — a small-cap "RSI < 5 inside an uptrend" can still be a CEO-leaving-tomorrow situation). |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match (~1000 US large caps at default mcap/volume floors). The screener returns at most 250 rows per request, so larger values are paginated automatically with `offset`. Pass an explicit positive integer to cap. If you raise above the cached size, the cache is force-refreshed. |
| `--refresh-universe` / `--no-refresh-universe` | (TTL 7d) | Force refresh / use cache regardless of age. |
| `--ticker` | — | Single-ticker diagnostic mode (e.g. `--ticker AAPL`). Bypasses universe scan; shows trend template pass/fail, RSI(2), 5DMA distance, signal classification, ATR stop, and historical reliability over the last ~60 trading days for this name. ~2-5s vs ~30-60s for full scan. Honors `--format json`. No history is written. |
| `--show-history` | — | Print history summary including running win rate; no new scan. |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--prune-non-trading-days` | — | One-shot cleanup: drop history rows whose ET-date `run_date` is not an NYSE trading day. |
| `--no-save` | — | Don't append this run to history. |
| `--save-stale` | — | Override the non-trading-day guard. By default the script skips `append_history` on weekends / NYSE holidays so streak counts and outcome resolution don't double-count duplicate-data days. Pre-market runs on a real trading day are still saved. |
| `--allow-same-day` | — | Append even if a row exists for today's ET date. Default overwrites today's snapshot. |
| `--format` | markdown | `markdown` or `json`. |
| `--regime-gate` | warn | `off` skips SPY trend calc. `warn` shows banner + RISK-OFF caveat but still prints top-N. `strict` suppresses top-N when RISK-OFF (history still saved). RISK-ON requires SPY > 200DMA AND 200DMA slope (20 trading days) above a small `-0.05%` dead band. **Mean-reversion longs are particularly dangerous in RISK-OFF** — the canonical failure mode is "every oversold bounce is followed by more selling" (2008 H2, 2020 March, 2022 H1). Strict gate is recommended for live trading. |
| `--atr-stop-mult` | 2.5 | ATR-based stop multiplier. Computes 14-day ATR and adds a `Stop` column showing `last_close - mult × ATR`. Typical: 2.0 tight, 2.5 standard, 3.0 loose. Pass `0` or negative to disable the column. The stop is **also persisted to history.csv** so outcome resolution can check whether the stop was hit between signal and target. |
| `--no-sectors` | — | Disable sector tagging. Default fetches sector/industry from yfinance for top-N picks (cached, 30-day TTL) and shows a Sector column + breakdown line. |
| `--vol-collapse-ratio` | 0.2 | Acquisition-target / lock-in filter. Same logic as the sister skills: a stock pinned at a cash buyout offer trivially satisfies "low RSI(2)" while not being tradable as mean reversion. Excludes names where 2nd-half realized vol over a 3-month window is < ratio × 1st-half vol. Default 0.2; raise to 0.3 for more aggressive exclusion (more false positives), lower to 0.15 for stricter. Hard cap 1.0. Pass `0` or negative to disable. |
| `--persistent-min-streak` | 3 | Streak threshold for the **Stuck oversold** section. **In mean reversion, a long streak is a yellow flag, not green** — it means the bounce hasn't materialized after multiple runs, suggesting something structural is happening rather than a noise overreaction. Default 3 surfaces these for review. |
| `--target-window-days` | 5 | Number of trading days within which the bounce-to-5DMA must occur for an outcome to count as WON. Connors's canonical exit is "first close ≥ 5DMA"; we use intraday high to be charitable. After this many days without target or stop hit, outcome is EXPIRED. |

## Output shape

A regime banner, sector breakdown, an optional **Excluded by vol-collapse filter** section, the main top-N table with Sig column, and 2-3 discovery sections (recently-resolved picks with running win rate, stuck-oversold leaders). Sections with zero entries are skipped. Sample (illustrative — picks change daily):

```
# Mean-reversion scan — 2026-05-14 16:32 UTC

**Params**: rsi2_threshold=5.0, target_window=5d, mcap>5e+09
**Universe**: 1035 tickers · **Passed filter**: 18 (vol-collapse: 0 excluded) · **Prior runs**: 12
**Regime**: SPY 742.3 vs 200DMA 672.2 (+10.4%) · 50DMA > 200DMA · 200DMA slope (20d): +1.50% · Breadth: 58% > 200DMA → **RISK-ON**
**Win rate** (last 30d, 47 resolved): 73% (34W / 13L) · avg days to target: 1.9
**Sectors**: Tech 5 · Health 4 · Financ 3 · Cons Cyc 2 · Energy 2 · Other 2

## Top 18

| # | Ticker | Sector | RSI(2) | 5DMA% | 50DMA% | 200DMA% | Score | Sig | Streak | Freq60d | Stop | Target |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **AAPL** | Tech | 1.8 | -3.2 | +4.1 | +14.2 | 65 | 🔵 | 1 | 2 | $228.40 (-2.7%) | $237.55 (+1.2%) |
| 2 | **JNJ** | Health | 4.2 | -2.8 | +1.1 | +8.5 | 35 | 🟢 | 1 | 1 | $158.20 (-4.3%) | $164.10 (+0.9%) |
...

## Recently resolved (last 5 days, 6 picks)
**Won** (4):
- **NVDA** — signaled 2026-05-09 @ $144.20, hit target $147.10 in 1 day (+2.0%)
- **MSFT** — signaled 2026-05-12 @ $431.50, hit target $437.20 in 2 days (+1.3%)
- ...
**Lost** (1):
- **XYZ** — signaled 2026-05-10 @ $52.80, stopped at $48.10 (-8.9%) in 3 days
**Expired** (1):
- **DEF** — signaled 2026-05-08, drifted -0.4% over 5 days, neither target nor stop hit

## Stuck oversold (streak ≥ 3 runs — REVIEW for structural break)
- **GHI** — streak 4, first seen 2026-05-08, RSI(2) still 3.1 (was 4.5 → 2.8 → 3.6 → 3.1)
  ⚠️ Bounce hasn't materialized in 4 sessions. Possible: real breakdown, missed news catalyst, sector-wide pressure.
```

Column meanings:

- **RSI(2)** — 2-period RSI using Wilder's smoothing. Connors's canonical signal. Below threshold = oversold; lower = more oversold.
- **5DMA%** — `(last_close / SMA(5) - 1) × 100`. Negative = price below 5-day average (the canonical Connors target for the bounce). The reversion target is the 5DMA itself; this column shows how far you are from it.
- **50DMA%, 200DMA%** — distance from the longer averages. Both should be positive for a healthy "MR inside uptrend" setup. If 50DMA% goes negative, the trend is wobbling and the MR signal is lower-conviction.
- **Score** — composite 0-100 Reversion Score. All components are *variable* (no constant offsets — the trend filter is a hard gate before scoring). Components: RSI(2) depth (40pts: rsi2=0 → 40, rsi2=threshold → 0), 5DMA pullback magnitude (30pts: dist_5dma=-15% → 30), trend buffer quality (15pts: dist_200dma=+30% → 15, rewards "MR inside a real uptrend, not a borderline one"), frequency uniqueness (15pts: never-fired → 15, freq=8 → 0). Realistic calibration: textbook 🟢 picks land 50-65, 🔵 with buffer + low freq lands 70-85, 90+ is rare.
- **Sig** — entry classifier:
  - **🟢 fresh trigger** — RSI(2) below threshold AND price > 200DMA AND trend healthy. The canonical Connors setup; act today or set a price-improvement limit.
  - **🔵 deep oversold** — RSI(2) below half-threshold (default 2.5). The deeper the panic, the more reliable the bounce historically — but also the higher chance of a real news driver. Verify there's no major catalyst.
  - **🟡 setup forming** — RSI(2) within `[threshold, threshold × 2]`. Approaching trigger; monitor for a further selloff to confirm.
  - **🔴 too late** — RSI(2) > 50 (already bouncing). Don't initiate; the move may be partly done.
- **Streak** — consecutive prior runs this ticker has appeared. **In MR, high streak is a warning, not a confirmation** — see "Stuck oversold" section.
- **Freq60d** — number of times this ticker triggered RSI(2) < threshold in the last 60 trading days. Lower = more idiosyncratic event = better signal. Higher = noisy name where this signal fires frequently and has less informational value.
- **Stop** — ATR-based stop level: `last_close - mult × ATR(14)`. Format `$price (-%)`. The persisted-to-CSV stop level used for outcome resolution.
- **Target** — `5DMA × 1.0` (the canonical Connors exit). Format `$price (+%)`. Persisted to CSV alongside Stop for outcome resolution.

The `Win rate` line in the banner aggregates **resolved** outcomes across all history (only WON or LOST count; OPEN and EXPIRED are excluded from the rate but counted in the resolved total). It becomes meaningful after ~10 resolved picks and reliable after ~30.

### Recently resolved section

For each pick from the last `--target-window-days × 2` calendar days, the script looks at the price action since the signal date and classifies:

- **WON** — high reached `target` within `--target-window-days` (default 5). Shows entry price → target price + days to target.
- **LOST** — low touched `stop` before the target was hit. Shows entry → stop level + days to stop.
- **EXPIRED** — neither target nor stop hit within the window. Shows the drift % over the window.
- **OPEN** — fewer than `--target-window-days` trading days have passed since signal. Not displayed (still in flight).

Resolution is **deterministic from history.csv plus current price data** — no separate outcome ledger to maintain. Each run re-resolves the relevant prior signals using fresh price data.

### Stuck oversold section

Names with streak ≥ `--persistent-min-streak` (default 3). The interpretation flips vs. the trend-following sister skills:

- In `momentum-scan`: high streak = durable winner = more conviction
- In `base-breakout-scan`: high streak = base maturing = more conviction
- In `mean-reversion-scan`: **high streak = bounce never came = LESS conviction**

The mean-reversion thesis is "panic + healthy trend → quick bounce". When the bounce doesn't happen for 3+ runs, the panic is being sustained — which usually means there's a real driver (news, sector rotation, broken trend) the price isn't telling you about yet. These names are flagged for review, not for buying.

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

The historical reliability section is unique to single-ticker mode — it scans the last 60 trading days of price data for past instances of this exact setup on this exact ticker and resolves their outcomes. Builds confidence (or skepticism) about applying the system to this specific name.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly.

1. **Lead with the regime banner.** Mean reversion has its absolute worst regime in confirmed bear markets — this is non-negotiable. If RISK-OFF, the recommendation should be "wait" or "paper-trade only", not "here's a name to buy". Even in `--regime-gate warn` mode where the table still prints, frame the names as research-only when SPY is below a falling 200DMA.

2. **Lead with the running win rate.** Once history has ≥ 10 resolved picks, the `Win rate` line is the most informative single number — it tells the user whether this system has been working *recently in this market*. Connors's published 70-75% is the long-term steady state on liquid US large caps in healthy regimes; if your historical rate is sitting at 50%, the system is currently struggling, which is itself a signal about the regime even if the gate says RISK-ON.

3. **Read Sig before Score.** The classifier captures the actionable distinction better than the composite:
   - **🔵 deep oversold** is the highest-EV bucket *if* you've verified there's no catalyst — RSI(2) < 2.5 is rare and historically bounces hard. Always cross-check news.
   - **🟢 fresh trigger** is the meat of the system — the canonical 70%+ setup.
   - **🟡 setup forming** is research, not action — set a limit order at a price that would pull RSI(2) below threshold.
   - **🔴 too late** is "missed the bus this time" — note for next occurrence.

4. **Frequency matters more than people realize.** A name with `Freq60d = 1` (this is the only RSI(2) panic in the last 3 months) is a much higher-conviction signal than a name with `Freq60d = 8` (this happens almost weekly — it's a noisy stock where the signal fires often and means little). Two names with similar Scores but very different Freq60d should be ranked by Freq60d (lower = better) before any other tiebreaker.

5. **The Stuck oversold section is more important than the top-N.** Names appearing here have failed the mean-reversion thesis — the bounce didn't come. The natural next step is to investigate *why*: news search, earnings calendar, sector ETF check. Often this surfaces information the broader market hasn't priced yet. **Never recommend buying these.** They're flagged for analysis, not action.

6. **Stop discipline is non-negotiable for this style.** The 70% win rate only translates into profitability if losses are capped. Connors-style MR is asymmetric in the wrong direction: many small wins, occasional larger losses. Without the stop, one breakdown trade can wipe out 5-10 winners. The Stop column is the hard floor; Target is the take-profit. Both are persisted to history so the win rate stat reflects realistic execution.

7. **Sector clustering means less here than in momentum.** A momentum scan with 16/30 Tech tells you AI infra is the cluster trade. A mean-reversion scan with 8/30 Tech in the same week probably means the Nasdaq had a bad day — that's a market-wide event, not a sector edge. The diversification value of the cluster is the inverse of momentum: you'd ideally want each pick from a different sector to avoid all your stops firing together on one bad SPY day.

8. **Never recommend specific buys, especially not for this style.** The 70% win rate is a population statistic — any individual trade is a coin flip with 70% bias. Frame results as "names where the Connors RSI(2) setup is currently triggered, here are the entry/stop/target levels", not "buy this". And always flag that mean-reversion strategies have spectacular tail-risk events when fundamental selloffs are misread as panic — the 2008 H2 case study is the canonical lesson.

## State files

- `state/history.csv` — one snapshot per US market day (America/New_York) × every top-N ticker. Columns: `run_id, run_date, ticker, rank, score_rank, score, rsi2, dist_5dma_pct, dist_50dma_pct, dist_200dma_pct, last_close, target_price, stop_price, signal, freq_60d`. Re-running the same ET day overwrites that day's rows. Writes are atomic (.tmp + rename). The `target_price` and `stop_price` columns are the load-bearing fields for outcome resolution — without them the win-rate stats can't be computed.
- `state/universe.txt` — cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json` — per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker.

Storage growth: ~30 rows × ~180 bytes ≈ 5.4 KB/day. A year of daily runs ≈ 2 MB; weekly ≈ 280 KB. Negligible.

## Cadence

Cadence-agnostic by design. One snapshot per US market day (ET); intraday re-runs refresh the snapshot rather than appending. Runs on weekends or NYSE holidays auto-skip from history.

**Recommended cadence**: daily, after the close. Mean-reversion is a short-time-frame signal — RSI(2) changes meaningfully day-to-day, and the 5-day target window means a signal goes stale within a trading week. Weekly cadence misses 80% of the signals; monthly is useless for this style. If you want lower-effort monitoring, set up a `cron` or `launchd` job to run after the 4pm ET close.

## Known limitations

- **Survivorship bias** — universe is current US large caps; delisted names are absent. The historical win rate can't account for catastrophic past events that wiped a name from the universe entirely.
- **Pre-cost** — no transaction costs, slippage, or taxes modeled. Mean-reversion's many small trades make it more cost-sensitive than momentum: 0.5% round-trip on a 1.5% target gain is a 33% haircut to expected return. Real execution shaves more off this style than any other.
- **Connors RSI(2) is a published, well-known system** — meaning some of its edge has been arbitraged away since the original 2008 publication. Modern win rates on liquid US large caps are typically 65-75% rather than the 75-80% in the original studies. The 5DMA target is conservative enough that this still works as a profitable system, but expectations should be calibrated to "good", not "great".
- **`MaxDD%` improbably small + RSI(2) < 5 is a buyout fingerprint** — same vol-collapse blind spot as the sister skills. The default `--vol-collapse-ratio 0.2` catches the canonical signature, but a deal announced in the last ~6 weeks (gap day in second half of the 3-month vol window) can still leak through. Cross-check via `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun for a pending merger. The `--ticker` mode also surfaces a vol-collapse warning at the top when triggered.
- **The 5DMA target is a moving goalpost.** As price drops, the 5DMA drops too — the target measured at signal time uses *today's* 5DMA, but the actual price needed to hit "above 5DMA" 3 days later may be different. We persist `target_price` at signal time and resolve against that fixed value, which is the cleanest definition for out-of-sample win-rate stats but slightly stricter than "first close above 5DMA" using the live 5DMA.
- **Running win rate ignores fees, slippage, and execution gaps.** A "WON" trade where price spiked through the target intraday and closed below it would still be marked WON in our resolver (we use the high). In live trading without a take-profit limit order at exactly the target, you might miss the wick. Treat the historical rate as an upper bound on what you'd realize.
- **Regime gate uses 200DMA + slope; doesn't catch fast regime flips.** A 1-week selloff (Aug 2024, March 2020) blows through the 200DMA before the slope flips. Mean-reversion signals fired into the start of such a crash are the canonical disaster case.
- **Trend Template is intentionally lite.** We only check `price > 200DMA`, `200DMA slope positive`, and `50DMA > 200DMA` — three of Minervini's 8 criteria. The omitted ones (RS Rating, distance from 52w low/high, etc.) would over-restrict a mean-reversion universe that benefits from including names temporarily out of favor. If you want a stricter trend filter, run `base-breakout-scan --ticker NAME` first to see if it passes the full Trend Template.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)** — only matters if Yahoo's response stops including the `total` field (schema drift). Same backstop as the sister skills.
- **History csv schema must include `target_price` and `stop_price`** — outcome resolution depends on these. If you have an old history file from an early version that lacks these columns, those rows will be skipped from win-rate stats (gracefully — no crash, just no stat contribution). Run `--clear-history` to start fresh if you want clean stats.
