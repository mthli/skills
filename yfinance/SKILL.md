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

- `scripts/fast_info.py` — current quote; optional `--with-isin` for opt-in ISIN lookup (slow ~1-5 s/ticker, spotty hit rate, see references/fast_info.md)
- `scripts/history.py` — historical OHLCV; full bars, `--summary` aggregates, `--events-only` corporate-action rows, `--shares` shares-outstanding time series (equity-only), or `--metadata` snapshot
- `scripts/info.py` — profile + fundamentals + analyst; full grouped sections or `--summary` flat dict
- `scripts/earnings.py` — upcoming + recent earnings dates with EPS estimates / actuals / surprise
- `scripts/financials.py` — annual / quarterly / TTM income statement, balance sheet, and cash flow
- `scripts/news.py` — recent Yahoo Finance news headlines per ticker
- `scripts/holders.py` — insider / institutional ownership rollup + top-10 institutional and mutual-fund holders
- `scripts/options.py` — option chain (calls + puts for one expiry); pair with `--moneyness` for an ATM-band slice + ATM/PCR summary
- `scripts/insiders.py` — Form 4 insider transactions (last ~24 mo) + 6-month buy/sell rollup + current roster
- `scripts/analyst.py` — analyst recommendations time series (0m / -1m / -2m / -3m bucket counts) + per-event grade-change feed with embedded price-target moves
- `scripts/screener.py` — market-wide discovery via Yahoo predefined screens or custom AND/OR queries; ≤250 quotes per call. **Ticker-free** — produces tickers from a filter, doesn't take one
- `scripts/fund_holdings.py` — ETF / mutual-fund holdings, weightings (sector / asset / bond-rating), expense ratio + AUM, fund-level P/E / P/B / duration. **Fund-only**
- `scripts/sec_filings.py` — SEC filings list (10-K / 10-Q / 8-K / DEF 14A / 20-F / 6-K / SC 13G/A / etc.) with date / type / URL / exhibits; ~75–120 filings per ticker. **SEC-registered securities only** (US-listed equities + ADRs)
- `scripts/calendars.py` — market-wide event calendars (earnings / IPO / splits / economic) over a date window. **Ticker-free** — discovery mode for "what's happening this week"; distinct from per-ticker `earnings.py`
- `scripts/sectors.py` — Yahoo's sector / industry hierarchy (11 sectors → ~150 industries → companies / ETFs / funds). **Ticker-free** — keys are sector / industry strings (`technology`, `semiconductors`)
- `scripts/market.py` — market-wide pulse across 8 Yahoo regions (`US`, `GB`, `ASIA`, `EUROPE`, `RATES`, `COMMODITIES`, `CURRENCIES`, `CRYPTOCURRENCIES`). **Ticker-free** — keys are region strings
- `scripts/valuation.py` — historical valuation time series (Current + last 5 quarter-end snapshots × 9 metrics: market_cap, enterprise_value, trailing_pe, forward_pe, peg_ratio, price_to_sales, price_to_book, ev_to_revenue, ev_to_ebitda). **Equity-only**; complements `info.valuation` (current snapshot) with the temporal dimension. HTML scrape — see references/valuation.md fragility caveat

Shared NaN/Inf-safe converters live in `scripts/helpers.py`. A
`scripts/smoke.py` test exercises every mode wrapper against
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

**Quote-type applicability.** Different modes apply to different
asset types. The bucket determines what happens on inapplicable
input — full per-mode empty-result semantics live in the matching
`references/<mode>.md` ("Empty / non-applicable result" subsection
where present, otherwise under "Mode-specific caveats").

| Bucket | Modes | Behavior on inapplicable type |
|---|---|---|
| Universal | `fast_info`, `history` (default + `--events-only`), `news` | Works for stocks / ETFs / indexes / crypto / futures / FX. Projection caveats: `history --events-only` is empty for indexes / crypto / FX / futures (no corporate actions); funds get a `Capital Gains` column (Yahoo coverage sparse). `history --metadata` is a separate projection that works for every type and returns `instrument_type` (EQUITY / ETF / MUTUALFUND / INDEX / CRYPTOCURRENCY / FUTURE / etc.) — doubles as a cheap quote-type sniff. |
| Equity + fund | `info` | Meaningful only for `quote_type` ∈ {EQUITY, ETF, MUTUALFUND}. Indexes / crypto / FX / futures return mostly null sections — don't waste the call. |
| Equity-only (pre-screened) | `earnings`, `financials` | Pre-screens on `quote_type`; non-equity short-circuits to empty + `note` without an HTTP call. |
| Equity-leaning ambiguous | `holders`, `insiders`, `analyst`, `sec_filings`, `options`, `history --shares`, `valuation` | All-empty result is success-with-`note`, **ambiguous** (non-equity / bogus / low-coverage equity all look identical). Chain `fast_info` to disambiguate via `quote_type` (`analyst` is the exception — it builds the `fast_info` call into its own 3-HTTP cost, so `quote_type` is already in the response). `options` is equity + ETF + US-listed ADRs only (mutual funds excluded); it also has a rarer second `note` for "valid expiry, empty chain" — try another date. `insiders` / `analyst` additionally use `coverage_note` for non-US primary listings (Yahoo's per-event / grade-change feed is US-centric — partial coverage is real data, not ambiguity). **ADRs (TM verified) get full coverage** — they trade on US exchanges. `sec_filings` covers ADRs via 6-K / 20-F (not 10-K / 10-Q / 8-K). `history --shares` values are **post-split actual counts, NOT split-adjusted** (4-for-1 split = clean 4× step). `valuation` is an HTML scrape of Yahoo's key-statistics page (the most fragile mode in the skill) — values are pre-rounded display strings (~3 sig figs); scrape breakage looks identical to "no data" so it's covered by the same `note`. |
| Fund-only | `fund_holdings` | Non-fund returns success-with-`note` carrying the resolved `quote_type` inline (no `fast_info` chain needed — this is the inverse-shape sibling of the ambiguous bucket). |
| Ticker-free | `screener`, `calendars`, `sectors`, `market` | No ticker input; produce/operate without one. `screener` = filter predicates → ticker list (`--format symbols \| xargs` to chain into per-ticker modes). `calendars` = market-wide event timeline by date window. `sectors` = Yahoo's curated 11-sector / ~150-industry taxonomy. `market` = live pulse across 8 canonical regions; **Yahoo quirk: `clock` always returns the US clock** regardless of region arg (surfaced as `clock_is_us_fallback: true` on non-US envelopes — read each summary row's `market_state` for per-region open/closed). |

Both `fast_info` and `info` return `quote_type` explicitly when
you're unsure which bucket a ticker falls in.

**Routing by question shape:**

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
| "all dividends paid by X", "split history", "capital-gain distributions" | `history --events-only` | corporate-action rows only — no OHLCV. Adds `capital_gains` field (fund-only — the column appears for ETFs / mutual funds, but Yahoo's actual coverage is sparse — see references/history.md "Capital Gains coverage") |
| "has X bought back stock", "shares-outstanding over time", "buyback / issuance history", "share-count change since IPO" | `history --shares` | `Ticker.get_shares_full` time series — sparse irregular-daily rows of `{date, shares_outstanding}`. Empty result for non-equity is success-with-`note` (ambiguous — chain `fast_info`; see bucket table). **Values are NOT split-adjusted** — a 4-for-1 split shows as a clean 4× step. Same-date dups deduped via `groupby(date).last()` (count surfaced in `same_date_duplicates_dropped`). Splits surfaced via `splits_detected` (heuristic — chain `--events-only` for ground truth). Coverage empirically ~10y back. **Cost is 1 HTTP per ticker** (multi-ticker is a serial loop) |
| "compare buyback rates across N tickers", "rank by shares-outstanding change", "peer compare share-count growth" | `history --shares --summary` | flat per-ticker dict: `start_shares` / `end_shares` / `change_abs` / `change_pct` (percent) / `min` / `max` with dates / `splits_detected_count`. Net out splits before quoting buyback rates (`change_pct` includes them as 4× jumps) |
| "when did X start trading", "what bar sizes does Yahoo accept for X", "IANA tz of HK ticker" | `history --metadata` | one-row-per-ticker projection of `Ticker.history_metadata` — `first_trade_date`, `valid_ranges`, `exchange_timezone_name`, `has_prepost`, plus quote / 52-week mirror fields |
| "what's the ISIN of X", "give me the ISIN for AAPL / SPY", "ISO security identifier" | `fast_info --with-isin` | EXPENSIVE opt-in (~1-5 s extra/ticker, ~3 HTTP: 2 to Yahoo + 1 to businessinsider.com). yfinance's `Ticker.isin` lookup, marked `*** experimental ***`. Hit rate is spotty even on liquid US names (2026-05 spot check resolved 3 of 6: AAPL ✓, SPY ✓, 0700.HK ✓, MSFT ✗, BMW.DE ✗, TM ✗). `-` / `^` tickers (crypto, indexes) short-circuit to null instantly. Null in output means "no match" (or "format check failed"), NOT "no ISIN exists" — verify externally before claiming a security has none. See references/fast_info.md "ISIN lookup" caveat |

> "S&P 500 P/E" or any index-fundamental question isn't answerable through `info` (or the other modes) — yfinance has no fundamentals for indexes; you'd need a different data source.

**Routing by question shape (continued):**

| Question shape | Mode | Why |
|---|---|---|
| "P/E", "P/B", "PEG" | `info` | `valuation` section |
| "P/E trend", "is X's P/E near its 1y high or low", "how has EV/EBITDA shifted over the last year", "valuation cheaper than 5 quarters ago" | `valuation` | Yahoo's key-statistics historical table — current + last 5 quarter-end snapshots × 9 metrics. Field names match `info.valuation.*` for clean interop; precision is ~3 sig figs (HTML-scraped display strings) so use `info` for exact current values, `valuation` for the trend |
| "compare P/E ranges across tickers", "rank by current vs 1y-low P/E", "which of these is near its valuation peak" | `valuation --summary` | flat per-ticker: `current_*` + `min_*` + `max_*` for trailing/forward P/E, P/B, EV/EBITDA across the window |
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
| "who's reporting earnings this week", "earnings calendar next 14 days", "upcoming earnings, large caps only" | `calendars --type earnings` | market-wide earnings calendar (default window: today + 7). Defaults to Yahoo's most-active filter (~200 most-active US tickers); pass `--no-most-active` for the firehose, `--market-cap 1e10` for large caps only. Distinct from per-ticker `earnings.py` (which needs a ticker). One HTTP (or 2 with default filter) |
| "upcoming IPOs", "IPO calendar this month", "what's pricing this week" | `calendars --type ipo --days 30` | IPO calendar with filing / pricing / amendment dates (full ISO datetime preserved — Yahoo's `04:00 UTC` encoding can be off-by-one in EST winter, so we don't truncate), price range, share count, and `action` status (`Expected` / `Priced` / `Postponed` / `Withdrawn`) |
| "stock splits this week", "any reverse splits coming up" | `calendars --type splits` | Splits calendar (forward and reverse) with **derived `direction` field** (`forward` / `reverse` / `even`) — no need to compare ratios manually. Empirically dominated by Korean reverse splits — filter consumer-side if you want US only |
| "economic events this week", "CPI / FOMC / GDP calendar", "macro releases next 7 days" | `calendars --type economic` | Macro calendar (CPI, FOMC, GDP, jobs, etc.) with consensus / actual / prior + **best-effort `unit` field** (`percent` / `index_level` / `thousands` / `currency`). ~48% of events have real intraday `event_time`; the rest fall back to midnight UTC (date-of-release). Country code in `region` |
| "what's happening this week", "all market events", "earnings + IPOs + splits in one query" | `calendars --type all` | Multi-type fetch in one invocation. Each type is a separate HTTP call; output is a list of envelopes (JSON) or per-record `record_class`-tagged rows (NDJSON / CSV). Pair with `--summary` for a digest |
| "who reported last week", "IPOs in the past 30 days", "macro releases I missed" | `calendars --past-days N` | Retrospective scan: window = (today − N) → today. Mutually exclusive with `--start` / `--end` / `--days` |
| "summary of this week's events", "rollup count of earnings / IPOs / splits" | `calendars --summary` | Per-type counts and aggregates (count_by_timing, count_by_action, count_forward / count_reverse, count_by_region_top10, ...) instead of full event lists. Pairs with `--type all` for cross-type peer compare |
| "what industries are in the technology sector", "breakdown of healthcare", "biggest sector by market cap" | `sectors <key>` | Yahoo's curated sector hierarchy. Default fetch = overview + top_companies (2 HTTP). For sector/industry decomposition pass `--section industries` (sector-only). Use `--list-sectors` to discover the 11 canonical keys without a network call |
| "top semiconductor stocks", "leading software companies", "best names in oil-and-gas" | `sectors <industry-key>` | Industry top_companies (sorted by market weight inside the industry). Auto-detects kind from the key. Use `--list-industries [SECTOR]` to discover canonical industry keys |
| "best technology ETFs", "top mutual funds for healthcare" | `sectors <sector-key> --section top_etfs,top_mutual_funds` | Yahoo's curated top-10 lists per sector (sector-only sections; industries return `coverage_note`). Chain a returned symbol into `fund_holdings.py` for expense ratio + holdings |
| "top performing semiconductor stocks YTD", "fastest-growing software companies" | `sectors <industry-key> --section top_performing_companies,top_growth_companies` | Industry-only sections. **Note:** `ytd_return` and `growth_estimate` are MULTIPLES (4.7 = +470%), not fractions — see references/sectors.md units |
| "compare 3 sectors side by side", "rank industries by market weight / company count" | `sectors --summary KEY1 KEY2 KEY3` | Flat per-key dict for peer compare. Auto-expands `--section` to all-applicable for the kind, so the rollup fields (top_company / top_industry / top_etf / top_performer) are populated. Mixed-kind runs work but the JSON shape differs per row |
| "what does the technology sector cover", "description of the energy sector" | `sectors <key> --section overview` | Yahoo's curated sector / industry description, market cap, market weight (FRACTION), and child counts. Cheapest call (1 HTTP after key validation) |
| "is the market open right now", "US market status", "when does the market open today" | `market US --section clock` | Live market clock (open / close datetimes + `status` string). **Yahoo quirk: always returns US clock** regardless of region arg — for non-US live status, read summary row `market_state`. 2 HTTP cost (yfinance fetches both sections together) |
| "how is Asia trading today", "what's the macro tape look like", "European indexes overview" | `market ASIA` (or `EUROPE` / `RATES` / `COMMODITIES`) | Yahoo's curated representative quotes for the region (ASIA = 5 INDEX + 1 CURRENCY pair; EUROPE = 4 mixed; RATES = 1 INDEX `^TYX` 30Y yield + 1 FUTURE `ZN=F` 10Y T-Note; COMMODITIES = 2 FUTURE — Brent + Copper). Each row has `change_pct` (PERCENT) + `market_state` |
| "is the region green or red right now", "compare US vs Asia vs Europe today" | `market --summary US ASIA EUROPE` | Cross-region peer compare: per-market avg/best/worst `change_pct` across featured quotes + top index. Quick "which region is leading today" digest |
| "what crypto / FX is featured", "today's CURRENCIES snapshot" | `market CRYPTOCURRENCIES` (or `CURRENCIES`) | Yahoo's curated featured pair (typically 1 row — `SOL-USD` / `MXN=X` rotates). Sparse by design; for the full crypto / FX universe use `screener --predefined` or chain `fast_info` over explicit pairs |

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

If the user asks "what's happening this week" / "who's reporting" /
"earnings calendar" / "upcoming IPOs" / "macro events" — that's
**market-wide event discovery**, jump to `calendars --type
earnings|ipo|splits|economic`. Per-ticker `earnings.py` can't answer
this without a ticker list (no inverse direction); `calendars` is
date-bounded and ticker-free.

If the user asks "what's in the X sector" / "top stocks in Y
industry" / "industries under sector Z" / "best ETFs / mutual funds
for sector W" — that's **hierarchy navigation**, jump to `sectors
<key>`. Distinct from `screener`: sectors browses Yahoo's curated
taxonomy (predefined top_companies / top_etfs / industries lists);
screener filters with custom predicates. Use `--list-sectors` /
`--list-industries` to discover canonical keys before the main
fetch if a key isn't obvious.

If the user asks "is the market open" / "how is Asia trading" /
"what's the macro tape" / "compare US vs Europe today" — that's
**live region pulse**, jump to `market <key>`. Distinct from
`calendars` (event timeline, date-bounded) and `sectors` (curated
hierarchy of one US-listed taxonomy): market is the cross-region
live snapshot. Use `--list-markets` to enumerate the 8 keys.

## Invocations

One canonical invocation per mode. For the full menu of flags
(intraday / extended-hours / `--summary` / `--limit` / `--format csv`
/ etc.), Read the matching `references/<mode>.md` "Run" section.

```bash
# fast_info — current quote (flags: --with-isin)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA

# history — historical OHLCV (flags: --period, --interval, --prepost, --summary, --events-only, --shares, --metadata, --tail)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py AAPL

# info — profile + fundamentals + analyst (flags: --summary)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL MSFT

# earnings — upcoming + recent earnings dates (flags: --summary, --estimates, --future-only). NOTE: needs extra `--with 'lxml'`.
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py AAPL

# financials — income / balance / cashflow (flags: --period {annual,quarterly,ttm}, --statement {income,balance,cashflow}, --summary)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py AAPL

# news — Yahoo Finance headlines (flags: --limit, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py AAPL

# holders — ownership rollup + top inst / mutualfund (flags: --summary, --limit, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py AAPL

# options — option chain, ONE expiry (flags: --moneyness N, --expiry YYYY-MM-DD, --summary, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --moneyness 5 AAPL

# insiders — Form 4 + 6-mo rollup + roster (flags: --summary, --limit, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py AAPL

# analyst — recommendations + grade-change feed (flags: --summary, --limit, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py AAPL

# screener — market-wide discovery, ONE envelope per call, ≤250 quotes (flags: --predefined NAME, --query JSON|@file, --quote-type, --sort-field, --count, --list-predefined, --list-fields, --format {csv,symbols}, --full)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py --predefined day_gainers --count 10

# fund_holdings — ETF / mutual-fund holdings, fund-only (flags: --summary, --limit, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py SPY

# sec_filings — SEC filings list (flags: --type 10-K,10-Q,8-K,..., --limit, --since YYYY-MM-DD, --days N, --summary, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py AAPL

# calendars — market-wide event calendar, ticker-free (flags: --type {earnings,ipo,splits,economic,all} or comma-separated like `earnings,ipo`, --days, --past-days, --start, --end, --market-cap, --no-most-active, --limit, --summary, --full, --format)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py

# sectors — Yahoo's sector/industry hierarchy, ticker-free (flags: --section overview,top_companies,... or `all`, --kind {auto,sector,industry}, --summary, --limit, --list-sectors, --list-industries [SECS], --peers KEY, --full, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py technology

# market — market-wide pulse across 8 regions, ticker-free (flags: --section {clock,summary,all}, --summary, --limit, --list-markets, --full, --format csv)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py US

# valuation — historical valuation snapshots (flags: --summary, --format csv). Equity-only. HTML scrape — fragile.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/valuation.py AAPL
```

`<SKILL_DIR>` is the absolute path of the directory containing this
SKILL.md. Substitute it once when running.

## Cost / latency

Rough per-ticker cost: `fast_info` / `news` / `holders` / `insiders` /
`sec_filings` ~0.3–1.5 s (sec_filings is one quoteSummary call —
similar shape to news), `fund_holdings` ~0.7–1.5 s (single quoteSummary
call but fetches 4 modules at once — see references/performance.md),
`history` ~0.5–4 s, `info` / `earnings` / `financials` / `options` /
`analyst` / `valuation` ~1–5 s (`valuation` is an HTML scrape of the
Yahoo key-statistics page — 1 HTTP per ticker, no batching path). **`screener` and `calendars` are per-call, not
per-ticker** — `screener` is 1 HTTP returning ≤ 250 quotes (~1–3 s
typical), so the marginal cost of a wider screen is ~0. `calendars`
is 1 HTTP per call (~1–2 s) **except** earnings with the default
most-active filter, which costs 2 HTTP (the prescreen for the
most-active list runs first). **`sectors` is 1 HTTP per key
regardless of `--section` count** — verified 2026-05: yfinance hits
one Yahoo endpoint per `yf.Sector(key)` / `yf.Industry(key)` and
caches all sections on the instance, so `--section overview`,
`--section all`, and `--summary` (which auto-expands to all-
applicable) cost the same network-wise per key. `--section` only
affects projection / output cost. Cross-key fan-out is serial:
N keys = N HTTP, ~0.6–2 s per key, so a 5-sector `--summary` is ~5
HTTP / ~3–10 s. `--list-sectors` / `--list-industries` / `--peers`
are 0 HTTP (pure local lookup). **`market` is 2 HTTP per region**
(markettime + marketSummary; yfinance interleaves both fetches in
`_parse_data` to keep them time-aligned, so `--section` doesn't
reduce HTTP — only projection cost). N regions = 2N HTTP, serial,
~1.5–3 s per region. `--list-markets` is 0 HTTP.

**`analyst` makes 3 HTTP per ticker** (`recommendations`,
`upgrades_downgrades`, and `fast_info` for `quote_type` — each from
a different endpoint or module group, so none share a backend request;
the `fast_info` call is the disambiguator that lets the all-empty
`note` path be answered inline without a follow-up call).

All modes are serial (total ≈ N ×
per-ticker) **except `history`** — it batches N≥2 through
`yf.download` in one threaded HTTP call, so 10 tickers cost ~1–2 s
instead of ~5–15 s. **One wrinkle:** `history --metadata` is the
exception within history itself — it routes per-ticker through
`Ticker.history()` (cheapest possible window, `period=1d`) because
`yf.download` doesn't reliably populate per-ticker `history_metadata`
state. So a 10-ticker `--metadata` call is N HTTP, ~3–8 s, not the
batched 1-HTTP path. **`history --shares` is the second per-ticker
exception inside history**: `Ticker.get_shares_full` is a distinct
Yahoo timeseries endpoint with no `yf.download` equivalent, so
multi-ticker shares mode is also a serial loop — 10 tickers ≈ N HTTP,
~5–15 s (`get_shares_full` is slightly slower per call than the
metadata path). `--summary` and `--events-only` still go through the
batched path for N≥2. `--summary` does **not** reduce latency; it's a
post-fetch projection that only shrinks output JSON (use it to save
context tokens, not time). Same applies to `--shares --summary` —
the aggregate runs after the per-ticker fetch, so latency matches
default `--shares`. When a question
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

See references/setup.md for `uv` install, the lxml dependency
rationale (only `earnings.py` / `smoke.py` need it), and the
version-pin reasoning.

## Cross-cutting caveats

These apply to every mode. Mode-specific caveats live in the
matching `references/<mode>.md`. Grouped into three concerns:

### Data formats (interpreting the numbers)

- **Numeric units are not consistent across — or within — modes.**
  Don't guess a number's unit from the field name. Per-field unit
  tables and any non-obvious normalizations (e.g. `fund_holdings`
  inverting Yahoo's `1/ratio` P/E, dividing percents by 100) live in
  each `references/<mode>.md` "Output schema" / "Unit landmines"
  section. High-level map:
  - **Percent (`16.43` = 16.43%)**: `fast_info.change_pct`,
    `history --summary.change_pct`, `info.dividend.five_year_avg_dividend_yield`,
    `info.fund.ytd_return`, `options[*].change_pct`,
    `earnings_dates.surprise_pct`.
  - **Fraction (`0.272` = 27.2%, multiply ×100 for display)**: most
    `info` margins / growth / returns / payout, `financials --summary.*_growth_yoy`,
    `earnings --estimates` growths, `holders` `*_pct` / `pct_held`,
    `insiders` `pct_*`, `analyst --summary.buy_pct_*`,
    `fund_holdings` expense_ratio / weights / sectors / bond_ratings,
    `options[*].implied_vol` and `options --summary.atm_*_iv`,
    `info.dividend.trailing_annual_dividend_yield`,
    `info.fund.three_year_avg_return` / `five_year_avg_return` (CAGR).
  - **Likert 1–5 (1=strong_buy, 5=strong_sell, lower is more bullish)**:
    `analyst --summary.consensus_score_*`, `info.recommendation_mean`.
    Don't accidentally render as a percentage.

  Cross-mode landmines (the only ones the router needs to flag —
  rest live in each reference):
  - `info.fund.total_assets` is in **whole units**;
    `fund_holdings.operations.total_net_assets_millions` is in
    **MILLIONS**. 1e6× difference for the same metric — pick one mode
    and stick with it.
  - `earnings --estimates` ADR rows: `eps_currency` (trading ccy) and
    `revenue_currency` (home reporting ccy) are **separate fields,
    not duplicates** (TM EPS in USD, revenue in JPY). Read both.
  - `earnings --estimates[*].index_growth` is **the same number
    globally** (Yahoo-internal benchmark, not locale- or sector-aware).
    Don't read a HK-listed ticker's `index_growth` as Hang Seng growth.
  - Inside `earnings` the new `--estimates` growth fields are
    fractions but the older `earnings_dates.surprise_pct` is still
    percent — easy to swap.
  - Inside `options` rows: `change_pct` is percent but `implied_vol`
    is a fraction (`5.2` daily move; `0.25` = 25% IV).
  - `analyst.upgrades_downgrades` enum case is inconsistent (Yahoo
    quirk): `action` lowercase (`up` / `down` / `main` / `init` /
    `reit`); `price_target_action` capitalized (`Raises` / `Lowers` /
    `Maintains` / `Announces` / `Adjusts`).

  Sanity heuristic: any "yield" > 1.0 is the percent variant; any
  3y/5y avg-return < 0.5 is the fraction (CAGR) variant. When in
  doubt, prefer `trailing_annual_dividend_yield` (always fraction).
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
  Default `json` is pretty-printed; `ndjson` is one JSON object per
  line (parse-by-line); `csv` uses `\n` line endings (Unix tools).
  Per-mode CSV row schema, discriminator columns, and any nested-only
  fields dropped from CSV live in each `references/<mode>.md` "Output
  schema" section.

  Three row-shape families:
  - **One row per ticker.** `fast_info` and all `--summary` modes
    (`history` / `info` / `earnings` / `financials` / `holders` /
    `options` / `insiders` / `analyst` / `fund_holdings` /
    `sec_filings` / `valuation`). Strict 1:1 ticker ↔ row.
  - **Row per event** (with `symbol` repeating across rows for one
    ticker). Default modes of `history` (one row per bar), `earnings`
    (per `earnings_date`), `news` (per article), `holders` /
    `insiders` / `analyst` / `fund_holdings` / `sec_filings` /
    `options` / `valuation` (per record / contract / period; uses
    `record_class` / `leg` / `holder_class` / `period_label`
    discriminator columns). Tickers with no data or an error still
    emit a single carry row carrying `symbol` + `note` + meta fields
    so they aren't silently dropped.
  - **Single envelope, ticker-free.** `screener` (one screen call →
    one set of quotes), `calendars` (one envelope per `--type`, or a
    list of envelopes for `--type all` / multi-type), `sectors` /
    `market` (one envelope per positional key, with multiple sections
    flattened via `record_class`). NDJSON drops envelope and emits
    one record per line; CSV emits one row per record. Envelope
    metadata (`total` / `predefined` / `title`) is JSON-only. On
    error / no-match, NDJSON / CSV emit a single carry line / row.
    `screener` adds a `symbols` format (one ticker per line) for
    `xargs` piping into per-ticker modes.

  Two CSV defaults vary: `info` and `financials` have nested-only
  defaults and only emit CSV under `--summary`. One combo rejected
  by argparse: `calendars --format csv --full` (raw Yahoo keys break
  column stability — use `--format ndjson --full` instead).
- **`note` field convention.** Several modes expose a per-result
  `note` string carrying **ambiguous-but-successful** state, distinct
  from `error` (which only appears on failure). Cross-mode invariants:
  1. **`note` and `error` never co-occur** in one result dict; both
     appear as columns in CSVs that emit `note`, so neither gets
     dropped from tabular output.
  2. **`note` and `coverage_note` are mutually exclusive** in modes
     that expose both. Distinct action implications: `note` = no data
     / ambiguous cause, chain `fast_info` to disambiguate;
     `coverage_note` = real data with thin event coverage, the empty
     fields ARE the answer (don't re-fetch).
  3. **Two modes use `note` for "zero matches", not ambiguity.**
     `screener` / `calendars` set `note` when the screen / date
     window returned no rows — the empty result IS the answer; no
     `fast_info` chain needed. CSV / NDJSON emit a single carrying
     row.

  Modes with a `coverage_note` companion (real data, thin coverage):
  - `earnings` — IPO fall-through (calendar empty, estimates populated).
  - `insiders` / `analyst` — non-US primary listings get partial Yahoo
    coverage (per-event / grade-change feed is US-centric); ADRs still
    get full coverage.
  - `sectors` — `--section` requested that doesn't apply to the kind
    (e.g. `--section industries` on an industry).

  Modes with an analogous-but-distinct companion field:
  - `sec_filings.filter_note` — Yahoo returned data, but local `--type`
    / `--since` / `--days` filters reduced the displayed list to zero.
    Different from `coverage_note` (which is about Yahoo-side coverage
    gaps, not client-side filtering). Mutually exclusive with `note`.
  - `sectors.section_errors` — per-section transient HTTP failure
    (other sections in the same envelope succeeded). Independent from
    `coverage_note` (which is a kind-mismatch detected without HTTP).

  `fund_holdings` is special: its `note` carries the resolved
  `quote_type` inline, so callers don't need the `fast_info` chain to
  disambiguate. Per-mode `note` triggers and recommended next steps
  live in the matching `references/<mode>.md` ("Empty / non-applicable
  result" subsection where present, otherwise under "Mode-specific
  caveats").
- **Cache staleness.** yfinance maintains a persistent SQLite cache
  at `~/.cache/py-yfinance/...` and can return stale prices on
  repeat calls within a session. For genuinely live data, the first
  call of a session is freshest; for repeat polling, expect lag.
- **Unofficial.** yfinance is unaffiliated with Yahoo and its endpoints
  can break at any time. If a script returns nothing for a ticker that
  should exist, the upstream API may have changed — don't keep retrying.
