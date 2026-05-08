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

Shared NaN/Inf-safe converters live in `scripts/helpers.py`. A
`scripts/smoke.py` test exercises all nine wrappers against
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
> (try a different expiry from the array). `insiders` mirrors the
> `holders` shape with one twist: ETFs / indexes / crypto / FX /
> futures / bogus tickers all return three empty frames
> (success-with-`note` — same ambiguity, same `fast_info` chain to
> disambiguate), but a few real equities (verified `BMW.DE`, `TM`)
> return `purchases_summary` populated and `transactions` + `roster`
> empty — that's partial Yahoo coverage of non-US per-event filings,
> not ambiguity, surfaced via a separate `coverage_note` field
> (mutually exclusive with `note`) so the asymmetry is visible
> in-band rather than silently shaped like real activity. Both
> `fast_info` and `info` return `quote_type` explicitly when you're
> unsure.

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
| "what's the analyst rating / price target on X" | `info` (analyst section) | `info.analyst.target_mean_price`, `recommendation_key` — complementary to `earnings --estimates` (which has the underlying EPS/revenue forecasts; price target is derived from those) |
| "income statement", "balance sheet", "cash flow", "revenue/FCF trend over N years" | `financials` | per-statement period lists; scope with `--statement income\|balance\|cashflow` |
| "latest quarter", "QoQ revenue", "TTM trailing twelve months" | `financials --period quarterly\|ttm` | ~5–7 most-recent quarters; `ttm` = 1-row rollup (income + cashflow only) |
| "compare 3+ tickers' revenue / FCF / margins / growth" | `financials --summary` | flat per-ticker dict + period-over-period growth (`*_growth_yoy`) |
| "what's the latest news on X", "recent headlines", "what's driving X today" | `news` | up to ~10 articles per ticker; works for all quote types; use `--limit` to tighten |
| "who owns X", "top institutional holders", "% insider / institution ownership" | `holders` | rollup pcts + top-10 institutional + top-10 mutualfund holders, one Yahoo call total |
| "compare ownership concentration across tickers", "top-5 institutional %" | `holders --summary` | flat per-ticker dict + `top5_institutions_pct` concentration signal |
| "top mutual-fund holders of X", "which Vanguard / iShares funds hold X" | `holders` | mutualfund section. Mostly broad index trackers; specialist active funds rarely surface |
| "is the CEO buying / selling", "did insiders buy this dip", "Form 4 activity", "current insider holdings", "how many shares does the CEO own" | `insiders` | three sections in one HTTP: 6-month buy/sell rollup (`purchases_summary`, all `pct_*` are FRACTIONS), per-event Form 4 transactions (`transactions`, last ~24 months, name / position / shares / value / date), current roster (`roster`, with direct + indirect holdings — sort by `shares_owned_directly` desc to rank) |
| "net insider buying across tickers", "rank by insider sentiment", "who's the largest insider holder" | `insiders --summary` | flat per-ticker peer compare: `net_shares_purchased` / `pct_net_shares_purchased`, `latest_transaction_date` (recency), `top_insider_by_direct_shares` + `top_insider_direct_shares` |
| "AAPL options chain", "TSLA puts near-the-money", "NVDA implied vol" | `options --moneyness 5` | nearest expiry's calls + puts, strikes within ±5% of spot. Default fetch is 1 HTTP per ticker (cheap) — cost doubles when you pin `--expiry` |
| "AAPL call options expiring 2026-06-19", "TSLA Jan 2027 puts" | `options --expiry YYYY-MM-DD` | specific expiry; bad date returns `error_kind: not_found` with available list to pick from |
| "compare ATM IV across NVDA / AMD / AVGO", "put-call ratio", "options sentiment" | `options --summary --moneyness 5` | flat per-ticker dict: ATM call/put IV, total volume, PCR by volume and OI; `moneyness_pct` echoed for self-describing output |
| "what expirations does X have" | `options` | the `expirations` array is always populated on success; default fetch is the nearest expiry |

A single user request can need multiple modes. "What's AAPL trading at, how
much is it up YTD, and what's its P/E?" → `fast_info` for the live price,
`history --period ytd --summary` for the YTD return, `info` for the P/E.

**Catch-all rule.** If the user is open-ended ("tell me about NVDA",
"what's up with TSLA?") and you can't pin them to a row above, default
to `fast_info` first — it's the cheapest and answers "where is it
trading, how big is it, what's the recent range" in one call. Add `info`
only if the user follows up about sector / fundamentals / analyst views,
or if the original question already mentions those.

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
```

`<SKILL_DIR>` is the absolute path of the directory containing this
SKILL.md. Substitute it once when running.

## Cost / latency

Rough per-ticker cost: `fast_info` / `news` / `holders` / `insiders` ~0.3–1.5 s,
`history` ~0.5–4 s, `info` / `earnings` / `financials` / `options`
~1–5 s. All modes are serial (total ≈ N × per-ticker) **except
`history`** — it batches N≥2 through `yf.download` in one threaded HTTP
call, so 10 tickers cost ~1–2 s instead of ~5–15 s. `--summary` does
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

These apply to all eight modes. Mode-specific caveats live in the
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
  
  **CSV row shapes split into two families.** Strict "one row per
  ticker": `fast_info` and all `--summary` modes (`history` / `info` /
  `earnings` / `financials` / `holders` / `options` / `insiders`).
  "Row-per-event" (with `symbol` column repeating across rows for the
  same ticker): `history` default (one row per bar), `earnings`
  default (one row per `earnings_date`), `news` (one row per article),
  `holders` default (one row per holder, plus one rollup row per
  ticker tagged `holder_class=summary`), `options` default (one row
  per contract, tagged `leg=call` / `leg=put`), `insiders` default
  (one row per record, tagged `record_class=purchases` /
  `transaction` / `roster`). For `news`, `holders`, `options`, and
  `insiders` specifically: tickers with no data or an error still
  get a single row carrying the symbol + `note` + meta fields so
  they aren't silently dropped.
- **`note` field convention.** Several modes expose a per-result
  `note` string carrying **ambiguous-but-successful** state, distinct
  from `error` (which only appears on failure). Two cross-mode
  invariants:
  1. **`note` and `error` never co-occur** in one result dict;
     both appear as columns in the CSVs of modes that emit `note`,
     so neither category gets dropped from tabular output.
  2. **`note` and `coverage_note` are mutually exclusive** in
     modes that expose both (currently `earnings` and `insiders`
     — both signal "successful but unusual shape", but with
     different action implications: `note` = no data / ambiguous
     cause, chain `fast_info`; `coverage_note` = real data with
     thin event coverage, the empty fields ARE the answer). Both
     fields appear as CSV columns in modes that emit them.
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
