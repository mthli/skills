[← back to SKILL.md](../SKILL.md)

# `holders` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`holders.py --summary AAPL QQQ BOGUS123` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting holders results](#presenting-holders-results) · [Mode-specific caveats](#mode-specific-caveats)

Ownership data for one or more tickers — three sections per ticker:

- **`summary`** (rollup from `Ticker.major_holders`) — % of shares
  held by insiders / institutions, % of public float held by
  institutions, total count of institutions on file.
- **`institutional`** (from `Ticker.institutional_holders`) — top ~10
  institutional holders with shares / dollar value / change vs prior
  reporting period.
- **`mutualfund`** (from `Ticker.mutualfund_holders`) — top ~10 mutual
  fund holders with the same per-row schema. (NB: institutional holders
  file 13Fs quarterly; mutual funds report monthly to quarterly. So
  "prior reporting period" cadence varies between the two sections.)

All three properties **appear to share one Yahoo backend HTTP call**.
Observed (not source-confirmed) timing on a fresh `Ticker`: first read
~120 ms cold, next two ~0 ms — consistent with yfinance issuing one
`quoteSummary` request and caching the result on the instance, but the
sharing could equally be HTTP-level (session cookie reuse) rather than
explicit module batching. Either way, fetching all three is no more
expensive than fetching one — so there is no `--scope` flag (no
latency to save). If a future use case wants to skip a section purely
to reduce JSON output size, prefer `--summary` (which already drops
the lists) over reintroducing `--scope`.

**Equity-focused.** ETFs / mutual funds / indexes / crypto / FX /
futures all return three empty DataFrames; bogus / delisted tickers
ALSO return three empty DataFrames. The empty case is genuinely
ambiguous — see [All-empty is ambiguous](#empty-ambiguous) below.

## Run

```bash
# Default: full sections, pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py AAPL

# Peer rollup: flat per-ticker dict (~10× smaller than default)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --summary AAPL MSFT GOOGL

# Cap to top 5 in each list
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --limit 5 AAPL

# CSV — default mode emits one row per holder (with holder_class discriminator)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --format csv AAPL

# CSV — summary mode emits strict one row per ticker (peer-comparison table)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/holders.py --format csv --summary AAPL MSFT 0700.HK
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap institutional / mutualfund rows per ticker.
  Default: keep all (Yahoo returns up to ~10 each). Does not affect
  the `summary` rollup. Yahoo's own cap is ~10 — `--limit 20` won't
  give you 20.
  **Silently ignored in `--summary` mode.** The flat metrics
  (`top5_*_pct`, `*_rows_returned`) are computed from Yahoo's full
  response so they describe the data, not the display knob — and stay
  invariant under `--limit` by design. If a future use case wants
  "top-N concentration" with a custom N, add a separate `--top-n`
  flag rather than overloading `--limit`.
- `--summary` — flat per-ticker projection. Lifts the rollup pcts /
  count to top-level fields, plus the single best institutional and
  mutualfund holder, plus the sum of the top-5 in each list as a
  concentration signal. Same network cost as default mode (post-fetch
  projection); use it to save context tokens.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array, one record per ticker. `ndjson` emits one JSON
  record per ticker per line. `csv` shape depends on mode:
  - **default mode** — one row per holder, with a `holder_class`
    discriminator column whose values are `summary` (the rollup row,
    one per ticker) / `institutional` / `mutualfund`. The four
    summary-only columns are populated only on the `summary` row;
    the per-holder columns only on `institutional` / `mutualfund`
    rows. Empty / errored tickers emit a single row carrying
    `symbol` + `note` + meta fields.
  - **`--summary` mode** — strict one row per ticker (every column
    populated where the ticker has data). Same row shape across all
    tickers, friendly for spreadsheet pivots.

CSV column order (default mode, left to right): `symbol`,
`holder_class`, the 4 summary-rollup columns, the 6 per-holder
columns, `note`, then the 3 meta fields (`error`, `error_kind`,
`attempts`).

## Output schema

### Default mode

Per ticker (illustrative):

```json
[
  {
    "symbol": "AAPL",
    "summary": {
      "insiders_pct": 0.0164,
      "institutions_pct": 0.6535,
      "institutions_float_pct": 0.66439,
      "institutions_count": 7558
    },
    "institutional": [
      {
        "date_reported": "2025-12-31",
        "holder": "Vanguard Group Inc",
        "pct_held": 0.0971,
        "shares": 1426283914,
        "value": 409971051722,
        "pct_change": 0.0192
      }
    ],
    "mutualfund": [
      {
        "date_reported": "2025-12-31",
        "holder": "VANGUARD INDEX FUNDS-Vanguard Total Stock Market Index Fund",
        "pct_held": 0.0316,
        "shares": 464375258,
        "value": 133480025293,
        "pct_change": -0.0059
      }
    ]
  }
]
```

#### Summary section (4 fields, all from `major_holders`)

| Field | Type | Notes |
|---|---|---|
| `insiders_pct` | float / null | **FRACTION** (`0.0164` = 1.64% insider ownership). Multiply ×100 for display. |
| `institutions_pct` | float / null | **FRACTION** of total shares outstanding held by institutions (`0.6535` = 65.35%). |
| `institutions_float_pct` | float / null | **FRACTION** of public float held by institutions. Always ≥ `institutions_pct` because float ≤ total shares. |
| `institutions_count` | int / null | Number of distinct institutions on file with the SEC for the ticker. Verified 2026-05: thousands to tens of thousands for US large-caps (AAPL = 7558, MSFT = 8024); hundreds to ~1k for non-US (0700.HK = 996, BMW.DE = 578). |

#### Institutional / mutualfund row (6 fields each, identical shape)

| Field | Type | Notes |
|---|---|---|
| `date_reported` | str / null | YYYY-MM-DD — the **regulatory filing date**, NOT real-time. For US 13F filings this lags real holdings by ~45 days (calendar-quarter-end + filing window). May differ within one ticker's list (some holders file by calendar quarter, some by fiscal). |
| `holder` | str / null | Holder name as Yahoo emits it. Often verbose for funds (`"VANGUARD INDEX FUNDS-Vanguard Total Stock Market Index Fund"`); leave as-is unless presenting. |
| `pct_held` | float / null | **FRACTION** of total shares held by this holder (`0.0971` = 9.71%). May display as `0.0` for tiny holders — that's Yahoo's rounding to 4 decimal places, not a true zero. |
| `shares` | int / null | Number of shares held. Always populated when row is present. |
| `value` | int / null | Dollar value = `shares × current_price`, in the ticker's **TRADING currency** (USD for AAPL, HKD for `0700.HK`, EUR for `BMW.DE`, etc.). NOT a snapshot at the filing date — it's recomputed at fetch time, so it drifts when the stock moves. To know the currency code, call `fast_info` on the same ticker. |
| `pct_change` | float / null | **FRACTION** change in shares vs the prior reporting period (`0.0192` = 1.92% increase, `-0.5237` = 52.37% reduction). `1.0000` (100%) **typically signals** a brand-new position (prior shares were 0 → Yahoo emits 1.0 instead of `Inf`); a real 100%-doubled position would also report `1.0`, so treat `≥ 0.99` as "very likely newly initiated" rather than mathematically certain. `0.0` means held the same. |

### `--summary` mode

Flat per-ticker dict (rollup + top picks + top-5 concentration):

```json
[
  {
    "symbol": "AAPL",
    "insiders_pct": 0.0164,
    "institutions_pct": 0.6535,
    "institutions_float_pct": 0.66439,
    "institutions_count": 7558,
    "top_institution": "Vanguard Group Inc",
    "top_institution_pct": 0.0971,
    "top5_institutions_pct": 0.2621,
    "institutional_rows_returned": 10,
    "top_mutualfund": "VANGUARD INDEX FUNDS-Vanguard Total Stock Market Index Fund",
    "top_mutualfund_pct": 0.0316,
    "top5_mutualfunds_pct": 0.0942,
    "mutualfund_rows_returned": 10
  }
]
```

`top5_*_pct` is the sum of `pct_held` across the top 5 rows in each
list — a concentration signal. When fewer than 5 rows exist, it sums
whatever's there (0700.HK only has 2 institutional rows, so its
`top5_institutions_pct` equals the sum of those 2). When zero rows,
it's `null`.

`institutional_rows_returned` / `mutualfund_rows_returned` are the number of rows
**actually returned by Yahoo** (capped at ~10) — distinct from
`institutions_count` (the rollup figure: thousands to tens of thousands
for US large-caps, hundreds to ~1k for non-US — see the schema table
above for verified examples).

### Empty / non-applicable result

When all three sections come back empty, the response is success with
a `note` rather than an error. See
[All-empty is ambiguous](#empty-ambiguous) below.

```json
{
  "symbol": "QQQ",
  "summary": { "insiders_pct": null, "institutions_pct": null, "institutions_float_pct": null, "institutions_count": null },
  "institutional": [],
  "mutualfund": [],
  "note": "no holder data (Yahoo's holders endpoint covers operating-company equities; ETFs / mutual funds / indexes / crypto / FX / futures return empty, as do bogus tickers and very low-coverage equities — call fast_info to disambiguate)"
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

## Presenting holders results

**Multiply fractions ×100 when displaying.** All `*_pct` fields are
fractions. Render as `9.71%`, not `0.0971`. Same convention as `info`'s
margin / growth fields — easy mistake to make when sliding from one
mode to another.

**Single-ticker rollup.** Lead with the rollup percentages, then the
top 3–5 institutional holders, then the top 3–5 mutual funds. Format
template (placeholders — illustrative shape):

> **\<TICKER\> — ownership**
> - Insiders: **\<X.XX\>%**, Institutions: **\<XX.XX\>%** of shares
>   (\<XX.XX\>% of float). \<N\> institutions on file.
> - **Top institutional holders** (as of \<YYYY-MM-DD\>):
>   - \<holder name\> — \<X.XX\>% (\<shares\> sh, \<currency\> \<value\>)
>   - ...
> - **Top mutual fund holders** (as of \<YYYY-MM-DD\>):
>   - ...

Always state the currency on `value`. For US tickers `\$` is fine;
for `0700.HK` write `HK\$`, for `BMW.DE` write `€`. If you don't know
the trading currency, omit `value` rather than guessing — call
`fast_info` first if needed.

**Multi-ticker peer compare.** Use `--summary`; render as a table with
columns: ticker | inst % | top inst | top inst % | top5 inst %.
The `top5_*_pct` column is the most useful concentration signal —
high values (>40%) flag concentrated ownership, low values (<20%)
flag dispersed ownership.

**Date formatting.** `date_reported` is already YYYY-MM-DD. Most rows
share one date (e.g. `2025-12-31` for the calendar-quarter filings
reported in early Q1); some institutions file off-cycle, so dates can
mix within one ticker's list. Don't pretend they're a single snapshot —
note the date range or pick the most-common date if the user asks
"as of when".

**Escape `$` as `\$` in prose** — same rationale as the other modes.

## Mode-specific caveats

- <a id="empty-ambiguous"></a>**All-empty is ambiguous (full discussion).**
  yfinance returns three empty DataFrames in all of these cases.
  Verified empirically (2026-05):
  - ETF (`QQQ`)
  - Mutual fund (`VFIAX`)
  - Index (`^GSPC`)
  - Crypto (`BTC-USD`)
  - FX (`EURUSD=X`, `JPY=X`)
  - Futures (`ES=F`, `GC=F`)
  - Bogus / delisted ticker (`BOGUS123XYZ`)
  
  Not yet probed but behavior expected to match (real but very low-
  coverage equity, e.g. micro-cap with no 13F filers — rare and
  hard to construct a stable test ticker for, so left unverified).
  
  Yahoo logs an `HTTP Error 404` line on stderr for some of these
  (verified for ETFs and bogus tickers in 2026-05) but does not raise.
  We can't distinguish them at the holders endpoint — there's no
  signal in the response. We deliberately don't promote empty to
  `error_kind: not_found` (low-coverage equities and ETFs aren't
  errors) and instead emit success with a `note` string carrying the
  disambiguation hint. To resolve which case you're in, **call
  `fast_info`** on the same symbol — it returns `quote_type` for valid
  tickers (EQUITY / ETF / MUTUALFUND / INDEX / CRYPTOCURRENCY /
  FUTURE / CURRENCY) and a not_found classification for bogus.
- **`pct_held` and `pct_change` are FRACTIONS, not percentages.**
  Re-stating because this is the most likely mistake. `pct_held: 0.0971`
  means **9.71% of shares**, not 0.0971%. Multiply ×100 for display.
  The same applies to `insiders_pct`, `institutions_pct`,
  `institutions_float_pct`. Consistent with `info`'s margin / growth
  conventions.
- **`value` is in trading currency.** For ADRs (TM, BABA) and
  non-US-listed tickers (`0700.HK`, `BMW.DE`) the dollar value is in
  the listing currency, not USD. Mixing tickers from different
  exchanges in one summary table without converting will produce
  apples-to-bananas comparisons. Either filter to one currency,
  convert via fast_info's `currency` + an FX rate, or annotate the
  table with the per-ticker currency.
- **`value` is recomputed at fetch time, not a filing-date snapshot.**
  Yahoo serves `shares × current_price`, so a holder's reported value
  drifts daily even when their actual share count hasn't changed since
  the filing. Don't read `value` as "what the holder reported owning";
  read it as "what their position is worth right now (at last close)".
- **`pct_change` of `1.0000` (100%) means a brand-new position.**
  Verified across BMW.DE / 0700.HK in 2026-05 — when prior-period
  shares are 0, Yahoo emits `pctChange = 1.0`, not `Inf`. Treat
  `pct_change ≥ 0.99` as "newly initiated" rather than literal "100%
  growth". `pct_change` of exactly 0.0 means the holder did not change
  their position vs the prior filing.
- **`pct_held: 0.0` is a rounding artifact for tiny holders, not always
  a true zero.** Yahoo rounds to 4 decimal places; a 0.003% holder
  appears as `pct_held: 0.0` while still carrying a real `shares` and
  `value`. If you need precision below 1bp, compute it yourself in one
  of two ways:
  - **From total shares outstanding (preferred):** pull
    `shares_outstanding` via `info` (under `info.shares.shares_outstanding`)
    or `fast_info` (`fast_info.shares`), then `pct = holder.shares /
    shares_outstanding`.
  - **By back-solving from a non-tiny holder in the same response:**
    if any other holder has a non-rounded `pct_held` (e.g. Vanguard at
    0.0971), then `total ≈ holder.shares / holder.pct_held`, and the
    same `total` applies to every row in the response. Cheaper (no
    extra Yahoo call) but less precise — the divisor itself is
    4-decimal-rounded.

  Realistically users asking holder questions don't need sub-bp
  precision; this is documented for completeness.
- **Non-US tickers often have very few institutional rows.** Verified
  in 2026-05: `0700.HK` returns 2 institutional + 10 mutualfund rows;
  `BMW.DE` same shape. The mutual fund list is typically global index
  trackers (Vanguard / iShares / Goldman) holding the name as part of
  a broad international fund, not specialist active funds. Don't read
  "only 2 institutional holders" as "thin ownership" — it means
  "Yahoo's coverage of 13F-equivalent foreign filings is thin", not
  the underlying ownership reality.
- **`institutions_count` (rollup) ≠ `institutional_rows_returned`
  (rows).** Two adjacent fields, two different things:
  - `institutions_count` (in the `summary` section) is the **total
    number of institutions on file with the SEC** for the ticker
    (7,558 for AAPL, 996 for `0700.HK`).
  - `institutional_rows_returned` (in `--summary` mode's flat dict)
    is **how many holders Yahoo returned in the top-N list** for this
    fetch — typically ≤10.

  The fully-spelled `_rows_returned` suffix is intentional: an earlier
  draft used `institutional_count`, which collided with
  `institutions_count` in conversation and made it easy to misread
  prose like "AAPL has 10 institutional_count" as a 10-institutions-
  on-file claim. The verbose name pays for itself the first time
  someone debugs a wrong-magnitude bug from this collision.
- **`Date Reported` is regulatory, not real-time.** For US tickers
  this is calendar-quarter-end + a filing-window lag (up to ~45 days
  for 13Fs). So holder data fetched in mid-Q1 typically reflects
  prior-Q4 positions. The list does NOT include trading activity
  inside the lag window — for that, see the `insiders` mode (Form 4
  events from `Ticker.insider_transactions`, last ~24 months), which
  is the right pairing with this mode for "how is ownership
  shifting" questions.
- **Three properties appear to share one HTTP call (observed, not
  source-confirmed).** Verified 2026-05 by timing only: NVDA cold
  fetch was 119 ms / 0 ms / 0 ms across `major_holders` /
  `institutional_holders` / `mutualfund_holders`. The 0 ms reads on
  properties 2 and 3 are consistent with yfinance fetching all three
  modules in a single `quoteSummary` request and caching the response
  on the `Ticker` instance — but a session-cookie-level HTTP cache
  would produce the same timing without explicit module batching, and
  we haven't read the yfinance source to confirm which it is. Either
  way, the implication for users is the same: there's no latency
  saving from a hypothetical `--scope` flag, so the script doesn't
  expose one. If yfinance ever splits these into independent requests,
  the current `_fetch_three()` implementation still works correctly —
  retry would just become coarser-grained than ideal.
- **Hard cap of ~10 rows per list.** Yahoo limits institutional and
  mutualfund lists to roughly the top 10 holders. `--limit 20` won't
  give you 20 rows; it'll give you whatever Yahoo returned (up to ~10).
  For deeper holder lists you'd need a paid data source.
