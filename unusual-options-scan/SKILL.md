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
  - **🎯 far-OTM lottery** — `%OTM ≥ --far-otm-pct AND DTE ≤ 30`. Concentrated informed-buying pattern.
  - **🔥 short-DTE squeeze candidate** — DTE ≤ 10 with high notional and far-OTM strike. The most catalyst-imminent pattern; also the most likely to expire worthless.
  - **📊 directional skew** — ticker C/P ratio above `--cp-ratio-extreme` (or below `1/ratio`).
  - **💰 notional/ADV outlier** — ticker total options notional ≥ `--notional-adv-mult × equity ADV`.
- **CP%** — ticker-level call_volume / put_volume ratio (using all today's contracts on this ticker, not just flagged ones). > 3 = call-heavy; < 0.33 = put-heavy.
- **Notnl/ADV** — ticker total options notional ÷ stock 20-day average dollar volume. > 1 means options notional today exceeded equity ADV (large institutional positioning).
- **OIΔ** — change in OI on this contract vs the most recent prior snapshot (only meaningful if this exact contract appeared in that snapshot — i.e. cross-day confirmation). `✅` = OI grew ≥ +20% (positions built and held). `≈` = OI grew +5% to +20% (mixed; some retained, some closed). `❌` = OI grew < +5% (day-trade churn, position closed out). `🆕` = no prior snapshot for this contract.
- **Streak** — number of consecutive prior runs this exact contract has been flagged.

The **Cross-day OI confirmation** section is the load-bearing signal — yesterday's anomalies broken into three tiers (✅ strong / ≈ partial / ❌ closed-out) by how their OI evolved overnight, plus the spot drift over the same window. ✅ rows are the real positioning signals; ❌ rows are noise candidates that closed out same-day; ≈ rows are ambiguous (some institutional positioning was retained but not all). The section header reflects the **actual prior date** (may be "1 day ago" or e.g. "3 days ago" if sessions were skipped — common after a long weekend).

The **Repeat offenders** section surfaces contracts flagged ≥ 3 days running with persistent OI growth — the highest-conviction subset.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly.

1. **The "Cross-day OI confirmation" section is more important than today's top-N.** Day-1 flags are candidates; day-2 OI growth (✅ tier) turns a candidate into a real signal. Lead with the ✅ rows when history exists; the ❌ rows are themselves informative as the *negative* class (yesterday's "big trade" turned out to be intraday churn, not accumulation).

2. **A single flagged contract is much weaker than a cluster.** When a ticker shows 4-5 flagged contracts (e.g., `$40C, $42C, $45C` all flagged the same day), that's a sweep-pattern across a strike ladder — much more institutional than a single concentrated trade. The `+N more` annotation in the TopContract column hints at this; the JSON output exposes the full per-contract list.

3. **🎯 + 🔥 together (far-OTM + short-DTE) is the catalyst-imminent pattern.** When both flags fire on calls, someone is betting on an upside catalyst within days. When on puts, downside. Cross-check with the earnings calendar and news — if there's no public catalyst, that's exactly when the flag is most informative.

4. **📊 directional skew alone is weak.** Many liquid names sit at C/P = 2-3 chronically (e.g., TSLA, NVDA — perpetually call-heavy). The directional skew flag only matters when CP% jumps materially vs that ticker's normal — which requires history this skill doesn't yet maintain. Treat 📊 as confirming, not initiating.

5. **💰 notional/ADV > 1.5× is the strongest *single-day* aggregate signal.** When today's options dollar flow on a name exceeds the equity's 20-day avg dollar volume by 50%+, that's an institutional-scale position being built that day. Especially noteworthy on names where equity ADV is in the $100M+ range — getting options notional to half that bar is uncommon without a thesis.

6. **OI growth ≠ price direction.** OI confirmation tells you a position was built; it does NOT tell you the position is right. Plenty of well-positioned trades lose. Frame the watchlist as "names where someone is positioning aggressively — worth a research dig", not "names that will go up".

7. **Cross-check for known catalysts BEFORE treating as informed flow.** Earnings, FDA dates, M&A rumors, sector-wide hot themes (post-news call buying is normal). Use the `yfinance` skill's `calendars.py` for earnings dates; check news headlines manually. Most "unusual" activity has a mundane explanation — the goal is to surface the small subset that doesn't.

8. **Never recommend specific options trades.** Options are leveraged + decay-sensitive; flagging a contract as "unusually active" is not a recommendation to buy it. The watchlist surfaces *underlying tickers* worth investigating; any actual options trade requires the user's own Greeks/sizing/timing analysis.

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
- **No vol-collapse / M&A-lock filter.** Sister skills use a vol-collapse filter to catch acquisition-target equities pinned at cash buyout prices. UOA scans don't need this filter because *the activity itself* on a locked name typically dries up — once shares stop moving, options stop trading. But a name in the very early stages of a leaked deal (pre-announcement, options buying ahead of news) is exactly what this skill is designed to find — and would be visible as a 🎯🔥 + 💰 cluster.
- **No fundamental check.** Pure flow analysis; doesn't know if the company has known earnings tomorrow, FDA next week, etc. Always cross-check the earnings calendar before treating a flag as "no public catalyst → likely informed".
- **No baseline of normal Vol/OI per ticker.** Some names (TSLA, NVDA) chronically run Vol/OI > 1 across half their chain because of high speculative volume. The default `--min-vol-oi 3` filters most of this, but very actively-traded names will surface frequently with marginal signal. Future enhancement: per-ticker Vol/OI z-score using the markdown history.
