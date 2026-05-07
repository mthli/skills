[← back to SKILL.md](../SKILL.md)

# `earnings` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-full-list) · [Output — `--summary` mode](#output----summary-mode) · [When to use `--summary`](#when-to-use---summary) · [Presenting earnings results](#presenting-earnings-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **Surprise(%) units**, **scrape fragility**, **lxml dependency**)

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
- `--past-only` / `--future-only` — mutually exclusive filters. Default is
  both. Filters apply BEFORE `--limit`, so `--past-only --limit 8` returns
  the 8 most recent reported quarters. Incompatible with `--summary` (the
  summary uses both directions to compute next_ vs last_).
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`:
  default mode = one row per `(symbol, earnings_date)` with `symbol`,
  `quote_type`, `timezone`, `note` prepended; `--summary` mode = one row
  per ticker, with `note` between `quote_type` and the summary fields.
  The `note` column is blank for equity rows and populated for non-equity
  short-circuits — gives CSV consumers the same non-equity signal as
  JSON without dropping the field.

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

**Retry surfacing.** First-shot success has no `attempts` field. If either
the quote_type pre-check OR the earnings scrape retried before succeeding,
the response gains `"attempts": N` at the top level. N is **`max(qt_attempts,
scrape_attempts)`** — the most retries seen in any single underlying yfinance
call. This matches the convention used by `fast_info` / `history` / `info`
(each of which reports retries from a single internal call), so an
`attempts: 3` here means the same as in those modes. Surfaced only when > 1.

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

Always escape `$` as `\$` in prose (see SKILL.md "Cross-cutting caveats").

## Mode-specific caveats

- **`Surprise(%)` is percent, NOT fraction.** `surprise_pct: -10.88` means
  EPS missed by 10.88%, not 0.1088. Sign matches direction (positive = beat,
  negative = miss). This breaks the `info`-section convention where most
  ratios are fractions — for earnings this field is percent-encoded
  upstream and we pass it through unchanged.
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
