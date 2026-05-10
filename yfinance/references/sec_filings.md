[← back to SKILL.md](../SKILL.md)

# `sec_filings` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`sec_filings.py --summary AAPL TM SPY 0700.HK BOGUSXYZ` if you suspect
upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting sec_filings results](#presenting-sec_filings-results) · [Mode-specific caveats](#mode-specific-caveats)

SEC filings list for one or more tickers, sourced from
`Ticker.sec_filings`. One section per ticker — the `filings` list. Each
filing has `date / type / title / edgar_url / primary_url /
exhibit_count / exhibit_keys / exhibits`. Yahoo's response is bounded
by row count (~75–120 filings per ticker), which translates to a
~4-year window for active large-caps (verified 2026-05: AAPL 76
filings going back to 2022-01-27; MSFT 76 to 2022-01-25; TM 120 to
2022-02-03). Quieter issuers may reach further back; very active
issuers may have a tighter window.

**SEC-coverage scope.** Only SEC-registered securities have data:

- ✅ **US-listed equities** (AAPL, MSFT, NVDA, …) — full 10-K / 10-Q /
  8-K / DEF 14A / SC 13G/A / S-8 / etc. coverage.
- ✅ **ADRs** (TM = Toyota, BABA = Alibaba ADR, PBR = Petrobras ADR) —
  foreign issuers register with SEC and file 6-K (interim) / 20-F
  (annual). TM in 2026-05 has 120 filings, ~107 of them 6-K.
- ❌ **Non-US primary listings** (BMW.DE, 0700.HK) — return empty.
  Distinct from ADRs: a German company's primary Frankfurt listing
  isn't SEC-registered, but its US ADR (if any) would be.
- ❌ **ETFs** (SPY, QQQ), **mutual funds** (VFIAX), **indexes** (^GSPC),
  **crypto** (BTC-USD), **FX** (EURUSD=X), **futures** (ES=F) — all
  return empty.
- ❌ **Bogus / delisted** tickers — also return empty.

The five empty cases (non-US primary / non-equity / bogus) are
**indistinguishable** at the API level — Yahoo returns `{}` for all of
them. The script reports success-with-`note` rather than guessing the
cause; chain `fast_info` to disambiguate. No `coverage_note`
partial-empty path: the SEC-filings endpoint is binary (filings exist
or they don't), unlike `holders` / `insiders` / `analyst` where some
non-US issuers expose partial data.

## Run

```bash
# Default: all filings, pretty JSON (full schema incl. exhibits dict)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py AAPL

# Top 5 most recent
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --limit 5 AAPL

# Filter by type — quarterly + annual reports only (US issuer)
# Case-insensitive: `10-k` matches Yahoo's `10-K`.
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --type 10-K,10-Q AAPL

# Filter by type + cap — most recent 8-K (event filing)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --type 8-K --limit 1 TSLA

# Date floor — filings on or after a specific date
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --since 2024-01-01 AAPL

# Date floor — convenience: last N days (mutually exclusive with --since)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --days 30 --type 8-K TSLA AAPL NVDA

# Peer rollup: flat per-ticker dict (~10× smaller than default)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --summary AAPL MSFT NVDA

# Summary across mixed US issuer + ADR (latest_10k_date for AAPL,
# latest_20f_date for TM)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --summary --format csv AAPL TM

# CSV default mode — one row per filing (exhibits dict dropped;
# primary_url + exhibit_count carry the actionable signals)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sec_filings.py --format csv --limit 5 AAPL
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap filings per ticker. Default: keep all (Yahoo
  returns up to ~75–120 going back ~4 years for active issuers).
  Applied **after** `--since` / `--days` and `--type`, so
  `--since 2024-01-01 --type 10-K --limit 1` yields the most recent
  10-K filed on or after 2024-01-01 (not "the first of the (last 1)
  filings if it happens to be a 10-K").
  **Ignored in `--summary` mode** (with a stderr warning when used
  alongside `--summary`). The flat metrics (`total_filings`,
  `latest_*_date`, `filings_last_90d`) are computed from Yahoo's
  full response so they describe the data, not the display knob —
  and stay invariant under `--limit` by design. Same contract as
  `holders.py` / `insiders.py`, except those modes don't warn.
- `--since YYYY-MM-DD` — include only filings dated on or after this
  date (inclusive). Accepts ISO date or datetime (`2024-01-01` or
  `2024-01-01T00:00:00`); the time component is discarded and the
  date normalized to YYYY-MM-DD before comparison. Lexicographic
  string compare on the normalized date — exact and tz-agnostic
  (no DST / locale issues). Bad input rejected at argparse layer
  (rc=2). Surfaces a stderr warning if combined with `--summary`
  (the summary metrics describe Yahoo's full response, not the
  display knobs); the call still runs but the flag is a no-op.
- `--days N` — convenience shortcut for `--since (today - N days)`.
  Computes the cutoff in UTC; a 1-day smear at the edge is fine for
  rough-recency queries. Mutually exclusive with `--since`. Same
  `--summary` warning as `--since`.
- `--type T1[,T2,...]` — comma-separated filing types to include.
  **Case-insensitive** — `--type 10-k` and `--type 10-K` both match
  Yahoo's `10-K`. Whitespace **inside** a type is preserved
  (`DEF 14A` is two tokens internally and stays so after upper-case
  normalization). Default: include all types. Tickers whose entire
  filing set is filtered out emit a single CSV row with the symbol +
  the `filter_note` column populated (mutually exclusive with `note`)
  — see [filter_note semantics](#filter_note-semantics) below. Same
  `--summary` warning as `--since`. Common types:
  - **US issuer**: `10-K` (annual), `10-Q` (quarterly), `8-K` (event)
  - **Governance**: `DEF 14A` (proxy statement), `DEFA14A` (additional
    proxy materials)
  - **ADR / foreign issuer**: `20-F` (annual), `6-K` (interim — most
    common foreign-issuer filing)
  - **Equity offerings**: `S-8` (employee stock plan registration),
    `S-3ASR` (automatic shelf), `S-4` (M&A registration)
  - **Ownership**: `SC 13G/A` (passive 5%+ holder amendment),
    `SC 13D/A` (active 5%+ holder amendment)
  - **Other**: `SD` (specialized disclosure, e.g. conflict minerals),
    `CORRESP` (SEC correspondence), `11-K` (employee stock purchase
    plan annual)
- `--summary` — flat per-ticker projection. Lifts headline metrics
  (latest date / type, latest 10-K / 10-Q / 8-K / 20-F / 6-K / proxy
  dates, 90-day filing count) to a one-record-per-ticker dict.
  Useful for peer-comparison tables; ~10× smaller than default. Same
  network cost as default (post-fetch projection).
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array, one record per ticker. `ndjson` emits one JSON
  record per ticker per line. `csv` shape depends on mode:
  - **default mode** — one row per filing, with the `symbol` column
    repeating across a ticker's filings. **The nested `exhibits` dict
    is dropped from CSV** (no clean tabular shape — variable keys per
    filing); `primary_url` + `exhibit_count` + `exhibit_keys` (a
    pipe-joined list of exhibit keys, e.g.
    `"10-Q|EX-31.1|EX-31.2|EX-32.1"`) carry the actionable signals.
    Three empty paths each emit a single carry row instead of data
    rows: errored tickers carry `error*` cols; tickers Yahoo returned
    nothing for carry `note`; tickers whose filings were all filtered
    out by `--type` / `--since` / `--days` carry `filter_note`. The
    three are mutually exclusive at the result level.
  - **`--summary` mode** — strict one row per ticker (every column
    populated where the ticker has data). Same row shape across all
    tickers, friendly for spreadsheet pivots.

CSV column order (default mode, left to right): `symbol`, `date`,
`type`, `title`, `edgar_url`, `primary_url`, `exhibit_count`,
`exhibit_keys`, `note`, `filter_note`, then the 3 meta fields
(`error`, `error_kind`, `attempts`).

## Output schema

### Default mode

Per ticker (illustrative; real AAPL fetch in 2026-05):

```json
[
  {
    "symbol": "AAPL",
    "count": 76,
    "filings": [
      {
        "date": "2026-05-01",
        "type": "10-Q",
        "title": "Periodic Financial Reports",
        "edgar_url": "https://finance.yahoo.com/sec-filing/AAPL/0000320193-26-000013_320193",
        "primary_url": "https://cdn.yahoofinance.com/prod/sec-filings/0000320193/000032019326000013/aapl-20260328.htm",
        "exhibit_count": 4,
        "exhibit_keys": "10-Q|EX-31.1|EX-31.2|EX-32.1",
        "exhibits": {
          "10-Q":    "https://cdn.yahoofinance.com/.../aapl-20260328.htm",
          "EX-31.1": "https://cdn.yahoofinance.com/.../a10-qexhibit311...htm",
          "EX-31.2": "https://cdn.yahoofinance.com/.../a10-qexhibit312...htm",
          "EX-32.1": "https://cdn.yahoofinance.com/.../a10-qexhibit321...htm"
        }
      }
    ]
  }
]
```

`count` is **always** Yahoo's response size (set by `fetch()`, not
mutated by filters) — a 76-filing AAPL fetch keeps `count: 76` even
when `--type 10-K --limit 1` shrinks `filings` to one row. To recover
the displayed count, use `len(result["filings"])` or `total_filings`
in `--summary` mode.

#### Per-filing fields (8 fields)

| Field | Type | Notes |
|---|---|---|
| `date` | str / null | Filing date as `YYYY-MM-DD`. Sourced from Yahoo's pre-formatted `date` string when it's a clean ISO date; falls back to converting `epochDate` (UTC) if the string is malformed. |
| `type` | str / null | SEC form type — Yahoo emits mixed case (`10-K`, `8-K`, `DEF 14A`, `SC 13G/A`). The `--type` filter is **case-insensitive**, so user input case doesn't matter; the value emitted here is whatever Yahoo gave us, preserved verbatim. |
| `title` | str / null | Yahoo's human description (e.g. `"Periodic Financial Reports"`, `"Corporate Changes & Voting Matters"`). Coarse — Yahoo groups many distinct form types under one title; trust `type` for the precise classification. |
| `edgar_url` | str / null | Yahoo's redirector page (`finance.yahoo.com/sec-filing/...`). Renders Yahoo's wrapper UI; useful for a human "click to read" link, less useful for direct doc fetch. |
| `primary_url` | str / null | Best-guess URL of the main filing document. Heuristic: `exhibits[type]` if present (the convention — verified across AAPL / MSFT / TM 2026-05 payloads), else the first exhibit value, else None. Direct CDN link to the filing HTML on Yahoo's mirror — no auth required. **8-K caveat:** points at the 8-K form itself (a thin SEC wrapper) rather than the typical `EX-99.1` press release that contains the substantive announcement. Pull `exhibits["EX-99.1"]` directly when you want the press release text. |
| `exhibit_count` | int | Number of exhibits attached to this filing. 0 for filings with no exhibits dict (rare). Always integer (never null) so CSV consumers can sort / aggregate without coercion. |
| `exhibit_keys` | str | Pipe-joined list of exhibit keys in Yahoo's emission order (e.g. `"10-Q|EX-31.1|EX-31.2|EX-32.1"`). Empty string when no exhibits — chose empty over null so CSV consumers can do exact-match string filters (e.g. `exhibit_keys` containing `EX-99.1`) without nullable-string handling. Flat surface for tabular consumers that can't read the nested `exhibits` dict. Redundant with `exhibits.keys()` in JSON. |
| `exhibits` | dict / null | Map from exhibit type (`10-Q`, `EX-31.1`, `EX-99.1`, …) to direct CDN URL. Variable keys per filing — a 10-Q typically has 4 (the form + 3 certifications); an 8-K has 1–2 (the form + maybe a press release exhibit); a 10-K has 6+ (form + subsidiaries list + auditor consent + certifications). **JSON / NDJSON only** — dropped from CSV (no clean tabular shape; use `exhibit_keys` + `primary_url` for tabular access). |

### `--summary` mode

Per ticker (real AAPL + TM fetch in 2026-05):

```json
[
  {
    "symbol": "AAPL",
    "total_filings": 76,
    "latest_date": "2026-05-01",
    "latest_type": "10-Q",
    "latest_10k_date": "2025-10-31",
    "latest_10q_date": "2026-05-01",
    "latest_8k_date": "2026-04-30",
    "latest_20f_date": null,
    "latest_6k_date": null,
    "latest_proxy_date": "2026-01-08",
    "latest_proxy_type": "DEF 14A",
    "filings_last_90d": 4
  },
  {
    "symbol": "TM",
    "total_filings": 120,
    "latest_date": "2026-05-08",
    "latest_type": "6-K",
    "latest_10k_date": null,
    "latest_10q_date": null,
    "latest_8k_date": null,
    "latest_20f_date": "2023-06-30",
    "latest_6k_date": "2026-05-08",
    "latest_proxy_date": null,
    "latest_proxy_type": null,
    "filings_last_90d": 5
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `total_filings` | int | Count of filings Yahoo returned (pre-filter, pre-limit). 0 for empty / errored tickers. Always integer. |
| `latest_date` | str / null | Most recent filing date across all types. Computed by `max()` over filing dates — does not assume Yahoo's sort order. None when `total_filings == 0`. |
| `latest_type` | str / null | Filing type of the most-recent filing (matches `latest_date`). For US issuers active monthly this is usually `8-K`; for ADRs `6-K`. **Tie-breaking is non-deterministic**: when two filings share the same date (rare but possible), `max()` returns one of them based on Yahoo's iteration order — don't rely on a specific type winning. |
| `latest_10k_date` | str / null | Most recent 10-K (annual report — US issuer). `null` for ADRs (use `latest_20f_date` instead) and for issuers that haven't filed a 10-K within Yahoo's ~4-year row-bounded window. |
| `latest_10q_date` | str / null | Most recent 10-Q (quarterly report — US issuer). `null` for ADRs (which file 6-K interim reports instead). |
| `latest_8k_date` | str / null | Most recent 8-K (event / material change — US issuer). `null` for ADRs. |
| `latest_20f_date` | str / null | Most recent 20-F (annual report — foreign issuer ADR). `null` for US issuers. Note: 20-F has a long filing window — TM's most-recent 20-F as of 2026-05 dates to 2023-06-30. |
| `latest_6k_date` | str / null | Most recent 6-K (interim report — foreign issuer ADR). `null` for US issuers. |
| `latest_proxy_date` | str / null | Most recent **proxy** filing — covers both `DEF 14A` (definitive proxy statement, annual) AND `DEFA14A` (definitive additional proxy materials, supplemental). Picks the max date across both forms; same governance cycle, distinct Yahoo form codes. `null` for ADRs (which don't file DEF 14A) and most non-equity. For just the formal annual proxy, use default mode + `--type "DEF 14A"`. |
| `latest_proxy_type` | str / null | Companion to `latest_proxy_date` — the Yahoo form code (`"DEF 14A"` or `"DEFA14A"`) that won the bucket-max. Useful for distinguishing "annual definitive proxy was just filed" from "company filed supplemental materials between proxies". `null` when `latest_proxy_date` is `null`. Only emitted for multi-form buckets — single-form buckets like 10-K don't get a `_type` companion since the type would always equal the bucket constant. |
| `filings_last_90d` | int | Count of filings dated within the last 90 days (UTC today minus 90d, inclusive lower bound). 0 for empty tickers (a known answer, not unknown — distinct from the `latest_*_date` fields which are null when truly unknown). Useful as a recency / activity signal — a US issuer with `>= 5` is in heavy event-filing mode (M&A, mat. changes, earnings), `<= 1` is quiet. |

**Naming-convention asymmetry note.** Single-form buckets (`10-K`,
`10-Q`, `8-K`, `20-F`, `6-K`) use the form name directly
(`latest_10k_date`). The proxy bucket — currently the only multi-form
bucket — uses a category name (`latest_proxy_date`) and adds a
`latest_proxy_type` companion to recover the form. Naming is
asymmetric on purpose: form names are the well-known surface for
single-form cases; category names disambiguate the bucket scope when
multiple forms are folded together. New multi-form buckets (if any
added later) will follow the proxy pattern (`latest_<category>_date`
+ `latest_<category>_type`).

## Presenting sec_filings results

Default presentation (single ticker, 1–5 filings): markdown table with
columns `Date | Type | Title | Link`. Use `primary_url` for the link
target — it's the direct doc, not the redirector. Drop `exhibits` from
the table; mention "and N exhibits" inline if `exhibit_count > 1` and
the user might want them.

```markdown
| Date | Type | Title | Link |
|---|---|---|---|
| 2026-05-01 | 10-Q | Periodic Financial Reports | [Read](primary_url) |
| 2026-04-30 | 8-K | Corporate Changes & Voting Matters | [Read](primary_url) |
```

For "what's the latest 10-K of X" prose answers, lean on the
`--summary` projection: `latest_10k_date` is the answer, and the user
can drill into the doc via a follow-up `--type 10-K --limit 1` call.

For multi-ticker peer compares ("filing activity across NVDA / AMD /
AVGO this quarter"), use `--summary` and present `total_filings` /
`filings_last_90d` / `latest_8k_date` side by side. The 90-day count
is the cleanest peer-comparable activity signal.

For ADR + US issuer mixed cohorts, both `latest_10k_date` and
`latest_20f_date` are populated across the result set (different
tickers populate different fields). Render both columns; users can
filter visually.

**Don't render `edgar_url` directly to users.** It points to Yahoo's
wrapper UI, which adds noise around the actual filing. Use
`primary_url` (direct CDN HTML) as the click target. `edgar_url` is
useful programmatically (it identifies the filing accession number)
but not for human reading.

<a id="filter_note-semantics"></a>
## `filter_note` semantics

`filter_note` is a result-level field set by `_apply_filters` when
the ticker fetched successfully but `--type` / `--since` / `--days`
reduced the filings list to zero. It's **mutually exclusive with
`note`** at the result level — together with `error` they form a
3-way disjoint set:

| Path | Field set | Cause | Action |
|---|---|---|---|
| Fetch failed | `error` + `error_kind` | rate_limit / not_found / network / unknown | Retry (rate_limit / network) or move on (not_found) |
| Yahoo returned no data | `note` | non-US primary listing / non-equity / bogus / no SEC coverage | Chain `fast_info` to disambiguate |
| Filters ate all rows | `filter_note` | type / date filter excluded everything (e.g. `--type 10-K` on TM) | Drop the filter to see the full list, or accept empty as the answer |

The `filter_note` string names the **specific filter that took the
count from positive to zero**, not all applied filters. Example:
`"120 filings fetched, all eliminated by --type 10-K (filings exist;
filter excluded everything)"`. When multiple filters are applied
(e.g. `--since X --type Y`) and the date floor zeros the list first,
the note names `--since X` even if `--type Y` is also active —
points the user at the exact knob to relax.

In CSV / NDJSON output the field appears as a column / key alongside
`note`, exactly one of which is populated for any given ticker. JSON
default mode shows it inline on the result dict.

`filter_note` is NOT carried through `--summary` mode — summary
metrics describe Yahoo's full response (the filters are no-ops in
summary mode by design), so a filter-to-empty case can't even arise
there.

## Mode-specific caveats

- **`exhibits` dict has variable keys per filing.** A 10-Q has
  `{10-Q, EX-31.1, EX-31.2, EX-32.1}` (4 keys); an 8-K has `{8-K}` or
  `{8-K, EX-99.1}` (1–2 keys); a 10-K has 6+ keys (form + subsidiaries
  + auditor consent + certifications). The script doesn't normalize
  these — Yahoo's keys pass through verbatim. Don't expect a fixed
  schema across filings. Use `exhibit_keys` (pipe-joined string) for
  flat tabular access.
- **`primary_url` heuristic isn't perfect.** The script picks
  `exhibits[type]` if present (the convention), else the first exhibit
  by insertion order. Verified to match the main doc for 10-K / 10-Q /
  20-F / 6-K across AAPL / MSFT / TM 2026-05 payloads, but the rule
  is a poor fit for **8-K**: `exhibits["8-K"]` is the form itself
  (often a thin SEC wrapper), while `exhibits["EX-99.1"]` is by
  convention the press release containing the substantive
  announcement. The script still picks the form (consistency with
  other types > form-specific override), but consumers wanting the
  press release should pull `exhibits["EX-99.1"]` directly when
  present.
- **`--type` is case-insensitive.** User input `10-k`, `10-K`,
  `Def 14a` all match Yahoo's `10-K`, `DEF 14A` after upper-case
  normalization. Whitespace WITHIN a type is preserved (`DEF 14A` is
  two tokens internally). Whitespace AROUND comma separators is
  stripped.
- **`--type` filtering to empty emits a `filter_note` carry row.**
  When a ticker fetched successfully but `--type` / `--since` /
  `--days` excluded every filing (e.g. `--type 10-K` against TM, an
  ADR that files 20-F instead), CSV / JSON emit a single carry row
  with `filter_note` populated explaining the cause and listing
  which filters were applied. Mutually exclusive with `note` (which
  is reserved for "Yahoo returned no data at all"). See
  [filter_note semantics](#filter_note-semantics) above.
- **Yahoo's filing list is bounded by row count, not time.** The
  endpoint returns ~75–120 rows per ticker. For active large-caps
  this translates to a ~4-year window (verified 2026-05: AAPL 76
  rows back to 2022-01-27, MSFT 76 rows back to 2022-01-25, TM 120
  rows back to 2022-02-03). Quieter issuers reach further back; very
  active issuers may have a tighter window. Older filings (e.g.
  AAPL's 2018 10-K) aren't accessible — for full historical filing
  access, use SEC EDGAR directly. yfinance is a recency window, not
  a complete archive.
- **`count` is preserved through filters.** The result-level `count`
  field is set once by `fetch()` to the size of Yahoo's response and
  is **not mutated** by `_apply_filters` — so a 76-filing AAPL fetch
  keeps `count: 76` even when `--type 10-K --limit 1` shrinks
  `filings` to one row. To recover the displayed count, use
  `len(result["filings"])` or `total_filings` in `--summary` mode.
  Rationale: `count` is the only place the user can recover Yahoo's
  original response size in default mode, and silently losing it
  when filters apply would mask "ticker has data" vs "ticker has
  data but my filter ate it" (now also surfaced via `filter_note`,
  but the explicit count remains useful).
- **Order is descending by date in observed payloads.** Default-mode
  output preserves Yahoo's order (most recent first). The
  `--summary` `latest_*_date` computations don't rely on this — they
  use `max()` — but the CSV / JSON list order does. If you need
  oldest-first, sort consumer-side by `date`.
- **HTTP 404 is logged to stderr for empty cases.** yfinance's
  underlying call logs `HTTP Error 404: ...` via its internal logger
  when the SEC-filings module is missing for a ticker — visible on
  stderr but does NOT raise. Same noise pattern as `holders` /
  `insiders` for non-equity tickers; the script handles the empty
  return value (`{}`) and emits success-with-`note`. Suppress at the
  shell level if needed (`2>/dev/null`).
- **Date sourcing.** `date` prefers Yahoo's pre-formatted string
  (always observed as YYYY-MM-DD); falls back to converting
  `epochDate` (UTC) if malformed. Both fields are always present in
  current payloads, so the fallback is insurance against future drift.
- **No batched fetch.** Each ticker is one HTTP. Per-ticker latency
  is ~0.7–1.5s (one `quoteSummary` call), so a 10-ticker batch costs
  ~7–15s sequentially. No parallelism — same as `holders` / `insiders`
  / `analyst`.
- **Cache hint dropped.** Yahoo's `maxAge` field per-filing is an
  internal cache TTL; we drop it (no consumer use).
