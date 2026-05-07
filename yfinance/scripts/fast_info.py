#!/usr/bin/env python3
"""Fetch yfinance fast_info for one or more tickers and print results as JSON.

See `fast_info.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker. Failed tickers carry an "error" field instead of price
fields so a single bad symbol does not poison the whole batch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import RESULT_META, denan, emit_json_or_ndjson, with_retry

import yfinance as yf

FIELDS = [
    "last_price",
    "previous_close",
    "open",
    "day_high",
    "day_low",
    "last_volume",
    "currency",
    "market_cap",
    "exchange",
    "timezone",
    "shares",
    "fifty_day_average",
    "two_hundred_day_average",
    "year_high",
    "year_low",
]

# Fields computed in fetch() from raw FIELDS. Per-result metadata
# (error / error_kind / attempts) lives in helpers.RESULT_META and is
# imported above for cross-script consistency.
_COMPUTED_FIELDS = ("change_abs", "change_pct")


def _materialize_fields(symbol: str) -> dict:
    """Materialize all fast_info fields. Empirically the `.fast_info`
    accessor is lazy (~5 ms, no network); the actual Yahoo call happens
    on the first field read (~3 s). So retry must wrap field iteration,
    not just the accessor — a 429 during materialization needs to
    propagate up to with_retry for classification & backoff.

    Per-field KeyError/AttributeError/TypeError stays caught locally
    (those mean "this ticker just doesn't have this field"), so they
    don't trigger retry. Anything else (rate_limit, network, etc.)
    propagates out and gets classified by with_retry.
    """
    info = yf.Ticker(symbol).fast_info
    out = {}
    for f in FIELDS:
        try:
            out[f] = denan(info[f])
        except (KeyError, AttributeError, TypeError):
            out[f] = None
    return out


def fetch(symbol: str) -> dict:
    fields, err_kind, attempts = with_retry(lambda: _materialize_fields(symbol))
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    out = {"symbol": symbol, **fields}
    if out.get("last_price") is None:
        out["error"] = "no quote returned (delisted, wrong suffix, or rate-limited)"
        out["error_kind"] = "not_found"
        # Error path always carries attempts (mirrors info / history).
        # Always 1 here since with_retry succeeded, but keep explicit
        # for cross-mode shape consistency.
        out["attempts"] = attempts
    else:
        # Always emit the keys for shape stability; null when prev is
        # missing or zero (the latter would also divide-by-zero below).
        prev = out.get("previous_close")
        if prev:
            out["change_abs"] = out["last_price"] - prev
            out["change_pct"] = (out["last_price"] - prev) / prev * 100
        else:
            out["change_abs"] = None
            out["change_pct"] = None
        # Success path: surface attempts only when retried, so the
        # happy path stays uncluttered.
        if attempts > 1:
            out["attempts"] = attempts
    return out


def main() -> None:
    # Build the field list at runtime so adding/renaming a FIELDS entry
    # auto-updates --help, no manual sync.
    fields_str = ", ".join(FIELDS)
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch current Yahoo Finance quote snapshots via yfinance.fast_info.\n\n"
            f"Returns per ticker ({len(FIELDS)} raw fields): {fields_str}.\n"
            "Plus computed change_abs (in source currency) and change_pct\n"
            "(today vs prev close, in percent units, not fraction)."
        ),
        epilog=(
            "Examples:\n"
            "  fast_info.py AAPL\n"
            "  fast_info.py AAPL MSFT TSLA\n"
            "  fast_info.py 0700.HK 600519.SS\n"
            "  fast_info.py --format csv AAPL MSFT          # CSV: one row per ticker\n"
            "  fast_info.py --format ndjson AAPL MSFT       # one JSON object per line\n"
            "\n"
            "Quotes are ~15 min delayed for US equities (more for many non-US\n"
            "markets). Use history.py for historical bars, info.py for\n"
            "fundamentals. See references/fast_info.md for the field schema and\n"
            "SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    csv_cols_summary = (
        "symbol + " + ", ".join(FIELDS) + ", "
        + ", ".join(_COMPUTED_FIELDS) + ", " + ", ".join(RESULT_META)
    )
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one JSON record per line; "
                         f"csv = one row per ticker (cols: {csv_cols_summary}).")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()
    results = [fetch(s.strip().upper()) for s in args.symbols if s.strip()]
    _emit(results, args.format)


def _emit(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # csv: one row per ticker. Cols derived from FIELDS / _COMPUTED_FIELDS
    # / RESULT_META so adding a field auto-updates header + help text.
    import csv as _csv
    cols = ["symbol", *FIELDS, *_COMPUTED_FIELDS, *RESULT_META]
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
