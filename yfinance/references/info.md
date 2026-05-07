[← back to SKILL.md](../SKILL.md)

# `info` reference

_Yahoo encodings & ticker examples below verified: 2026-05, yfinance 1.3.x.
If a unit / behavior diverges, re-run `scripts/smoke.py` and update this
doc — these are upstream passthrough fields and Yahoo can shift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-full-sections) · [Output — `--summary` mode](#output----summary-mode) · [When to use `--summary`](#when-to-use---summary) · [Sections by `quote_type`](#sections-by-quote_type) · [Analyst rating scale](#analyst-rating-scale) · [Currency](#currency) · [Presenting info results](#presenting-info-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **Unit landmines**)

Company profile, valuation, fundamentals, dividend, analyst targets, and
fund metadata (ETFs / mutual funds). Slowest mode in the skill (~1–3 s
per ticker, multiple Yahoo modules). Meaningful only for `quote_type` ∈
{`EQUITY`, `ETF`, `MUTUALFUND`}; for `INDEX`, `CRYPTOCURRENCY`, `FUTURE`,
`CURRENCY` it returns mostly null — redirect to
[`fast_info`](fast_info.md) / [`history`](history.md).

## Run

```bash
# Default: full grouped sections, one entry per ticker
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL MSFT

# Summary mode: flat per-ticker dict (peer comparison)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py --summary AAPL MSFT GOOGL

# CSV (only valid with --summary; default mode is nested)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py --summary --format csv AAPL MSFT GOOGL

# NDJSON — one JSON object per line
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py --format ndjson AAPL MSFT
```

Tickers are positional args. Default-mode output is a JSON array of grouped
objects; summary-mode output is a JSON array of flat dicts. Failed tickers
carry an `error` field in either mode.

## CLI arguments

- `--summary` — flag. Output a flat dict per ticker (symbol, quote_type,
  currency, **exchange**, sector, industry, category, market_cap,
  trailing_pe, forward_pe, trailing_eps, **trailing_annual_dividend_yield**,
  target_mean_price, recommendation_key, total_assets, expense_ratio,
  five_year_avg_return) instead of the full grouped sections. Exact field
  counts (summary, stock-default, fund-default) are computed at runtime —
  run `info.py --help` for the current numbers. Use when comparing 3+
  tickers; output is roughly 10× smaller than default and reads as a
  comparison table. Stock-specific fields (sector, P/E, EPS, target) are
  null for ETFs; fund-specific fields (category, total_assets,
  expense_ratio, 5y return) are null for stocks. Summary uses
  `trailing_annual_dividend_yield` (always fraction); the percent-encoded
  `dividend_yield` is no longer emitted by either mode, so every numeric
  field has a stable unit.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`
  is **only valid with `--summary`** (default mode has nested sections
  that don't flatten cleanly); using `--format csv` without `--summary`
  produces an argparse error. Columns in CSV mode are the SUMMARY_FIELDS
  set + `error` + `error_kind`.

## Output — default mode (full sections)

Abbreviated example (full field list lives in `scripts/info.py` →
`SECTIONS`). Sample numbers / dates below are illustrative, not a
real capture — Yahoo's values drift constantly.

```jsonc
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "currency": "USD",
    "exchange": "NMS",
    "profile": {
      "long_name": "Apple Inc.",
      "sector": "Technology",
      "industry": "Consumer Electronics",
      "employees": 166000,
      "summary": "Apple Inc. designs, manufactures..."
      // + 4 more: short_name, country, city, website
    },
    "valuation": {
      "market_cap": 4222761828352,
      "trailing_pe": 34.77,
      "forward_pe": 30.04,
      "ev_to_ebitda": 26.50
      // + 5 more: enterprise_value, peg_ratio, price_to_book,
      //          price_to_sales, ev_to_revenue
    },
    "fundamentals": {
      "trailing_eps": 8.27,
      "profit_margin": 0.272,        // fraction → 27.2%
      "return_on_equity": 1.415,     // fraction → 141.5%
      "revenue_growth": 0.166,       // fraction → 16.6%
      "free_cashflow": 101090746368,
      "beta": 1.065
      // + 16 more: forward_eps, book_value, revenue_per_share,
      //          operating_margin, gross_margin, ebitda_margin,
      //          return_on_assets, earnings_growth,
      //          earnings_quarterly_growth, total_revenue,
      //          total_cash, total_debt, debt_to_equity,
      //          current_ratio, quick_ratio, operating_cashflow
    },
    "dividend": {                    // 6 fields — units still vary, see caveat
      "dividend_rate": 1.08,                       // currency / share / yr
      "payout_ratio": 0.126,                       // fraction → 12.6%
      "ex_dividend_date": "2026-05-11",
      "five_year_avg_dividend_yield": 0.51,        // percent → 0.51%
      "trailing_annual_dividend_rate": 1.04,       // currency / share / yr
      "trailing_annual_dividend_yield": 0.0037     // fraction → 0.37%
    },
    // NOTE: Yahoo's percent-encoded `dividend_yield` is deliberately
    // dropped from this schema — its unit clashed with every other ratio
    // in `info` (which are all fraction). Use `trailing_annual_dividend_yield`
    // (fraction) or compute `dividend_rate / current_price` instead.
    "analyst": {                     // all 7 fields shown
      "recommendation_key": "buy",
      "recommendation_mean": 1.92,                 // 1=strong buy … 5=strong sell
      "number_of_analyst_opinions": 42,
      "target_mean_price": 303.38,
      "target_high_price": 355.0,
      "target_low_price": 215.0,
      "target_median_price": 310.0
    },
    "shares": {
      "shares_outstanding": 14687356000,
      "short_ratio": 3.24,
      "held_percent_insiders": 0.0164,
      "held_percent_institutions": 0.653
      // + 3 more: float_shares, shares_short, short_percent_of_float
    }
  }
]
```

Stocks get the sections shown above. ETFs / mutual funds get an
additional `fund` section — for `quote_type` ∈ {`ETF`, `MUTUALFUND`}
only. For everything else (stocks, indexes, crypto, futures, FX) the
`fund` section is omitted entirely; reading `data["fund"]` would
`KeyError`, so use `data.get("fund", {})` or check `quote_type` first.
All other sections are always present; missing individual fields within
them are `null`.

For ETF / mutual fund tickers the `fund` section looks like:

```json
{
  "category": "Large Blend",
  "total_assets": 735060819968,
  "nav_price": 723.62,
  "ytd_return": 5.68,
  "three_year_avg_return": 0.2216,
  "five_year_avg_return": 0.1327,
  "fund_family": "State Street Investment Management",
  "legal_type": "Exchange Traded Fund",
  "expense_ratio": null
}
```

ETFs / mutual funds also get all the stock-shared sections (`profile`,
`valuation`, `fundamentals`, `dividend`, `analyst`, `shares`) — same field
schema as stocks, but most fields are null because the issuer isn't an
operating company. The ones that *do* populate come from the fund's portfolio
aggregation (e.g., SPY's `valuation.trailing_pe = 27.55` is the holdings-
weighted P/E; `dividend.dividend_rate` is the trust's distribution rate).
`analyst.*` is reliably null for ETFs (no sell-side coverage), and
`shares.shares_outstanding` reflects the fund's creation/redemption units
rather than corporate shares.

**Retry surfacing.** First-shot success has no `attempts` field. If the
call retried before succeeding (transient 429 / network), the response
gains `"attempts": N` at the top level — both default-mode and
`--summary` mode preserve this. Same convention across all three modes.

**Most ratios are fractions** — `profit_margin: 0.272` means 27.2%. Multiply
by 100 for display. But several yield/return fields are encoded as **percent**
instead — see "Unit landmines" below for the full list.

`ex_dividend_date` is ISO `YYYY-MM-DD` (UTC) and represents the **next
upcoming** ex-div date, not the most recent past one. Verified empirically
across AAPL / MSFT / JNJ / KO / WMT — all returned future dates within the
following weeks. For a *past* ex-div date (e.g. "when did AAPL last go
ex-div?") use `history` and look at rows where `dividends > 0`.

A failed ticker looks like:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "no info returned (delisted, wrong suffix, or rate-limited)",
  "error_kind": "not_found",
  "attempts": 1
}
```

`error_kind` ∈ `{rate_limit, not_found, network, unknown}`; `attempts`
is the retry count (1 for not_found, up to 3 for transient failures).
See SKILL.md cross-cutting caveats for what each kind implies for
retry. Surface the per-ticker error and report the rest, don't fail
the whole batch.

## Output — `--summary` mode

Sample values below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "currency": "USD",
    "exchange": "NMS",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "category": null,
    "market_cap": 4222761828352,
    "trailing_pe": 34.77,
    "forward_pe": 30.04,
    "trailing_eps": 8.27,
    "trailing_annual_dividend_yield": 0.0037,
    "target_mean_price": 303.38,
    "recommendation_key": "buy",
    "total_assets": null,
    "expense_ratio": null,
    "five_year_avg_return": null
  },
  {
    "symbol": "SPY",
    "quote_type": "ETF",
    "currency": "USD",
    "exchange": "PCX",
    "sector": null,
    "industry": null,
    "category": "Large Blend",
    "market_cap": null,
    "trailing_pe": 27.55,
    "forward_pe": null,
    "trailing_eps": null,
    "trailing_annual_dividend_yield": 0.0078,
    "target_mean_price": null,
    "recommendation_key": null,
    "total_assets": 735060819968,
    "expense_ratio": null,
    "five_year_avg_return": 0.1327
  }
]
```

Same key set for every ticker — stocks fill the stock-specific fields and
null the fund-specific ones; ETFs do the reverse. **All numeric fields in
summary mode use stable units**: ratios are fractions (×100 for percent
display), monetary fields are in `currency`. The notorious percent-encoded
`dividend_yield` was the lone unit outlier — it's now dropped from the
default-mode schema entirely, so neither default nor summary mode
exposes it. Use `trailing_annual_dividend_yield` (fraction) for the
unambiguous yield. Errors look identical to default mode.

## When to use `--summary`

Reach for `--summary` when comparing 3+ tickers ("which has the lowest P/E",
"sort by dividend yield", peer-group screen). Default mode is ~5 KB per
ticker; summary is ~0.5 KB. For a single ticker just use default — the
extra context is useful and the size doesn't matter.

## Sections by `quote_type`

| `quote_type` | sections emitted | populated content |
|---|---|---|
| `EQUITY` | profile, valuation, fundamentals, dividend, analyst, shares | all populated for liquid US names; thinly-covered tickers may have null analyst |
| `ETF` | profile, valuation, fundamentals, dividend, analyst, shares, **fund** | `fund` + parts of `valuation` / `dividend`; analyst usually null |
| `MUTUALFUND` | profile, valuation, fundamentals, dividend, analyst, shares, **fund** | similar to ETF |
| `INDEX` | profile, valuation, fundamentals, dividend, analyst, shares | mostly null — yfinance has no fundamentals for indexes; "what's S&P 500's P/E" needs a different source |
| `CRYPTOCURRENCY` | profile, valuation, fundamentals, dividend, analyst, shares | mostly null — use `fast_info` / `history` instead |
| `FUTURE` / `CURRENCY` | profile, valuation, fundamentals, dividend, analyst, shares | mostly null — `fast_info` / `history` only |

The `fund` section is the only one whose presence varies — it's emitted
only for ETFs / mutual funds and dropped entirely for everything else
(so AAPL has one fewer section than SPY). All other sections are always
present, even when sparsely populated. The script doesn't error on the all-null cases; check
`quote_type` first if you suspect it's not a stock or fund — saves you
reading null fields and lets you redirect to `fast_info` / `history`.

## Analyst rating scale

`recommendation_mean` is on a reverse 1–5 scale: **1 = strong buy →
5 = strong sell**. Lower number = more bullish (trips up people who expect
"higher is better"). `recommendation_key` is the discrete bucket — empirical
enum verified across ~55 tickers:

| key | observed `mean` band | seen in batch? |
|---|---|---|
| `strong_buy` | 1.0–1.5 | ✓ (MSFT, NVDA, META) |
| `buy` | 1.5–2.5 | ✓ (AAPL 1.92) |
| `hold` | 2.5–3.5 | ✓ (INTC 2.65, SNAP 2.65) |
| `underperform` | 3.5–4.5 | ✓ (BYND 3.86) |
| `sell` | 4.5–4.8 (estimated) | ✗ — rarely consensus on stocks |
| `strong_sell` | 4.8–5.0 (estimated) | ✗ — almost never consensus |
| `null` (JSON) | n/a | ✓ — no analyst coverage |

No-coverage note: Yahoo's raw API returns **either** the string `"none"` **or**
JSON `null` — same meaning ("no analyst coverage / data") but they appear
independently across tickers (e.g., AI / TLRY / BB return `"none"`; WBA /
delisted tickers return `null`). `info.py` normalizes both to `null`, so
consumers of this skill can `if key is None: ...` without testing for two
sentinels. If you ever query yfinance directly bypassing `info.py`, you do
need to test for both.

## Currency

**All monetary fields are in `currency`, not USD.** `market_cap`,
`enterprise_value`, `target_*_price`, `total_revenue`, `total_cash`,
`total_debt`, `dividend_rate`, `nav_price`, `total_assets`, EPS, book value
etc. are denominated in the top-level `currency` field. For `0700.HK` that's
HKD (market_cap 4.3T HKD ≈ \$550B USD; target HK\$726). Don't write "\$X"
without checking, and don't silently FX-convert — quote in source currency
unless the user asked for USD.

## Presenting info results

For a single stock, lead with a one-paragraph profile + the headline numbers:

> Apple Inc. (AAPL) — Technology / Consumer Electronics, US, 166k employees.
> Market cap \$4.22T, trailing P/E 34.8, forward P/E 30.0, EPS \$8.27. Margins:
> 27.2% net, 32.3% operating. Dividend \$1.08/yr (yield ~0.38%, payout 12.6%).
> Analyst mean target \$303 (n=42), rating "buy".

For a single ETF, swap in the fund section:

> SPDR S&P 500 ETF Trust (SPY) — Large Blend, AUM \$735B, NAV \$723.62, YTD
> +5.7%, 5-yr avg return +13.3%. Issuer State Street.

For 2+ stocks (peer comparison), a compact valuation table:

| Symbol | Sector | Market Cap | P/E | P/B | EPS | Margin | Yield |
|---|---|---|---|---|---|---|---|

For 2+ ETFs, a fund table:

| Symbol | Category | AUM | Expense | YTD | 3Y Avg | 5Y Avg |
|---|---|---|---|---|---|---|

Always escape `$` as `\$` in prose (see SKILL.md "Cross-cutting caveats")
and trim the `summary` text — Yahoo's `longBusinessSummary` runs 500–1500
characters; quote at most a sentence or two.

For `--summary` mode (peer comparison view), render the array straight to a
markdown table — keys map 1:1 to columns. Drop columns that are null for
every row in the batch (stocks → no fund cols; ETFs → no analyst cols).

> **Cross-currency peer warning.** If `currency` differs across rows
> (e.g., AAPL `USD` next to 0700.HK `HKD`), monetary fields — `market_cap`,
> `target_mean_price`, `trailing_eps`, `total_assets` — are **not directly
> comparable**. Numbers can look similar in magnitude but mean wildly
> different things (4.2T USD vs 4.3T HKD ≈ \$550B USD, ~8× apart). Either
> note the currency per row and skip ranking those columns, or FX-convert
> to a single currency before comparing. Ratios (P/E, yield, margin) are
> fine across currencies — they're unitless.

> Stock peer comparison — same currency (AAPL / MSFT / GOOGL, all USD):
>
> | Symbol | Sector | Cap | P/E (ttm) | P/E (fwd) | EPS | Yield (TTM) | Target | Rating |
> |---|---|---|---|---|---|---|---|---|
> | AAPL | Technology | \$4.22T | 34.8 | 30.0 | \$8.27 | 0.37% | \$303 | buy |
> | MSFT | Technology | \$3.07T | 24.7 | 21.4 | \$16.78 | 0.87% | \$560 | strong buy |
> | GOOGL | Communication Services | \$2.4T | 30.3 | 25.1 | \$8.95 | 0.22% | \$422 | buy |
>
> Stock peer comparison — mixed currency (AAPL vs 0700.HK): **add a
> Currency column and don't rank by Cap / EPS / Target across rows** —
> the numbers aren't directly comparable. Ratios stay valid:
>
> | Symbol | Currency | Sector | Cap (native) | P/E (ttm) | EPS (native) | Yield (TTM) |
> |---|---|---|---|---|---|---|
> | AAPL | USD | Technology | \$4.22T | 34.8 | \$8.27 | 0.37% |
> | 0700.HK | HKD | Communication Services | HK\$4.30T | 22.1 | HK\$22.40 | 0.65% |
>
> ETF peer comparison (SPY / VOO / IVV):
>
> | Symbol | Category | AUM | Expense | 5Y CAGR | Yield (TTM) |
> |---|---|---|---|---|---|

Number formatting:

- `trailing_annual_dividend_yield` is a fraction — multiply by 100 and
  append `%`. For non-dividend payers (TSLA, NFLX) the field is `null`;
  render as `—` or `n/a` and document briefly that the company doesn't
  pay a dividend.
- `five_year_avg_return` is also a fraction (CAGR — already annualized).
- `market_cap` and `total_assets` round nicely with `T` / `B` / `M`
  suffixes (\$4.22T, \$735B).
- `null` for any individual field becomes `—` in the table cell.

## Mode-specific caveats

- **`info` is much slower than `fast_info`.** Each ticker triggers multiple
  Yahoo API calls (~1–3s per ticker), so it's the most rate-limit-prone of the
  three modes. Don't batch more than ~5 tickers in one `info.py` call without
  pausing.
- **Unit landmines.** Most ratios are **fractions** (multiply ×100 for
  display) but a few yield / return fields are encoded as **percent** and
  silently break the rule. Encodings empirically verified across AAPL
  and 0700.HK (verification date in this file's header) — re-run smoke
  if you suspect yfinance/Yahoo shifted:

  | Field | Encoding | Sample → meaning |
  |---|---|---|
  | margins, growth, returns (`profit_margin`, `revenue_growth`, `return_on_equity`, `payout_ratio`, …) | fraction | `0.272` → 27.2% |
  | `dividend.five_year_avg_dividend_yield` | **percent** | `0.51` → 0.51% |
  | `dividend.trailing_annual_dividend_yield` | fraction | `0.0037` → 0.37% |
  | ~~`dividend.dividend_yield`~~ | _dropped_ | percent-encoded; replaced by `trailing_annual_dividend_yield` |
  | `fund.ytd_return` | **percent** | `5.68` → 5.68% |
  | `fund.three_year_avg_return` / `five_year_avg_return` | fraction, **annualized (CAGR)** | `0.1327` → 13.27%/yr |

  CAGR claim verified empirically: SPY's `five_year_avg_return = 0.1327`
  matches 13.23% CAGR computed from 5y price history (cumulative would be
  ~86%). Sanity heuristics: a "yield" field > 1.0 is the percent variant;
  a 3y/5y avg return < 0.5 is the fraction variant. When you need a yield
  without unit guessing, prefer `trailing_annual_dividend_yield` (always
  fraction) or compute `dividend_rate / current_price` yourself.
- **Fund return fields are stale.** `ytd_return`, `three_year_avg_return`,
  `five_year_avg_return` come from the fund's most recent monthly /
  quarterly statement, not real-time NAV. Empirical example: SPY's
  `ytd_return` at one verification date was 5.68% while a same-day calc
  from `history` gave 7.71% — gaps of 1–2% are typical. For up-to-date
  returns prefer [`history --period ytd --summary`](history.md) (or
  `5y --summary`) — that uses today's close. Keep the `info` field only
  when the user explicitly wants the "official" prospectus number.
- **`fund` section is emitted only for ETFs / mutual funds.** For
  `quote_type` ∈ {`ETF`, `MUTUALFUND`} the response includes a `fund`
  section; for everything else it's omitted (reading `data["fund"]` raises
  KeyError on a stock — use `data.get("fund", {})` or check `quote_type`
  first). Conversely `analyst` and most of `fundamentals` are typically
  null for ETFs. Yahoo's ETF metadata is patchy: `expense_ratio` is null
  for many major ETFs (including SPY at last verification), so don't
  promise an expense number — say "not available from Yahoo" if it's `null`.
- **`forward_*` fields are analyst estimates.** `forward_pe`, `forward_eps`
  come from the analyst consensus and can drift from the most recent
  quarterly disclosure — not directly comparable to trailing values.
