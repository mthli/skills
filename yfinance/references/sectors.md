[← back to SKILL.md](../SKILL.md)

# `sectors` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`sectors.py technology --section overview` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Per-section schemas](#per-section-schemas) · [`--summary` rollups](#summary-rollups) · [Discovery flags](#discovery-flags) · [Presenting sector results](#presenting-sector-results) · [Mode-specific caveats](#mode-specific-caveats)

Browse Yahoo's curated sector / industry hierarchy:

```
11 sectors  →  ~150 industries  →  companies / ETFs / funds
```

One mode handles both `Sector` and `Industry` because the APIs are
~80% overlapping (overview, top_companies, research_reports). The
`--kind` flag picks the class; `auto` (default) infers from the key
— sector keys are a fixed set of 11 known to `yfinance.const`.

**Discovery axis distinct from `screener`.** Screener filters the
universe by user-defined predicates; sectors browses Yahoo's
hand-curated taxonomy. Sectors is the right tool for "what's the
breakdown of the technology sector" / "which industries roll up
under healthcare" / "top semiconductor stocks" — none of which
screener can answer (it doesn't expose Yahoo's hierarchy).

## Run

```bash
# Default: overview + top_companies for the technology sector (2 HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py technology

# Industry, full sections (autodetect kind)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  semiconductors --section all

# Sector with industries + ETFs + mutual funds
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  technology --section overview,industries,top_etfs,top_mutual_funds

# Peer compare across sectors (--summary auto-fetches all sections)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --summary technology healthcare financial-services

# Compare industries within a sector
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --summary semiconductors software-infrastructure consumer-electronics

# Industry top performers / growth-leaders only
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  semiconductors --section top_performing_companies,top_growth_companies

# Discovery: list the 11 sector keys (no HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --list-sectors

# Discovery: list industries within a sector (no HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --list-industries technology

# Discovery: list industries across multiple sectors (no HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --list-industries technology,healthcare

# Discovery: sibling industries of an industry (no HTTP)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  --peers semiconductors

# Drill-down chain: pick a sector's top industry, then top companies
SEC=technology
TOP_IND=$(uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  $SEC --section industries --limit 1 --format csv | tail -1 | cut -d, -f2)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/sectors.py \
  $TOP_IND --section top_companies --limit 5
```

## CLI arguments

```text
keys                    one or more sector / industry keys (positional);
                        omit when using --list-sectors / --list-industries

--kind {auto,sector,industry}
                        which class to instantiate; `auto` (default) infers
                        from the key — sector keys are a fixed set of 11

--section SECTION       comma-separated, case-insensitive, or `all`.
                        default: overview,top_companies (2 HTTP / key)
                        auto-expands to all-applicable when --summary is set
                        without an explicit --section

  Common sections:      overview, top_companies, research_reports
  Sector-only:          industries, top_etfs, top_mutual_funds
  Industry-only:        top_performing_companies, top_growth_companies

--limit N               cap row count of each list section (default: no cap)

--summary               flat per-key dict for peer compare; auto-expands
                        --section to all-applicable when not explicit

--full                  raw Yahoo payload (DataFrames as records, dicts
                        as-is); mutually exclusive with --summary

--list-sectors          dump the 11 sector keys + child industry counts
                        (no HTTP — local lookup from yfinance.const)

--list-industries [SECTOR[,SECTOR...]]
                        dump industry keys (optionally filtered to one
                        or more sectors, comma-separated)
                        (no HTTP — local lookup from yfinance.const)

--peers INDUSTRY        sibling industries within the parent sector
                        of <industry>, with `is_self` flag
                        (no HTTP — local lookup from yfinance.const)

--format {json,ndjson,csv}
                        output format. NDJSON / CSV use a `record_class`
                        discriminator (meta / top_company / industry /
                        top_performer / top_growth_company / top_etf /
                        top_mutual_fund / research_report)
```

## Output schema

Default mode: list of envelopes (one per key). Each envelope:

```json
{
  "key":         "technology",
  "kind":        "sector",
  "name":        "Technology",
  "symbol":      "^YH311",
  "sector_key":  null,                 // industry rows only
  "sector_name": null,                 // industry rows only
  "overview":    { ... },              // when requested (default: yes)
  "top_companies":            [ ... ], // when requested (default: yes)
  "industries":               [ ... ], // sector kind, when requested
  "top_etfs":                 [ ... ], // sector kind, when requested
  "top_mutual_funds":         [ ... ], // sector kind, when requested
  "top_performing_companies": [ ... ], // industry kind, when requested
  "top_growth_companies":     [ ... ], // industry kind, when requested
  "research_reports":         [ ... ], // when requested
  "coverage_note":            "...",   // section requested but inapplicable
  "section_errors":           { ... }  // per-section transient errors
}
```

Identity fields (`key`, `kind`, `name`, `symbol`, `sector_key`,
`sector_name`) come from yfinance's parser state populated by the
overview probe — no extra HTTP. `sector_key` / `sector_name` are
populated only when `kind == "industry"` (back-ref to parent).

Failed envelopes carry standard `error` + `error_kind` + `attempts`
fields — same convention as the per-ticker wrappers. `error_kind:
not_found` fires when Yahoo returns no data for a given key (typo,
wrong kind, or a key that's been retired).

## Per-section schemas

### `overview`

```json
{
  "companies_count":  825,           // int — count in this sector/industry
  "industries_count": 12,            // sector only; null on industry rows
  "market_cap":       26665082159104, // int — total market cap (USD-ish; Yahoo aggregates without a currency tag)
  "market_weight":    0.31782493,    // FRACTION of total US market (0.318 = 31.8%)
  "employee_count":   7964531,       // int — sum of employee counts
  "message_board_id": "INDEXYH311",
  "description":      "Companies engaged in..."
}
```

`market_weight` is a **fraction**, matching `holders.summary` /
`info` margins / `analyst.buy_pct_*` conventions. Multiply ×100 for
display.

### `top_companies`

DataFrame from Yahoo with index = symbol, projected to a list of
records:

```json
[
  {"symbol": "NVDA", "name": "NVIDIA Corporation", "rating": "Strong Buy", "market_weight": 0.19670111},
  {"symbol": "AAPL", "name": "Apple Inc.",         "rating": "Buy",        "market_weight": 0.16204797},
  ...
]
```

- `rating` ∈ {`Strong Buy`, `Buy`, `Hold`, `Underperform`, `Sell`} —
  capitalized + spaced. **Cross-mode warning:** `info.analyst.recommendation_key`
  is the same Yahoo field but emitted in snake_case (`strong_buy` /
  `buy` / `hold` / `underperform` / `sell`). Joining `sectors`
  `top_companies` rows against `info` results requires a
  `s.replace(" ", "_").lower()` step on either side. We don't
  normalize here because Yahoo serves the capitalized form on the
  sector endpoint — round-tripping it would lose fidelity.
- `market_weight` is a **fraction of the parent sector / industry**.
  Sector top_companies' weights sum to ~the sector's market_weight; an
  industry's top_companies sum to ~1.0 within the industry.
- Returned **~50 rows for sectors** (capped at 50 by Yahoo). For
  industries the row count tracks the industry's company population
  (verified 2026-05 across 8 industries: oil-gas-integrated=5,
  gold=12, apparel-manufacturing=15, utilities-regulated-electric=33,
  restaurants=35, semiconductors=47, biotechnology=50,
  asset-management=50; **observed range 5–50**). Use `--limit N` to
  cap.

### `industries` _(sector-only)_

DataFrame with index = industry key:

```json
[
  {"key": "semiconductors",          "name": "Semiconductors",            "symbol": "^YH31130020", "market_weight": 0.40628640},
  {"key": "software-infrastructure", "name": "Software - Infrastructure", "symbol": "^YH31110030", "market_weight": 0.19045636},
  ...
]
```

- `market_weight` is a **fraction of the parent sector** (industry's
  share of the sector). Weights across all industries in a sector
  sum to ~1.0.
- The `key` is the canonical industry key suitable for chaining back
  into `sectors.py <key>`.

### `top_etfs` / `top_mutual_funds` _(sector-only)_

Yahoo returns these as `{symbol: name}` dicts; we project to records
for shape consistency:

```json
[
  {"symbol": "VGT",  "name": "Vanguard Information Tech ETF"},
  {"symbol": "XLK",  "name": "State Street Technology Select "},
  ...
]
```

- ~10 entries per section (Yahoo's default; not configurable).
- `name` may be `null` for less-common funds — Yahoo sometimes omits
  it (verified for `FFOJX`, `FFOTX`, etc.).
- Names occasionally truncated to ~30 chars (Yahoo's convention).
- **Ordering is Yahoo's, not necessarily by AUM.** The script
  preserves the dict insertion order (Python 3.7+ guaranteed) which
  is whatever Yahoo's API serves; the underlying ranking criterion
  isn't documented (likely an internal popularity / fund-flow
  signal). Don't read position 0 as "biggest by AUM" — for true
  ranking, fetch `fund_holdings.py` per symbol and sort yourself.
- To get richer fund metadata (expense ratio, AUM, holdings), chain
  the symbol into `fund_holdings.py`.

### `top_performing_companies` _(industry-only)_

```json
[
  {"symbol": "MXL",  "name": "MaxLinear, Inc.",       "ytd_return": 4.7275, "last_price": 99.83,  "target_price": 51.6},
  {"symbol": "INTC", "name": "Intel Corporation",     "ytd_return": 2.3854, "last_price": 124.92, "target_price": 81.6476},
  ...
]
```

- `ytd_return` is a **multiple** in observed payloads (`4.7275` ≈
  +473% YTD for MXL — biotech / semis are common outliers). Don't
  read as fraction. Verify with a quick `history.py --period ytd
  --summary MXL` if it looks too large.
- `target_price` is the analyst consensus mean (same field as
  `info.analyst.target_mean_price`).
- ~5 rows.

### `top_growth_companies` _(industry-only)_

```json
[
  {"symbol": "MU",   "name": "Micron Technology, Inc.",                "ytd_return": 1.6166, "growth_estimate": 5.846153846153847},
  {"symbol": "SMTC", "name": "Semtech Corporation",                    "ytd_return": 0.6530, "growth_estimate": 3.857142857142857},
  ...
]
```

- `growth_estimate` is Yahoo's per-row growth metric powering the
  "Top Growth Companies" UI section — **horizon is unspecified** in
  Yahoo's API and likely **multi-year (probably 5-year) forward**
  given the magnitude consistently lands in the 3–6× range.
  Verified 2026-05 against `earnings.py --estimates` for the same
  tickers: `growth_estimate` does NOT match `+1y eps_growth`
  (verified for SMTC: sectors=3.857 vs earnings 0y=0.293, +1y=0.317
  — 10× larger; verified for SITM: sectors=3.833 vs earnings 0y=1.45,
  +1y=0.36) — so don't read it as a 1-year fraction. Surfaced as a
  raw float; **treat as a relative-rank signal within the industry,
  not as an arithmetic value**. For absolute EPS growth at known
  horizons, use `earnings.py --estimates`.
- `ytd_return` same multiple convention as `top_performing_companies`.
- ~5 rows.

### `research_reports`

```json
[
  {
    "id":                  "MS_0P000004SG_AnalystReport_1778284194000",
    "title":               "STMicroelectronics N.V. — A merger between...",
    "provider":            "Morningstar",
    "report_type":         "Analyst Report",
    "report_date":         "2026-05-08T23:49:54Z",
    "investment_rating":   "Bearish",
    "target_price":        46.0,
    "target_price_status": "Maintained"
  },
  ...
]
```

- ~4 reports per section. Mostly Morningstar.
- `target_price_status` ∈ {`Maintained`, `Raised`, `Lowered`,
  `Initiated`, `Discontinued`} (verified subset; may extend).
- `investment_rating` uses Morningstar's vocabulary (`Bullish` /
  `Bearish` / `Neutral`), NOT Yahoo's `recommendation_key`.

## `--summary` rollups

Flat per-key dict for cross-key compare. Identity + headline counts +
top-row pointers + description. Example for sectors:

```json
{
  "key": "technology",
  "kind": "sector",
  "name": "Technology",
  "symbol": "^YH311",
  "sector_key": null,
  "sector_name": null,
  "companies_count": 825,
  "industries_count": 12,
  "market_cap": 26665082159104,
  "market_weight": 0.31782493,
  "employee_count": 7964531,
  "description": "Companies engaged in the design, development, and support of computer operating systems and applications. ...",
  "top_company_symbol": "NVDA",
  "top_company_weight": 0.19670111,
  "top_companies_returned": 50,
  "top_industry_key": "semiconductors",
  "top_industry_weight": 0.4062864,
  "top_etf_symbol": "VGT",
  "top_mutual_fund_symbol": "VITAX"
}
```

Industry summary shape differs (no `top_industry_*` / `top_etf_*`
since Industry doesn't have those sections; gains
`top_performer_*` / `top_growth_*` instead):

```json
{
  "key": "semiconductors",
  "kind": "industry",
  "name": "Semiconductors",
  "symbol": "^YH31130020",
  "sector_key": "technology",
  "sector_name": "Technology",
  "companies_count": 59,
  "industries_count": null,
  "market_cap": 10833659691008,
  "market_weight": 0.4062864,
  "employee_count": 1299795,
  "top_company_symbol": "NVDA",
  "top_company_weight": 0.48118845,
  "top_companies_returned": 47,
  "top_performer_symbol": "MXL",
  "top_performer_ytd_return": 4.7275,
  "top_growth_symbol": "MU",
  "top_growth_estimate": 5.846153846153847
}
```

Mixed-kind `--summary` runs work but the JSON shape differs per row
— consumers iterating both should `.get(...)` rather than positional
indexing.

`--summary` auto-expands `--section` to all-applicable for the kind
so the rollup fields (`top_industry_*`, `top_etf_*`, `top_performer_*`,
etc.) are populated rather than null. **No extra HTTP cost for the
auto-expand** (1 HTTP per key regardless of section count — see Cost
in Mode-specific caveats). Pass an explicit `--section` only if you
want to drop sections from the JSON output to save context tokens —
network-wise it's the same.

## Discovery flags

`--list-sectors` — emit the 11 sector keys + child industry counts.
No HTTP, pure local lookup from `yfinance.const.SECTOR_INDUSTY_MAPPING_LC`.

```json
[
  {"key": "basic-materials",        "industry_count": 14},
  {"key": "communication-services", "industry_count": 7},
  ...
]
```

`--list-industries [SECTOR[,SECTOR...]]` — emit `(sector_key,
industry_key)` pairs. Optional positional arg filters to one or
more sectors (comma-separated).

```json
[
  {"sector_key": "technology", "industry_key": "computer-hardware"},
  {"sector_key": "technology", "industry_key": "semiconductors"},
  {"sector_key": "technology", "industry_key": "software-application"},
  ...
]
```

`--peers <industry>` — given an industry, emit its sibling
industries within the same parent sector (with an `is_self` flag
on the matching row). Cheaper than the chained `sectors.py
<industry> --section overview` → `sectors.py <parent_sector>
--section industries` two-step (which is 2 HTTP); this is 0 HTTP.
Trade-off: `is_self` is the only per-row signal; market_weight per
sibling is NOT included (chain to `--section industries` if you
need weights).

```json
[
  {"sector_key": "technology", "industry_key": "semiconductors",          "is_self": true},
  {"sector_key": "technology", "industry_key": "semiconductor-equipment-materials", "is_self": false},
  {"sector_key": "technology", "industry_key": "software-application",    "is_self": false},
  ...
]
```

All three flags ignore `--kind` / `--section` / `--summary` /
`--full` and short-circuit before any HTTP. They respect
`--format` so you can pipe the output into shell loops. They are
mutually exclusive — pass at most one per invocation.

## Presenting sector results

- **Render market weights as percent.** `market_weight: 0.318` →
  "31.8%". Same for `top_companies[*].market_weight`. Mention they're
  fractions in a footnote — easy mistake when sliding from
  `fast_info.change_pct` (percent) to here.
- **`top_performing_companies.ytd_return` is a multiple, not a
  fraction.** `4.7275` = +472.75% YTD. Prefix with `+` for clarity.
- **`growth_estimate` horizon is unspecified.** Don't say "+485%
  YoY" or any specific horizon in prose — Yahoo doesn't document
  whether it's 1y / 3y / 5y, and verification (see top_growth_companies
  section above) shows it does NOT match `earnings.py --estimates
  +1y eps_growth`. Render as "Yahoo growth estimate: 5.85" or "ranks
  #1 by growth_estimate within {industry}" — relative-rank framing
  rather than a confident horizon-bound claim.
- **Drill-down chain.** Sector → top industries → industry → top
  companies → per-ticker `info` / `financials`. Each `key` value is
  designed to be re-used as the next call's positional arg.
- **Don't quote the description verbatim** — it's a fixed Yahoo
  marketing blurb, often dated. Summarize.
- **Top 5 / top 10 is usually enough.** Sector top_companies returns
  ~50 rows; for prose use `--limit 5` or 10 to keep the answer tight.

## Mode-specific caveats

- **Em-dash quirk in yfinance.const.** A handful of industry keys in
  `yfinance.const.SECTOR_INDUSTY_MAPPING_LC` carry a unicode em-dash
  (U+2014, e.g. `software—application`), but Yahoo's actual industry
  endpoint requires a regular hyphen (`software-application`).
  Verified 2026-05: `Industry('software—application').overview` →
  `None`; `Industry('software-application').overview` → real data.
  This script normalizes em-dash → hyphen on both ingest (const →
  API form) and on user input (paste-tolerant), so the
  user-visible keys are always the API form. If you bypass this
  script and call `yf.Industry(...)` directly, run keys through
  `str.replace("—", "-")` first.

- **Auto-detect ambiguity.** Sector keys are 11 fixed values; if a
  key isn't in that set, `--kind auto` infers `industry`. There's no
  ambiguous overlap between the two sets, but a typo on a sector key
  (`tech` instead of `technology`) will be silently routed to
  Industry and 404. Guard yourself by validating against
  `--list-sectors` first if a key looks suspicious. The argparse path
  catches typos that match neither set with a clean error pointing
  at the discovery flags.

- **Auto-detect tracks `yfinance.const`, not Yahoo live.** The 11
  sector / ~150 industry key sets are lifted from
  `yfinance.const.SECTOR_INDUSTY_MAPPING_LC` at import time. If
  Yahoo introduces a new sector or rename and the yfinance package
  hasn't shipped a const-table update yet, our auto-detect will
  silently misroute the new key (probably to `industry` since it
  won't be in the 11-sector set) and 404. Defensive moves: pin
  `yfinance>=1.3,<2` (we already do via the `uv run` invocation) and
  re-run `--list-sectors` after upgrading yfinance to spot a count
  drift. Pass `--kind sector` or `--kind industry` explicitly to
  bypass the inference if you know the kind out-of-band.

- **Cost is 1 HTTP per key, regardless of `--section` count.**
  Verified 2026-05 (counting `yfinance.data.YfData.get` calls):
  `yf.Sector(key)` and `yf.Industry(key)` each hit ONE Yahoo
  endpoint (`/v1/finance/sectors/<key>` or
  `/v1/finance/industries/<key>`) and cache the full response on
  the instance — every section property (`overview`, `top_companies`,
  `industries`, `top_etfs`, `top_mutual_funds`,
  `top_performing_companies`, `top_growth_companies`,
  `research_reports`) reads from that one cached payload. So:

  | invocation                                  | HTTP per key |
  |---------------------------------------------|--------------|
  | `--section overview`                        | 1            |
  | `--section overview,top_companies` (default) | 1            |
  | `--section all`                             | 1            |
  | `--summary` (auto-expands to all-applicable) | 1            |

  `--section` only changes **projection cost** (DataFrame parsing /
  dict projection per requested section, all CPU-only after the
  first access). It does NOT change network cost. **Implication:
  `--section all` is essentially free vs `--section overview`** for
  network — opt for fewer sections only if the JSON output size
  matters or DataFrame parsing latency matters.

  Cross-key fan-out IS serial network — N keys = N HTTP. For 11
  sectors (the full `--list-sectors` set) `--summary` budget is
  ~11 HTTP / ~6–20 s.

  The script prints a one-line `info: sectors plan = ...` cost
  preview to stderr when N ≥ 8 keys, so callers see what they're
  paying for before it commits. Pipe stderr to `/dev/null` if you
  want a clean console; the projection is advisory, not a hard cap.

  **Cache scope:** per-instance, NOT cross-call. Re-running
  `sectors.py technology` twice = 2 HTTP. There's no on-disk cache
  between invocations.

- **`overview` always fetched.** Even when `--section` doesn't
  include `overview`, the script fetches it for key validation
  (Yahoo returns None for unknown keys; without the probe we'd fire
  N 404-bound requests). The result is reused — no double HTTP.
  Setting `--section top_companies` alone is still 2 HTTP per key
  (overview validation + top_companies); only the JSON payload
  drops the overview block.

- **Per-section error isolation.** A 404 / network failure on one
  section doesn't fail the envelope — the failed section is set to
  `null` and a per-section message is added to `section_errors`.
  Other sections proceed. Per-section retries follow the standard
  `with_retry` policy (3 attempts, exponential backoff on
  `rate_limit` / `network`).

- **`coverage_note` for cross-kind sections.** If you ask for
  `--section industries` on an industry key (or
  `--section top_performing_companies` on a sector), the script
  doesn't attempt the HTTP call (the property doesn't exist on the
  wrong class) and emits a `coverage_note` listing the skipped
  sections. Same shape contract as `analyst.coverage_note` — present
  alongside successful data, mutually exclusive with `error`.

- **`top_companies` is a curated subset, not a screen.** Yahoo
  picks the top ~50 by market weight; smaller companies in the
  sector / industry are absent. For a screened list (custom filters
  on size / valuation / momentum), use `screener.py` instead. For
  the full company list of an industry, you'd need a third-party
  data source — yfinance doesn't expose one.

- **`market_cap` aggregation has no currency tag.** Yahoo returns
  the total market cap in what appears to be USD across global
  sectors / industries (international names are converted), but the
  payload has no explicit currency field. Treat as USD-equivalent
  for cross-sector compare.

- **No date / time field on overview.** The market_cap / market_weight
  / employee_count are point-in-time snapshots Yahoo refreshes
  asynchronously; there's no `as_of` field. Treat as "recent" with
  no precise vintage.

- **Stale `top_*_companies` against intraday moves.** Yahoo's
  hierarchy data appears to refresh end-of-day, so a top performer
  in `top_performing_companies` may not reflect today's move.
  Cross-reference with `history.py --period ytd --summary <symbol>`
  if you need confirmed intraday alignment.

- **Research reports are sparse.** Only ~4 per section, mostly
  Morningstar coverage of US large-caps. ETFs / non-US listings
  rarely have reports here. For ticker-level coverage see
  `analyst.py` (which has the per-ticker upgrades_downgrades feed).

- **Research reports overlap between sector and industry.** A
  Morningstar report on `MU` (Micron) appears in both
  `Sector('technology').research_reports` and
  `Industry('semiconductors').research_reports` — the same
  underlying analyst note tagged at multiple hierarchy levels.
  We don't dedupe across sections; if you fetch both, expect
  to see the same `id` twice and dedupe consumer-side on the
  `id` field.

- **CSV emits 32 columns.** That's the union of all per-record-class
  schemas (meta + 7 record classes × ~3-5 fields each). Most rows
  have many empty cells. **If you want narrower output**, prefer
  `--format ndjson` (each line has only the populated keys) or
  filter columns post-hoc with `awk` / `pandas`. The wide-union
  shape is intentional — it matches `calendars --type all` so a
  cross-mode pipeline of CSVs concatenates cleanly with stable
  column positions.
