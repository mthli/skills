[← back to SKILL.md](../SKILL.md)

# `market` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`market.py US` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Per-section schemas](#per-section-schemas) · [`--summary` rollups](#summary-rollups) · [Discovery flags](#discovery-flags) · [Presenting market results](#presenting-market-results) · [Mode-specific caveats](#mode-specific-caveats)

Market-wide pulse: market clock + featured-quote summary across the
8 canonical Yahoo regions:

```
US  GB  ASIA  EUROPE  RATES  COMMODITIES  CURRENCIES  CRYPTOCURRENCIES
```

Two sections per market:

- **clock** — open / close datetimes + status string (`open` / `closed` /
  `pre` / etc.). **Yahoo quirk: always returns the U.S. clock** regardless
  of `market` arg — see [Mode-specific caveats](#mode-specific-caveats).
- **summary** — Yahoo's curated representative quotes for the region.
  Sparse on purpose: US / ASIA = ~6 indexes; CURRENCIES /
  CRYPTOCURRENCIES often = 1 featured pair. Use `screener` /
  `fast_info` for the full universe.

**Discovery axis distinct from `calendars` and `screener`.** Calendars
is event-timeline-bounded (earnings / IPO / splits / economic over a
date window); screener is field-predicate filtering over a quote
universe. `market` is the live pulse: "what indexes are leading
today, what's the macro tape look like." Doesn't take a ticker.

## Run

```bash
# Default: US clock + 6 featured indexes (2 HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py US

# Multi-region (one envelope per market, 2 HTTP each)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  US ASIA EUROPE

# Peer compare across regions (avg/best/worst change_pct rollup)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  --summary US GB ASIA EUROPE RATES COMMODITIES CURRENCIES CRYPTOCURRENCIES

# Discovery: list canonical keys (no HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  --list-markets

# Just the clock (smaller output; 2-HTTP cost unchanged — see caveats)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  US --section clock

# CSV: one row per featured quote across regions
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  US ASIA EUROPE --format csv

# NDJSON: meta line + per-quote lines, record_class discriminator
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  US --format ndjson

# Raw Yahoo passthrough (debug; bypass projection).
# CAUTION: in --full mode `summary` is a DICT keyed by exchange_code
# (Yahoo's raw shape), NOT a list — quote dicts preserve camelCase
# keys (regularMarketPrice, shortName, fullExchangeName) instead of
# the snake_case projection. --limit is a no-op against the dict.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  US --full

# `--summary` + `--limit` is silently nonsense (clipping before averaging
# would yield a 1-row rollup). The wrapper warns on stderr and ignores
# `--limit`; aggregation always runs over the full Yahoo-curated set.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/market.py \
  --summary --limit 2 US     # stderr: "info: --limit is ignored ..."
```

## CLI arguments

```text
markets                 one or more region keys (positional, case-insensitive);
                        omit when using --list-markets

--section SECTION       comma-separated, case-insensitive, or `all`.
                        default: clock,summary
                        Section selection only affects PROJECTION cost —
                        2 HTTP fire regardless (see caveats).

--limit N               cap summary row count per market (default: no cap;
                        Yahoo only returns 1–6 rows per region anyway)

--summary               flat per-market dict for peer compare; identity +
                        clock_status + summary_count + top_* + avg/best/worst
                        change_pct across the region's featured rows

--full                  raw Yahoo passthrough (status dict + summary
                        {code: quote} dict); mutually exclusive with --summary

--list-markets          enumerate the 8 canonical region keys with one-line
                        descriptions (no HTTP); mutually exclusive with
                        positional keys

--format {json,ndjson,csv}
                        json (default) = list of envelope dicts
                        ndjson = one record per line with `record_class`
                          discriminator (`meta` for clock, `quote` per
                          summary row). Under --summary, NDJSON emits
                          one flat dict per market with NO `record_class`
                          field (single record class — adding a constant
                          discriminator would just be noise).
                        csv = same discriminator in tabular form
```

## Output schema

One envelope per market. Default sections: `clock` + `summary`.

```jsonc
{
  "market": "US",
  "clock": {
    "id": "us",
    "name": "U.S. markets",
    "status": "closed",                     // "open" | "closed" | "pre" | ...
    "yfit_market_status": "YFT_MARKET_CLOSED",
    "message": "U.S. markets closed",
    "open":  "2026-05-11T13:30:00+00:00",   // ISO 8601 (UTC offset preserved)
    "close": "2026-05-11T20:00:00+00:00",
    "timezone": "America/New_York",         // IANA tz name
    "tz_short": "EDT",
    "gmt_offset_seconds": -14400,
    "dst": "true"
  },
  "clock_is_us_fallback": true,           // ONLY present (always true) when market != "US"; see Mode-specific caveats
  "summary": [
    {
      "exchange_code": "SNP",            // Yahoo dict key (NOT the symbol)
      "symbol": "^GSPC",
      "short_name": "S&P 500",
      "quote_type": "INDEX",             // INDEX | FUTURE | CURRENCY | CRYPTOCURRENCY
      "full_exchange_name": "SNP",       // human-readable (Yahoo `fullExchangeName`)
      "price": 7398.93,
      "previous_close": 7337.11,
      "change": 61.82,
      "change_pct": 0.84,                // PERCENT (Yahoo's encoding; matches fast_info)
      "regular_market_time": "2026-05-08T20:46:46+00:00",   // ISO from epoch
      "data_delayed_by_minutes": 0,      // 0 = real-time; 10/15/20 = delayed feed. Per-quote (US has ^GSPC at 0 but ^RUT at 15). DISTINCT from yfinance's SQLite response cache (separate staleness source).
      "market_state": "CLOSED",          // CLOSED | REGULAR | PRE | POST
      "listing_region": "US",            // Yahoo's listing-region tag — NOT the home market of the underlying. ^N225 / ^HSI both return "US" because Yahoo treats them as US-listed quote feeds. Use `exchange_timezone` for the actual home market.
      "exchange_timezone": "America/New_York"
    }
    // ... more rows
  ],
  "summary_count": 6,
  "sections_returned": ["clock", "summary"]
}
```

Errored envelope (rate limit / network):

```jsonc
{
  "market": "US",
  "error": "fetch failed (rate_limit, after 3 attempt(s))",
  "error_kind": "rate_limit",            // rate_limit | not_found | network | unknown
  "attempts": 3
}
```

## Per-section schemas

### `clock`

The full set of fields after projection (see top-level schema). Source:
yfinance `Market.status`, which is parsed from Yahoo's `markettime`
endpoint.

Two fields are dropped from the projection (available via `--full`):

- **`tz`** — `datetime.timezone` object, not JSON-serializable. The IANA
  name is on `timezone` and the abbreviation on `tz_short`, so nothing
  is lost.
- **`yfit_market_id`** — internal Yahoo identifier (`us_market`); not
  useful downstream.

Two fields get type-coerced from yfinance's raw output:

- **`dst`** — Yahoo serializes as `"true"` / `"false"` strings; we coerce
  via `safe_bool` so it's a real bool. `--full` keeps the raw string.
- **`gmt_offset_seconds`** — yfinance's `gmtoffset` field has been
  observed as integer seconds (`-14400` = -4h EDT). We auto-correct if
  the magnitude exceeds ±86400 (= ±1 day in seconds, larger than any
  real tz offset of ±14h max) by dividing by 1000 to handle a potential
  future ms-encoded variant. If yfinance's encoding flips, the code
  silently does the right thing; if Yahoo invents a third encoding the
  field will be wrong — re-verify with a known offset on schema drift.

### `summary`

A list of projected quote rows (one per featured exchange/quote). Yahoo's
underlying response is `{exchange_code: quote_dict}` — we list-ify it
and lift the dict key into `exchange_code` for shape consistency with
other modes.

The `quote_type` enum matches Yahoo's `quoteType` field globally:

| `quote_type`     | Where it appears                                      |
|------------------|-------------------------------------------------------|
| `INDEX`          | US, GB, ASIA, EUROPE, RATES (`^TYX` 30-Yr yield)      |
| `FUTURE`         | COMMODITIES, US (Gold), RATES (`ZN=F` 10-Yr T-Note)   |
| `CURRENCY`       | CURRENCIES, ASIA (USD/JPY), EUROPE (a CCY pair), GB   |
| `CRYPTOCURRENCY` | CRYPTOCURRENCIES                                      |

Several regions mix `quote_type`s — RATES has 1 INDEX + 1 FUTURE; ASIA
has 5 INDEX + 1 CURRENCY; etc. This is why `--summary` aggregates over
the **dominant** quote_type rather than all rows (see [`--summary` rollups](#summary-rollups)).

Per-region featured-quote counts (verified 2026-05):

| Region              | Count | Sample symbols                                                         |
|---------------------|-------|------------------------------------------------------------------------|
| `US`                | 6     | `^GSPC`, `^DJI`, `^IXIC`, `^RUT`, `^VIX`, `GC=F`                       |
| `GB`                | 3     | `^FTAI` (FTSE AIM All-Share), sparse — UK coverage mostly in `EUROPE`  |
| `ASIA`              | 6     | `000001.SS`, `^N225`, `^HSI`, `^AXJO`, `^ADOW`, `JPY=X`                |
| `EUROPE`            | 4     | `^FTSE`, plus DE / FR / a CCY pair (varies)                            |
| `RATES`             | 2     | `^TYX` (30-Yr Bond, INDEX) + `ZN=F` (10-Yr T-Note Futures, FUTURE)     |
| `COMMODITIES`       | 2     | `BZ=F` (Brent), `HG=F` (Copper) — both FUTURE                          |
| `CURRENCIES`        | 1     | One featured pair (e.g. `MXN=X`); rotates                              |
| `CRYPTOCURRENCIES`  | 1     | One featured pair (e.g. `SOL-USD`); rotates                            |

Yahoo decides which quotes to feature; the list is curated, not exhaustive.
For the full FX / crypto universe use `screener --quote-type` or
`fast_info` directly. **Note: `^FTSE` (FTSE 100) lives in `EUROPE`,
not `GB`** — the GB region is a sparse residual feed.

Two fields get **renamed / dropped** from yfinance's raw quote dict:

- **`region` → `listing_region`** (renamed). The original name was
  misleading: Yahoo tags every row `"region": "US"` regardless of the
  underlying instrument's home market — `^N225` (Tokyo), `^HSI` (Hong
  Kong), and `^AXJO` (Sydney) all come back as `"US"` because Yahoo
  treats them as US-listed quote feeds. Renamed to `listing_region`
  so the field name reflects the actual semantics. Read
  `exchange_timezone` for the underlying instrument's home market.
- **`exchange` dropped**. In observed data it always equals
  `exchange_code` (the dict key), so it's redundant. The distinct
  human-readable name (`"Shanghai"` vs key `"SHH"`) lives in
  `full_exchange_name`. `--full` preserves both.

## `--summary` rollups

Flat per-market dict for cross-region peer compare:

```jsonc
{
  "market": "US",
  "clock_status":      "closed",
  "clock_open":        "2026-05-11T13:30:00+00:00",
  "clock_close":       "2026-05-11T20:00:00+00:00",
  "clock_timezone":    "America/New_York",
  "summary_count":     6,                   // total rows in the curated summary
  "top_symbol":        "^GSPC",             // summary[0] regardless of quote_type
  "top_short_name":    "S&P 500",
  "top_quote_type":    "INDEX",
  "top_price":         7398.93,
  "top_change_pct":    0.84,                // PERCENT (matches summary row encoding)
  "top_market_state":  "CLOSED",
  "avg_change_pct":    0.80,                // mean of summary rows of `avg_quote_type` only
  "best_change_pct":   1.71,                // max within those rows
  "worst_change_pct":  0.02,                // min within those rows
  "avg_quote_type":    "INDEX",             // which quote_type fed the avg
  "avg_rows_used":     5                    // how many rows fed it (≤ summary_count)
}
```

**Avg / best / worst are computed over rows of the dominant `quote_type`**
— NOT over all summary rows. Yahoo's curated summaries mix dimensions
(ASIA = 5 INDEX + 1 CURRENCY; RATES = 1 INDEX + 1 FUTURE), and
averaging an index daily return with an FX move is dimensionally
meaningless. We pick the most-populous `quote_type` in each region and
aggregate within it. `avg_quote_type` echoes which type fed the avg so
the row is self-describing; `avg_rows_used` reports the count.

**Worked example** (US summary has 5 INDEX + 1 FUTURE rows):
- All 6 rows: `[0.84, 0.02, 1.71, 0.76, 0.64, 0.42]` (5 indexes + Gold future)
- INDEX-only (`avg_quote_type=INDEX`, `avg_rows_used=5`): `[0.84, 0.02, 1.71, 0.76, 0.64]`
- `avg_change_pct = 0.80` (mean of the 5 indexes; Gold's 0.42 excluded)
- `best/worst = 1.71 / 0.02` (within the index subset only)

For pure single-type regions (COMMODITIES = 2 FUTURE; CRYPTOCURRENCIES
= 1 CRYPTOCURRENCY; CURRENCIES = 1 CURRENCY) the dominant filter is a
no-op and avg/best/worst cover all rows. `avg_quote_type` reflects
that single type so consumers know what they're comparing.

**`top_*` fields are NOT filtered by quote_type** — they always reflect
`summary[0]`, which is Yahoo's editorial "leading quote" for the region
(typically the broadest index: `^GSPC` for US, `^FTSE` for EUROPE, etc.).
This is intentional — the top quote is what a region's "headline"
would show, even if its quote_type is a minority within the summary.

**`--limit` is ignored when `--summary` is set.** A stderr warning fires
if both flags are passed. Aggregating over a user-truncated subset
would yield a meaningless rollup (`--limit 1 --summary` would give
`avg = best = worst = top_change_pct`). If you need a slimmer output,
pipe through `jq` or set `--format csv` and clip downstream.

## Discovery flags

### `--list-markets` (0 HTTP)

Enumerates the 8 canonical region keys + one-line descriptions. Useful
to remind yourself / the model which keys are valid:

```bash
$ market.py --list-markets
[
  {"key": "US",               "description": "U.S. equity indexes ..."},
  {"key": "GB",               "description": "U.K. — FTSE AIM + ..."},
  ...
]
```

Mutually exclusive with positional keys.

## Presenting market results

- **Currency on prices.** Each summary row's `quote_type` and
  `exchange_timezone` give you the home market — but Yahoo doesn't
  return a per-row `currency` field on this endpoint. For US indexes
  it's USD; for `^N225` it's JPY; for `^HSI` it's HKD. Don't render
  cross-region tables in a single currency without disclosing.
- **Escape `$` as `\$` in prose.** Many markdown renderers treat
  `$...$` as math mode and eat the digits between two unescaped
  dollar signs (e.g. `$237.30` may render as `.30`). Always write
  `\$237.30` instead.
- **`change_pct` is PERCENT, not fraction.** `0.84` = 0.84%, NOT
  84%. Matches `fast_info.change_pct` and `history --summary.change_pct`.
  Don't multiply by 100 again.
- **`regular_market_time` can lag — three independent reasons.** It's
  the timestamp of the last trade Yahoo has on file. For closed markets
  it's typically last Friday's close. For live markets, three sources of
  staleness compound:
  1. **API feed delay** — `data_delayed_by_minutes` per-quote, surfaces
     Yahoo's `exchangeDataDelayedBy`. Verified ranges: US large indexes
     0min, Russell `^RUT` 15min, futures `GC=F` 10min, ASIA indexes
     15–20min. 0 = real-time.
  2. **yfinance SQLite cache** — see [Mode-specific caveats](#yfinances-persistent-sqlite-cache-hides-staleness).
     Repeat calls within minutes can return cached responses (0 HTTP).
  3. **Closed-market freeze** — outside trading hours the timestamp is
     last close, regardless of when you query.
  Use `data_delayed_by_minutes` to distinguish (1) from (2) / (3): if
  the field is 15 but `regular_market_time` is hours behind, you're
  hitting cache or a closed market, not just feed delay.
- **`listing_region` is Yahoo's listing-region tag, not the market key
  or the home market of the underlying.** All summary rows return
  `"listing_region": "US"` regardless of which Yahoo region you queried
  — that's the index's listing region per Yahoo's internal taxonomy
  (e.g., `^N225` is tagged `"US"` even though it tracks Tokyo). Use
  `exchange_timezone` for the actual home market, or filter by symbol
  prefix. (Yahoo's raw field name is `region`; we rename to
  `listing_region` so the field name matches what it actually means
  — see [Per-section schemas](#per-section-schemas).)

## Mode-specific caveats

### **`clock` always returns the U.S. market** (Yahoo quirk)

Verified 2026-05, yfinance 1.3.x: `Market(<region>).status` always
returns the U.S. clock regardless of `<region>`. yfinance hits Yahoo's
`/v6/finance/markettime` endpoint with the `market=<region>` query
param — Yahoo accepts the param but consistently returns
`name="U.S. markets"`, `timezone.short="EDT"`, and the U.S. trading
session bounds for **every** region (verified across `ASIA`,
`EUROPE`, `RATES`, `COMMODITIES`, `CURRENCIES`, `CRYPTOCURRENCIES`).

Two implications:

1. The `clock` section is **only useful for `market="US"`**. For
   non-US regions we still surface it (the data IS valid — just for
   the US, not the requested region) and add a `clock_is_us_fallback:
   true` flag at the envelope level so callers can branch
   programmatically. The flag is a bool (not a long string) so
   CSV / NDJSON consumers don't repeat the same warning text per row.
2. **For per-region open/closed signals, read each summary row's
   `market_state`** field instead. That IS region-specific (`CLOSED`
   for Friday-evening US-time Asian markets, `REGULAR` for US gold
   futures still trading after the cash-equity close, etc.).

If Yahoo ever fixes the endpoint, the `clock_is_us_fallback` flag
will become misleading — re-verify with a quick `market.py ASIA` and
inspect `clock.name` / `clock.timezone`. Update or remove the flag-
setting code if it no longer matches.

### Cost: 2 HTTP per market, regardless of `--section`

yfinance's `Market._parse_data` interleaves both endpoint fetches to
keep them time-aligned ("Fetch both to ensure they are at the same
time" per yfinance source). So `--section clock` does NOT save the
summary HTTP, and `--section summary` does NOT save the markettime
HTTP. Use `--section` only to slim the output payload, not to
reduce network cost.

N markets = **2 × N HTTP**, serial. The cost preview fires on stderr
at ≥ 4 markets:

```
info: market plan = 4 region(s) × 2 HTTP each = 8 HTTP total; ~6–12s typical
```

### Sparse summaries are normal, not error

`CURRENCIES` and `CRYPTOCURRENCIES` typically return **1 featured
quote** each (e.g. `MXN=X`, `SOL-USD`). This is Yahoo's curated
front-page snapshot, not an error and not a coverage gap. `GB` is
similarly sparse (~3 rows; UK coverage is mostly under `EUROPE`).

For full FX / crypto coverage:

- FX universe: `screener --predefined currencies_us` (if available) or
  iterate explicit pairs via `fast_info EURUSD=X JPY=X ...`
- Crypto universe: `screener --predefined top_crypto_us` or `fast_info
  BTC-USD ETH-USD ...`

### Invalid market keys: argparse rejects before HTTP

Unknown keys are validated against the canonical 8 at argparse-time, so
`market.py BOGUS` exits with rc=2 and a clear error rather than firing
a 2-HTTP probe. yfinance's underlying behavior on bad keys is partial-
silent failure (status returns US data; summary throws inside the
parser) — we short-circuit before that.

### `quote_type` enum surprises

`ASIA` includes a `CURRENCY` row (`USD/JPY`) alongside the 5 indexes —
Yahoo treats Asia FX as part of the regional pulse. CSV / table
consumers iterating across regions should not assume `summary` rows
are uniformly `INDEX`; filter on `quote_type` if you only want indexes.

`US` summary's last row is `GC=F` (Gold) — `quote_type=FUTURE`,
`full_exchange_name=COMEX`. Same caveat: Yahoo bundles a representative
commodity into the US pulse. `RATES` is similar — it returns 1 INDEX
(`^TYX` 30-Yr yield) + 1 FUTURE (`ZN=F` 10-Yr T-Note futures), so
"RATES" really means "Treasury landscape," not "yields-only feed."

### yfinance's persistent SQLite cache hides staleness

`yfinance.Market()` runs HTTP through `yfinance.data.YfData`, which
caches Yahoo responses in a process-shared SQLite file (default:
`~/.cache/py-yfinance/...`). For per-ticker modes (`info`, `financials`,
etc.) cache hits are usually fine — fundamentals don't move minute-to-
minute. For a **live pulse** mode like `market`, cache hits can
silently return stale prices.

Smoke-test evidence (verified 2026-05): calling `Market("US").summary`
twice in the same process within ~minutes returns the second response
from cache (0 HTTP). The smoke test for "2 HTTP per market" had to
use **different markets** for the two probes to bypass this — see
the comment in `smoke.py`'s market-section HTTP-count regression.

What this means for callers:

- A repeat `market.py US` call within a short window may show prices
  from the previous call, not "now." For genuinely live data, the
  first call of a session is the freshest.
- The cache TTL isn't easily inspectable from outside yfinance — empirically
  responses persist tens of minutes to hours. Check `regular_market_time`
  on each summary row to verify freshness; if every row's timestamp
  matches a previous fetch, you got the cache.
- This applies to `info` / `holders` / `insiders` / etc. too, but the
  staleness is more visible here because users expect "live."

`market.py` doesn't expose a `--no-cache` flag because bypassing the
yfinance cache requires monkey-patching `YfData.cache_get` (no public
API). If freshness is critical, delete `~/.cache/py-yfinance/` between
calls or use a shorter-lived data source (Yahoo's frontend chart API,
Polygon, etc.).

## See also

- [`fast_info.md`](fast_info.md) — single-ticker quote (price + market
  cap + 52w range). Use to fetch one quote at a time for any ticker
  surfaced in a `market` summary.
- [`history.md`](history.md) — historical OHLCV. Use after `market`
  identifies a leading index to chart its return curve.
- [`screener.md`](screener.md) — universe-wide filter by predicate
  (`peratio < 15 AND eps_growth > 0.20`). Distinct discovery axis
  from `market`: screener filters; market reports a curated regional
  pulse.
- [`calendars.md`](calendars.md) — date-bounded event calendar
  (earnings / IPO / splits / economic). Distinct from `market`:
  calendars is "what's happening this week"; market is "what's
  trading right now."
- [`sectors.md`](sectors.md) — Yahoo's curated sector / industry
  hierarchy (US-listed taxonomy). Distinct from `market` (cross-region
  pulse): sectors is single-market drill-down by industry.
