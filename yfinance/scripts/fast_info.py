#!/usr/bin/env python3
"""Fetch yfinance fast_info for one or more tickers and print results as JSON.

See `fast_info.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker. Failed tickers carry an "error" field instead of price
fields so a single bad symbol does not poison the whole batch.

Optional `--with-isin` flag adds a per-ticker ISIN lookup via yfinance's
experimental `Ticker.isin` API. Slow (~1-5 s extra per ticker — forces
yfinance's heavy quote.info fetch plus an external businessinsider.com
call) and spotty hit rate even on liquid US names. See
references/fast_info.md "ISIN lookup" caveat for details.
"""
from __future__ import annotations
import yfinance as yf
from helpers import RESULT_META, denan, emit_json_or_ndjson, with_retry

import argparse
import re
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


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

# Canonical ISIN shape per ISO 6166: 2 alphabetic country code + 9
# alphanumeric national identifier + 1 numeric check digit. Used as a
# defensive guard around yfinance's experimental ISIN lookup — anything
# that doesn't match this shape (including the `'-'` sentinel and any
# future weirdness yfinance might emit) gets nullified rather than
# leaking through as a fake identifier. Exposed at module scope so
# smoke tests can reuse the same regex the script enforces.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


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


def _fetch_isin(symbol: str) -> dict:
    """Fetch the ISIN for `symbol` via yfinance's experimental lookup.

    Marked `*** experimental ***` upstream — the implementation forces a
    full `quote.info` fetch (~2 HTTP to Yahoo: `quoteSummary` + `quote`
    endpoints) to pull `shortName`, then queries
    `markets.businessinsider.com` (1 more external call) to map name →
    ISIN. ~3 external HTTP per ticker on top of fast_info's lightweight
    call. Four effects worth knowing about:

      1. **Cost.** ~1-5 s extra per ticker on top of fast_info's ~0.3-0.5 s
         (5-10× the base cost). For multi-ticker batch fetches this
         dominates.
      2. **Hit rate.** Empirically spotty even on liquid names — in a
         2026-05 spot check 3 of 6 resolved (AAPL ✓, SPY ✓, 0700.HK ✓;
         MSFT ✗, BMW.DE ✗, TM ✗) despite all six having well-known real
         ISINs. Yfinance short-circuits to `'-'` instantly for tickers
         containing `-` or `^` (crypto / indexes), so those are free but
         never resolve.
      3. **Format validation.** Anything that doesn't match the canonical
         ISIN shape `[A-Z]{2}[A-Z0-9]{9}\\d` is nullified — including
         yfinance's `'-'` sentinel AND any future weirdness if upstream
         changes its "no match" encoding. Better to under-resolve than
         leak garbage as a fake identifier.
      4. **Retry budget is 2 attempts** (vs 3 for the main fast_info
         call). The ISIN call is already slow (1-5 s); a full retry-
         with-backoff stack on flaky rate-limit responses could push
         worst-case wall time to 10-16 s per ticker. A single retry on
         transient errors is the compromise.

    Returns `{"isin": <str or None>, ...}` with optional
    `isin_error_kind` (on actual fetch failure) and `isin_attempts`
    (mirrors the main-path convention: always present on error; only
    present on success when > 1).
    """
    raw, err_kind, isin_attempts = with_retry(
        lambda: yf.Ticker(symbol).isin, attempts=2
    )
    if err_kind:
        # Error path: always carry attempts (mirrors the main-path `attempts`
        # convention — always on error, > 1 only on success).
        return {
            "isin": None,
            "isin_error_kind": err_kind,
            "isin_attempts": isin_attempts,
        }
    # Validate against the canonical ISIN shape. This nullifies three kinds
    # of input: (a) yfinance's `'-'` sentinel, (b) None / empty / non-string
    # returns, (c) any future non-ISIN string the upstream might start
    # returning.
    if not isinstance(raw, str) or not _ISIN_RE.match(raw):
        out = {"isin": None}
    else:
        out = {"isin": raw}
    # Success path: surface attempts only when retried, keeping the
    # happy path uncluttered (mirrors main fast_info `attempts` handling).
    if isin_attempts > 1:
        out["isin_attempts"] = isin_attempts
    return out


def fetch(symbol: str, with_isin: bool = False) -> dict:
    fields, err_kind, attempts = with_retry(
        lambda: _materialize_fields(symbol))
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
        # ISIN lookup is opt-in and ONLY attempted on the happy path —
        # if the main fetch errored (rate_limit / not_found / network),
        # the ISIN call would almost certainly hit the same failure mode,
        # so skip it to avoid doubling the latency on already-failing rows.
        if with_isin:
            out.update(_fetch_isin(symbol))
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
            "  fast_info.py --with-isin AAPL SPY 0700.HK    # also fetch ISIN (slow, ~1-5s/ticker; spotty)\n"
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
                         f"csv = one row per ticker (cols: {csv_cols_summary}). "
                         "When --with-isin is set, three columns are appended: "
                         "`isin`, `isin_error_kind` (set on network/rate-limit "
                         "failure), `isin_attempts` (retry count, set on failure "
                         "or when > 1).")
    ap.add_argument("--with-isin", action="store_true",
                    help="Also fetch the ISIN identifier per ticker. EXPENSIVE: "
                         "adds ~1-5 s/ticker (forces yfinance's heavy quote.info "
                         "fetch + an external businessinsider.com lookup). Hit "
                         "rate is spotty even on liquid US names (yfinance marks "
                         "this *** experimental ***); a null value means 'no "
                         "match or format check failed', not 'no ISIN exists'. "
                         "Crypto / index tickers (`-` or `^` in symbol) "
                         "short-circuit to null instantly.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()
    results = [fetch(s.strip().upper(), with_isin=args.with_isin)
               for s in args.symbols if s.strip()]
    _emit(results, args.format, with_isin=args.with_isin)


def _emit(results: list, fmt: str, with_isin: bool = False) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # csv: one row per ticker. Cols derived from FIELDS / _COMPUTED_FIELDS
    # / RESULT_META so adding a field auto-updates header + help text.
    # ISIN cols are appended only when --with-isin is set so the default
    # CSV layout is unchanged for existing consumers.
    import csv as _csv
    cols = ["symbol", *FIELDS, *_COMPUTED_FIELDS, *RESULT_META]
    if with_isin:
        cols += ["isin", "isin_error_kind", "isin_attempts"]
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
