[← back to SKILL.md](../SKILL.md)

# `history` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-full-rows) · [Output — `--summary` mode](#output----summary-mode) · [Output — `--events-only` mode](#output----events-only-mode) · [Output — `--shares` mode](#output----shares-mode) · [Output — `--shares --summary` mode](#output----shares---summary-mode) · [Output — `--metadata` mode](#output----metadata-mode) · [Multi-ticker batch](#multi-ticker-batch-behavior) · [When to use `--summary`](#when-to-use---summary) · [Presenting history results](#presenting-history-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **adjusted-vs-price-only**, **`total_dividends` double-count**, **intraday window caps**, **Capital Gains coverage**, **shares are not split-adjusted**, **same-date dedup**, **splits_detected is heuristic**)

Two orthogonal axes describe `history`'s output:

- **Data source** (one at a time): `default` (OHLCV) | `--events-only`
  (corporate actions) | `--shares` (share-count time series, equity-only)
  | `--metadata` (`Ticker.history_metadata` snapshot — no rows)
- **Projection**: row stream (default) | `--summary` (per-ticker
  aggregate). `--summary` is valid with **default OHLCV** or
  **`--shares`**; it doesn't combine with `--events-only` (corporate-
  action rows have no natural aggregate over the existing summary
  fields) or `--metadata` (already a snapshot, not a series).

Six modes in total: `default-rows`, `default-summary`, `events-only`,
`shares-rows`, `shares-summary`, `metadata`. (Of the 4 × 2 = 8 axis
combinations, two are CLI-rejected: `events-only --summary` and
`metadata --summary` — neither has a meaningful aggregate.) The mutex
is hand-checked in argparse — see the [CLI arguments](#cli-arguments)
below.

## Run

```bash
# Default: 1mo of daily bars, full rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py AAPL

# Custom period + interval
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1y AAPL MSFT

# Multi-ticker batch — one yf.download call, threaded internally (~3-4×
# faster than the equivalent serial loop). See "Multi-ticker batch behavior"
# section below for the schema diff.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1mo --summary AAPL MSFT GOOGL META NVDA

# Summary mode — aggregate stats, no rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period ytd --summary AAPL

# Events-only — corporate action rows only (dividend/split/capital_gains), no OHLCV
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5y --events-only AAPL
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 10y --events-only --tail 5 VFIAX

# Shares — shares-outstanding history (buyback / issuance signal), equity-only
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 2y --shares AAPL
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period max --shares --tail 5 AAPL
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 2y --shares --summary AAPL MSFT GOOGL

# Metadata — currency / exchange / first_trade_date / valid_ranges / etc.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --metadata AAPL
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --metadata --format csv AAPL MSFT 0700.HK

# Intraday
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5d --interval 1h AAPL

# Intraday including pre-market + after-hours bars
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1d --interval 5m --prepost AAPL

# Price-only series (no dividend adjustment) — for separating price return
# from total return. Note: still split-adjusted, NOT raw printed-tape prices.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5y --no-adjust AAPL

# Explicit date window (alternative to --period)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --start 2023-01-15 --end 2023-01-22 AAPL

# Last 10 rows only — useful when --period max would dump ~11k rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period max --tail 10 AAPL

# CSV output (one row per bar; symbol/period/timezone columns prepended)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5d --format csv AAPL MSFT
```

## CLI arguments

- `--period` — yfinance period string. Default `1mo` (used when neither
  `--period` nor `--start` is given). Valid: `1d`, `5d`, `1mo`, `3mo`,
  `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max`. **Mutually exclusive with
  `--start`.**
- `--start` / `--end` — ISO `YYYY-MM-DD` window. Use when you need an
  exact date range (e.g., "AAPL Jan 15 to Jan 22, 2023") instead of a
  rolling window. `--end` requires `--start`; if `--end` is omitted,
  the window is start → today (and the response echoes today as `end`
  so the output is self-describing). Mutually exclusive with `--period`.
  **`end` is exclusive in yfinance**, so to query a single trading day
  D, pass `--start D --end D+1` (e.g., `--start 2023-01-17 --end 2023-01-18`
  for that one bar). `--start D` alone gives ~30+ days through today.
- `--interval` — bar size. Default `1d`. Valid: `1m`, `2m`, `5m`, `15m`,
  `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo`. Yahoo caps
  intraday windows: `1m` ≤ 7 days, other intraday ≤ 60 days, `1h` ≤ 730 days.
- `--summary` — flag. Output aggregate stats (start/end close, change_abs,
  change_pct, period high/low with dates, avg volume, total dividends, splits)
  instead of full rows.
- `--events-only` — flag. Output only rows where a corporate action fired —
  dividend, split, or capital-gain distribution. OHLCV columns are stripped;
  rows have just `date` + `dividends` + `split_ratio` + `capital_gains`. Adds
  a top-level `has_capital_gains_column` (True for fund tickers; False for
  non-funds — the underlying yfinance DataFrame literally lacks the column
  for non-funds, so a 0.0 here means "Yahoo doesn't track this for this
  instrument", not "no distribution paid"). **Mutually exclusive with
  `--summary`, `--metadata`, `--prepost`, and intraday `--interval` values**
  (`1m` / `5m` / ... / `1h`) — corporate actions are end-of-day events,
  extended-hours bars contain no event data, and Yahoo's intraday windows
  are too short (7-60 days) to capture meaningful events. `--head` /
  `--tail` apply the same way as default mode (post-fetch projection over
  the events list, with `rows_truncated: {total, shown}` surfaced when
  truncation actually applies — same shape as default mode for schema
  consistency).
- `--shares` — flag. Output a shares-outstanding time series via
  `Ticker.get_shares_full`. Each row carries `date` +
  `shares_outstanding` (integer). Yahoo emits a row only when the share
  count changes (issuance / buyback / split), so the series is sparse
  irregular-daily — typical equities return dozens to hundreds of rows
  per year, NOT one per trading day. Values are **post-split actual
  counts, NOT split-adjusted to a single base**: AAPL's 4-for-1 split
  in 2020-08 shows as a clean 4× step (verified: prev 4.28B, current
  17.10B, ratio 4.0 in `splits_detected`). Use for buyback / issuance
  analysis; for valuation-multiple math use point-in-time
  `fast_info.shares_outstanding` instead.

  **Dedup.** Yahoo occasionally emits multiple rows for one calendar
  date (different upstream filings collide). Verified: AAPL Mar 2024
  returned 16 rows over 12 unique dates, with 2026-03-26 carrying 3
  distinct values. The script collapses these via
  `groupby(date).last()` — Yahoo's emission order is preserved and the
  LAST observation wins. When dedup actually fires,
  `same_date_duplicates_dropped: N` is surfaced top-level.

  **Split detection.** Adjacent-row ratios `>= 1.5` (forward) or
  `<= 0.667` (reverse) populate a `splits_detected: [{date, prev_shares,
  current_shares, ratio}]` field — heuristic. Boundary is **inclusive**,
  so exact 3-for-2 (ratio = 1.5) and 2-for-3 (≈ 0.667) splits register.
  For ground truth (event date + ratio from Yahoo's actions feed),
  chain `history --events-only --start S --end E`. Splits are detected
  on the post-dedup, pre-truncation series so a `--tail 5` user still
  sees them.

  **Coverage: equities only.** ETFs / mutual funds / indexes / crypto /
  FX / futures all return `rows: []` with a `note` field (no
  shares-outstanding concept for these instruments). Bogus / delisted
  tickers also fall through that note path (Yahoo logs an HTTP 404 to
  stderr but the underlying call returns `None`, not an exception).
  **Narrow windows on real equities** (1-day-inside-data window,
  pre-IPO, future) ALSO return `None` — verified empirically — and
  share the same `note`. Four indistinguishable causes; chain
  `fast_info` to disambiguate via `quote_type`.

  **Cost is 1 HTTP per ticker** (no batching — there's no `yf.download`
  equivalent for shares; multi-ticker is a serial loop, same shape as
  `--metadata`). Each ticker's `timezone` field carries its native
  exchange tz directly (no UTC roundtrip, no `exchange_tz` companion
  field).

  **Combinable with `--summary`** — `--shares --summary` projects the
  deduped series to a per-ticker flat aggregate (start/end shares,
  change_abs, change_pct, min/max with dates, `splits_detected_count`,
  `same_date_duplicates_dropped`). See
  [Output — `--shares --summary` mode](#output----shares---summary-mode).

  **Mutually exclusive with `--events-only`, `--metadata`, `--prepost`,
  `--no-adjust`, and intraday `--interval` values** — share-count
  changes are end-of-day events that don't fire in extended hours, the
  underlying values are integers (not adjustable prices), and intraday
  windows are meaningless for daily-or-coarser changes. `--head` /
  `--tail` apply post-fetch in row mode (same shape as default /
  events-only); under `--summary` they're rejected (would silently
  distort the aggregate over a clipped slice).
- `--metadata` — flag. Return `Ticker.history_metadata` only — currency,
  exchange (short + full name), instrument_type, first_trade_date,
  regular_market_time, valid_ranges, has_prepost, IANA exchange tz, and a
  small set of "current quote" mirror fields (regular_market_price,
  fifty_two_week_high/low, etc.). One row per ticker, no per-bar / per-event
  data. Datetime fields are pre-converted to ISO strings; raw `*_epoch`
  siblings are preserved for callers that need their own arithmetic. **Cost
  is 1 HTTP per ticker** (no batching — yfinance's `yf.download` doesn't
  reliably populate per-Ticker `history_metadata`, so the metadata path
  serializes through `Ticker.history()`). **Mutually exclusive with
  `--summary`, `--events-only`, `--head`, `--tail`, `--no-adjust`, and
  `--prepost`** (all of those describe how to project rows; metadata is a
  rowless snapshot).
- `--prepost` — flag. Include pre-market (04:00–09:30 ET) and after-hours
  (16:00–20:00 ET) bars. Intraday-only — daily+ intervals ignore it. Use for
  "what's the after-hours price right now", "where did the stock open
  pre-market after earnings", or any extended-hours question. Caveats:
  extended-hours bars have much lower volume and wider spreads, and the bar
  immediately after the open / before the close may show outsized prints.
- `--head N` / `--tail N` — keep only the first / last N rows of the
  default-mode output. Mutually exclusive with each other; ignored for
  `--summary`. yfinance always pulls the full window — `--head` / `--tail`
  is a post-fetch projection to keep output size manageable. When applied,
  the response gains a `rows_truncated: {total: N, shown: M}` field so
  the caller can see what was dropped.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one record per line (streaming-friendly,
  one ticker per line). `csv` flattens: default mode = one CSV row per
  OHLCV bar with `symbol`/`period`/`start`/`end`/`interval`/`timezone`
  prepended and `error`/`error_kind`/`attempts` appended; `--summary`
  mode = one row per ticker. **For multi-ticker batches** (N≥2) the
  header gains an `exchange_tz` column right after `timezone` so the
  per-ticker daily-date calendar is self-describing; single-ticker CSV
  keeps the original 6-col base for backward-compat. **Nested fields are
  silently dropped in CSV** — `splits` (list of split events) and
  `rows_truncated` (the head/tail metadata dict) are absent from CSV
  output. Use `json` or `ndjson` if you need them.
- `--no-adjust` — flag. Pass `auto_adjust=False` to yfinance. **This does
  NOT return raw printed-tape prices** — yfinance's `auto_adjust=False`
  still split-adjusts the close column; it only stops backing dividends
  out of the price curve. Empirical: around AAPL's 4-for-1 split on
  2020-08-31, both `--no-adjust` and default give ~\$125 (post-split-
  equivalent), not the printed ~\$500 pre-split. The two diverge only
  over windows that contain dividend payments, where the default's
  adjusted start is lower than `--no-adjust`'s by the cumulative
  dividend-reinvestment effect (AAPL `--period max` start_close is
  ≈\$0.10 default vs ≈\$0.13 no-adjust — a 1.3× factor from 45 yrs of
  dividends, not from splits). Use `--no-adjust` to separate **price
  return** (no-adjust) from **total return** (default); use default for
  almost everything else. Neither mode is suitable for matching a
  brokerage-statement printed price from before a split — that requires
  a different data source.

## Output — default mode (full rows)

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "period": "1mo",
    "interval": "1d",
    "timezone": "America/New_York",
    "rows": [
      {
        "date": "2026-04-08",
        "open": 184.92, "high": 186.50, "low": 184.10, "close": 185.75,
        "volume": 45123456,
        "dividends": 0.0,
        "split_ratio": 0.0
      }
    ]
  }
]
```

Daily+ bars use `YYYY-MM-DD` dates in the market's local timezone. For
single-ticker calls that's the top-level `timezone` field (e.g.
`America/New_York` for NYSE / Nasdaq, `Asia/Hong_Kong` for HK); for
multi-ticker batches `timezone` is always `"UTC"` and the per-ticker
exchange tz lives in `exchange_tz` instead — see [Multi-ticker batch
behavior](#multi-ticker-batch-behavior). Either way, `2026-05-07` for
`0700.HK` is a different actual moment than `2026-05-07` for `AAPL`.
Intraday uses ISO timestamps with the offset baked in.

**Closes are split- and dividend-adjusted by default** (yfinance
`auto_adjust=True`) — total-return view, what you want for return
calculations. With `--no-adjust` (`auto_adjust=False`), closes are still
split-adjusted but dividends are not backed out — that's a price-return
view. **Neither mode reproduces the actual printed pre-split price** (e.g.
AAPL pre-2020-split prints around \$500 are not retrievable here). See
`--no-adjust` in CLI args above for the empirical diff. For dividend
amounts as a separate stream (rather than baked into closes), see
[`info` dividend section](info.md#output--default-mode-full-sections)
or use `dividends` column in default `history` rows.

`dividends` is non-zero only on ex-dividend days; `split_ratio` is non-zero
only on split days (4.0 = 4-for-1 forward; 0.5 = 1-for-2 reverse / share
consolidation). Prices are raw floats — round for display, not for storage.

**Retry surfacing.** First-shot success has no `attempts` field. If the
call retried before succeeding (transient 429 / network), the response
gains `"attempts": N` at the top level (alongside `symbol`, `period`,
`rows`, etc.). Same convention across all three modes.

A failed ticker looks like:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "no data returned (delisted, wrong suffix, or rate-limited)",
  "error_kind": "not_found",
  "attempts": 1
}
```

`error_kind` ∈ `{rate_limit, not_found, network, unknown}`; `attempts`
is the retry count (1 for not_found, up to 3 for transient failures).

## Output — `--summary` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "period": "1y",
    "interval": "1d",
    "timezone": "America/New_York",
    "rows_count": 252,
    "start_date": "2025-05-08",
    "end_date": "2026-05-07",
    "start_close": 184.92,
    "end_close": 215.30,
    "change_abs": 30.38,
    "change_pct": 16.4287,
    "period_high": 237.49,
    "period_high_date": "2026-01-15",
    "period_low": 169.21,
    "period_low_date": "2025-08-05",
    "avg_volume": 52341890,
    "total_dividends": 0.96,
    "splits": []
  }
]
```

`change_pct` is end vs start, in percent. `splits` is a list of
`{"date", "ratio"}` objects. Forward splits have `ratio > 1.0` (4.0 means
4-for-1, share count 4×); reverse splits have `ratio < 1.0` (0.5 means
1-for-2 consolidation, share count halved). Empty list is normal — most
tickers don't split during a given period.

**Watch the magnitude on long windows.** `--period max` for a long-listed
name like AAPL gives `start_close` ≈ \$0.10 (split-adjusted) and
`end_close` ≈ \$215 — that's a literally-correct `change_pct` of ~215000%.
Don't paste that figure into prose; for any window > ~10 years prefer
**ratio-of-magnitudes** ("up ~2150× since IPO") or **CAGR**
(`(end/start) ** (1/years) - 1`) as the human-readable framing.

Note: `--summary` works with intraday intervals too. The semantics don't
change — `start_close` / `end_close` are still first/last bar closes,
`period_high` / `period_low` still scan the window. The only visible
difference is that the date fields become ISO timestamps instead of
`YYYY-MM-DD`. `total_dividends` and `splits` will usually be `0` / `[]`
because corporate actions don't fire mid-session.

## Output — `--events-only` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "period": "5y",
    "interval": "1d",
    "timezone": "America/New_York",
    "rows": [
      {"date": "2025-02-10", "dividends": 0.25, "split_ratio": 0.0, "capital_gains": 0.0},
      {"date": "2025-05-12", "dividends": 0.26, "split_ratio": 0.0, "capital_gains": 0.0},
      {"date": "2025-08-11", "dividends": 0.26, "split_ratio": 0.0, "capital_gains": 0.0}
    ],
    "has_capital_gains_column": false
  }
]
```

Each row carries one date + the three event fields. Non-event days are
filtered out — every row in `rows` has at least one nonzero value among
`dividends` / `split_ratio` / `capital_gains`. When `--head` / `--tail`
truncates the list, the response gains `rows_truncated: {total, shown}`
(same shape as default mode — total is pre-truncation, shown is final
list length).

`has_capital_gains_column` is a fund-only signal: `true` when the
underlying yfinance DataFrame contained a `Capital Gains` column (fund
ticker — ETF / mutual fund), `false` for non-funds (equity / index /
crypto / FX / future) where the column simply doesn't exist. A
`capital_gains: 0.0` row for a fund means "no distribution that day"; the
same value on a non-fund row means "Yahoo doesn't track this for this
instrument type" — the column is uniformly 0.0 for schema completeness.

For the multi-ticker batch path, `--events-only` rows still get the
`exchange_tz` fold so dates land in each ticker's local trading-day
calendar (same logic as default mode — see [Multi-ticker batch
behavior](#multi-ticker-batch-behavior)).

## Output — `--shares` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "period": "max",
    "start": null,
    "end": null,
    "interval": "1d",
    "timezone": "America/New_York",
    "rows": [
      {"date": "2015-10-28", "shares_outstanding": 5575330000},
      {"date": "2020-10-22", "shares_outstanding": 17102499840},
      {"date": "2024-08-02", "shares_outstanding": 15204137984},
      {"date": "2025-05-02", "shares_outstanding": 14935826000},
      {"date": "2026-05-05", "shares_outstanding": 14687356000}
    ],
    "same_date_duplicates_dropped": 80,
    "splits_detected": [
      {"date": "2020-10-22", "prev_shares": 4275630080, "current_shares": 17102499840, "ratio": 4.0}
    ]
  }
]
```

Each row is one observation of the share count as of that date. Yahoo
emits a row only when the count changes (issuance / buyback / split) —
not one per trading day — so series spacing is irregular.

**`same_date_duplicates_dropped`** appears top-level when at least one
calendar date had multiple Yahoo observations and dedup fired (verified
empirically — AAPL `--period max` typically drops ~80 rows). Yahoo's
emission order is preserved and the LAST observation wins. Field is
absent when no dedup was needed.

**`splits_detected`** appears top-level when adjacent-row ratios cross
the forward (`>= 1.5×`) or reverse (`<= 0.667×`) threshold (boundary
inclusive — exact 3-for-2 / 2-for-3 splits register). Heuristic — for
ground truth use `history --events-only --start S --end E`. AAPL's
2020-08-31 4-for-1 split shows here as a single entry with ratio = 4.0
(observe: Yahoo's first post-split shares row may lag the split date by
a filing cycle — the 2020-08-31 split shows up on 2020-10-22 in this
endpoint). Detection runs on the post-dedup, pre-truncation series so
splits remain visible under `--head` / `--tail`.

For a buyback question, diff `rows[0]` vs `rows[-1]` for window-spanning
net change. **If `splits_detected` is non-empty, net out the split
factor first** — a 4× jump otherwise looks like a 4× secondary issuance.

**Empty-result path** (non-equity / bogus / no-coverage / narrow window):

```json
[
  {
    "symbol": "SPY",
    "period": "2y",
    "start": null,
    "end": null,
    "interval": "1d",
    "timezone": null,
    "rows": [],
    "note": "no shares data — likely non-equity (ETF/fund/index/crypto/FX/future) / bogus / no coverage / window too narrow; chain fast_info via quote_type"
  }
]
```

The `note` field carries the ambiguous-empty signal (same shape as
`holders` / `insiders` / `analyst` / `sec_filings`). Four
indistinguishable causes at this endpoint: non-equity instrument,
bogus / delisted ticker, real equity with no Yahoo coverage, or window
too narrow on a real equity (verified: 1-day-inside-data, pre-IPO, and
future windows all return `None` from `get_shares_full`, just like
non-equities). Chain `fast_info` to read `quote_type` for the first
three; widen `--period` to rule out the fourth. `note` and `error` are
mutually exclusive.

CSV layout: one row per share-count observation, columns are
`symbol / period / start / end / interval / timezone / date /
shares_outstanding / note / error / error_kind / attempts`. Note rows
(empty-result path) emit a single carrying row with `date` and
`shares_outstanding` blank but the rest of the meta populated, so the
ticker isn't silently dropped from CSV. The nested `splits_detected`
list and `same_date_duplicates_dropped` int are not projected to CSV
(use JSON / NDJSON if you need them — same convention as default
summary's `splits` list). No `exchange_tz` column — multi-ticker shares
is a serial loop (not batched), so each ticker's `timezone` is already
its native IANA zone.

**Window flag handling.** `--shares` honors `--period` / `--start` /
`--end` (unlike `--metadata` which is window-invariant). With `--period`
the script translates to ISO dates internally — `mo = 30d`, `y = 365d`
**approximations**, NOT calendar months / years (so `--period 1y`
translates to "last 365 days from today", which differs from
`relativedelta(years=1)` by 1 day in leap years). Fine for sparse
irregular daily data; document explicitly because OHLCV `--period`
semantics in yfinance ARE calendar-aware. `--period 1d` / `5d` are
typically too narrow for shares data (returns `None` → empty / note);
recommend `--period 1mo` or longer. `--period max` maps to
`1970-01-01 → today` to maximize Yahoo's available coverage (typically
~10 years for major equities; AAPL goes back to 2015-10).

`--interval` (when daily-or-coarser) is silently ignored under
`--shares` — `get_shares_full` is not interval-parametrized; the field
is echoed in the response for schema consistency only.

## Output — `--shares --summary` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "period": "2y",
    "start": null,
    "end": null,
    "interval": "1d",
    "timezone": "America/New_York",
    "rows_count": 82,
    "start_date": "2024-05-11",
    "end_date": "2026-05-05",
    "start_shares": 15334099968,
    "end_shares": 14687356000,
    "change_abs": -646743968,
    "change_pct": -4.2177,
    "min_shares": 14667688000,
    "min_shares_date": "2026-05-02",
    "max_shares": 15787400192,
    "max_shares_date": "2025-03-11",
    "splits_detected_count": 0,
    "same_date_duplicates_dropped": 40
  }
]
```

One row per ticker — flat aggregate over the deduped series, designed
for **peer compare** ("rank AAPL / MSFT / GOOGL by 2y buyback rate").
`change_pct` is in **percent** to match default `--summary.change_pct`
convention (don't confuse with `info`-style fraction encoding). Negative
`change_abs` / `change_pct` = net buyback; positive = net issuance.

**Splits warning carries through.** When `splits_detected_count > 0`,
the `change_abs` / `change_pct` / `min_shares` / `max_shares` fields
include the split as a 4× (or N×) jump — same caveat as default-mode
rows. The full `splits_detected` list is JSON-only (CSV summary drops
it; only the count column survives) — read it from JSON / NDJSON to net
splits out before quoting buyback rates.

`--head` / `--tail` are **rejected** under `--shares --summary`
(clipping the row stream before the aggregate would silently distort
change_pct / min / max). Empty-result rows still carry `note` — the
summary fields collapse out and only `symbol / period / start / end /
interval / timezone / rows: [] / note` survive on those rows.

CSV layout: one row per ticker, columns are `symbol / period / start /
end / interval / timezone /` `rows_count / start_date / end_date /
start_shares / end_shares / change_abs / change_pct / min_shares /
min_shares_date / max_shares / max_shares_date / splits_detected_count /
same_date_duplicates_dropped / note / error / error_kind / attempts`.

## Output — `--metadata` mode

Sample numbers and dates below are illustrative, not a real capture.

```json
[
  {
    "symbol": "AAPL",
    "currency": "USD",
    "exchange_name": "NMS",
    "full_exchange_name": "NasdaqGS",
    "instrument_type": "EQUITY",
    "first_trade_date": "1980-12-12",
    "first_trade_date_epoch": 345479400,
    "regular_market_time": "2026-05-08T19:59:58",
    "regular_market_time_epoch": 1778270402,
    "has_prepost": true,
    "gmt_offset": -14400,
    "timezone_short": "EDT",
    "exchange_timezone_name": "America/New_York",
    "data_granularity": "1h",
    "valid_ranges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"],
    "long_name": "Apple Inc.",
    "short_name": "Apple Inc.",
    "regular_market_price": 215.30,
    "previous_close": 213.85,
    "chart_previous_close": 211.41,
    "fifty_two_week_high": 237.49,
    "fifty_two_week_low": 169.21,
    "regular_market_day_high": 216.10,
    "regular_market_day_low": 213.50,
    "regular_market_volume": 45708423,
    "price_hint": 2
  }
]
```

The metadata projection serves three distinct use cases that no other
mode covers:

1. **`first_trade_date`** — when did this ticker start trading on Yahoo?
   Useful for context ("AAPL has been listed since 1980-12-12") and for
   bounding `--period max` queries.
2. **`valid_ranges`** — which `--period` strings does Yahoo accept for
   *this specific ticker*? The skill-wide `VALID_PERIODS` list is the
   intersection across all tickers; some instruments support fewer
   periods (e.g., recently-listed names have no `max` data, some
   indexes refuse `5d`).
3. **`exchange_timezone_name`** — IANA tz string for downstream
   `tz_convert` calls. Disambiguates from `timezone_short` (Yahoo's
   short code like `EDT` / `HKT` which is DST-dependent).

The `regular_market_*` and `fifty_two_week_*` fields overlap with
`fast_info` (use whichever you already have a call to); `currency` and
`instrument_type` overlap with both `fast_info` and `info`. The unique
value-adds are the three above, plus `has_prepost` (whether extended
hours data is fetchable for this ticker — `false` for HK, `true` for
US) and `data_granularity` (what bar size yfinance returned in the
underlying `.history()` call).

**No `period` / `start` / `end` / `interval` fields appear in metadata
output** — those describe a query window, but metadata is a snapshot
that's window-invariant. Pass any window flag to satisfy CLI parsing;
the value is consumed by the underlying `.history()` call (cheapest is
`--period 1mo`, the default) but isn't echoed back.

CSV layout: one row per ticker, no `exchange_tz` column (the IANA tz
already lives in `exchange_timezone_name`), `valid_ranges` is
JSON-encoded into a single cell.

A failed ticker has the same error shape as other modes:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "no metadata returned (delisted, wrong suffix, or rate-limited)",
  "error_kind": "not_found",
  "attempts": 1
}
```

## Multi-ticker batch behavior

Passing **two or more symbols** routes through `yf.download` — one HTTP
request, threaded internally by yfinance, then sliced per ticker.
Empirically (5 US daily-summary tickers, US connection): ~2.5 s
sequential `Ticker.history` vs ~0.7 s batched, so ~3–4× faster.
Yahoo also tends to throttle a single batched request less aggressively
than N serial ones, so 429s drop too. Single-ticker calls (one symbol)
keep the original `Ticker.history` path — output schema unchanged.

The batch path adds two metadata keys not present on single-ticker calls:

- `"timezone": "UTC"` — intraday timestamps emit with `+00:00` offset.
- `"exchange_tz": "<IANA tz>"` — chosen by `helpers.infer_exchange_tz()`
  from the ticker conventions (no Yahoo round-trip). Daily date strings
  are folded into this tz so they match each instrument's natural
  trading-day calendar. The decision tree:
  1. **Indexes** (`^GSPC`, `^N225`, `^HSI`, ...) → home market tz from
     `INDEX_TZ` map; **unknown `^FOO` falls back to UTC**.
  2. **FX / futures** (`USDJPY=X`, `CL=F`) → UTC (no single home market).
  3. **Crypto** (`BTC-USD`, `ETH-USDT`, ...) → UTC (24/7 trading; UTC is
     the natural daily boundary).
  4. **Suffixed equities** (`0700.HK`, `BMW.DE`, ...) → `TZ_BY_SUFFIX`;
     unknown suffix defaults to `America/New_York`.
  5. **Plain ticker** (`AAPL`, `BRK.B`) → `America/New_York`.

  Without this fold, a `0700.HK` day-bar at midnight HKT (= 16:00 prev-day
  UTC) would `strftime` to the wrong calendar date in batch mode. The
  smoke test compares each batched HK date against the single-ticker
  (native-tz) date string — they must be identical, which is what
  guarantees the fold is correct.

Sample batch output (cross-market `--summary`):

```json
[
  {
    "symbol": "AAPL",
    "period": "1mo",
    "interval": "1d",
    "timezone": "UTC",
    "exchange_tz": "America/New_York",
    "rows_count": 22,
    "start_date": "2026-04-08",
    "end_date": "2026-05-07",
    "start_close": 184.92,
    "end_close": 215.30,
    "change_pct": 16.4287,
    ...
  },
  {
    "symbol": "0700.HK",
    "timezone": "UTC",
    "exchange_tz": "Asia/Hong_Kong",
    "start_date": "2026-04-08",
    "end_date": "2026-05-07",
    ...
  }
]
```

Per-ticker error isolation: a delisted / mistyped symbol comes back as
a `not_found` error dict (with `exchange_tz` still populated so batch
CSV columns stay aligned); sibling tickers in the batch are unaffected.
A network or sustained-rate_limit failure of the **whole** batch retries
via `with_retry` and, if exhausted, marks every ticker with the
batch-level error.

**Equity-suffix coverage** matches SKILL.md's exchange table. Unknown
suffix defaults to `America/New_York`, which silently produces
wrong-by-a-day daily dates for the unmapped exchange — if you spot
that, add the suffix to `TZ_BY_SUFFIX`. Same caveat for unknown
`^FOO` indexes: if you spot off-by-half-day dates for an index that
should land on a specific market's calendar, add it to `INDEX_TZ`.

## When to use `--summary`

Reach for `--summary` when the user asks "how much did X change", "what was
the high last year", or any other question that boils down to a few numbers
over a window. Full rows for a 1y daily series are ~252 rows × 8 fields ≈
2k data points per ticker; for 5y it's ≈ 10k. Use full rows when the user
actually wants to see / plot / compare individual bars (typically short
windows, or an explicit "show me the last N days").

## Presenting history results

Default rows mode → compact markdown table:

| Date | Open | High | Low | Close | Volume |
|---|---|---|---|---|---|

Trim to the most recent ~10 rows in chat unless the user asked for more, and
note the period and interval in the heading.

`--summary` mode → one-sentence summary:
> AAPL is up 16.43% over the past year, from \$184.92 (2025-05-08) to \$215.30 (2026-05-07). Period high \$237.49 on 2026-01-15; low \$169.21 on 2025-08-05. Avg volume 52.3M, \$0.96 paid in dividends.

## Mode-specific caveats

- **Intraday windows are capped.** `1m` only goes back ~7 days; other sub-hour
  intervals ~60 days; `1h` up to ~730 days. Asking for longer + intraday
  silently returns less data than requested.
- **Adjusted vs price-only closes.** Default = split + dividend adjusted
  (total-return view). `--no-adjust` = split-adjusted only (price-return
  view). Neither reproduces the actual pre-split printed price you'd see
  on a brokerage statement; that needs a different data source.
- **`Capital Gains` coverage is sparse.** The `capital_gains` field in
  `--events-only` output (and the underlying `Capital Gains` column on
  the yfinance DataFrame for fund tickers) is populated by Yahoo
  inconsistently — most Vanguard and Fidelity index funds verified
  2026-05 returned the column with all zeros across multi-year windows,
  even where the funds publicly distributed cap gains. Treat the column
  as schema-complete but data-sparse: the presence of the column
  (`has_capital_gains_column: true`) tells you this is a fund;
  non-zero values are a real signal but absence is not. For
  authoritative cap-gains data you'll need a different source. The
  same caveat applies to `Ticker.capital_gains` directly — the yfinance
  property returns an empty Series for the same set of funds. This is
  a Yahoo data quality issue, not a yfinance bug.
- **`--shares` values are NOT split-adjusted.** `Ticker.get_shares_full`
  returns the actual share count as of each date — a 4-for-1 split
  multiplies the count on the split date (NOT a continuous backward-
  adjustment that hides the discontinuity). AAPL is the canonical
  example: ≈ 5.5B in Oct 2015 → 4× on 2020-08-31 → ≈ 22B → buybacks →
  ≈ 14.7B today. Useful: split events show up as a clean step in the
  series, surfaced via `splits_detected`. NOT useful: computing a
  per-share metric that should be split-adjusted (e.g. EPS on the
  pre-split share count). For per-share fundamentals use `info` or
  `financials` (Yahoo split-adjusts those backwards). Coverage
  empirically goes back ~10 years for major equities; older data isn't
  in Yahoo's window for this endpoint regardless of `--period max`.
- **`--shares` same-date dedup is "last-wins".** Yahoo emits multiple
  rows per calendar date when several upstream filings collide
  (verified: AAPL 2024-03-26 returned 3 distinct values). The script
  collapses these via `groupby(date).last()` and surfaces
  `same_date_duplicates_dropped: N` top-level when N > 0. "Last" is
  Yahoo's emission order — deterministic but NOT principled (Yahoo
  doesn't expose filing-date semantics here, so we don't know which
  observation is the "official" one). For most buyback-rate questions
  this is fine; for forensic analysis of a specific date, fetch the raw
  Series via `Ticker.get_shares_full` directly and inspect all rows.
- **`splits_detected` is heuristic, not authoritative.** Adjacent-row
  ratios `>= 1.5` fire as forward-split candidates and `<= 0.667` as
  reverse-split candidates. Threshold catches all common ratios
  (3-for-2 / 2-for-1 / 3-for-1 / 4-for-1 forward; 2-for-3 / 1-for-2 /
  1-for-3 / 1-for-4 reverse — boundary INCLUSIVE so exact 1.5 / 0.667
  register) with comfortable gap from organic buyback / issuance (rare
  to exceed 5% per single Yahoo observation). False positives: a
  same-date dedup edge case where a non-canonical row crosses the
  threshold; an extreme single-observation issuance (>= 50%) — rare
  in practice. False negatives: splits where Yahoo's first post-split
  row hasn't propagated yet. **For ground truth chain `history
  --events-only --start S --end E`** — Yahoo's actions feed has the
  official event date and ratio. Empirical oddity: `splits_detected`
  may surface a split on a date weeks AFTER the actual split (e.g.
  AAPL's 2020-08-31 4-for-1 shows on 2020-10-22 in this endpoint —
  Yahoo's filing-cycle lag, not our bug).
- **`total_dividends` + adjusted closes can double-count.** Default mode
  has `auto_adjust=True`, so `change_pct` already reflects total return
  (price + reinvested dividends). `total_dividends` reports the *nominal
  cash* dividends paid over the window — a separate number, not an addend.
  Don't add it to `change_pct` thinking you're "including dividends" — the
  closes already include them. Use `total_dividends` for "how much income
  did this position generate" questions, not for return adjustments. With
  `--no-adjust`, `change_pct` is price-only and `total_dividends` is the
  correct addend.
