#!/usr/bin/env python3
"""Fetch yfinance Ticker.get_valuation_measures() for one or more tickers
and print as JSON / NDJSON / CSV.

See `valuation.py --help` for usage. Output is a JSON array on stdout,
one entry per ticker; failed / non-applicable tickers carry an "error" or
a "note" field instead of data so a single bad symbol does not poison the
batch. Field schema lives in the *_KEYS / *_CSV_COLS constants below.

Unique value vs info.py's `valuation` section: this returns a HISTORICAL
time series of valuation metrics (current + last 5 quarter-end snapshots)
rather than a single current snapshot. Lets callers answer "is X's P/E
near its 1y high or low" / "how has EV/EBITDA shifted over the last year"
type questions in one HTTP — uniquely structured numerical data that
web search can't reproduce.

IMPORTANT: yfinance implements this as an HTML scrape of the Yahoo
key-statistics page (NOT a structured quoteSummary API call). Values
arrive as pre-formatted display strings ('4.31T', '35.51', '--') with
3-significant-figure rounding baked in by Yahoo's renderer. This is the
most fragile yfinance mode — Yahoo can rearrange the table without
warning. Smoke tests check the row labels and column count; if they
fail, the scrape probably broke upstream.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, with_retry,
)

import yfinance as yf


# All non-equity quote types return an empty DataFrame at the
# key-statistics scrape — verified 2026-05 for SPY (ETF), VFIAX (mutual
# fund), ^GSPC (index), BTC-USD (crypto), EURUSD=X (FX), ES=F (futures),
# and BOGUS123 (bogus). Equities US + ADRs + non-US primary listings
# (0700.HK, BMW.DE) all return the full 9-row × 6-col frame.
#
# Same ambiguity shape as holders / sec_filings: empty result alone
# can't distinguish non-equity from bogus from low-coverage equity.
# Note text also flags the HTML-scrape-breakage edge case (yfinance
# implements this via BeautifulSoup; if Yahoo restyles the page the
# scrape returns empty and we can't tell that from a legitimate
# non-equity at the response level — the only signal is "blue-chip
# equity returning empty"). Callers can chain fast_info themselves.
_EMPTY_NOTE = (
    "no valuation data (Yahoo's key-statistics scrape returned empty — "
    "expected for ETFs / mutual funds / indexes / crypto / FX / futures "
    "and bogus tickers; if seen on a blue-chip equity, Yahoo may have "
    "restyled the page and broken the HTML scrape — call fast_info to "
    "disambiguate)"
)

# Yahoo's row labels → our snake_case field names. Insertion order
# below IS the per-period emit order (CSV column order), so callers
# should treat this dict as canonical and derive emit order from it.
# Anchors are deliberately short prefixes (e.g. "PEG Ratio" not
# "PEG Ratio (5yr expected)") so prefix matching at runtime survives
# Yahoo adding / dropping / restyling parenthetical qualifiers — the
# silent-null failure mode would otherwise hide upstream drift behind
# correct-looking output. Verified 2026-05 against AAPL / 0700.HK /
# BMW.DE / TM that the actual labels Yahoo emits start with these
# prefixes; matching is case-sensitive (Yahoo's HTML is stable on
# casing in observed payloads).
#
# Field names match info.py's valuation section so the two modes'
# outputs interoperate cleanly:
#   info[t].valuation.trailing_pe  ←→  valuation[t].periods[0].trailing_pe
_ROW_LABEL_TO_KEY = {
    "Market Cap":               "market_cap",
    "Enterprise Value":         "enterprise_value",
    "Trailing P/E":             "trailing_pe",
    "Forward P/E":              "forward_pe",
    "PEG Ratio":                "peg_ratio",
    "Price/Sales":              "price_to_sales",
    "Price/Book":               "price_to_book",
    "Enterprise Value/Revenue": "ev_to_revenue",
    "Enterprise Value/EBITDA":  "ev_to_ebitda",
}

# Anchors for prefix-match resolution, sorted by anchor length
# descending so more-specific labels match before their parents
# (`Enterprise Value/Revenue` must be tried before `Enterprise Value`
# or the latter would absorb the former). Derived from the canonical
# dict above so a future label addition flows through automatically.
_ROW_ANCHORS = sorted(_ROW_LABEL_TO_KEY.items(), key=lambda kv: -len(kv[0]))

# Size-metric keys get integer coercion via round() (precision-loss
# artifact correction — see _to_size_int docstring). All other keys
# stay float. Frozenset for O(1) membership check; centralized so
# adding a future size metric (or moving one to ratio bucket) is one
# edit.
_SIZE_KEYS = frozenset({"market_cap", "enterprise_value"})

# Per-period record keys, in emit order. Derived from the canonical
# dict above; updating _ROW_LABEL_TO_KEY automatically reshapes
# _PERIOD_KEYS and the CSV columns.
_PERIOD_KEYS = ("period_label", "period_date", *_ROW_LABEL_TO_KEY.values())

# Magnitude suffix → multiplier. Yahoo's renderer emits these for the
# two size metrics (market_cap / enterprise_value). Verified empirically
# (2026-05): T / B / M observed in the wild; K not seen but included
# defensively (a thousand-USD market cap is implausible for a listed
# equity but Yahoo's template could in principle emit it).
_MAGNITUDE_SUFFIX = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}

# Which ratios get current / min / max in --summary mode. Picked for
# practical "is X currently expensive vs the recent past" answers:
# trailing/forward P/E (the canonical valuation ratios), price_to_book
# (asset-heavy industries), and EV/EBITDA (capital-structure-neutral).
# Skipped: peg_ratio (empirically often '--' so noisy), price_to_sales
# / ev_to_revenue (less commonly asked-about). Add a flag if a use case
# wants the full nine-metric trend in summary form.
_SUMMARY_TREND_METRICS = (
    "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda",
)

# Summary-mode flat projection: current market cap + per-metric
# current/min/max across the returned period window. Window size is
# echoed via `periods_returned` so the user knows what "min/max" was
# computed over (typically 6 = current + 5 quarter-end snapshots).
_SUMMARY_FLAT_KEYS = (
    "periods_returned",
    "oldest_period_date",
    "current_market_cap",
    *(f"{stat}_{m}"
      for m in _SUMMARY_TREND_METRICS
      for stat in ("current", "min", "max")),
)

# Default-mode CSV: one row per period, with `symbol` repeating. No
# `record_class` discriminator (only one record shape here). Empty /
# errored tickers emit a single carry row with symbol + note + meta.
_DEFAULT_CSV_COLS = (
    "symbol", *_PERIOD_KEYS, "note", *RESULT_META,
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS, "note", *RESULT_META)


def _parse_yahoo_display_value(s) -> float | None:
    """Parse Yahoo's key-statistics display strings to floats.

    Handles three encodings produced by Yahoo's renderer:
      '--'             → None  (missing / N/A — loss-makers' P/E, etc.)
      '<num><suffix>'  → float, suffix ∈ {T, B, M, K} → ×1e12 / 1e9 / 1e6 / 1e3
      '<num>'          → float (plain decimal, used for all ratios)

    Whitespace-only or unrecognized strings also → None (defensive: if
    Yahoo introduces a new suffix or formatting quirk, we surface as
    missing rather than blowing up). Returning None for unrecognized
    input is the right shape because every downstream field is
    Optional[float] anyway.

    Negative values aren't generated by Yahoo for any of the 9 rows
    (loss-makers' P/E render as '--', not a negative number — verified
    PLUG / RIVN / NIO in 2026-05), so no minus-sign handling needed.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s == "--":
        return None
    last = s[-1].upper()
    if last in _MAGNITUDE_SUFFIX:
        base = safe_float(s[:-1])
        return base * _MAGNITUDE_SUFFIX[last] if base is not None else None
    return safe_float(s)


def _parse_period_label(col: str) -> tuple[str, str | None]:
    """Yahoo column header → (period_label, period_date).

    Yahoo emits one of two formats:
      'Current'      → ("current", None)
      'M/D/YYYY'     → ("YYYY-MM-DD", "YYYY-MM-DD")

    The M/D/YYYY format is locale-fragile (no leading zeros, US
    month-first order) but stable in Yahoo's HTML — verified across
    AAPL / 0700.HK / BMW.DE in 2026-05. Validated via `strptime` so
    invalid dates (`5/45/2026`) surface as the raw label + null date
    rather than parsing to a nonsense ISO string (`2026-05-45`).
    Unparseable → fall back to using the raw column string as the
    label, period_date=None, so a Yahoo format change surfaces as a
    visible weird label rather than a parse exception.
    """
    if col.strip().lower() == "current":
        return "current", None
    try:
        dt = datetime.strptime(col.strip(), "%m/%d/%Y")
    except ValueError:
        return col, None
    iso = dt.strftime("%Y-%m-%d")
    return iso, iso


def _resolve_row_key(row_label: str) -> str | None:
    """Map a Yahoo row label to our snake_case key, with prefix-match
    fallback for tolerated qualifier changes.

    Walks `_ROW_ANCHORS` (sorted longest-first) and returns the first
    key whose anchor is a prefix of `row_label`. Returns None for
    labels matching nothing in our map (the row is silently skipped
    downstream — Yahoo can add unknown rows without breaking us).

    The longest-first sort matters: `Enterprise Value/Revenue` starts
    with `Enterprise Value`, so without ordering by length the shorter
    anchor would absorb the longer label and produce the wrong key.
    Verified by smoke (`Enterprise Value/Revenue` → ev_to_revenue,
    NOT enterprise_value).
    """
    if not row_label:
        return None
    for anchor, key in _ROW_ANCHORS:
        if row_label.startswith(anchor):
            return key
    return None


def _to_size_int(v) -> int | None:
    """Round float to int with None passthrough — used for market_cap
    and enterprise_value.

    Why round-then-int rather than plain int(): Yahoo's display
    strings ('4.31T') parse via float('4.31') * 1e12 =
    4309999999999.9995, which int() would truncate to ...9999 —
    visually misleading given Yahoo only shows ~3 sig figs to begin
    with. round() gives the cleaner 4310000000000 that matches what
    Yahoo intended to display. No NaN/Inf guard needed: callers
    pre-filter via `safe_float`, which routes those to None.
    """
    return int(round(v)) if v is not None else None


def _project_periods(df) -> list[dict]:
    """Yahoo DataFrame → list of per-period dicts, one per column.

    Iterates the actual rows in `df.index` (rather than walking our
    map of expected labels), feeding each through `_resolve_row_key`
    for prefix-tolerant resolution. Unknown labels are silently
    skipped; expected labels missing from `df.index` surface as None
    in the output (and a smoke canary fires).

    `market_cap` / `enterprise_value` get rounded-to-int via
    `_to_size_int` to undo float-precision artifacts from parsing
    Yahoo's display strings. Field-name parity with info.py's
    valuation section is preserved.
    """
    if df is None or df.empty:
        return []
    out = []
    for col in df.columns:
        label, date = _parse_period_label(col)
        # Pre-populate every expected key to None so the row shape is
        # invariant across periods (Yahoo could drop a row in one
        # column but not another — the JSON / CSV shape stays stable).
        row = {"period_label": label, "period_date": date}
        for key in _ROW_LABEL_TO_KEY.values():
            row[key] = None
        for row_label in df.index:
            key = _resolve_row_key(str(row_label))
            if key is None:
                continue
            v = _parse_yahoo_display_value(df.loc[row_label, col])
            row[key] = _to_size_int(v) if key in _SIZE_KEYS else v
        out.append(row)
    return out


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for trend questions.

    For each of the 4 trend metrics, emits current / min / max across
    the returned period window. None values are excluded from min/max
    (a loss-maker with '--' P/E across the window has min/max=None
    rather than crashing). When every value is None for a metric,
    all three fields stay None.

    "Current" is located by `period_date is None` (a structural
    invariant set by `_parse_period_label`) rather than `periods[0]`
    — defensive against any Yahoo emit-order change. Falls back to
    `periods[0]` if no period has `period_date=None` (an unobserved
    case but graceful by design — the row most likely to carry
    "current" semantics is still the first one).

    `oldest_period_date` walks the dated periods (excluding the
    None-dated current row) and takes the earliest. Lets callers
    compute their own change-vs-oldest math without re-parsing
    `periods`.
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}
    periods = full.get("periods") or []
    out["periods_returned"] = len(periods)
    if not periods:
        for k in ("note", *RESULT_META):
            if k in full:
                out[k] = full[k]
        return out

    # Locate the "current" row by structural marker (period_date is
    # None) rather than position. Fallback to periods[0] preserves
    # graceful behavior on emit-order drift.
    current = next(
        (p for p in periods if p.get("period_date") is None),
        periods[0],
    )
    out["current_market_cap"] = current.get("market_cap")

    # Oldest snapshot: walk all dated periods and take the min date.
    # The current row (period_date=None) is excluded by construction.
    dated = [p["period_date"] for p in periods if p.get("period_date") is not None]
    if dated:
        out["oldest_period_date"] = min(dated)

    for m in _SUMMARY_TREND_METRICS:
        vals = [p.get(m) for p in periods if p.get(m) is not None]
        out[f"current_{m}"] = current.get(m)
        out[f"min_{m}"] = min(vals) if vals else None
        out[f"max_{m}"] = max(vals) if vals else None

    for k in ("note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def fetch(symbol: str) -> dict:
    """Fetch Yahoo's key-statistics valuation table for `symbol`."""
    def _f():
        return yf.Ticker(symbol).get_valuation_measures()

    df, err_kind, attempts = with_retry(_f)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    periods = _project_periods(df)
    out = {"symbol": symbol, "periods": periods}
    if not periods:
        # Empty frame: ambiguous (non-equity / bogus / very-low-coverage).
        # Same convention as holders / sec_filings — emit success-with-note,
        # caller chains fast_info to disambiguate.
        out["note"] = _EMPTY_NOTE
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance's HISTORICAL valuation table for one or\n"
            "more tickers (current snapshot + the last 5 quarter-end\n"
            "snapshots, 9 metrics each: market_cap, enterprise_value,\n"
            "trailing_pe, forward_pe, peg_ratio, price_to_sales,\n"
            "price_to_book, ev_to_revenue, ev_to_ebitda).\n\n"
            "Unique vs info.py: info gives only the CURRENT snapshot; this\n"
            "mode answers TREND questions — 'is AAPL's P/E near its 1y\n"
            "high/low', 'how has EV/EBITDA shifted over the last year',\n"
            "etc. Field names match info.valuation.* for clean interop.\n\n"
            "Equity-focused: ETFs / mutual funds / indexes / crypto / FX /\n"
            "futures all return empty (success-with-note, all verified\n"
            "empirically); bogus tickers also return empty (ambiguous —\n"
            "chain fast_info to disambiguate).\n\n"
            "FRAGILITY: yfinance implements this via an HTML scrape of the\n"
            "Yahoo key-statistics page (NOT a structured API). Values\n"
            "arrive as pre-rounded display strings (3 sig figs) and the\n"
            "scrape can break if Yahoo restyles the page. Smoke tests will\n"
            "catch breakage. UNITS: market_cap / enterprise_value in the\n"
            "ticker's TRADING currency (USD for AAPL, HKD for 0700.HK,\n"
            "EUR for BMW.DE — call fast_info to resolve which); ratios\n"
            "are unitless."
        ),
        epilog=(
            "Examples:\n"
            "  valuation.py AAPL                              # full history (default)\n"
            "  valuation.py --summary AAPL MSFT GOOGL         # trend rollup, peer compare\n"
            "  valuation.py --format csv AAPL                 # one row per period\n"
            "  valuation.py --format csv --summary AAPL MSFT  # one row per ticker\n"
            "\n"
            "See references/valuation.md for the field schema, presentation\n"
            "guidance, and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: current market cap + "
                         "current/min/max for the 4 most-asked ratios "
                         "(trailing_pe, forward_pe, price_to_book, ev_to_ebitda) "
                         "across the full returned window. Useful for "
                         "answering 'is X currently near its 1y high or "
                         "low P/E' and for peer-compare tables. Same "
                         "network cost as default (post-fetch projection); "
                         "use to save context tokens.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; ndjson = one JSON record per "
                         "ticker per line; csv = default mode emits one row per "
                         "period (with `symbol` repeating, `period_label` and "
                         "`period_date` as discriminators); --summary csv emits "
                         "strict one-row-per-ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    results = [fetch(s.strip().upper())
               for s in args.symbols if s.strip()]

    if args.summary:
        results = [_summarize(r) for r in results]
        _emit_summary(results, args.format)
    else:
        _emit_default(results, args.format)


def _emit_default(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # CSV: row-per-period, with `symbol` repeating. Empty / errored
    # tickers emit a single row carrying symbol + note + meta.
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        carry = {k: r[k] for k in ("note", *RESULT_META) if k in r}
        periods = r.get("periods") or []
        if not periods:
            row = {"symbol": symbol, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        for p in periods:
            row = {"symbol": symbol, **p, **carry}
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
