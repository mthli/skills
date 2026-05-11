[← back to SKILL.md](../SKILL.md)

# `fast_info` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run `scripts/smoke.py`
if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting fast_info results](#presenting-fast_info-results) · [Mode-specific caveats](#mode-specific-caveats) (incl. **delayed-data** caveat, **ISIN lookup** caveat)

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

# --with-isin — also pull the ISIN per ticker (slow, ~1-5s extra per ticker)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/fast_info.py --with-isin AAPL SPY 0700.HK
```

Tickers are positional args.

## CLI arguments

- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`
  emits a single header row plus one row per ticker; columns are
  `symbol` + the raw FIELDS list + `change_abs` + `change_pct` +
  `error` + `error_kind` + `attempts` (the trailing three are blank
  for successful rows). When `--with-isin` is set, three more columns
  are appended: `isin`, `isin_error_kind`, `isin_attempts`.

- `--with-isin` — opt-in ISIN lookup, off by default. Adds an `isin`
  field (string or `null`) to each output record. See the **ISIN
  lookup** caveat below for cost (~1-5 s extra per ticker — 5-10× the
  base fast_info cost) and hit-rate gotchas — null does NOT mean "no
  ISIN exists", it means "yfinance's experimental lookup did not find
  a match, or returned a non-conforming string we nullified
  defensively." Failed lookups (network / rate-limit, not "no match")
  surface an `isin_error_kind` field alongside the null, and the
  optional `isin_attempts` field carries the retry count (always
  present on failure; present on success only when > 1, mirroring the
  main `attempts` convention).

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

With `--with-isin`, successful records gain an `isin` field:

```json
{
  "symbol": "AAPL",
  "last_price": 191.07,
  ...
  "isin": "US0378331005"
}
```

A `null` `isin` means the lookup completed without finding a valid
identifier — either yfinance returned its `'-'` sentinel (including
the instant short-circuit on `-` / `^` tickers), or it returned a
string that failed the canonical ISO 6166 shape check. A null PLUS
an `isin_error_kind` is a different story: the lookup itself failed
(network / rate limit) — the price snapshot is still valid in that
case:

```json
{
  "symbol": "AAPL",
  "last_price": 191.07,
  ...
  "isin": null,
  "isin_error_kind": "rate_limit",
  "isin_attempts": 2
}
```

The **presence** of the `isin` key (even when null) is itself a
signal: present = lookup ran; **absent** = lookup was skipped because
the main fast_info call failed (don't waste 1-5 s of network on an
already-failing row). This mirrors the existing convention for
`change_abs` / `change_pct` — both also only appear on the success
path.

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

- **ISIN lookup (`--with-isin`).** Off by default; opt-in only because:

  - **Cost.** Each ticker adds ~1-5 s on top of fast_info's ~0.3-0.5 s
    base (5-10× the base cost). The lookup forces yfinance's heavy
    `quote.info` fetch (~2 HTTP to Yahoo: `quoteSummary` + `quote`
    endpoints) to pull `shortName`, then queries an external service
    (`markets.businessinsider.com`, 1 more HTTP) to map name → ISIN.
    ~3 external HTTP per ticker. For a batch of 10 tickers, expect
    15-50 s of extra wall time.
  - **Hit rate.** Marked `*** experimental ***` upstream. Empirically
    spotty even on liquid names — in a 2026-05 spot check, 3 of 6
    resolved (AAPL ✓, SPY ✓, 0700.HK (Tencent) ✓; MSFT ✗, BMW.DE ✗,
    TM (Toyota ADR) ✗ — all six have well-known real ISINs). A
    `null` in the output means "no match from this specific
    name-search", **not** "no ISIN exists" — verify with another
    source before claiming a security lacks an ISIN.
  - **Instant short-circuit for `-` / `^`.** Tickers containing `-`
    (crypto like `BTC-USD`, class shares like `BRK-B`) or starting
    with `^` (indexes like `^GSPC`) are zero-cost: yfinance returns
    `'-'` without hitting the network. Real ISINs for any dash-
    tickered share (e.g. `BRK-B`, `RDS-A`) won't resolve via this
    path — use a different data source.
  - **Format validation.** The returned string is checked against the
    canonical ISO 6166 shape `^[A-Z]{2}[A-Z0-9]{9}\d$` (2 country code
    + 9 alphanumeric + 1 check digit). Anything that doesn't match —
    including yfinance's `'-'` sentinel, `None`, empty strings, and
    any future non-ISIN encoding upstream might introduce — gets
    nullified. Better to under-resolve than leak garbage as a fake
    identifier. If yfinance changes its sentinel encoding (currently
    `'-'`), the null behavior stays correct because anything failing
    the shape check is nullified — but you won't get an explicit
    signal that the sentinel changed.
  - **Retry budget is 2 attempts** (vs 3 on the main fast_info path).
    The ISIN call's high base cost (1-5 s) means a full retry-with-
    backoff stack on a flaky rate_limit could push worst-case wall
    time to 10-16 s per ticker; a single retry is the compromise.
    `isin_attempts` surfaces the retry count: always present on
    failure (mirrors RESULT_META convention), present on success only
    when > 1 (keeps happy path clean).
  - **Error encoding.** "No match" / "format failed" is `isin: null`
    with no `isin_error_kind` field. Network / rate-limit failures
    surface `isin: null` PLUS `isin_error_kind:
    <rate_limit|network|...>` so callers can distinguish "yfinance
    tried and failed" from "yfinance ran but didn't find anything".
    The main `error` / `error_kind` fields are reserved for fast_info
    itself — ISIN failures never poison the row (the price data is
    still emitted).
  - **Skipped on main-fetch failure → shape signal.** If the
    fast_info call itself errored (rate-limit / not_found / network),
    the ISIN lookup is skipped — it'd almost certainly hit the same
    failure and double the wall time on already-failing rows. Failed
    rows simply omit the `isin` field entirely. **The presence /
    absence of the `isin` key itself is a signal**: present (even
    when null) = lookup ran; absent = lookup was skipped. This
    mirrors the existing convention for `change_abs` / `change_pct`,
    which also only appear on the success path. In CSV mode the
    column is always emitted (blank for skipped rows) so tabular
    consumers see a stable schema.
