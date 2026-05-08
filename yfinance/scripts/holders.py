#!/usr/bin/env python3
"""Fetch yfinance ownership data (insider/institution rollup, top
institutional holders, top mutual-fund holders) for one or more tickers
and print as JSON / NDJSON / CSV.

See `holders.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed / non-applicable tickers carry an "error" or a
"note" field instead of data so a single bad symbol does not poison the
batch. Field schema lives in the *_KEYS / *_CSV_COLS constants below.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, safe_int, safe_str, with_retry,
)

import yfinance as yf


# Yahoo's holders endpoint covers operating-company equities. Empirically
# verified (2026-05) to return three empty DataFrames: ETFs (QQQ), mutual
# funds (VFIAX), indexes (^GSPC), crypto (BTC-USD), FX (EURUSD=X, JPY=X),
# futures (ES=F, GC=F), and bogus / delisted tickers — yfinance logs an
# HTTP 404 for the bogus case but does not raise.
#
# An all-empty result is genuinely ambiguous — we can't distinguish
# "non-equity" from "low-coverage equity" from "bogus" without an extra
# fast_info round-trip. Rather than pay for that pre-check (~0.3s) on
# every call, we report the empty case as success-with-note and document
# the ambiguity. Callers can chain a fast_info call if they need to
# disambiguate (it returns quote_type for valid symbols, not_found for bogus).
_EMPTY_NOTE = (
    "no holder data (Yahoo's holders endpoint covers operating-company "
    "equities; ETFs / mutual funds / indexes / crypto / FX / futures "
    "return empty, as do bogus tickers and very low-coverage equities — "
    "call fast_info to disambiguate)"
)

# major_holders rollup index labels (Yahoo's keys, returned literal). All
# four are float64 in pandas; the first three are FRACTIONS (0.0164 = 1.64%
# insider ownership), the fourth is an integer count. Document loudly — these
# are NOT percent-encoded, distinct from info's mixed conventions.
_SUMMARY_KEYS = (
    "insiders_pct",
    "institutions_pct",
    "institutions_float_pct",
    "institutions_count",
)
_MAJOR_INDEX_TO_KEY = {
    "insidersPercentHeld": "insiders_pct",
    "institutionsPercentHeld": "institutions_pct",
    "institutionsFloatPercentHeld": "institutions_float_pct",
    "institutionsCount": "institutions_count",
}

# Per-holder schema (institutional + mutualfund use the same shape — Yahoo
# returns identical 6-column DataFrames for both). pct_held and pct_change
# are FRACTIONS; value is in the ticker's TRADING currency (USD for AAPL,
# HKD for 0700.HK, EUR for BMW.DE) — call fast_info to resolve which.
_HOLDER_KEYS = (
    "date_reported",
    "holder",
    "pct_held",
    "shares",
    "value",
    "pct_change",
)

# Default-mode CSV columns. We split the rollup into ITS OWN row class so
# all three sections fit in one CSV — `holder_class` is the discriminator:
#   summary       — single row carrying the rollup pcts/count (holder col empty)
#   institutional — one row per institutional holder
#   mutualfund    — one row per mutual fund holder
# The four `_pct` / `_count` columns are populated only on the summary row;
# the per-holder columns (date_reported, holder, ...) are populated only on
# institutional / mutualfund rows. Empty/error tickers emit a single row
# carrying symbol + note + meta so they're not silently dropped.
_DEFAULT_CSV_COLS = (
    "symbol", "holder_class",
    *_SUMMARY_KEYS,
    *_HOLDER_KEYS,
    "note", *RESULT_META,
)

# Summary-mode flat projection. Rollup fields lifted to top level + the
# single-best institutional / mutualfund holder + a top-5 sum for each list
# (handy peer-comparison signal: "how concentrated is the ownership"). When
# the per-list count is < 5, top5 falls back to summing whatever's there;
# when 0, top5 is None.
#
# `*_rows_returned` (post-limit row count) is intentionally distinct from
# `summary.institutions_count` (the rollup figure — total institutions on
# file with the SEC, often tens of thousands). The fully-spelled name is
# uglier but the prior `institutional_count` collided in conversation with
# `institutions_count` and made bug reports painful to disambiguate.
_SUMMARY_FLAT_KEYS = (
    *_SUMMARY_KEYS,
    "top_institution", "top_institution_pct",
    "top5_institutions_pct", "institutional_rows_returned",
    "top_mutualfund", "top_mutualfund_pct",
    "top5_mutualfunds_pct", "mutualfund_rows_returned",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS, "note", *RESULT_META)


def _fetch_three(symbol: str):
    """Fetch all three holders DataFrames in a single retry-wrapped call.

    The three property reads appear to share one backend `quoteSummary`
    HTTP request — observed timing (not source-confirmed): first read
    ~120 ms, next two ~0 ms. The 0 ms reads on properties 2 and 3 are
    consistent with either yfinance batching the modules or a session-
    level HTTP cache; we haven't read upstream source to disambiguate.
    Either way: there is no benefit to retrying each property
    independently — if the underlying request 429s, all three would
    429 together. One with_retry around the trio mirrors the observed
    single-call cost, halves code complexity vs three independent
    retries, and avoids the partial-failure mode where one section
    is stale while another is fresh.
    """
    def _f():
        t = yf.Ticker(symbol)
        # Materialize all three before returning so any exception falls
        # inside this function (and thus into with_retry's try/except).
        return t.major_holders, t.institutional_holders, t.mutualfund_holders
    return with_retry(_f)


def _ts_to_date(ts) -> str | None:
    """pandas Timestamp / NaT / None → 'YYYY-MM-DD' or None."""
    if ts is None:
        return None
    # NaT compares unequal to itself (NaN-like) — easiest check.
    try:
        if ts != ts:
            return None
    except TypeError:
        pass
    try:
        return ts.strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return None


def _project_summary(major_df) -> dict:
    """major_holders DataFrame → flat dict keyed by _SUMMARY_KEYS.

    Iterating index → known-key mapping lets us tolerate Yahoo reordering
    rows or adding new ones without blowing up. Unknown rows are silently
    skipped — surface them in smoke if anything important shows up.
    """
    out = {k: None for k in _SUMMARY_KEYS}
    if major_df is None or major_df.empty:
        return out
    for raw_key, our_key in _MAJOR_INDEX_TO_KEY.items():
        if raw_key in major_df.index:
            v = major_df.loc[raw_key, "Value"]
            # institutions_count is conceptually int; the others are
            # fractions in [0, 1] — keep float to preserve precision.
            out[our_key] = safe_int(v) if our_key == "institutions_count" else safe_float(v)
    return out


def _project_holder_row(row) -> dict:
    return {
        "date_reported": _ts_to_date(row.get("Date Reported")),
        "holder": safe_str(row.get("Holder")),
        "pct_held": safe_float(row.get("pctHeld")),
        "shares": safe_int(row.get("Shares")),
        "value": safe_int(row.get("Value")),
        "pct_change": safe_float(row.get("pctChange")),
    }


def _project_holders(df) -> list[dict]:
    """Project all rows from `df`. `--limit` is applied later by
    `_apply_limit` so summary metrics see the full Yahoo response."""
    if df is None or df.empty:
        return []
    rows = df.to_dict(orient="records")
    return [_project_holder_row(r) for r in rows]


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for peer comparison.

    Reads from the FULL (pre-limit) institutional / mutualfund lists so
    `top5_*_pct` and `*_rows_returned` are invariant to the user's
    `--limit` choice — those metrics are meant to describe Yahoo's
    response, not the display knob. (Earlier version sliced post-limit
    and silently misreported `top5` as `top<limit>` when limit < 5.)

    Carries `note` / `error` / `error_kind` / `attempts` through unchanged
    so tickers in the empty / failed states still surface in summary CSVs
    rather than collapsing to all-None rows that look identical to a
    legitimate zero-coverage equity.
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}
    summary = full.get("summary") or {}
    for k in _SUMMARY_KEYS:
        out[k] = summary.get(k)

    inst = full.get("institutional") or []
    out["institutional_rows_returned"] = len(inst)
    if inst:
        out["top_institution"] = inst[0].get("holder")
        out["top_institution_pct"] = inst[0].get("pct_held")
        # Sum whatever's available up to 5; skip None entries so a single
        # missing pct_held doesn't poison the whole sum (rare — Yahoo
        # populates pctHeld consistently in the observed payloads).
        top5 = [r.get("pct_held") for r in inst[:5] if r.get("pct_held") is not None]
        out["top5_institutions_pct"] = sum(top5) if top5 else None

    mf = full.get("mutualfund") or []
    out["mutualfund_rows_returned"] = len(mf)
    if mf:
        out["top_mutualfund"] = mf[0].get("holder")
        out["top_mutualfund_pct"] = mf[0].get("pct_held")
        top5 = [r.get("pct_held") for r in mf[:5] if r.get("pct_held") is not None]
        out["top5_mutualfunds_pct"] = sum(top5) if top5 else None

    for k in ("note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def _apply_limit(result: dict, limit: int | None) -> dict:
    """Truncate institutional / mutualfund lists to `limit` rows IN-PLACE.

    **Mutates `result`.** The dict object passed in is modified directly;
    the return value is the same object (for fluent chaining), not a copy.
    A caller doing `_apply_limit(orig, 3)` will see `orig["institutional"]`
    truncated to 3 rows, even if they discard the return value. If you need
    to preserve the original, deep-copy first.

    Used only by the default-mode emit path. `--summary` mode never calls
    this — its flat metrics are computed from full lists in `_summarize`,
    so `--limit` is silently a no-op for summary output. (Documented in
    the `--limit` help text.) No-op when `limit is None`.
    """
    if limit is None:
        return result
    if "institutional" in result:
        result["institutional"] = result["institutional"][:limit]
    if "mutualfund" in result:
        result["mutualfund"] = result["mutualfund"][:limit]
    return result


def fetch(symbol: str) -> dict:
    """Fetch the full Yahoo holders payload for `symbol`. No `--limit` —
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
    major_df, inst_df, mf_df = result
    summary = _project_summary(major_df)
    institutional = _project_holders(inst_df)
    mutualfund = _project_holders(mf_df)

    out = {
        "symbol": symbol,
        "summary": summary,
        "institutional": institutional,
        "mutualfund": mutualfund,
    }
    # All three sections empty: ambiguous (non-equity / bogus / low-coverage).
    # Yahoo gives us identical empty payloads for all three causes, so we
    # surface a note rather than guessing. We DON'T flip this to error_kind:
    # not_found because for a real low-coverage equity the ticker is fine —
    # there's just no holder data. Caller can chain fast_info to disambiguate.
    all_empty = (all(v is None for v in summary.values())
                 and not institutional and not mutualfund)
    if all_empty:
        out["note"] = _EMPTY_NOTE
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance ownership data for one or more tickers.\n\n"
            "Three sections per ticker:\n"
            "  summary       — insider / institution / float-of-institution percentages\n"
            "                  + institutions_count rollup (from major_holders)\n"
            "  institutional — top ~10 institutional holders\n"
            "  mutualfund    — top ~10 mutual-fund holders\n\n"
            "All three appear to share one backend HTTP request (observed\n"
            "timing; not source-confirmed), so cost is the same as fetching\n"
            "just one. Equity-focused: ETFs / mutual funds / indexes / crypto /\n"
            "FX / futures all return empty (success-with-note, all verified\n"
            "empirically); bogus tickers also return empty (ambiguous — chain\n"
            "fast_info to disambiguate).\n\n"
            "CSV default-mode output uses a `holder_class` column with values\n"
            "summary / institutional / mutualfund as a row-class discriminator,\n"
            "with the symbol col repeating across a ticker's rows.\n\n"
            "UNITS: pct_held, pct_change, insiders_pct, institutions_pct,\n"
            "institutions_float_pct are all FRACTIONS (0.0971 = 9.71%) — NOT\n"
            "percent-encoded. value is in the ticker's TRADING currency."
        ),
        epilog=(
            "Examples:\n"
            "  holders.py AAPL                                   # full sections\n"
            "  holders.py --summary AAPL MSFT GOOGL              # peer rollup\n"
            "  holders.py --limit 5 AAPL                         # top 5 only (default mode)\n"
            "  holders.py --format csv --summary AAPL MSFT GOOGL\n"
            "  holders.py --format csv AAPL                      # one row per holder\n"
            "                                                    # (tagged summary/institutional/mutualfund)\n"
            "\n"
            "See references/holders.md for the field schema, presentation guidance,\n"
            "and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap institutional / mutualfund rows per ticker. "
                         "Default: keep all (Yahoo returns up to ~10 each). "
                         "Does not affect the summary section. "
                         "Silently ignored in --summary mode — the flat "
                         "metrics (top5_*_pct, *_rows_returned) are "
                         "computed from Yahoo's full response and stay "
                         "invariant under --limit by design.")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: rollup pcts + the single "
                         "best institutional / mutualfund holder + the sum of "
                         "the top-5 in each list. Useful for peer-comparison "
                         "tables; ~10× smaller output than default.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = default mode emits one row per HOLDER (with a "
                         "`holder_class` discriminator: summary / institutional "
                         "/ mutualfund), with the symbol col repeating; "
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
    # CSV: row-per-holder, with a `holder_class` discriminator so all three
    # sections (rollup + institutional + mutualfund) live in one table.
    # Empty / errored tickers emit a single row carrying symbol + note + meta.
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        carry = {k: r.get(k, "") for k in ("note", *RESULT_META) if k in r}
        # Errored or all-empty: one carry row, no holder data.
        if "error" in r or "note" in r:
            row = {"symbol": symbol, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        # Summary row (rollup pcts + count) — emitted only when at least
        # one rollup field is populated. holder_class="summary" so a
        # consumer filtering by class can either include or skip it.
        summary = r.get("summary") or {}
        if any(v is not None for v in summary.values()):
            row = {"symbol": symbol, "holder_class": "summary",
                   **summary, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        for klass, key in (("institutional", "institutional"),
                           ("mutualfund", "mutualfund")):
            for h in r.get(key, []):
                row = {"symbol": symbol, "holder_class": klass, **h, **carry}
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
