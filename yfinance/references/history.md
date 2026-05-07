[← back to SKILL.md](../SKILL.md)

# `history` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-full-rows) · [Output — `--summary` mode](#output----summary-mode) · [Multi-ticker batch](#multi-ticker-batch-behavior) · [When to use `--summary`](#when-to-use---summary) · [Presenting history results](#presenting-history-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **adjusted-vs-price-only**, **`total_dividends` double-count**, **intraday window caps**)

Historical OHLCV time series. Default mode returns full bars; `--summary`
returns aggregate stats over the window.

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
- **`total_dividends` + adjusted closes can double-count.** Default mode
  has `auto_adjust=True`, so `change_pct` already reflects total return
  (price + reinvested dividends). `total_dividends` reports the *nominal
  cash* dividends paid over the window — a separate number, not an addend.
  Don't add it to `change_pct` thinking you're "including dividends" — the
  closes already include them. Use `total_dividends` for "how much income
  did this position generate" questions, not for return adjustments. With
  `--no-adjust`, `change_pct` is price-only and `total_dividends` is the
  correct addend.
