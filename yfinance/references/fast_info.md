[← back to SKILL.md](../SKILL.md)

# `fast_info` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting fast_info results](#presenting-fast_info-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **delayed-data** caveat)

Per-ticker current quote (latest snapshot, market cap, 52-week range, etc.).
Cheapest mode in the skill (~0.3–0.5 s per ticker).

## Run

```bash
# Default JSON output, one entry per ticker
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA

# CSV — one row per ticker, all fields as columns
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py --format csv AAPL MSFT

# NDJSON — one JSON object per line, streaming-friendly
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py --format ndjson AAPL MSFT
```

Tickers are positional args.

## CLI arguments

- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`
  emits a single header row plus one row per ticker; columns are
  `symbol` + the raw FIELDS list + `change_abs` + `change_pct` +
  `error` + `error_kind` (the trailing two are blank for successful
  rows).

## Output schema

Numeric values below are illustrative — they do not represent a real
captured snapshot of any ticker on any date.

```json
[
  {
    "symbol": "AAPL",
    "last_price": 191.07,
    "previous_close": 187.62,
    "open": 188.01,
    "day_high": 191.20,
    "day_low": 187.50,
    "last_volume": 41234567,
    "currency": "USD",
    "market_cap": 2950000000000,
    "exchange": "NMS",
    "timezone": "America/New_York",
    "shares": 15500000000,
    "fifty_day_average": 184.10,
    "two_hundred_day_average": 178.50,
    "year_high": 199.62,
    "year_low": 164.08,
    "change_abs": 3.45,
    "change_pct": 1.84
  }
]
```

`change_pct` is today vs `previous_close` (percent, not fraction);
`change_abs` is the absolute change in the source currency (the
top-level `currency` field — USD, HKD, etc.). Both `change_abs` and
`change_pct` are always present; they're `null` (not absent) when
`previous_close` is missing or zero. Any other field Yahoo did not
return is also `null`. Prices are raw floats — round for display.

**Retry surfacing.** If the call succeeded on the first attempt (the
common case), the response has no `attempts` field. If it took 2 or 3
tries (transient 429 / network error), the response gains an
`"attempts": N` field at the top level. Use this signal to spot tickers
where Yahoo is currently flaky.

A failed ticker looks like:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "no quote returned (delisted, wrong suffix, or rate-limited)",
  "error_kind": "not_found",
  "attempts": 1
}
```

`error_kind` ∈ `{rate_limit, not_found, network, unknown}`. `attempts`
is the number of tries before giving up — always 1 for `not_found`,
up to 3 for `rate_limit`/`network`. See SKILL.md cross-cutting caveats
for retry semantics. Surface the per-ticker error and report the rest —
don't fail the whole batch.

## Presenting fast_info results

Compact markdown table for 2+ tickers:

| Symbol | Last | Prev Close | Change % | Day Range | Volume | Market Cap |
|---|---|---|---|---|---|---|

For a single ticker, a one-sentence summary is friendlier:
> AAPL is at \$191.07, up 1.84% on the day (range \$187.50–\$191.20, volume 41.2M).

Always include the currency if it isn't USD, and round prices sensibly
(2 decimals for most equities, more for low-priced or FX-like symbols).
Escape `$` as `\$` in prose — see SKILL.md "Cross-cutting caveats" for why.

## Mode-specific caveats

- **Delayed data.** `fast_info` quotes are ~15 min delayed for US equities
  and more for many non-US markets — don't claim real-time.
  [`history`](history.md) daily bars don't have this issue: they're
  final once the session closes.
