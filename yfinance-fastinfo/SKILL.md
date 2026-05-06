---
name: yfinance-fastinfo
description: >
  Fetch a current stock quote (price, day range, volume, market cap, 52-week
  high/low, etc.) from Yahoo Finance via yfinance's fast_info API. Use whenever
  the user asks for the latest price or a "quote" of one or more tickers —
  e.g. "what's AAPL trading at", "quote TSLA and NVDA", "current price of
  0700.HK", "market cap of MSFT". Do NOT use for historical OHLCV, financials,
  options chains, or anything beyond a single snapshot — write yfinance code
  directly for those.
---

# yfinance fast_info

A thin wrapper around `yfinance.Ticker(...).fast_info` that returns a JSON
snapshot of the latest quote for one or more tickers. Built for the common
"what's X trading at right now" question.

## When to use this skill

Use it when the user wants any of:
- Last / current price of a stock
- Previous close, open, day high, day low
- Day's volume, market cap, shares outstanding
- 50- / 200-day moving averages, 52-week high/low
- A side-by-side quote of multiple symbols

Do **not** use it for:
- Historical price series → write yfinance code with `Ticker.history(...)` directly
- Financials, holders, options, dividends, splits → use the relevant yfinance API directly
- Real-time / sub-second data → yfinance is delayed (~15 min for US, more for others)

## How it works

The skill ships a script at `scripts/fast_info.py`. It is invoked through `uv`
so yfinance and its (heavy) dependencies install into a managed temp env
without touching the user's system Python.

### Step 1 — Make sure `uv` is available

```bash
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
```

`uv` is Astral's Rust-based Python package manager. The official installer
drops the binary in `~/.local/bin`, which may not yet be on `PATH` in the
current shell — the `export` line covers that case.

### Step 2 — Run the script

Pass the tickers as positional CLI args. Replace `<SKILL_DIR>` with the absolute
path to this skill's directory (the same directory that contains this
`SKILL.md`):

```bash
uv run --with yfinance python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA
```

`uv run --with yfinance` resolves and caches yfinance in an ephemeral env,
which means:
- First call on a fresh machine takes ~5–15 s while uv downloads wheels
- Subsequent calls are nearly instant (uv reuses the cache)
- No `pip install` side-effects on the user's global Python

### Step 3 — Parse and present

The script prints a JSON array to stdout. Read it, then format for the user.

## Output schema

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
    "change_pct": 0.9754
  }
]
```

`change_pct` is computed by the script from `last_price` and `previous_close`
(percent, not fraction). Any field Yahoo did not return comes back as `null`.

If a ticker is invalid, delisted, or rate-limited, that entry has an `"error"`
field instead of price fields. **Don't fail the whole batch on one bad
ticker** — surface the per-ticker error and report the rest.

## Presenting results

Default to a compact markdown table when there are 2+ tickers:

| Symbol | Last | Prev Close | Change % | Day Range | Volume | Market Cap |
|---|---|---|---|---|---|---|

For a single ticker, a one-sentence summary is friendlier:
> AAPL is at $189.45, up 0.98% on the day (range $187.50–$190.20, volume 41.2M).

Always include the currency if it isn't USD, and round prices sensibly
(2 decimals for most equities, more for low-priced or FX-like symbols).

## Caveats to surface when relevant

- **Delayed data.** yfinance scrapes Yahoo's public endpoints. US equities are
  ~15 min delayed; many non-US markets more. Don't claim it's real-time.
- **Ticker suffixes for non-US markets.** Hong Kong: `0700.HK`. Shenzhen: `000001.SZ`.
  Shanghai: `600519.SS`. London: `BARC.L`. Tokyo: `7203.T`. Korea: `005930.KS`.
  Frankfurt: `BMW.DE`. If a user gives a bare HK/CN ticker, ask or guess the suffix.
- **Rate limits.** Yahoo will start returning empty / 429 if you hammer it. For
  more than ~20 symbols in one go, batch in groups and sleep a second between
  batches.
- **Unofficial.** The library is unaffiliated with Yahoo and its endpoints can
  break at any time. If `fast_info` returns nothing for a ticker that should
  exist, the upstream API may have changed — don't keep retrying.
