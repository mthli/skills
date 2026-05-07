---
name: yfinance
description: >
  Fetch Yahoo Finance market data via the yfinance library. Use for stock,
  ETF, mutual fund, index, crypto, futures, or FX questions — current
  quotes, historical OHLCV (daily / intraday), and company / fund
  fundamentals (P/E, dividend yield, sector, analyst targets, AUM, etc.).
  Triggers include "current price", "YTD performance", "N-year chart",
  "P/E ratio", "compare/rank tickers", "what does X do", and non-US
  tickers like 0700.HK or BMW.DE. See SKILL.md for the per-mode router.
---

# yfinance

Python scripts wrapping [yfinance](https://github.com/ranaroussi/yfinance),
one per mode — currently `scripts/fast_info.py` (current quote),
`scripts/history.py` (historical OHLCV; full bars or `--summary`
aggregates), `scripts/info.py` (profile + fundamentals + analyst; full
grouped sections or `--summary` flat dict), and `scripts/earnings.py`
(upcoming + recent earnings dates with EPS estimates / actuals / surprise).
Shared NaN/Inf-safe converters live in `scripts/helpers.py`. A
`scripts/smoke.py` test exercises all four wrappers against representative
tickers — run after editing schema or when yfinance API drift is suspected:
`uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/smoke.py`
(the `lxml` extra is needed only by `earnings.py`'s HTML scrape; harmless
for the others). All scripts use `argparse` (run with `--help` for inline
usage) and run under `uv` so yfinance installs into a managed temp env, no
side-effects on the user's system Python.

This SKILL.md is the router: pick a mode below, copy the invocation, then
**Read the matching `references/<mode>.md` for the field schema, full CLI flags,
output examples, presentation guidance, and mode-specific caveats**. The
reference files are loaded only when needed so the entry doc stays small
as functionality grows.

## When to use which mode

> **Quote-type precondition:** `fast_info` and `history` work for any
> ticker (stocks, ETFs, indexes, crypto, futures, FX). `info` is
> meaningful only for `quote_type` ∈ {`EQUITY`, `ETF`, `MUTUALFUND`} —
> for indexes / crypto / futures / FX it returns mostly null, so don't
> waste the call. `earnings` is **equity-only** (no quarterly EPS for
> ETFs / indexes / crypto); non-equities short-circuit to an empty
> list with a `note`. Both `fast_info` and `info` return `quote_type`
> explicitly when you're unsure.

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

> "S&P 500 P/E" or any index-fundamental question isn't answerable through `info` (or the other modes) — yfinance has no fundamentals for indexes; you'd need a different data source.

| Question shape | Mode | Why |
|---|---|---|
| "P/E", "P/B", "PEG" | `info` | `valuation` section |
| "what does X do", "sector", "industry" | `info` | `profile` section |
| "profit margin", "ROE", "revenue growth", "EPS" | `info` | `fundamentals` section |
| "dividend yield", "payout ratio", "ex-div date" | `info` | `dividend` section (mind the unit caveat) |
| "analyst target price", "buy/sell rating" | `info` | `analyst` section |
| "ETF expense ratio", "AUM", "fund category" | `info` | `fund` section (ETFs / mutual funds only) |
| "compare 3+ tickers", "peer screen", "rank by P/E / yield" | `info --summary` | flat per-ticker dict (~10× smaller than default) for table rendering |
| "when does X report next", "next earnings date" | `earnings --summary` | `next_date` is the upcoming-quarter datetime (preserves AMC/BMO timing) |
| "did X beat last quarter", "earnings surprise" | `earnings --summary` | `last_surprise_pct` (percent; positive = beat) |
| "X's earnings history", "EPS trend over last N quarters" | `earnings --past-only` | full reported quarters with estimate/actual/surprise |
| "which of these consistently beats" | `earnings --summary` | `beat_rate_last_4` across multiple tickers |

A single user request can need multiple modes. "What's AAPL trading at, how
much is it up YTD, and what's its P/E?" → `fast_info` for the live price,
`history --period ytd --summary` for the YTD return, `info` for the P/E.

**Catch-all rule.** If the user is open-ended ("tell me about NVDA",
"what's up with TSLA?") and you can't pin them to a row above, default
to `fast_info` first — it's the cheapest and answers "where is it
trading, how big is it, what's the recent range" in one call. Add `info`
only if the user follows up about sector / fundamentals / analyst views,
or if the original question already mentions those.

## Invocations

Pick the line you need, then Read the corresponding `references/<mode>.md` for
flags, schema, and caveats.

```bash
# fast_info — current quote (see references/fast_info.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py AAPL MSFT TSLA

# history — historical OHLCV (see references/history.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py AAPL                              # 1mo daily, full rows
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period ytd --summary AAPL       # aggregate stats
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 5d --interval 1h AAPL    # intraday
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/history.py --period 1d --interval 5m --prepost AAPL  # extended-hours

# info — profile + fundamentals + analyst (see references/info.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL MSFT                            # full sections
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py --summary AAPL MSFT GOOGL            # peer comparison

# earnings — upcoming + recent earnings dates (see references/earnings.md)
# NOTE: earnings.py needs an extra `--with 'lxml'` (the others don't).
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py AAPL                # 12 rows default
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary AAPL MSFT NVDA  # peer beat-rate
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --future-only AAPL  # only upcoming
```

`<SKILL_DIR>` is the absolute path of the directory containing this
SKILL.md. Substitute it once when running.

## Cost / latency

_Last measured: 2026-05 (US connection, US daytime). Re-measure if numbers
feel off — yfinance/Yahoo backends drift. Run
`time uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL`
and subtract ~0.5s of uv startup for the network-only delta._

| Mode | Latency | Why |
|---|---|---|
| `fast_info` | ~0.3–0.5 s | one Yahoo call, small payload |
| `history` (≤1y daily, 1 ticker) | ~0.5–1.5 s | one call, ~252 rows for 1y |
| `history` (≤1y daily, N tickers) | ~0.7 s for 5, ~1.5–2.5 s for 10 | yf.download batches N≥2 in one request, threaded |
| `history` (max / 5y intraday) | ~2–4 s | larger payload |
| `info` | ~1–3 s | multiple internal modules — as of yfinance 1.3.x: `financialData`, `quoteType`, `defaultKeyStatistics`, `assetProfile`, `summaryDetail` |
| `earnings` (equity) | ~1.5–2.5 s | quote_type pre-check (~0.3s) + HTML scrape (~1–2s) |
| `earnings` (non-equity) | ~0.3–0.5 s | quote_type pre-check only; scrape skipped via short-circuit |

`fast_info`, `info`, and `earnings` are still serial — total ≈ N × per-ticker
cost. A 10-ticker `info` batch is ~15–30 s and is the most likely path to
trigger Yahoo's empty-response / 429 rate-limit. `history` is the
exception: N ≥ 2 routes through `yf.download` (one HTTP request, threaded),
so 5–10 tickers cost ~1–2 s total instead of ~5–15 s. When a question is
answerable by multiple modes, pick the cheapest. Don't call `info` if
`fast_info` already has the field you need (e.g., `market_cap` is in both).

A retried call adds backoff (~0.5–1.5 s per retry, 3 attempts max).
`fast_info` retry is the worst case: each retry replays all field reads
from scratch (the first read in a session is ~3 s; subsequent ones
~150 ms cached), so a single retried `fast_info` call can total ~5–7 s
instead of the nominal 0.3–0.5 s. Watch the `attempts` field in the
response — it appears whenever a call retried.

`--summary` modes (`history --summary` and `info --summary`) **don't reduce
latency** — they're post-fetch projections of the same payload, so network
cost is identical to the default mode. Only the output JSON shrinks (~10×
for `info --summary`, more for `history --summary` when the period is long).
Use `--summary` to save context tokens, not to save time.

## Setup

**Python 3.9+ required** (the helpers use PEP 604 unions and lowercase
`tuple[]` subscripts). `uv run` picks a recent Python automatically; if
you bypass `uv` and run scripts directly under an older interpreter,
helpers.py will refuse to import with a clear message.

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

**`earnings.py` needs an extra `--with 'lxml'`** — yfinance scrapes earnings
from Yahoo's HTML calendar via `pandas.read_html`, which requires `lxml`.
yfinance documents the requirement in a comment but doesn't pin it as a hard
dependency, so without `--with 'lxml'` every earnings fetch fails with
`error_kind: unknown` and a misleading "Missing optional dependency 'lxml'"
log line. The other three modes (`fast_info`, `history`, `info`) hit Yahoo's
JSON API and don't need it. `smoke.py` also needs lxml because it imports
`earnings`. Use `--with 'yfinance>=1.3,<2' --with 'lxml'` for those two.

## Cross-cutting caveats

These apply to all four modes. Mode-specific caveats live in the
matching `references/<mode>.md`. Grouped into three concerns:

### Data formats (interpreting the numbers)

- **Numeric units are not consistent across — or within — modes.** Don't
  guess a number's unit from the field name. Quick reference:
  - `fast_info.change_pct` and `history --summary.change_pct` → **percent**
    (`16.43` means 16.43%).
  - `info` margins / growth / returns / payout ratios → **fractions**
    (`0.272` means 27.2%, multiply ×100 for display).
  - `info` yield-and-fund-return fields are a **mix** (full table in
    references/info.md "Unit landmines"). Percent-encoded:
    `dividend.five_year_avg_dividend_yield`, `fund.ytd_return`.
    Fraction-encoded: `dividend.trailing_annual_dividend_yield`,
    `fund.three_year_avg_return`, `fund.five_year_avg_return` (CAGR).
    (Yahoo's percent-encoded `dividend_yield` was the worst offender —
    we drop it from the schema entirely; use `trailing_annual_dividend_yield`.)

  Sanity heuristic: any yield > 1.0 is the percent variant; any 3y/5y
  avg-return < 0.5 is the fraction (CAGR) variant. When in doubt, prefer
  `trailing_annual_dividend_yield` (always fraction) or compute the yield
  yourself from `dividend_rate / current_price`.
- **DST.** ET (`America/New_York`) is DST-aware. yfinance ISO timestamps
  carry the correct UTC offset, so data is fine. Only worry about DST when
  translating ET to a user's local timezone in prose (e.g., 09:30 ET =
  21:30 Beijing summer / 22:30 winter).
- **Escape `$` as `\$` in prose.** Many markdown renderers (including
  Claude Code's) treat `$...$` as a math-mode delimiter and will swallow
  the digits between two unescaped dollar signs (e.g. `$237.30` may render
  as `.30`). Always write `\$237.30` instead.

### Identifiers (tickers and exchanges)

- **Ticker suffixes for non-US markets.** Hong Kong: `0700.HK`. Shenzhen:
  `000001.SZ`. Shanghai: `600519.SS`. London: `BARC.L`. Tokyo: `7203.T`.
  Korea: `005930.KS`. Frankfurt: `BMW.DE`. If a user gives a bare HK/CN
  ticker, ask or guess the suffix.
- **`exchange` codes are short Yahoo identifiers, not human names.** Decode
  before showing to the user — render "AAPL (Nasdaq)" not "AAPL (NMS)":

  | Code | Exchange | Code | Exchange |
  |---|---|---|---|
  | `NMS` | Nasdaq | `HKG` | HKEX (Hong Kong) |
  | `NYQ` | NYSE | `SHH` | Shanghai |
  | `ASE` | NYSE American | `SHZ` | Shenzhen |
  | `PCX` | NYSE Arca (most ETFs) | `TYO` / `JPX` | Tokyo |
  | `BATS` | Cboe BZX | `KSC` | KOSPI / KRX |
  | `TOR` | TSX (Toronto) | `LSE` | London |
  | `ASX` | ASX (Australia) | `GER` | Xetra (Frankfurt) |
  | `NSI` | NSE (India) | `EBS` | SIX Swiss |
  | `BOM` | BSE (India) | `MIL` | Borsa Italiana |

  Don't infer exchange from the ticker suffix — both `fast_info` and `info`
  return it explicitly. `0700.HK` → `HKG`, `BMW.DE` → `GER`, `7203.T` → `JPX`.

### Calling conventions (errors, retries, output)

- **Rate limits & retry.** Yahoo will start returning empty / 429 if you
  hammer it. Each script wraps its Yahoo call with exponential backoff
  + jitter (3 attempts, base ~0.5s) on `rate_limit` and `network`
  failures, but won't retry `not_found` (delisted / wrong suffix). For
  `fast_info` / `info` / `earnings` (which loop per ticker), querying
  many tickers means N independent Yahoo calls — call in smaller groups
  (~5) and pause between calls; retry helps with transient 429s, not
  sustained throttling. **`history` is the exception**: N≥2 routes
  through `yf.download` (one batched request, threaded internally), so
  a single call of 5–10 tickers is normal and triggers fewer 429s than
  the equivalent serial loop.
- **`error_kind` and `attempts` on results.** Failed tickers carry
  `error`, `error_kind` ∈ {`rate_limit`, `not_found`, `network`,
  `unknown`}, and `attempts` (how many tries before giving up). Use
  `error_kind` to decide retry at the request level: `rate_limit` ⇒
  wait & try later; `not_found` ⇒ ticker is bad, don't bother;
  `network`/`unknown` ⇒ one more try but escalate if persistent.
  Successful results also include `attempts` *only when > 1* — useful
  for spotting tickers that took a retry to succeed.
- **Output formats.** Every script accepts `--format json|ndjson|csv`.
  Default `json` is pretty-printed for human reading; `ndjson` (one
  JSON object per line) is friendliest for streaming/parse-by-line;
  `csv` is the most compact but only works on flat outputs (`fast_info`,
  `history` rows or summary, `info --summary`). `info` default mode
  refuses `--format csv` because the nested sections don't flatten.
  CSV output uses `\n` line endings (not `\r\n`) so Unix tools work.
- **Unofficial.** yfinance is unaffiliated with Yahoo and its endpoints
  can break at any time. If a script returns nothing for a ticker that
  should exist, the upstream API may have changed — don't keep retrying.
