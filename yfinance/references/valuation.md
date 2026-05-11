[← back to SKILL.md](../SKILL.md)

# `valuation` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`valuation.py --summary AAPL SPY BOGUS123` if you suspect upstream drift.
This mode is unusually drift-prone — see [HTML-scrape fragility](#html-fragility)._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting valuation results](#presenting-valuation-results) · [Mode-specific caveats](#mode-specific-caveats)

Historical valuation snapshots for one or more tickers — Yahoo's
key-statistics page returns a 9-metric × 6-period table:

- **9 metrics** per period: `market_cap`, `enterprise_value`,
  `trailing_pe`, `forward_pe`, `peg_ratio`, `price_to_sales`,
  `price_to_book`, `ev_to_revenue`, `ev_to_ebitda`.
- **6 periods** per ticker: `Current` (live, latest-price-driven) +
  the last 5 quarter-end snapshots (`YYYY-MM-DD` ISO-formatted).

**Unique vs `info`**: `info.valuation` returns ONLY the current
snapshot. This mode adds the temporal dimension — the last ~15 months
of how those metrics have moved. Field names match `info.valuation.*`
exactly so the two outputs interoperate: `info.valuation.trailing_pe`
↔ `valuation[0].periods[0].trailing_pe` (the `current` row).

**Equity-only.** ETFs / mutual funds / indexes / crypto / FX / futures
all return the empty case; ADRs (TM, BABA) and non-US primary listings
(`0700.HK`, `BMW.DE`) work. See
[All-empty is ambiguous](#empty-ambiguous) for handling.

## Run

```bash
# Default: full 9-metric × 6-period table, pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/valuation.py AAPL

# Peer rollup: flat per-ticker dict with current / min / max for the
# 4 most-asked ratios (trailing_pe, forward_pe, price_to_book, ev_to_ebitda)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/valuation.py --summary AAPL MSFT GOOGL

# CSV — default mode emits one row per period
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/valuation.py --format csv AAPL

# CSV — summary mode: strict one row per ticker
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/valuation.py --format csv --summary AAPL MSFT 0700.HK
```

Tickers are positional args. Case-insensitive; non-US needs a suffix
(`0700.HK`, `BMW.DE`).

## CLI arguments

- `--summary` — flat per-ticker projection. Lifts current market cap +
  current/min/max across the period window for the 4 most-asked
  ratios (`trailing_pe`, `forward_pe`, `price_to_book`, `ev_to_ebitda`)
  to top-level fields. PEG, price_to_sales, ev_to_revenue, and the
  full enterprise_value series are dropped (they're available in
  default mode if needed). Same network cost as default (post-fetch
  projection); use to save context tokens. ~5× smaller output.
- `--format json|ndjson|csv` — output format. `json` (default) is
  the pretty JSON array, one record per ticker. `ndjson` emits one
  JSON record per ticker per line. `csv` shape depends on mode:
  - **default mode** — one row per period, with `period_label` and
    `period_date` as discriminators and `symbol` repeating across a
    ticker's 6 rows. Empty / errored tickers emit a single row
    carrying `symbol` + `note` + meta fields.
  - **`--summary` mode** — strict one row per ticker (every column
    populated where the ticker has data). Same row shape across all
    tickers, friendly for spreadsheet pivots.

CSV column order (default mode, left to right): `symbol`,
`period_label`, `period_date`, the 9 metric columns, `note`, then the
3 meta fields (`error`, `error_kind`, `attempts`).

## Output schema

### Default mode

Per ticker (illustrative — truncated to 2 periods):

```json
[
  {
    "symbol": "AAPL",
    "periods": [
      {
        "period_label": "current",
        "period_date": null,
        "market_cap": 4310000000000,
        "enterprise_value": 4320000000000,
        "trailing_pe": 35.51,
        "forward_pe": 33.44,
        "peg_ratio": 2.57,
        "price_to_sales": 9.64,
        "price_to_book": 40.46,
        "ev_to_revenue": 9.58,
        "ev_to_ebitda": 27.03
      },
      {
        "period_label": "2026-03-31",
        "period_date": "2026-03-31",
        "market_cap": 3720000000000,
        "enterprise_value": 3750000000000,
        "trailing_pe": 32.13,
        "forward_pe": 29.5,
        "peg_ratio": 2.25,
        "price_to_sales": 8.69,
        "price_to_book": 42.21,
        "ev_to_revenue": 8.6,
        "ev_to_ebitda": 24.5
      }
    ]
  }
]
```

#### Per-period row schema (11 fields)

| Field | Type | Notes |
|---|---|---|
| `period_label` | str | Either the literal `"current"` (live row) or an ISO date `"YYYY-MM-DD"` (quarter-end snapshot). Mirrors `period_date` when the latter is set. Use this as a stable column key in tables — the `Current` row's date drifts daily, but its label stays `"current"`. |
| `period_date` | str / null | ISO `"YYYY-MM-DD"` for quarter-end snapshots, `null` for the `current` row. The `current` row's effective date is "now" (latest price + most-recent reported fundamentals), not a fixed point. |
| `market_cap` | int / null | Shares outstanding × current price. **TRADING currency** (USD for AAPL, HKD for `0700.HK`, EUR for `BMW.DE`). The `current` row is real-time; quarter-end rows reflect that quarter's closing price. |
| `enterprise_value` | int / null | `market_cap + total_debt - cash_and_equivalents`. Same currency as `market_cap`. |
| `trailing_pe` | float / null | Price ÷ TTM EPS. `null` for loss-makers (Yahoo emits `--`, not negative — verified PLUG / RIVN / NIO 2026-05). |
| `forward_pe` | float / null | Price ÷ analyst consensus next-12-month EPS. `null` when consensus EPS is negative or coverage is thin. |
| `peg_ratio` | float / null | Forward P/E ÷ analyst 5-year EPS growth estimate. Highly variable coverage — often `null` for small / mid caps. |
| `price_to_sales` | float / null | Market cap ÷ TTM revenue. Always populated for revenue-generating companies. |
| `price_to_book` | float / null | Market cap ÷ book equity. Less meaningful for asset-light businesses (software, internet); standard for financials and asset-heavy industries. |
| `ev_to_revenue` | float / null | EV ÷ TTM revenue. |
| `ev_to_ebitda` | float / null | EV ÷ TTM EBITDA. `null` for negative-EBITDA periods. |

**All metrics are unitless ratios** except `market_cap` and
`enterprise_value` (in trading currency). No percent-vs-fraction
ambiguity in this mode — every field is its conventional
human-readable magnitude (`35.51` ≡ "P/E of 35.51", not 0.3551).

### `--summary` mode

Flat per-ticker dict — current market cap + current/min/max for the 4
most-asked ratios across the full returned window:

```json
[
  {
    "symbol": "AAPL",
    "periods_returned": 6,
    "oldest_period_date": "2025-03-31",
    "current_market_cap": 4310000000000,
    "current_trailing_pe": 35.51,
    "min_trailing_pe": 31.96,
    "max_trailing_pe": 38.64,
    "current_forward_pe": 33.44,
    "min_forward_pe": 25.71,
    "max_forward_pe": 33.44,
    "current_price_to_book": 40.46,
    "min_price_to_book": 40.46,
    "max_price_to_book": 57.14,
    "current_ev_to_ebitda": 27.03,
    "min_ev_to_ebitda": 22.31,
    "max_ev_to_ebitda": 27.92
  }
]
```

| Field | Notes |
|---|---|
| `periods_returned` | Number of period columns Yahoo returned for the ticker (typically 6; surfaced so you know what window the min/max covers). |
| `oldest_period_date` | ISO date of the oldest quarter-end snapshot in the window — combine with the `current` value to compute your own "vs ~5q ago" delta if needed. |
| `current_market_cap` | Market cap from the period whose `period_date` is `null` (the "current" row). Located by structural marker rather than list position, so the lookup survives any future Yahoo emit-order reshuffle. Quick size signal alongside the ratio trends. |
| `current_<ratio>` | Latest value (lifted from the `current` row). |
| `min_<ratio>` | Minimum across the full returned window (current + all 5 snapshots). `null` values are excluded — for loss-makers with `--` P/E across every period, min/max stay `null` rather than crashing. |
| `max_<ratio>` | Maximum across the window. Same null-exclusion. |

**The 4 trend metrics in summary** are `trailing_pe`, `forward_pe`,
`price_to_book`, `ev_to_ebitda` — picked for practical "is X
currently expensive vs the recent past" use. `peg_ratio`,
`price_to_sales`, `ev_to_revenue`, and the size metrics
(`enterprise_value`) are dropped from summary; they're available in
default mode if needed.

### Empty / non-applicable result

When the key-statistics scrape returns an empty DataFrame, the response
is success with a `note` rather than an error. See
[All-empty is ambiguous](#empty-ambiguous) below.

```json
{
  "symbol": "SPY",
  "periods": [],
  "note": "no valuation data (Yahoo's key-statistics scrape returned empty — expected for ETFs / mutual funds / indexes / crypto / FX / futures and bogus tickers; if seen on a blue-chip equity, Yahoo may have restyled the page and broken the HTML scrape — call fast_info to disambiguate)"
}
```

### Failed result

```json
{
  "symbol": "AAPL",
  "error": "fetch failed (rate_limit, after 3 attempt(s))",
  "error_kind": "rate_limit",
  "attempts": 3
}
```

See SKILL.md "Cross-cutting caveats" for retry semantics and the full
`error_kind` enum.

**In `--summary` mode, error tickers also have `periods_returned: 0`**
(no periods key in the failed result → defaults to empty list →
length 0). That's the same number a legitimate empty-success ticker
emits, so don't try to disambiguate via `periods_returned` alone.
Use the `error` / `error_kind` vs `note` fields instead — the
cross-cutting convention is that `note` and `error` never co-occur,
so exactly one of them will be present and tells you which case
you're in.

## Presenting valuation results

**Single-ticker trend.** Lead with the current ratio + where it sits
in the recent range, then the date range covered. Format template
(placeholders — illustrative shape):

> **\<TICKER\> — valuation trend** (last ~\<N\> quarters, \<oldest\>→today)
> - Trailing P/E: **\<X.XX\>** _(range \<min\>–\<max\>)_
> - Forward P/E: **\<X.XX\>** _(range \<min\>–\<max\>)_
> - EV/EBITDA: **\<X.XX\>** _(range \<min\>–\<max\>)_
> - Market cap: **\<ccy\>\<N\>B/T**

**State whether the current value is near the high or low.** That's
the headline answer to "is X cheap or expensive right now?" — e.g.
"AAPL's trailing P/E of 35.5 is below its 1y high of 38.6 but well
above its 1y low of 32.0 — close to the upper end of the range."

**Always state the currency on `market_cap` / `enterprise_value`.**
For US tickers `\$` is fine; for `0700.HK` write `HK\$`, for `BMW.DE`
write `€`. If you don't know the trading currency, call `fast_info`
first or omit the absolute values and quote ratios only.

**Multi-ticker peer compare.** Use `--summary`; render as a table
with `current_*` columns and parenthetical `(min–max)` ranges. Don't
mix tickers from different currencies in one `market_cap` column
without converting — apples-to-bananas otherwise. Either filter to one
currency or annotate the column with per-ticker currency.

**Escape `$` as `\$` in prose** — same rationale as the other modes.

**Don't compute percentages from the ratios.** All 9 metrics are
already in their conventional magnitudes; multiplying by 100 produces
nonsense (35.51 → "3551%"). The only thing you'd compute is a
percent-change between two snapshots (e.g. "current vs ~5q ago"),
which IS a legitimate percent operation: `(current - oldest) / oldest × 100`.

## Mode-specific caveats

- <a id="html-fragility"></a>**HTML-scrape fragility (the big one).**
  yfinance implements `Ticker.get_valuation_measures()` as a
  BeautifulSoup parse of the Yahoo `key-statistics` page (verified via
  source inspection 2026-05) — NOT a structured `quoteSummary` API
  call. Values arrive as pre-rounded display strings (`"4.31T"`,
  `"35.51"`, `"--"`); we parse the magnitude suffixes back into raw
  numbers, but the underlying precision is whatever Yahoo's renderer
  displayed (typically 3 sig figs). **This is the most fragile
  yfinance mode in the skill.**

  We mitigate two of the most likely drift modes inside the wrapper:
  - **Row-label qualifier changes** (e.g. Yahoo renaming
    `"PEG Ratio (5yr expected)"` to `"PEG Ratio (5y forward)"`) are
    handled by prefix-match resolution — anchors in `_ROW_LABEL_TO_KEY`
    are bare prefixes (`"PEG Ratio"`), so any parenthetical change
    survives. The PEG canary in smoke catches a more disruptive change
    (renaming the row to something that doesn't start with `"PEG"`).
  - **Float-precision artifacts** (`"4.31T"` parsing to `...9999.9995`)
    are corrected by round-not-truncate when coercing to int.

  Two drift modes we DON'T mitigate:
  - **Column-format restyle** (`"$4.31 trillion"` instead of `"4.31T"`).
    Our parser falls through to `safe_float`, which returns None,
    surfacing as silent nulls.
  - **Wholesale page restructure** (Yahoo replaces the HTML table
    with a JS-rendered widget). yfinance returns an empty DataFrame
    and we emit `_EMPTY_NOTE`.

  No fallback HTTP request — if the scrape fails (HTTP error or
  unexpected HTML), yfinance returns an empty DataFrame, which we
  classify as the standard empty-result case and surface the
  `_EMPTY_NOTE`. The note text mentions the scrape-breakage edge case
  but at the response level we can't distinguish "Yahoo broke the
  page" from "ticker has no valuation data." If you see the empty
  result on a blue-chip US equity (e.g. AAPL / MSFT), that's the
  scrape breakage signal — run smoke and check the PEG / market_cap
  canaries first.

- **Precision: ~3 significant figures.** Yahoo's renderer rounds before
  we ever see the values. `AAPL.market_cap` shows as `"4.31T"` and
  is emitted as `4310000000000` (clean — we round-not-truncate when
  coercing the parsed float to int, so you don't see the ugly
  trailing-9s artifact from `float("4.31") * 1e12 = 4309999999999.9995`).
  Treat market cap / EV as ±0.5% accurate (rounding to the nearest
  0.01T / 0.01B), and ratios as ±0.005 (rounding to the nearest 0.01).
  For exact values use the structured-API equivalents:
  - `info.valuation.market_cap` (structured `marketCap`, full integer
    precision)
  - `info.valuation.trailing_pe` (structured `trailingPE`, full float
    precision)
  - For other ratios, `info.valuation` covers them all in the current
    snapshot (just not historical periods).

  When in doubt: use `info` for current-snapshot numbers, use
  `valuation` for historical trend.

- <a id="empty-ambiguous"></a>**All-empty is ambiguous.** An empty
  `periods` list happens for ALL of these causes:
  - ETF (`SPY`)
  - Mutual fund (`VFIAX`)
  - Index (`^GSPC`)
  - Crypto (`BTC-USD`)
  - FX (`EURUSD=X`)
  - Futures (`ES=F`)
  - Bogus / delisted ticker (`BOGUS123XYZ`)
  - (Hypothetically) HTML-scrape breakage on a real equity — see above

  All verified empirically 2026-05 _except_ the scrape-breakage case
  (which is by definition not reproducible until Yahoo restyles).
  Yahoo may log an `HTTP Error 404` to stderr for some — but does NOT
  raise.

  We don't promote empty to `error_kind: not_found` because legitimate
  non-equity quotes aren't errors; we emit success with a `note`. To
  resolve which case you're in, **call `fast_info`** on the same
  symbol — it returns `quote_type` for valid tickers (EQUITY / ETF /
  MUTUALFUND / INDEX / CRYPTOCURRENCY / FUTURE / CURRENCY) and a
  `not_found` classification for bogus.

- **`market_cap` and `enterprise_value` are in trading currency.**
  Same convention as `info.valuation.market_cap` and
  `holders.*.value`. For ADRs (TM, BABA) and non-US primary listings
  (`0700.HK`, `BMW.DE`) the absolute values are in the listing
  currency, not USD. Mixing tickers from different exchanges in one
  summary table without converting will produce apples-to-bananas
  comparisons. Either filter to one currency, convert via
  `fast_info.currency` + an FX rate, or annotate per-ticker.

- **The `current` row updates intraday; quarter-end rows don't.**
  The `Current` column reflects the latest price (and the most-recent
  reported fundamentals), so its values drift through the trading
  session. The five quarter-end rows are anchored to historical
  closes and are stable until the next quarter rolls in. If you
  cache `valuation` output, the `current` row goes stale within minutes
  but the snapshot rows remain valid for ~3 months.

- **Quarter-end dates ≠ fiscal-quarter-end dates.** Yahoo emits
  CALENDAR-quarter-end dates (`3/31`, `6/30`, `9/30`, `12/31`) even
  for companies whose fiscal year doesn't align with the calendar
  (e.g. AAPL's fiscal year ends in September). **Yahoo doesn't
  publicly document the exact semantics of these quarter-end
  snapshots** — the safest reading is "a point-in-time valuation as
  of that calendar date, using whichever fundamentals were latest at
  the time", NOT an assumption that the values are aligned with the
  company's fiscal quarter. For fiscal-period-aligned data use
  `financials --period quarterly`.

- **`peg_ratio` is often missing.** Empirically (2026-05) `peg_ratio`
  is `null` for ~50% of small / mid caps and many non-US listings.
  When present, it's Yahoo's 5-year-expected variant (`PEG Ratio (5yr
  expected)` in Yahoo's raw label) — distinct from a trailing PEG.
  Don't chain on `peg_ratio` for screening; use `forward_pe` +
  `info.fundamentals.earnings_growth` if you need PEG-like logic.

- **Loss-makers' P/E renders as `null`, not negative.** Yahoo emits
  `"--"` in its HTML rather than a negative ratio. PLUG, RIVN, NIO
  verified 2026-05 — every period's `trailing_pe` and `forward_pe`
  are `null`. Same goes for `ev_to_ebitda` when EBITDA is negative.
  `price_to_book` and `price_to_sales` are generally still populated
  (book equity and revenue stay positive for loss-makers).

- **6 periods is empirical, not guaranteed.** Yahoo currently emits 6
  columns (Current + 5 quarter-end snapshots) — verified across
  AAPL / MSFT / 0700.HK / BMW.DE / TM / BABA. Newly-listed or
  recently-IPO'd tickers may have fewer columns. Read
  `periods_returned` (in summary mode) or `len(periods)` (default
  mode) rather than hard-coding 6.

- **Field-name parity with `info.valuation`.** All 9 ratio / size
  fields use identical snake_case names to `info`'s valuation section:
  `market_cap`, `enterprise_value`, `trailing_pe`, `forward_pe`,
  `peg_ratio`, `price_to_book`, `price_to_sales`, `ev_to_revenue`,
  `ev_to_ebitda`. Lets you join the two outputs by field name and
  compare `info.valuation.trailing_pe` (full-precision current) vs
  `valuation[0].periods[0].trailing_pe` (scraped current, 3-sig-fig
  rounded) — the latter should match the former to within rounding.
  Useful as an in-band sanity check that the scrape is current.

- **One HTTP per ticker.** No batching path (no `yf.download`
  equivalent for key-statistics). Multi-ticker is a serial loop — 10
  tickers ≈ 10 HTTP, ~5–15 s total. Same shape as
  `history --metadata` / `history --shares`. Triggers the standard
  rate-limit guidance: at batch sizes >5 keep an eye on `attempts >
  1` in the response.
