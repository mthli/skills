---
name: momentum-scan
description: Scan US large-cap equities for smooth uptrends — high trailing return paired with shallow drawdown — and track which names persist across runs. Use when the user wants to find what's working in the market, scan for momentum, discover the next NVDA / LITE / MU-style breakout before headlines, spot leading sectors or themes (AI infra, semis, defense, lithium, etc.), surface persistent winners across runs, or compare current leaders to a prior run. Also covers re-runs and parameter tweaks ("run it again", "anything new showing up", "3 month window", "include small caps"). Do NOT use for single-ticker price or fundamentals lookups, ETF holdings, chart generation, value-investing screens, or generic explanations of momentum investing — those need other tools or plain answers.
---

# momentum-scan

Find US equities in **smooth uptrends** — high trailing return with shallow drawdown — and surface which names are durable leaders vs single-week pops. The value over a one-shot screener is **persistence tracking**: each US market day (America/New_York) is logged once to `state/history.csv` (re-running the same day refreshes that day's snapshot rather than appending), so each subsequent run can compute streak, rank changes, dropouts, and new entrants.

By default every run also surfaces two **entry-timing layers** on top of the momentum filter: a **pullback entry signal** (MA20 distance + RSI(14) → 🟢 buy zone / 🔵 deep pullback / 🟡 in trend / 🟠 stretched / 🔴 overextended) that flags whether each pick is currently buyable vs already extended, and an **ATR-based stop loss** (2.5× ATR by default) for per-position risk sizing. The pullback signal answers "is this buyable right now?" — the canonical complement to momentum's "what's running?" question, since momentum names typically arrive already 30-50% above MA20, a state where mean-reversion pullbacks often give back a meaningful slice of the gain before the trend resumes.

A **vol-collapse filter** (`--vol-collapse-ratio`, default 0.2) also runs after the score-based ranking (before persistence enrichment) to catch the canonical signature of an acquisition target trading at the announced cash offer price: a single-day gap up on the deal announcement, then daily range collapses to pennies as the stock is pinned at the deal price and only M&A-arb spreads trade. Without this filter, the gap day inflates the window return while the post-event flat tape gives a misleadingly tiny max drawdown — together yielding an outlier Score that isn't tradable as momentum. The same signature catches reverse-merger / SPAC lock-ins and (less cleanly) some halted-into-cash situations. Excluded names get a dedicated section in the output with their pre/post-event annualized vol so you can sanity-check the trigger. The filter's sensitivity depends on where the gap day lands in the scoring window — see Known limitations.

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2` (the script uses `format="ISO8601"`, added in pandas 2.0), `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run — 3mo window, top 30
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/scan.py

# Longer window for smoother, slower-moving leaders
... python <SKILL_DIR>/scripts/scan.py --window-months 6

# Inspect the run history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# Machine-readable JSON output
... python <SKILL_DIR>/scripts/scan.py --format json

# Strict trend filter: suppress top-N when SPY is below a rising 200DMA
... python <SKILL_DIR>/scripts/scan.py --regime-gate strict

# Vol-targeted sizing: cohort 60d vol → leverage; per-name Weight% column
... python <SKILL_DIR>/scripts/scan.py --target-vol-pct 15

# Override ATR stop multiplier (default 2.5; pass 0 to disable)
... python <SKILL_DIR>/scripts/scan.py --atr-stop-mult 3.0

# Skip pullback entry indicator (no MA20% / RSI / Sig columns)
... python <SKILL_DIR>/scripts/scan.py --no-pullback

# Skip sector tagging (faster first run, no Sector column or breakdown line)
... python <SKILL_DIR>/scripts/scan.py --no-sectors

# Disable the vol-collapse acquisition-target filter (keep buyouts in the table; see parameter table for range)
... python <SKILL_DIR>/scripts/scan.py --vol-collapse-ratio 0
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--window-months` | 3 | Lookback for return + max drawdown. Shorter = earlier signals, more noise. Bump to 6 for smoother, slower-moving leaders. |
| `--top-n` | 30 | How many names to display + log to history. |
| `--min-return-pct` | 30 | Filter floor on trailing return over the window. |
| `--max-dd-pct` | 20 | Filter ceiling on max drawdown (absolute value). |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower = include small-cap rockets but more noise. |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match the screener reports (currently ~1000 US large caps at default mcap/volume floors). The screener returns at most **250 rows per request** (Yahoo's hard cap; `yf.screen` raises `ValueError` above that), so the universe is paginated automatically in 250-row pages with `offset` — at default filters that's ~5 paginated requests, taking a few extra seconds, but only on cache refresh (every 7 days). Pass an explicit positive integer to cap the universe at the top-N largest by market cap (e.g. `250` for a one-request refresh, `500` for the previous default size); 0 / negative values are rejected by argparse. If you raise `--universe-count` above the number of tickers already in `state/universe.txt`, the cache is force-refreshed even within TTL — otherwise you'd silently get the smaller cached pool. Older yfinance versions (without `offset` support) fall back to a single 250-row page. |
| `--refresh-universe` | (auto, 7d TTL) | Force-refresh universe (ignore cache). |
| `--no-refresh-universe` | — | Use cached universe even if past TTL (offline / testing). |
| `--show-history` | — | Dump history summary, no new scan. |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--no-save` | — | Run but don't append to history (useful for one-off exploration). |
| `--save-stale` | — | Override the non-trading-day guard. By default the script skips `append_history` when today's ET date is a weekend or NYSE-observed holiday so streak counts don't inflate from duplicate-data days. Pre-market runs on a real trading day are still saved. |
| `--allow-same-day` | — | Keep existing rows for today's ET date instead of overwriting them (debugging / forcing multiple snapshots). |
| `--prune-non-trading-days` | — | One-shot cleanup: drop history rows whose ET-date `run_date` is not an NYSE trading day. Use after upgrading from a pre-guard version, or after intentional `--save-stale` runs. No scan is performed. |
| `--format` | markdown | `markdown` or `json`. |
| `--regime-gate` | warn | Market trend filter. `off` skips it entirely (and the longer data fetch). `warn` shows a SPY/breadth banner + a RISK-OFF caveat; top-N still printed. `strict` suppresses the top-N when RISK-OFF (history is still saved so streaks survive). RISK-ON means SPY > 200DMA *and* the 200DMA slope over the last 20 trading days is above a small `-0.05%` dead band (so a near-flat MA doesn't flip on single-bar noise). |
| `--target-vol-pct` | (off) | Portfolio vol target in % (e.g. `15` for 15% annualized). When set: computes the equal-weight cohort's 60-day realized vol, surfaces `suggested leverage = target / cohort_vol` (clipped to `[0.25, 1.0]` — deleverage-only, per Daniel-Moskowitz 2016), and adds a `Weight%` column using equal-risk-contribution × leverage. The weights sum to `leverage × 100`, so a 0.6× leverage means you hold 60% notional and 40% cash. Off = no Weight% column. |
| `--atr-stop-mult` | 2.5 | ATR-based stop multiplier. Computes 14-day ATR for each top-N pick, adds a `Stop` column showing `last_close - mult × ATR` as both price and % from spot. Names with `Streak ≥ --persistent-min-streak` also get a `TrailStop` line in the Persistent leaders section, anchored to the peak since `FirstSeen`. Typical multipliers: `2.0` tight (frequent stop-outs, lower per-trade loss), `2.5` standard (default), `3.0` loose (rarer stop-outs, larger per-trade loss). Pass `0` or a negative value to disable the Stop column entirely. |
| `--no-pullback` | — | Disable the pullback entry indicator. Default behavior computes MA20 distance and RSI(14, Wilder) for each top-N pick and shows three columns: `MA20%` (price relative to its 20-day average), `RSI` (14-day Wilder RSI), and `Sig` (🟢/🔵/🟡/🟠/🔴 classification). Evaluation order is 🟢 → 🔵 → 🔴 → 🟠 → 🟡 (first match wins). 🟢 = MA20% in [-3, +3] *and* RSI in [40, 55] (classic Trend Pullback buy zone). 🔵 = MA20% ≤ -3 *and* RSI < 40 (Connors-style deep pullback in a still-intact uptrend — potentially strong risk/reward *if* the trend holds, but harder to confirm than 🟢 since it can also be the leading edge of a broken trend). 🔴 = MA20% > 25 *or* RSI > 80 (overextended, chasing here tends to give back a meaningful slice on the first pullback). 🟠 = MA20% in (15, 25] or RSI in (70, 80] (stretched, wait). 🟡 = everything else (in trend, neutral). Pair with the momentum filter to filter for buyable-right-now names rather than already-extended runners. |
| `--persistent-min-streak` | 3 | Streak threshold used by both the **Persistent leaders** section and the ATR `TrailStop`. Default `3` matches the historical display threshold. Bump to `4` if you only want streaks that have survived multiple periods of noise — the "real signal" cutoff from the interpretation guide. |
| `--no-sectors` | — | Disable sector tagging. Default: fetches sector/industry from yfinance for the top-N picks (cached in `state/sectors.json`, 30-day TTL), shows a `Sector` column in the table and a `**Sectors**` breakdown line in the header. First-run cost is ~1–2s per uncached pick (parallelized at 10 workers); subsequent runs hit the cache. Pass `--no-sectors` to skip entirely. |
| `--vol-collapse-ratio` | 0.2 | Acquisition-target / lock-in filter. Computes annualized realized vol for the **first** and **second** halves of the scoring window and excludes names where `vol_second / vol_first < ratio` (with a `vol_first ≥ 5%` annualized floor — names already at very low vol have an unstable ratio). Default 0.2 catches the canonical signature (announcement gap + post-event price-pin → 2nd-half vol collapses to single digits) while leaving normal consolidating-after-a-rally names alone. **Raise** to `0.3` (hard cap `1.0`) to catch more lock-in patterns at the cost of more false positives on calmly-drifting earnings-pop names. **Lower** to `0.15` to require a more dramatic collapse (fewer exclusions, may leak buyouts through). Pass `0` or negative to disable. See the Output shape section ("Vol-collapse filter — output details") for the section placement, field schema, and lifecycle. |

## Output shape

A markdown table of the top N, plus three discovery sections. Sample (truncated):

```
# Momentum scan — 2026-05-11 16:07 UTC

**Params**: window=3mo, min_return=30.0%, max_dd=20.0%, mcap>5e+09
**Universe**: ~1000 tickers · **Passed filter**: ~50 (vol-collapse: 0 excluded) · **Prior runs**: 1
**Regime**: SPY 612.4 vs 200DMA 558.2 (+9.7%) · 50DMA > 200DMA · 200DMA slope (20d): +0.18% · Breadth: 68% > 200DMA → **RISK-ON**
**Vol target**: cohort 60d vol 24.8% → suggested leverage **0.60x** (target 15%, raw 0.60x, clip 0.25–1.00x)
**Sectors**: Technology 11 · Energy 5 · Healthcare 4 · Communication Services 3 · Industrials 2 · Other 5

## Top 10

| # | Ticker | Sector | 3m% | MaxDD% | AnnVol% | Score | Streak | RankΔ | FirstSeen | FromHigh% | MA20% | RSI | Sig | Stop | Weight% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **NOK**  | Tech  | +92.4  | -8.0  | 38 | 11.6 | 2 | +5 ↗ | 2026-05-11 | 0.0  | +19.9 | 74 | 🟠 | $5.42 (-5.5%)  | 2.6 |
| 2 | **MRVL** | Tech  | +109.4 | -10.8 | 52 | 10.1 | 1 | 🆕   | 🆕         | 0.0  | +9.1  | 69 | 🟡 | $82.10 (-6.8%) | 1.5 |
| 3 | **DELL** | Tech  | +107.8 | -10.8 | 50 | 10.0 | 1 | 🆕   | 🆕         | -3.8 | +16.0 | 69 | 🟠 | $128.40 (-7.1%)| 1.5 |
| 4 | **AMD**  | Tech  | +115.0 | -11.6 | 55 | 9.9  | 1 | 🆕   | 🆕         | 0.0  | +36.2 | 81 | 🔴 | $231.10 (-7.4%)| 1.4 |
| 5 | **CIEN** | Tech  | +103.5 | -16.8 | 58 | 6.2  | 2 | -7 ↘ | 2026-05-11 | 0.0  | +12.5 | 67 | 🟡 | $108.30 (-9.2%)| 1.2 |
...

## Dropouts since last run (6)
- **ASX** (was #2, 3m=+126.6%)
- **TTE** (was #3, 3m=+45.8%)
...

## New entrants (6)
- **MRVL** at #2 (3m +109.4%, MaxDD -10.8%)
- **DELL** at #3 (3m +107.8%, MaxDD -10.8%)
...

## Persistent leaders (streak ≥ 3 runs)
- **CIEN** — streak 4, first seen 2026-04-21, now #5 · trail stop $108.50 (-9.3% from spot, peak $120.30)
```

(The `trail stop ...` suffix only appears when `--atr-stop-mult` is set *and* `Streak ≥ --persistent-min-streak` — which controls both the Persistent leaders threshold and the trail-stop attach threshold. Names below it skip the suffix cleanly.)

Numbers above are illustrative; real `Stop%` spans roughly -5% (sedate) to -22% (high-vol breakouts). The sample doesn't include a 🔵 row because deep pullbacks in still-strong momentum names are uncommon — they appear primarily after sharp short-term sell-offs in otherwise-trending leaders.

Column meanings:

- **AnnVol%** — annualized realized volatility over the scoring window (`--window-months`). Useful both as a per-name sanity check (a +100% return at 60% vol is a coin flip held the right way; the same return at 25% vol is a real trend) and as the input to the per-name `Weight%` allocation when `--target-vol-pct` is set.
- **Score** — return ÷ |max drawdown|. Higher = more return per unit of pain.
- **Streak** — consecutive prior runs this ticker was in the top N (1 = first appearance).
- **RankΔ** — `(score_rank at latest prior appearance) − (current score_rank)`. Positive ↗ = rising; negative ↘ = slipping; 🆕 = no prior appearance in the entire history. Note: "latest prior appearance" is not necessarily the *immediately previous run* — a ticker that fell out for a few runs and is now back will show the delta against its last-seen rank, not against the previous run (where it was absent). The `FirstSeen` column and the **New entrants** / **Dropouts** sections cover the "in-and-out" view; RankΔ stays focused on "how has this name moved since we last saw it". **Important**: delta is computed on `score_rank` (the pre-vol-collapse score-based rank — see Output shape), not on the display rank `#` shown in the leftmost column. This means a vol-collapse exclusion of a top pick won't produce false `+1 ↗` for every name below it. In rare cases when vol-collapse status flips between adjacent runs for the same name (e.g., a name re-passing the filter after a few days excluded), the displayed `prev_rank` and the new display `#` may not match RankΔ arithmetically — that's intentional and reflects that RankΔ measures real score movement, while `prev_rank` shows what the user saw last time.
- **FirstSeen** — earliest date this ticker appeared in any past run.
- **Weight%** — only present with `--target-vol-pct`. Equal-risk-contribution weight (∝ 1/ann_vol) scaled by the suggested leverage. The column sums to `leverage × 100` (so e.g. 60 means 60% notional, 40% cash). Treat as a sizing *starting point*, not a target portfolio — see Known limitations on the correlation simplification.
- **Sector** — only present when sector tagging is enabled (default; disable with `--no-sectors`). Abbreviated GICS-ish sector from yfinance. The `**Sectors**` breakdown line above the table gives full names and counts.
- **MA20%, RSI, Sig** — only present when the pullback indicator is enabled (default; disable with `--no-pullback`). `MA20%` is `(last_close / MA20 - 1) × 100` — positive means above the 20-day average, negative means below. `RSI` is the 14-day Wilder RSI (canonical, EWMA with α=1/14). `Sig` is the buy-zone classifier (🟢 buy / 🔵 deep pullback / 🟡 watch / 🟠 stretched / 🔴 overextended) — see the `--no-pullback` row in the parameter table for thresholds. Reading rule: 🟢 and 🔵 are *candidates worth investigating today*; 🟠/🔴 are quality momentum but you're late — set a price alert at MA20 and wait.
- **Stop** — present by default (disable by passing `--atr-stop-mult 0`). Format: `$price (-%)`. The stop price = `last_close - mult × ATR(14)`. The % shows how far below the current price that sits. For names with `Streak ≥ --persistent-min-streak` (default 3), the Persistent leaders section also surfaces a `TrailStop` anchored to the peak since `FirstSeen` (locks in profits as the trend matures).

The three discovery sections (dropouts / new entrants / persistent leaders) are computed against the most recent prior run. When the vol-collapse filter removes one or more names, an **Excluded by vol-collapse filter** section appears *between the Regime banner and the Top-N table* (above the main table, not below). This placement is deliberate: the exclusions are warnings about names that *look* like momentum but aren't, and they print even when `--regime-gate strict` suppresses the rest of the output — so the user always sees them. The section lists each ticker with its `1st-half% → 2nd-half%` annualized vol and the resulting ratio, so you can sanity-check the trigger and recognize the underlying situation (most commonly a cash buyout pending shareholder vote).

JSON output mirrors the markdown structure: the top-level envelope has a `picks` array (kept entries, with `rank` = display position 1..N and `score_rank` = pre-filter score ordering) and an `excluded_vol_collapse` array (excluded entries, with `rank: null`, `pre_filter_rank` = where they sat in the score ordering, and the `vol_first_pct` / `vol_second_pct` / `vol_ratio` triple). `rank_delta` on kept picks is computed from `score_rank` (not display rank), so removing a top pick by vol-collapse doesn't make every name below it falsely show +1 ↗. The `--show-history` view applies the same score_rank-aware delta when computing biggest climbers / droppers across run pairs (falls back to display rank for old history rows without the column).

### Vol-collapse filter — output details

When `--vol-collapse-ratio > 0` (default 0.2), the universe banner reflects the filter state in a unified pattern:

- **Disabled** (`--vol-collapse-ratio 0`): `**Passed filter**: 83`
- **Active, 0 excluded**: `**Passed filter**: 83 (vol-collapse: 0 excluded)`
- **Active, N excluded**: `**Passed filter**: 82 (vol-collapse: 1 excluded of 83)`

The **"Excluded by vol-collapse filter"** section prints *between the Regime banner and the Top-N table* (above the main table, not below). This placement is deliberate: exclusions are warnings about names that look like momentum but aren't, and they print even when `--regime-gate strict` suppresses the Top-N — so the user always sees them.

Excluded entries in JSON carry only the score-stage fields plus the new vol-collapse fields: `ticker`, `score`, `score_rank` (immutable score-based rank), `pre_filter_rank` (= `score_rank` at exclusion time; redundant but explicit), `rank: null` (display rank is meaningless), `return_pct`, `max_dd_pct`, `from_high_pct`, `ann_vol_pct` (annualized over the *full* scoring window — distinct from `vol_first_pct` / `vol_second_pct` which are the half-window slices), `vol_first_pct`, `vol_second_pct`, `vol_ratio`. Sector, pullback, and ATR fields are computed only on kept top-N picks to save HTTP; excluded entries don't get them.

#### Short-window caveat

At `--window-months 1`, each half has only ~10 returns (the `MIN_RETURNS_PER_HALF` constant); std estimates at this size are noisy and the filter can flag legitimate names. The script prints a `Warning` to stderr at any `--window-months < 2`. The recommended minimum for reliable filtering is 2 months.

#### Lifecycle of a vol-collapse-excluded ticker

Because excluded names are NOT saved to history, a ticker appears in the **Dropouts since last run** section *exactly once* — on the first run after the filter starts excluding it (when it was in the prior run's saved top-N but isn't this run). The dropout line gets a `*filtered by vol-collapse this run*` annotation. On subsequent runs the ticker is absent from both prior and current top-N (because it's still excluded and was never saved), so it stops appearing in Dropouts. It does, however, continue to appear in the **Excluded by vol-collapse filter** section every day the pattern persists — so that's where you'd look for "what's the status of MASI" after the initial dropout. Once the vol-collapse pattern resolves (deal closes, stock delists, or vol genuinely normalizes), the ticker returns to the top-N (assuming it still passes the score filter) with **its pre-exclusion history intact**: `prev_rank` and `first_seen` point to the last-saved appearance, not the immediately-previous run. `streak` starts fresh at 1 because the run-id chain was broken during the exclusion gap (the streak counter walks backward through run_ids and stops at the first run missing the ticker). The ticker is **not** flagged as a new entrant — `prev_rank` is populated from the old history rows.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly.

1. **Read the Regime banner first.** SPY above a *rising* 200DMA with breadth above ~60% is where long-momentum has historically had the cleanest risk/reward. RISK-OFF banners — SPY below 200DMA, the 200DMA itself rolling over, or breadth collapsing while SPY still holds up — flip the read: treat the names below as *who's holding up* in a weak tape, not *what to buy*. Be explicit that the filter doesn't defend against the post-bear momentum crashes (2009 Q2, 2020 Q2, early 2023) — those happen right after the gate turns back on, when prior leaders get sold to fund the rotation into the bombed-out cohort. The filter helps with bear-market downside, not with the regime-flip itself.

2. **Sector clusters beat individual names.** Momentum arrives as a theme (AI infra, semis, defense, lithium, etc.). Group the top 10–15 by sector and call out the cluster — that's what the user can research, hedge, or fade. New entrants joining an existing cluster confirm the theme; isolated newcomers in unrelated sectors are more likely noise.

3. **Streak ≥ 4 and top-5 dropouts are the real signals.** Long streaks have survived multiple periods of market noise — these are the durable trends the rank score alone can't surface. The Persistent leaders section uses a `≥ 3` threshold by default to surface emerging stickiness early; bump `--persistent-min-streak 4` to filter to only the high-conviction names. A name leaving the top 5 usually marks a broken trend (max drawdown blew through the filter) and is often the leading edge of a regime shift.

4. **Vol-target the cohort, not individual names.** When `--target-vol-pct` is set, lead with the cohort vol and suggested leverage — that's the actual antidote to the post-bear momentum crash the trend filter misses. A cohort vol drifting up while leverage drops from 1.0x to 0.4x is the vol-target system *working* — it deleverages into the storm. The per-name Weight% column is useful but secondary; emphasize the leverage number.

5. **Lead with the pullback Sig column when calling out candidate names.** When pullback is enabled (default), the `Sig` column is the single most decision-relevant cell per row — use it as a *research-priority filter*, not a buy signal:
   - **🟢** — top priority for research today; setup geometry tends to pair with the tighter end of the cohort's Stop% range (RSI cooling usually coincides with realized-vol cooling, which compresses ATR).
   - **🔵** — also actionable but harder to confirm: a deep pullback in a still-trending name can be the best risk/reward setup *if* the long-term trend is intact, or the leading edge of a broken trend. Cross-check `Streak ≥ 3` and sector stability before recommending.
   - **🟠** — research the underlying, set a price alert at MA20, don't initiate today.
   - **🔴** — skip; add to watchlist. Stop% is usually wider on high-vol breakouts, but a 🔴 triggered by RSI alone on a low-vol name (e.g., a defensive sector pop where ATR stayed small) can still have a tight stop — always read Stop% per row rather than assuming it from the Sig color.

   A cohort dominated by 🔴 (typical of late-stage momentum runs after several uninterrupted up-weeks) is itself a signal — the cohort as a whole is overextended and even healthy ones will get sold in a market wobble. When the cohort flips to mostly 🟢/🔵/🟡 it usually means the broader correction already happened and survivors have rebased.

6. **When ATR stops are on, use Stop% as the buy-time risk number.** With `--atr-stop-mult 2.5`, Stop% is typically `-5 to -10%` for sedate names (UNH, large-cap utilities) and `-15 to -22%` for high-vol breakouts (ARM, INTC during a +150% run). The wide range reflects that ATR is name-specific — that's the *point*, not a bug. That % is the *per-trade* max loss if you enter at current price and the stop holds. For dollar-position sizing, `shares = risk_per_trade / (mult × ATR)` — independent of (and complementary to) the vol-target Weight% column, which sizes by *portfolio* risk. The TrailStop on persistent-leader names (streak ≥ `--persistent-min-streak`) is the more important number for already-running positions: it answers "where would I cut this without giving back the gain". Stops moving up week-over-week is the trend confirming itself.

7. **When sectors are on, lead with the cluster, not individual names.** The `**Sectors**` line is the most user-actionable single piece of info — Tech 12 of 23 means the cohort is concentrated and the vol-target Weight% column is understating true portfolio risk (correlated names). If the top sectors are clearly one cluster (e.g. AI infra: Tech + parts of Comm Svc), say so explicitly. Diversifying across sectors with the same total leverage is almost always better than chasing the highest-Score name.

8. **Never recommend specific buys.** Frame results as "names worth investigating", not "you should buy". Always flag that momentum strategies carry multi-year underperformance risk — 2023 was a textbook momentum crash where the 2022 leaders (energy) lost to a completely different cohort (mega-cap tech) for the entire year.

## State files

- `state/history.csv` — one snapshot per US market day (America/New_York) × every top-N ticker. Columns: `run_id, run_date, ticker, rank, score, return_pct, max_dd_pct, ann_vol_pct, from_high_pct`. Re-running the same ET day overwrites that day's rows (newer prices replace older), so streak counts scan-days rather than scan invocations. Writes are atomic (tmp file + rename) so a crash mid-write can't truncate the file. The whole point of the skill is to build this up over time — first run is informationally thin; the skill gets more useful with each subsequent run.
- `state/universe.txt` — cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json` — per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker. Fetched lazily on the top-N picks (not the full universe) so the cache grows organically as different names cycle through leadership. Deleting the file forces a clean refresh on next run.

If the user wants to start fresh, `--clear-history` wipes only `history.csv` (no confirmation prompt — pair with `git` if irreversibility matters). The universe cache regenerates automatically.

Storage growth: at default `--top-n 30`, each run adds ~30 rows × ~120 bytes ≈ 3.6 KB. A year of daily runs is ~1.3 MB, weekly is ~190 KB. Negligible for years of typical use; if it ever matters, prune by `run_date` with any CSV tool.

Tests for the history I/O live next to the script at `scripts/test_history.py`. Run from the skill root with:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  --with 'pytest' pytest scripts/
```

## Cadence

Cadence-agnostic by design. At most one snapshot is kept per US market day (America/New_York), so streak always counts **consecutive prior scan-days** containing this ticker — running multiple times on the same ET day just refreshes that day's entry. Aligning to ET date instead of UTC matches what the underlying data represents (US market sessions) and behaves predictably across DST transitions. Runs on weekends or NYSE-observed market holidays are auto-skipped from history so streak doesn't inflate from duplicate-data days; results still print, just nothing is appended. Pre-market runs on a real trading day **do** save — today is a real trading day from the streak's perspective regardless of run time. Override the guard with `--save-stale`. To retroactively clean up snapshots saved on non-trading days (pre-guard, or after `--save-stale`), run `--prune-non-trading-days`. The `FirstSeen` dates tell you the natural granularity:

- Daily runs → streak unit is days. Finest granularity but typically adds limited extra signal over weekly.
- Weekly runs → streak unit is weeks. Recommended sweet spot — captures trend formation 4× faster than monthly while smoothing daily noise.
- Monthly runs → streak unit is months. Smoothest, slowest signal; matches the cadence of the original backtest in this conversation.

For automatic recurring runs, use a local scheduler (macOS `launchd` LaunchAgent, or `cron`) pointed at `scripts/scan.py`. The `schedule` skill runs *remote* agents in Anthropic-managed sandboxes that can't see this local `state/` directory, so it doesn't fit this use case.

## Known limitations

- **Survivorship bias** — universe is current US large caps; delisted names (Lehman, SVB, etc.) are absent. Backtested CAGR is 1–2% optimistic vs. a true point-in-time universe.
- **Pre-cost** — no transaction costs, slippage, or taxes modeled. Real execution shaves another ~0.5–1% CAGR.
- **mcap floor at $5B** — small-cap moonshots are excluded by default; bump `--min-market-cap 1e9` to widen if the user wants to see them.
- **3mo window is noisier** — fresher-breakout signals come with more single-week pops; bump `--window-months 6` for smoother trends if needed.
- **Yahoo data quirks** — rare missing bars, occasional late dividend adjustments. If a single name looks wrong, sanity-check it via the `yfinance` skill's `fast_info` mode.
- **Trend-filter limits** — `--regime-gate` reduces *bear-market* downside but can't catch the regime-flip momentum crash itself (2009/2020/2023); the slope check defuses single-bar whipsaws but two consecutive months of choppy 200DMA crossings can still flip the verdict twice. The `Breadth` figure is the % of the current ~1000-large-cap universe above its own 200DMA — that's a tech-tilted read of internals, not a true market-wide A/D line.
- **Vol target ignores correlations** — `--target-vol-pct` uses the equal-weight cohort's *realized* portfolio vol (which does capture correlations implicitly in the historical basket return) but the per-name `Weight%` allocation uses `1/ann_vol_i` *as if names were independent*. When the top-N is dominated by one cluster (e.g., AI infra), true portfolio risk is higher than the weights imply. Counter by lowering `--target-vol-pct` (treat 15% as if it were 10% when the cohort is concentrated), or by manually capping per-name weight. The 60-day SMA lookback is also lagging — vol regime shifts show up with a delay; an EWMA would react faster but adds a tunable.
- **Vol-target lookback is fixed at 60 trading days, independent of `--window-months`** — the per-name `AnnVol%` in the table is annualized over the *scoring* window (3mo by default, 6mo if you bump it), but the cohort vol driving the leverage calculation is always the last 60 trading days. This is intentional: the literature (Daniel-Moskowitz, Barroso-Santa-Clara) converges on ~60d as the regime estimator that best predicts the next-period momentum crash. If you bump `--window-months 6`, expect to see the per-name vol numbers drift while the cohort vol / leverage stay anchored to the shorter view.
- **ATR stop is per-trade, not portfolio** — `--atr-stop-mult` sizes one position's loss-on-stop; it doesn't account for correlated drawdowns across the top-N. If 20 names share a sector cluster and the cluster sells off, all stops can fire together for a portfolio-wide loss far above any single Stop%. Combine with `--target-vol-pct` (portfolio sizing) for both axes of protection. The 14-day SMA ATR also lags — fast regime changes (gap-down opens, news shocks) blow through computed stops; treat Stop% as a *risk budget*, not a guarantee.
- **Pullback Sig is a buyability filter, not a directional forecast** — 🟢/🔵 means "if you wanted to buy this name, this is a reasonable entry"; it does *not* mean the name will go up next. Equally, 🔴 doesn't mean "sell" — it means "don't initiate a new long here". On strong-trending names, the Sig can sit in 🔴 for weeks without the price actually correcting (RSI stays > 70 in a true uptrend); the Trend Pullback edge is *probability* of a better entry showing up, not certainty. RSI thresholds (40-55 buy zone, > 80 overext) are conventional literature defaults that behave well on liquid US large-caps. When `--min-market-cap` is lowered, the failure modes cut both ways. Small-caps and biotech routinely run RSI > 90 in single-stock squeezes without mean-reverting (false 🔴). Illiquid small-caps can sit at MA20 with RSI 50 for weeks without going anywhere (false 🟢) — the indicator detects setup *geometry*, not price follow-through. Lookback is 20 trading days for MA and 14 for RSI — fixed, not exposed as flags.
- **Sectors are Yahoo, not GICS** — `state/sectors.json` mirrors yfinance's `Ticker.info.sector` / `.industry`, which closely approximates GICS but isn't identical (e.g., Yahoo uses "Technology"/"Financial Services" where GICS uses "Information Technology"/"Financials"). The abbreviation map handles the common variants; new labels fall through as the first 10 chars of the raw string. Some tickers (ADRs, recent IPOs) return empty sector strings and show as `—` in the table; they don't contribute to the `**Sectors**` breakdown count.
- **Universe pagination has a hard stop at `SCREENER_MAX_PAGES` (20 pages = ~5000 tickers)** — this only matters if Yahoo's response stops including the `total` field (schema drift), in which case the script falls back to per-page heuristics (short page / zero new tickers) to detect end-of-results. The 20-page cap is the absolute backstop; if it ever triggers you'll see `refresh_universe: hit SCREENER_MAX_PAGES=20 backstop` on stderr and the universe will be capped at ~5000 — well above any realistic large-cap match count, so triggering it is a strong signal something is wrong upstream.
- **Universe size affects historical rank comparability** — if you raise `--universe-count` (or the underlying universe grows from market-cap drift) between runs, ranks recorded in `state/history.csv` from before the change aren't directly comparable to ranks after. A larger universe means more names can pass the filter, which can demote previously-high-ranked names not because their fundamentals weakened but because new entrants were added to the pool. `Streak` and `FirstSeen` survive (a ticker is still "in the top N" or not), but `RankΔ` across a universe-size change should be read with that caveat. If you want clean before/after comparison, run `--clear-history` after changing universe size.
- **Vol-collapse filter sensitivity depends on where the gap day lands in the window** — `--vol-collapse-ratio` compares the realized vol of the window's two halves (mid = `n // 2` of the daily-return series). The filter works cleanly when the announcement gap falls in the **first** half: `v1` gets inflated by the gap-day return plus normal pre-deal trading, `v2` is post-event pin, ratio → ~0.02, name excluded. It **fails** when the gap day lands in the second half, because the single huge gap return dominates whichever half it's in:
  - **Recent announcements**: a deal announced ≤ halfway into the window puts the gap day in the second half. Result: `v2` inflated by the gap, `v1` is normal pre-deal trading, ratio > 1, name passes through. *Failure mode is late detection, not silent miss* — by the next run (or definitely within a few weeks) the gap drifts into the first half and the filter catches it.
  - **Longer windows**: with `--window-months 6` and a ~3-month-old announcement, the gap lands right at the half boundary and typically falls into the second half. The 6mo scan can leak a name the 3mo scan filters cleanly. Cross-checked empirically on MASI (Feb 17 announcement, May 14 scan): 3mo window excludes (`vol 98% → 2%, ratio 0.02`); 6mo window does *not* exclude (gap is in second half, MASI happens to fall below the 6mo top-30 cut for unrelated reasons but a similarly-structured higher-return buyout would leak into the table).
  - **Mitigations**: if you suspect a leak, the lock-in fingerprint is **`MaxDD%` improbably small** (typically `> -3%`, sometimes `> -1%`) for a name that's also +30% / +50% / +100% over the window. `FromHigh%` of 0.0 *alone* isn't suspicious — it's the default state for any momentum leader making fresh highs. It becomes diagnostic only in combination: `FromHigh% = 0.0` **AND** `MaxDD% > -3%` is the literal price-pin fingerprint (the stock is sitting at the deal price with essentially no daily noise). Cross-check by chaining the `yfinance` skill — the **smoking guns** are `sec_filings --type PREM14A,DEFM14A` (the "M" stands for Merger and confirms a definitive deal); `sec_filings --type 8-K,DEFA14A` is suggestive but not conclusive (8-Ks cover many events, DEFA14A is supplementary proxy materials that may or may not relate to a merger).

  **False-positive direction**: names that gapped on an *earnings beat* and then drifted very calmly higher (e.g., a defensive utility post-strong-quarter) can compress 2nd-half vol enough to trip the filter. Lower `--vol-collapse-ratio` toward 0.15 to reduce these. The 5%-annualized minimum first-half vol guard is a constant (not a flag) to prevent already-low-vol names from being flagged on noise; bumping `--min-market-cap` doesn't change this. The filter doesn't catch tender-offer situations that *don't* involve a single-day gap (e.g., a slow accumulation of shares at small premiums) — those produce a normal-looking price chart with no anomaly to detect.
