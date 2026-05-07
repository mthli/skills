[← back to SKILL.md](../SKILL.md)

# `financials` reference

_Yahoo encodings & line item names verified: 2026-05, yfinance 1.3.x.
Yahoo's line item taxonomy can shift — re-run `scripts/smoke.py` if a
field starts coming back null across tickers that previously populated._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output — default mode](#output--default-mode-per-statement-period-lists) · [Output — `--summary` mode](#output----summary-mode) · [When to use `--summary`](#when-to-use---summary) · [Schema — line items per statement](#schema--line-items-per-statement) · [Period semantics](#period-semantics) · [Currency](#currency) · [Partial success and per-statement errors](#partial-success-and-per-statement-errors) · [Presenting financials results](#presenting-financials-results) · [Mode-specific caveats](#mode-specific-caveats)

Per-ticker income statement, balance sheet, and cash flow statement —
annual, quarterly, or trailing-twelve-months. Equity-only: ETFs / indexes
/ crypto / FX / futures get empty statement lists with a `note` (their
issuers report fund-level financials, not corporate financials, and Yahoo
exposes those through `info`'s `fund` section instead).

## Run

```bash
# Default: all 3 statements, annual, ~5 periods
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py AAPL

# Quarterly statements
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --period quarterly AAPL

# Single statement
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --statement income AAPL

# TTM (income + cashflow only — balance has no TTM concept)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --period ttm AAPL

# Truncate to N most recent periods
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --limit 3 AAPL

# Summary — flat per-ticker dict (peer comparison + period-over-period growth)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --summary AAPL MSFT GOOGL

# CSV (only valid with --summary)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/financials.py --summary --format csv AAPL MSFT GOOGL
```

Tickers are positional args. Default-mode output is a JSON array with
per-statement period lists; summary-mode output is a JSON array of flat
dicts. Failed tickers carry an `error` field in either mode.

## CLI arguments

- `--statement {all,income,balance,cashflow}` — which statement(s) to
  fetch. `all` (default) returns income, balance, and cashflow. Pick a
  single statement to **shrink the output JSON, not to save time** —
  yfinance shares the underlying fundamentals payload across the three
  statement properties, so `--statement income` and `--statement all`
  cost the same Yahoo round-trip. Use the flag to save context tokens
  (~3× smaller JSON for one statement vs. all three).
- `--period {annual,quarterly,ttm}` — reporting period. `annual` (default)
  returns ~5 fiscal years; `quarterly` returns ~5–7 most-recent quarters;
  `ttm` returns one trailing-twelve-months row. **`--statement balance
  --period ttm` is rejected** at the CLI (balance sheets are point-in-time
  snapshots — no TTM). `--statement all --period ttm` omits the balance
  sheet with a top-level `note`. Programmatic callers passing
  `statements=("balance",), period="ttm"` to `fetch()` get the same
  empty-with-note result, not an error.
- `--limit N` — truncate each statement's period list to the N most recent
  periods. Default keeps all periods yfinance returns.
- `--summary` — flag. Project to a flat per-ticker dict — base meta
  (symbol, quote_type, currency, period, period_end, prev_period_end)
  + latest-period headline fields + period-over-period growth fractions.
  Run `financials.py --help` for the live field count (computed from
  `SUMMARY_BASE_KEYS` / `SUMMARY_HEADLINES` / `SUMMARY_GROWTH` in
  `scripts/financials.py`). Use for peer comparison and "what's X's
  revenue / FCF / net income" single-shot answers. Output is roughly
  10× smaller than default and renders straight to a comparison table.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array. `ndjson` emits one JSON record per line. `csv`
  is **only valid with `--summary`** (default mode has nested
  per-statement period lists that don't flatten cleanly); using
  `--format csv` without `--summary` produces an argparse error.

## Output — default mode (per-statement period lists)

Abbreviated example (full field list lives in `scripts/financials.py` →
`INCOME_FIELDS` / `BALANCE_FIELDS` / `CASHFLOW_FIELDS`). Sample numbers
are illustrative.

```jsonc
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "currency": "USD",                       // reporting currency
    "period": "annual",
    "income_stmt": [
      {
        "period_end": "2025-09-30",          // newest first
        "total_revenue": 416161000000.0,
        "gross_profit": 195201000000.0,
        "research_and_development": 34550000000.0,
        "operating_income": 133050000000.0,
        "ebitda": 144748000000.0,
        "net_income": 112010000000.0,
        "diluted_eps": 7.46
        // + 9 more: cost_of_revenue, selling_general_and_administration,
        //          operating_expense, ebit, interest_expense,
        //          pretax_income, tax_provision, basic_eps,
        //          diluted_average_shares
      },
      { "period_end": "2024-09-30", /* ... */ },
      // up to ~5 annual periods, newest first
    ],
    "balance_sheet": [
      {
        "period_end": "2025-09-30",
        "total_assets": 359241000000.0,
        "cash_and_cash_equivalents": 35934000000.0,
        "total_debt": 98657000000.0,
        "total_liabilities": 285508000000.0,
        "stockholders_equity": 73733000000.0,
        "working_capital": -2200000000.0
        // + 12 more — see schema table below
      }
      // ...
    ],
    "cashflow": [
      {
        "period_end": "2025-09-30",
        "operating_cashflow": 111482000000.0,
        "capital_expenditure": -12715000000.0,    // negative: cash out
        "free_cashflow": 98767000000.0,
        "depreciation_and_amortization": 11539000000.0
        // + 11 more
      }
      // ...
    ]
  }
]
```

A failed ticker looks like:

```json
{
  "symbol": "ZZZZNOTREAL",
  "error": "fetch failed (not_found, after 1 attempt(s))",
  "error_kind": "not_found",
  "attempts": 1
}
```

A non-equity ticker looks like:

```json
{
  "symbol": "SPY",
  "quote_type": "ETF",
  "currency": "USD",
  "period": "annual",
  "note": "financials only meaningful for equities; this is ETF",
  "income_stmt": [],
  "balance_sheet": [],
  "cashflow": []
}
```

A `--period ttm --statement all` request looks like:

```jsonc
{
  "symbol": "AAPL",
  // ...
  "period": "ttm",
  "income_stmt":   [{ "period_end": "2026-03-31", /* ... */ }],   // 1 row
  "balance_sheet": [],                                            // empty
  "cashflow":      [{ "period_end": "2026-03-31", /* ... */ }],   // 1 row
  "note": "balance sheet has no TTM concept (point-in-time snapshot); balance_sheet omitted from this period"
}
```

A `--statement income` request scopes the output — only `income_stmt`
appears, the other statement keys are absent:

```jsonc
{
  "symbol": "AAPL",
  "quote_type": "EQUITY",
  "currency": "USD",
  "period": "annual",
  "income_stmt": [
    { "period_end": "2025-09-30", "total_revenue": 416161000000.0, /* ... */ },
    // ...
  ]
  // no balance_sheet, no cashflow keys
}
```

`error_kind` ∈ `{rate_limit, not_found, network, unknown}`; `attempts`
is the retry count (1 for not_found, up to 3 for transient failures).
Surface the per-ticker error and report the rest, don't fail the whole
batch. See SKILL.md cross-cutting caveats for what each kind implies.

**Retry surfacing.** First-shot success has no `attempts` field. If any of
the per-statement Yahoo calls retried before succeeding, the response
gains `"attempts": N` at the top level (max across the calls).
`--summary` mode preserves this.

## Output — `--summary` mode

```json
[
  {
    "symbol": "AAPL",
    "quote_type": "EQUITY",
    "currency": "USD",
    "period": "annual",
    "period_end": "2025-09-30",
    "prev_period_end": "2024-09-30",
    "total_revenue": 416161000000.0,
    "gross_profit": 195201000000.0,
    "operating_income": 133050000000.0,
    "net_income": 112010000000.0,
    "ebitda": 144748000000.0,
    "diluted_eps": 7.46,
    "total_assets": 359241000000.0,
    "total_liabilities": 285508000000.0,
    "stockholders_equity": 73733000000.0,
    "cash_and_cash_equivalents": 35934000000.0,
    "total_debt": 98657000000.0,
    "operating_cashflow": 111482000000.0,
    "free_cashflow": 98767000000.0,
    "capital_expenditure": -12715000000.0,
    "revenue_growth_yoy": 0.0643,             // fraction: 6.43% YoY
    "net_income_growth_yoy": 0.1950,
    "free_cashflow_growth_yoy": -0.0923
  }
]
```

Same key set for every ticker — ETFs and other non-equities fill `note`
and null all the numeric fields. Errors look identical to default mode.

`*_growth_yoy` fields are **fractions** (matching `info`'s `revenue_growth`
encoding — `0.0643` means 6.43%, multiply ×100 for display). `null` when
prev is missing or non-positive (a negative-prev base produces sign-confused
ratios; we surface null rather than mislead).

The `_yoy` suffix disambiguates from
[`info.fundamentals.revenue_growth`](info.md), which is Yahoo's own
TTM-based revenue growth metric (computed by Yahoo, not by us, and
covering a different period than the one selected here). Both fields can
appear on the same ticker — use the suffixed one when you want the
period-bounded fiscal-year or fiscal-quarter comparison; use the
non-suffixed `revenue_growth` from `info` when you want Yahoo's TTM
headline number. The growth window depends on `--period`: see
[Period semantics](#period-semantics).

## When to use `--summary`

Reach for `--summary` when comparing 3+ tickers (peer screen, "rank by
operating margin", "who has the most cash"), or for one-shot "what's X's
revenue / net income / free cash flow" answers where you don't need
multi-period history. Default mode is ~3–5 KB per ticker per statement;
summary is ~0.5 KB. For a single ticker where you want the trend, use
default.

## Schema — line items per statement

Curated subset (~50 fields total) of the 30–70 line items yfinance
exposes. Order below matches the JSON output. All values are raw floats
(no rounding); `null` when the line item is missing for that period.
Yahoo line items NOT in the schema are **dropped** — see the comment
block in `scripts/financials.py` after `CASHFLOW_FIELDS` for the list of
deliberately-excluded items and the rationale (mostly reconciliation /
normalization rows or aggregate-already-covered detail).

**Income statement** (16 fields)

| Output key | Yahoo line item |
|---|---|
| `total_revenue` | Total Revenue |
| `cost_of_revenue` | Cost Of Revenue |
| `gross_profit` | Gross Profit |
| `research_and_development` | Research And Development |
| `selling_general_and_administration` | Selling General And Administration |
| `operating_expense` | Operating Expense |
| `operating_income` | Operating Income |
| `ebitda` | EBITDA |
| `ebit` | EBIT |
| `interest_expense` | Interest Expense |
| `pretax_income` | Pretax Income |
| `tax_provision` | Tax Provision |
| `net_income` | Net Income |
| `diluted_eps` | Diluted EPS |
| `basic_eps` | Basic EPS |
| `diluted_average_shares` | Diluted Average Shares |

**Balance sheet** (18 fields)

| Output key | Yahoo line item |
|---|---|
| `total_assets` | Total Assets |
| `current_assets` | Current Assets |
| `cash_and_cash_equivalents` | Cash And Cash Equivalents |
| `cash_cash_equivalents_and_short_term_investments` | Cash Cash Equivalents And Short Term Investments |
| `receivables` | Receivables |
| `inventory` | Inventory |
| `net_ppe` | Net PPE (property, plant, and equipment, net of depreciation) |
| `total_non_current_assets` | Total Non Current Assets |
| `current_liabilities` | Current Liabilities |
| `accounts_payable` | Accounts Payable |
| `long_term_debt` | Long Term Debt |
| `total_debt` | Total Debt |
| `net_debt` | Net Debt |
| `total_liabilities` | Total Liabilities Net Minority Interest |
| `retained_earnings` | Retained Earnings |
| `stockholders_equity` | Stockholders Equity |
| `working_capital` | Working Capital (current assets − current liabilities) |
| `ordinary_shares_number` | Ordinary Shares Number (shares outstanding at period end) |

**Cash flow statement** (15 fields)

| Output key | Yahoo line item |
|---|---|
| `net_income` | Net Income From Continuing Operations |
| `depreciation_and_amortization` | Depreciation And Amortization |
| `stock_based_compensation` | Stock Based Compensation |
| `change_in_working_capital` | Change In Working Capital |
| `operating_cashflow` | Operating Cash Flow |
| `capital_expenditure` | Capital Expenditure (negative = cash outflow) |
| `investing_cashflow` | Investing Cash Flow |
| `issuance_of_debt` | Issuance Of Debt |
| `repayment_of_debt` | Repayment Of Debt (negative = cash outflow) |
| `repurchase_of_capital_stock` | Repurchase Of Capital Stock (buybacks; negative) |
| `cash_dividends_paid` | Cash Dividends Paid (negative) |
| `financing_cashflow` | Financing Cash Flow |
| `beginning_cash_position` | Beginning Cash Position |
| `end_cash_position` | End Cash Position |
| `free_cashflow` | Free Cash Flow (operating CF + capex; capex is negative so this is opCF − \|capex\|) |

**Naming convention.** `*_cashflow` keys (`free_cashflow`,
`operating_cashflow`, `investing_cashflow`, `financing_cashflow`) use the
one-word `cashflow` spelling to match `info.py`'s `free_cashflow` /
`operating_cashflow` keys (which themselves mirror Yahoo's camelCase
`freeCashflow`). Yahoo's DataFrame index uses three-word labels ("Free
Cash Flow", etc.) but we normalize to one word so consumers running
`info` and `financials` on the same ticker see consistent keys.

**`net_income` appears in both income statement and cash flow.** The
income-statement value is "Net Income" (bottom line, the headline
earnings number). The cash-flow value is "Net Income From Continuing
Operations" — usually equal but can differ for companies with
discontinued operations. Reach for the income-statement value for
"earnings" questions; the cashflow value is just the reconciliation
starting point.

## Period semantics

- **annual**: 5 fiscal years, newest first. `period_end` is the fiscal
  year-end (e.g., `2025-09-30` for AAPL — Apple's FY ends late September,
  not December).
- **quarterly**: 5–7 most-recent quarters, newest first. `period_end` is
  the quarter-end date.
- **ttm**: 1 row of trailing-twelve-months figures (income + cashflow
  only). `period_end` is the end of the most recent quarter for which
  Yahoo has constructed a TTM rollup.

**Fiscal years are NOT calendar years.** AAPL ends FY in September, MSFT
in June, NVDA in January. When comparing across tickers, the `period_end`
dates won't align — note this when peer-tabling. (`--summary --period
quarterly` uses YoY same-quarter, which dodges most of this.)

**`--summary` growth window:**

| `--period` | `prev_period_end` is | growth direction |
|---|---|---|
| `annual` | the prior year-end (1 row back) | year-over-year |
| `quarterly` | same quarter, prior year (4 rows back if available, else 1) | year-over-year same-quarter, falling back to QoQ for thinly-covered names |
| `ttm` | n/a — only one period exists | growth fields are `null` |

The 4-back-quarterly fallback to 1-back when fewer than 5 quarters are
available means newly-IPO'd names get sequential QoQ growth instead of
silently dropping the field. The field name stays `*_growth_yoy` even in
the QoQ-fallback case — watch the `prev_period_end` field to know which
window you actually got.

When `--limit 1` (or fewer periods are available than needed), there is
no usable prev: `prev_period_end` is `null` and all `*_growth_yoy`
fields are `null`. The base headline fields still populate from the
single available period.

## Currency

The top-level `currency` field is the **reporting currency** — the
currency the financial statements are denominated in. For most tickers
this matches the ticker's trading currency, but **for ADRs and some
cross-listed names they differ** and the script does the right thing:

| Ticker | Trading currency | Reporting currency (`currency` in output) |
|---|---|---|
| `AAPL` | USD | **USD** |
| `0700.HK` (Tencent, direct HK listing) | HKD | **CNY** (Tencent reports in RMB) |
| `7203.T` (Toyota, direct Japan listing) | JPY | **JPY** |
| `TM` (Toyota, US ADR) | USD | **JPY** |
| `BABA` (Alibaba, US ADR) | USD | **CNY** |
| `PBR` (Petrobras, US ADR) | USD | **BRL** |

This is sourced from `info["financialCurrency"]`, which costs an extra
~1–2s `info` round-trip per equity ticker (vs. the cheaper `fast_info`
pre-check used elsewhere in the skill). Worth it: without it, the
monetary values would be silently mislabeled with the trading currency
for ADRs — a footgun for peer comparisons and dollar-conversion logic.

When the reporting-currency lookup can't produce a definitive answer,
the script soft-falls-back to a trading-currency surrogate and populates
`note` so callers can detect the situation.

**Important framing:** the **values** returned by yfinance's financial
statement endpoints are always in the company's actual reporting
currency (Yahoo pulls them from official filings without FX conversion).
What can be wrong on a fallback path is the **`currency` label** we
attach — never the values themselves. The notes are worded to make this
distinction clear so a reader doesn't mistakenly discount correct data
as ambiguous.

Four distinct fallback paths, all of which include the substring
`"trading currency"` in the note (so a single string match catches any
of them):

| Path | Trigger | Source used | Behavior |
|---|---|---|---|
| (a) | `info()` fetch failed (transient 429 / network) | `fast_info["currency"]` (with retry) | `currency` field labeled with trading currency; values still in actual reporting currency |
| (b) | `info` succeeded but `financialCurrency` field is absent / null | `info["currency"]` | same — label may be wrong, values are correct |
| (c) | `info` succeeded but both `financialCurrency` and `currency` are missing | `fast_info["currency"]` (last resort) | same |
| (d) | All three sources unavailable (rare — Yahoo really has nothing) | nothing — `currency` is `null` | values still correct (in reporting currency), but no label exists |

Path (b) is the most insidious — `info` looks healthy and returns most
fields, but the one field we actually care about for financials is
absent. The `currency` label gets the trading currency by default,
which would silently mislead an ADR consumer if the note weren't there.
Always check for the `note` field when sanity-checking ADR /
cross-listed responses; if it contains `"trading currency"`, the
labeled `currency` may not match the actual denomination of the
statement values, but the values themselves are reliable.

The script also normalizes Yahoo's `"None"` / `"n/a"` / `"unknown"` /
`"—"` / `"-"` string sentinels (which Yahoo occasionally returns
instead of JSON null) to None for any currency field — without this,
those would propagate as literal currency codes and look like real data.

> **Cross-currency peer warning.** If `currency` differs across rows
> (e.g., AAPL `USD` next to 0700.HK `CNY` next to TM `JPY`), monetary
> fields are **not directly comparable**. Numbers can look similar in
> magnitude but mean wildly different things (\$400B USD vs CN¥750B ≈
> \$104B USD vs ¥45T JPY ≈ \$300B USD). Either note the currency per
> row and skip ranking those columns, or FX-convert. Ratios (margin,
> growth, ROE — derived in Claude, not here) are fine across currencies.

## Partial success and per-statement errors

When `--statement all` is used and some statements succeed while others
hit transient errors (e.g., balance sheet rate-limited on its own
fetch), the script **does not** fail the whole ticker. Instead:

- Successfully-fetched statements appear with their period lists as
  usual.
- Failed statements appear as empty lists (`"balance_sheet": []`).
- A top-level `partial_errors` dict surfaces the per-statement
  `error_kind` and `attempts`:

```json
{
  "symbol": "AAPL",
  "quote_type": "EQUITY",
  "currency": "USD",
  "period": "annual",
  "income_stmt":   [ /* ... */ ],
  "balance_sheet": [],
  "cashflow":      [ /* ... */ ],
  "partial_errors": {
    "balance_sheet": { "error_kind": "rate_limit", "attempts": 3 }
  },
  "note": "partial fetch failure on: balance_sheet",
  "attempts": 3
}
```

Only when **every** attempted statement fails does the response collapse
to a top-level error dict (with the worst-priority `error_kind` —
`rate_limit > network > unknown > not_found`). When the user has asked
for a single statement (`--statement income`) and that one fails, the
result is also a top-level error — there's nothing partial about it.

**Edge case: mixed empty + errored statements.** When some attempted
statements return legitimately-empty data (no Yahoo error, just no
coverage for that specific line-item set) AND others error out, the
script still emits the partial-success schema rather than escalating.
You'll see all three statement keys present but empty, with
`partial_errors` listing only the ones that errored. Read this as
"these specific statements had transient failures and might recover on
retry; the others legitimately have no data". For example, with income
errored and balance + cashflow returning empty:

```jsonc
{
  "income_stmt":   [],   // failed; see partial_errors
  "balance_sheet": [],   // attempted, no data — Yahoo coverage gap
  "cashflow":      [],   // attempted, no data — Yahoo coverage gap
  "partial_errors": { "income_stmt": { "error_kind": "rate_limit", ... } },
  "note": "partial fetch failure on: income_stmt"
}
```

The `partial_errors` dict is the source of truth for "which statements
errored", not the empty lists themselves.

`--summary` mode preserves `partial_errors` and `note` so callers can
detect which fields might be unreliable in the flat dict (a missing
balance section means `total_assets` / `stockholders_equity` will be
null in summary even though the ticker is otherwise live).

## Presenting financials results

**Single ticker, default mode** — render headlines as a one-paragraph
takeaway plus a multi-period table for the trend:

> Apple Inc. (AAPL) — FY2025 revenue \$416.2B (+6.4% YoY), net income
> \$112.0B (+19.5%), free cash flow \$98.8B (-9.2%, capex up to \$12.7B).
> Cash + short-term investments \$66.7B against total debt \$98.7B.

Then a 5-year table with the headline rows that matter to the question.
Don't dump every line item — pick the 4–6 most relevant.

**Single statement** — a 5-period table is usually right. For income:

| Period end | Revenue | Gross profit | Operating income | Net income | Diluted EPS |
|---|---|---|---|---|---|

For cashflow, focus on the operating → free → capital allocation chain:

| Period end | Operating CF | Capex | Free CF | Buybacks | Dividends |
|---|---|---|---|---|---|

For balance, the most useful framing is the asset / liability / equity
identity plus liquidity:

| Period end | Total assets | Total debt | Stockholders equity | Cash + ST inv | Working capital |
|---|---|---|---|---|---|

**Peer comparison (`--summary` for 2+ tickers)** — render straight to a
markdown table, dropping null-everywhere columns. For same-currency peers:

| Symbol | Revenue | Op income | Net income | EBITDA | FCF | Debt | Cash | Rev growth (YoY) | NI growth (YoY) |
|---|---|---|---|---|---|---|---|---|---|

For mixed-currency peers, add a Currency column and **don't rank by
absolute monetary fields across rows** — only growth/ratio fields are
comparable. Especially watch ADR rows (TM, BABA, PBR) where `currency`
will be JPY/CNY/BRL even though the user might think of them as
"US-listed".

Number formatting:

- Convert large amounts to `T` / `B` / `M` (\$416.2B, not 416161000000).
- `*_growth_yoy` fractions → multiply ×100 and append `%`. Render `null`
  growth as `—` or `n/a`.
- `diluted_eps` is currency per share; render with `$` (escaped as `\$`).
- Negative values for `capital_expenditure`, `repurchase_of_capital_stock`,
  `cash_dividends_paid`, `repayment_of_debt` are correct — these are cash
  outflows. Don't sign-flip; describe them as "spent \$12.7B on capex"
  rather than "capex of -\$12.7B".

Always escape `$` as `\$` in prose (see SKILL.md "Cross-cutting caveats").

## Mode-specific caveats

- **Curated schema, not pass-through.** yfinance returns 30–70 line items
  per statement; this script keeps ~15 per statement. Fields outside the
  schema (`Tax Effect Of Unusual Items`, `Reconciled Cost Of Revenue`,
  `Tradeand Other Payables Non Current`, `Net PPE Purchase And Sale`,
  …) are **dropped**. The full list of deliberately-excluded items lives
  in a comment block in `scripts/financials.py` right after
  `CASHFLOW_FIELDS`. If a user asks for a missing line item, add it to
  the relevant `*_FIELDS` constant rather than reading yfinance directly.
  Adding a field is a one-line edit.
- **Sign conventions for cashflow outflows.** Capital expenditure, debt
  repayment, dividend payments, and stock buybacks come back **negative**
  (cash leaving the company). Free cash flow already incorporates this
  (`operating_cashflow + capital_expenditure`, where capex is negative).
  Don't sign-flip. When narrating, say "spent \$12.7B" not "negative
  \$12.7B".
- **Yahoo's line item taxonomy can shift.** Yahoo periodically renames
  rows ("Total Liabilities" → "Total Liabilities Net Minority Interest",
  the addition of "Tradeand Other Payables Non Current" with the
  unfortunate run-on, etc.). When a previously-populating field starts
  coming back null across many tickers, the upstream label probably
  changed — check yfinance's DataFrame index against the schema.
  `scripts/smoke.py` has invariants on key fields (`total_revenue`,
  `net_income`, `total_assets`, `operating_cashflow`, `free_cashflow`)
  to catch this.
- **Latency includes a reporting-currency `info` call for equities.**
  The script trades cheap-but-wrong (fast_info trading currency) for
  correct-but-slow (info financialCurrency) — the `info` round-trip is
  the dominant per-equity cost. Non-equities short-circuit via the
  cheaper fast_info path. See the cost / latency table in SKILL.md
  for the authoritative per-stage numbers.
- **Equity-only.** ETFs / mutual funds report fund-level financials
  (NAV, holdings, expense ratio) which Yahoo exposes through `info`'s
  `fund` section, not here. Crypto / FX / futures have no income
  statement at all. The script short-circuits non-equities to empty
  lists with a `note`. For ETF AUM / expense ratio / fund return, use
  [`info`](info.md). For "what does X do" / sector / industry on an
  equity, also use `info` — `financials` is just the numbers.
- **Financial-services and non-US accounting standards.** Banks, insurers,
  and IFRS-reporting non-US companies don't have "Cost Of Revenue" /
  "Gross Profit" in the same sense as a product company — those fields
  often come back null. Use `total_revenue`, `operating_income`, and
  `net_income` as the cross-industry-stable fields; don't rely on gross
  margin computations for banks. Also: some non-US tickers omit
  `Total Revenue` entirely from yfinance's DataFrame (verified for TM
  / Toyota at last check) — `total_revenue` will be null while
  `operating_income` / `net_income` / `ebitda` populate. Report what
  you have and note the gap; don't infer the missing field.
- **Recently-IPO'd names.** Companies with < 5 fiscal years of history
  return shorter period lists. `--summary` quarterly growth falls back
  from YoY (4-back) to QoQ (1-back) when fewer than 5 quarters are
  available — check `prev_period_end` if the comparison window matters.
  When only 1 period exists (newly listed, or `--limit 1`),
  `prev_period_end` and all `*_growth_yoy` fields are `null`.
- **`revenue_growth_yoy` vs `info`'s `revenue_growth`.** Both fields can
  appear on the same ticker with different values. `info`'s
  `revenue_growth` is Yahoo's TTM revenue growth (rolling 4-quarter sum
  vs. prior 4-quarter sum, computed by Yahoo). `financials`'
  `revenue_growth_yoy` is computed locally from the requested period
  (FY-vs-FY for `--period annual`, same-quarter-prior-year for
  `--period quarterly`). Use the suffixed one when you want a specific
  fiscal-period comparison; use the non-suffixed `info` field when you
  want Yahoo's headline TTM number.
- **No segment / geographic breakdown.** Yahoo doesn't expose segment
  reporting through this endpoint. For "iPhone vs Services revenue"
  questions, the data isn't here — point the user at the company's
  10-K / 10-Q on EDGAR.
