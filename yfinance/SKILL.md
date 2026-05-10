---
name: yfinance
description: >
  Fetch Yahoo Finance market data via the yfinance library. Use for stock,
  ETF, mutual fund, index, crypto, futures, or FX questions — current
  quotes, historical OHLCV (daily / intraday), and company / fund
  fundamentals (P/E, dividend yield, sector, analyst targets, AUM, etc.).
  Triggers include "current price", "YTD performance", "N-year chart",
  "P/E ratio", "compare/rank tickers", "what does X do", and non-US
  tickers like 0700.HK or BMW.DE.
---

# yfinance

Python scripts wrapping [yfinance](https://github.com/ranaroussi/yfinance),
one per mode:

- `scripts/fast_info.py` — current quote
- `scripts/history.py` — historical OHLCV; full bars or `--summary` aggregates
- `scripts/info.py` — profile + fundamentals + analyst; full grouped sections or `--summary` flat dict
- `scripts/earnings.py` — upcoming + recent earnings dates with EPS estimates / actuals / surprise
- `scripts/financials.py` — annual / quarterly / TTM income statement, balance sheet, and cash flow
- `scripts/news.py` — recent Yahoo Finance news headlines per ticker
- `scripts/holders.py` — insider / institutional ownership rollup + top-10 institutional and mutual-fund holders
- `scripts/options.py` — option chain (calls + puts for one expiry); pair with `--moneyness` for an ATM-band slice + ATM/PCR summary
- `scripts/insiders.py` — Form 4 insider transactions (last ~24 mo) + 6-month buy/sell rollup + current roster
- `scripts/analyst.py` — analyst recommendations time series (0m / -1m / -2m / -3m bucket counts) + per-event grade-change feed with embedded price-target moves
- `scripts/screener.py` — market-wide discovery. Run a Yahoo predefined screen (`day_gainers`, `undervalued_growth_stocks`, `top_etfs_us`, …) or a custom AND/OR query over fields like `intradaymarketcap`, `peratio.lasttwelvemonths`, `epsgrowth.lasttwelvemonths`. Returns up to 250 quotes per call. **Only mode that produces tickers from a filter rather than starting from a known ticker.**
- `scripts/fund_holdings.py` — ETF / mutual-fund holdings. Top-10 positions, sector / asset / bond-rating weightings, expense ratio + AUM, fund-level P/E / P/B / duration. **Fund-only** (equity / index / crypto / FX / future return success-with-note carrying the resolved `quote_type` — no follow-up `fast_info` chain needed). One HTTP per ticker covers all 9 sections.
- `scripts/sec_filings.py` — SEC filings list (10-K / 10-Q / 8-K / DEF 14A / 20-F / 6-K / SC 13G/A / etc.) with date, type, title, primary doc URL, and exhibits dict. Up to ~75–120 filings going back ~3 years per ticker. Coverage is **SEC-registered securities only**: US-listed equities and ADRs (TM = 6-K + 20-F) get full data; non-US primary listings (BMW.DE, 0700.HK), ETFs, mutual funds, indexes, crypto, FX, futures, and bogus tickers all return success-with-note (ambiguous — chain `fast_info` to disambiguate). No `coverage_note` partial-empty path — the SEC-filings endpoint is binary.

Shared NaN/Inf-safe converters live in `scripts/helpers.py`. A
`scripts/smoke.py` test exercises all thirteen wrappers against
representative tickers — run after editing schema or when yfinance API
drift is suspected:
`uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/smoke.py`
(the `lxml` extra is needed only by `earnings.py`'s HTML scrape; harmless
for the others). All scripts use `argparse` (run with `--help` for inline
usage) and run under `uv` so yfinance installs into a managed temp env, no
side-effects on the user's system Python.

This SKILL.md is the router: pick a mode below, copy the invocation, then
**Read the matching `references/<mode>.md` for the field schema, full CLI flags,
output examples, presentation guidance, and mode-specific caveats**. The
reference files are loaded only when needed so the entry doc stays small
as functionality grows.

## When to use which mode

> **Quote-type precondition:** `fast_info`, `history`, and `news` work
> for any ticker (stocks, ETFs, indexes, crypto, futures, FX). `info` is
> meaningful only for `quote_type` ∈ {`EQUITY`, `ETF`, `MUTUALFUND`} —
> for indexes / crypto / futures / FX it returns mostly null, so don't
> waste the call. `earnings` and `financials` are **equity-only** (no
> quarterly EPS or income statement for ETFs / indexes / crypto); both
> short-circuit non-equities to empty lists with a `note`. `holders` is
> also effectively equity-only (Yahoo's holders endpoint covers
> operating companies; ETFs / indexes / crypto / FX / futures all
> return three empty DataFrames), but unlike earnings / financials it
> doesn't pre-screen — it returns success-with-`note` for the empty
> case (the empty result is **ambiguous**: non-equity / bogus /
> low-coverage equity all look identical). `options` mirrors the
> `holders` shape: equity / ETF / a subset of US-listed ADRs only;
> indexes / crypto / FX / futures / non-US primary listings / mutual
> funds all return success-with-`note` and an empty `expirations`
> array — same ambiguity, same `fast_info` chain to disambiguate.
> A second, rarer `note` covers the case where `expirations` is
> populated but Yahoo returned an empty chain for the requested date
> (try a different expiry from the array). `sec_filings` mirrors the
> `holders` shape: ETFs / mutual funds / indexes / crypto / FX /
> futures / non-US primary listings (`BMW.DE`, `0700.HK`) / bogus all
> return empty — `{}` from yfinance, not a list — surfaced as
> success-with-`note` (ambiguous; chain `fast_info` to disambiguate).
> No `coverage_note` partial-empty path — the SEC-filings endpoint
> is binary. **ADRs (verified `TM`) ARE covered** (foreign issuers
> file 6-K / 20-F instead of 10-K / 10-Q / 8-K), distinct from
> non-US **primary** listings which aren't SEC-registered at all.
> `insiders` mirrors the
> `holders` shape with one twist: ETFs / indexes / crypto / FX /
> futures / bogus tickers all return three empty frames
> (success-with-`note` — same ambiguity, same `fast_info` chain to
> disambiguate), but a few real equities (verified `BMW.DE`, `TM`)
> return `purchases_summary` populated and `transactions` + `roster`
> empty — that's partial Yahoo coverage of non-US per-event filings,
> not ambiguity, surfaced via a separate `coverage_note` field
> (mutually exclusive with `note`) so the asymmetry is visible
> in-band rather than silently shaped like real activity. `analyst`
> mirrors the `insiders` shape: ETFs / indexes / crypto / FX / futures
> / bogus tickers all return both frames empty (success-with-`note`,
> ambiguous — chain `fast_info` to disambiguate); non-US **primary**
> listings (verified `0700.HK`, `BMW.DE`) get `recommendations`
> populated but `upgrades_downgrades` empty (Yahoo's grade-change
> feed is US-centric) — that's the partial-empty path with
> `coverage_note` set, mutually exclusive with `note`. **ADRs
> (verified `TM`) still get full coverage** because they trade on
> US exchanges. Both `fast_info` and `info` return `quote_type`
> explicitly when you're unsure. `fund_holdings` is the **inverse-shape
> sibling** of `holders` / `insiders` / `analyst`: it's **fund-only**
> (ETF / MUTUALFUND). Equity / index / crypto / FX / futures all raise
> `YFDataException` inside yfinance and surface as success-with-`note`
> here — but unlike `holders` / `insiders` / `analyst` (which leave
> the caller to chain `fast_info` for disambiguation), `fund_holdings`
> captures the resolved `quote_type` from yfinance's parser state and
> includes it inline on the note response. There's no `coverage_note`
> partial-empty path because the endpoint is binary (you're a fund or
> you're not). Bogus tickers route through the standard `error_kind:
> not_found` path (HTTP 404 at the network layer, before the parser
> sees the response — so `quote_type` is null on that path).
>
> **`screener` doesn't take a ticker** — it's the inverse direction
> (filter spec → ticker list). The `--quote-type {equity,fund,etf}`
> flag picks the **target set** the screen filters against (Yahoo's
> `quoteType` for custom queries; predefined screens have it baked
> in). Chain into per-ticker modes via `--format symbols | xargs`.

| Question shape | Mode | Why |
|---|---|---|
| "what's X trading at", "quote X", "current price" | `fast_info` | latest snapshot |
| "market cap", "shares outstanding", "currency" | `fast_info` | static fields |
| "52-week high/low", "200-day MA" | `fast_info` | fixed windows yfinance precomputes |
| "today's change", "X up or down today" | `fast_info` | `change_pct` is today vs prev close |
| "YTD performance", "1-year return", "how much did X gain" | `history --summary` | period-bounded, gives `change_pct` over chosen window |
| "period high/low and when it happened" | `history --summary` | `period_high_date` / `period_low_date` |
| "dividends paid last quarter / year" | `history --summary` | `total_dividends` over period |
| "show me last N days / weeks", "plot the chart" | `history` (default) | full OHLCV rows |
| "intraday last 5 days" | `history --interval 1h` (or `5m`/`15m`) | tick-level rows |
| "after-hours price right now", "pre-market gap after earnings" | `history --interval 5m --prepost` | extended-hours bars |

> "S&P 500 P/E" or any index-fundamental question isn't answerable through `info` (or the other modes) — yfinance has no fundamentals for indexes; you'd need a different data source.

| Question shape | Mode | Why |
|---|---|---|
| "P/E", "P/B", "PEG" | `info` | `valuation` section |
| "what does X do", "sector", "industry" | `info` | `profile` section |
| "profit margin", "ROE", "revenue growth", "EPS" | `info` | `fundamentals` section |
| "dividend yield", "payout ratio", "ex-div date" | `info` | `dividend` section (mind the unit caveat) |
| "analyst target price", "buy/sell rating" | `info` | `analyst` section |
| "ETF expense ratio", "AUM", "fund category" | `info` | `fund` section (ETFs / mutual funds only) |
| "compare 3+ tickers", "peer screen", "rank by P/E / yield" | `info --summary` | flat per-ticker dict (~10× smaller than default) for table rendering |
| "when does X report next", "next earnings date" | `earnings --summary` | `next_date` is the upcoming-quarter datetime (preserves AMC/BMO timing) |
| "did X beat last quarter", "earnings surprise" | `earnings --summary` | `last_surprise_pct` (percent; positive = beat) |
| "X's earnings history", "EPS trend over last N quarters" | `earnings --past-only` | full reported quarters with estimate/actual/surprise |
| "which of these consistently beats" | `earnings --summary` | `beat_rate_last_4` across multiple tickers |
| "consensus EPS / revenue forecast", "next quarter estimate", "analyst high / low / # analysts", "FY consensus", "estimate revisions / trend", "stock vs market growth", "long-term growth (LTG)" | `earnings --estimates` | full analyst panel: consensus avg/low/high, 90-day estimate trend, 7d/30d revision counts, broad-market benchmark, LTG. Per-period rows for `0q` / `+1q` / `0y` / `+1y`. ADRs split EPS (USD) and revenue (home ccy) into separate currency fields. See references/earnings.md. |
| "recent IPO with no past reports", "ticker hasn't reported earnings yet" | `earnings --estimates` | **IPO fall-through.** The same flag also rescues IPOs from the default `not_found`: when the calendar scrape returns empty but the analyst panel has data, the response is success with `earnings_dates: []`, `timezone: null`, and a `coverage_note` explaining the empty calendar. Default `earnings` (without `--estimates`) returns `error_kind: not_found` with a hint pointing at this flag. |
| "what's the analyst rating / price target on X" | `info` (analyst section) | `info.analyst.target_mean_price`, `recommendation_key` — static current consensus. For **time series** of how consensus has shifted, or per-event grade changes, see `analyst` below |
| "has consensus shifted on X", "any rating upgrades / downgrades recently", "who upgraded / downgraded X", "did Morgan Stanley raise their target", "consensus drift over last 3 months" | `analyst` | recommendations time series (0m / -1m / -2m / -3m bucket counts) + per-event grade-change feed (`upgrades_downgrades`) with embedded price-target moves (`Raises` / `Lowers`). 977+ rows for major US large-caps going back to ~2012 — use `--limit` or `--summary`. Complements `info.analyst` (static snapshot) and `earnings --estimates` (EPS forecasts). |
| "compare analyst sentiment across tickers", "rank by % buy / consensus score", "net upgrades last 90d" | `analyst --summary` | flat per-ticker dict: `total_analysts_current`, `buy_pct_current` / `buy_pct_change`, `consensus_score_current` (1=strong_buy ... 5=strong_sell — comparable to `info.recommendation_mean`), 90-day rollups (`upgrades_last_90d`, `target_raises_last_90d`), latest event |
| "income statement", "balance sheet", "cash flow", "revenue/FCF trend over N years" | `financials` | per-statement period lists; scope with `--statement income\|balance\|cashflow` |
| "latest quarter", "QoQ revenue", "TTM trailing twelve months" | `financials --period quarterly\|ttm` | ~5–7 most-recent quarters; `ttm` = 1-row rollup (income + cashflow only) |
| "compare 3+ tickers' revenue / FCF / margins / growth" | `financials --summary` | flat per-ticker dict + period-over-period growth (`*_growth_yoy`) |
| "what's the latest news on X", "recent headlines", "what's driving X today" | `news` | up to ~10 articles per ticker; works for all quote types; use `--limit` to tighten |
| "latest 10-K / 10-Q / 8-K of X", "recent SEC filings", "any 8-Ks lately" | `sec_filings --type 10-K --limit 1` (or `--type 8-K --limit 5`, etc.) | up to ~75–120 filings ~4 years back (row-bounded, not time-bounded); `--type` is case-insensitive; use `--limit` to cap |
| "ADR annual report", "Toyota 20-F", "foreign-issuer interim filings" | `sec_filings --type 20-F,6-K` | ADRs file 6-K + 20-F (not 10-Q/10-K); non-US primary listings (`BMW.DE`, `0700.HK`) return empty (not SEC-registered) |
| "filings since 2024", "8-Ks in last 30 days", "what's filed lately" | `sec_filings --since YYYY-MM-DD` or `--days N` | date floor on filings; combine with `--type` for "8-Ks last 30d" type questions; mutually exclusive flags (`--since` is ISO date, `--days` is rolling-N convenience) |
| "compare filing activity across tickers", "rank by recent 8-Ks", "who's most active in SEC filings" | `sec_filings --summary` | flat per-ticker dict: `total_filings`, `latest_*_date` per headline type (10-K / 10-Q / 8-K / 20-F / 6-K / proxy), `filings_last_90d` recency count |
| "who owns X", "top institutional holders", "% insider / institution ownership" | `holders` | rollup pcts + top-10 institutional + top-10 mutualfund holders, one Yahoo call total |
| "compare ownership concentration across tickers", "top-5 institutional %" | `holders --summary` | flat per-ticker dict + `top5_institutions_pct` concentration signal |
| "top mutual-fund holders of X", "which Vanguard / iShares funds hold X" | `holders` | mutualfund section. Mostly broad index trackers; specialist active funds rarely surface |
| "is the CEO buying / selling", "did insiders buy this dip", "Form 4 activity", "current insider holdings", "how many shares does the CEO own" | `insiders` | three sections in one HTTP: 6-month buy/sell rollup (`purchases_summary`, all `pct_*` are FRACTIONS), per-event Form 4 transactions (`transactions`, last ~24 months, name / position / shares / value / date), current roster (`roster`, with direct + indirect holdings — sort by `shares_owned_directly` desc to rank) |
| "net insider buying across tickers", "rank by insider sentiment", "who's the largest insider holder" | `insiders --summary` | flat per-ticker peer compare: `net_shares_purchased` / `pct_net_shares_purchased`, `latest_transaction_date` (recency), `top_insider_by_direct_shares` + `top_insider_direct_shares` |
| "AAPL options chain", "TSLA puts near-the-money", "NVDA implied vol" | `options --moneyness 5` | nearest expiry's calls + puts, strikes within ±5% of spot. Default fetch is 1 HTTP per ticker (cheap) — cost doubles when you pin `--expiry` |
| "AAPL call options expiring 2026-06-19", "TSLA Jan 2027 puts" | `options --expiry YYYY-MM-DD` | specific expiry; bad date returns `error_kind: not_found` with available list to pick from |
| "compare ATM IV across NVDA / AMD / AVGO", "put-call ratio", "options sentiment" | `options --summary --moneyness 5` | flat per-ticker dict: ATM call/put IV, total volume, PCR by volume and OI; `moneyness_pct` echoed for self-describing output |
| "what expirations does X have" | `options` | the `expirations` array is always populated on success; default fetch is the nearest expiry |
| "what does SPY hold", "top holdings of QQQ", "what stocks are in VTI" | `fund_holdings` | top 10 positions with weights — the unique capability vs `info` (which has no holdings detail). ETF / mutual fund only; equity / index / crypto / FX / future return success-with-note carrying the resolved `quote_type`. Use `--limit N` for top-N |
| "what's the expense ratio of X", "AUM of SPY", "category of QQQ" | `fund_holdings` | `operations` (expense ratio, turnover, AUM in MILLIONS of fund-reporting currency) + `fund_overview` (category / family / legal type). `info` has a leaner `fund` section nominally covering the same headline fields, but its coverage is patchier — verified 2026-05: `info.fund.expense_ratio` is `null` for SPY while `fund_holdings.operations.expense_ratio` returns `0.000945`. **Prefer `fund_holdings` for fund metadata**; reach for `info` only if you already need other `info` sections. **Cross-mode warning**: `info.fund.total_assets` is in WHOLE units, `fund_holdings.operations.total_net_assets_millions` in MILLIONS — 1e6× scale difference for the same metric (see references/fund_holdings.md "Cross-mode AUM unit drift") |
| "ETF sector breakdown", "asset mix of VFIAX", "stock vs bond %" | `fund_holdings` | `sector_weightings` + `asset_classes` (both fractions). Pure bond funds return `sector_weightings: {}` — use `bond_metrics` (duration / maturity) and `bond_ratings` (per-credit-rating %) instead. `info` has none of these — `fund_holdings` is the only path |
| "P/E of SPY's holdings", "fund-level P/E / P/B", "duration of AGG" | `fund_holdings` | `equity_metrics` (PE / PB / PS / PCF — **inverted from Yahoo's raw `1/ratio` encoding**, surfaced as conventional ratios) + `bond_metrics` (duration / maturity in years). NB: this is fund-level aggregation, NOT a single stock's P/E (use `info` for that) |
| "compare 3 ETFs side by side", "rank ETFs by expense / AUM / concentration" | `fund_holdings --summary` | flat per-fund dict: expense ratio, AUM, top holding + weight, `holdings_concentration` (sum of weights across `holdings_returned` rows — read together; for bond ETFs returning 0–1 rows it's NOT "top-10 concentration"), top sector, P/E, P/B, duration |
| "find me stocks where X", "top gainers / losers today", "undervalued growth stocks", "best ETFs", "screen by P/E + dividend + sector" | `screener` | **only discovery mode** — every other mode starts from a known ticker. Two paths: `--predefined NAME` (19 Yahoo saved screens like `day_gainers`, `undervalued_growth_stocks`, `top_etfs_us`) or `--query JSON` (custom AND/OR tree). Discovery flags: `--list-predefined` (catalog with descriptions), `--list-fields equity\|fund\|etf` (valid fields for custom queries). Output: default JSON envelope, or `--format symbols` for `xargs`-friendly ticker lists, or `--full` for the raw Yahoo payload (~60-85 fields per quote) |

A single user request can need multiple modes. "What's AAPL trading at, how
much is it up YTD, and what's its P/E?" → `fast_info` for the live price,
`history --period ytd --summary` for the YTD return, `info` for the P/E.

**Discovery → details two-step.** When the user wants to find tickers
by criteria *and* drill in, run `screener` first to produce the ticker
list, then chain a per-ticker mode (`info` / `financials` / `analyst`
/ etc.) over the result. `screener` projects ~28 fields per quote —
plenty for ranking and selection, but not the full per-ticker depth.
Treat screener as the funnel, not the destination.

**Catch-all rule.** If the user is open-ended ("tell me about NVDA",
"what's up with TSLA?") and you can't pin them to a row above, default
to `fast_info` first — it's the cheapest and answers "where is it
trading, how big is it, what's the recent range" in one call. Add `info`
only if the user follows up about sector / fundamentals / analyst views,
or if the original question already mentions those.

If the user asks "find me X" / "screen for X" / "top movers" / "best
ETFs" and you don't yet have a ticker list, jump to `screener`
(predefined or custom) — none of the other modes can answer the
discovery shape.

## Invocations

Pick the line you need, then Read the corresponding `references/<mode>.md` for
flags, schema, and caveats.

```bash
# fast_info — current quote (see references/fast_info.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA

# history — historical OHLCV (see references/history.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py AAPL                              # 1mo daily, full rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period ytd --summary AAPL       # aggregate stats
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5d --interval 1h AAPL    # intraday
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1d --interval 5m --prepost AAPL  # extended-hours

# info — profile + fundamentals + analyst (see references/info.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL MSFT                            # full sections
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py --summary AAPL MSFT GOOGL            # peer comparison

# earnings — upcoming + recent earnings dates (see references/earnings.md)
# NOTE: earnings.py needs an extra `--with 'lxml'` (the others don't).
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py AAPL                # 12 rows default
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary AAPL MSFT NVDA  # peer beat-rate
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --estimates AAPL    # + full analyst panel (consensus, trend, revisions, sector, LTG)
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary --estimates AAPL MSFT NVDA  # peer compare incl. consensus_* fields
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --future-only AAPL  # only upcoming

# financials — income / balance / cashflow statements (see references/financials.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py AAPL                                 # all 3 statements, annual
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --period quarterly AAPL              # quarterly statements
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --statement income --period ttm AAPL # TTM income only
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --summary AAPL MSFT GOOGL            # peer headline + YoY growth

# news — recent Yahoo Finance headlines (see references/news.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py AAPL                              # ~10 articles, JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py --limit 3 AAPL MSFT TSLA           # tight scan
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py --format csv --limit 5 AAPL MSFT   # one row per article

# holders — ownership rollup + top institutional / mutual-fund holders (see references/holders.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py AAPL                                  # all 3 sections, JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --summary AAPL MSFT GOOGL             # peer ownership rollup
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --limit 5 AAPL                        # top-5 in each list
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --format csv --summary AAPL MSFT GOOGL  # peer-compare CSV

# options — option chain (calls + puts, ONE expiry) (see references/options.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --moneyness 5 AAPL                    # nearest expiry, ±5% ATM (recommended default)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py AAPL                                  # nearest expiry, FULL ladder (often 30-200 rows/leg)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --expiry 2026-06-19 AAPL              # specific expiry (2 HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --summary --moneyness 5 NVDA AMD AVGO # peer ATM IV / PCR
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --format csv --moneyness 5 AAPL MSFT  # CSV: row per contract

# insiders — Form 4 transactions + 6-month buy/sell rollup + current roster (see references/insiders.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py AAPL                                 # all 3 sections, JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --summary AAPL MSFT GOOGL            # peer net-buying rollup
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --limit 10 AAPL                      # top 10 transactions / roster rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --format csv --summary AAPL MSFT     # peer-compare CSV

# analyst — recommendations time series + per-event grade-change feed (see references/analyst.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py AAPL                                  # both sections, JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --summary AAPL MSFT NVDA              # peer consensus / 90d rollups
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --limit 20 AAPL                       # top 20 grade-change events
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --format csv --summary AAPL MSFT NVDA # peer-compare CSV

# screener — market-wide discovery (see references/screener.md)
# Single-call API: emits ONE envelope dict (not per-ticker array). Up to 250 quotes per call.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined day_gainers --count 10                      # top intraday US gainers
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined undervalued_growth_stocks --count 25         # PE<20 + PEG<1 + EPS growth
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined top_etfs_us --count 10                       # top US ETFs
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --list-predefined                                         # catalog of 19 saved screens
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --list-fields equity                                      # valid fields for custom queries
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --query '{"operator":"and","operands":[{"operator":"eq","operands":["region","us"]},{"operator":"gt","operands":["intradaymarketcap",1e10]},{"operator":"lt","operands":["peratio.lasttwelvemonths",15]}]}' --sort-field intradaymarketcap --count 25  # custom AND/OR tree
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --query @my_query.json --quote-type equity                # custom query from file
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined undervalued_growth_stocks --count 25 --format csv  # CSV: row per quote
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined day_gainers --count 50 --format symbols           # tickers only — pipe into other modes
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined day_gainers --count 1 --full                       # raw Yahoo payload (~60-85 fields)

# fund_holdings — ETF / mutual-fund holdings (see references/fund_holdings.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py SPY                                # all 9 sections, JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --summary SPY VTI QQQ              # peer compare
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --limit 5 SPY                      # top 5 holdings only
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --format csv --summary SPY VTI QQQ # peer-compare CSV

# sec_filings — SEC filings list (see references/sec_filings.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py AAPL                                 # all filings (~75-120), JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --type 10-K,10-Q AAPL                # quarterly + annual (case-insensitive)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --type 8-K --limit 5 TSLA            # last 5 events
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --type 20-F,6-K TM                   # ADR foreign-issuer filings
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --since 2024-01-01 AAPL              # ISO date floor
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --days 30 --type 8-K AAPL TSLA NVDA  # 8-Ks in last 30 days
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --summary AAPL MSFT NVDA             # peer rollup (latest_10k_date, latest_proxy_date, filings_last_90d, ...)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --format csv --limit 5 AAPL          # CSV: row per filing (exhibits dict dropped, exhibit_keys preserved)
```

`<SKILL_DIR>` is the absolute path of the directory containing this
SKILL.md. Substitute it once when running.

## Cost / latency

Rough per-ticker cost: `fast_info` / `news` / `holders` / `insiders` /
`sec_filings` ~0.3–1.5 s (sec_filings is one quoteSummary call —
similar shape to news), `fund_holdings` ~0.7–1.5 s (single quoteSummary
call but fetches 4 modules at once — see references/performance.md),
`history` ~0.5–4 s, `info` / `earnings` / `financials` / `options` /
`analyst` ~1–5 s. **`screener` is per-call, not per-ticker** —
1 HTTP returns ≤ 250 quotes (~1–3 s typical), so the marginal cost
of a wider screen is ~0. (`analyst` makes **3 HTTP per ticker** —
`recommendations`, `upgrades_downgrades`, and `fast_info` for
`quote_type` — each from a different endpoint or module group, so
none share a backend request; the `fast_info` call is the
disambiguator that lets the all-empty `note` path be answered inline
without a follow-up call). All modes are serial (total ≈ N ×
per-ticker) **except `history`** — it batches N≥2 through
`yf.download` in one threaded HTTP call, so 10 tickers cost ~1–2 s
instead of ~5–15 s. `--summary` does
**not** reduce latency; it's a post-fetch projection that only shrinks
output JSON (use it to save context tokens, not time). When a question
is answerable by multiple modes, pick the cheapest — don't call `info`
for a field already in `fast_info` (e.g. `market_cap`); don't call
`financials` for AAPL's P/E (that's in `info`).

A 10-ticker `info` / `financials` / `earnings` batch (~15–30 s) is the
most common path to trip Yahoo's 429 rate-limit. Drop batch size to ~5
and pause between calls if you see retries — `attempts > 1` on a result
flags a retry happened. **For the full per-mode latency table, retry
worst-cases, the `earnings --estimates` cumulative-sleep math, and
`options` 1-HTTP vs 2-HTTP rules, see references/performance.md.**

## Setup

Scripts run under `uv run` (Python 3.9+ required, no global pip
install). `earnings.py` and `smoke.py` additionally need `--with 'lxml'`.
See references/setup.md for the `uv` install one-liner, the lxml
explanation, and the version-pin rationale.

## Cross-cutting caveats

These apply to all thirteen modes. Mode-specific caveats live in the
matching `references/<mode>.md`. Grouped into three concerns:

### Data formats (interpreting the numbers)

- **Numeric units are not consistent across — or within — modes.** Don't
  guess a number's unit from the field name. Quick reference:
  - `fast_info.change_pct` and `history --summary.change_pct` → **percent**
    (`16.43` means 16.43%).
  - `info` margins / growth / returns / payout ratios → **fractions**
    (`0.272` means 27.2%, multiply ×100 for display).
  - `financials --summary.*_growth_yoy` → **fractions** (matches `info`'s
    encoding; `0.064` means 6.4%). Disambiguated from `info.revenue_growth`
    (Yahoo TTM-based) by the `_yoy` suffix — both can co-occur with
    different values, see references/financials.md.
  - `earnings --estimates[*].eps_growth` / `revenue_growth` /
    `index_growth` → **fractions** (matches `info` and `financials
    --summary` conventions; `0.2043` means 20.43% YoY). Inside the same
    `earnings` response the older `earnings_dates.surprise_pct` is still
    **percent** — see references/earnings.md "Mode-specific caveats"
    for the rationale. `index_growth` is **the same number for every
    ticker globally** (verified across US sectors AND HK / Frankfurt /
    Tokyo / KOSPI / London listings) — it's a Yahoo-internal global
    benchmark, not locale- or sector-aware. Don't read it as a HK-listed
    ticker getting Hang Seng growth or as sector-specific.
  - `earnings --estimates` ADR currency split: `eps_currency` and
    `revenue_currency` are separate fields, **not** duplicates. For ADRs
    (TM, PBR) Yahoo reports per-share EPS in the trading currency (USD)
    but revenue in the home reporting currency (JPY, BRL). Always read
    both — don't assume one currency for the row.
  - `holders.summary` (`insiders_pct`, `institutions_pct`,
    `institutions_float_pct`) and per-holder `pct_held` / `pct_change`
    → **fractions** (matches `info`'s fraction-encoded margins;
    `0.0971` = 9.71% of shares held). Easy mistake to make when sliding
    from a percent-encoded mode (`fast_info`, `history --summary`).
    `insiders.purchases_summary.pct_net_shares_purchased` /
    `pct_buy_shares` / `pct_sell_shares` follow the SAME fraction
    convention (`0.001` = 0.1%, verified empirically: AAPL net=246332 /
    total_held=240872640 ≈ 0.00102) — easy to misread because the row
    label in Yahoo's source carries a `%` sigil that looks like
    "already percent". Multiply ×100 for display.
    `holders.summary.institutions_count` is an **integer count**
    (thousands to tens of thousands for US large-caps; present in both
    default-mode JSON and `--summary` flat dict), distinct from
    `--summary`-mode-only `institutional_rows_returned` / `mutualfund_rows_returned`
    (rows actually returned in this fetch, ≤ 10). Easy to misread one
    for the other if you skim — see references/holders.md for the
    naming-collision rationale.
  - `analyst --summary.buy_pct_current` / `buy_pct_oldest` /
    `buy_pct_change` → **fractions** (matches `info` and `holders`
    fraction conventions; `0.65` = 65% buy-or-better, multiply ×100
    for display). `analyst --summary.consensus_score_current` /
    `consensus_score_oldest` are on **Yahoo's 1-5 Likert scale**
    (1 = unanimous strong_buy, 5 = unanimous strong_sell, lower is
    more bullish) — directly comparable to `info.analyst.recommendation_mean`
    so consumers can swap data sources without converting. Don't
    accidentally render the Likert scale as a percentage.
    `analyst.upgrades_downgrades[*].current_price_target` /
    `prior_price_target` → floats in the **trading currency** (=
    `fast_info.currency`); USD for AAPL and ADRs (`TM`); 0.0 is
    Yahoo's "no target" sentinel and projects to null. The two
    enum fields have **inconsistent case** (Yahoo's quirk):
    `action ∈ {up, down, main, init, reit}` (lowercase),
    `price_target_action ∈ {Raises, Lowers, Maintains, Announces,
    Adjusts}` (capitalized) — exact-match comparisons need to
    respect both cases.
  - `options[*].change_pct` → **inferred percent** (Yahoo's
    `percentChange` field; sibling `regularMarketChangePercent` in
    the same options API payload is verified percent-encoded, but
    per-contract values are uniformly 0.0 off-hours so direct
    confirmation is pending — see references/options.md "Mode-specific
    caveats" for the verification status). `options[*].implied_vol`
    and `options --summary`'s `atm_call_iv` / `atm_put_iv` →
    **fractions** (`0.25` = 25% IV, verified). Two unit conventions
    in one row — easy to swap. Cheat sheet: `change_pct: 5.2` ≈ 5.2%
    daily move; `implied_vol: 0.25` ≈ 25% annualized IV. `pcr_volume`
    / `pcr_oi` are dimensionless ratios (>1 = more put activity than
    call). **`options.currency` (top-level)** is the underlying /
    trading currency (= `fast_info.currency`); per-contract
    `contract_currency` is renamed from Yahoo's `currency` field to
    avoid a CSV header collision — in observed payloads the two
    always match. **`options --summary.moneyness_pct`** echoes the
    user's `--moneyness` arg (None when unset), so a peer-compare
    CSV mixing filtered and unfiltered runs stays self-describing.
    **Sentinel values:** any `implied_vol < 1e-3` is Yahoo's
    "couldn't compute" placeholder (treat as missing, not 0.1%);
    `bid: 0.0`, `ask: 0.0`, and `open_interest: 0` across an entire
    chain are off-hours sentinels (Yahoo zeroes them when US market
    is closed); `total_*_volume` / `total_*_oi` are `null` (not 0)
    when every row's value is None — see references/options.md.
  - `info` yield-and-fund-return fields are a **mix** (full table in
    references/info.md "Unit landmines"). Percent-encoded:
    `dividend.five_year_avg_dividend_yield`, `fund.ytd_return`.
    Fraction-encoded: `dividend.trailing_annual_dividend_yield`,
    `fund.three_year_avg_return`, `fund.five_year_avg_return` (CAGR).
    (Yahoo's percent-encoded `dividend_yield` was the worst offender —
    we drop it from the schema entirely; use `trailing_annual_dividend_yield`.)
  - `fund_holdings` exposes a **mixed unit zoo** that the script
    normalizes for callers — read references/fund_holdings.md "Units"
    before consuming raw values:
    - `equity_metrics.pe_ratio` / `pb_ratio` / `ps_ratio` / `pcf_ratio`
      (and their `_category_avg` companions) → conventional multiples
      (we **invert Yahoo's raw `1/ratio`** encoding; verified SPY P/E
      raw 0.03706 → 26.98).
    - `equity_metrics.median_market_cap` (and `_category_avg`) →
      **MILLIONS** of fund-reporting currency (verified VFIAX = 404537
      ≈ \$404B), `safe_int`-coerced.
    - `equity_metrics.earnings_growth_3y` (and `_category_avg`) →
      **FRACTION** (we **divide Yahoo's raw percent by 100**; verified
      VFIAX raw 18.03 → 0.1803). Matches `info.fund.three_year_avg_return`
      / `financials --summary.*_growth_yoy` conventions.
    - `bond_metrics.duration_years` / `maturity_years` (and
      `_category_avg`) → years.
    - `operations.expense_ratio` / `turnover` /
      `asset_classes.*_pct` / `sector_weightings.*` / `bond_ratings.*`
      / `top_holdings[*].weight` → fractions.
    - `operations.total_net_assets_millions` → **MILLIONS** of
      fund-reporting currency. **Cross-mode landmine**:
      `info.fund.total_assets` reports the same metric in **whole
      units, not millions** (verified SPY: info = 735060819968 ≈
      \$735B vs fund_holdings = 479387.62 millions ≈ \$479B; values
      can also drift between modes due to different snapshots). Pick
      one mode and stick with it; if mixing, normalize explicitly.

  Sanity heuristic: any yield > 1.0 is the percent variant; any 3y/5y
  avg-return < 0.5 is the fraction (CAGR) variant. When in doubt, prefer
  `trailing_annual_dividend_yield` (always fraction) or compute the yield
  yourself from `dividend_rate / current_price`.
- **Trading currency vs reporting currency.** `fast_info.currency` and
  `info.currency` return the **trading currency** (the currency you'd
  buy the stock in — USD for AAPL, USD for ADRs like TM/BABA/PBR, HKD for
  0700.HK). `financials.currency` returns the **reporting currency**
  (what the financial statements are denominated in — USD for AAPL but
  JPY for TM, CNY for BABA/0700.HK, BRL for PBR). For most direct-listed
  US/EU equities they match; for ADRs and some cross-border listings
  they don't. When mixing modes, don't assume `fast_info.currency` and
  `financials.currency` agree — check both. See references/financials.md
  for the per-ticker examples.
- **DST.** ET (`America/New_York`) is DST-aware. yfinance ISO timestamps
  carry the correct UTC offset, so data is fine. Only worry about DST when
  translating ET to a user's local timezone in prose (e.g., 09:30 ET =
  21:30 Beijing summer / 22:30 winter).
- **Escape `$` as `\$` in prose.** Many markdown renderers (including
  Claude Code's) treat `$...$` as a math-mode delimiter and will swallow
  the digits between two unescaped dollar signs (e.g. `$237.30` may render
  as `.30`). Always write `\$237.30` instead.

### Identifiers (tickers and exchanges)

- **Ticker suffixes for non-US markets.** Hong Kong: `0700.HK`. Shenzhen:
  `000001.SZ`. Shanghai: `600519.SS`. London: `BARC.L`. Tokyo: `7203.T`.
  Korea: `005930.KS`. Frankfurt: `BMW.DE`. If a user gives a bare HK/CN
  ticker, ask or guess the suffix.
- **`exchange` codes are short Yahoo identifiers, not human names.** `NMS`
  → Nasdaq, `NYQ` → NYSE, `PCX` → NYSE Arca, `HKG` → HKEX, `JPX` → Tokyo,
  `LSE` → London, `GER` → Xetra (Frankfurt). Decode before showing to the
  user — render "AAPL (Nasdaq)" not "AAPL (NMS)". Full code table (TSX /
  ASX / KRX / NSE / BSE / SIX / Borsa / Shanghai / Shenzhen / Cboe BZX) in
  references/exchanges.md. Don't infer from ticker suffix — both
  `fast_info` and `info` return `exchange` explicitly.

### Calling conventions (errors, retries, output)

- **Retry semantics.** Each script wraps its Yahoo call with
  exponential backoff + jitter (3 attempts, base ~0.5s) on
  `rate_limit` and `network` errors only — `not_found` (delisted /
  wrong suffix) never retries. Per-mode latency, batching guidance,
  `options --expiry` 2-HTTP doubling, the `history` batched exception,
  and sustained-429 worst cases all live in references/performance.md.
- **`error_kind` and `attempts` on results.** Failed tickers carry
  `error`, `error_kind` ∈ {`rate_limit`, `not_found`, `network`,
  `unknown`}, and `attempts` (how many tries before giving up). Use
  `error_kind` to decide retry at the request level: `rate_limit` ⇒
  wait & try later; `not_found` ⇒ ticker is bad, don't bother;
  `network`/`unknown` ⇒ one more try but escalate if persistent.
  Successful results also include `attempts` *only when > 1* — useful
  for spotting tickers that took a retry to succeed.
- **Output formats.** Every script accepts `--format json|ndjson|csv`.
  Default `json` is pretty-printed for human reading; `ndjson` (one
  JSON object per line) is friendliest for streaming/parse-by-line.
  CSV output uses `\n` line endings (not `\r\n`) so Unix tools work.
  CSV support per mode:
  - `fast_info` — ✅ default.
  - `history` — ✅ default rows; ✅ `--summary`.
  - `info` — ❌ default (nested sections); ✅ `--summary`.
  - `earnings` — ✅ default (one row per `earnings_date`); ✅ `--summary`.
  - `financials` — ❌ default (nested per-statement period lists); ✅ `--summary`.
  - `news` — ✅ default (no `--summary` mode).
  - `holders` — ✅ default (one row per holder, with a `holder_class`
    discriminator: `summary` / `institutional` / `mutualfund`); ✅ `--summary`.
  - `options` — ✅ default (one row per contract, with a `leg`
    discriminator: `call` / `put`; symbol / spot / expiry repeat
    across a ticker's rows); ✅ `--summary`.
  - `insiders` — ✅ default (one row per record, with a `record_class`
    discriminator: `purchases` / `transaction` / `roster`; `position`
    and `url` columns are deduplicated and shared across `transaction`
    and `roster` rows since both record types semantically have them);
    ✅ `--summary`.
  - `analyst` — ✅ default (one row per record, with a `record_class`
    discriminator: `recommendation` / `change`); ✅ `--summary`.
  - `screener` — ✅ default (one row per quote; envelope metadata
    `total` / `predefined` / `title` is JSON-only — not projected
    to CSV). No `--summary` mode (default output is already flat).
  - `fund_holdings` — ✅ default (one row per record, with a
    `record_class` discriminator: `meta` / `operations` /
    `asset_class` / `sector` / `bond_rating` / `equity_metric` /
    `bond_metric` / `holding`); ✅ `--summary`.
  - `sec_filings` — ✅ default (one row per filing; the nested
    `exhibits` dict is dropped from CSV — `primary_url` +
    `exhibit_count` carry the headline signals); ✅ `--summary`.

  **`screener` shape note.** Screener is the only mode that emits a
  **single envelope dict** (one screener call → one result), not a
  list of per-ticker records. JSON output wraps the quotes in
  `total` / `returned` / `predefined` / `title` metadata. NDJSON
  drops the envelope and emits one quote per line. CSV emits one
  row per quote (no envelope columns). On error / no-match,
  NDJSON / CSV emit a single envelope-summary line / row instead
  of empty stdout.
  
  **CSV row shapes split into two families.** Strict "one row per
  ticker": `fast_info` and all `--summary` modes (`history` / `info` /
  `earnings` / `financials` / `holders` / `options` / `insiders` /
  `analyst` / `fund_holdings` / `sec_filings`). "Row-per-event" (with
  `symbol` column repeating across rows for the same ticker): `history`
  default (one row per bar), `earnings` default (one row per
  `earnings_date`), `news` (one row per article), `holders` default
  (one row per holder, plus one rollup row per ticker tagged
  `holder_class=summary`), `options` default (one row per contract,
  tagged `leg=call` / `leg=put`), `insiders` default (one row per
  record, tagged `record_class=purchases` / `transaction` / `roster`),
  `analyst` default (one row per record, tagged
  `record_class=recommendation` / `change`), `fund_holdings` default
  (one row per record, tagged `record_class=meta` / `operations` /
  `asset_class` / `sector` / `bond_rating` / `equity_metric` /
  `bond_metric` / `holding`), `sec_filings` default (one row per
  filing; nested `exhibits` dict dropped — `primary_url` +
  `exhibit_count` columns carry the headline signals).
  For `news`, `holders`, `options`, `insiders`, `analyst`,
  `fund_holdings`, and `sec_filings` specifically: tickers with no
  data or an error still get a single row carrying the symbol +
  `note` + meta fields so they aren't silently dropped.

  **`screener` is its own family — single envelope, row-per-quote.**
  Unlike the per-ticker modes which iterate N tickers and emit N
  records, screener is one screen call → one set of quotes. Default
  CSV emits one row per quote (no `symbol` repeats — each row is a
  unique ticker), envelope metadata (`total` / `predefined` / `title`)
  is not projected to CSV. On error / no-match it emits a single
  carry row with `note` / meta cols populated. Plus a `symbols`
  format (one ticker per line, no header) for piping into per-ticker
  modes via xargs.
- **`note` field convention.** Several modes expose a per-result
  `note` string carrying **ambiguous-but-successful** state, distinct
  from `error` (which only appears on failure). Two cross-mode
  invariants:
  1. **`note` and `error` never co-occur** in one result dict;
     both appear as columns in the CSVs of modes that emit `note`,
     so neither category gets dropped from tabular output.
  2. **`note` and `coverage_note` are mutually exclusive** in
     modes that expose both (currently `earnings`, `insiders`, and
     `analyst` — all three signal "successful but unusual shape",
     but with different action implications: `note` = no data /
     ambiguous cause, chain `fast_info`; `coverage_note` = real
     data with thin event coverage, the empty fields ARE the
     answer). Both fields appear as CSV columns in modes that
     emit them.
  Per-mode semantics differ — see references/<mode>.md for the
  contract:
  - `news.note` — empty Yahoo response (ambiguous: bogus / low
    coverage / transient gap).
  - `earnings.note` — **non-equity short-circuit only** (contract-
    asserted: never set on EQUITY). Earnings has a companion field
    `earnings.coverage_note` for the IPO fall-through case (equity
    with empty calendar but populated estimates) — mutually exclusive
    with `note`. Both fields appear as columns in default-mode AND
    `--summary`-mode CSVs, so an IPO fall-through row carries the
    disambiguation signal in either layout.
  - `financials.note` — non-equity short-circuit, partial fetch
    failure (one or two of three statements failed), or reporting-
    currency-fallback path. Looser semantics than earnings.
  - `holders.note` — **all-empty result**, regardless of cause. Three
    causes Yahoo doesn't disambiguate: non-equity (ETF / index /
    crypto / FX / future / mutual fund), bogus / delisted ticker, or
    real but very low-coverage equity. Unlike `earnings` / `financials`
    we don't pre-screen on `quote_type` (no benefit — the holders call
    is the cheap fast path). Caller can chain `fast_info` to
    disambiguate. See references/holders.md "All-empty is ambiguous".
  - `insiders.note` — **all-three-empty result only**, regardless
    of cause. Same ambiguity shape as `holders` (non-equity /
    bogus / low-coverage); chain `fast_info` to disambiguate. The
    partial-empty case (purchases rollup populated, events empty)
    is surfaced via a companion field `coverage_note` instead — see
    references/insiders.md for the full mutually-exclusive contract.
    Both fields appear as columns in default-mode and `--summary`-
    mode CSVs.
  - `analyst.note` — **both-frames-empty result only**. Same
    ambiguity shape as `holders` / `insiders` (non-equity / bogus
    / no-coverage); chain `fast_info` to disambiguate. The
    partial-empty case (recommendations populated, upgrades_downgrades
    empty — verified for `0700.HK`, `BMW.DE` non-US primary
    listings; ADRs like `TM` still get full coverage) is surfaced
    via a companion field `coverage_note` (mutually exclusive with
    `note`). Both fields appear as columns in default-mode and
    `--summary`-mode CSVs.
  - `screener.note` — **zero matches** (predefined screen returned
    no rows in current market state, or custom query is too
    restrictive). No `error_kind` is set; the empty `quotes` array
    IS the answer. CSV / NDJSON emit a single carrying row / line.
  - `fund_holdings.note` — **non-fund symbol** (`YFDataException`
    caught from yfinance: equity / index / crypto / FX / future). The
    response carries `symbol` + `quote_type` + `note`; no data
    sections. The `quote_type` is captured from yfinance's parser
    state (set BEFORE the parse error raised), so callers know what
    they got back without a follow-up `fast_info` chain — distinct
    from `holders` / `insiders` / `analyst` which all defer
    disambiguation to the caller. No `coverage_note` partial-empty
    path either (Yahoo's funds endpoint is binary). Bogus / delisted
    tickers route through `error_kind: not_found` instead, and in
    that path `quote_type` is null because the parser never ran.
  - `sec_filings.note` — **Yahoo returned no data** (`{}` instead
    of a list), regardless of cause. Same ambiguity shape as
    `holders` / `insiders` / `analyst` (non-US primary listing /
    non-equity / bogus). ADRs (`TM`) are NOT in this bucket —
    they're SEC-registered foreign issuers and get full 6-K / 20-F
    coverage. Caller can chain `fast_info` to disambiguate. No
    `coverage_note` partial-empty path — the SEC-filings endpoint
    is binary. Distinct companion field `sec_filings.filter_note`
    fires when the ticker DID fetch successfully but `--type` /
    `--since` / `--days` filters reduced the displayed list to
    zero (e.g. `--type 10-K` on TM, an ADR that files 20-F). The
    two are mutually exclusive at the result level — `note`
    means "no data from Yahoo", `filter_note` means "Yahoo
    returned data, the display filters ate it". CSV consumers
    should check both columns.
  - `options.note` — **two distinct empty paths, same `note`
    convention.** (1) **No options listed at all** (`t.options`
    returns `()`): same ambiguity shape as `holders` — non-equity
    (index / crypto / FX / future / mutual fund / non-US equity),
    bogus ticker, or real equity too small / illiquid for option
    listing. Caller can chain `fast_info` to disambiguate. (2)
    **Empty chain on a valid expiry** (rare): `expirations` is
    populated but Yahoo returned `{}` for the requested date —
    user can retry with a different date from the array. The two
    paths use different note strings so the action implication is
    explicit; both have empty `calls` / `puts` arrays + no
    `error_kind`. See references/options.md "Empty / non-applicable
    result" for the exact texts.
- **Unofficial.** yfinance is unaffiliated with Yahoo and its endpoints
  can break at any time. If a script returns nothing for a ticker that
  should exist, the upstream API may have changed — don't keep retrying.
