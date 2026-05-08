#!/usr/bin/env python3
"""Fetch yfinance insider data (purchases summary, Form 4 transactions,
current insider roster) for one or more tickers and print as JSON / NDJSON
/ CSV.

See `insiders.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed / non-applicable tickers carry an "error" or a
"note" field instead of data so a single bad symbol does not poison the
batch. Field schema lives in the *_KEYS / *_CSV_COLS constants below.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, safe_int, safe_str, with_retry,
)

import yfinance as yf


# Yahoo's three insider properties cover operating-company equities. Empirically
# verified (2026-05) to return three EMPTY DataFrames (shape (0,0)) for: ETFs
# (QQQ), indexes (^GSPC), crypto (BTC-USD), and bogus / delisted tickers — for
# all four yfinance prints "HTTP Error 404" on stderr but does not raise.
#
# An all-empty result is genuinely ambiguous (non-equity / bogus / low-coverage),
# same as holders. Emit success-with-note rather than paying a fast_info
# pre-check; callers who need disambiguation can chain fast_info themselves.
#
# Partial-empty IS NOT in the same bucket. BMW.DE and TM (Toyota ADR) return a
# populated `purchases` rollup but empty transactions + empty roster — that's a
# real equity with partial Yahoo coverage. We surface a `coverage_note` (a
# different field from `note`) so consumers don't silently misread the empty
# event lists as "no activity"; the lists really ARE the answer for these
# tickers, but the asymmetry is non-obvious and worth flagging in-band.
_EMPTY_NOTE = (
    "no insider data (Yahoo's insider endpoints cover operating-company "
    "equities; ETFs / indexes / crypto / FX / futures and bogus tickers all "
    "return three empty frames — call fast_info to disambiguate)"
)

# Soft signal for the partial-empty case (purchases rollup populated, but
# transactions + roster both empty). Verified for BMW.DE and TM (Toyota ADR)
# in 2026-05. Distinct from `_EMPTY_NOTE` (mutually exclusive at the result
# level): `note` fires when there's NO data and the cause is ambiguous;
# `coverage_note` fires when there IS rollup data but Yahoo's per-event
# tables aren't populated (typical for non-US issuers and some ADRs where
# the 13F-style aggregate exists but Form 4 equivalents don't).
_COVERAGE_NOTE_PARTIAL = (
    "purchases_summary populated but transactions + roster empty — typical "
    "for non-US issuers / ADRs where Yahoo aggregates the 6-month rollup "
    "but doesn't expose per-event filings (the empty lists ARE the answer, "
    "not a fetch failure)"
)


# --- purchases_summary projection -------------------------------------------
#
# Yahoo's `insider_purchases` is a transposed metric table: 7 rows × 3 cols.
# Column 0 name carries the period ("Insider Purchases Last 6m" — the "Last 6m"
# is the only place the rollup window appears in the response). Rows are seven
# fixed metrics, in this order:
#
#   0. Purchases                  Shares + Trans (count)
#   1. Sales                      Shares + Trans
#   2. Net Shares Purchased (Sold) Shares + Trans
#   3. Total Insider Shares Held  Shares only (Trans = NA)
#   4. % Net Shares Purchased (Sold)  Shares only (FRACTION)
#   5. % Buy Shares               Shares only (FRACTION)
#   6. % Sell Shares              Shares only (FRACTION)
#
# Verified empirically (2026-05, AAPL): % Net = 0.001 with Net=246332 and
# Total Held=240872640 → 246332/240872640 ≈ 0.00102. So the "%" rows are
# **fractions**, NOT percent-encoded. Document loudly — easy mistake to make
# given the row label has a "%" sigil.
#
# Project to a flat dict so callers don't have to reproduce row-label parsing.
# Map by row-label substring (case-insensitive) so Yahoo can reorder rows or
# rephrase ("Last 6m" → "Last 12m") without breaking us — though we still
# require the exact label tokens.
_PURCHASES_KEYS = (
    "period_label",
    "purchases_shares", "purchases_count",
    "sales_shares", "sales_count",
    "net_shares_purchased", "net_count",
    "total_insider_shares_held",
    "pct_net_shares_purchased",
    "pct_buy_shares",
    "pct_sell_shares",
)

# Per-transaction schema (insider_transactions). All 9 Yahoo columns are
# preserved, projected to JSON-friendly types. `transaction_code` and `url`
# are empirically empty in current Yahoo responses (AAPL: 0/73 rows have
# either populated) — kept in the schema so a future Yahoo change that fills
# them silently surfaces rather than getting dropped. `value` is float (Yahoo
# emits NaN for non-monetary events like option grants); `shares` is int.
# `value` is in the ticker's TRADING currency (= fast_info.currency).
# `ownership` is the single letter Yahoo emits ('D' = direct, 'I' = indirect).
#
# Naming: `transaction_text` is the human-readable description column
# (Yahoo's `Text`, e.g. "Sale at price 275.00 per share."); `transaction_code`
# is Yahoo's `Transaction` column, presumed to hold a coded type string.
# Renamed from a bare `transaction` because the bare name was indistinguishable
# from `transaction_text` to JSON readers — a reader couldn't tell which was
# the description and which was the code without the schema doc. The `_code`
# suffix makes the role explicit.
_TRANSACTION_KEYS = (
    "date",
    "insider",
    "position",
    "ownership",
    "shares",
    "value",
    "transaction_text",
    "transaction_code",
    "url",
)

# Per-roster-member schema. AAPL exposes 7 columns (no indirect holdings);
# TSLA 9 (indirect cols present). We project the indirect pair as None when
# absent. `latest_transaction_date` and `position_direct_date` /
# `position_indirect_date` are dates; `most_recent_transaction` is a
# human-readable string ("Sale", "Purchase", "Stock Gift", ...).
_ROSTER_KEYS = (
    "name",
    "position",
    "most_recent_transaction",
    "latest_transaction_date",
    "shares_owned_directly",
    "position_direct_date",
    "shares_owned_indirectly",
    "position_indirect_date",
    "url",
)

# Default-mode CSV columns. `record_class` discriminates the three section
# row classes — same pattern as holders.py's `holder_class`. Section-specific
# columns are populated only on rows of that class; `symbol` repeats. Empty
# / errored tickers emit a single row carrying symbol + note + meta.
#
# `dict.fromkeys(...)` deduplicates while preserving order: `position` and
# `url` appear in both _TRANSACTION_KEYS and _ROSTER_KEYS (both record types
# semantically have a position/url), so without dedupe the CSV header would
# have those columns twice and a row-dict lookup would populate both with
# the same value. Deduped, the columns are shared — readers disambiguate
# via `record_class`.
_DEFAULT_CSV_COLS = tuple(dict.fromkeys((
    "symbol", "record_class",
    *_PURCHASES_KEYS,
    *_TRANSACTION_KEYS,
    *_ROSTER_KEYS,
    "note", "coverage_note", *RESULT_META,
)))

# Summary-mode flat projection. Lifts the rollup + adds *_returned counts +
# the most recent transaction date as a recency signal + the largest single
# direct-shares insider holder for peer compare. `*_returned` are computed
# from the FULL pre-limit lists (matches holders' invariant — the metric
# describes Yahoo's response, not the display knob).
#
# `top_insider_by_direct_shares` / `top_insider_direct_shares` mirror
# holders' `top_institution` / `top_institution_pct` peer-compare signal.
# Computed as the roster row with the highest non-null
# `shares_owned_directly`; ties broken by Yahoo's order. Indirect holdings
# deliberately ignored — they're a different concept (held via trusts /
# foundations) and mixing the two would silently surface a non-comparable
# number depending on the ticker (TSLA's Musk would dominate any stock he
# touches via indirect; AAPL's Cook would lead via direct).
_SUMMARY_FLAT_KEYS = (
    *_PURCHASES_KEYS,
    "transactions_returned",
    "latest_transaction_date",
    "roster_returned",
    "top_insider_by_direct_shares",
    "top_insider_direct_shares",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS,
                     "note", "coverage_note", *RESULT_META)


def _fetch_three(symbol: str):
    """Fetch all three insider DataFrames in one retry-wrapped call.

    Three property reads share one backend `quoteSummary` HTTP request —
    verified by timing (2026-05): first read ~1.2s cold, next two ~0ms.
    Same pattern as holders.py's `_fetch_three`; one retry around the trio
    rather than three independent retries avoids the partial-failure mode
    where one section is stale while another is fresh.
    """
    def _f():
        t = yf.Ticker(symbol)
        # Materialize all three before returning so any exception falls
        # inside this function (and thus into with_retry's try/except).
        return (t.insider_purchases,
                t.insider_transactions,
                t.insider_roster_holders)
    return with_retry(_f)


def _ts_to_date(ts) -> str | None:
    """pandas Timestamp / NaT / None → 'YYYY-MM-DD' or None."""
    if ts is None:
        return None
    # NaT compares unequal to itself (NaN-like).
    try:
        if ts != ts:
            return None
    except TypeError:
        pass
    try:
        return ts.strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return None


def _project_purchases(df) -> dict:
    """insider_purchases (7-row metric table) → flat dict.

    Robust to row reorder: each metric is matched by case-insensitive
    substring on the first column's value. Unknown rows are silently
    skipped — surface in smoke if Yahoo adds new metrics.
    """
    out = {k: None for k in _PURCHASES_KEYS}
    if df is None or df.empty:
        return out
    if len(df.columns) < 3:
        return out
    label_col = df.columns[0]
    # The label column header itself carries the rollup window
    # ("Insider Purchases Last 6m" → "Last 6m"). Strip the boilerplate
    # prefix so callers get just the period.
    #
    # Case-insensitive regex: a plain `.replace("Insider Purchases", "")`
    # would silently pass through the whole header verbatim if Yahoo
    # changed casing ("INSIDER PURCHASES Last 6m" or "insider purchases
    # last 6m") — the smoke canary on `period_label == "Last 6m"` would
    # fail with a confusing "got 'INSIDER PURCHASES Last 6m'" message.
    # The `\b` word boundary anchors to the start so we strip only the
    # leading boilerplate, not any in-string occurrence.
    if isinstance(label_col, str):
        stripped = re.sub(r'^\s*insider\s+purchases\s*', '', label_col, flags=re.I).strip()
        out["period_label"] = stripped if stripped else None
    else:
        out["period_label"] = None
    # Row-label → (shares_key, count_key) routing. None means "this row has
    # no count value (Trans column is NA)".
    #
    # ORDER MATTERS — first match wins. The four "% ..." rows come first
    # because "% Net Shares Purchased (Sold)" also contains the substring
    # "net shares" — without the `%`-prefix tokens going first, row 4
    # would route to `net_shares_purchased` (the count row's slot) and
    # overwrite the row-2 value. Same trap would apply to "% Buy Shares"
    # vs a hypothetical "Buy Shares" row, so we hold the same invariant
    # for all three percent rows.
    #
    # Why substring (`in`) and not exact / regex match: Yahoo's row labels
    # have varied wording across observed payloads ("Net Shares Purchased
    # (Sold)" vs hypothetical "Net Shares Purchased") and the substring
    # approach tolerates that without a label-by-label maintenance burden.
    # Empirically the seven tokens here don't false-positive against each
    # other across the 7 known rows (verified 2026-05); add a routing
    # entry above any token it would alias against if Yahoo introduces a
    # new metric row.
    routing = (
        ("% net shares",      "pct_net_shares_purchased", None),
        ("% buy shares",      "pct_buy_shares",           None),
        ("% sell shares",     "pct_sell_shares",          None),
        ("purchases",         "purchases_shares",         "purchases_count"),
        ("sales",             "sales_shares",             "sales_count"),
        ("net shares",        "net_shares_purchased",     "net_count"),
        ("total insider",     "total_insider_shares_held", None),
    )
    for _, row in df.iterrows():
        label = safe_str(row.get(label_col))
        if not label:
            continue
        label_lc = label.lower()
        for token, shares_key, count_key in routing:
            if token in label_lc:
                shares_val = row.get("Shares")
                # The "%" rows are FRACTIONS (verified empirically); the others
                # are integer share counts. safe_int truncates; the % rows want
                # safe_float to preserve 4 decimals.
                if shares_key.startswith("pct_"):
                    out[shares_key] = safe_float(shares_val)
                else:
                    out[shares_key] = safe_int(shares_val)
                if count_key is not None:
                    out[count_key] = safe_int(row.get("Trans"))
                break
    return out


def _project_transaction(row) -> dict:
    return {
        "date": _ts_to_date(row.get("Start Date")),
        "insider": safe_str(row.get("Insider")),
        "position": safe_str(row.get("Position")),
        "ownership": safe_str(row.get("Ownership")),
        "shares": safe_int(row.get("Shares")),
        "value": safe_float(row.get("Value")),
        "transaction_text": safe_str(row.get("Text")),
        # Yahoo's `Transaction` column → our `transaction_code`. Empirically
        # empty in current responses; renamed for clarity vs `transaction_text`.
        "transaction_code": safe_str(row.get("Transaction")),
        "url": safe_str(row.get("URL")),
    }


def _project_transactions(df) -> list[dict]:
    if df is None or df.empty:
        return []
    rows = df.to_dict(orient="records")
    return [_project_transaction(r) for r in rows]


def _project_roster_row(row) -> dict:
    # `.get(col)` on a pandas Series returns None when the column doesn't
    # exist (AAPL has no indirect cols; TSLA does). Lets us project both
    # variants with one schema rather than branching on column set.
    return {
        "name": safe_str(row.get("Name")),
        "position": safe_str(row.get("Position")),
        "most_recent_transaction": safe_str(row.get("Most Recent Transaction")),
        "latest_transaction_date": _ts_to_date(row.get("Latest Transaction Date")),
        "shares_owned_directly": safe_int(row.get("Shares Owned Directly")),
        "position_direct_date": _ts_to_date(row.get("Position Direct Date")),
        "shares_owned_indirectly": safe_int(row.get("Shares Owned Indirectly")),
        "position_indirect_date": _ts_to_date(row.get("Position Indirect Date")),
        "url": safe_str(row.get("URL")),
    }


def _project_roster(df) -> list[dict]:
    if df is None or df.empty:
        return []
    rows = df.to_dict(orient="records")
    return [_project_roster_row(r) for r in rows]


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for peer comparison.

    Reads from the FULL (pre-limit) lists so `transactions_returned` /
    `roster_returned` describe Yahoo's response, not the display knob —
    same invariant as holders._summarize.

    Carries `note` / `coverage_note` / `error` / `error_kind` / `attempts`
    through unchanged so tickers in the all-empty / partial-empty / failed
    states still surface in summary CSVs (otherwise the disambiguation
    signal silently drops in tabular output — same defensive shape as
    news / holders / options).
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}
    purchases = full.get("purchases_summary") or {}
    for k in _PURCHASES_KEYS:
        out[k] = purchases.get(k)

    transactions = full.get("transactions") or []
    out["transactions_returned"] = len(transactions)
    if transactions:
        # Find the most recent date (skip Nones). Don't assume Yahoo sorts
        # desc — observed sort is desc but a future Yahoo change shouldn't
        # silently flip latest to oldest.
        dates = [t["date"] for t in transactions if t.get("date")]
        out["latest_transaction_date"] = max(dates) if dates else None

    roster = full.get("roster") or []
    out["roster_returned"] = len(roster)
    if roster:
        # Largest direct-shares holder. Indirect holdings deliberately
        # excluded (see _SUMMARY_FLAT_KEYS comment for the rationale).
        # `max(default=...)` so an all-None roster doesn't crash.
        with_shares = [r for r in roster
                       if r.get("shares_owned_directly") is not None]
        if with_shares:
            top = max(with_shares, key=lambda r: r["shares_owned_directly"])
            out["top_insider_by_direct_shares"] = top.get("name")
            out["top_insider_direct_shares"] = top.get("shares_owned_directly")

    for k in ("note", "coverage_note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def _apply_limit(result: dict, limit: int | None) -> dict:
    """Truncate transactions / roster lists to `limit` rows IN-PLACE.

    **Mutates `result`.** Mirrors holders._apply_limit's contract — the
    object is modified directly; return value is the same object. Used
    only by the default-mode emit path. `--summary` mode never calls this.
    No-op when `limit is None`. Does not affect `purchases_summary`.
    """
    if limit is None:
        return result
    if "transactions" in result:
        result["transactions"] = result["transactions"][:limit]
    if "roster" in result:
        result["roster"] = result["roster"][:limit]
    return result


def fetch(symbol: str) -> dict:
    """Fetch the full Yahoo insider payload for `symbol`. No `--limit` —
    callers slice via `_apply_limit` (default mode) or read from full
    lists (`--summary`)."""
    result, err_kind, attempts = _fetch_three(symbol)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    purchases_df, transactions_df, roster_df = result
    purchases_summary = _project_purchases(purchases_df)
    transactions = _project_transactions(transactions_df)
    roster = _project_roster(roster_df)

    out = {
        "symbol": symbol,
        "purchases_summary": purchases_summary,
        "transactions": transactions,
        "roster": roster,
    }
    # Three result classes — `note` and `coverage_note` are mutually
    # exclusive at the result level (see _COVERAGE_NOTE_PARTIAL):
    #
    # 1. All-empty (purchases all-None AND transactions empty AND roster
    #    empty). Ambiguous: non-equity / bogus / low-coverage. → `note`,
    #    no `coverage_note`. Caller chains fast_info to disambiguate.
    # 2. Partial-empty (purchases has data BUT transactions+roster both
    #    empty). Real data with asymmetric Yahoo coverage — verified for
    #    BMW.DE and TM (Toyota ADR). → `coverage_note`, no `note`. The
    #    empty event lists ARE the answer; consumers should know they're
    #    not a transient gap.
    # 3. Anything else (any of: full coverage, transactions empty but
    #    roster populated, roster empty but transactions populated). →
    #    neither field set; the data is unambiguously what Yahoo gave us.
    purchases_has_data = any(v is not None for v in purchases_summary.values())
    if not purchases_has_data and not transactions and not roster:
        out["note"] = _EMPTY_NOTE
    elif purchases_has_data and not transactions and not roster:
        out["coverage_note"] = _COVERAGE_NOTE_PARTIAL
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance insider data for one or more tickers.\n\n"
            "Three sections per ticker:\n"
            "  purchases_summary  — Last 6m rollup: shares purchased / sold /\n"
            "                       net + transaction counts + % of holdings\n"
            "                       (from insider_purchases)\n"
            "  transactions       — Form 4 events: insider, position, ownership\n"
            "                       (D/I), shares, value, date (last ~24 months)\n"
            "                       (from insider_transactions)\n"
            "  roster             — current insider holders with shares owned\n"
            "                       directly + indirectly (from\n"
            "                       insider_roster_holders)\n\n"
            "All three share one backend HTTP request (verified by timing:\n"
            "first read ~1.2s, next two ~0ms), so cost is the same as\n"
            "fetching just one. Equity-focused: ETFs / indexes / crypto / FX /\n"
            "futures all return three empty frames (success-with-`note`); bogus\n"
            "tickers also return empty (ambiguous — chain fast_info to\n"
            "disambiguate). Some real equities (BMW.DE, ADRs like TM) return\n"
            "purchases_summary populated but transactions+roster empty —\n"
            "surfaced via a separate `coverage_note` field (mutually exclusive\n"
            "with `note`). Distinguish: `note` = no data, ambiguous; \n"
            "`coverage_note` = real data, asymmetric Yahoo coverage.\n\n"
            "CSV default-mode output uses a `record_class` column with values\n"
            "purchases / transaction / roster as a row-class discriminator,\n"
            "with the symbol col repeating across a ticker's rows.\n\n"
            "UNITS: pct_net_shares_purchased / pct_buy_shares / pct_sell_shares\n"
            "are FRACTIONS (0.001 = 0.1%) — NOT percent-encoded. shares is int;\n"
            "value is float in the ticker's TRADING currency (USD for AAPL,\n"
            "HKD for 0700.HK); Yahoo emits NaN for non-monetary events."
        ),
        epilog=(
            "Examples:\n"
            "  insiders.py AAPL                                  # full sections\n"
            "  insiders.py --summary AAPL MSFT GOOGL             # peer rollup\n"
            "  insiders.py --limit 10 AAPL                       # top 10 transactions / roster\n"
            "  insiders.py --format csv --summary AAPL MSFT GOOGL\n"
            "  insiders.py --format csv AAPL                     # one row per record\n"
            "                                                    # (tagged purchases/transaction/roster)\n"
            "\n"
            "See references/insiders.md for the field schema, presentation guidance,\n"
            "and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap transactions / roster rows per ticker. Default: "
                         "keep all (Yahoo returns up to ~24 months of "
                         "transactions — can be 70+ rows for active large-caps "
                         "— and ~10 roster rows). Does not affect "
                         "purchases_summary. "
                         "Silently ignored in --summary mode — the flat "
                         "metrics (transactions_returned / roster_returned) "
                         "are computed from Yahoo's full response and stay "
                         "invariant under --limit by design.")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: rollup fields + "
                         "transactions_returned / roster_returned + most "
                         "recent transaction date + top_insider_by_direct_"
                         "shares / top_insider_direct_shares (largest direct "
                         "holder from the roster, indirect holdings excluded). "
                         "Useful for peer-comparison tables; ~5-10× smaller "
                         "output than default.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = default mode emits one row per RECORD (with a "
                         "`record_class` discriminator: purchases / transaction "
                         "/ roster), with the symbol col repeating; "
                         "--summary csv emits strict one-row-per-ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    results = [fetch(s.strip().upper())
               for s in args.symbols if s.strip()]

    if args.summary:
        # _summarize reads from the full pre-limit lists, so --limit is a
        # no-op in this branch by design — the metric should describe
        # Yahoo's response, not the display knob.
        results = [_summarize(r) for r in results]
        _emit_summary(results, args.format)
    else:
        results = [_apply_limit(r, args.limit) for r in results]
        _emit_default(results, args.format)


def _emit_default(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # CSV: row-per-record, with a `record_class` discriminator so all three
    # sections (purchases rollup + transactions + roster) live in one table.
    # Empty / errored tickers emit a single row carrying symbol + note + meta.
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        # carry covers both `note` (all-empty) and `coverage_note`
        # (partial-empty). The two are mutually exclusive at the result
        # level (set in fetch()); included together here so a single
        # `carry` dict serves both the short-circuit row (note path) and
        # the per-record data rows (coverage_note path, which still has
        # data to emit).
        carry = {k: r.get(k, "")
                 for k in ("note", "coverage_note", *RESULT_META) if k in r}
        # Errored or all-empty: one carry row, no record data. coverage_note
        # is NOT a short-circuit — partial-empty tickers DO have purchases
        # data to emit, so they fall through to the per-record loop.
        if "error" in r or "note" in r:
            row = {"symbol": symbol, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        # Purchases rollup row — emitted only when at least one rollup field
        # is populated. record_class="purchases" so a consumer filtering by
        # class can either include or skip it.
        purchases = r.get("purchases_summary") or {}
        if any(v is not None for v in purchases.values()):
            row = {"symbol": symbol, "record_class": "purchases",
                   **purchases, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        for tx in r.get("transactions", []):
            row = {"symbol": symbol, "record_class": "transaction",
                   **tx, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        for ros in r.get("roster", []):
            row = {"symbol": symbol, "record_class": "roster",
                   **ros, **carry}
            writer.writerow([row.get(c, "") for c in cols])


def _emit_summary(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    import csv as _csv
    cols = list(_SUMMARY_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
