[← back to SKILL.md](../SKILL.md)

# `calendars` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`calendars.py --type earnings --limit 3` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Per-type schemas](#per-type-schemas) · [`--summary` rollups](#summary-rollups) · [Common region codes](#common-region-codes) · [Presenting calendar results](#presenting-calendar-results) · [Mode-specific caveats](#mode-specific-caveats)

Market-wide calendar mode — answers questions of the shape "what's
happening this week / month". **Single envelope per call** (one HTTP,
one result), same shape as `screener.py`. Distinct from `earnings.py`
which is **per-ticker** ("AAPL's earnings history") — calendars
discovers events without taking a ticker.

Four event types, one or many per invocation (`--type`):

- **earnings** (default) — companies reporting earnings. Defaults to
  Yahoo's "most active" filter so you don't drown in micro-caps.
- **ipo** — upcoming / recent IPOs (filing / pricing / amendment dates).
- **splits** — stock splits, forward and reverse (with derived
  `direction` field).
- **economic** — macro / central-bank events (CPI, FOMC, GDP, jobs, ...)
  with best-effort `unit` inference.

Multi-type (`--type earnings,ipo` or `--type all`) sequences the
per-type HTTP calls and tags each record with a `record_class`
discriminator in NDJSON / CSV output.

## Run

```bash
# Earnings this week (default — most-active filter on, today + 7 days)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py

# Earnings next 14 days, large caps only ($10B+)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --days 14 --market-cap 10e9 --limit 50

# All earnings (disable most-active filter — full firehose)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --no-most-active --limit 100

# Upcoming IPOs in the next 30 days
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type ipo --days 30 --limit 50

# Stock splits this week
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type splits --limit 50

# Economic events this week (CPI / FOMC / GDP / ...)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type economic --limit 50

# What's happening this week — all 4 types in one call
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type all --limit 25 --format ndjson

# Retrospective: who reported in the last 7 days
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --past-days 7 --limit 100

# Specific date range (ISO dates, both inclusive)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --start 2026-06-01 --end 2026-06-15

# Summary digest — counts / aggregates per type
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type all --summary --limit 100

# CSV — one row per event, type-specific columns
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/calendars.py \
  --type earnings --days 7 --format csv
```

No positional args. The default window is **today through today + 7
days** — matches yfinance's `Calendars()` default.

## CLI arguments

- `--type {earnings,ipo,splits,economic,all}` — calendar type(s).
  **Case-insensitive.** Single value, comma-separated list, or `all`
  alias for all four. Default `earnings`. Each type is a separate
  HTTP call (1 per type, or 2 for earnings with the default
  most-active filter).
- `--start YYYY-MM-DD` — window start (inclusive). Default: today
  (UTC). **Strict format**: argparse rejects `2026/06/01`,
  `2026-06-01T12:00:00`, US-style `06-01-2026`, etc.
- `--end YYYY-MM-DD` — window end (inclusive). Default: `--start + 7
  days`. Mutually exclusive with `--days` and `--past-days`.
- `--days N` — convenience: `--end = --start + N days`. Mutually
  exclusive with `--end` and `--past-days`.
- `--past-days N` — retrospective scan: window = (today − N) through
  today. Mutually exclusive with `--end`, `--days`, and `--start`.
  Useful for "who reported last week" / "IPOs in the past 30 days".
- `--limit N` — max records per type. Default 25 (yfinance's
  underlying default is 12; we lift it slightly for a meaningful
  default-invocation scan). **Yahoo caps at 100**; argparse rejects
  higher.
- `--offset N` — pagination offset. Default 0. **Caveat for
  earnings**: yfinance silently disables Yahoo's most-active filter
  when offset > 0; the envelope's `filter_most_active` field reports
  the effective state.
- `--market-cap USD` — earnings-only floor (e.g. `1e10` for \$10B).
  Filters Yahoo's data AND the most-active prescreen. Ignored with a
  stderr warning when no earnings type is present.
- `--no-most-active` — earnings-only: disable Yahoo's most-active
  filter (default ON). Ignored with a stderr warning when no
  earnings type is present.
- `--summary` — emit per-type rollup counts/aggregates instead of
  the full event list. See [`--summary` rollups](#summary-rollups).
  Mutually exclusive with `--full`.
- `--full` — emit raw Yahoo column names (snake_cased, no projection)
  instead of the curated schema. Useful when Yahoo serves a field
  outside the curated set. Incompatible with `--format csv`.
  Mutually exclusive with `--summary`.
- `--format json|ndjson|csv` — output format.
  - **json** (default): single envelope dict (single `--type`) OR
    list of envelopes (multi-`--type`).
  - **ndjson**: one record per line. Every record gets a
    `record_class` field (matching `--type`) so multi-type rows are
    grouped consistently.
  - **csv**: one row per record. Single-type CSV uses type-specific
    cols; multi-type CSV uses union of cols + `record_class`
    discriminator with empty cells where N/A.

## Output schema

### Single `--type`

```json
{
  "type": "earnings",
  "start": "2026-05-10",
  "end": "2026-05-17",
  "filter_most_active": true,
  "market_cap_floor": null,
  "returned": 25,
  "offset": 0,
  "results": [ <records — schema varies by type> ]
}
```

### Multi-`--type` or `--type all`

```json
[
  { "type": "earnings", ..., "results": [...] },
  { "type": "ipo",      ..., "results": [...] },
  { "type": "splits",   ..., "results": [...] },
  { "type": "economic", ..., "results": [...] }
]
```

Envelope keys (per single envelope):

| Key | Type | Notes |
|---|---|---|
| `type` | str | One of `earnings` / `ipo` / `splits` / `economic`. |
| `start` | str | Window start, YYYY-MM-DD. |
| `end` | str | Window end, YYYY-MM-DD. |
| `filter_most_active` | bool | **Earnings only.** Effective state — `true` iff `--no-most-active` was NOT passed AND `--offset 0`. Shows `false` even when you didn't pass `--no-most-active` if offset > 0 disabled it upstream. |
| `market_cap_floor` | float / null | **Earnings only.** Echoes `--market-cap` (null when unset). |
| `returned` | int | Number of records in `results`. ≤ `--limit`. |
| `offset` | int | Pagination offset (echoes input). |
| `attempts` | int (optional) | Present only when fetch retried. |
| `note` | str (optional) | Empty-result hint. Mutually exclusive with `error`. |
| `error` | str (optional) | Failure path. |
| `error_kind` | str (optional) | Same enum as the rest of the skill. |
| `results` | list | Per-event records — schema varies by type. |

A failed call:

```json
{
  "type": "earnings",
  "start": "2026-05-10",
  "end": "2026-05-17",
  "filter_most_active": true,
  "market_cap_floor": null,
  "error": "fetch failed (rate_limit, after 3 attempt(s))",
  "error_kind": "rate_limit",
  "attempts": 3
}
```

NB: there's **no `total` field**. Yahoo's calendar endpoint doesn't
return a "total available" count separate from the page-returned
count, so we don't fake one.

## Per-type schemas

### `--type earnings` (9 fields)

| Field | Type | Notes |
|---|---|---|
| `symbol` | str | Ticker (e.g. `CSCO`, `BABA`). |
| `company` | str | Issuer short name. |
| `market_cap` | int / null | Intraday market cap in **USD** (Yahoo's USD-normalized `intradaymarketcap`, distinct from `info.py`'s `valuation.market_cap` which can be in trading currency for some non-US listings). |
| `event_name` | str | E.g. `"Q3 2026 Earnings Announcement"`. Includes fiscal period in the string itself. |
| `event_start_datetime` | str / null | ISO 8601 with offset. UTC in observed payloads. |
| `timing` | str / null | `BMO` (before market open), `AMC` (after market close), `TNS` (time not supplied), `TBD`. |
| `eps_estimate` | float / null | Consensus EPS in trading currency (USD for US-listed and ADRs). |
| `eps_actual` | float / null | Reported EPS — null until reported. |
| `surprise_pct` | float / null | **PERCENT** — `5.2` means +5.2% beat. Matches `earnings.py earnings_dates.surprise_pct`. Null until reported. |

### `--type ipo` (12 fields)

| Field | Type | Notes |
|---|---|---|
| `symbol` | str | Ticker assigned at listing. |
| `company` | str | Full company name including share class. |
| `exchange` | str | Yahoo's exchange short name (`"Nasdaq"`, `"NYSE American"`, `"NYSE"`, `"NYSEArca"`). NB: this is **not** the same code shorthand as `info.py.exchange` (which uses `NMS` / `NYQ`) — IPO calendar uses long names directly. |
| `filing_datetime` | str / null | ISO 8601 with offset. When the S-1 / F-1 was filed. Often null in upcoming-IPO rows. |
| `ipo_datetime` | str / null | ISO 8601 with offset. Listing date / expected listing date. |
| `amended_datetime` | str / null | ISO 8601 with offset. Most recent S-1/A amendment, if any. |
| `price_from` | float / null | Lower bound of indicative price range. |
| `price_to` | float / null | Upper bound of indicative price range. |
| `offer_price` | float / null | Final offer price (null pre-pricing). When populated, `price_from` / `price_to` are usually null. |
| `currency` | str | Pricing currency. |
| `shares` | int / null | Shares offered (null pre-pricing). |
| `action` | str | Status enum: `"Expected"` / `"Priced"` / `"Postponed"` / `"Withdrawn"`. |

**Why `_datetime` not `_date`**: Yahoo encodes calendar dates as
`04:00 UTC` (= midnight EDT during DST). In **EST** (winter, UTC−5),
that same encoding lands at `23:00 EST` on the *previous* calendar
day, so a naive truncation to `YYYY-MM-DD` would silently be
off-by-one in winter. We preserve the full datetime; the consumer
can localize and truncate if they need a date.

### `--type splits` (7 fields)

| Field | Type | Notes |
|---|---|---|
| `symbol` | str | Ticker. Empirically dominated by `*.KQ` (Korean) reverse splits. |
| `company` | str | Issuer short name. |
| `payable_datetime` | str / null | ISO 8601 with offset. Same `04:00 UTC` encoding caveat as IPO dates. |
| `optionable` | bool / null | Whether the underlying has listed options. False for most non-US listings. |
| `old_ratio` | float / null | OLD share count in the ratio (Yahoo's `Old Share Worth`). |
| `new_ratio` | float / null | NEW share count in the ratio (Yahoo's `Share Worth`). |
| `direction` | str / null | **Derived** field: `"forward"` (`new_ratio > old_ratio`), `"reverse"` (`new_ratio < old_ratio`), `"even"` (`==`), or `null` (either ratio missing). |

**Reading the ratio:** `old_ratio:new_ratio` = "M old shares become N
new shares". Examples:
- AAPL 2020 forward split (4:1): `old=1, new=4` (1 → 4, share count
  UP, price per share DOWN), `direction: "forward"`.
- Korean reverse split 10:1: `old=10, new=1` (10 → 1, consolidation),
  `direction: "reverse"`.

### `--type economic` (9 fields)

| Field | Type | Notes |
|---|---|---|
| `event` | str | Yahoo's event label, e.g. `"GDP YY*"`, `"CPI MM"`, `"PPI YY*"`. The `*` suffix appears on certain Yahoo-internal categories — empirically (2026-05 probe across 100 events) it dominates emerging-market and lower-frequency releases (TH / IE / LT / NG / TR / SG / KW), while major Western releases (CA / MX / GB / JP / CH / NZ) usually come without. The same event name can appear with AND without `*` for different regions ("Retail Sales MM" verified in both buckets). Treat as opaque — don't try to parse. |
| `region` | str | ISO-3166-alpha-2 country code, e.g. `"US"`, `"GB"`, `"PE"`. See [Common region codes](#common-region-codes). |
| `event_time` | str / null | ISO 8601 with offset. **Precision is mixed** (probe 2026-05): ~52% of rows are at `00:00:00 UTC` (date-of-release fallback for many emerging markets and end-of-day-batch releases), but ~48% have **real intraday timing** — US Michigan sentiment at `14:00 UTC`, US USDA reports at `19:00 UTC`, Canadian retail / producer prices at `12:30 UTC`, Mexican GDP at `12:00 UTC`, etc. Treat the time as reliable when present. |
| `period` | str / null | Reporting period the data covers, e.g. `"Mar"`, `"Apr"`, `"Q1"`, `"Q1 2026"`. Free-form string. |
| `actual` | float / null | Released value (null pre-release). Unit varies — see `unit`. |
| `expected` | float / null | Consensus / market expectation. |
| `prior` | float / null | Last release's value. |
| `prior_revised` | float / null | Revised value of the prior release, if any. |
| `unit` | str / null | **Best-effort** unit classification from the event name. One of `"percent"` / `"index_level"` / `"thousands"` / `"currency"` / `null` (no rule matched). Useful for "how to render this number" but not authoritative — Yahoo doesn't publish a per-row unit, and the heuristic relies on event-name conventions. When `null`, consult the event name and don't infer arithmetic semantics. |

**`unit` heuristic ordering** (most specific first; first match wins):
1. `\b(YY|MM|QQ)\b` → `percent` (rate-of-change suffix)
2. `\b(PMI|Sentiment|Confidence|Conditions|Expectations|Index|ISM|ZEW|IFO|Tankan|Manheim)\b` → `index_level`
3. `\b(CPI|PPI|Infl|Wage|Earnings|Yield)\b` → `percent`
4. `\b(Payrolls|Claims|NFP|Hiring|Vacancies|Job(?:less)?)\b` → `thousands`
5. `\b(Trade Balance|Current Account|Budget|Deficit|Reserves|Money Supply|M\d|Loan)\b` → `currency`
6. `\b(Rate Decision|Policy Rate|Funds Rate|Repo|Bank Rate)\b` → `percent`
7. `\bGDP\b` → `percent`

Add to `_UNIT_RULES` in `scripts/calendars.py` if you observe a new
event-name shape.

## `--summary` rollups

`--summary` replaces the `results` list with a per-type `summary` dict
of counts / aggregates. Pairs naturally with `--type all` for a
"what's happening this week" digest. Each rollup preserves envelope
metadata (type / start / end / offset) so peer comparison across
types still has window context.

### earnings rollup

| Field | Type | Notes |
|---|---|---|
| `count` | int | Total earnings rows in window. |
| `count_with_estimate` | int | Rows where `eps_estimate` is non-null. |
| `count_reported` | int | Rows where `eps_actual` is non-null (already-reported). |
| `count_by_timing` | dict | `{"AMC": 12, "BMO": 8, "TNS": 1, ...}`. |
| `avg_market_cap` | float / null | Mean of populated `market_cap` values. USD. |
| `max_market_cap` | int / null | Largest `market_cap`. |
| `min_market_cap` | int / null | Smallest `market_cap`. |

### ipo rollup

| Field | Type | Notes |
|---|---|---|
| `count` | int | Total IPO rows in window. |
| `count_by_action` | dict | `{"Expected": 5, "Priced": 2, ...}`. |
| `unique_exchanges` | list | Sorted exchange names that appear. |
| `count_with_offer_price` | int | Already-priced IPOs. |
| `count_with_price_range` | int | IPOs with both `price_from` and `price_to`. |

### splits rollup

| Field | Type | Notes |
|---|---|---|
| `count` | int | Total splits rows in window. |
| `count_forward` | int | `direction == "forward"`. |
| `count_reverse` | int | `direction == "reverse"`. |
| `count_even` | int | `direction == "even"` (rare; `old == new`). |
| `count_with_options` | int | `optionable is True`. |

### economic rollup

| Field | Type | Notes |
|---|---|---|
| `count` | int | Total economic rows in window. |
| `count_by_region` | dict | Top 10 regions by row count, descending (when fewer than 10 are present we just emit all of them — the field name doesn't claim "exactly 10"). |
| `unique_regions` | list | All regions present, sorted alphabetically (full list, not capped). |
| `count_by_unit` | dict | Distribution across `percent` / `index_level` / `thousands` / `currency` / `unknown`. |
| `count_with_actual` | int | Already-released rows. |

## Common region codes

`region` is ISO-3166-alpha-2. Quick decoder for the codes most likely
to appear in a real `--type economic` result; full list at
[en.wikipedia.org/wiki/ISO_3166-1_alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2).

| Code | Region | Code | Region | Code | Region |
|---|---|---|---|---|---|
| `US` | United States | `GB` | United Kingdom | `DE` | Germany |
| `JP` | Japan | `CN` | China | `FR` | France |
| `IN` | India | `KR` | South Korea | `CA` | Canada |
| `AU` | Australia | `HK` | Hong Kong | `MX` | Mexico |
| `BR` | Brazil | `CH` | Switzerland | `NZ` | New Zealand |

## Presenting calendar results

**Earnings.** Best rendered as a table — symbol, company, datetime
(or just date + AMC/BMO), event_name, EPS estimate, EPS actual,
surprise_pct (when populated). For multi-day windows, group by date
heading. Highlight surprise_pct sign (positive = beat). Convert
`event_start_datetime` to ET (or the user's local tz) for prose;
strip seconds.

> **Earnings — week of \<YYYY-MM-DD\>**
>
> ### Mon \<YYYY-MM-DD\>
> - **\<TICKER\>** \<Company\> — \<HH:MM\> ET (\<AMC|BMO\>); est. \<EPS\>
> - …

**IPOs.** Pricing-range tables. Show price_from / price_to (or
offer_price when priced), shares, exchange. Strip share-class
suffixes from `company` for compact rendering. Localize
`ipo_datetime` to ET before truncating to a date for display.

**Splits.** Render the `direction` field directly: *"4:1 forward
split"* / *"10:1 reverse split"*. Always show date and direction;
show the raw ratio only if the user explicitly asks.

**Economic events.** Group by region (or by event-name family for
cross-country compare). Columns: date, region, event, prior →
expected → actual. Use `unit` to format the number — append `%` for
percent, `K` for thousands, etc. For currency-denominated entries,
the currency is implicit in the region (USD for US, JPY for JP, ...).
Highlight actual vs expected when populated.

**Multi-type digest.** Best rendered as four sections (one per type),
each with a count and the top N entries. `--summary` mode is the
fastest path to this — emit the rollup counts first, then drill in
per type as needed.

**Escape `$` as `\$`** in any prose with dollar amounts (market caps,
prices) — same renderer-math-mode caveat as the rest of the skill.

## Mode-specific caveats

- **Earnings filter on by default — wide windows surface only major
  names.** yfinance defaults `filter_most_active=True`, which restricts
  the earnings calendar to ~200 most-active US tickers' upcoming
  reports. Verified empirically (2026-05): a default 7-day window on
  earnings returns 12–25 names, dominated by US large caps and ADRs.
  Pass `--no-most-active` for the wider firehose. The flag also kicks
  in silently when `--offset > 0` (yfinance limitation: pagination
  and most-active filter don't compose upstream). The envelope's
  `filter_most_active` field shows the *effective* state.
- **`market_cap` floor is in USD even for non-US listings.** The
  `intradaymarketcap` field Yahoo filters on is USD-normalized — so
  `--market-cap 1e10` correctly excludes a non-US ticker whose local
  market cap nominally exceeds \$10B in HKD. Don't translate.
- **All IPO / splits / filing / payable timestamps come back as `04:00
  UTC`.** Probed 50 splits + 13 IPOs (2026-05) — every single one was
  exactly `04:00 UTC`, regardless of issuer market (Korea, HK, US,
  Japan, India, Sweden, Frankfurt, etc.). That's `midnight EDT`
  during DST; in EST (winter) the same encoding lands at `23:00` on
  the *previous* calendar day. **Don't truncate to date in UTC** —
  localize first if you need a date. We preserve the full ISO
  datetime to avoid shifting that responsibility silently.
- **IPO calendar skews to upcoming, not historical.** Default 7-day
  window returns mostly `action: "Expected"` rows. For "recently
  priced" lookups, use `--past-days` or `--start <date>` in the past
  explicitly. There's no `action` filter flag — sort consumer-side
  after the fetch.
- **Splits payload is dominated by non-US listings.** ~70% of split
  rows in any given week are Korean (KQ suffix). Filter `symbol`
  consumer-side if you want US-only splits.
- **Economic events: `event_time` precision is mixed but better than
  pessimistic.** ~48% of observed events have real intraday times
  (verified 2026-05: US Michigan at 14:00 UTC, USDA at 19:00 UTC,
  CA / MX at 12:00–12:30 UTC). The other ~52% fall back to
  `00:00:00 UTC` — those are mostly emerging-market releases. Treat
  midnight UTC as "date-of-release without minute precision," not
  literally midnight.
- **Empty result is success-with-note, NOT error.** A narrow window
  (e.g. `--days 1` on a weekend, or splits in a quiet stretch)
  returns `results: []` plus a `note` string. CSV mode emits a single
  carry row with `note` populated; NDJSON emits a single
  envelope-summary line. No `error_kind` is set.
- **Multi-type cost.** `--type all` is 4 HTTP minimum (5 with default
  earnings most-active filter). Sequenced, not parallel — total ≈
  N × per-type cost. Use `--summary` to compress output without
  changing fetch cost.
- **`--past-days` ignores `--start`.** Combining the two raises an
  argparse error. The retrospective semantics (today − N → today)
  override an explicit start.
- **No `total` field in the envelope.** Yahoo's calendar endpoint
  doesn't return a "total available" count separate from the page
  size. The `returned` field is the only honest count.
- **Unofficial.** yfinance Calendars landed in 1.3.0 (Oct 2025) and
  the underlying Yahoo endpoint is undocumented. Schema drift is
  more likely here than in older endpoints. The script defends with
  fallback chains for the most-likely-renamed columns (`Marketcap`,
  `Surprise(%)`, `Exchange`, `Optionable`) — but if a fundamental
  shape change happens, re-run smoke (`scripts/smoke.py`) to catch it.
