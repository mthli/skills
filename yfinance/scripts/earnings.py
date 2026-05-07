#!/usr/bin/env python3
"""Fetch yfinance earnings dates for one or more tickers and print as JSON.

See `earnings.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed tickers carry an "error" field instead of data so
a single bad symbol does not poison the batch. Field schema lives in the
*_KEYS / *_CSV_COLS constants below.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, safe_str, with_retry,
)

import yfinance as yf


# Earnings only fire for actual operating companies — ETFs / indexes /
# crypto / futures / FX don't have quarterly EPS. Pre-check quote_type
# (cheap fast_info call, ~0.3s) and short-circuit non-equities so we skip
# the more expensive HTML scrape (~1–2s) entirely.
_EQUITY_QUOTE_TYPES = frozenset({"EQUITY"})

# Hard cap mirrors upstream (yfinance/base.py:617).
LIMIT_MIN = 1
LIMIT_MAX = 100
LIMIT_DEFAULT = 12  # yfinance default — typically ~4 future + ~8 past quarters

# Output schema. The *_KEYS constants below are CSV column names — fetch()
# and _summarize() build their dicts with hardcoded keys that match. Adding
# a field requires updating both the dict construction and the relevant
# *_KEYS / *_CSV_COLS constants.
#
# `note` is included as a column in both default and summary CSV layouts so
# CSV consumers can detect non-equity rows; in JSON it's only present for
# non-equity (we don't want every equity to carry `note: null`).
# `_BASE_KEYS` is the per-ticker CSV column prefix for default mode — NOT a
# guarantee of which keys are present in every fetch() dict. `note` is
# absent from equity-success dicts; CSV emit pulls it via `r.get("note", "")`
# which returns "" for the missing case. JSON path doesn't reference this
# constant at all, so equity JSON output stays note-free.
_BASE_KEYS = ("symbol", "quote_type", "timezone", "note")
_PER_ROW_KEYS = ("date", "is_future", "eps_estimate", "eps_actual",
                 "surprise_pct")
# Summary-mode dict construction uses these as the leading keys. `timezone`
# is omitted because each `next_date` / `last_date` ISO string already
# carries its own offset — a top-level field would be redundant. CSV summary
# adds `note` as a separate column (see _SUMMARY_CSV_COLS) for the same
# non-equity-detection reason as default mode.
_SUMMARY_BASE_KEYS = ("symbol", "quote_type")
_SUMMARY_KEYS = ("next_date", "next_eps_estimate",
                 "last_date", "last_eps_estimate", "last_eps_actual",
                 "last_surprise_pct",
                 "avg_surprise_last_4", "beat_rate_last_4")
# Final CSV column orders. Tuple-concat for immutability + cheap reuse.
_DEFAULT_CSV_COLS = _BASE_KEYS + _PER_ROW_KEYS + RESULT_META
_SUMMARY_CSV_COLS = _SUMMARY_BASE_KEYS + ("note",) + _SUMMARY_KEYS + RESULT_META


def _quote_type(symbol: str) -> tuple[str | None, str | None, int]:
    """Cheap quote_type lookup via fast_info (~0.3s vs ~1–3s for .info).

    Returns (quote_type_or_None, error_kind_or_None, attempts).
    `quote_type` is empirically present on yfinance's FastInfo across
    EQUITY / ETF / MUTUALFUND / INDEX / CRYPTOCURRENCY / FUTURE / CURRENCY.

    Access path notes (yfinance 1.3.x, verified empirically):
      - `fast_info["quoteType"]`          → string for valid; AttributeError
        ('PriceHistory' object has no attribute '_dividends') for bogus —
        a yfinance internal bug where the 404 is logged but the raised
        exception is unrelated to the underlying not_found.
      - `fast_info.get("quoteType")`      → same: AttributeError leaks through
        because FastInfo.get only catches KeyError.
      - `fast_info.get("quote_type")`     → None for everything (snake_case
        isn't a valid FastInfo key — silently misses).

    So we use `["quoteType"]` and translate the bogus-ticker AttributeError
    into a not_found classification ourselves, before with_retry sees it.
    """
    def _f():
        try:
            return yf.Ticker(symbol).fast_info["quoteType"]
        except AttributeError as exc:
            # yfinance internal bug for delisted/bogus tickers — reraise as
            # something classify_error recognizes so with_retry classifies
            # it as not_found (and won't retry).
            raise RuntimeError(f"not found: {exc}") from exc
    qt, kind, attempts = with_retry(_f)
    # Explicit parens — Python's conditional expression has very low
    # precedence and binds tighter than tuple-comma here, but the bare form
    # `safe_str(qt) if not kind else None, kind, attempts` looks ambiguous
    # and invites well-intentioned "fixes" that break it.
    return (safe_str(qt) if not kind else None), kind, attempts


def _row_to_dict(ts, row, now_utc) -> dict:
    """Convert one DataFrame row to our output schema.

    `is_future` is purely date-based: `ts > now_utc`. Yahoo signals
    "not yet reported" by leaving Reported EPS missing/NaN, but that's a
    *consequence* of being future, not the definition — past events with
    a missing actual EPS (rare: stopped/delisted-mid-quarter reporters in
    Yahoo's calendar) should stay flagged as past, not flipped to future.
    """
    is_future = ts.tz_convert("UTC").to_pydatetime() > now_utc
    return {
        "date": ts.isoformat(),  # tz-aware Timestamp → 'YYYY-MM-DDTHH:MM:SS±HH:MM'
        "is_future": bool(is_future),
        "eps_estimate": safe_float(row.get("EPS Estimate")),
        "eps_actual": safe_float(row.get("Reported EPS")),
        "surprise_pct": safe_float(row.get("Surprise(%)")),
    }


def _summarize(full: dict) -> dict:
    """Project the full earnings list into a flat headline dict per ticker.

    `next_*` = nearest future event; `last_*` = nearest past event;
    `avg_surprise_last_4` and `beat_rate_last_4` computed from the most
    recent 4 reported quarters (null when fewer exist).
    """
    if "error" in full:
        # Pass through error / error_kind / attempts; drop the per-row data
        # that would never have been populated anyway.
        out = {"symbol": full["symbol"]}
        for key in RESULT_META:
            if key in full:
                out[key] = full[key]
        return out

    rows = full.get("earnings_dates") or []
    base = {k: full.get(k) for k in _SUMMARY_BASE_KEYS}
    # Non-equity short-circuit pass-through: preserve note + null all fields.
    if "note" in full:
        base["note"] = full["note"]
        for k in _SUMMARY_KEYS:
            base[k] = None
        # Propagate retry meta — quote_type pre-check may have retried for
        # non-equity tickers too.
        for key in RESULT_META:
            if key in full:
                base[key] = full[key]
        return base

    # Rows arrive in "near-now first" order from fetch() — future events
    # ASC then past events DESC. Both modes (default output + this summary)
    # see the same order. Just partition here.
    future_rows = [r for r in rows if r.get("is_future")]
    past_rows = [r for r in rows if not r.get("is_future")]

    # After fetch's sort: future_rows = [nearest ... most-distant],
    # past_rows = [most-recent ... oldest]. So [0] of each is the
    # event closest to "now" in its direction.
    next_row = future_rows[0] if future_rows else None
    last_row = past_rows[0] if past_rows else None

    # avg_surprise / beat_rate over last 4 *reported* quarters.
    last_4_surprises = [r["surprise_pct"] for r in past_rows[:4]
                        if r.get("surprise_pct") is not None]
    if len(last_4_surprises) >= 4:
        avg_surprise = sum(last_4_surprises) / 4.0
        beat_rate = sum(1 for s in last_4_surprises if s > 0) / 4.0
    else:
        avg_surprise = None
        beat_rate = None

    base.update({
        "next_date": next_row["date"] if next_row else None,
        "next_eps_estimate": next_row["eps_estimate"] if next_row else None,
        "last_date": last_row["date"] if last_row else None,
        "last_eps_estimate": last_row["eps_estimate"] if last_row else None,
        "last_eps_actual": last_row["eps_actual"] if last_row else None,
        "last_surprise_pct": last_row["surprise_pct"] if last_row else None,
        "avg_surprise_last_4": avg_surprise,
        "beat_rate_last_4": beat_rate,
    })
    for key in RESULT_META:
        if key in full:
            base[key] = full[key]
    return base


def _validate_summary_keys() -> None:
    """Module-load sanity check: `_summarize` must populate every key in
    `_SUMMARY_KEYS` for both the equity-success and non-equity paths.
    Catches the case where someone adds a key to `_SUMMARY_KEYS` but
    forgets to set it in `_summarize`'s `base.update({...})` — a silent
    schema drift that the smoke integration tests don't always catch
    because per-key existence isn't asserted on every code path.
    Mirrors the pattern in `info.py` (`_validate_summary_fields`).
    """
    cases = (
        ({"symbol": "X", "quote_type": "EQUITY", "earnings_dates": []},
         "equity-empty"),
        ({"symbol": "X", "quote_type": "ETF", "note": "n/a",
          "earnings_dates": []}, "non-equity"),
    )
    for sample, label in cases:
        out = _summarize(sample)
        missing = set(_SUMMARY_KEYS) - set(out.keys())
        if missing:
            raise RuntimeError(
                f"_summarize ({label}) missed keys from _SUMMARY_KEYS: "
                f"{sorted(missing)}")


_validate_summary_keys()


def fetch(symbol: str, limit: int = LIMIT_DEFAULT,
          past_only: bool = False, future_only: bool = False,
          *, slice_to_limit: bool = True) -> dict:
    """Fetch earnings dates for one ticker.

    Flow:
      1. Cheap quote_type pre-check via fast_info (~0.3s). Bogus/delisted
         tickers exit here with `error_kind=not_found`. Non-equity tickers
         (ETF / INDEX / etc.) short-circuit with empty list + `note`,
         skipping the more expensive HTML scrape.
      2. HTML scrape via `Ticker.get_earnings_dates(limit=N)` (~1–2s).
      3. Convert each row to dict, apply "near-now first" sort (future ASC
         + past DESC, future block on top), apply `past_only` / `future_only`
         filter, optionally slice to `limit`.

    `slice_to_limit=False` is set by `main()` in `--summary` mode so the
    full bucket-sized fetch reaches `_summarize` for aggregate computation
    (avg_surprise_last_4 etc.). See `--limit` help text for per-mode semantic.

    Returns one of:
      - equity success:   {symbol, quote_type, timezone, earnings_dates: [...], attempts?}
      - non-equity short: {symbol, quote_type, note, earnings_dates: [], attempts?}
      - error:            {symbol, error, error_kind, attempts}
    """
    # Step 1: Cheap quote_type pre-check. Skips the much more expensive HTML
    # scrape for non-equities (ETFs, indexes, crypto, etc.) where the call
    # would return None anyway.
    qt, qt_err, qt_attempts = _quote_type(symbol)
    if qt_err:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({qt_err}, after {qt_attempts} attempt(s))",
            "error_kind": qt_err,
            "attempts": qt_attempts,
        }
    if qt is None:
        # quote_type lookup succeeded but returned None — bogus / delisted.
        return {
            "symbol": symbol,
            "error": "no quote_type returned (delisted, wrong suffix, or rate-limited)",
            "error_kind": "not_found",
            "attempts": qt_attempts,
        }
    if qt not in _EQUITY_QUOTE_TYPES:
        out = {
            "symbol": symbol,
            "quote_type": qt,
            "note": f"earnings only meaningful for equities; this is {qt}",
            "earnings_dates": [],
        }
        if qt_attempts > 1:
            out["attempts"] = qt_attempts
        return out

    # Step 2: HTML scrape via Ticker.get_earnings_dates().
    def _fetch():
        return yf.Ticker(symbol).get_earnings_dates(limit=limit)

    df, err_kind, attempts = with_retry(_fetch)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }

    if df is None or df.empty:
        # Equity that returned no earnings table — likely a small / non-US
        # name with no Yahoo coverage, or a recent IPO. Treat as not_found
        # rather than empty success: the caller should know we got nothing.
        return {
            "symbol": symbol,
            "error": "no earnings dates returned (low coverage, recent IPO, or rate-limited)",
            "error_kind": "not_found",
            "attempts": attempts,
        }

    # df.index is tz-aware (per yfinance/base.py — it explicitly tz_localizes
    # each row to the per-row timezone). Single timezone string is the tz of
    # the first row; in practice all rows for a single ticker share one tz.
    first_tz = df.index[0].tzinfo
    tz_name = str(first_tz) if first_tz is not None else None

    now_utc = datetime.now(tz=timezone.utc)
    rows = [_row_to_dict(ts, row, now_utc) for ts, row in df.iterrows()]

    # "Near-now first" sort: future events ASC (nearest upcoming at the
    # top of the future block), then past events DESC (most recent past
    # below the future block, oldest at the very bottom). Both halves
    # converge on the now-boundary in the middle. Rationale: small
    # `--limit` values then keep the most useful rows — `--limit 3`
    # for a typical equity gives the next earnings event plus the
    # 2 most recent reported quarters, instead of 3 distant future
    # estimates (which would happen under a flat DESC sort).
    #
    # Parse to tz-aware datetime: ISO lex compare is only safe within a
    # single tz offset, and DST-spanning windows mix `-04:00`/`-05:00`
    # for ET-listed tickers. Sort cost is trivial (≤100 rows).
    _key = lambda r: datetime.fromisoformat(r["date"])
    future_rows = sorted([r for r in rows if r["is_future"]], key=_key)
    past_rows = sorted([r for r in rows if not r["is_future"]],
                       key=_key, reverse=True)
    rows = future_rows + past_rows

    # Post-fetch projection (filters are mutually exclusive at the CLI layer).
    # Filter first, then truncate — `--past-only --limit 8` should give 8
    # past events, not "filter 8 events down to whatever's past".
    if past_only:
        rows = [r for r in rows if not r["is_future"]]
    elif future_only:
        rows = [r for r in rows if r["is_future"]]

    # Honor --limit strictly. yfinance's `limit` param actually maps to a
    # page-size bucket (25/50/100) and does NOT truncate the returned df —
    # so without this slice, `--limit 5` could return ~25 rows. Slice here
    # so the CLI semantic ("max rows in output") matches user expectations.
    #
    # `slice_to_limit=False` is set by main() in --summary mode: there we
    # want as many rows as yfinance returned for computing aggregates
    # (avg_surprise_last_4 etc.), not the user-facing cap. The summary
    # output is one row per ticker either way.
    if slice_to_limit:
        rows = rows[:limit]

    out = {
        "symbol": symbol,
        "quote_type": qt,
        "timezone": tz_name,
        "earnings_dates": rows,
    }
    # Surface attempts only when actually retried. Use `max` (not sum) across
    # the two underlying calls (quote_type pre-check + scrape) so the field
    # carries the same "max retries seen in any single yfinance call"
    # semantic as the other three modes — an `attempts: 3` here means the
    # same as in fast_info / history / info.
    max_attempts = max(qt_attempts, attempts)
    if max_attempts > 1:
        out["attempts"] = max_attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch upcoming + recent earnings dates from Yahoo Finance.\n\n"
            "Two output modes:\n"
            "  default     full earnings list per ticker (next + recent quarters)\n"
            "  --summary   flat per-ticker dict with next/last + 4-qtr avg surprise\n\n"
            "Equity-only: ETFs, indexes, crypto, FX, futures get an empty list\n"
            "with a `note` (not an error) — the quote_type pre-check skips the\n"
            "scrape for them."
        ),
        epilog=(
            "Examples:\n"
            "  earnings.py AAPL                              # default 12 rows (~1 future + ~11 past for typical equity)\n"
            "  earnings.py --summary AAPL MSFT NVDA          # peer-comparable dict\n"
            "  earnings.py --future-only AAPL                # only upcoming events\n"
            "  earnings.py --past-only --limit 20 AAPL       # 20 reported quarters\n"
            "  earnings.py --format csv AAPL MSFT            # one row per (symbol, date)\n"
            "\n"
            "See references/earnings.md for the full schema, unit notes\n"
            "(Surprise(%) is percent, not fraction), and presentation guidance."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=LIMIT_DEFAULT, metavar="N",
                    help=f"Default mode: max rows in output. --summary mode: "
                         f"fetch hint — yfinance buckets to page sizes "
                         f"25/50/100, so --limit 1–25 are equivalent (all "
                         f"fetch ~25 rows). Range [{LIMIT_MIN},{LIMIT_MAX}], "
                         f"default %(default)s. Includes future + past.")
    ap.add_argument("--summary", action="store_true",
                    help="Project earnings list to flat per-ticker dict for peer comparison.")
    direction = ap.add_mutually_exclusive_group()
    direction.add_argument("--past-only", action="store_true",
                           help="Keep only reported (past) earnings rows.")
    direction.add_argument("--future-only", action="store_true",
                           help="Keep only upcoming (future) earnings rows.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one JSON record per line; "
                         "csv = flattened — default mode emits one row per "
                         "(symbol, earnings_date); --summary mode emits one row per ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive. Earnings only "
                         "meaningful for equities (ETFs/indexes/crypto get empty + note).")
    args = ap.parse_args()

    if not (LIMIT_MIN <= args.limit <= LIMIT_MAX):
        ap.error(
            f"--limit must be in [{LIMIT_MIN}, {LIMIT_MAX}] "
            f"(yfinance hard cap), got {args.limit}")

    if args.summary and (args.past_only or args.future_only):
        # Summary projects from the full list; filters would conflict with the
        # next_/last_ semantics. Reject loudly rather than silently misbehave.
        ap.error("--summary is incompatible with --past-only / --future-only "
                 "(summary uses both directions to compute next_ and last_)")

    results = [
        fetch(s.strip().upper(), limit=args.limit,
              past_only=args.past_only, future_only=args.future_only,
              slice_to_limit=not args.summary)
        for s in args.symbols if s.strip()
    ]
    if args.summary:
        results = [_summarize(r) for r in results]
    _emit(results, args.format, summary=args.summary)


def _emit(results: list, fmt: str, *, summary: bool) -> None:
    """Render results to stdout in the requested format.

    json   pretty-printed JSON array (default)
    ndjson one JSON object per line (streaming-friendly)
    csv    flattened tabular:
             summary mode → cols=_SUMMARY_CSV_COLS, one row per ticker
             default mode → cols=_DEFAULT_CSV_COLS, one row per earnings
                            event with per-ticker meta (symbol/quote_type/
                            timezone/note) repeated on each row so the CSV
                            is self-contained.
    """
    if emit_json_or_ndjson(results, fmt):
        return
    # csv
    import csv as _csv
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    if summary:
        cols = list(_SUMMARY_CSV_COLS)
        writer.writerow(cols)
        for r in results:
            writer.writerow([r.get(c, "") for c in cols])
        return
    cols = list(_DEFAULT_CSV_COLS)
    writer.writerow(cols)
    for r in results:
        meta = [r.get(c, "") for c in _BASE_KEYS]
        meta_cells = [r.get(k, "") for k in RESULT_META]
        if "error" in r:
            writer.writerow(
                meta + [""] * len(_PER_ROW_KEYS) + meta_cells
            )
            continue
        events = r.get("earnings_dates", [])
        if not events:
            # Non-equity short-circuit OR equity with no events: emit a single
            # row with empty data cols so the ticker is still represented.
            writer.writerow(meta + [""] * len(_PER_ROW_KEYS) + meta_cells)
            continue
        for ev in events:
            writer.writerow(
                meta
                + [ev.get(c, "") for c in _PER_ROW_KEYS]
                + meta_cells
            )


if __name__ == "__main__":
    main()
