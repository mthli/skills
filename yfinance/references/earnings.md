[← back to SKILL.md](../SKILL.md)

# `earnings` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-full-list) · [Output — `--summary` mode](#output----summary-mode) · [`--estimates`](#--estimates-analyst-consensus) · [When to use `--summary`](#when-to-use---summary) · [Presenting earnings results](#presenting-earnings-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **Surprise(%) units**, **scrape fragility**, **lxml dependency**)

Per-ticker earnings dates: upcoming quarters (with EPS estimates) and recent
quarters (with reported EPS + surprise %). Equity-only — ETFs / indexes /
crypto / FX / futures don't have earnings, so the script short-circuits them
with an empty list and a `note`.

## Run

Earnings.py needs the extra `lxml` package (pandas's `read_html` requires it
and yfinance doesn't pin it). Add `--with 'lxml'` to every invocation:

```bash
# Default JSON output, ~12 rows per ticker (~4 future + ~8 past)
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py AAPL

# Summary — flat per-ticker dict (peer comparison)
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary AAPL MSFT NVDA

# Only upcoming events
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --future-only AAPL

# Only past events, up to 20 quarters
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --past-only --limit 20 AAPL

# + analyst consensus EPS / revenue (per period: 0q / +1q / 0y / +1y)
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --estimates AAPL

# Peer comparison incl. forward consensus
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary --estimates AAPL MSFT NVDA

# CSV — one row per (symbol, earnings_date) with metadata prepended
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --format csv AAPL MSFT
```

Tickers are positional args.

## CLI arguments

- `--limit N` — clamped to `[1, 100]` (yfinance's hard upstream cap).
  Default 12 mirrors yfinance's own default. Semantics differ by mode:
  - **Default mode**: max rows in output. **Strictly enforced** at the
    script layer — yfinance internally maps `limit` to a page-size bucket
    (25 / 50 / 100) and does NOT truncate the returned DataFrame, so we
    slice post-fetch to honor the requested count.
  - **`--summary` mode**: only matters when crossing yfinance's 25 / 50 /
    100 page-size bucket boundaries — `--limit 1` through `--limit 25`
    all trigger the same size=25 fetch (~25 rows). Output is one row per
    ticker regardless. The output slice is **bypassed** in summary mode
    so a small `--limit` doesn't starve the aggregates — `--limit 3
    --summary` still computes the 4-quarter beat rate from the ~25-row
    fetch. To deliberately *narrow* the aggregate window (rare), drop
    `--summary` and post-process.
- `--summary` — flag. Project the full list into a flat per-ticker dict
  (`next_*`, `last_*`, `avg_surprise_last_4`, `beat_rate_last_4`). Use for
  peer comparison or single-line answers like "when does X report next".
- `--estimates` — flag. Attach the full Yahoo analyst panel: consensus
  EPS + revenue, 90-day estimate trend, 7d/30d revision counts, sector /
  index growth comparison, long-term growth (LTG). Five Yahoo property
  reads on a shared Ticker, ~+1.5–3s per equity. Per-period rows for
  `0q` / `+1q` / `0y` / `+1y`; LTG surfaces at top level (`long_term_growth`).
  Independent of `--past-only` / `--future-only` — analyst data is
  forward-looking, not part of the earnings_dates timeline. Equity-only:
  non-equity tickers get `estimates: []` via the same short-circuit as
  `earnings_dates`. In `--summary` mode, projects `0q` to flat
  `consensus_*` fields (CSV-compatible); default mode emits the full
  panel. Default-mode `--format csv` is rejected (the per-row layout
  can't fit a 4-period analyst panel — use `--summary` for CSV). See
  [`--estimates` section](#--estimates-analyst-consensus) below for the
  full schema.
- `--past-only` / `--future-only` — mutually exclusive filters. Default is
  both. Filters apply BEFORE `--limit`, so `--past-only --limit 8` returns
  the 8 most recent reported quarters. Incompatible with `--summary` (the
  summary uses both directions to compute next_ vs last_).
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`:
  default mode = one row per `(symbol, earnings_date)` with `symbol`,
  `quote_type`, `timezone`, `note`, `coverage_note` prepended;
  `--summary` mode = one row per ticker, with `note` and `coverage_note`
  between `quote_type` and the summary fields. The `note` column is
  blank for equity rows and populated for non-equity short-circuits;
  the `coverage_note` column flags IPO fall-through rows in `--summary`
  mode and is always blank in default-mode CLI use (see "CSV summary
  gotcha" below for why). **Backward compat:** `coverage_note` was
  added in a recent revision; default-mode CSV column count is +1
  vs older skill snapshots, with the new column inserted at index 4
  (between `note` and `date`). Parse by column NAME, not by index, to
  stay forward-compatible.

## Output — default mode (full list)

Sample numbers + dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "timezone": "America/New_York",
    "earnings_dates": [
      {
        "date": "2026-07-30T16:00:00-04:00",
        "is_future": true,
        "eps_estimate": 1.89,
        "eps_actual": null,
        "surprise_pct": null
      },
      {
        "date": "2026-04-30T16:00:00-04:00",
        "is_future": false,
        "eps_estimate": 1.94,
        "eps_actual": 2.01,
        "surprise_pct": 3.46
      }
    ]
  }
]
```

Key things to notice:

- **`timezone` is a string for normal results.** It can be `null`
  in exactly one case: the `--estimates` IPO fall-through (empty
  `earnings_dates`, populated `estimates`) — there are no rows to
  derive a tz from. See the [Empty `earnings_dates` is non-fatal under
  `--estimates`](#empty-earnings_dates-is-non-fatal-under---estimates)
  subsection. In every other success path (default mode, populated
  earnings_dates, non-equity short-circuit) `timezone` is a tz string.
- **`date` is a tz-aware ISO string** with offset (e.g. `-04:00` for EDT,
  `-05:00` for EST — DST shifts visible across the year). The time component
  preserves AMC vs BMO timing — `T16:00:00-04:00` is 4 PM ET, after market
  close (AMC); `T07:30:00-04:00` would be before open (BMO). Don't strip
  the time when surfacing to users — it answers "are earnings before or
  after market" without a separate field.
- **`is_future` is purely date-based**: `date > now_utc`. It does **not**
  derive from null `eps_actual` — Yahoo signals unreported quarters by
  leaving Reported EPS blank (parsed as `NaN` → `null`), but that's a
  *consequence* of being future, not the definition. Past events with
  stale/missing actual EPS (rare: stopped reporters, delisted-mid-quarter
  rows in Yahoo's calendar) keep `is_future: false`.
- **Future rows typically have null `eps_actual` AND null `surprise_pct`** —
  they're not yet reported. (Edge case: since `is_future` is purely
  date-based, a row whose date just transitioned past while Yahoo updated
  `actual` could in theory carry non-null actual with `is_future=true`
  for a brief window. Hasn't been observed in practice.) Render `—` or
  `n/a` in tables for the typical case.
- **Rows arrive in "near-now first" order** — future events sorted
  ascending (**nearest upcoming at the top**, most-distant future at the
  bottom of the future block), then past events sorted descending (**most
  recent reported below the future block**, oldest at the very bottom).
  Both halves converge on the now-boundary in the middle. This means
  small `--limit` values keep the most useful rows: `--limit 3` for a
  typical equity returns the next earnings event plus the 2 most recent
  reported quarters, **not** 3 distant future estimates (which would
  happen under a flat DESC sort). yfinance's own return order varies;
  the script re-sorts inside `fetch()` to guarantee this layout. Both
  default mode and `--summary` see consistently sorted rows.

For non-equity quote types (ETF / INDEX / CRYPTOCURRENCY / FUTURE / CURRENCY)
the response short-circuits without scraping:

```json
[
  {
    "symbol": "SPY",
    "quote_type": "ETF",
    "note": "earnings only meaningful for equities; this is ETF",
    "earnings_dates": []
  }
]
```

This is **not an error** — it's a deliberate empty-result with a `note`
field. Successful path; `error` / `error_kind` absent. Save the user from
having to filter equity vs non-equity tickers before calling.

**Retry surfacing.** First-shot success has no `attempts` field. If any of
the underlying yfinance calls (quote_type pre-check, earnings scrape, or —
under `--estimates` — any of the five analyst-panel reads) retried before
succeeding, the response gains `"attempts": N` at the top level. N is
the **max retries seen in any single underlying call**, matching the
convention used by `fast_info` / `history` / `info`. Surfaced only when
> 1.

A failed ticker looks like:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "fetch failed (not_found, after 1 attempt(s))",
  "error_kind": "not_found",
  "attempts": 1
}
```

`error_kind` ∈ `{rate_limit, not_found, network, unknown}`. Bogus / delisted
tickers get `not_found` from the quote_type pre-check (the underlying
yfinance error path is buggy — see "Mode-specific caveats" below for why
the script catches `AttributeError` explicitly).

## Output — `--summary` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "next_date": "2026-07-30T16:00:00-04:00",
    "next_eps_estimate": 1.89,
    "last_date": "2026-04-30T16:00:00-04:00",
    "last_eps_estimate": 1.94,
    "last_eps_actual": 2.01,
    "last_surprise_pct": 3.46,
    "avg_surprise_last_4": 6.11,
    "beat_rate_last_4": 1.0
  }
]
```

Field semantics:
- `next_*` — chronologically nearest future event (the `is_future=true` row
  with the smallest date). Null if no future events scheduled (rare for
  liquid names — they always have one ahead).
- `last_*` — chronologically most recent past event. Null only for IPOs
  with no reporting history yet.
- `avg_surprise_last_4` — simple arithmetic mean of `surprise_pct` across
  the 4 most recent reported quarters. Null when fewer than 4 reported
  quarters have non-null surprise (recent IPOs, low-coverage names).
- `beat_rate_last_4` — fraction of those 4 with `surprise_pct > 0`
  (e.g. `0.75` = 3 of 4 quarters beat). Same null condition as
  `avg_surprise_last_4`. Surprise of exactly 0 (rare) counts as **not** a
  beat — only strictly positive.

For non-equity tickers `--summary` preserves the `note` and nulls all
numeric fields:

```json
{
  "symbol": "SPY",
  "quote_type": "ETF",
  "note": "earnings only meaningful for equities; this is ETF",
  "next_date": null,
  "next_eps_estimate": null,
  ...
}
```

## `--estimates` (analyst panel)

Attaches the full Yahoo analyst panel — five upstream DataFrames merged
per period:

| Source | Contributes |
|---|---|
| `earnings_estimate` | EPS consensus: avg / low / high / # analysts / YoY growth |
| `revenue_estimate` | Revenue consensus: avg / low / high / # analysts / YoY growth |
| `eps_trend` | EPS consensus 7 / 30 / 60 / 90 days ago — measures sentiment drift |
| `eps_revisions` | # of upward / downward revisions in last 7d / 30d — momentum |
| `growth_estimates` | sector / index benchmark growth + LTG (long-term row) |

Equity-only; non-equity tickers get `estimates: []` (same short-circuit
as `earnings_dates`). Independent of `--past-only` / `--future-only`
since the panel is forward-looking.

Sample (illustrative numbers; AAPL):

```json
{
  "symbol": "AAPL",
  "quote_type": "EQUITY",
  "timezone": "America/New_York",
  "earnings_dates": [...],
  "long_term_growth": {"stock": null, "index": 0.122},
  "estimates": [
    {
      "period": "0q",
      "eps_currency": "USD",
      "eps_avg": 1.89, "eps_low": 1.83, "eps_high": 1.99,
      "eps_year_ago": 1.57, "eps_growth": 0.2043, "eps_analysts": 30,
      "revenue_currency": "USD",
      "revenue_avg": 108790845560.0,
      "revenue_low": 107501000000.0,
      "revenue_high": 112000000000.0,
      "revenue_year_ago": 94036000000.0,
      "revenue_growth": 0.1569,
      "revenue_analysts": 26,
      "eps_trend_current": 1.89, "eps_trend_7d_ago": 1.74,
      "eps_trend_30d_ago": 1.74, "eps_trend_60d_ago": 1.72,
      "eps_trend_90d_ago": 1.73,
      "eps_revisions_up_7d": 22, "eps_revisions_up_30d": 22,
      "eps_revisions_down_7d": 0, "eps_revisions_down_30d": 0,
      "index_growth": 0.2461
    },
    { "period": "+1q", ... },
    { "period": "0y",  ... },
    { "period": "+1y", ... }
  ]
}
```

### Period codes

Yahoo's raw codes — passed through unchanged so cross-referencing the
upstream Yahoo Stats panel is direct:

| Period | Meaning |
|---|---|
| `0q` | Current quarter — the one that's about to be reported. **Usually** matches the upcoming `earnings_dates` future row. |
| `+1q` | One quarter ahead of `0q` |
| `0y` | Current fiscal year |
| `+1y` | Next fiscal year |

**Forward-compat:** if Yahoo ever extends the panel (e.g., adds `+2q`),
extra periods are passed through with their raw period strings — the
script doesn't filter to the canonical four. Iterate `period` rather
than positional index.

**0q vs upcoming `earnings_dates` row — usually but not always identical.**
Both come from Yahoo's analyst consensus for the same upcoming quarter,
but the calendar (`earnings_dates`) and the Stats panel
(`earnings_estimate`) update on different cadences. Around earnings
releases there's a transition window where the calendar's "next future
row" has rolled forward to the next quarter while the Stats panel's
`0q` still points at the just-reported quarter (or vice-versa). The
exact window length isn't documented and we haven't measured it
empirically. For tickers that just reported, double-check
`0q.eps_year_ago` against the calendar's most-recent past row's date
if alignment matters.

### Field semantics

- **`eps_avg` / `revenue_avg`** — mean of analyst estimates. The headline
  consensus.
- **`eps_low` / `eps_high` / `revenue_low` / `revenue_high`** — bottom and
  top of the analyst range. Spread = high − low ≈ disagreement signal.
- **`eps_year_ago` / `revenue_year_ago`** — the actual reported value for
  the same period one year prior. Pairs with `*_growth` so you can
  recompute or sanity-check.
- **`eps_growth` / `revenue_growth`** — YoY growth as a **fraction**
  (`0.2043` = 20.43%). Matches the `info` and `financials --summary`
  conventions and is the OPPOSITE of `earnings_dates.surprise_pct`,
  which is percent-encoded. Multiply by 100 for display.
- **`eps_analysts` / `revenue_analysts`** — # of analysts contributing.
  Often differ (EPS coverage usually broader than revenue), so each side
  carries its own count.
- **`eps_currency` / `revenue_currency`** — these are **separate fields,
  not duplicates**. Yahoo's analyst panel mixes per-share and aggregate
  currencies for ADRs:

  | Ticker | `eps_currency` | `revenue_currency` | Why |
  |---|---|---|---|
  | AAPL, MSFT (US-listed US co.) | USD | USD | trading and reporting agree |
  | TM (Toyota ADR) | **USD** | **JPY** | per-share EPS in trading ccy; revenue in home ccy |
  | PBR (Petrobras ADR) | **USD** | **BRL** | same pattern as TM |
  | BABA, 0700.HK | CNY | CNY | both already in reporting ccy |

  When formatting an ADR table, label the EPS column and revenue column
  with their respective currencies; don't assume one currency for the
  whole row.
- **`eps_trend_current` / `eps_trend_7d_ago` / `eps_trend_30d_ago` /
  `eps_trend_60d_ago` / `eps_trend_90d_ago`** — what the consensus EPS
  was at each lookback point. Same denomination as `eps_avg`
  (`eps_currency`). `(current - 90d_ago) / 90d_ago` → revision velocity.
- **`eps_revisions_up_7d` / `eps_revisions_up_30d` /
  `eps_revisions_down_7d` / `eps_revisions_down_30d`** — analyst
  count of upward / downward EPS revisions in the rolling window. Pure
  integers (no currency). Net = up − down ≈ analyst momentum.
- **`index_growth`** — same-period **single global benchmark** from
  `growth_estimates.indexTrend`. Empirically identical across every
  ticker we tested — not just within US large-caps but across global
  markets:

  | Verification | Sample | All returned same `indexTrend`? |
  |---|---|---|
  | Across US sectors | AAPL, MSFT, NVDA, KO, PG, XOM, CVX, JPM, JNJ, WMT | yes (+24.6% for `0q`) |
  | Across international markets | `0700.HK`, `BMW.DE`, `7203.T`, `TM`, `BARC.L`, `005930.KS` | yes (same +24.6%) |

  So `index_growth` is **not** locale-aware (no Hang Seng / DAX / Nikkei
  variants), **not** sector-aware, and the same float for every ticker
  in a given snapshot — it's a Yahoo-internal global aggregate of some
  kind. The exact methodology isn't documented, but the magnitudes we
  observed (+24.6% for `0q`, +21.6% for `0y`, ~12.2% for the LTG row)
  suggest the upper rows are some current-period aggregate calculation
  while the LTG row matches the rough scale of long-run market-growth
  assumptions (~8–12% annualized). The **important point for callers**
  is that `index_growth` is a *point-in-time per-period* number, not a
  long-term constant; don't confuse the per-quarter / per-year values
  with the long-term S&P growth assumption — for that, use
  `long_term_growth.index` instead. Pair `index_growth` with
  `eps_growth` for "this stock vs the broad market this period" in one
  number each. If you need a sector- or locale-specific benchmark,
  `--estimates` doesn't provide one — fetch a sector ETF separately and
  compute. (`growth_estimates.stockTrend` is identical to
  `earnings_estimate.growth` in every spot check, so we drop it as a
  duplicate; if you ever observe a ticker where they diverge, the
  drop assumption needs revisiting.)

### Top-level: `long_term_growth`

```json
"long_term_growth": {"stock": null, "index": 0.122}
```

From the `LTG` row of `growth_estimates`. Structurally distinct from the
quarterly / annual periods, so surfaced at top level. The `index` value
is the same across all tickers (~0.12 in our snapshot) — the same
global benchmark as `index_growth`, just projected long-term.

**`stock` is empirically null across our entire 17-ticker sample.**
Verified across:

| Group | Tickers | All `stock = NaN`? |
|---|---|---|
| US, mega-cap | AAPL, NVDA | yes |
| US, large-cap | CRM, PLTR | yes |
| US, mid-cap | SNOW, HOOD, AFRM, RIVN | yes |
| US, small-cap | BB | yes |
| Non-US listings and major ADRs | `0700.HK` (HK), `BMW.DE` (Frankfurt), `7203.T` (Tokyo), `005930.KS` (KOSPI), `BARC.L` (London), `TSM` (Taiwan ADR), `BABA` (China ADR), `TM` (Japan ADR) | yes |

Yahoo doesn't appear to publish per-ticker analyst LTG broadly — at
least not on the names we've sampled across cap sizes and global
markets. Treat `stock` as a forward-compat slot you shouldn't
hard-depend on but should still surface if Yahoo populates it for a
specific ticker. In practice, `long_term_growth` is effectively just
"Yahoo's global LTG benchmark via the `index` field." Field is
**omitted** entirely when both `stock` and `index` are NaN (rare).

### Units / denomination

- **`revenue_*` numbers are raw currency units, not millions or billions.**
  AAPL's 0q `revenue_avg: 108790845560.0` is \$108,790,845,560 ≈ \$108.8B.
  Format with `f"{v/1e9:.1f}B"` or similar at display time. Same
  convention as `info.market_cap` and `financials` line items.
- **`eps_*` numbers are per-share, in `eps_currency`.** For TM the EPS
  numbers are in USD (per ADR share), not JPY.

### Staleness

`eps_avg` is Yahoo's "current" snapshot of the consensus — it can be
days old depending on Yahoo's update cadence. `eps_trend_current` /
`eps_trend_7d_ago` / `eps_trend_30d_ago` etc. let you see how stale: if
`eps_trend_current == eps_trend_30d_ago`, no analyst has updated their
estimate in 30 days — coverage may be thin or stale.

### Empty `earnings_dates` is non-fatal under `--estimates`

The default-mode contract says "empty calendar → `error_kind: not_found`":
the user asked for the calendar and we got nothing usable. With
`--estimates` the contract is different — a recent IPO can have
analyst consensus coverage before any past reports exist on Yahoo's
calendar. So when `--estimates` is set:

- empty `earnings_dates` + populated `estimates` → **success** with
  `earnings_dates: []`, `timezone: null`, and a self-documenting
  `coverage_note: "empty calendar (recent IPO or low coverage); analyst
  panel returned"`. The analyst panel carries the data. No `error` field.
- empty `earnings_dates` + empty `estimates` → still
  `error_kind: not_found` (genuinely no data anywhere). The error
  message picks up the est-side `error_kind` so you can distinguish
  "Yahoo silent on both" from "rate-limited on both".

**IPO uses a separate field, not `note`.** `note` is reserved for the
non-equity short-circuit (ETF / INDEX / etc.) where `estimates` is
always `[]`. IPO equities populate `coverage_note` instead. Schema-wise
they're disjoint — code that filters `if d.get("note"):` only sees
non-equity rows; code that wants IPO-style fall-through opts in via
`coverage_note`. The `note` short-circuit is unambiguous; you don't
need to inspect `quote_type` to disambiguate.

In `--summary` mode the same logic applies — `next_*` / `last_*` are
all null when `earnings_dates` is empty, but `consensus_*` is projected
from the 0q estimate row as usual. The `coverage_note` passes through
to summary unchanged.

**CSV summary gotcha:** when an IPO row appears in `--summary --estimates
--format csv`, the `next_date` / `last_date` / surprise columns are all
empty while `consensus_*` columns are populated. This is intentional, not
a parse error — readers should treat empty cells as null, not as zeros
or "missing required field". The `coverage_note` column flags the IPO
case explicitly (and the regular `note` column stays empty for equities).

**Default-mode CSV note (no CLI path).** The default-mode CSV layout
also carries `note` and `coverage_note` columns (added for schema
consistency with `--summary` mode), but **no CLI invocation reaches an
IPO row in default-mode CSV**: argparse rejects `--estimates --format
csv` without `--summary` (the default layout has nowhere to put the
analyst panel), and an IPO without `--estimates` becomes
`error_kind: not_found` (an error row, not a coverage_note row). So
in CLI use the `coverage_note` column is always blank in default-mode
CSV; the column exists for programmatic callers that drive `_emit`
directly with a `coverage_note`-bearing dict (and as future-proofing
if the argparse rule ever relaxes).

### Soft-failure handling

If **both** consensus sources (`earnings_estimate` AND `revenue_estimate`)
fail, the response keeps `earnings_dates` and adds:

```json
{
  ...,
  "earnings_dates": [...],
  "estimates": [],
  "estimates_error": "rate_limit"
}
```

`estimates_error` ∈ `{rate_limit, not_found, network, unknown}`. The
top-level call is still considered successful — `error` / `error_kind`
are absent. Treat `estimates_error` as a hint to retry the estimates
selectively rather than re-fetching the whole earnings payload.

**Partial failures are silent.** If only one consensus side fails, the
other's columns null out per row and `estimates_error` is **not** set.
Same for trend / revisions / growth — those are enrichment, so a 429 on
just `eps_revisions` nulls out only the `eps_revisions_*` columns and
the response stays `estimates_error`-free.

### `--summary` mode projection

In `--summary --estimates` mode, the full estimates list is **dropped**
in favor of flat `consensus_*` fields projected from the `0q` row only.
This keeps summary's "small flat dict for peer comparison" promise:

| Default mode | Summary mode |
|---|---|
| `estimates: [{0q, +1q, 0y, +1y}, ...]` | `consensus_eps_avg`, `consensus_eps_low`, `consensus_eps_high`, `consensus_eps_growth_yoy`, `consensus_eps_analysts`, `consensus_eps_currency`, `consensus_revenue_avg`, `consensus_revenue_growth_yoy`, `consensus_revenue_analysts`, `consensus_revenue_currency` |

`long_term_growth` passes through unchanged in summary mode.

For the full 4-period panel (or trend / revisions detail), drop
`--summary` and use the default `--estimates` output.

### Latency

`--estimates` adds ~1.5–3s per equity (5 Yahoo property reads on a
shared Ticker; each retried independently). Non-equities pay nothing
extra (short-circuited). 10-equity `--estimates` batches cost roughly
~30–50s — use smaller groups if you hit 429s.

**Worst case under a 429 storm.** Each of the 5 sources retries up to
3 attempts; `with_retry` sleeps **between** attempts (not after the
final failed one), so 3 attempts means 2 backoff windows: ~0.5s after
attempt 1, ~1.0s after attempt 2, plus jitter — ~2s of cumulative
sleeps per failed source. 5 sources × ~2s = ~10s of sleeps total. Add
the 15 actual call attempts (each typically ~0.3s when Yahoo answers
429 quickly) and the worst-case wall-clock for one ticker is roughly
**~10–15s** before the call fails. Watch the `attempts` field on the
response (surfaced when > 1) — when it climbs to 3 across multiple
tickers, you're in throttling territory: drop batch size to ~3
tickers and pause for 30–60s between calls rather than retrying
immediately.

## When to use `--summary`

Reach for `--summary` when the question reduces to a few headline numbers:
- "when does AAPL report next" → `next_date`
- "did MSFT beat last quarter" → `last_surprise_pct`
- "which of these consistently beats" → `beat_rate_last_4` across 3+
  tickers (peer-comparison view)

Default mode is for "show me the whole earnings history" / "plot quarterly
EPS trend". A 12-row default output is ~3 KB JSON; `--summary` is ~0.4 KB —
roughly 8× smaller.

## Presenting earnings results

For "when does X report next", use `--summary` and answer in one line:

> AAPL reports next on **2026-07-30 (after market close, ET)**. Estimate \$1.89/share. Last 4 quarters averaged a +6.1% surprise (4/4 beats).

For a multi-ticker peer table (`--summary`):

| Symbol | Next Date | Next Est. EPS | Last Surprise | 4Q Avg Surprise | Beat Rate (4Q) |
|---|---|---|---|---|---|

For a full history (default mode), render past and future side-by-side or
in two sub-tables:

| Date | Time (ET) | EPS Estimate | Reported EPS | Surprise % |
|---|---|---|---|---|

For `--estimates` consensus (per ticker), four-row table by period —
multiply `*_growth` and `index_growth` by 100 for display, and
remember `eps_currency` may differ from `revenue_currency` for ADRs:

| Period | EPS Avg | EPS Low–High | EPS YoY | Rev Avg | Rev YoY | Sector YoY | # An. (EPS / Rev) |
|---|---|---|---|---|---|---|---|
| 0q (current Q) | \$1.89 | \$1.83–1.99 | +20.4% | \$108.8B | +15.7% | +24.6% | 30 / 26 |

For analyst momentum (peer table on a single period — usually `0q`):

| Symbol | EPS Avg | EPS YoY | Up 30d | Down 30d | Net | Trend (now vs 90d ago) |
|---|---|---|---|---|---|---|

Where "Net" = `eps_revisions_up_30d − eps_revisions_down_30d`,
"Trend" = `eps_trend_current / eps_trend_90d_ago − 1`.

Always escape `$` as `\$` in prose (see SKILL.md "Cross-cutting caveats").

### See also

- `info.analyst` (see `references/info.md`) — analyst **price targets**
  and **buy/hold/sell** rating summary. Complementary to `--estimates`,
  which gives the underlying **EPS / revenue forecasts** the price
  targets are derived from. Use both when answering "what do analysts
  think about X" — `info` for the rating, `--estimates` for the numbers.

## Mode-specific caveats

- **`Surprise(%)` is percent, NOT fraction.** `surprise_pct: -10.88` means
  EPS missed by 10.88%, not 0.1088. Sign matches direction (positive = beat,
  negative = miss). This breaks the `info`-section convention where most
  ratios are fractions — for earnings this field is percent-encoded
  upstream and we pass it through unchanged.
- **`--estimates` `*_growth` IS fraction.** Inside the same response,
  `earnings_dates.surprise_pct` is percent-encoded but `estimates[*].
  eps_growth` and `revenue_growth` are fraction-encoded — `0.2043` means
  20.43% YoY. The mismatch is upstream-driven (Yahoo's earnings calendar
  vs Yahoo's analyst-panel endpoint use different conventions); we pass
  both through unchanged to avoid hiding the asymmetry. If you're rendering
  both in one table, multiply `*_growth` by 100 but leave `surprise_pct`
  alone.
- **Date is tz-aware ISO with offset.** Don't strip — the offset includes
  DST (`-04:00` in summer, `-05:00` in winter for ET) and the time
  component encodes AMC (~16:00) vs BMO (~07:00–09:00). When showing a
  user-friendly date, you can render `YYYY-MM-DD (AMC)` or `YYYY-MM-DD
  (BMO)` based on whether the hour is ≥ 16 or ≤ 9.
- **HTML scrape, not JSON API.** Unlike the other three modes, earnings
  data comes from scraping `finance.yahoo.com/calendar/earnings` HTML —
  upstream changed in summer 2025 from a JSON endpoint to HTML. More
  fragile than the other modes: a Yahoo HTML rewrite breaks parsing
  silently or noisily. If `error_kind: unknown` shows up across many
  tickers, suspect upstream HTML drift first — re-run smoke and check
  yfinance changelog for parser fixes.
- **`lxml` dependency.** `pd.read_html` (used internally by yfinance)
  requires `lxml`. yfinance doesn't pin it as a hard requirement, so
  earnings.py invocations need an extra `--with 'lxml'`. Without it,
  every fetch fails with `error_kind: unknown` and a misleading
  "Missing optional dependency 'lxml'" log. The other three modes
  don't need lxml.
- **Bogus-ticker AttributeError workaround.** yfinance has an internal
  bug where `Ticker(bogus).fast_info["quoteType"]` raises
  `AttributeError: 'PriceHistory' object has no attribute '_dividends'`
  (the 404 is logged but the raised exception is unrelated). The script
  catches this explicitly in `_quote_type()` and reraises as a
  RuntimeError with text containing "not found", which `classify_error`
  then maps to `error_kind: not_found`. If yfinance fixes this bug
  upstream, the workaround is harmless (the new exception path will
  classify naturally).
- **Equity short-circuit saves a scrape.** Non-equity tickers (ETF,
  INDEX, CRYPTOCURRENCY, etc.) skip the HTML scrape based on
  `quote_type` from `fast_info` (the cheap path, ~0.3s). The trade-off
  vs unconditional scrape: equity calls cost ~0.3s extra for the
  pre-check, but non-equity calls save the ~1–2s scrape and give a
  user-friendly empty + note. Net: better UX when callers mix ticker
  types, slight overhead when they only call equities.
- **Page-size bucketing inside yfinance.** yfinance's `limit` parameter
  internally maps to a page size of 25 / 50 / 100 — `--limit 5` triggers
  size=25 and Yahoo returns ~25 rows. **Default mode** slices the
  returned list to `--limit` after fetch + filter, so output strictly
  honors the requested count. **`--summary` mode** bypasses the slice
  and feeds the full bucket to the aggregate computation (so `--limit 3
  --summary` still has enough rows for `avg_surprise_last_4`). Network
  cost is the bucketed size in both modes — `--limit 1` is no faster
  than `--limit 25`.
- **Fewer than 4 reported quarters → null summary stats.**
  `avg_surprise_last_4` and `beat_rate_last_4` are null for recent IPOs
  or low-coverage names where Yahoo's history doesn't reach 4 quarters
  with non-null surprise. The fields are present (with null values), so
  consumers don't need to test for key existence.
