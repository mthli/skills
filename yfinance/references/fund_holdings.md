[← back to SKILL.md](../SKILL.md)

# `fund_holdings` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`fund_holdings.py --summary SPY VTI QQQ AGG VFIAX` if you suspect
upstream drift — and especially recheck the `equity_metrics` inversion
(see [Mode-specific caveats](#mode-specific-caveats))._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting fund_holdings results](#presenting-fund_holdings-results) · [Mode-specific caveats](#mode-specific-caveats)

Fund-level data for one or more ETFs / mutual funds — nine sections
per ticker:

- **`fund_overview`** (from `FundsData.fund_overview`) — Morningstar-style
  category, fund family, legal type.
- **`description`** (from `FundsData.description`) — fund prospectus
  summary; can be empty string for some non-US ETFs.
- **`operations`** (from `FundsData.fund_operations`) — expense ratio,
  turnover, total net assets (**AUM in MILLIONS** of fund-reporting
  currency) plus category averages.
- **`asset_classes`** (from `FundsData.asset_classes`) — % stock / bond
  / cash / preferred / convertible / other.
- **`sector_weightings`** (from `FundsData.sector_weightings`) — per-sector
  %; empty `{}` for pure bond funds.
- **`bond_ratings`** (from `FundsData.bond_ratings`) — per-credit-rating
  %; mostly `{us_government: 0.0}` for pure-equity funds, full ladder
  (aaa / aa / a / bbb / bb / b / below_b / other / us_government) for
  bond and mixed funds.
- **`equity_metrics`** (from `FundsData.equity_holdings`) — P/E, P/B,
  P/S, P/CF + median market cap (MILLIONS) + 3y earnings growth, **each
  paired with a `_category_avg` companion** (Morningstar category mean).
  **Yahoo's raw encoding is `1/ratio`** for the four price-multiples;
  we invert and surface as conventional ratios. **`earnings_growth_3y`
  is unit-normalized too** — Yahoo emits PERCENT (raw `18.03`) and we
  divide by 100 to surface a fraction (`0.1803`) matching `info` /
  `financials --summary` conventions. See
  [Mode-specific caveats](#mode-specific-caveats) for evidence.
- **`bond_metrics`** (from `FundsData.bond_holdings`) — duration,
  maturity (both in years), credit quality (opaque numeric), each
  paired with a `_category_avg` companion (often the only populated
  side for equity funds — Yahoo gives the bond-fund category benchmark
  even when the fund has no bond exposure).
- **`top_holdings`** (from `FundsData.top_holdings`) — up to 10 positions
  with symbol / name / weight.

All nine properties **share one Yahoo backend HTTP call** — the
`FundsData` object fetches all four `quoteSummary` modules
(`quoteType`, `summaryProfile`, `topHoldings`, `fundProfile`) on first
property access and serves the rest from cached attributes (verified
2026-05 by reading upstream `yfinance/scrapers/funds.py:_fetch_and_parse`).
So fetching all nine sections costs the same as fetching one.

**Fund-only.** Equity (`AAPL`), index (`^GSPC`), crypto (`BTC-USD`), FX
(`EURUSD=X`), and futures (`ES=F`) all raise `YFDataException("<sym>:
No Fund data found.")` inside yfinance — we catch this and emit
success-with-`note`. The non-fund response carries `quote_type` (the
asset type yfinance resolved BEFORE the parse error raised — verified
2026-05 against AAPL → EQUITY, ^GSPC → INDEX, BTC-USD → CRYPTOCURRENCY,
EURUSD=X → CURRENCY, ES=F → FUTURE), so callers don't need a follow-up
`fast_info` chain to know what kind of asset they got back. Bogus /
delisted tickers raise `HTTPError 404` at fetch time (before yfinance's
parser runs, so no `quote_type` is recoverable) and route through the
standard `error_kind: not_found` path.

## Run

```bash
# Default: full sections, pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py SPY

# Peer compare: flat per-ticker dict
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --summary SPY VTI QQQ AGG

# Cap to top 5 holdings (other sections unaffected)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --limit 5 SPY

# CSV — default mode emits one row per record (with record_class discriminator)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --format csv SPY

# CSV — summary mode emits strict one row per ticker
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fund_holdings.py --format csv --summary SPY VTI QQQ
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap `top_holdings` rows per ticker. Default: keep all
  (Yahoo returns up to 10). Does not affect the other eight sections —
  they're fixed-shape per fund. Yahoo's own cap is ~10 — `--limit 20`
  won't give you 20.
  **Silently ignored in `--summary` mode.** The flat metrics
  (`holdings_concentration`, `holdings_returned`) are computed from
  Yahoo's full response by design — they describe the data, not the
  display knob.
- `--summary` — flat per-ticker projection for ETF peer compare. Lifts
  expense ratio / AUM / asset-class % / top holding / top sector /
  P/E / P/B / duration to top-level fields. Same network cost as
  default (post-fetch projection); use it to save context tokens.
- `--format json|ndjson|csv` — output format. `json` (default) is a
  pretty JSON array. `ndjson` emits one record per line. `csv` shape
  depends on mode:
  - **default mode** — one row per record, with a `record_class`
    discriminator column whose values are `meta` / `operations` /
    `asset_class` / `sector` / `bond_rating` / `equity_metric` /
    `bond_metric` / `holding`. Section-specific columns are populated
    only on the matching row class. Empty / errored tickers emit a
    single carry row with `note` / `error` / meta cols populated.
  - **`--summary` mode** — strict one row per ticker.

## Output schema

### Default mode

Per ticker (illustrative, top_holdings truncated to 3 for brevity):

```json
[
  {
    "symbol": "SPY",
    "quote_type": "ETF",
    "description": "The trust seeks to achieve its investment objective by holding a portfolio of the common stocks ...",
    "fund_overview": {
      "category": "Large Blend",
      "family": "State Street Investment Management",
      "legal_type": "Exchange Traded Fund"
    },
    "operations": {
      "expense_ratio": 0.000945,
      "expense_ratio_category_avg": 0.007218,
      "turnover": 0.03,
      "turnover_category_avg": 0.9461,
      "total_net_assets_millions": 479387.62,
      "total_net_assets_category_avg_millions": 479387.62
    },
    "asset_classes": {
      "stock_pct": 0.9994,
      "bond_pct": 0.0,
      "cash_pct": 0.0006,
      "preferred_pct": 0.0,
      "convertible_pct": 0.0,
      "other_pct": 0.0
    },
    "sector_weightings": {
      "technology": 0.356,
      "financial_services": 0.1179,
      "communication_services": 0.1123,
      "consumer_cyclical": 0.1014,
      "healthcare": 0.085,
      "industrials": 0.083,
      "consumer_defensive": 0.049,
      "energy": 0.0351,
      "utilities": 0.0235,
      "realestate": 0.0192,
      "basic_materials": 0.0178
    },
    "bond_ratings": {
      "us_government": 0.0
    },
    "equity_metrics": {
      "pe_ratio": 26.98,
      "pe_ratio_category_avg": null,
      "pb_ratio": 5.20,
      "pb_ratio_category_avg": null,
      "ps_ratio": 3.59,
      "ps_ratio_category_avg": null,
      "pcf_ratio": 19.58,
      "pcf_ratio_category_avg": null,
      "median_market_cap": null,
      "median_market_cap_category_avg": null,
      "earnings_growth_3y": null,
      "earnings_growth_3y_category_avg": null
    },
    "bond_metrics": {
      "duration_years": null,
      "duration_years_category_avg": null,
      "maturity_years": null,
      "maturity_years_category_avg": null,
      "credit_quality": null,
      "credit_quality_category_avg": null
    },
    "top_holdings": [
      {"symbol": "NVDA",  "name": "NVIDIA Corp",     "weight": 0.0785},
      {"symbol": "AAPL",  "name": "Apple Inc",       "weight": 0.0645},
      {"symbol": "MSFT",  "name": "Microsoft Corp",  "weight": 0.0490}
    ]
  }
]
```

#### `fund_overview`

| Field | Type | Notes |
|---|---|---|
| `category` | str / null | Morningstar-style category (`"Large Blend"`, `"Intermediate Core Bond"`, `"Large Growth"`). |
| `family` | str / null | Fund issuer (`"Vanguard"`, `"iShares"`, `"State Street Investment Management"`). |
| `legal_type` | str / null | Wrapper type (`"Exchange Traded Fund"`, mutual fund variants). |

#### `operations` (6 fields)

| Field | Type | Notes |
|---|---|---|
| `expense_ratio` | float / null | **FRACTION** — annual expense ratio of THIS fund (`0.000945` = 9.45 bps for SPY). Multiply ×100 for display percent, ×10000 for bps. |
| `expense_ratio_category_avg` | float / null | **FRACTION** — same units, averaged across the Morningstar category. Useful for "is this fund cheap for its category?" |
| `turnover` | float / null | **FRACTION** — annual portfolio turnover (`0.03` = 3% turnover for SPY, `0.81` = 81% for AGG). Sometimes `null` for mutual funds (verified VFIAX). |
| `turnover_category_avg` | float / null | Same units, category average. |
| `total_net_assets_millions` | float / null | Fund AUM in **MILLIONS of fund-reporting currency** (`479387.62` = \$479.4B for SPY, USD). Sometimes `null` (VFIAX) or `0.0` (AGG returns 0.0 — Yahoo's data quirk; AGG actually has ~\$130B AUM). **WARNING**: `info.fund.total_assets` reports the same metric in **whole units, not millions** (verified 2026-05: `info.fund.total_assets = 735060819968` for SPY ≈ \$735B vs `fund_holdings.operations.total_net_assets_millions = 479387.62`); the values can also drift between modes because Yahoo serves them from different snapshots. Don't bounce between modes for AUM without converting. Currency = fund's reporting currency (≠ trading currency for cross-listed ETFs — see [Mode-specific caveats](#mode-specific-caveats)). |
| `total_net_assets_category_avg_millions` | float / null | Often equals the per-fund value (Yahoo seems to fall back to repeating the same number for some categories) — don't read it as a meaningful comparison without confirming the values differ. |

#### `asset_classes` (6 fields, all FRACTIONS, sum to ~1.0)

`stock_pct`, `bond_pct`, `cash_pct`, `preferred_pct`, `convertible_pct`,
`other_pct`. Can be slightly negative for leveraged funds (verified:
VFIAX `cash_pct = -0.0003`).

#### `sector_weightings` (variable keys, all FRACTIONS)

11 sectors when populated: `technology`, `financial_services`,
`communication_services`, `consumer_cyclical`, `healthcare`,
`industrials`, `consumer_defensive`, `energy`, `utilities`, `realestate`,
`basic_materials`. **Empty `{}` for pure bond funds** (verified AGG /
BND).

#### `bond_ratings` (variable keys, all FRACTIONS)

Up to 9 buckets when populated: `aaa`, `aa`, `a`, `bbb`, `bb`, `b`,
`below_b`, `other`, `us_government`. Pure-equity funds typically return
just `{us_government: 0.0}`.

#### `equity_metrics` (12 fields — 6 metrics × {fund, category_avg})

Each metric is paired with a `_category_avg` companion (Morningstar category mean
of the same metric — verified populated for VFIAX 2026-05). The category_avg
columns are emitted only into the `equity_metric` row of default-mode CSV;
they're not lifted into `--summary` mode (peer compare focuses on cross-fund,
not within-category, comparison).

| Field | Type | Notes |
|---|---|---|
| `pe_ratio` / `pe_ratio_category_avg` | float / null | Conventional P/E (price ÷ earnings). **We invert Yahoo's raw `1/ratio` encoding** — Yahoo's `priceToEarnings.raw` = 0.03706 for SPY, which we surface as 26.98 (matches Yahoo Finance website's listing). `null` for bond / commodity funds (Yahoo returns 0.0 raw → we map to `null`, since 1/0 isn't meaningful). VFIAX example: `pe_ratio = 25.81`, `pe_ratio_category_avg = 22.98`. |
| `pb_ratio` / `pb_ratio_category_avg` | float / null | Conventional P/B; same inversion. |
| `ps_ratio` / `ps_ratio_category_avg` | float / null | Conventional P/S; same inversion. |
| `pcf_ratio` / `pcf_ratio_category_avg` | float / null | Conventional Price/Cashflow; same inversion. |
| `median_market_cap` / `median_market_cap_category_avg` | int / null | Median market cap of underlying holdings, **in MILLIONS of fund-reporting currency** (verified VFIAX 2026-05: `median_market_cap = 404537` ≈ \$404B median market cap, plausible for an S&P 500 fund's mega-cap-weighted distribution; `category_avg = 335222` ≈ \$335B for the Large Blend category). Coerced with `safe_int` (truncates fractional component, matching `helpers.py` convention for count fields like `marketCap` / `enterpriseValue`). |
| `earnings_growth_3y` / `earnings_growth_3y_category_avg` | float / null | **FRACTION** — 3y trailing earnings growth across underlying holdings. **Yahoo emits PERCENT** (verified VFIAX 2026-05: raw `18.03` → ≈18% S&P 500 3y trailing EPS growth); we **divide by 100** so callers see fractions matching `info.fund.three_year_avg_return` and `financials --summary.*_growth_yoy` conventions. VFIAX example: `earnings_growth_3y = 0.1803`, `category_avg = 0.2125`. |

#### `bond_metrics` (6 fields — 3 metrics × {fund, category_avg})

Same pattern as `equity_metrics` — every metric has a `_category_avg`
companion. Bond ETFs typically populate the fund column; equity funds
often populate only the `category_avg` side (verified VFIAX 2026-05:
`duration_years = null` but `duration_years_category_avg = 4.6`, even
though VFIAX's category is "Large Blend" — equity, not bond).
**The underlying logic is undocumented and we haven't verified it**:
plausibly a Morningstar-style category-mean that includes mixed-asset
funds in Large Blend that hold some bonds, or a Yahoo-side fallback
default. Don't read the value as a clean "this category's bond
duration" signal without sanity-checking.

| Field | Type | Notes |
|---|---|---|
| `duration_years` / `duration_years_category_avg` | float / null | Effective duration in YEARS (3.79 for AGG; 4.6 for the value Yahoo returns under VFIAX's Large Blend category — see caveat above re: source / methodology). `null` for pure-equity funds in the fund column. |
| `maturity_years` / `maturity_years_category_avg` | float / null | Average maturity in years (9.41 for AGG). |
| `credit_quality` / `credit_quality_category_avg` | float / null | Opaque numeric — Yahoo doesn't document the scale. Surface as-is. |

#### `top_holdings` (up to 10 rows)

| Field | Type | Notes |
|---|---|---|
| `symbol` | str / null | Holding ticker (`"NVDA"`). For bond funds the symbol can be a money-market fund ticker (`"BISXX"` for AGG's only listed holding). |
| `name` | str / null | Holding's full name. |
| `weight` | float / null | **FRACTION** of fund AUM (`0.0785` = 7.85% of SPY's portfolio). |

### `--summary` mode

Flat per-ticker dict for peer comparison:

```json
[
  {
    "symbol": "SPY",
    "quote_type": "ETF",
    "category": "Large Blend",
    "family": "State Street Investment Management",
    "expense_ratio": 0.000945,
    "turnover": 0.03,
    "total_net_assets_millions": 479387.62,
    "stock_pct": 0.9994,
    "bond_pct": 0.0,
    "cash_pct": 0.0006,
    "top_holding_symbol": "NVDA",
    "top_holding_weight": 0.0785,
    "holdings_concentration": 0.3843,
    "holdings_returned": 10,
    "top_sector": "technology",
    "top_sector_weight": 0.356,
    "pe_ratio": 26.98,
    "pb_ratio": 5.20,
    "duration_years": null,
    "earnings_growth_3y": null
  }
]
```

`holdings_concentration` is the sum of `weight` across the returned
holdings (≤10) — high values (>50%) flag concentrated funds (QQQ at
~47%), low values (<25%) flag diversified ones (VTI at ~32%).
**Always read alongside `holdings_returned`** — when fewer than 10
rows came back (frequent for bond ETFs: AGG returns 1 row at 2.7%),
this is "concentration of the rows we got", not "top-10 concentration".
For AGG specifically, the 2.7% reading is an artifact of Yahoo not
listing individual bonds, NOT a signal that AGG holds one bond at 2.7%.
`holdings_returned` says how many top_holdings rows Yahoo actually
returned — frequently 10 for equity ETFs, 0–1 for bond ETFs.

### Empty / non-applicable result

When Yahoo raises `YFDataException` (valid symbol but not a fund), the
response is success-with-`note`. The resolved `quote_type` is captured
from yfinance's parser state and surfaced inline:

```json
{
  "symbol": "AAPL",
  "quote_type": "EQUITY",
  "note": "no fund data — Yahoo's funds endpoint covers ETFs and mutual funds only. See `quote_type` for the resolved asset type; use info for equity fundamentals, history for index / crypto / FX / future price data."
}
```

`quote_type` may be `null` in the rare case where the parser failed
before setting it (e.g., a malformed Yahoo response) — fall back to
chaining `fast_info` if present.

### Failed result

Bogus / delisted tickers go through the standard error envelope:

```json
{
  "symbol": "BOGUSXYZ",
  "error": "fetch failed (not_found, after 1 attempt(s))",
  "error_kind": "not_found",
  "attempts": 1
}
```

See SKILL.md "Cross-cutting caveats" for retry semantics and the full
`error_kind` enum.

## Presenting fund_holdings results

**Multiply fractions ×100 when displaying.** All `*_pct`, `weight`,
`expense_ratio`, sector / asset / bond-rating values are fractions.
Render as `7.85%`, not `0.0785`. Same convention as `info` / `holders`.

**Expense ratio is conventionally shown in basis points or percent.**
`0.000945` → `9.45 bps` or `0.0945%`. Cheap ETFs are <10 bps; expensive
ones are >100 bps.

**Single-fund report.** Lead with overview + AUM + expense ratio, then
asset-class mix, then top sector / top holdings. Format template:

> **\<TICKER\> — \<category\> ETF**
> - Family: \<family\>. AUM: \<currency\>\<X.XB\> (\<reporting ccy\>).
> - Expense ratio: \<X.XX\>% (vs category avg \<X.XX\>%).
> - Asset mix: \<XX.X\>% stocks, \<X.X\>% bonds, \<X.X\>% cash.
> - Top sector: \<sector\> (\<XX.X\>%).
> - Top holdings: \<sym1\> (\<X.X\>%), \<sym2\> (\<X.X\>%), ...

**Multi-fund peer compare.** Use `--summary`; render as a table with
columns: ticker | category | expense | AUM | top_holding (sym + %) |
holdings_concentration | P/E (or duration for bond ETFs).

**Don't render `total_net_assets_millions` as raw `479387.62`** — that
reads as 480 thousand. Convert: divide by 1000 for billions or 1000000
for trillions, and label the unit explicitly.

**Bond ETFs.** Substitute `duration_years` for `pe_ratio` in the peer
table — bond ETFs return `null` for the price-multiples but populated
duration / maturity. Use `holdings_concentration` cautiously: AGG
returns 1 row at 2.7% — that's "Yahoo doesn't list individual bonds",
not "AGG holds one bond at 2.7%".

**Escape `$` as `\$` in prose** — same rationale as the other modes.

## Mode-specific caveats

- **`equity_metrics` price-multiples are inverted from Yahoo's raw
  encoding.** Yahoo's `priceToEarnings.raw` etc. are stored as
  `1/ratio`, NOT as the conventional ratio. Verified 2026-05:
  - SPY: raw `priceToEarnings = 0.03706` → 1 / 0.03706 = **26.98**
    (matches Yahoo Finance's website P/E ≈ 27 for SPY).
  - VTI: raw `0.03985` → **25.09** (matches website).
  - QQQ: raw `0.0307` → **32.57** (matches website).
  - AGG (bond ETF): raw `0.0` → **`null`** (we treat 0.0 as
    "no equity component" sentinel; 1/0 isn't meaningful).
  
  Same inversion applied to `priceToBook`, `priceToSales`,
  `priceToCashflow`. The script does the inversion in `_invert_or_none`;
  the JSON / CSV emits already-conventional ratios. **If you build a
  follow-up script that re-uses `funds_data` directly, remember to
  invert manually** — yfinance's upstream parser passes the raw value
  through unchanged.

- **`total_net_assets_millions` is in MILLIONS, not whole units.**
  SPY = `479387.62` means \$479.4B AUM, not \$479K. Common mistake when
  rendering raw — always convert and label.

- **Reporting currency ≠ trading currency for cross-listed ETFs.**
  `total_net_assets_millions`, `median_market_cap`, and `top_holdings`
  values are denominated in the fund's **reporting currency** (the
  currency the issuer's NAV is struck in), which can differ from the
  ticker's **trading currency**. Concrete example: `IWDA.L` (iShares
  Core MSCI World UCITS ETF) trades in GBP on LSE but reports NAV in
  USD; `info.currency` and `fast_info.currency` both return GBP (the
  trading ccy), so chaining them gives the wrong unit for AUM. yfinance
  does not surface the reporting currency directly — you have to know
  the issuer's convention or assume USD for major non-US-listed
  international ETFs. Same trap as the `financials.currency`
  (reporting) vs `fast_info.currency` (trading) split documented in
  SKILL.md cross-cutting caveats. For US-listed funds (SPY, VTI, AGG,
  VFIAX) the two match.

- **Cross-mode AUM unit drift.** `info.fund.total_assets` reports the
  same metric as `fund_holdings.operations.total_net_assets_millions`
  but in **whole currency units**, not millions — verified 2026-05 for
  SPY (`info.fund.total_assets = 735060819968` ≈ \$735B, vs
  `fund_holdings.operations.total_net_assets_millions = 479387.62` ≈
  \$479B). The 1e6× scale difference is silent — render the wrong one
  and you're off by a factor of a million. The values can also drift
  between modes because Yahoo serves them from different snapshots.
  Pick one mode for AUM and stick with it; if mixing, normalize units
  explicitly.

- **Fraction-encoded fields (full list).** Re-stating in one place
  because this is the most likely display bug. All of these are
  fractions in this mode (multiply ×100 for percent display):
  `operations.expense_ratio` / `expense_ratio_category_avg` /
  `turnover` / `turnover_category_avg`; every key in `asset_classes`,
  `sector_weightings`, `bond_ratings`; `top_holdings[*].weight`;
  `equity_metrics.earnings_growth_3y` / `earnings_growth_3y_category_avg`
  (we **divide Yahoo's raw percent by 100**); summary-mode
  `top_holding_weight` / `holdings_concentration` / `top_sector_weight`
  / `stock_pct` / `bond_pct` / `cash_pct`. Consistent with `info` /
  `holders` / `insiders` / `analyst` conventions and inconsistent with
  `fast_info.change_pct` / `history --summary.change_pct` (those are
  percent-encoded). Fields that are NOT fractions in this mode:
  P/E / P/B / P/S / P/CF (multiples); `median_market_cap` /
  `total_net_assets_millions` (millions of currency);
  `duration_years` / `maturity_years` (years); `credit_quality`
  (opaque numeric).

- **`description` can be empty string for non-US ETFs.** Verified
  `IWDA.L` and `0050.TW` return `description = ""` while every other
  section is populated. Don't error on this — it's just thin Yahoo
  metadata coverage.

- **`asset_classes.cash_pct` can be slightly negative.** Verified
  VFIAX: `-0.0003`. Funds with leveraged / shorted positions can have
  negative cash; don't pin to ≥0 in code.

- **Yahoo's data has occasional bad fields — surface AS-IS.**
  AGG returns `total_net_assets_millions = 0.0` despite ~\$130B real
  AUM. VFIAX returns `null` for both `turnover` and
  `total_net_assets_millions`. Don't paper over these — they're Yahoo's
  data quality, not script bugs. If a value looks suspicious, treat
  another data source (issuer's prospectus, Morningstar, the issuer's
  fact sheet) as ground truth — **don't cross-check with `info.fund.*`
  to "fix" it**, both modes draw from Yahoo and the unit conventions
  differ (see "Cross-mode AUM unit drift" above) so a mismatch there
  doesn't tell you which one is right.

- **`category_avg` fields are not always meaningful.** Yahoo
  occasionally fills `*_category_avg` fields with the same value as
  the per-fund field (verified SPY: both `total_net_assets` cells =
  479387.62), making the comparison degenerate. Treat
  `expense_ratio_category_avg` as the most reliable of the bunch
  (varies meaningfully across funds: 0.72% large-blend ETFs vs
  0.54% bond ETFs).

- **All 9 properties share one HTTP call.** Verified by reading
  upstream `yfinance/scrapers/funds.py` — `FundsData._fetch_and_parse`
  hits `/v10/finance/quoteSummary` once with four modules
  (`quoteType`, `summaryProfile`, `topHoldings`, `fundProfile`),
  populates every section as a side effect, and caches everything
  on the `FundsData` instance. Subsequent property accesses are
  free. So the cost is per-ticker, not per-section.

- **YFDataException → success-with-note path.** When Yahoo's
  `quoteSummary` returns a payload missing the expected fund modules
  (i.e., the symbol resolves but isn't a fund), yfinance's parser
  raises `YFDataException("<sym>: No Fund data found.")`. The
  classifier in `helpers.classify_error` doesn't pattern-match this
  message text (it lacks the "404" / "not found" / "no quote"
  substrings the heuristic looks for) — so we catch `YFDataException`
  inside the retry-wrapped closure and return a sentinel that surfaces
  as success-with-note. Bogus / delisted tickers raise `HTTPError 404`
  at the network layer (before the parser runs) and route through the
  standard `error_kind: not_found` path.

- **Quote-type coverage probed empirically (2026-05):**
  - ✅ ETF: `SPY`, `VTI`, `QQQ`, `AGG`, `BND`, `GLD`, `IWDA.L`, `0050.TW`
  - ✅ MUTUALFUND: `VFIAX`
  - ❌ EQUITY: `AAPL` (note path)
  - ❌ INDEX: `^GSPC` (note path)
  - ❌ CRYPTOCURRENCY: `BTC-USD` (note path)
  - ❌ CURRENCY: `EURUSD=X` (note path)
  - ❌ FUTURE: `ES=F` (note path)
  - ❌ Bogus: `BOGUSXYZ` (error path, `not_found`)

- **Commodity ETFs return mostly empty.** Verified `GLD` (gold ETF):
  `description` populated, `fund_overview` populated, `top_holdings`
  empty, `sector_weightings = {}`, `equity_metrics` all null. Treat
  commodity / single-asset ETFs as having only `fund_overview` +
  `operations` + `asset_classes` worth presenting.

- **Hard cap of 10 rows in `top_holdings`.** Yahoo's
  `topHoldings.holdings` array is capped at 10. `--limit 20` won't
  give you 20. For full holdings disclosure, use the issuer's
  prospectus / NPORT-P filings (not in scope for yfinance).
