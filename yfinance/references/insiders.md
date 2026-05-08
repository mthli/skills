[← back to SKILL.md](../SKILL.md)

# `insiders` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`insiders.py --summary AAPL TSLA QQQ BMW.DE BOGUS123` if you suspect
upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting insiders results](#presenting-insiders-results) · [Mode-specific caveats](#mode-specific-caveats)

Insider data for one or more tickers — three sections per ticker:

- **`purchases_summary`** (rollup from `Ticker.insider_purchases`) —
  6-month rolling totals: shares purchased / sold / net + transaction
  counts + total insider holdings + % of holdings traded.
- **`transactions`** (from `Ticker.insider_transactions`) — the last
  ~24 months of Form 4 events: insider name, position, ownership type
  (D/I), shares, dollar value, date, transaction text. Often 50–100
  rows for active large-caps (AAPL: 73, TSLA: 96 in 2026-05).
- **`roster`** (from `Ticker.insider_roster_holders`) — current
  insiders with shares owned directly and (when present) indirectly.
  Typically ~10 rows.

All three properties **share one Yahoo backend HTTP call**. Verified by
timing (2026-05): cold fetch on AAPL was ~1.2s for the first read,
~0ms each for the next two — consistent with yfinance issuing one
`quoteSummary` request and caching all three modules on the `Ticker`
instance. Same single-call pattern as `holders` (which talks to a
different `quoteSummary` module group). Implication: there's no
latency saving from a hypothetical `--scope` flag; the script doesn't
expose one. Use `--summary` if you want smaller output.

**Equity-focused.** ETFs / mutual funds / indexes / crypto / FX /
futures all return three empty DataFrames. Bogus / delisted tickers
ALSO return three empty DataFrames. The all-empty case is genuinely
ambiguous — see [All-empty is ambiguous](#empty-ambiguous) below.
**Partial empty is different**: `BMW.DE` and `TM` (Toyota ADR) have
a populated `purchases_summary` rollup but empty `transactions` +
`roster` — that's a real equity with thin Yahoo coverage of the
per-event endpoints, not ambiguity. The script surfaces this in-band
via a separate `coverage_note` field (mutually exclusive with `note`)
so consumers don't silently misread the empty event lists as "no
activity". See [coverage_note semantics](#coverage-note) below.

## Run

```bash
# Default: full sections, pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py AAPL

# Peer rollup: flat per-ticker dict (~5–10× smaller than default)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --summary AAPL MSFT GOOGL

# Cap to top 10 transactions / roster rows (purchases_summary unaffected)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --limit 10 AAPL

# CSV — default mode emits one row per record (with record_class discriminator)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --format csv AAPL

# CSV — summary mode emits strict one row per ticker (peer-comparison table)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/insiders.py --format csv --summary AAPL MSFT 0700.HK
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap transactions / roster rows per ticker. Default:
  keep all (Yahoo returns up to ~24 months of transactions, often
  70+ rows for active large-caps; ~10 roster rows). Does not affect
  the `purchases_summary` rollup.
  **Silently ignored in `--summary` mode.** The flat metrics
  (`transactions_returned`, `roster_returned`,
  `latest_transaction_date`) are computed from Yahoo's full response
  so they describe the data, not the display knob — and stay
  invariant under `--limit` by design. Same contract as
  `holders.py`'s `--limit`.
- `--summary` — flat per-ticker projection. Lifts the rollup fields
  to top level + adds returned-row counts + the most recent
  transaction date. Same network cost as default mode (post-fetch
  projection); use it to save context tokens.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array, one record per ticker. `ndjson` emits one JSON
  record per ticker per line. `csv` shape depends on mode:
  - **default mode** — one row per record, with a `record_class`
    discriminator column whose values are `purchases` (one rollup
    row per ticker) / `transaction` / `roster`. Section-specific
    columns are populated only on rows of that class. `position`
    and `url` columns are shared across `transaction` and `roster`
    rows (deduplicated in the header — both classes semantically
    have a position/url). Empty / errored tickers emit a single
    row carrying `symbol` + `note` + meta fields. Partial-empty
    tickers (`BMW.DE`, `TM`) emit their data rows normally AND
    carry the `coverage_note` string in the `coverage_note`
    column on every row of that ticker (since the note describes
    the whole ticker, not any single record). `note` and
    `coverage_note` are mutually exclusive at the result level —
    one or the other, never both.
  - **`--summary` mode** — strict one row per ticker (every column
    populated where the ticker has data). Same row shape across all
    tickers, friendly for spreadsheet pivots.

CSV column order (default mode, left to right): `symbol`,
`record_class`, the 11 purchases-rollup columns, the 9
transaction-row columns, the 7 roster-only columns (after dedupe),
`note`, `coverage_note`, then the 3 meta fields (`error`,
`error_kind`, `attempts`).

## Output schema

### Default mode

Per ticker (illustrative; real AAPL fetch in 2026-05):

```json
[
  {
    "symbol": "AAPL",
    "purchases_summary": {
      "period_label": "Last 6m",
      "purchases_shares": 346569,
      "purchases_count": 15,
      "sales_shares": 100237,
      "sales_count": 4,
      "net_shares_purchased": 246332,
      "net_count": 19,
      "total_insider_shares_held": 240872640,
      "pct_net_shares_purchased": 0.001,
      "pct_buy_shares": 0.001,
      "pct_sell_shares": 0.0
    },
    "transactions": [
      {
        "date": "2026-04-23",
        "insider": "PAREKH KEVAN",
        "position": "Chief Financial Officer",
        "ownership": "D",
        "shares": 1534,
        "value": 421850.0,
        "transaction_text": "Sale at price 275.00 per share.",
        "transaction_code": null,
        "url": null
      }
    ],
    "roster": [
      {
        "name": "ADAMS KATHERINE L",
        "position": "General Counsel",
        "most_recent_transaction": "Stock Gift",
        "latest_transaction_date": "2025-11-12",
        "shares_owned_directly": 175408,
        "position_direct_date": "2025-11-12",
        "shares_owned_indirectly": null,
        "position_indirect_date": null,
        "url": null
      }
    ]
  }
]
```

#### `purchases_summary` (11 fields, all from `insider_purchases`)

`insider_purchases` is a 7-row × 3-col metric table that we project
into a flat dict. The first column header carries the rollup window
(e.g. `"Insider Purchases Last 6m"` → `period_label: "Last 6m"`).

| Field | Type | Notes |
|---|---|---|
| `period_label` | str / null | The rollup window — extracted from Yahoo's first-column header. Currently `"Last 6m"` for all observed tickers; pin if you ever see a different value. |
| `purchases_shares` | int / null | Total shares purchased by insiders in the window. |
| `purchases_count` | int / null | Number of buy transactions in the window. |
| `sales_shares` | int / null | Total shares sold. |
| `sales_count` | int / null | Number of sell transactions. |
| `net_shares_purchased` | int / null | `purchases_shares - sales_shares`. Negative when sales exceed buys. |
| `net_count` | int / null | `purchases_count + sales_count` (yes, sum, not difference — it's the total transaction count). |
| `total_insider_shares_held` | int / null | Aggregate shares held by all insiders right now. The denominator the next three "%" fields use. |
| `pct_net_shares_purchased` | float / null | **FRACTION** (`0.001` = 0.1%, NOT 0.001%). Verified empirically (AAPL 2026-05): net=246332 / total_held=240872640 ≈ 0.00102. Multiply ×100 for display. |
| `pct_buy_shares` | float / null | **FRACTION** of total holdings represented by purchases. |
| `pct_sell_shares` | float / null | **FRACTION** of total holdings represented by sales. |

#### `transactions` row (9 fields, from `insider_transactions`)

Form 4 events from the last ~24 months. Yahoo sorts most-recent-first
in current responses, but don't rely on order — derive the latest
date with `max()` if it matters.

| Field | Type | Notes |
|---|---|---|
| `date` | str / null | YYYY-MM-DD. The Form 4 `Start Date` (transaction date, not filing date). |
| `insider` | str / null | Insider name as Yahoo emits it (uppercase, e.g. `"COOK TIMOTHY D"`). |
| `position` | str / null | Role at the company at the time of the transaction (`"Chief Executive Officer"`, `"Officer"`, `"Director"`, ...). |
| `ownership` | str / null | Single letter — `"D"` direct, `"I"` indirect (held via a trust / foundation / family LLC). Yahoo emits the letter, not the word; we pass it through verbatim. |
| `shares` | int / null | Number of shares transacted. Always populated when row is present. |
| `value` | float / null | Dollar value of the transaction in the ticker's **TRADING currency** (USD for AAPL, HKD for `0700.HK`, EUR for `BMW.DE`). Yahoo emits `NaN` for non-monetary events (option grants, RSU vests, gifts) — projects to `null`. |
| `transaction_text` | str / null | Human-readable description, e.g. `"Sale at price 275.00 per share."` or `"Sale at price 251.25 - 256.00 per share."` (range notation when filled in slices). Sometimes empty. |
| `transaction_code` | str / null | Yahoo's `Transaction` column (presumed coded type string), preserved for forward compat; **empirically empty in 2026-05 responses** (0/73 AAPL rows had a value). Kept in the schema so a future Yahoo change surfaces rather than gets dropped silently. Renamed from a bare `transaction` to remove ambiguity with `transaction_text` — the `_code` suffix makes the role explicit. |
| `url` | str / null | Yahoo's column for a detail link; **empirically empty**. Same rationale as `transaction_code`. |

#### `roster` row (9 fields, from `insider_roster_holders`)

Current insiders. Some tickers expose only direct holdings (AAPL has
7 columns, no indirect); others (TSLA) expose 9 columns including
the indirect pair. We project both — fields that don't exist in the
DataFrame come through as `null`.

| Field | Type | Notes |
|---|---|---|
| `name` | str / null | Insider name (uppercase, e.g. `"MUSK ELON REEVE"`). |
| `position` | str / null | Current role (`"Chief Executive Officer"`, `"Director"`, ...). |
| `most_recent_transaction` | str / null | Type of the latest event for this insider — `"Sale"`, `"Purchase"`, `"Stock Gift"`, ... Different from the transactions section's `transaction_text`. |
| `latest_transaction_date` | str / null | YYYY-MM-DD. Date of the most recent event. |
| `shares_owned_directly` | int / null | Shares held directly. `null` for insiders whose only holdings are indirect (TSLA's Gebbia / Murdoch / Musk all show `null` here). |
| `position_direct_date` | str / null | YYYY-MM-DD. Date the direct-position figure is as-of. |
| `shares_owned_indirectly` | int / null | Shares held indirectly (via a trust / foundation / family LLC). `null` when not present in payload — either the insider has no indirect holdings, OR the ticker's roster doesn't expose indirect cols at all (AAPL doesn't; TSLA does). |
| `position_indirect_date` | str / null | YYYY-MM-DD. Same as-of caveat. |
| `url` | str / null | Yahoo's per-insider link column; empirically empty. |

### `--summary` mode

Flat per-ticker dict (rollup + transaction-list rollup + top roster
holder):

```json
[
  {
    "symbol": "AAPL",
    "period_label": "Last 6m",
    "purchases_shares": 346569,
    "purchases_count": 15,
    "sales_shares": 100237,
    "sales_count": 4,
    "net_shares_purchased": 246332,
    "net_count": 19,
    "total_insider_shares_held": 240872640,
    "pct_net_shares_purchased": 0.001,
    "pct_buy_shares": 0.001,
    "pct_sell_shares": 0.0,
    "transactions_returned": 73,
    "latest_transaction_date": "2026-04-23",
    "roster_returned": 10,
    "top_insider_by_direct_shares": "LEVINSON ARTHUR D",
    "top_insider_direct_shares": 4125580
  }
]
```

The 11 `purchases_summary` keys are lifted to top level. Five extras:

- `transactions_returned` — number of transaction rows Yahoo returned
  (computed from the FULL pre-limit list). Distinct from the
  `purchases_count + sales_count` rollup figure: the rollup is only
  the last 6 months, while the transactions list goes back ~24
  months, so this number is typically much larger.
- `latest_transaction_date` — YYYY-MM-DD, the max `date` across all
  transactions. Useful as a recency signal: stale insider activity
  on a busy stock often correlates with blackout windows.
- `roster_returned` — number of roster rows Yahoo returned (~10
  typical, capped by Yahoo).
- `top_insider_by_direct_shares` — name of the insider with the
  largest `shares_owned_directly` from the roster (verified AAPL
  2026-05: `"LEVINSON ARTHUR D"` at 4.13M direct shares — chairman,
  beats CEO Cook's 3.28M). Mirrors holders' `top_institution`
  peer-compare signal.
- `top_insider_direct_shares` — the share count that goes with the
  name above. Indirect holdings are deliberately excluded from this
  ranking (TSLA's Musk holds 932M+ shares INDIRECTLY through a
  trust; mixing direct + indirect would silently make him dominate
  any peer compare he appears in). For "largest insider by
  combined holdings", compute it caller-side from the full default-
  mode response.

### Empty / non-applicable result

When all three sections come back empty, the response is success
with a `note` rather than an error. See
[All-empty is ambiguous](#empty-ambiguous) below.

```json
{
  "symbol": "QQQ",
  "purchases_summary": { "period_label": null, "purchases_shares": null, "...": null },
  "transactions": [],
  "roster": [],
  "note": "no insider data (Yahoo's insider endpoints cover operating-company equities; ETFs / indexes / crypto / FX / futures and bogus tickers all return three empty frames — call fast_info to disambiguate)"
}
```

**Partial empty — see [coverage_note semantics](#coverage-note)
below.** Some real equities — verified for `BMW.DE` and `TM`
(Toyota ADR) in 2026-05 — return a populated `purchases_summary`
(typically with mixed null/zero counts) but empty `transactions` +
`roster`. The script surfaces this in-band via a separate
`coverage_note` field rather than `note`, so consumers can tell
apart "no data, ambiguous cause" (`note`) from "real data,
asymmetric Yahoo coverage" (`coverage_note`).

```json
{
  "symbol": "BMW.DE",
  "purchases_summary": { "period_label": "Last 6m", "total_insider_shares_held": 304778400, "...": "..." },
  "transactions": [],
  "roster": [],
  "coverage_note": "purchases_summary populated but transactions + roster empty — typical for non-US issuers / ADRs where Yahoo aggregates the 6-month rollup but doesn't expose per-event filings (the empty lists ARE the answer, not a fetch failure)"
}
```

In CSV, the same partial-empty ticker emits one `purchases` row
(no transaction / roster rows since both lists are empty), with
the `coverage_note` column carrying the full note string and the
`note` column empty (mutually exclusive):

```
symbol,record_class,period_label,...,total_insider_shares_held,...,note,coverage_note,error,error_kind,attempts
BMW.DE,purchases,Last 6m,...,304778400,...,,"purchases_summary populated but transactions + roster empty — typical for non-US issuers / ADRs where Yahoo aggregates the 6-month rollup but doesn't expose per-event filings (the empty lists ARE the answer, not a fetch failure)",,,
```

(intermediate columns elided for readability — the row has values
for the purchases-rollup columns Yahoo provided, with empties for
the transaction / roster columns. Some rollup columns can be null
even on the partial-empty path: BMW.DE's `sales_shares` is null
while `sales_count` is 0 — see the "Partial-empty rollups can
have asymmetric null/zero columns" caveat below.)

### Failed result

```json
{
  "symbol": "AAPL",
  "error": "fetch failed (rate_limit, after 3 attempt(s))",
  "error_kind": "rate_limit",
  "attempts": 3
}
```

See SKILL.md "Cross-cutting caveats" for retry semantics and the full
`error_kind` enum.

## Presenting insiders results

**Multiply fractions ×100 when displaying.** All `pct_*` fields are
fractions. Render `0.001` as `0.1%`, not `0.001%`. Same convention
as `holders` and `info` margin/growth fields.

**Single-ticker rollup.** Lead with the 6-month buy/sell summary,
then the most recent 3–5 transactions, then the top roster members
by direct holdings. **Yahoo's `roster` is NOT pre-sorted by holding
size** (observed: alphabetical-ish in current responses). Sort by
`shares_owned_directly` descending (skipping `null` rows) before
slicing to top-N. Same caveat applies if you want to combine direct
+ indirect — sort by `(shares_owned_directly or 0) +
(shares_owned_indirectly or 0)` and remember that mixing makes
`null` semantically tricky.

Format template (placeholders — illustrative shape):

> **\<TICKER\> — insider activity (\<period_label\>)**
> - Net: **\<+/-N\>** shares (\<pct_net_shares_purchased ×100\>%
>   of holdings) across \<purchases_count\> buys, \<sales_count\>
>   sells.
> - Total insider holdings: \<total_insider_shares_held\> shares.
> - **Recent transactions** (last 30 days):
>   - \<YYYY-MM-DD\> — \<insider\> (\<position\>): \<Sale/Purchase\>
>     \<shares\> sh @ \<price-from-text\> ≈ \<currency\> \<value\>
>   - ...
> - **Top current insiders** (sort `roster` by
>   `shares_owned_directly` desc first):
>   - \<name\> (\<position\>) — \<shares_owned_directly\> sh
>   - ...

State the currency on `value`. For US tickers `\$` is fine; for
`0700.HK` write `HK\$`, for `BMW.DE` write `€`. If you don't know
the trading currency, omit `value` rather than guessing — call
`fast_info` first if needed.

**Multi-ticker peer compare.** Use `--summary`; render as a table
with columns: ticker | net shares | net % | buy count | sell count |
last transaction. The sign of `net_shares_purchased` is the headline
signal — positive flags net insider buying, negative flags net selling.
Don't compare absolute share counts across tickers without normalizing
to `pct_net_shares_purchased` (a 246k buy on AAPL ≠ a 246k buy on a
small-cap).

**Date formatting.** `date` and `latest_transaction_date` are already
YYYY-MM-DD. Mix of dates is normal across an insider list — different
people file independently. Don't pretend they're a single snapshot.

**Escape `$` as `\$` in prose** — same rationale as the other modes.

## Mode-specific caveats

- <a id="empty-ambiguous"></a>**All-empty is ambiguous (full discussion).**
  yfinance returns three empty DataFrames in all of these cases.
  Verified empirically (2026-05):
  - ETF (`QQQ`)
  - Index (`^GSPC`)
  - Crypto (`BTC-USD`)
  - Bogus / delisted ticker (`ZZZZNOTREAL`)
  
  yfinance prints `HTTP Error 404` to stderr for these but does not
  raise. We can't distinguish them at the insider endpoints — same
  signal-free empty payload as `holders`. We deliberately don't
  promote empty to `error_kind: not_found` (a low-coverage equity
  isn't an error) and instead emit success with a `note` carrying
  the disambiguation hint. To resolve which case you're in, **call
  `fast_info`** on the same symbol — it returns `quote_type` for
  valid tickers and `not_found` for bogus.

- <a id="coverage-note"></a>**`coverage_note` semantics
  (full discussion).** Two ambiguity-handling fields, mutually
  exclusive at the result level:
  - `note` — fires when **no data** is available and the cause is
    ambiguous (non-equity / bogus / low-coverage). Caller should
    chain `fast_info` to disambiguate. See
    [All-empty is ambiguous](#empty-ambiguous).
  - `coverage_note` — fires when **rollup data IS available** but
    `transactions` + `roster` are both empty. Verified (2026-05)
    for `BMW.DE` (canonical shape — real rollup numbers, e.g.
    `total_insider_shares_held: 304M`) and `TM` (Toyota ADR —
    same partial-empty branch but Yahoo's rollup data for TM is
    degraded to all-zeros / nulls, so the rollup row is mostly
    empty cells with `period_label` filled in). Both cases
    categorize the same way — Yahoo aggregates whatever it can
    match for the rollup but doesn't expose per-event tables for
    many non-US issuers / ADRs. The empty event lists ARE the
    answer (not a transient gap, not a fetch failure). For
    presenting partial-empty results, prefer BMW.DE-grade
    tickers; for TM-grade tickers, treat the rollup as
    "Yahoo has nothing useful" rather than "literal zero
    insider activity". If you need per-event detail and Yahoo doesn't
    have it, you'd need a different data source (e.g., national
    filings registry).

  Three result classes you may see, in increasing data
  completeness:
  1. All-empty → `note` set, `coverage_note` absent.
  2. Partial-empty (purchases-only) → `coverage_note` set, `note`
     absent.
  3. Full or near-full data → neither field present.

  Both fields serialize as columns in default-mode and `--summary`-
  mode CSVs, so neither category drops out of tabular output.

- **All `pct_*` fields are FRACTIONS, not percentages.** Re-stating
  because this is the most likely mistake. `pct_net_shares_purchased:
  0.001` means **0.1% of total insider holdings**, NOT 0.001%.
  Multiply ×100 for display. Verified empirically (AAPL 2026-05):
  net=246332 / total_held=240872640 ≈ 0.00102. Same convention as
  `holders.summary.insiders_pct` and `info`'s margin / growth fields.

- **`value` is in trading currency.** Same caveat as
  `holders.value`. For ADRs (TM, BABA) and non-US-listed tickers
  (`0700.HK`, `BMW.DE`) the dollar value is in the listing
  currency, not USD. Mixing tickers from different exchanges in one
  summary table without converting will produce apples-to-bananas
  comparisons.

- **`value` is `null` for non-monetary events.** Option grants, RSU
  vests, and gifts have a `shares` count but no `value` (Yahoo emits
  NaN). Sum `value` across transactions only when you want
  cash-equivalent flow; sum `shares` to get share-count flow. For a
  buy/sell magnitude headline, `purchases_summary.purchases_shares`
  / `sales_shares` is the right field — those are pre-aggregated
  share counts and don't need to dodge NaN values.

- **`ownership` is single-letter D/I, not the full word.** `"D"` =
  direct, `"I"` = indirect (held through a trust / foundation /
  family LLC / similar entity). Yahoo emits the letter; we pass it
  through verbatim. Don't render `"D"` to a user — translate to
  `"direct"` / `"indirect"` (or omit) when presenting.

- **Roster column set varies by ticker.** Verified (2026-05): AAPL
  exposes 7 columns (no indirect holdings present); TSLA exposes 9
  (Musk's 932M+ shares are indirect through a trust). The script
  projects both pairs (`shares_owned_indirectly` /
  `position_indirect_date`); when the underlying DataFrame doesn't
  have those columns, the projected values are `null`. Don't read a
  `null` `shares_owned_indirectly` as "this insider has no indirect
  holdings" — it could mean that, OR it could mean Yahoo's payload
  for this ticker doesn't expose the indirect columns at all.
  Cross-check by looking at any other roster row for the same
  ticker: if every row has `shares_owned_indirectly: null` and the
  ticker is a US large-cap, it's probably the missing-column case.

- **`transaction_code` and `url` columns are usually empty.**
  Verified (2026-05): 0/73 AAPL transaction rows had a non-empty
  `transaction_code` or `url` field. Yahoo seems to be in the
  middle of deprecating these (the `transaction_text` field
  carries the human description). Kept in the schema so a future
  Yahoo change that re-populates them surfaces rather than gets
  silently dropped. If you're presenting transactions, lead with
  `transaction_text` and ignore `transaction_code` until empirical
  evidence shows it's populated.

- **`date` is the transaction date, not the filing date.** Form 4
  filings happen up to 2 business days after the trade. So a "May
  3" transaction date might have been filed May 5 — but yfinance's
  `Start Date` (which we project as `date`) is May 3. There's no
  separate filing-date field exposed. For "what insiders did THIS
  WEEK", `date >= today - 5 business days` is the right filter;
  for "what was disclosed this week", you'd need a different data
  source.

- **Partial-empty rollups can have asymmetric null/zero columns.**
  Verified for `BMW.DE` in 2026-05: `purchases_summary` came back
  with `sales_shares: null` but `sales_count: 0` (and similar
  null/zero pairs across the rollup). That's a Yahoo data quirk —
  some non-US tickers expose the transaction count without the
  share total — not a projection bug. When summing across tickers,
  treat `null` as "data unavailable" (skip it) rather than "zero
  shares" (which `null` is NOT semantically equivalent to). The
  7 rollup rows can independently be null/integer/zero in any
  combination, so always null-guard before arithmetic.

- **Three properties share one HTTP call.** Verified (2026-05) by
  timing: AAPL cold fetch was 1190 ms / 0 ms / 0 ms across
  `insider_purchases` / `insider_transactions` /
  `insider_roster_holders`. Same pattern as `holders` — all three
  modules come from one `quoteSummary` request and are cached on
  the `Ticker` instance. No latency saving from a hypothetical
  `--scope` flag; the script doesn't expose one.

- **Hard cap of ~24 months of transactions.** Yahoo's
  `insider_transactions` covers the last roughly 24 months of Form
  4 events. For deeper history you'd need a different source
  (EDGAR's Form 4 archive, OpenInsider, etc.).

- **Roster is current insiders only.** People who left the company
  no longer appear in `insider_roster_holders`, even though their
  prior transactions remain in `insider_transactions`. So a
  transaction whose `insider` doesn't appear in `roster` either (a)
  was filed by someone who has since left, or (b) was filed by
  someone who never met the "officer / director / 10%-owner"
  threshold for inclusion in the roster. Don't treat the
  intersection of the two lists as authoritative.
