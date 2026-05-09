[← back to SKILL.md](../SKILL.md)

# `screener` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`screener.py --predefined day_gainers --count 3` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Predefined screens](#predefined-screens) · [Custom queries](#custom-queries) · [Presenting screener results](#presenting-screener-results) · [Mode-specific caveats](#mode-specific-caveats)

Discovery mode — answers questions of the shape "find me tickers where
…". Every other mode in the skill starts from a **known** ticker;
screener is the only path that *produces* tickers from a filter
specification. Two flavors:

- **Predefined**: 19 screens that ship with yfinance (`day_gainers`,
  `undervalued_growth_stocks`, `top_etfs_us`, etc.). Cheapest path —
  Yahoo curates the criteria + sort.
- **Custom**: build an AND/OR tree over fields like `intradaymarketcap`,
  `peratio.lasttwelvemonths`, `epsgrowth.lasttwelvemonths`,
  `dividendyield`. Three quote_types (`equity` / `fund` / `etf`)
  determine the valid field set. Use `--list-fields <quote_type>` to
  enumerate.

## Run

```bash
# Predefined — top intraday US gainers, default count=25
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --predefined day_gainers --count 10

# Predefined — undervalued growth (PE < 20, PEG < 1, EPS growth >= 25%)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --predefined undervalued_growth_stocks --count 25

# Predefined — top ETFs / mutual funds
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --predefined top_etfs_us --count 10
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --predefined top_mutual_funds --count 10

# Catalog discovery
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --list-predefined
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --list-fields equity   # or `fund` / `etf`

# Custom query — US large-caps under PE 15, sort by market cap desc
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --query '{"operator":"and","operands":[
    {"operator":"eq","operands":["region","us"]},
    {"operator":"gt","operands":["intradaymarketcap",1e10]},
    {"operator":"lt","operands":["peratio.lasttwelvemonths",15]}
  ]}' --sort-field intradaymarketcap --count 25

# Custom query — read JSON from file
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --query @my_query.json --quote-type equity

# CSV row-per-quote output, suitable for table rendering
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/screener.py \
  --predefined undervalued_growth_stocks --count 25 --format csv
```

## CLI arguments

Exactly one of these mode flags is required:

- `--predefined NAME` — run a Yahoo predefined screen by name. See
  [Predefined screens](#predefined-screens) for the catalog.
- `--query JSON` — run a custom AND/OR query. The arg is a JSON tree
  (literal string or `@path/to/file.json`). See
  [Custom queries](#custom-queries) for the grammar and a walked
  example.
- `--list-predefined` — print the catalog of predefined screens.
  Output is a **flat list** (one row per screen) with fields
  `name / quote_type / sort_field / sort_asc / description`.
  Supports `--format json|ndjson|csv|symbols` (csv = one row per
  screen with the standard columns; symbols = just the screen names,
  one per line — convenient for shell loops).
- `--list-fields {equity,fund,etf}` — print valid screener fields
  grouped by category, plus enum value sets for fields that take a
  fixed-vocab value (`region`, `exchange`, `sector`, `industry`,
  `categoryname`, etc.). **JSON-only** (the field schema is nested);
  passing `--format ndjson|csv|symbols` is rejected with a clear error.
  Use this to discover field names for `--query`.

Shared with both `--predefined` and `--query`:

- `--quote-type {equity,fund,etf}` — only meaningful with `--query`.
  When omitted with `--query`, defaults to `equity`. (argparse default
  is `None`, which lets us tell "user explicitly chose" apart from
  "user accepted the default" — used for the mismatch warning paired
  with `--predefined`.) Determines which Query subclass wraps the tree
  (`EquityQuery` / `FundQuery` / `ETFQuery`), which dictates Yahoo's
  `quoteType` filter and the valid field set. With `--predefined`,
  passing a *mismatching* quote_type prints a stderr warning; passing
  the matching one (e.g. `--quote-type equity` alongside
  `--predefined day_gainers`) is silent (redundant but harmless).
- `--count N` — max results to return (default 25 per Yahoo, max 250).
  Maps to `count` for predefined screens and `size` for custom queries
  (the upstream API uses different parameter names for the two paths;
  the script handles the dispatch).
- `--offset N` — pagination offset (default 0). With `--predefined`,
  supplying `--offset` switches yfinance internally to the
  custom-query API path because Yahoo's predefined endpoint ignores
  offset. Functionally invisible to you, but worth knowing if you're
  reading yfinance source.
- `--sort-field FIELD` — field to sort by. Predefined screens supply
  their own default (visible via `--list-predefined`); custom queries
  default to Yahoo's `ticker`.
- `--sort {asc,desc}` — sort direction. When omitted, predefined screens
  use the direction baked into their definition (visible via
  `--list-predefined`) and custom queries default to descending. Use this
  to override — e.g. `--sort asc` to flip a desc-by-default predefined.
  **Caveat:** Yahoo's predefined endpoint silently ignores sort flags;
  the override only takes effect with `--predefined` if you also pass
  `--offset N` (which switches yfinance to the custom-query API path).
  For pure custom `--query` calls the override always works.
- `--format {json,ndjson,csv,symbols}` — output format. See
  [Output schema](#output-schema). `symbols` is a pipe-friendly
  one-ticker-per-line emission for chaining into other modes
  (`screener.py … --format symbols | xargs info.py`).
- `--full` — pass through the raw Yahoo quote payload (~60–85 fields,
  varies by quote_type: ~85 for equity, ~75 for ETF, ~58 for mutual
  fund) instead of the curated 28-field projection. Useful when you
  need fields outside the projection (`epsCurrentYear`, `dividendDate`,
  `bookValue`, `trailingThreeMonthReturns`, etc.). **Incompatible with
  `--format csv`** (raw keys would break CSV column stability across
  calls); use `--format ndjson` for raw rows or drop `--full` for the
  projected CSV.

## Output schema

Unlike the per-ticker wrappers (which emit a JSON array, one record
per ticker), screener emits a **single envelope dict** — one screener
call, one result. NDJSON emits one quote per line (envelope keys
dropped); CSV emits one row per quote (no envelope columns).

### JSON (default)

Envelope is **metadata-first, quotes last** — so the screen identity /
total / counts stay visible without scrolling past 25-250 rows of
quote data:

```json
{
  "predefined": "day_gainers",
  "title": "Day Gainers",
  "description": "Discover the equities with the greatest gains in the trading day.",
  "total": 242,
  "returned": 3,
  "offset": 0,
  "quotes": [
    { "symbol": "INOD", "name": "Innodata Inc.", ... },
    { "symbol": "RKLB", "name": "Rocket Lab Corporation", ... },
    { "symbol": "FLNC", "name": "Fluence Energy, Inc.", ... }
  ]
}
```

Envelope fields:

| Field | Type | Notes |
|---|---|---|
| `total` | int | Total rows matching the query (Yahoo cap is high; you can paginate via `--offset` if you need beyond `--count`). |
| `returned` | int | Rows actually emitted in this response (≤ `--count`). |
| `offset` | int | Echo of the `--offset` arg (`start` in Yahoo's payload). |
| `quotes` | list | Per-quote projection — see below. |
| `predefined` | str / null | Echo of `--predefined` if used; absent for custom. |
| `title` / `description` | str / null | Yahoo-supplied title + description for predefined screens; absent for custom. |
| `quote_type_filter` | str | Echoed only for `--query`. One of `EQUITY` / `MUTUALFUND` / `ETF`. |
| `attempts` | int | Surfaces only when retried (transient 429 / network); absent on clean first-attempt success — same convention as the per-ticker modes. |
| `note` | str | Set when `quotes` is empty (zero matches). Mutually exclusive with `error` per the skill-wide note convention. |

Per quote (28 fields). **All fields are present in every row regardless
of `quote_type`** — equity-only fields are null for ETFs / funds, and
ETF/fund-only fields are null for equities. Identifying which set
applies is straightforward via `quote_type`:

| Field | Type | Applies to | Notes |
|---|---|---|---|
| `symbol` | str | all | Yahoo ticker. |
| `name` | str | all | `longName` ‖ `shortName` ‖ `displayName`. |
| `quote_type` | str | all | `EQUITY`, `ETF`, `MUTUALFUND`. |
| `exchange` | str | all | Short Yahoo code (`NMS`, `NYQ`, `PCX`, `NAS`, …). See SKILL.md "exchange codes". |
| `full_exchange_name` | str | all | Human exchange name (`NasdaqGS`, `NYSE`, `NYSEArca`, `Nasdaq`, …). Already user-friendly — prefer over `exchange` when rendering. |
| `currency` | str | all | Trading currency. |
| `region` | str | all | Two-letter region code (`US`, `HK`, `JP`, …). |
| `price` | float | all | Current `regularMarketPrice` in trading currency. |
| `change_pct` | float | all | **PERCENT** today vs. previous close (e.g. `5.2` = 5.2%). Same encoding as `fast_info.change_pct`. |
| `volume` | int | all | Today's volume. |
| `avg_volume_3m` | int | all | 3-month average daily volume. |
| `market_cap` | int | EQUITY | Null for ETFs / funds (they have `net_assets`). |
| `trailing_pe` | float | EQUITY | Null when EPS ≤ 0 or unavailable. **Clamped to null when `|v| > 1000`** — see [forward_pe sanity clamp](#forward-pe-clamp). |
| `forward_pe` | float | EQUITY | Forward-EPS-based; can be negative when forward EPS is negative (expected losses, not a bargain). **Clamped to null when `|v| > 1000`** — Yahoo otherwise emits values like `-199000` or `136000` when forward EPS is near zero (division blows up). See [forward_pe sanity clamp](#forward-pe-clamp). |
| `price_to_book` | float | EQUITY | |
| `eps_ttm` | float | EQUITY | TTM EPS. |
| `eps_forward` | float | EQUITY | Forward EPS estimate. |
| `trailing_annual_dividend_yield` | float | all | **FRACTION** (`0.0123` = 1.23%). Matches `info.trailing_annual_dividend_yield` encoding. Null / 0.0 for non-payers. **Distinct from Yahoo's `dividendYield` / `yieldTTM` raw fields** which are PERCENT — we project the fraction-encoded one for cross-mode consistency. |
| `trailing_annual_dividend_rate` | float | all | Dividend per share annually, in trading currency. |
| `fifty_two_week_high` | float | all | |
| `fifty_two_week_low` | float | all | |
| `fifty_two_week_change_pct` | float | all | **PERCENT** (`137.05` = 137% gain over 52w). Matches the skill-wide PERCENT convention used by `change_pct`. |
| `next_earnings_date` | str (ISO date) | EQUITY | UTC date from `earningsTimestamp`. Null for ETFs / funds / no-coverage equities. |
| `net_assets` | int | ETF, MUTUALFUND | AUM in trading currency. Null for equities. |
| `expense_ratio_pct` | float | ETF, MUTUALFUND | **PERCENT** (`0.6` = 0.60%). |
| `ytd_return_pct` | float | ETF, MUTUALFUND | **PERCENT** YTD return (`64.81` = 64.81%). |
| `three_year_return_pct` | float | ETF, MUTUALFUND | **PERCENT** trailing-3y NAV total return (Yahoo's `annualReturnNavY3`). **Closer to annualized than cumulative**, but Yahoo's exact methodology isn't documented and the value diverges from a naive `(today_price / 3y_ago_price)^(1/3) − 1` calculation by 5–30 pp depending on ticker (verified empirically across 10 ETFs in 2026-05). Likely uses NAV (not market price), chained monthly returns, and a month-end reference date. Treat as a comparable cross-ETF signal, not a precise return calculation. **Definitely not cumulative** — values stay in the ~10–120% range while cumulative 3y returns for these same ETFs were 95–400%. |
| `five_year_return_pct` | float | ETF, MUTUALFUND | **PERCENT** trailing-5y NAV total return (`annualReturnNavY5`). Same caveats as `three_year_return_pct`. |

**Unit landmines.** Yahoo's screener payload mixes encodings within a
single quote — same family-wide quirk as `info`. Cheat sheet:

- **Percent-encoded** (skill-wide convention: `_pct` suffix or `change_pct`):
  `change_pct`, `fifty_two_week_change_pct`, `expense_ratio_pct`,
  `ytd_return_pct`, `three_year_return_pct`, `five_year_return_pct`.
  `1.23` = 1.23%.
- **Fraction-encoded**: `trailing_annual_dividend_yield`. `0.0123` =
  1.23%. Convention matches `info.trailing_annual_dividend_yield` —
  skipping the explicit suffix because adding `_fraction` everywhere
  would be noisy and the field name is already long.

**Empty result is success, not error.** Zero matches →
`returned: 0, quotes: []` + a `note` string; **no** `error_kind` is
set. Same convention as `news` / `holders` / `analyst`. The CSV path
projects `note` and the meta cols so empty runs still emit one row
carrying the disambiguation signal.

### NDJSON

One JSON object per **quote**, line by line. Envelope metadata
(`total`, `predefined`, `title`) is **not** emitted — NDJSON is for
streaming the rows, not the metadata. On error / no-match, the script
emits a single line carrying the envelope keys (`error` / `note` /
`predefined` / etc.) so consumers see the failure rather than parse
empty stdout.

### CSV

One row per **quote**. Header is the 28 quote fields followed by
`note` and the 3 meta cols (`error`, `error_kind`, `attempts`) — same
schema across all calls regardless of envelope shape. On error /
no-match, the script emits one row with empty quote columns and the
relevant `note` / meta cols populated. Envelope metadata (`total`,
`predefined`, `title`) is **not** projected to CSV.

CSV is **incompatible with `--full`** — argparse rejects the combo
upstream. Use `--format ndjson` with `--full` for raw rows.

### symbols

One ticker per line, no header. On error / no-match, stdout stays empty
**but the error or `note` is surfaced to stderr** (rc unchanged), so
shell pipelines can detect failure rather than silently producing
nothing. Designed for chaining:
`screener.py --predefined day_gainers --count 50 --format symbols |
xargs uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py
--summary`. Useful for the discovery → details two-step (see SKILL.md
"Discovery → details"). NB: `--list-predefined` rejects `--format
symbols` because the catalog has screen names, not tickers — use
`--format json` (or `csv`) for catalog discovery.

## Predefined screens

19 screens ship with yfinance, grouped by quote_type. Run
`--list-predefined` for the canonical list (and the up-to-date
sort-field defaults). Snapshot as of yfinance 1.3.x:

**Equity (9):**
- `aggressive_small_caps` — small caps with EPS growth < 15% (NMS / NYQ).
- `day_gainers` — US stocks up > 3% intraday, market cap ≥ \$2B, price ≥ \$5.
- `day_losers` — US stocks down > 2.5% intraday, market cap ≥ \$2B.
- `growth_technology_stocks` — Technology sector with quarterly revenue growth ≥ 25% AND TTM EPS growth ≥ 25%.
- `most_actives` — US stocks with daily volume > 5M, market cap ≥ \$2B.
- `most_shorted_stocks` — sorted by `short_percentage_of_shares_outstanding`.
- `small_cap_gainers` — market cap < \$2B, NMS / NYQ.
- `undervalued_growth_stocks` — TTM PE in (0, 20), PEG (5y) < 1, TTM EPS growth ≥ 25%.
- `undervalued_large_caps` — TTM PE in (0, 20), PEG (5y) < 1, market cap \$10B–\$100B.

**Mutual fund (6):**
- `conservative_foreign_funds`, `high_yield_bond`, `portfolio_anchors`,
  `solid_large_growth_funds`, `solid_midcap_growth_funds`,
  `top_mutual_funds` — all gated on Morningstar performance rating ≥ 4
  and various risk / category filters.

**ETF (4):**
- `top_etfs_us`, `top_performing_etfs`, `technology_etfs`, `bond_etfs`.

The exact filter trees are visible in upstream
`yfinance/screener/screener.py` (look for `PREDEFINED_SCREENER_QUERIES`)
— useful as templates when building your own custom queries.

## Custom queries

The query is an AND/OR tree of field comparisons. Grammar:

```json
{
  "operator": "<op>",
  "operands": [<sub-query> | <leaf-value>, ...]
}
```

**Operators:**

| Op | Operands | Semantics |
|---|---|---|
| `and` / `or` | list of ≥ 2 sub-queries | Boolean combinator. |
| `eq` | `[field, value]` | Field equals value. Value-restricted fields (e.g. `region`, `sector`, `exchange`, `industry`) only accept the enum values listed in `--list-fields`. |
| `is-in` | `[field, val1, val2, ...]` | Field ∈ {values}. Internally expanded to `or` of `eq`s. |
| `gt` / `lt` / `gte` / `lte` | `[field, number]` | Numeric comparison. |
| `btwn` | `[field, low, high]` | Inclusive numeric range. |

**Walked example.** "US large-caps with TTM PE < 15, sorted by market
cap descending":

```json
{
  "operator": "and",
  "operands": [
    {"operator": "eq",  "operands": ["region", "us"]},
    {"operator": "gt",  "operands": ["intradaymarketcap", 1e10]},
    {"operator": "lt",  "operands": ["peratio.lasttwelvemonths", 15]}
  ]
}
```

Run as:

```bash
screener.py --query '<the JSON above>' --sort-field intradaymarketcap --count 25
```

**Field discovery.** Field names are non-obvious (Yahoo's screener
fields don't always match the `info` field names — e.g. it's
`peratio.lasttwelvemonths` here, not `trailingPE`). Run
`--list-fields equity|fund|etf` to enumerate. Output is grouped by
category (price, valuation, dividend, financials, etc.) plus a
`valid_values` map for enum-restricted fields like `region`, `sector`,
`exchange`, `industry`, `categoryname`.

**Validation happens client-side.** yfinance's `EquityQuery` /
`FundQuery` / `ETFQuery` validate field names and enum values at
construction time and raise `ValueError`. Our wrapper catches that and
returns `error_kind: not_found` with the underlying message. So a
typo'd field is a fast user error, not a wasted Yahoo round-trip.

## Presenting screener results

**Single screen, narrow scan.** A short bullet list with name,
ticker, the headline numbers (price + change), and the most relevant
filter-driving field works well:

> **Day Gainers** (top 10 of 242)
> - **INOD** Innodata Inc. — \$84.89 (+86.0% today, market cap \$2.8B)
> - **RKLB** Rocket Lab Corporation — \$105.47 (+34.2% today, market cap \$61B)
> - …

**Wide table, peer comparison.** When showing many tickers (≥ 5),
prefer a markdown table — same approach as `info --summary`. Pick the
3–5 columns that drove the screener's selection. For
`undervalued_growth_stocks`: symbol, name, price, trailing PE,
forward PE, market cap. For `top_etfs_us`: symbol, name, expense
ratio, YTD return, 3y return, AUM. **Don't just dump all 28 columns**
— most aren't load-bearing for the user's question.

**Currency for non-US results.** Custom queries that don't filter on
`region` will mix currencies. Always include the `currency` column
when results are heterogeneous, and don't sort by `price` across
mixed currencies (the comparison is meaningless).

**Escape `$` as `\$`** in prose — same skill-wide markdown caveat.

## Mode-specific caveats

- **One screen call ≠ one ticker call.** Screener returns ≤ 250 rows
  in one HTTP, so the per-quote latency cost is much lower than batch
  `info` for the same N. But each quote carries a *partial* fundamentals
  set — only the ~28 fields we project (raw payload is ~60-85 fields,
  varies by quote_type — accessible via `--full`). For
  full per-ticker data (sector, industry, profile, financials, etc.)
  follow the screen with a per-symbol mode (`info`, `financials`, …)
  on the result. Treat screener as the funnel that produces tickers,
  not as a one-shot data source.
- **`marketCap` vs `intradaymarketcap`.** Yahoo's screener field is
  `intradaymarketcap` (custom query) but the **response** field is
  `marketCap` (which we project as `market_cap`). They're the same
  underlying value — the naming asymmetry is upstream's quirk, not
  ours. Same pattern for `dayvolume` (filter) → `regularMarketVolume`
  (response, projected as `volume`).
- **`peratio.lasttwelvemonths` semantics.** This is the screener
  field name. Yahoo's response field is `trailingPE` (projected as
  `trailing_pe`). When filtering on this field, note that Yahoo
  returns 0 for negative-EPS stocks (so `lt: 20` would include
  loss-makers). The predefined `undervalued_growth_stocks` /
  `undervalued_large_caps` use `btwn: [0, 20]` — the explicit lower
  bound matters. **Replicate that pattern** in custom queries when
  you want "low PE among profitable stocks".
- **No `quote_type` filter in custom queries.** The `--quote-type`
  arg only controls which Query subclass wraps your tree (and
  therefore which fields are valid). It implicitly sets Yahoo's
  `quoteType` filter, but you can't combine quote_types in one query
  (no equity-OR-ETF). For mixed-type discovery, run two screens and
  union the results consumer-side.
- **Field availability per quote_type asymmetry.** Some fields exist
  in equity but not fund / ETF (`peratio.lasttwelvemonths`,
  `epsgrowth.lasttwelvemonths`); others exist only in fund / ETF
  (`fundnetassets`, `annualreportnetexpenseratio`,
  `categoryname`). `--list-fields` is authoritative — don't transfer
  field names across quote_types.
- **Predefined screen results are point-in-time.** `day_gainers` /
  `day_losers` / `most_actives` change throughout the trading day.
  Two consecutive calls minutes apart can return different ticker
  sets — that's expected, not a bug. For "stable" screens
  (`undervalued_growth_stocks`, `growth_technology_stocks`) the
  result set drifts on a slower cadence (quarterly fundamentals
  updates, daily price moves).
- **`offset` quirk for predefined.** Yahoo's predefined-saved endpoint
  ignores `offset`, so yfinance silently switches to the custom-query
  endpoint when you supply `--offset` with `--predefined`. The
  result shape is the same to you, but `attempts` / latency may
  differ slightly from a no-offset predefined call. If you don't
  need pagination, omit `--offset` for the cheaper path.
- **No `--summary` mode.** The default output is already in
  summary-style flat shape (one row per quote, ~28 fields). Use
  `--format csv` or `--format ndjson` for tabular / streaming
  consumption; pick a subset of columns presentation-side.
- **Errors don't retry on `not_found`.** Bad predefined name, invalid
  custom-query field, malformed JSON — all classified as `not_found`
  (no point retrying). Rate-limit / network errors retry up to 3
  times with exponential backoff per the shared `with_retry` helper
  (see SKILL.md "Retry semantics").
- **Unit landmines (recap, also in [Output schema](#output-schema)).**
  `change_pct` / `fifty_two_week_change_pct` / `*_return_pct` /
  `expense_ratio_pct` are PERCENT;
  `trailing_annual_dividend_yield` is FRACTION. Don't display them
  without converting first.
- **<a id="forward-pe-clamp"></a>`forward_pe` / `trailing_pe` sanity
  clamp.** Yahoo emits values like `-199000` (RKLB observed) or
  `136000` when the underlying EPS is near zero (division blows up).
  The projection clamps `|value| > 1000` to null on both
  `trailing_pe` and `forward_pe`. The threshold is conservative — the
  most extreme legitimate PE we've observed in real data is ~300 for
  hot growth names, so 1000 cleanly separates real values from
  near-zero-EPS garbage. Use `--full` if you want the raw Yahoo value
  passed through; the clamp only applies to the projected schema.
