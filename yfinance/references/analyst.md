[← back to SKILL.md](../SKILL.md)

# `analyst` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`analyst.py --summary AAPL NVDA TM 0700.HK QQQ ZZZZNOTREAL` if you
suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting analyst results](#presenting-analyst-results) · [Mode-specific caveats](#mode-specific-caveats)

Analyst data for one or more tickers — two sections per ticker:

- **`recommendations`** (from `Ticker.recommendations`) — short time
  series (3-4 rows: `0m` / `-1m` / `-2m` / `-3m`) of analyst rating
  distribution. Each row carries the count in each bucket
  (`strong_buy` / `buy` / `hold` / `sell` / `strong_sell`) plus a
  derived `total`. `Ticker.recommendations_summary` is a verified
  ALIAS (same DataFrame); we only call `recommendations`.
- **`upgrades_downgrades`** (from `Ticker.upgrades_downgrades`) — full
  per-event grade-change feed going back to ~2012 for major US
  large-caps (AAPL: 977 events, NVDA: 981 in 2026-05). Each row
  carries the firm, from/to grade, an `action` enum (up / down / main
  / init / reit), and **embedded price-target moves** — `priceTargetAction`
  (Raises / Lowers / Maintains / Announces / Adjusts) plus
  `currentPriceTarget` and `priorPriceTarget`. Roughly 75% of rows
  are `main` or `reit` (rating reaffirmations) where the only news
  is a target tweak.

**Complementary to `info` and `earnings --estimates`.** `info`'s
`analyst` section has the *static current* consensus
(`target_mean_price`, `recommendation_key`, `num_analyst_opinions`).
`earnings --estimates` has the underlying EPS / revenue forecasts
that drive those targets. THIS mode adds:
- the **time series** of how the rating distribution has shifted over
  the last ~3 months (`recommendations` 0m vs -3m), and
- the **per-event log** of who changed their mind when, including
  target-only moves that don't show up as rating changes
  (`upgrades_downgrades`).

**Equity-focused.** ETFs / mutual funds / indexes / crypto / FX /
futures all return both frames empty — see
[All-empty is ambiguous](#empty-ambiguous). Non-US primary listings
(`0700.HK`, `BMW.DE`) get `recommendations` populated but
`upgrades_downgrades` empty (Yahoo's grade-change feed is US-centric;
ADRs like `TM` still get full coverage) — surfaced via `coverage_note`,
see [coverage_note semantics](#coverage-note).

## Run

```bash
# Default: full sections, pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py AAPL

# Peer rollup: flat per-ticker dict (~10× smaller than default)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --summary AAPL MSFT GOOGL

# Cap upgrades_downgrades to top 20 events (recommendations unaffected)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --limit 20 AAPL

# CSV — default mode emits one row per record (with record_class discriminator)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --format csv --limit 5 AAPL

# CSV — summary mode emits strict one row per ticker (peer-comparison table)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/analyst.py --format csv --summary AAPL MSFT NVDA
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap `upgrades_downgrades` rows per ticker. Default:
  keep all (Yahoo returns the full history; **often 900+ rows** for
  major US large-caps going back to ~2012). Does not affect the
  `recommendations` time series (Yahoo already caps at 3-4 rows).
  **Silently ignored in `--summary` mode.** The flat metrics
  (`rating_changes_returned`, `upgrades_last_90d`, etc.) are computed
  from Yahoo's full response so they describe the data, not the
  display knob — and stay invariant under `--limit` by design. Same
  contract as `insiders.py`'s `--limit`.
- `--summary` — flat per-ticker projection. Lifts the current/oldest
  recommendations snapshot, the buy-pct change over the available
  window, and 90-day rating-change rollups (upgrades / downgrades /
  target raises / lowers) + the latest event. Same network cost as
  default mode (post-fetch projection); use it to save context tokens.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array, one record per ticker. `ndjson` emits one JSON
  record per ticker per line. `csv` shape depends on mode:
  - **default mode** — one row per record, with a `record_class`
    discriminator column whose values are `recommendation` (3-4
    rows per ticker, one per period) / `change` (one row per
    grade-change event). Section-specific columns are populated only
    on rows of that class. `symbol` repeats. Empty / errored tickers
    emit a single row carrying `symbol` + `note` + meta. Partial-empty
    tickers (`0700.HK`, `BMW.DE`) emit their `recommendation` rows
    normally AND carry the `coverage_note` string in the
    `coverage_note` column on every row of that ticker. `note` and
    `coverage_note` are mutually exclusive at the result level.
  - **`--summary` mode** — strict one row per ticker. Every column
    populated where the ticker has data; partial-empty tickers have
    populated `*_current` / `*_oldest` columns and null `*_last_90d`
    columns plus the `coverage_note` string.

CSV column order (default mode, left to right): `symbol`,
`quote_type`, `record_class`, the 7 recommendation columns, the 8
change columns, `note`, `coverage_note`, then the 3 meta fields
(`error`, `error_kind`, `attempts`). `quote_type` is a top-level
ticker attribute that repeats across a ticker's rows (same shape as
`note` / `coverage_note`).

## Output schema

### Default mode

Per ticker (illustrative; real AAPL fetch in 2026-05):

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "recommendations": [
      {"period": "0m",  "strong_buy": 7, "buy": 24, "hold": 15, "sell": 1, "strong_sell": 1, "total": 48},
      {"period": "-1m", "strong_buy": 7, "buy": 25, "hold": 14, "sell": 1, "strong_sell": 1, "total": 48},
      {"period": "-2m", "strong_buy": 6, "buy": 25, "hold": 15, "sell": 1, "strong_sell": 1, "total": 48},
      {"period": "-3m", "strong_buy": 5, "buy": 25, "hold": 16, "sell": 1, "strong_sell": 1, "total": 48}
    ],
    "upgrades_downgrades": [
      {
        "date": "2026-05-08T14:02:49",
        "firm": "Wedbush",
        "to_grade": "Outperform",
        "from_grade": "Outperform",
        "action": "main",
        "price_target_action": "Raises",
        "current_price_target": 400.0,
        "prior_price_target": 350.0
      }
    ]
  }
]
```

**`quote_type`** (top-level field, always present on success — i.e.
on the data, `coverage_note`, and `note` paths; **absent only on
hard fetch failure**, where `error_kind` is set instead): the Yahoo
`quoteType` enum from `fast_info` — `"EQUITY"`, `"ETF"`,
`"MUTUALFUND"`, `"INDEX"`, `"CRYPTOCURRENCY"`, `"CURRENCY"`,
`"FUTURE"`, or `null` when `fast_info` itself crashes (typical for
bogus / delisted tickers — yfinance's `fast_info` raises an internal
`AttributeError` we catch and project to `null`). This field
disambiguates the all-empty `note` path inline so callers don't
need a follow-up `fast_info` call: `quote_type: "ETF"` on an empty
result confirms the `note` cause; `quote_type: null` on an empty
result is the bogus-ticker signal.

#### `recommendations` row (7 fields)

3-4 row time series. Yahoo emits one row per period offset; observed
periods are `0m`, `-1m`, `-2m`, `-3m` (most-recent first in current
responses, but don't rely on order — `_summarize` uses period parsing).
Tickers with shorter analyst-tracking history may return only 3 rows
(verified: `TM` returned 3 rows in 2026-05 with `oldest_period: "-2m"`).

| Field | Type | Notes |
|---|---|---|
| `period` | str / null | Yahoo's period label: `"0m"` (current month), `"-1m"`, `"-2m"`, `"-3m"`. The `m` is "months ago"; `0m` is today. |
| `strong_buy` | int / null | Number of analysts with a strong-buy rating in that period. |
| `buy` | int / null | Buy / overweight ratings. |
| `hold` | int / null | Hold / neutral. |
| `sell` | int / null | Sell / underweight. |
| `strong_sell` | int / null | Strong sell. |
| `total` | int / null | Sum of the five buckets. Derived (not from Yahoo) so consumers don't need to recompute it. **Strict null**: if ANY of the five buckets is null, `total` is null too — partial sums would look like real totals while excluding unknown buckets. In practice all buckets are populated together (verified across AAPL / NVDA / TM / 0700.HK / BMW.DE in 2026-05); the strict rule only bites pathological responses. |

#### `upgrades_downgrades` row (8 fields)

Per-event grade change. Yahoo emits desc-by-date (most recent first)
in current responses but we don't depend on that — `_summarize`
recomputes the latest event via `max()`.

| Field | Type | Notes |
|---|---|---|
| `date` | str / null | ISO `'YYYY-MM-DDTHH:MM:SS'`. Yahoo's `GradeDate` index, naive datetime (no timezone info — source tz is undocumented; we don't tack on a fake `Z`/+00:00). |
| `firm` | str / null | Brokerage / firm name (`"Wedbush"`, `"Morgan Stanley"`, `"Goldman Sachs"`, ...). |
| `to_grade` | str / null | Grade after the change (`"Outperform"`, `"Buy"`, `"Hold"`, ...). Pass-through string — text varies by firm. |
| `from_grade` | str / null | Grade before. For `action: init` (initiated coverage), `from_grade` is typically empty / null. For `action: main` (maintained), `from_grade == to_grade`. |
| `action` | str / null | **LOWERCASE** enum (verified across AAPL's 977 rows in 2026-05): `"up"` (upgrade), `"down"` (downgrade), `"main"` (rating maintained — most common, ~74%), `"reit"` (reiterated, ~16%), `"init"` (initiated coverage, ~2%). Yahoo's encoding; passed through as-is. |
| `price_target_action` | str / null | **CAPITALIZED** enum (Yahoo's case inconsistency, not ours): `"Raises"` (~50%), `"Lowers"` (~18%), `"Maintains"` (~22%), `"Announces"` (~7%, broker initiated coverage and announced a starting target — typically pairs with `action: init` since you can only "announce" a target on first coverage), `"Adjusts"` (rare; 1/977 for AAPL — Yahoo data anomaly with unclear semantics, treat as "target was modified but Yahoo couldn't categorize as raise / lower / maintain"), or `null` (empty string in Yahoo's payload for old pre-2018 rows where the target-action column wasn't populated). |
| `current_price_target` | float / null | Target after the change, in the ticker's **TRADING currency** (USD for AAPL, also USD for ADRs like TM). **0.0 → null**: Yahoo encodes "no target published" as 0.0 (verified empirically: AAPL's 24 `init` rows + many old pre-2018 rows). A genuine $0 target on an analyst-covered equity is implausible. |
| `prior_price_target` | float / null | Target before. Same 0.0 → null sentinel. |

### `--summary` mode

Flat per-ticker dict (recommendations snapshot + 90-day rollups + latest event):

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "total_analysts_current": 48,
    "total_analysts_oldest": 48,
    "oldest_period": "-3m",
    "buy_pct_current": 0.6458333333333334,
    "buy_pct_oldest": 0.625,
    "buy_pct_change": 0.02083333333333337,
    "consensus_score_current": 2.2708333333333335,
    "consensus_score_oldest": 2.3333333333333335,
    "consensus_score_change": -0.0625,
    "rating_changes_returned": 977,
    "upgrades_last_90d": 1,
    "downgrades_last_90d": 0,
    "net_rating_changes_90d": 1,
    "target_raises_last_90d": 11,
    "target_lowers_last_90d": 1,
    "latest_event_date": "2026-05-08",
    "latest_event_firm": "Wedbush",
    "latest_event_action": "main",
    "latest_event_to_grade": "Outperform",
    "latest_event_current_price_target": 400.0,
    "latest_rating_change_date": "2026-04-17",
    "latest_rating_change_firm": "BNP Paribas",
    "latest_rating_change_action": "up",
    "latest_rating_change_from_grade": "Neutral",
    "latest_rating_change_to_grade": "Outperform",
    "latest_rating_change_current_price_target": 300.0
  }
]
```

The 28 flat fields (`symbol` + `quote_type` + 26 metrics):

**Recommendations snapshot (current vs oldest available row):**
- `total_analysts_current` — sum of buckets at `period: "0m"`. The current analyst pool size.
- `total_analysts_oldest` — sum at the most-negative period Yahoo gave us (typically `-3m`, sometimes `-2m`). **Pool size can drift between periods** — analyst churn is normal — so don't read `current - oldest` as "lost coverage" without checking the change magnitude.
- `oldest_period` — the period label Yahoo's oldest row carries. Echoes the source so consumers can tell the comparison window (`"-3m"` vs `"-2m"`).
- `buy_pct_current` — `(strong_buy + buy) / total` at 0m. **FRACTION** (`0.65` = 65%, NOT percent-encoded). Matches the rest of the skill's fraction-encoded percentages (`info` margins, `holders.pct_held`).
- `buy_pct_oldest` — same metric at `oldest_period`.
- `buy_pct_change` — `buy_pct_current - buy_pct_oldest`. Positive = consensus shifted bullish over the window. Magnitude tiny (single-digit percentage points) by design — Yahoo's window is short.
- `consensus_score_current` — weighted mean rating at 0m: `(1×strong_buy + 2×buy + 3×hold + 4×sell + 5×strong_sell) / total`. **1.0 = unanimous strong_buy, 5.0 = unanimous strong_sell**. Lower = more bullish. Approximates `info.analyst.recommendation_mean` from a separate endpoint — comparable encoding, so consumers can swap data sources without converting.
- `consensus_score_oldest` — same at oldest_period.
- `consensus_score_change` — `consensus_score_current - consensus_score_oldest`. **SIGN IS OPPOSITE of `buy_pct_change`**: because the score is on a 1-5 scale where 1 is most bullish, a NEGATIVE delta means consensus moved more bullish over the window (`current` is a lower number than `oldest`). Magnitude tiny by design (Yahoo's window is short). Null when either snapshot is null.

**Rating-change rollups (last 90 days from `now()`):**
- `rating_changes_returned` — total `upgrades_downgrades` rows Yahoo returned (FULL pre-limit count). Independent of `--limit`. **Null when the upgrades_downgrades list is empty**, which signals the partial-empty / all-empty path; with at least one row this is always populated.
- `upgrades_last_90d` — count of rows where `action == "up"` AND `date >= today - 90d`. Counts *rating* upgrades only — does NOT count `init` (new coverage) or `reit` (reiteration). Null when the upgrades list is empty (see below).
- `downgrades_last_90d` — count of `action == "down"` rows in the same window.
- `net_rating_changes_90d` — `upgrades_last_90d - downgrades_last_90d`. Single-number directional signal; positive = net bullish moves. Null when rollups are null.
- `target_raises_last_90d` — count of rows where `price_target_action == "Raises"` (regardless of `action`). Captures "rating maintained but target raised" cases that `upgrades_last_90d` misses. Often the LARGER of the two signals — for AAPL in 2026-05, target raises (11) outnumbered rating upgrades (1) by 11×.
- `target_lowers_last_90d` — count of `"Lowers"` rows.

**Latest EVENT (any action type — most recent regardless):**
- `latest_event_date` — date of the most recent `upgrades_downgrades` row (recomputed via `max()`, not relying on Yahoo's sort). YYYY-MM-DD.
- `latest_event_firm` — firm of the latest event.
- `latest_event_action` — its action (`"up"` / `"down"` / `"main"` / `"reit"` / `"init"`). **Note: most recent EVENT, not most recent rating CHANGE** — `"main"` / `"reit"` are common values here (~90% of events on US large-caps are target tweaks rather than rating moves). Use the `latest_rating_change_*` fields below for the narrow "when did anyone last move their rating" question.
- `latest_event_to_grade` — its `to_grade`.
- `latest_event_current_price_target` — its `current_price_target` (already 0→null filtered). The freshest published target. (Renamed from earlier `latest_current_price_target` for naming consistency with the rest of this group.)

**Latest RATING CHANGE (filtered to `action ∈ {up, down}`):**
- `latest_rating_change_date` — date of the most recent row with `action == "up"` or `action == "down"`. YYYY-MM-DD. **Null when no up/down events exist** (a stock with consensus-only target adjustments and no historical rating moves — uncommon but possible). Recomputed via `max()` over the filtered subset.
- `latest_rating_change_firm` — firm that did the change.
- `latest_rating_change_action` — `"up"` or `"down"` (never `"main"` / `"reit"` / `"init"`).
- `latest_rating_change_from_grade` — grade before the change (e.g., `"Neutral"` for an up). Useful context that the event-level field provides but is more interesting on the rating-change subset.
- `latest_rating_change_to_grade` — grade after.
- `latest_rating_change_current_price_target` — the price target the firm set when they actually changed their rating. Symmetric with `latest_event_current_price_target` (which captures the latest event's target regardless of action). Same trading-currency / 0→null filtering rules. Useful for "when X upgraded, what target did they set" questions.

The two latest-* groups can return the SAME event (when the most
recent event happens to be an up/down) or DIFFERENT events (when
the most recent event is a `main`/`reit`/`init`). In the 2026-05
verification snapshot AAPL's groups differed: latest event was
Wedbush (`main`, target raise from \$350 → \$400), latest rating
change was BNP Paribas a few weeks earlier (`up`, Neutral →
Outperform). At any moment, whichever happened most recently
populates each group independently.

**Empty-list rationale.** When `upgrades_downgrades` is empty
(partial-empty for non-US primary listings, OR all-empty for
non-equity / bogus), all the count fields and BOTH latest-*
groups (`latest_event_*` AND `latest_rating_change_*`) are null —
NOT 0. Reporting `upgrades_last_90d: 0` for `0700.HK` would be a
confidently-wrong claim (Yahoo doesn't index the events; there
could be plenty of upgrades the user can't see). With at least
one historical event, 0 is real ("no events in the window"). Same
defensive null vs 0 distinction as `options`'s `total_*_volume`
fields.

### Empty / non-applicable result

When both sections come back empty, the response is success with a
`note` rather than an error. See
[All-empty is ambiguous](#empty-ambiguous).

```json
{
  "symbol": "QQQ",
  "quote_type": "ETF",
  "recommendations": [],
  "upgrades_downgrades": [],
  "note": "no analyst data (Yahoo's analyst endpoints cover equities; ETFs / indexes / crypto / FX / futures and bogus tickers all return empty frames — call fast_info to disambiguate)"
}
```

The `quote_type` field disambiguates the cause of the `note` inline
— here `"ETF"` confirms the empty result is the non-equity short-
circuit. For bogus / delisted tickers `quote_type` is `null`
(yfinance's `fast_info` itself crashes on bogus, projected to null).
The `note` text still suggests `fast_info` for users who want a
fuller quote payload, but the basic disambiguation answer is now
inline.

**Partial empty — see [coverage_note semantics](#coverage-note)
below.** Non-US primary listings (verified for `0700.HK` and `BMW.DE`
in 2026-05) return `recommendations` populated but
`upgrades_downgrades` empty. The script surfaces this via
`coverage_note` rather than `note`:

```json
{
  "symbol": "0700.HK",
  "recommendations": [
    {"period": "0m", "strong_buy": 8, "buy": 35, "hold": 2, "sell": 1, "strong_sell": 0, "total": 46}
  ],
  "upgrades_downgrades": [],
  "coverage_note": "recommendations populated but upgrades_downgrades empty — typical for non-US primary listings (Yahoo's grade-change feed is US-centric; ADRs like TM still get full coverage). The empty event list is asymmetric Yahoo coverage, not a fetch failure"
}
```

In CSV (default mode), `0700.HK` emits one `recommendation` row per
period (4 rows total) carrying the `coverage_note` string in the
`coverage_note` column on every row, and zero `change` rows.

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

## Presenting analyst results

**Multiply fractions ×100 when displaying.** `buy_pct_*` are
fractions. Render `0.65` as `65%`, not `0.65%`. Same convention as
`info` margins / `holders.pct_held` / `insiders.pct_*`.

**`consensus_score` is on Yahoo's 1-5 scale** (1 = strong_buy, 5 =
strong_sell). Lower is more bullish. Don't accidentally render this
as a percentage — it's a Likert-style mean. A score of 2.27 on AAPL
maps to "Buy/Outperform-ish" in Yahoo's `recommendation_key` taxonomy.

**Single-ticker rollup.** Lead with the current consensus distribution,
then the 3-month drift, then the most recent ~5 events. **Yahoo's
`upgrades_downgrades` is sorted desc by date in current responses,
so `[:5]` gives the most recent five — but `_summarize` recomputes
`latest_event_*` and `latest_rating_change_*` via `max()` because Yahoo's sort isn't a contract.

Format template (placeholders — illustrative shape):

> **\<TICKER\> — analyst consensus (as of today)**
> - **\<total_analysts_current\>** analysts: \<strong_buy\> strong buy,
>   \<buy\> buy, \<hold\> hold, \<sell\> sell, \<strong_sell\> strong sell.
> - **\<buy_pct_current ×100\>%** buy-or-better (vs \<buy_pct_oldest ×100\>%
>   at \<oldest_period\> — \<delta\>pp shift).
> - **Mean rating: \<consensus_score_current\>** on the 1-5 scale (1 = strong buy).
>
> **Recent activity (last 90 days)**
> - \<upgrades_last_90d\> upgrades / \<downgrades_last_90d\> downgrades.
> - \<target_raises_last_90d\> target raises / \<target_lowers_last_90d\> target lowers.
>
> **Latest events** (most recent first):
> - \<YYYY-MM-DD\> — \<firm\>: \<action\> (\<from_grade\> → \<to_grade\>),
>   target \<prior\> → \<current\>
> - ...

State the currency on price targets. For US tickers `\$` is fine; for
ADRs (`TM`) the targets are USD too. For `0700.HK` you won't see
targets (partial-empty path).

**Multi-ticker peer compare.** Use `--summary`; render as a table
with columns: ticker | analysts | mean rating | buy% | 90d upgrades |
90d downgrades | latest event. The `consensus_score_current` column
is the headline ranking field — sort ascending (lower = more bullish).

**Date formatting.** `latest_event_date` / `latest_rating_change_date` are YYYY-MM-DD; per-event
`date` is the full `YYYY-MM-DDTHH:MM:SS` ISO string. Strip to date
unless the user asked for intraday-resolution timing.

**Escape `$` as `\$` in prose** — same rationale as the other modes.

## Mode-specific caveats

- <a id="empty-ambiguous"></a>**All-empty is ambiguous (full discussion).**
  yfinance returns both DataFrames empty in all of these cases.
  Verified empirically (2026-05):
  - ETF (`QQQ`)
  - Index (`^GSPC`)
  - Crypto (`BTC-USD`)
  - Bogus / delisted ticker (`ZZZZNOTREAL`)

  yfinance prints `HTTP Error 404` to stderr for these but does not
  raise. We can't distinguish them at the analyst endpoints — same
  signal-free empty payload as `holders` / `insiders` / `options`.
  We don't promote empty to `error_kind: not_found` (a non-equity
  isn't an error) and instead emit success with a `note` carrying
  the disambiguation hint. To resolve which case you're in, **call
  `fast_info`** on the same symbol — it returns `quote_type` for
  valid tickers and `not_found` for bogus.

- <a id="coverage-note"></a>**`coverage_note` semantics
  (full discussion).** Two ambiguity-handling fields, mutually
  exclusive at the result level:
  - `note` — fires when **both frames are empty** and the cause is
    ambiguous (non-equity / bogus). Caller should chain `fast_info`
    to disambiguate. See [All-empty is ambiguous](#empty-ambiguous).
  - `coverage_note` — fires when **`recommendations` IS populated**
    but `upgrades_downgrades` is empty. Verified (2026-05) for
    `0700.HK` (Tencent, HKEX primary) and `BMW.DE` (BMW, Xetra
    primary). Yahoo aggregates analyst rating distributions
    globally but its grade-change feed appears US-centric — ADRs
    that have a US listing (`TM` — Toyota ADR on NYSE) DO get full
    upgrades_downgrades coverage. The empty event list IS the
    answer for non-US primaries; Yahoo simply doesn't have
    grade-change events for these tickers.

  Three result classes you may see, in increasing data
  completeness:
  1. All-empty → `note` set, `coverage_note` absent.
  2. Partial-empty (recommendations-only) → `coverage_note` set,
     `note` absent.
  3. Full or near-full data → neither field present.

  Both fields serialize as columns in default-mode and `--summary`-
  mode CSVs, so neither category drops out of tabular output.

- **`recommendations` and `recommendations_summary` are aliases.**
  Verified (2026-05) on AAPL, NVDA, TM, 0700.HK, BMW.DE: both
  properties return the identical 4-row × 6-col DataFrame. We only
  call `recommendations` to avoid a redundant module fetch. If
  Yahoo ever desynchronizes them (e.g., adds extra fields to one),
  the smoke canary on field set will fire.

- **Action enum is LOWERCASE; priceTargetAction is CAPITALIZED.**
  Yahoo's inconsistency: `action ∈ {up, down, main, init, reit}`,
  `price_target_action ∈ {Raises, Lowers, Maintains, Announces,
  Adjusts}` (plus null for old rows). We pass case through
  unchanged — easier for consumers to filter than guessing case
  conventions. If you compare these enums in code, the case must
  match exactly (`action == "Up"` will silently never match).

- **`action: main` and `action: reit` are NOT rating changes.**
  ~74% of rows for major US large-caps are `main` (maintained) and
  ~16% are `reit` (reiterated) — these are firms reaffirming an
  existing rating, often with only a price-target tweak as the news.
  `upgrades_last_90d` / `downgrades_last_90d` count `up` / `down`
  only. If you want "any analyst activity in the last 90d", count
  all rows in the window. If you want "real rating changes", stick
  with up + down.

- **Price targets are in TRADING currency (= `fast_info.currency`).**
  USD for AAPL and for ADRs (`TM` returns USD targets even though
  Toyota reports in JPY — see `financials.currency` for the
  reporting-currency split). For `0700.HK` / `BMW.DE` you won't see
  targets at all (partial-empty path), so the currency question is
  moot for those.

- **0.0 in price-target fields is Yahoo's "no target" sentinel —
  projected to null.** Verified empirically: AAPL's 24 `init` rows
  all have `currentPriceTarget: 0` and `priorPriceTarget: 0`; many
  pre-2018 rows also use 0 in both fields. A genuine $0 target on
  an analyst-covered equity is implausible. Project to null to
  dodge the "100% increase from 0" arithmetic trap. If a future
  penny-stock case turns up where 0 is a real target, this rule
  would need to be revisited.

- **`recommendations` time series is short and Yahoo-controlled.**
  Yahoo only exposes 4 monthly snapshots (`0m` to `-3m`). To track
  consensus drift over a longer window (a year, multiple quarters),
  you'd need an external archive. The `buy_pct_change` field in
  `--summary` is computed over the available window — typically
  3 months, sometimes 2 (verified `TM` in 2026-05).

- **Pool size drift between periods is normal.** AAPL had 48
  analysts in all four periods (2026-05); NVDA had 60 at 0m and 63
  at -3m (3 dropped); 0700.HK went from 51 at -3m to 46 at 0m (5
  dropped). Don't read pool drift as "lost coverage" without
  context — analysts rotate covers, sabbatical, leave the firm,
  etc. The rating distribution shift (`buy_pct_change`) is more
  meaningful than absolute pool size.

- **`GradeDate` index has no timezone info.** Yahoo's source tz is
  undocumented; we emit `'YYYY-MM-DDTHH:MM:SS'` without `Z` or an
  offset rather than assuming. If you need to compare these dates
  to anything tz-aware, treat the value as opaque-to-tz (or assume
  Eastern Time as a best guess for US-listed grade events).

- **`latest_event_*` reflects the most recent EVENT, not the most
  recent rating CHANGE.** ~90% of `upgrades_downgrades` rows are
  `main` / `reit` (target tweaks without rating changes), so the
  latest event is usually a target raise/lower, not an
  upgrade/downgrade. The `latest_rating_change_*` companion fields
  (filtered to `action ∈ {up, down}`) answer the narrow "when did
  anyone last move their rating" question and can return a much
  older date — in the 2026-05 verification snapshot AAPL's latest
  event was a Wedbush target raise (`main`) while the latest
  rating change was a BNP Paribas upgrade weeks earlier. When
  both groups are populated they MAY refer to the same event
  (the most recent event was itself an up/down) or different
  events.

- **`info.analyst.recommendation_mean` and our `consensus_score_current`
  approximate each other.** Yahoo computes `recommendation_mean`
  from a different (broader?) source — values are typically within
  ±0.1 but exact equality is not guaranteed. Cross-checking is
  fine for sanity; treating them as equal is not.

- **`upgrades_downgrades` history can be long.** AAPL: 977 rows
  going back to 2012; NVDA: 981. For multi-ticker peer compares
  default-mode JSON can balloon — use `--limit` to slice or
  `--summary` if you only need the rollups. `--summary` is the
  recommended path for any peer compare > 3 tickers.

- **Three HTTP requests per ticker.** `recommendations`,
  `upgrades_downgrades`, and `fast_info` (for `quote_type`) come
  from different Yahoo endpoints / module groups so they don't
  share a backend request (unlike `holders` / `insiders` which
  fold three properties into one HTTP). Latency is roughly 3× a
  single property fetch — empirically ~0.5-1s warm, ~2-3s cold —
  so a 10-ticker batch can take 10-30 seconds. The `fast_info`
  call adds ~0.15-0.3s but is the disambiguator that lets the
  `note` path be answered inline; without it callers would need
  to chain `fast_info` themselves on every ambiguous result. If
  you're rate-limited and don't need `quote_type`, batch in
  chunks of ~5 tickers and pause between batches.

- **Rating words vary by firm.** `to_grade` is whatever Yahoo
  ingested from the source feed — `"Outperform"`, `"Overweight"`,
  `"Buy"`, `"Strong Buy"`, `"Market Perform"`, `"Equal-Weight"`,
  `"Hold"`, `"Neutral"`, etc. There's no canonicalization. The
  `recommendations` distribution buckets DO canonicalize (everything
  collapses to strong_buy / buy / hold / sell / strong_sell), so
  if you want comparable ratings across firms, read the
  distribution counts rather than per-event grade strings.
