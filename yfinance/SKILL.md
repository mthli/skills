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
grouped sections or `--summary` flat dict), `scripts/earnings.py`
(upcoming + recent earnings dates with EPS estimates / actuals /
surprise), and `scripts/financials.py` (annual / quarterly / TTM income
statement, balance sheet, and cash flow). Shared NaN/Inf-safe converters
live in `scripts/helpers.py`. A `scripts/smoke.py` test exercises all
five wrappers against representative tickers — run after editing schema
or when yfinance API drift is suspected:
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
> waste the call. `earnings` and `financials` are **equity-only** (no
> quarterly EPS or income statement for ETFs / indexes / crypto); both
> short-circuit non-equities to empty lists with a `note`. Both
> `fast_info` and `info` return `quote_type` explicitly when you're
> unsure.

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
| "consensus EPS / revenue forecast", "next quarter estimate", "analyst high / low / # analysts", "FY consensus", "estimate revisions / trend", "stock vs market growth", "long-term growth (LTG)" | `earnings --estimates` | full analyst panel: consensus avg/low/high, 90-day estimate trend, 7d/30d revision counts, broad-market benchmark, LTG. Per-period rows for `0q` / `+1q` / `0y` / `+1y`. ADRs split EPS (USD) and revenue (home ccy) into separate currency fields. See references/earnings.md. |
| "recent IPO with no past reports", "ticker hasn't reported earnings yet" | `earnings --estimates` | **IPO fall-through.** The same flag also rescues IPOs from the default `not_found`: when the calendar scrape returns empty but the analyst panel has data, the response is success with `earnings_dates: []`, `timezone: null`, and a `coverage_note` explaining the empty calendar. Default `earnings` (without `--estimates`) returns `error_kind: not_found` with a hint pointing at this flag. |
| "what's the analyst rating / price target on X" | `info` (analyst section) | `info.analyst.target_mean_price`, `recommendation_key` — complementary to `earnings --estimates` (which has the underlying EPS/revenue forecasts; price target is derived from those) |
| "income statement", "balance sheet", "cash flow", "revenue/FCF trend over N years" | `financials` | per-statement period lists; scope with `--statement income\|balance\|cashflow` |
| "latest quarter", "QoQ revenue", "TTM trailing twelve months" | `financials --period quarterly\|ttm` | ~5–7 most-recent quarters; `ttm` = 1-row rollup (income + cashflow only) |
| "compare 3+ tickers' revenue / FCF / margins / growth" | `financials --summary` | flat per-ticker dict + period-over-period growth (`*_growth_yoy`) |

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
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --estimates AAPL    # + full analyst panel (consensus, trend, revisions, sector, LTG)
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --summary --estimates AAPL MSFT NVDA  # peer compare incl. consensus_* fields
uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/earnings.py --future-only AAPL  # only upcoming

# financials — income / balance / cashflow statements (see references/financials.md)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py AAPL                                 # all 3 statements, annual
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --period quarterly AAPL              # quarterly statements
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --statement income --period ttm AAPL # TTM income only
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --summary AAPL MSFT GOOGL            # peer headline + YoY growth
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
| `earnings --estimates` (equity) | ~+1.5–3 s on top of baseline (total ~3–5.5 s) | five Yahoo property reads on a shared Ticker (`earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions`, `growth_estimates`); equity-only. **Worst case under sustained 429:** each of the 5 sources independently retries up to 3 attempts. `with_retry` sleeps between attempts only — 3 attempts means 2 backoff windows (~0.5 s after attempt 1, ~1.0 s after attempt 2, plus jitter), so per-source max sleep ≈ 2 s. 5 sources × ~2 s = ~10 s of cumulative sleep, plus the 15 actual call attempts, gives a total worst-case of ~10–15 s before failing. Drop batch size to ~3 and pause between calls if you see this pattern. |
| `earnings` (non-equity) | ~0.3–0.5 s | quote_type pre-check only; scrape skipped via short-circuit (and `--estimates` short-circuits too) |
| `financials` (equity, any `--statement` value) | ~2 s | quote_type pre-check (~0.3s) + `info["financialCurrency"]` (~1.5s) + statement fetches; yfinance shares the underlying fundamentals payload, so `--statement <one>` and `--statement all` cost the same |
| `financials` (equity, ADR / cross-listed) | ~3–5 s | same path but `info` round-trip is slower for less-common tickers (verified: TM ~4.8s) |
| `financials` (non-equity) | ~1 s | quote_type pre-check only; financials fetch skipped, no `info` call |
| `financials` (equity, soft-fallback path triggered) | up to +3.5 s | when `info["financialCurrency"]` is unavailable (transient 429 / network / field missing), `_meta` retries via `_trading_currency` with backoff. Worst case (sustained 429 on both `info` and `fast_info`) adds ~1.5–3.5s of retry sleeps to the equity baseline above. Watch for `"trading currency"` substring in `note` to detect this path. |

`fast_info`, `info`, `earnings`, and `financials` are still serial — total
≈ N × per-ticker cost. A 10-ticker `info` or `financials` batch is
~15–30 s and is the most likely path to trigger Yahoo's empty-response /
429 rate-limit (`financials` actually issues an `info` call internally
for reporting-currency lookup, so the cost profile is similar to `info`).
`history` is the exception: N ≥ 2 routes through `yf.download` (one HTTP
request, threaded), so 5–10 tickers cost ~1–2 s total instead of
~5–15 s. When a question is answerable by multiple modes, pick the
cheapest. Don't call `info` if `fast_info` already has the field you
need (e.g., `market_cap` is in both); don't call `financials` for
"what's AAPL's P/E" — that's in `info`.

`financials` cost note: `--statement income` does NOT save latency over
`--statement all` — yfinance shares the underlying fundamentals payload
across the three statement properties, so all three come back from one
call. Use `--statement <one>` to save **context tokens** (smaller JSON
output), not time.

A retried call adds backoff (~0.5–1.5 s per retry, 3 attempts max).
`fast_info` retry is the worst case: each retry replays all field reads
from scratch (the first read in a session is ~3 s; subsequent ones
~150 ms cached), so a single retried `fast_info` call can total ~5–7 s
instead of the nominal 0.3–0.5 s. Watch the `attempts` field in the
response — it appears whenever a call retried.

`--summary` modes (`history --summary`, `info --summary`, `earnings --summary`,
`financials --summary`) **don't reduce latency** — they're post-fetch
projections of the same payload, so network cost is identical to the
default mode. Only the output JSON shrinks (~10× for `info --summary`
and `financials --summary`, more for `history --summary` when the period
is long). Use `--summary` to save context tokens, not to save time.

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
  - `financials --summary.*_growth_yoy` → **fractions** (matches `info`'s
    encoding; `0.064` means 6.4%). Disambiguated from `info.revenue_growth`
    (Yahoo TTM-based) by the `_yoy` suffix — both can co-occur with
    different values, see references/financials.md.
  - `earnings --estimates[*].eps_growth` / `revenue_growth` /
    `index_growth` → **fractions** (matches `info` and `financials
    --summary` conventions; `0.2043` means 20.43% YoY). Inside the same
    `earnings` response the older `earnings_dates.surprise_pct` is still
    **percent** — see references/earnings.md "Mode-specific caveats"
    for the rationale. `index_growth` is **the same number for every
    ticker globally** (verified across US sectors AND HK / Frankfurt /
    Tokyo / KOSPI / London listings) — it's a Yahoo-internal global
    benchmark, not locale- or sector-aware. Don't read it as a HK-listed
    ticker getting Hang Seng growth or as sector-specific.
  - `earnings --estimates` ADR currency split: `eps_currency` and
    `revenue_currency` are separate fields, **not** duplicates. For ADRs
    (TM, PBR) Yahoo reports per-share EPS in the trading currency (USD)
    but revenue in the home reporting currency (JPY, BRL). Always read
    both — don't assume one currency for the row.
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
- **Trading currency vs reporting currency.** `fast_info.currency` and
  `info.currency` return the **trading currency** (the currency you'd
  buy the stock in — USD for AAPL, USD for ADRs like TM/BABA/PBR, HKD for
  0700.HK). `financials.currency` returns the **reporting currency**
  (what the financial statements are denominated in — USD for AAPL but
  JPY for TM, CNY for BABA/0700.HK, BRL for PBR). For most direct-listed
  US/EU equities they match; for ADRs and some cross-border listings
  they don't. When mixing modes, don't assume `fast_info.currency` and
  `financials.currency` agree — check both. See references/financials.md
  for the per-ticker examples.
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
