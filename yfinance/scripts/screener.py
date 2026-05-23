#!/usr/bin/env python3
"""Run a Yahoo Finance screener — predefined or custom query.

See `screener.py --help` for usage. Two modes:

  --predefined NAME    run one of Yahoo's saved screeners (e.g. day_gainers,
                       undervalued_growth_stocks, top_etfs_us, …). The full
                       list ships with yfinance — see `--list-predefined`.
  --query JSON         run a custom AND/OR query against Yahoo's screener
                       API. Pair with --quote-type {equity,fund,etf} (default
                       equity). The JSON tree mirrors yfinance's QueryBase:
                         {"operator": "and", "operands": [
                            {"operator": "gt",  "operands": ["percentchange", 3]},
                            {"operator": "eq",  "operands": ["region", "us"]}
                         ]}
                       To discover valid fields per quote_type, run
                       `--list-fields equity|fund|etf`.

Unlike the per-ticker wrappers (which emit a JSON array, one record per
ticker), screener emits a SINGLE envelope dict — one screener call, one
result. Output formats:

  json     pretty-printed envelope dict (metadata first, quotes last).
  ndjson   one quote per line; envelope metadata dropped. On error /
           no-match emits a single envelope-summary line so consumers
           see the failure rather than empty stdout.
  csv      one row per quote, no envelope columns. On error / no-match
           emits a single carry row with `note` / meta cols populated.
           Incompatible with --full (raw keys would break column
           stability across calls — argparse rejects the combo).
  symbols  one ticker per line, no header — pipe-friendly. On error /
           no-match emits nothing on stdout but surfaces the error /
           note to stderr (rc unchanged) so shell pipelines can detect
           failure.
"""
from __future__ import annotations
from yfinance import EquityQuery, ETFQuery, FundQuery
import yfinance as yf
from helpers import (
    RESULT_META,
    epoch_to_date,
    safe_float,
    safe_int,
    safe_str,
    with_retry,
)

import argparse
import json
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# yfinance dispatches on the Query subclass to set the screener's
# `quoteType` filter (EQUITY / MUTUALFUND / ETF). Map our user-facing
# arg to the right class.
QUERY_CLASS_BY_TYPE = {
    "equity": EquityQuery,
    "fund": FundQuery,
    "etf": ETFQuery,
}

# User-facing quote_type label for class — inverse of QUERY_CLASS_BY_TYPE.
# Used to suggest the right --quote-type when a custom-query field is
# rejected by the wrong subclass.
_TYPE_LABEL_BY_CLASS = {
    EquityQuery: "equity",
    FundQuery: "fund",
    ETFQuery: "etf",
}

# Known-valid (operator, operand) per class, so we can instantiate a
# sample query to read `valid_fields` / `valid_values` without tripping
# the constructor's validators. Field choice is class-specific:
#   - EquityQuery / ETFQuery accept `region`
#   - FundQuery rejects `region` but accepts `exchange` (with NAS as a
#     valid enum value, used by every fund predefined upstream)
_SAMPLE_QUERY_INIT = {
    EquityQuery: ("eq", ["region", "us"]),
    FundQuery:   ("eq", ["exchange", "NAS"]),
    ETFQuery:    ("eq", ["region", "us"]),
}


def _make_sample(cls):
    """Build a valid sample instance of `cls` for introspecting valid_fields
    / valid_values without running a screener call."""
    op, operand = _SAMPLE_QUERY_INIT[cls]
    return cls(op, operand)


# Hand-curated descriptions for `--list-predefined`. yfinance's
# PREDEFINED_SCREENER_QUERIES dict doesn't carry descriptions (Yahoo
# returns them only on the screen-call response, so fetching all 19 to
# enumerate would cost ~30s). Mirroring references/screener.md.
_PREDEFINED_DESCRIPTIONS = {
    # Equity (9)
    "aggressive_small_caps": "Small caps with EPS growth < 15% (NMS / NYQ).",
    "day_gainers": "US stocks up > 3% intraday, market cap >= $2B, price >= $5.",
    "day_losers": "US stocks down > 2.5% intraday, market cap >= $2B.",
    "growth_technology_stocks": "Technology sector with quarterly revenue growth >= 25% AND TTM EPS growth >= 25%.",
    "most_actives": "US stocks with daily volume > 5M, market cap >= $2B.",
    "most_shorted_stocks": "US stocks sorted by short_percentage_of_shares_outstanding (descending).",
    "small_cap_gainers": "Market cap < $2B, NMS / NYQ.",
    "undervalued_growth_stocks": "TTM PE in (0, 20), PEG (5y) < 1, TTM EPS growth >= 25%.",
    "undervalued_large_caps": "TTM PE in (0, 20), PEG (5y) < 1, market cap $10B-$100B.",
    # Mutual fund (6)
    "conservative_foreign_funds": "Foreign large/mid funds with Morningstar rating >= 4 + low risk + low minimum.",
    "high_yield_bond": "High-yield bond funds with Morningstar rating >= 4 + low risk.",
    "portfolio_anchors": "Large-blend funds with Morningstar rating >= 4 + low risk.",
    "solid_large_growth_funds": "Large-growth funds with Morningstar rating >= 4 + low risk.",
    "solid_midcap_growth_funds": "Mid-cap growth funds with Morningstar rating >= 4 + low risk.",
    "top_mutual_funds": "Mutual funds with Morningstar rating >= 4, sorted by % change.",
    # ETF (4)
    "top_etfs_us": "US ETFs with Morningstar rating >= 4, sorted by % change.",
    "top_performing_etfs": "US ETFs with Morningstar rating >= 4, sorted by lowest expense ratio.",
    "technology_etfs": "US Technology-category ETFs sorted by lowest expense ratio.",
    "bond_etfs": "US bond ETFs across various categories sorted by lowest expense ratio.",
}

# Per-quote projection. Yahoo's screener payload has ~60-85 fields per
# quote (verified empirically: ~85 for equity, ~75 for ETF, ~58 for
# mutual fund); we project to 28 of them. Equity-only fields (PE, EPS,
# earnings date) are null for ETFs / funds; ETF/fund-only fields
# (net_assets, expense_ratio, returns) are null for equities — both
# co-exist in the projected schema so a mixed batch (e.g. a custom query
# that doesn't filter on quote_type) renders cleanly.
QUOTE_FIELDS = (
    # Identity
    "symbol",
    "name",
    "quote_type",
    "exchange",
    "full_exchange_name",
    "currency",
    "region",
    # Pricing snapshot
    "price",
    "change_pct",                       # PERCENT (regularMarketChangePercent)
    "volume",
    "avg_volume_3m",
    # Equity valuation
    "market_cap",
    "trailing_pe",
    "forward_pe",
    "price_to_book",
    "eps_ttm",
    "eps_forward",
    # Dividend (matches info.py naming)
    "trailing_annual_dividend_yield",   # FRACTION
    "trailing_annual_dividend_rate",    # currency
    # 52-week
    "fifty_two_week_high",
    "fifty_two_week_low",
    "fifty_two_week_change_pct",        # PERCENT
    # Equity-specific timestamp
    "next_earnings_date",
    # ETF / fund-specific
    "net_assets",
    "expense_ratio_pct",                # PERCENT (netExpenseRatio)
    "ytd_return_pct",                   # PERCENT
    "three_year_return_pct",            # PERCENT (annualReturnNavY3)
    "five_year_return_pct",             # PERCENT (annualReturnNavY5)
)


# PE values blow up to nonsense when EPS is near zero (observed
# `forward_pe: -199000` for RKLB, `trailing_pe: 136000` for thinly-
# traded names). The most extreme legitimate PE we've seen is ~300
# for hot growth tickers, so 1000 is a safe absolute cap that
# preserves all real values while collapsing division-by-near-zero
# garbage to null. Applied to BOTH `trailing_pe` and `forward_pe`:
# Yahoo already returns null for negative-EPS stocks on `trailingPE`
# but occasional epsilon-EPS reads still leak through.
_PE_CLAMP_ABS = 1000.0


def _safe_pe(v):
    """safe_float + clamp |v| > _PE_CLAMP_ABS to None.

    Used for both trailing_pe and forward_pe in the projection.
    """
    f = safe_float(v)
    if f is None or abs(f) > _PE_CLAMP_ABS:
        return None
    return f


def _project_quote(q: dict) -> dict:
    """Project Yahoo's raw quote payload to QUOTE_FIELDS."""
    name = q.get("longName") or q.get("shortName") or q.get("displayName")
    return {
        "symbol": safe_str(q.get("symbol")),
        "name": safe_str(name),
        "quote_type": safe_str(q.get("quoteType")),
        "exchange": safe_str(q.get("exchange")),
        "full_exchange_name": safe_str(q.get("fullExchangeName")),
        "currency": safe_str(q.get("currency")),
        "region": safe_str(q.get("region")),
        "price": safe_float(q.get("regularMarketPrice")),
        "change_pct": safe_float(q.get("regularMarketChangePercent")),
        "volume": safe_int(q.get("regularMarketVolume")),
        "avg_volume_3m": safe_int(q.get("averageDailyVolume3Month")),
        "market_cap": safe_int(q.get("marketCap")),
        "trailing_pe": _safe_pe(q.get("trailingPE")),
        "forward_pe": _safe_pe(q.get("forwardPE")),
        "price_to_book": safe_float(q.get("priceToBook")),
        "eps_ttm": safe_float(q.get("epsTrailingTwelveMonths")),
        "eps_forward": safe_float(q.get("epsForward")),
        "trailing_annual_dividend_yield": safe_float(q.get("trailingAnnualDividendYield")),
        "trailing_annual_dividend_rate": safe_float(q.get("trailingAnnualDividendRate")),
        "fifty_two_week_high": safe_float(q.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": safe_float(q.get("fiftyTwoWeekLow")),
        "fifty_two_week_change_pct": safe_float(q.get("fiftyTwoWeekChangePercent")),
        "next_earnings_date": epoch_to_date(q.get("earningsTimestamp")),
        "net_assets": safe_int(q.get("netAssets")),
        "expense_ratio_pct": safe_float(q.get("netExpenseRatio")),
        "ytd_return_pct": safe_float(q.get("ytdReturn")),
        "three_year_return_pct": safe_float(q.get("annualReturnNavY3")),
        "five_year_return_pct": safe_float(q.get("annualReturnNavY5")),
    }


def _build_query(spec, cls):
    """Recursively build a `cls`-typed Query tree from a JSON dict.

    `spec` is a dict like:
        {"operator": "and", "operands": [<sub-spec> | <leaf-value>, ...]}
    Sub-dicts recurse; leaf values (strings / numbers) are passed through
    as terminal operands of value operators (eq, gt, btwn, …).
    """
    if not isinstance(spec, dict):
        raise ValueError(
            f"query node must be an object with 'operator'/'operands', "
            f"got {type(spec).__name__}: {spec!r}"
        )
    op = spec.get("operator")
    operands = spec.get("operands")
    if op is None or operands is None:
        raise ValueError(
            f"query node missing 'operator' or 'operands': {spec!r}"
        )
    if not isinstance(operands, list):
        raise ValueError(
            f"'operands' must be a list, got {type(operands).__name__}: {operands!r}"
        )
    converted = [_build_query(o, cls) if isinstance(o, dict) else o
                 for o in operands]
    return cls(op, converted)


def _humanize_query_error(exc: Exception, cls) -> str:
    """Strip the `<class 'yfinance.screener.query.EquityQuery'>` repr from
    yfinance's validator messages; add a hint about --quote-type if the
    rejected field is valid for a different subclass.

    NB: this depends on yfinance's exact error wording (`<class '...'>`
    in the repr; "Invalid field" prefix). If upstream changes either,
    we silently fall back to the raw message — wrap the whole thing in
    a try/except to make sure a humanizer bug never replaces the real
    error with a traceback.
    """
    try:
        msg = str(exc)
        # yfinance uses str(type(self)) which renders as "<class '...'>"; replace
        # with the user-facing label.
        label = _TYPE_LABEL_BY_CLASS.get(cls, "?")
        msg = msg.replace(
            f"<class 'yfinance.screener.query.{cls.__name__}'>",
            f"{cls.__name__} (--quote-type {label})",
        )
        # If the message names a field, check whether it's valid for a sibling
        # class — that's the most common cause of this error.
        if "Invalid field" in msg:
            # Extract field name (text inside the trailing quotes)
            parts = msg.rsplit('"', 2)
            field = parts[1] if len(parts) >= 2 else None
            if field:
                matched = []
                for sibling_cls, sibling_label in _TYPE_LABEL_BY_CLASS.items():
                    if sibling_cls is cls:
                        continue
                    sample = _make_sample(sibling_cls)
                    all_fields = set().union(
                        *(set(v) for v in sample.valid_fields.values())
                    )
                    if field in all_fields:
                        matched.append(sibling_label)
                if matched:
                    # `categoryname` is valid for both fund and ETF — list both
                    # rather than picking one arbitrarily.
                    msg += (
                        f" (hint: '{field}' is a valid field for "
                        f"--quote-type {' or '.join(matched)})"
                    )
        return msg
    except Exception:
        # Humanizer failed — fall back to the raw exception message rather
        # than poisoning the user-facing error with a different bug.
        return str(exc)


# Map predefined screen name → quote_type label, used to narrow the
# --quote-type warning to actual mismatches (passing the matching type
# alongside --predefined is harmless and shouldn't trigger a warning).
def _predefined_quote_type(name: str) -> str | None:
    body = yf.PREDEFINED_SCREENER_QUERIES.get(name)
    if not body:
        return None
    q = body.get("query")
    if isinstance(q, EquityQuery):
        return "equity"
    if isinstance(q, FundQuery):
        return "fund"
    if isinstance(q, ETFQuery):
        return "etf"
    return None


def _list_predefined() -> list:
    """Enumerate yf.PREDEFINED_SCREENER_QUERIES rows with quote_type + sort + description."""
    out = []
    for name, body in yf.PREDEFINED_SCREENER_QUERIES.items():
        q = body.get("query")
        if isinstance(q, EquityQuery):
            qt = "EQUITY"
        elif isinstance(q, FundQuery):
            qt = "MUTUALFUND"
        elif isinstance(q, ETFQuery):
            qt = "ETF"
        else:
            qt = None
        sort_type = (body.get("sortType") or "DESC").upper()
        out.append({
            "name": name,
            "quote_type": qt,
            "sort_field": safe_str(body.get("sortField")),
            "sort_asc": sort_type == "ASC",
            "description": _PREDEFINED_DESCRIPTIONS.get(name),
        })
    return out


def _list_fields(qt: str) -> dict:
    """Enumerate valid screener fields and value enums for a quote_type.

    QueryBase validators run in __init__, so we need a class-specific
    known-valid (op, operand) to instantiate without tripping
    validation. `_make_sample` handles that — see _SAMPLE_QUERY_INIT
    for the per-class init. From the sample we then read `valid_fields`
    / `valid_values` (no Yahoo round-trip).
    """
    cls = QUERY_CLASS_BY_TYPE[qt]
    sample = _make_sample(cls)
    fields = {category: list(items)
              for category, items in sample.valid_fields.items()}
    values = {}
    for fld, vv in sample.valid_values.items():
        if isinstance(vv, dict):
            # Yahoo's docstring helper nests some enums by sub-category
            # (e.g. exchange grouped by region) — flatten to a single sorted set.
            merged = set().union(*[set(v) for v in vv.values()])
            values[fld] = sorted(merged)
        else:
            try:
                values[fld] = sorted(vv)
            except TypeError:
                values[fld] = list(vv)
    return {
        "quote_type": qt.upper(),
        "fields_by_category": fields,
        "valid_values": values,
    }


def _parse_query_arg(raw: str) -> dict:
    """Resolve --query arg to a dict. Supports literal JSON or @path."""
    if raw.startswith("@"):
        path = Path(raw[1:])
        try:
            text = path.read_text()
        except OSError as exc:
            raise ValueError(f"cannot read query file {path}: {exc}") from exc
    else:
        text = raw
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--query is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError(
            f"--query must decode to an object, got {type(spec).__name__}"
        )
    return spec


def fetch(
    *,
    predefined: str | None = None,
    query_dict: dict | None = None,
    quote_type: str = "equity",
    count: int | None = None,
    offset: int | None = None,
    sort_field: str | None = None,
    sort_asc: bool | None = None,
    full: bool = False,
) -> dict:
    """Run a single screener call. Returns an envelope dict (one call,
    one result) — distinct from per-ticker wrappers' list-of-records shape.

    Envelope key order is metadata-first (predefined / title / description
    / total / returned / offset) then `quotes` last, so a long quotes array
    doesn't bury the screen identity for human readers.

    `full=True` skips the QUOTE_FIELDS projection and emits raw Yahoo
    payload per quote (~60-85 fields, varies by quote_type). Useful
    when you need fields outside the curated set (`epsCurrentYear`,
    `dividendDate`, `bookValue`, `trailingThreeMonthReturns`, etc.).
    """
    if predefined is not None:
        # Preempt yfinance's "is probably not a predefined query" stdout
        # print on bad names — it would land in our JSON stream.
        if predefined not in yf.PREDEFINED_SCREENER_QUERIES:
            return {
                "predefined": predefined,
                "error": (
                    f"unknown predefined screen {predefined!r}; "
                    f"see --list-predefined for the catalog"
                ),
                "error_kind": "not_found",
                "attempts": 0,
            }
        kwargs: dict = {}
        if count is not None:
            kwargs["count"] = count
        if offset is not None:
            kwargs["offset"] = offset
        if sort_field is not None:
            kwargs["sortField"] = sort_field
        if sort_asc is not None:
            kwargs["sortAsc"] = sort_asc
        raw, err_kind, attempts = with_retry(
            lambda: yf.screen(predefined, **kwargs)
        )
    else:
        cls = QUERY_CLASS_BY_TYPE[quote_type]
        try:
            q = _build_query(query_dict, cls)
        except (ValueError, TypeError) as exc:
            # Validation error from QueryBase or our walker. Treat as
            # not_found-class user error (no point retrying).
            return {
                "quote_type_filter": (
                    "MUTUALFUND" if quote_type == "fund"
                    else quote_type.upper()
                ),
                "error": f"invalid query: {_humanize_query_error(exc, cls)}",
                "error_kind": "not_found",
                "attempts": 0,
            }
        kwargs = {}
        # Custom queries take `size`, predefined take `count` (yfinance quirk).
        if count is not None:
            kwargs["size"] = count
        if offset is not None:
            kwargs["offset"] = offset
        if sort_field is not None:
            kwargs["sortField"] = sort_field
        if sort_asc is not None:
            kwargs["sortAsc"] = sort_asc
        raw, err_kind, attempts = with_retry(lambda: yf.screen(q, **kwargs))

    if err_kind:
        out = {}
        if predefined:
            out["predefined"] = predefined
        out["error"] = f"fetch failed ({err_kind}, after {attempts} attempt(s))"
        out["error_kind"] = err_kind
        out["attempts"] = attempts
        return out

    quotes_raw = raw.get("quotes") or []
    # Build envelope metadata-first; `quotes` last so the screen
    # identity / total / counts stay visible without scrolling past
    # 25-250 rows of quote data.
    out: dict = {}
    if predefined:
        out["predefined"] = predefined
        out["title"] = safe_str(raw.get("title"))
        out["description"] = safe_str(raw.get("description"))
    else:
        out["quote_type_filter"] = (
            "MUTUALFUND" if quote_type == "fund" else quote_type.upper()
        )
    out["total"] = safe_int(raw.get("total"))
    out["returned"] = len(quotes_raw)
    out["offset"] = safe_int(raw.get("start")) or 0
    if attempts > 1:
        out["attempts"] = attempts
    if not quotes_raw:
        out["note"] = (
            "no matches — filters may be too restrictive, or this predefined "
            "screen has zero hits in current market state"
        )
    out["quotes"] = quotes_raw if full else [
        _project_quote(qq) for qq in quotes_raw]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Run a Yahoo Finance screener and return up to ~250 quotes\n"
            "matching the criteria. Two modes: predefined (Yahoo's saved\n"
            "screens, e.g. day_gainers / top_etfs_us) and custom (build an\n"
            "AND/OR tree of field comparisons).\n\n"
            f"Per quote ({len(QUOTE_FIELDS)} fields): "
            f"{', '.join(QUOTE_FIELDS[:10])}, …\n"
            "Equity-only fields are null for ETFs/funds and vice versa."
        ),
        epilog=(
            "Examples:\n"
            "  # Predefined: top intraday US gainers\n"
            "  screener.py --predefined day_gainers --count 10\n"
            "\n"
            "  # Predefined: undervalued growth stocks (PE < 20, PEG < 1, EPS\n"
            "  # growth >= 25%)\n"
            "  screener.py --predefined undervalued_growth_stocks --count 25\n"
            "\n"
            "  # List all predefined screeners (name, quote_type, sort_field)\n"
            "  screener.py --list-predefined\n"
            "\n"
            "  # Custom: US large-caps under PE 15, sorted by market cap desc\n"
            "  screener.py --query '{\"operator\":\"and\",\"operands\":[\n"
            "      {\"operator\":\"eq\",\"operands\":[\"region\",\"us\"]},\n"
            "      {\"operator\":\"gt\",\"operands\":[\"intradaymarketcap\",1e10]},\n"
            "      {\"operator\":\"lt\",\"operands\":[\"peratio.lasttwelvemonths\",15]}\n"
            "  ]}' --sort-field intradaymarketcap --count 25\n"
            "\n"
            "  # Custom: read query JSON from file\n"
            "  screener.py --query @my_query.json --quote-type equity\n"
            "\n"
            "  # Discover valid fields for an equity query\n"
            "  screener.py --list-fields equity\n"
            "\n"
            "See references/screener.md for the field schema, the predefined\n"
            "screen catalog, custom-query field discovery, and unit caveats.\n"
            "SKILL.md covers cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode selection (mutually exclusive). One of these is required.
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--predefined", metavar="NAME",
        help="Run a Yahoo saved screen by name. See --list-predefined.",
    )
    mode.add_argument(
        "--query", metavar="JSON",
        help=(
            "Custom query as JSON (literal string or @path/to/file.json). "
            "Tree shape: {\"operator\": <op>, \"operands\": [...]}, where "
            "operands are nested query objects (for and/or) or "
            "[field, value...] leaf lists (for eq/gt/lt/btwn/is-in)."
        ),
    )
    mode.add_argument(
        "--list-predefined", action="store_true",
        help="Print the catalog of yfinance's predefined screens and exit.",
    )
    mode.add_argument(
        "--list-fields", choices=("equity", "fund", "etf"), metavar="QUOTE_TYPE",
        help=(
            "Print valid screener fields (and enum values where applicable) "
            "for the given quote_type and exit. Use this to discover field "
            "names for --query."
        ),
    )

    # Custom-query helpers. argparse default is None so we can tell
    # "user explicitly set this" apart from "user left default" — used
    # for the mismatch warning when paired with --predefined. When unset
    # AND --query is used, fetch() resolves to "equity".
    ap.add_argument(
        "--quote-type", default=None, choices=("equity", "fund", "etf"),
        help=(
            "Quote type for --query. When omitted with --query, defaults "
            "to equity. Determines which Query subclass wraps the tree "
            "(EquityQuery / FundQuery / ETFQuery), which dictates Yahoo's "
            "`quoteType` filter and the valid field set. Has no effect "
            "with --predefined (each predefined picks its own type); "
            "passing a mismatching type alongside --predefined prints a "
            "stderr warning."
        ),
    )

    # Pagination / sort
    ap.add_argument(
        "--count", type=int, metavar="N",
        help=(
            "Max results to return (default 25 per Yahoo, max 250). "
            "Maps to `count` for predefined screens, `size` for custom queries."
        ),
    )
    ap.add_argument(
        "--offset", type=int, metavar="N",
        help=(
            "Pagination offset (default 0). NB: with --predefined, supplying "
            "--offset switches yfinance to the custom-query API path "
            "(predefined endpoint ignores offset upstream)."
        ),
    )
    ap.add_argument(
        "--sort-field", metavar="FIELD",
        help=(
            "Field to sort by. Predefined screens supply their own default "
            "(see --list-predefined); custom queries default to 'ticker'."
        ),
    )
    ap.add_argument(
        "--sort", choices=("asc", "desc"), default=None,
        help=(
            "Sort direction. Default: each predefined uses its own (visible "
            "via --list-predefined); custom queries default to descending. "
            "Use this to override — e.g. --sort asc to flip a desc-by-default "
            "predefined."
        ),
    )

    ap.add_argument(
        "--format", default="json",
        choices=("json", "ndjson", "csv", "symbols"),
        help=(
            f"Output format. json (default) = single envelope dict with "
            f"metadata + a `quotes` array; ndjson = one JSON object per "
            f"quote per line (envelope keys not emitted; on error / no-match "
            f"emits a single record carrying the error / note); csv = one "
            f"row per quote (cols: the {len(QUOTE_FIELDS)} quote fields + "
            f"note + meta; not compatible with --full); symbols = one "
            f"ticker per line, no header — pipe-friendly "
            f"(`screener.py ... --format symbols | xargs info.py`); on "
            f"error / no-match, errors surface to stderr (rc unchanged). "
            f"Format applies to screener results; `--list-predefined` "
            f"supports json / ndjson / csv (csv = one row per screen, "
            f"`symbols` rejected since it's about tickers not screen names); "
            f"`--list-fields` is JSON-only (nested shape)."
        ),
    )

    ap.add_argument(
        "--full", action="store_true",
        help=(
            "Pass through the raw Yahoo quote payload (~60-85 fields, "
            "varies by quote_type) instead of the curated 28-field "
            "projection. Useful when you need fields outside the "
            "projection (epsCurrentYear, dividendDate, bookValue, "
            "trailingThreeMonthReturns, etc.). Incompatible with "
            "--format csv (raw keys would break CSV column stability "
            "across calls — argparse rejects the combo); use --format "
            "ndjson with --full, or drop --full for projected CSV."
        ),
    )

    args = ap.parse_args()

    # Warn if --quote-type was passed with --predefined AND it doesn't
    # match the predefined's actual type. Matching it (e.g. day_gainers
    # is EQUITY, user passes --quote-type equity) is redundant but
    # harmless — no need to bother them.
    if args.predefined and args.quote_type is not None:
        actual_qt = _predefined_quote_type(args.predefined)
        if actual_qt is not None and actual_qt != args.quote_type:
            print(
                f"warning: --quote-type {args.quote_type} ignored with "
                f"--predefined {args.predefined} (actually {actual_qt}; "
                f"each predefined picks its own type — see "
                f"--list-predefined)",
                file=sys.stderr,
            )

    # Reject --full --format csv: --full asks for raw Yahoo fields, but
    # CSV with raw keys would have unstable schema across calls (varies
    # by quote_type). User can pick one of the two; --format ndjson + --full
    # gives raw rows without the schema problem.
    if args.full and args.format == "csv":
        ap.error(
            "--full is incompatible with --format csv (raw Yahoo keys "
            "would break CSV column stability across calls); use "
            "--format ndjson with --full, or drop --full for the "
            "projected 28-column CSV"
        )

    # Discovery paths.
    if args.list_predefined:
        _emit_list_predefined(_list_predefined(), args.format, ap=ap)
        return
    if args.list_fields:
        if args.format != "json":
            ap.error(
                f"--list-fields supports json only "
                f"(got --format {args.format}; the field schema is nested)"
            )
        print(json.dumps(_list_fields(args.list_fields), indent=2,
                         ensure_ascii=False))
        return

    # Validate count/offset bounds early — surface the user error rather
    # than letting yfinance raise (its message is fine but late).
    if args.count is not None and (args.count < 1 or args.count > 250):
        ap.error("--count must be between 1 and 250 (Yahoo limit)")
    if args.offset is not None and args.offset < 0:
        ap.error("--offset must be >= 0")

    # Resolve --query JSON (literal or @file).
    query_dict = None
    if args.query is not None:
        try:
            query_dict = _parse_query_arg(args.query)
        except ValueError as exc:
            print(json.dumps({
                "error": str(exc),
                "error_kind": "not_found",
                "attempts": 0,
            }, indent=2, ensure_ascii=False))
            sys.exit(1)

    result = fetch(
        predefined=args.predefined,
        query_dict=query_dict,
        quote_type=args.quote_type or "equity",
        count=args.count,
        offset=args.offset,
        sort_field=args.sort_field,
        sort_asc=(True if args.sort == "asc"
                  else False if args.sort == "desc"
                  else None),
        full=args.full,
    )
    _emit(result, args.format)


def _emit_list_predefined(rows: list, fmt: str, ap=None) -> None:
    """Emit the predefined-screens catalog in json / ndjson / csv. The
    catalog is a flat list (one screen per row) so all three formats
    translate cleanly. `symbols` is rejected here because the format
    name is misleading for catalog rows (would emit screen names, not
    tickers); use `--format json` instead.
    """
    if fmt == "symbols":
        # Reject upfront — `symbols` semantically means tickers, not
        # screen names. Pointing users at csv (which has a `name` col)
        # is the cleaner answer.
        if ap is not None:
            ap.error(
                "--list-predefined doesn't support --format symbols "
                "(symbols emits tickers, but the catalog has screen "
                "names); use --format json|ndjson|csv instead"
            )
        return
    if fmt == "json":
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if fmt == "ndjson":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return
    # csv
    import csv as _csv
    cols = ["name", "quote_type", "sort_field", "sort_asc", "description"]
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for row in rows:
        writer.writerow([row.get(c, "") for c in cols])


def _emit(result: dict, fmt: str) -> None:
    """Emit the screener result in the requested format.

    json     one pretty-printed envelope dict (metadata first, quotes last)
    ndjson   one quote per line; on error / empty, one envelope-summary line
    csv      one row per quote; on error / empty, one row carrying note/meta
    symbols  one ticker per line; on error / empty, no output (rc unchanged)
    """
    if fmt == "json":
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return

    quotes = result.get("quotes") or []

    if fmt == "symbols":
        # Pipe-friendly: just the tickers, one per line. On error /
        # empty result, stdout stays empty BUT we surface the failure
        # to stderr so a caller piping into xargs / a shell loop sees
        # the signal (otherwise rc=0 + empty stdout looks identical to
        # "screen returned no matches at this moment").
        if result.get("error"):
            print(
                f"screener: {result['error']} "
                f"(error_kind={result.get('error_kind')})",
                file=sys.stderr,
            )
        elif not quotes and result.get("note"):
            print(f"screener: {result['note']}", file=sys.stderr)
        for q in quotes:
            sym = q.get("symbol")
            if sym:
                print(sym)
        return

    if fmt == "ndjson":
        if not quotes:
            # No data — emit a single line carrying the envelope so consumers
            # see error / note / metadata rather than parse-empty stdout.
            carry = {k: v for k, v in result.items() if k != "quotes"}
            print(json.dumps(carry, default=str, ensure_ascii=False))
            return
        for q in quotes:
            print(json.dumps(q, default=str, ensure_ascii=False))
        return

    # csv: one row per quote. Cols are the projected QUOTE_FIELDS plus
    # `note` and RESULT_META so error / no-match runs aren't silently
    # empty (they emit one row carrying just symbol="" + the meta cols).
    # `--full --format csv` is rejected upstream in main(), so quotes
    # here are always projected dicts with QUOTE_FIELDS keys.
    import csv as _csv
    cols = [*QUOTE_FIELDS, "note", *RESULT_META]
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    if not quotes:
        carry = {k: result.get(k, "") for k in ("note", *RESULT_META)}
        writer.writerow([carry.get(c, "") for c in cols])
        return
    for q in quotes:
        writer.writerow(
            [q.get(c, "") if c in QUOTE_FIELDS else "" for c in cols])


if __name__ == "__main__":
    main()
