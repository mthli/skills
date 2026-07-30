---
name: unusual-options-scan
description: "Scan US large-cap equities for unusual options activity — Vol/OI spikes, far-OTM short-DTE accumulation, extreme call/put skew, total options notional outsized vs equity ADV. Use when the user wants smart-money options positioning, possible M&A / catalyst leaks, or a 'follow the flow' watchlist. Triggers on 'unusual options activity', 'UOA', 'options flow', 'huge call buying', 'put volume spike', 'options sentiment'. Cross-day OI-growth confirmation kicks in once 2+ daily runs of history exist. Do NOT use for real-time intraday sweep alerts (need a paid feed), single-ticker chain inspection (use the yfinance skill), or Greeks / GEX analysis."
---

# unusual-options-scan

Find US equities with **unusual options activity** in today's snapshot — contracts whose volume blew past their open interest, far-OTM short-dated lottery tickets being accumulated, extreme call/put skew, or total options notional outsized relative to the stock's average dollar volume. Surface a daily watchlist of the most anomalous tickers, and once 2+ runs of history exist, cross-reference yesterday's flags to show **which prior anomalies actually became real positions** (OI grew the next day) vs which were closed out same-day (noise).

The natural complement to the sister scans:
- `momentum-scan` finds **what's already running** in the stock price
- `base-breakout-scan` finds **what's about to run** in the stock price
- `mean-reversion-scan` finds **what just got punched but is structurally fine**
- `unusual-options-scan` finds **where someone is positioning ahead of a move** — the options market often telegraphs intent (catalysts, M&A, earnings) before the stock price moves

**The big idea**: institutions don't usually buy 10,000 OTM calls on a quiet name "for fun". When Vol/OI on a contract is 5× and the trade is concentrated in short-dated OTM strikes, there's a non-trivial chance someone knows or strongly suspects something. The edge is **not in following every flag** — most resolve to nothing — but in **scanning a few hundred names daily and treating persistent anomalies (OI grows, repeat appearances) as a lead-generation funnel** for further research.

⚠️ **What the 2026-05→07 outcome backtest actually found** (see **Backtested outcomes**): flagged names as a pool *underperformed* the universe (−0.9% excess at T+5, −2.4% at T+10) while moving ~26% more in absolute terms, and the direction of the flow (calls vs puts) carried **zero** directional information about the stock. Read the list as a **volatility/attention detector and a de-risking prompt** — never as a buy list.

**What this is not**: a real-time sweep detector. yfinance gives us end-of-day chain snapshots — no time & sales, no bid/ask side classification, no block-trade tape. Paid feeds (Unusual Whales, Cheddar Flow, Polygon, CBOE LiveVol) are the right tool for that. This skill answers the daily question "where did unusual activity show up today, and which of yesterday's flags persisted?"

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2`, `numpy>=1.24,<3`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run — top ~150 most-liquid US large caps, nearest 2 expiries
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/scan.py

# Tighter Vol/OI gate (high-conviction only)
... python <SKILL_DIR>/scripts/scan.py --min-vol-oi 5 --min-contract-vol 1000

# Scan only nearest expiry (faster, more focused on imminent catalysts)
... python <SKILL_DIR>/scripts/scan.py --num-expiries 1

# Wider universe (slower — ~3 expiries × 500 tickers × ~1s each = several minutes)
... python <SKILL_DIR>/scripts/scan.py --universe-count 500

# Inspect history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# Machine-readable JSON
... python <SKILL_DIR>/scripts/scan.py --format json

# Don't append today's run to history (one-off exploration)
... python <SKILL_DIR>/scripts/scan.py --no-save

# Outcome backtest: replay state/history/*.md and measure what the flags
# predicted in the underlying stock (see "Backtested outcomes" section)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILL_DIR>/scripts/backtest_outcomes.py             # information content (close entry)
... python <SKILL_DIR>/scripts/backtest_outcomes.py --entry next-open  # realistic execution
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--min-vol-oi` | 3.0 | Minimum `volume / open_interest` ratio for a contract to qualify as an anomaly candidate. Below 2 is too noisy; above 5 is high-conviction but thin. Open interest is yesterday's EOD figure (refreshed overnight), so a freshly-listed strike with `OI = 0` is filtered separately via `--min-contract-vol`. |
| `--min-contract-vol` | 500 | Absolute volume floor per contract. Discards the long tail of low-liquidity strikes where Vol/OI looks dramatic but the underlying notional is small. Raise to 1000 for high-conviction only; lower to 200 in small-cap-inclusive runs. |
| `--min-contract-notional` | 50000 | Per-contract dollar notional floor (`volume × last_price × 100`). A 5000-contract Vol/OI spike on a $0.05 contract is still only $25k notional — not interesting. This filter cuts ~half the noise from `--min-contract-vol` alone. |
| `--num-expiries` | 2 | Number of nearest expirations to scan per ticker. yfinance fetches each expiration separately (no batch endpoint), so this directly affects wall-clock time. 1 is fastest (~50% time) but misses anomalies in the 2nd-month contract; 3+ adds time without much marginal signal (most UOA clusters in the front two months). |
| `--max-dte` | 60 | Skip expirations more than this many days out. Long-dated LEAPS show structurally different activity (sized hedging, less catalyst-driven) and dilute the signal. |
| `--far-otm-pct` | 10 | Minimum % distance from spot for a contract to count as "far OTM" for the lottery-ticket signal. 10% OTM with < 30 DTE is the canonical informed-buying pattern. |
| `--cp-ratio-extreme` | 3.0 | Per-ticker total call-volume / total put-volume ratio above this (or below `1/ratio`) flags directional skew. Combined with the notional/ADV filter, this catches names where the options tape is screaming a direction. |
| `--notional-adv-mult` | 0.5 | Flag tickers whose total options notional today ≥ this multiple of the stock's average daily dollar volume (20-day). 0.5 = options notional ≥ half the equity ADV — a high bar; institutional options flow this large vs the equity tape usually has a reason. |
| `--top-n` | 30 | How many anomalous tickers to display + log to history. Each ticker may contribute multiple contract rows. |
| `--min-market-cap` | 5e9 | Universe market-cap floor. yfinance options coverage is best on US-listed large caps; small-cap chains are spotty and often have 1-2 strike clusters that won't trigger anyway. |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | (all matches) | Universe size pulled from Yahoo's screener. Default unset = pull every match (~1000 US large caps at default mcap/volume floors). The screener returns at most 250 rows per request, so larger values are paginated automatically. **However, the scan iterates each ticker individually for options data** (no batch endpoint), so wall-clock scales linearly with universe size. Use 250-500 for full daily runs; 1000+ only when you have time. |
| `--refresh-universe` / `--no-refresh-universe` | (TTL 7d) | Force-refresh / use cache regardless of age. |
| `--show-history` | — | Print history summary; no new scan. |
| `--clear-history` | — | Wipe `state/history/*.md`. |
| `--no-save` | — | Don't write today's snapshot to history. |
| `--allow-same-day` | — | Append even if a row exists for today's ET date. Default overwrites. |
| `--format` | markdown | `markdown` or `json`. |
| `--max-workers` | 16 | Parallel threads for per-ticker options-chain fetches. yfinance is thread-safe for reads; 16 is a reasonable default that avoids rate-limiting. Lower to 4-8 if you see HTTP 429s. |

## Output shape

A funnel summary line, params recap, regime banner (informational only — no gating), then the top-N table grouped by ticker. Each ticker's row carries its single highest-conviction contract; tickers with multiple flagged contracts get a `+N more` annotation. Cross-day signals (OI growth confirmation, repeat-offender streak) populate once 2+ days of history exist. Per-name options-volume baseline-vs-history is not yet computed and is noted under Known limitations. Sample (illustrative — picks change daily):

```
Funnel: 1035 universe → 487 with options coverage → 312 with contract-level flag → 47 tickers (post min-vol-oi/notional) → 30 top-N

# Unusual options activity — 2026-05-22 22:14 UTC

**Params**: min_vol_oi=3.0, min_contract_vol=500, min_notional=$50k, num_expiries=2, max_dte=60
**Universe**: 1035 tickers · **Flagged**: 47 (30 displayed) · **Prior runs**: 8
**Regime**: SPY 742.3 (informational only — UOA signal not regime-gated)

## Top 30

| # | Ticker | Sector | TopContract | Vol | OI | Vol/OI | Notional | DTE | %OTM | Flags | CP% | Notnl/ADV | OIΔ | Streak |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **PLTR** | Tech | $40C 06-20 | 18,420 | 2,103 | 8.8 | $1.84M | 28 | +14% | ⚡🎯 | 4.2 | 1.83× | +312% ✅ | 3 |
| 2 | **MARA** | Tech | $32C 05-30 | 12,015 | 845 | 14.2 | $0.96M | 7 | +18% | ⚡🎯🔥💰 | 7.8 | 2.41× | +540% ✅ | 2 |
| 3 | **WBA** | Health | $11P 06-20 | 8,200 | 1,520 | 5.4 | $0.41M | 28 | -8% | ⚡ | 0.21 | 0.92× | 🆕 | 1 |
| 4 | **DKS** _(+3 more)_ | Cons Cyc | $235C 05-30 | 4,180 | 502 | 8.3 | $0.55M | 7 | +6% | ⚡🔥💰 | 5.1 | 1.18× | +15% ≈ | 1 |
...

## Cross-day OI confirmation — vs 2026-05-21 (1 day ago)

_18 of yesterday's flags re-joined: 5 ✅ strong growth · 4 ≈ partial · 9 ❌ closed-out_

### ✅ Strong growth (OI ≥ +20%) — position built and held (5)

| Ticker | Contract | YDay Vol | YDay→Today OI | Spot vs YDay |
|---|---|---|---|---|
| **PLTR** | $40C 06-20 | 18,420 | 2,103 → 8,672 (+312%) | $35.10 → $35.62 (+1.5%) |
| **MARA** | $32C 05-30 | 12,015 | 845 → 5,408 (+540%) | $27.05 → $27.40 (+1.3%) |
...

### ≈ Partial growth (OI +5% to +20%) — mixed; some retained, some closed (4)

| Ticker | Contract | YDay Vol | YDay→Today OI | Spot vs YDay |
|---|---|---|---|---|
| **DKS** | $235C 05-30 | 4,180 | 502 → 580 (+15%) | $228.10 → $232.40 (+1.9%) |
...

### ❌ Closed out (OI < +5%) — day-trade churn, not accumulation (9)

| Ticker | Contract | YDay Vol | YDay→Today OI | Spot vs YDay |
|---|---|---|---|---|
| **AMD** | $200C 05-30 | 5,200 | 1,820 → 1,790 (-2%) | $193.40 → $194.10 (+0.4%) |
...

## Repeat offenders (3+ days)
- **PLTR $40C 06-20** — flagged 3 days running. Each day's vol > 10k; OI grew 2,103 → 8,672 → 18,500. Sustained accumulation.
```

Column / flag meanings:

- **TopContract** — the single highest-Vol/OI contract on this ticker that passes all filters. `$40C 06-20` = $40 strike call expiring June 20.
- **Vol** — today's contract volume.
- **OI** — yesterday's EOD open interest (yfinance refreshes overnight). Vol/OI compares today's flow to the pre-existing position base.
- **Notional** — `volume × last_price × 100` (per-contract dollar size of today's flow).
- **DTE** — calendar days to expiration.
- **%OTM** — `(strike / spot - 1) × 100` for calls; `(spot / strike - 1) × 100` for puts. Positive = OTM. Far-OTM short-DTE concentrations get the 🎯 flag. The C/P letter in TopContract carries the side, so no separate Side column.
- **Flags** — anomaly tags on this ticker (single contract may carry multiple):
  - **⚡ Vol/OI spike** — the contract-level signal that originally surfaced this row.
  - **🎯 far-OTM lottery** — `%OTM ≥ --far-otm-pct AND DTE ≤ 30`. Concentrated informed-buying pattern. ⚠️ Backtest: no directional edge (signed excess ≈ 0), and the strikes are lottery-priced — 10–15% OTM calls touched their strike before expiry 25% of the time, ≥25% OTM just 5% (**Backtested outcomes** #7).
  - **🔥 short-DTE squeeze candidate** — DTE ≤ 10 with high notional and far-OTM strike. The most catalyst-imminent pattern; also the most likely to expire worthless — backtest-quantified: 83% of far-OTM short-DTE calls never touch the strike.
  - **📊 directional skew** — ticker C/P ratio above `--cp-ratio-extreme` (or below `1/ratio`). ⚠️ Backtest: non-discriminating — call-skewed and put-skewed tickers both underperformed (−0.65% vs −1.09% xT+5), no direction gradient (**Backtested outcomes** #8).
  - **💰 notional/ADV outlier** — ticker total options notional ≥ `--notional-adv-mult × equity ADV`. ⚠️ So rare it's untestable: 13 ticker-days in 2.3 months (**Backtested outcomes** #8).
- **CP%** — ticker-level call_volume / put_volume ratio (using all today's contracts on this ticker, not just flagged ones). > 3 = call-heavy; < 0.33 = put-heavy.
- **Notnl/ADV** — ticker total options notional ÷ stock 20-day average dollar volume. > 1 means options notional today exceeded equity ADV (large institutional positioning).
- **OIΔ** — change in OI on this contract vs the most recent prior snapshot (only meaningful if this exact contract appeared in that snapshot — i.e. cross-day confirmation). `✅` = OI grew ≥ +20% (positions built and held). `≈` = OI grew +5% to +20% (mixed; some retained, some closed). `❌` = OI grew < +5% (day-trade churn, position closed out). `🆕` = no prior snapshot for this contract. ⚠️ Backtest: ✅ rows did *not* outperform ❌ rows in forward stock returns (−0.32% vs −0.14% signed xT+5) — read the tier as position-persistence evidence, not expected return (**Backtested outcomes** #6).
- **Streak** — number of consecutive prior runs this exact contract has been flagged. ⚠️ Backtest: no edge at any streak depth (**Backtested outcomes** #6).

The **Cross-day OI confirmation** section distinguishes real position-building from day-trade churn — yesterday's anomalies broken into three tiers (✅ strong / ≈ partial / ❌ closed-out) by how their OI evolved overnight, plus the spot drift over the same window. ✅ rows mean a position was genuinely built and held; ❌ rows were closed out same-day. The section header reflects the **actual prior date** (may be "1 day ago" or e.g. "3 days ago" if sessions were skipped — common after a long weekend). ⚠️ The backtest could not validate the tiers as a *return* signal: ✅ contracts did not out-predict ❌ ones for the underlying's forward move (**Backtested outcomes** #6). The tiers tell you the position is real; they do not tell you it's right.

The **Repeat offenders** section surfaces contracts flagged ≥ 3 days running with persistent OI growth. ⚠️ Backtest: streak depth carried no forward-return edge (Beat5 = 49% at every depth) — treat as a "someone is persistent here, find out why" research trigger, not conviction (**Backtested outcomes** #6).

## Backtested outcomes (2026-05-23 → 2026-07-29 sample)

`scripts/backtest_outcomes.py` replays every flagged contract in `state/history/*.md` and measures the *underlying stock's* forward returns at T+1/T+5/T+10 vs the equal-weight universe mean on the same session (excess), with call-flagged flow signed as a bet up and put-flagged as a bet down. Measured on 7,193 resolved ticker-days (28k contract rows, 817 tickers, 40 snapshots) over a mostly RISK-ON tape. This scan has no trade convention, so the backtest measures **information content**, not fills. Findings, strongest first:

1. **The flagged pool as a whole underperforms — this is not a buy list.** All flagged ticker-days: raw T+5 **−0.62%** vs universe **+0.26%** (excess −0.88%); T+10 −1.96% vs +0.44% (**excess −2.40%**); only 47% positive at T+5. Robust to entry timing (`--entry next-open`: excess −0.87% at T+5). Anomalous options activity in EOD data marks *attention and froth*, not smart-money accumulation.
2. **Flags predict movement, not direction.** Flagged names moved **|5.9%|** on average over 5 sessions vs |4.6%| for the universe (+26%) — the "something is about to move here" premise is real. But the *side* of the flow says nothing about which way: ≥90%-call ticker-days ran −0.76% xT+5, ≥90%-put −0.92%, mixed −1.26% — **no call→put gradient at all**. (Put-heavy flow "predicting" a fall is just the pool's general underperformance.) EOD volume can't distinguish opening buys from closing sells or covered writes — and it shows.
3. **Cluster size is inverted vs the old doctrine.** 1 flagged contract: −0.58% xT+5 · 2–4: −0.94% · 5–9: −1.10% · **10+: −2.01% (only 38% positive)**. A whole flagged strike-ladder in EOD data marks a name the whole market is speculating on, not a quiet institutional sweep. Read `+N more` as a *froth multiplier*, not conviction.
4. **Chronic residents are the worst pocket; episodic appearances the only clean one.** Names in >67% of snapshots: −1.41% xT+5 / −4.23% xT+10. Names in ≤10% of snapshots: −0.13% xT+5 — statistically flat, the only bucket that isn't a drag. The known per-ticker-baseline limitation is now quantified: **the scan's noise lives almost entirely in the chronic names** (TSLA/NVDA-class perma-flagged tickers).
5. **Post-rip flags are a fade tell.** Flags on names up >+10% in the prior 5 sessions: **−2.36% xT+5, −5.92% xT+10, |T+5| = 8.9%, 38% positive** — the single worst cell in the sample. Post-dump names (≤−5%) also lag (−0.96% xT+5). Even flat-entry names underperform mildly (−0.45%), so the drag isn't *only* reversal chasing — but chasing a rip that options flow "confirmed" was the most reliable way to lose.
6. **Persistence signals didn't validate.** Cross-day OI confirmation (4,504 re-flagged joins): ✅ built-and-held ran −0.32% signed xT+5 vs ❌ closed-out −0.14% — the tiers separate *position persistence*, not returns. Contract streaks: 0 / 1 / 2+ days → −0.12% / −0.19% / −0.24%, 49% Beat5 everywhere — "repeat offenders" carried zero edge. (Caveat: the replay can only join contracts re-flagged the next day; the live scan's join against full chains — the "vol normalized, OI held" case — remains untested.)
7. **🎯 lottery strikes hit at lottery rates.** Touch-the-strike-before-expiry: calls 10–15% OTM **25%**, 15–25% OTM 13%, ≥25% OTM **5%**; puts 10–15% OTM 35%, ≥15% 18%. When a touch happens it happens fast (median 2–3 sessions). No directional edge in the underlying either (signed excess ≈ 0). The pattern's value, if any, is as a catalyst-research pointer — not as a trade.
8. **The remaining flags are rare or dead.** 💰 (notional ≥ 0.5× ADV) fired on 13 ticker-days in 2.3 months — untestable; its contract-level short-horizon hint (79% Beat5 at T+5, n=125) reverses by T+10, so treat as unproven. **🔥+💰 — the canonical "catalyst-imminent" combo — never co-occurred even once.** 📊 skew: confirmed non-discriminating (#2's gradient test).

**Caveats**: one ~2.3-month window, one regime; ticker-days cluster heavily (one flow episode → many rows, all names share each session's tape — excess-vs-universe removes the day effect, nothing removes the episode effect); the flagged pool structurally skews to high-vol names, so part of the underperformance may be a style effect of this particular tape (the flat-pre-move bucket's −0.45% is the cleanest read of the flow-specific component); chronicity is computed full-sample (approximate in live use with trailing appearance counts); current-universe baseline adds survivorship optimism; bucket cutoffs chosen in-sample. Re-run quarterly — especially #1's sign (a froth-marker in a RISK-ON tape may behave differently in a selloff) and #4's chronic-name drag.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly. **Where the backtest above and the flag doctrine disagree, lead with the backtest and say so.**

1. **Frame the list as an attention/volatility detector, never as a buy list.** The validated read (**Backtested outcomes** #1, #2): these names will likely move more than the market (+26% more absolute movement over 5 sessions) while *underperforming* it on average (−0.9% xT+5, −2.4% xT+10), and the call/put side of the flow tells you nothing about direction. Two legitimate uses: a research funnel for "why is someone positioning here?", and a **de-risking prompt** — if the user *holds* a name that shows up with a big cluster after a +10% week, that specific cell ran −2.4% xT+5 / 38% positive in-sample (#5).
2. **Discount chronic residents; prioritize episodic appearances.** A name in the list most days (TSLA/NVDA-class) is structural options-casino volume — the worst-performing pocket (#4). A name that almost never appears and suddenly does is the only bucket that held up. Check the Streak column and your memory of recent runs; when in doubt, `--show-history` shows which tickers are perma-residents.
3. **The "Cross-day OI confirmation" section separates real positions from churn — not winners from losers.** ✅ tier = the position was genuinely built and held (the ❌ class is confirmed same-day churn and safely ignorable). But ✅ did not out-predict ❌ for the stock's forward move (#6), so present ✅ rows as "confirmed positioning worth a research dig", never as "smart money says up/down".
4. **A big flagged cluster is froth, not conviction.** Inverted from the original doctrine: 10+ flagged contracts on one name ran −2.0% xT+5 (#3). A whole strike ladder lighting up in EOD data means the crowd is there. Single-contract flags on episodic names are the more interesting anomaly.
5. **🎯/🔥 are catalyst-research pointers with lottery odds.** Touch rates before expiry: ~25% at 10–15% OTM, 5% at ≥25% OTM (#7). When both fire on a no-news name, the right move is checking the earnings calendar / FDA dates / M&A rumor flow — not extrapolating a direction (signed edge ≈ 0). Note the canonical 🔥+💰 "imminent catalyst" cluster has never actually occurred in the recorded history (#8).
6. **📊 and 💰 are context columns, not signals.** 📊 skew is confirmed non-discriminating; many liquid names sit at C/P 2–3 chronically. 💰 is so rare (13 ticker-days in 2.3 months) that its claimed strength is untested — if it fires, treat it as "unusually large absolute flow, verify by hand", nothing more (#8).
7. **Cross-check for known catalysts BEFORE treating as informed flow.** Earnings, FDA dates, M&A rumors, sector-wide hot themes (post-news call buying is normal). Use the `yfinance` skill's `calendars.py` for earnings dates; check news headlines manually. Most "unusual" activity has a mundane explanation — the goal is to surface the small subset that doesn't.
8. **Never recommend specific options trades.** Options are leveraged + decay-sensitive; flagging a contract as "unusually active" is not a recommendation to buy it. The watchlist surfaces *underlying tickers* worth investigating; any actual options trade requires the user's own Greeks/sizing/timing analysis. The backtest makes this concrete: even the average *stock* behind these flags lost to the market, and 83% of the far-OTM call strikes were never touched.

## State files

- `state/history/YYYY-MM-DD.md` — one markdown file per scan day (ET). Contains today's full anomaly table (not just top-N) keyed by `(ticker, expiry, strike, type)`. The format is human-readable + git-diffable + parseable by tomorrow's run for the cross-day join. No SQLite or parquet — at ~30-300 rows/day, markdown is sufficient and trivially inspectable.
- `state/universe.txt` — cached universe list, auto-refreshed every 7 days via Yahoo's screener.
- `state/sectors.json` — per-ticker `{sector, industry, ts}` cache. 30-day TTL.

Storage growth: ~50-300 rows × ~200 bytes ≈ 10-60 KB/day. A year of daily runs ≈ 4-20 MB. Markdown parsing of the previous N days takes < 100ms.

## Cadence

**One scan per US market day, after the close.** Open interest only refreshes overnight (it's an EOD figure), so intraday re-runs would show today's accumulating volume against yesterday's OI — meaningful but not the canonical "EOD Vol/OI" the heuristics were calibrated on. The OI-growth confirmation specifically requires yesterday's snapshot vs today's *fresh-overnight* OI; running before 6pm ET will compare today's intraday-so-far volume against stale figures.

Recommended cadence: **daily, 6pm ET or later**. Weekly cadence loses the OI-growth confirmation signal entirely (you'd be comparing 7-day-old OI to today's, which is useless). Monthly is meaningless for this style.

For automated runs use a local scheduler (macOS `launchd` or `cron`) pointing at `scripts/scan.py`. The `schedule` skill runs *remote* agents that can't see this local `state/` directory.

## Known limitations

- **End-of-day snapshots only.** No intraday flow, no time & sales, no sweep detection, no bid/ask side classification. yfinance gives one snapshot of the chain after the close. For real-time UOA / sweep alerts, use a paid feed (Unusual Whales, Polygon, Cheddar Flow, CBOE LiveVol).
- **OI is yesterday's EOD figure.** Throughout the trading day, "Vol/OI" compares today's accumulating volume against yesterday's position base. Yahoo refreshes OI overnight (typically before 9pm ET), so running at 6pm ET gets today's final volume against yesterday's OI; running the next morning would get today's volume against today's just-refreshed OI (cleaner). Both produce usable signals; the difference matters mainly for the OI-growth confirmation step, which needs the *next day's* OI to verify positions were actually built.
- **No IV percentile.** Single-day IV without baseline isn't very informative — a name's IV being "high" requires knowing where it normally sits. Building IV percentile requires accumulated history we don't yet store (v0 skips this; could add by extending the markdown snapshot to include a per-ticker ATM IV column, then computing percentile after ~60 daily snapshots).
- **Total options volume baseline requires accumulated history.** The `Notnl/ADV` metric uses *equity* ADV (which we get from OHLCV) as the denominator. A cleaner signal would be "options notional vs this ticker's own 20-day options notional baseline" — which we don't have until 20+ daily runs accumulate. Currently the proxy (vs equity ADV) catches most outliers but misses cases where this name has *chronically* high options notional (the actual baseline is high too, so today isn't really anomalous).
- **No dealer-positioning / GEX analysis.** Gamma exposure metrics require either OPRA full-tape data or assumptions about dealer hedging behavior. Out of scope for daily snapshots; mentioning this because users coming from MenthorQ / SpotGamma may expect it.
- **yfinance options coverage is mostly US large-cap equities + major ETFs.** Foreign primary listings (e.g., `0700.HK`), futures, FX, mutual funds, and most ADRs return empty option chains. The universe-load already filters to US large caps so this isn't a practical issue, but expect some tickers to silently drop out at the "with options coverage" funnel step.
- **Vol/OI on a brand-new strike is undefined.** When OI = 0 (strike was added today), Vol/OI is infinity. We surface those rows separately under the contract-volume floor (`--min-contract-vol` still applies) and label them `OI=new` in the table; they're real signals but uncomparable to the ratio for established strikes.
- **No vol-collapse / M&A-lock filter.** Sister skills use a vol-collapse filter to catch acquisition-target equities pinned at cash buyout prices. UOA scans don't need this filter because *the activity itself* on a locked name typically dries up — once shares stop moving, options stop trading. But a name in the very early stages of a leaked deal (pre-announcement, options buying ahead of news) is exactly what this skill is designed to find — and would be visible as a 🎯🔥 + 💰 cluster. (Note: that exact cluster has never fired in 2.3 months of recorded history — it's a theoretical pattern, not a recurring one.)
- **No fundamental check.** Pure flow analysis; doesn't know if the company has known earnings tomorrow, FDA next week, etc. Always cross-check the earnings calendar before treating a flag as "no public catalyst → likely informed".
- **No baseline of normal Vol/OI per ticker.** Some names (TSLA, NVDA) chronically run Vol/OI > 1 across half their chain because of high speculative volume. The default `--min-vol-oi 3` filters most of this, but very actively-traded names will surface frequently with marginal signal. The 2026-05→07 backtest quantified the damage: names appearing in >67% of snapshots were the worst-performing pocket (−1.41% xT+5), while ≤10% episodic appearances were the only clean bucket (**Backtested outcomes** #4). That makes the per-ticker appearance/Vol-OI baseline (z-score from the markdown history) the highest-value future enhancement — it would both de-noise the top-N and sharpen the one bucket that works.
