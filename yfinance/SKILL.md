---
name: yfinance
description: >
  Fetch stock / ETF / index data from Yahoo Finance via the yfinance library.
  Two modes: current quote (price, market cap, 52-week range) via `fast_info`,
  and historical OHLCV time series (daily or intraday, with period high/low,
  dividends, splits) via `Ticker.history`. Use whenever the user asks about a
  ticker — "what's AAPL trading at", "TSLA YTD performance", "MSFT 5-year
  chart", "show me 0700.HK last 10 days". Do NOT use for financials, options
  chains, holders, or order-book data — write yfinance code directly for those.
---

# yfinance

Thin Python wrappers around [yfinance](https://github.com/ranaroussi/yfinance)
in `scripts/fast_info.py` (current quote) and `scripts/history.py` (historical
OHLCV). Both run under `uv` so yfinance installs into a managed temp env, no
side-effects on the user's system Python.

## When to use which mode

| Question shape | Mode | Why |
|---|---|---|
| "what's X trading at", "quote X", "current price" | `fast_info` | latest snapshot |
| "market cap", "shares outstanding", "currency" | `fast_info` | static fields |
| "52-week high/low", "200-day MA" | `fast_info` | fixed windows yfinance precomputes |
| "today's change", "X up or down today" | `fast_info` | `change_pct` is today vs prev close |
| "YTD performance", "1-year return", "how much did X gain" | `history --summary` | period-bounded, gives `change_pct` over chosen window |
| "period high/low and when it happened" | `history --summary` | `period_high_date` / `period_low_date` |
| "dividends paid last quarter / year" | `history --summary` | `total_dividends` over period |
| "show me last N days / weeks", "plot the chart" | `history` (default) | full OHLCV rows |
| "intraday last 5 days" | `history --interval 1h` (or `5m`/`15m`) | tick-level rows |
| "after-hours price right now", "pre-market gap after earnings" | `history --interval 5m --prepost` | extended-hours bars |

A single user request can need both. "What's AAPL trading at and how much is
it up YTD?" → call `fast_info` for the live price, then `history --period ytd
--summary` for the YTD return. (Names are shorthand; the actual invocations
are the full `uv run --with ...` lines below.)

## Setup

```bash
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
```

`uv` is Astral's Rust-based Python package manager. The official installer
drops the binary in `~/.local/bin`, which may not yet be on `PATH` in the
current shell — the `export` line covers that case.

`uv run --with 'yfinance>=1.3,<2'` resolves and caches yfinance in an
ephemeral env:
- First call on a fresh machine takes ~5–15 s while uv downloads wheels
- Subsequent calls are nearly instant (uv reuses the cache)
- No `pip install` side-effects on the user's global Python
- The version pin guards against yfinance's not-infrequent breaking changes;
  bump the upper bound deliberately, not by accident

`<SKILL_DIR>` in the example commands below is a placeholder for the absolute
path of this skill's directory (the directory containing this `SKILL.md`).
Substitute it once when running.

## `fast_info` — current quote

### Run

```bash
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA
```

Tickers are positional args. Output is a JSON array, one entry per ticker.

### Output schema

```json
[
  {
    "symbol": "AAPL",
    "last_price": 189.45,
    "previous_close": 187.62,
    "open": 188.01,
    "day_high": 190.20,
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
    "change_abs": 1.83,
    "change_pct": 0.9754
  }
]
```

`change_pct` is today vs `previous_close` (percent, not fraction); `change_abs`
is the dollar change (in whatever currency the quote is in). Any field Yahoo
did not return is `null`. Prices are raw floats — round for display.

A failed ticker looks like:

```json
{ "symbol": "ZZZZNOTREAL", "error": "no quote returned (delisted, wrong suffix, or rate-limited)" }
```

Surface the per-ticker error and report the rest, don't fail the whole batch.

### Presenting fast_info results

Compact markdown table for 2+ tickers:

| Symbol | Last | Prev Close | Change % | Day Range | Volume | Market Cap |
|---|---|---|---|---|---|---|

For a single ticker, a one-sentence summary is friendlier:
> AAPL is at \$189.45, up 0.98% on the day (range \$187.50–\$190.20, volume 41.2M).

Always include the currency if it isn't USD, and round prices sensibly
(2 decimals for most equities, more for low-priced or FX-like symbols).
Escape `$` as `\$` in prose — many markdown renderers (including Claude Code's)
treat `$...$` as a math-mode delimiter and will swallow the digits between two
unescaped dollar signs (e.g. `$237.30` may render as `.30`).

## `history` — historical OHLCV

### Run

```bash
# Default: 1mo of daily bars, full rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py AAPL

# Custom period + interval
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1y AAPL MSFT

# Summary mode — aggregate stats, no rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period ytd --summary AAPL

# Intraday
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5d --interval 1h AAPL

# Intraday including pre-market + after-hours bars
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1d --interval 5m --prepost AAPL
```

### CLI arguments

- `--period` — yfinance period string. Default `1mo`. Valid: `1d`, `5d`,
  `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max`.
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

### Output — default mode (full rows)

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

Daily+ bars use `YYYY-MM-DD` dates in the market's local timezone (the
top-level `timezone` field — e.g. `America/New_York` for NYSE / Nasdaq,
`Asia/Hong_Kong` for HK). So `2026-05-07` for `0700.HK` is a different actual
moment than `2026-05-07` for `AAPL`. Intraday uses ISO timestamps with the
offset baked in.

**Closes are split- and dividend-adjusted** (yfinance default
`auto_adjust=True`) — what you want for return calculations, not what shows
up on a print newspaper page. If a stock split since you last looked, every
historical close in the response will have moved.

`dividends` is non-zero only on ex-dividend days; `split_ratio` is non-zero
only on split days (e.g. 4.0 for a 4-for-1). Prices are raw floats — round
for display, not for storage.

A failed ticker looks like:

```json
{ "symbol": "ZZZZNOTREAL", "error": "no data returned (delisted, wrong suffix, or rate-limited)" }
```

### Output — `--summary` mode

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
`{"date", "ratio"}` objects (e.g. a 4-for-1 split has ratio 4.0). Empty list
is normal — most tickers don't split during a given period.

Note: `--summary` works with intraday intervals too. The semantics don't
change — `start_close` / `end_close` are still first/last bar closes,
`period_high` / `period_low` still scan the window. The only visible
difference is that the date fields become ISO timestamps instead of
`YYYY-MM-DD`. `total_dividends` and `splits` will usually be `0` / `[]`
because corporate actions don't fire mid-session.

### When to use `--summary`

Reach for `--summary` when the user asks "how much did X change", "what was
the high last year", or any other question that boils down to a few numbers
over a window. Full rows for a 1y daily series are ~252 rows × 8 fields ≈
2k data points per ticker; for 5y it's ≈ 10k. Use full rows when the user
actually wants to see / plot / compare individual bars (typically short
windows, or an explicit "show me the last N days").

### Presenting history results

Default rows mode → compact markdown table:

| Date | Open | High | Low | Close | Volume |
|---|---|---|---|---|---|

Trim to the most recent ~10 rows in chat unless the user asked for more, and
note the period and interval in the heading.

`--summary` mode → one-sentence summary:
> AAPL is up 16.43% over the past year, from \$184.92 (2025-05-08) to \$215.30 (2026-05-07). Period high \$237.49 on 2026-01-15; low \$169.21 on 2025-08-05. Avg volume 52.3M, \$0.96 paid in dividends.

## Caveats to surface when relevant

- **Delayed data (fast_info only).** `fast_info` quotes are ~15 min delayed
  for US equities and more for many non-US markets — don't claim real-time.
  `history` daily bars don't have this issue: they're final once the session
  closes.
- **DST and "ET" labels.** ET / `America/New_York` is DST-aware, so US market
  wall-clock hours are stable year-round (regular 09:30–16:00 ET, pre-market
  04:00–09:30 ET, after-hours 16:00–20:00 ET). What shifts is the UTC offset:
  EST (UTC-5) Nov–Mar, EDT (UTC-4) Mar–Nov. yfinance ISO timestamps include
  the correct offset automatically — no conversion needed for data. **You
  only need to think about DST when translating ET to a user's local
  timezone in prose** (e.g. 09:30 ET = 21:30 Beijing in summer, 22:30 in
  winter). Check the current ET offset before quoting a local equivalent.
- **Ticker suffixes for non-US markets.** Hong Kong: `0700.HK`. Shenzhen:
  `000001.SZ`. Shanghai: `600519.SS`. London: `BARC.L`. Tokyo: `7203.T`.
  Korea: `005930.KS`. Frankfurt: `BMW.DE`. If a user gives a bare HK/CN
  ticker, ask or guess the suffix.
- **Intraday windows are capped.** `1m` only goes back ~7 days; other sub-hour
  intervals ~60 days; `1h` up to ~730 days. Asking for longer + intraday
  silently returns less data than requested.
- **Rate limits.** Yahoo will start returning empty / 429 if you hammer it.
  The scripts don't throttle — if you're querying many tickers, call in
  smaller groups and pause between calls.
- **Unofficial.** The library is unaffiliated with Yahoo and its endpoints
  can break at any time. If a script returns nothing for a ticker that should
  exist, the upstream API may have changed — don't keep retrying.
