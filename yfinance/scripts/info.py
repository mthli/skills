#!/usr/bin/env python3
"""Fetch yfinance Ticker.info for one or more tickers and print as JSON.

See `info.py --help` for usage and modes. Output is a JSON array on stdout,
one entry per ticker; failed tickers carry an "error" field instead of data
so a single bad symbol does not poison the batch. Field schema lives in the
SECTIONS / SUMMARY_FIELDS constants below.
"""
from __future__ import annotations
import yfinance as yf
from helpers import (
    RESULT_META, emit_json_or_ndjson, epoch_to_date, safe_float, safe_int,
    safe_str, with_retry,
)

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# No-coverage sentinels Yahoo has been observed to return (lowercased).
# "none" is the dominant one (~55 tickers verified); the others are
# defensive — Yahoo has no published spec for this field, so a wording
# shift (e.g. "n/a" or an em-dash) shouldn't silently leak through. Add
# new strings here if a real ticker ever surfaces one. Empty string is
# *not* listed: safe_str() collapses "" to None upstream of this check.
_NO_COVERAGE_SENTINELS = frozenset({"none", "n/a", "na", "unknown", "—", "-"})


def _normalize_recommendation_key(v):
    """Normalize Yahoo's no-coverage strings to None.
    Yahoo returns various sentinels OR JSON null for no-coverage tickers;
    consumers get a single null sentinel out of this function. Field-
    specific (only applies to recommendationKey), so it lives here
    rather than in helpers.py."""
    s = safe_str(v)
    if s is None:
        return None
    return None if s.lower() in _NO_COVERAGE_SENTINELS else s


# (output_key, yfinance_key, converter) — declarative schema; add a field with one line.
#
# Iteration order is part of the public output contract: JSON sections render
# in this order, and consumers (including references/info.md examples) rely
# on it. Don't reorder sections without bumping documentation; relies on
# Python 3.7+ dict insertion-order preservation.
SECTIONS: dict[str, list[tuple[str, str, Callable[[Any], Any]]]] = {
    "profile": [
        ("short_name", "shortName", safe_str),
        ("long_name", "longName", safe_str),
        ("sector", "sector", safe_str),
        ("industry", "industry", safe_str),
        ("country", "country", safe_str),
        ("city", "city", safe_str),
        ("website", "website", safe_str),
        ("employees", "fullTimeEmployees", safe_int),
        ("summary", "longBusinessSummary", safe_str),
    ],
    "valuation": [
        ("market_cap", "marketCap", safe_int),
        ("enterprise_value", "enterpriseValue", safe_int),
        ("trailing_pe", "trailingPE", safe_float),
        ("forward_pe", "forwardPE", safe_float),
        ("peg_ratio", "pegRatio", safe_float),
        ("price_to_book", "priceToBook", safe_float),
        ("price_to_sales", "priceToSalesTrailing12Months", safe_float),
        ("ev_to_revenue", "enterpriseToRevenue", safe_float),
        ("ev_to_ebitda", "enterpriseToEbitda", safe_float),
    ],
    "fundamentals": [
        ("trailing_eps", "trailingEps", safe_float),
        ("forward_eps", "forwardEps", safe_float),
        ("book_value", "bookValue", safe_float),
        ("revenue_per_share", "revenuePerShare", safe_float),
        ("profit_margin", "profitMargins", safe_float),
        ("operating_margin", "operatingMargins", safe_float),
        ("gross_margin", "grossMargins", safe_float),
        ("ebitda_margin", "ebitdaMargins", safe_float),
        ("return_on_assets", "returnOnAssets", safe_float),
        ("return_on_equity", "returnOnEquity", safe_float),
        ("revenue_growth", "revenueGrowth", safe_float),
        ("earnings_growth", "earningsGrowth", safe_float),
        ("earnings_quarterly_growth", "earningsQuarterlyGrowth", safe_float),
        ("total_revenue", "totalRevenue", safe_int),
        ("total_cash", "totalCash", safe_int),
        ("total_debt", "totalDebt", safe_int),
        ("debt_to_equity", "debtToEquity", safe_float),
        ("current_ratio", "currentRatio", safe_float),
        ("quick_ratio", "quickRatio", safe_float),
        ("free_cashflow", "freeCashflow", safe_int),
        ("operating_cashflow", "operatingCashflow", safe_int),
        ("beta", "beta", safe_float),
    ],
    "dividend": [
        ("dividend_rate", "dividendRate", safe_float),
        # NOTE: Yahoo's `dividendYield` is percent-encoded (0.38 = 0.38%),
        # while every other ratio in `info` is fraction. We deliberately
        # do NOT emit it — consumers should use `trailing_annual_dividend_yield`
        # (always fraction) to avoid the unit landmine.
        ("payout_ratio", "payoutRatio", safe_float),
        ("ex_dividend_date", "exDividendDate", epoch_to_date),
        ("five_year_avg_dividend_yield", "fiveYearAvgDividendYield", safe_float),
        ("trailing_annual_dividend_rate", "trailingAnnualDividendRate", safe_float),
        ("trailing_annual_dividend_yield",
         "trailingAnnualDividendYield", safe_float),
    ],
    "analyst": [
        ("recommendation_key", "recommendationKey", _normalize_recommendation_key),
        ("recommendation_mean", "recommendationMean", safe_float),
        ("number_of_analyst_opinions", "numberOfAnalystOpinions", safe_int),
        ("target_mean_price", "targetMeanPrice", safe_float),
        ("target_high_price", "targetHighPrice", safe_float),
        ("target_low_price", "targetLowPrice", safe_float),
        ("target_median_price", "targetMedianPrice", safe_float),
    ],
    "shares": [
        ("shares_outstanding", "sharesOutstanding", safe_int),
        ("float_shares", "floatShares", safe_int),
        ("shares_short", "sharesShort", safe_int),
        ("short_ratio", "shortRatio", safe_float),
        ("short_percent_of_float", "shortPercentOfFloat", safe_float),
        ("held_percent_insiders", "heldPercentInsiders", safe_float),
        ("held_percent_institutions", "heldPercentInstitutions", safe_float),
    ],
    "fund": [
        ("category", "category", safe_str),
        ("total_assets", "totalAssets", safe_int),
        ("nav_price", "navPrice", safe_float),
        ("ytd_return", "ytdReturn", safe_float),
        ("three_year_avg_return", "threeYearAverageReturn", safe_float),
        ("five_year_avg_return", "fiveYearAverageReturn", safe_float),
        ("fund_family", "fundFamily", safe_str),
        ("legal_type", "legalType", safe_str),
        ("expense_ratio", "annualReportExpenseRatio", safe_float),
    ],
}


def _build_section(info: dict, fields: list) -> dict:
    return {out_key: convert(info.get(src_key))
            for out_key, src_key, convert in fields}


# (output_key, source_path) — source_path is "section.field" for nested,
# or "field" for top-level. Drives --summary projection. Yield field is
# `trailing_annual_dividend_yield` (fraction) — see the dividend SECTIONS
# entry for why we don't expose Yahoo's percent-encoded `dividend_yield`.
SUMMARY_FIELDS = [
    ("symbol",                          "symbol"),
    ("quote_type",                      "quote_type"),
    ("currency",                        "currency"),
    ("exchange",                        "exchange"),
    ("sector",                          "profile.sector"),
    ("industry",                        "profile.industry"),
    ("category",                        "fund.category"),
    ("market_cap",                      "valuation.market_cap"),
    ("trailing_pe",                     "valuation.trailing_pe"),
    ("forward_pe",                      "valuation.forward_pe"),
    ("trailing_eps",                    "fundamentals.trailing_eps"),
    ("trailing_annual_dividend_yield",  "dividend.trailing_annual_dividend_yield"),
    ("target_mean_price",               "analyst.target_mean_price"),
    ("recommendation_key",              "analyst.recommendation_key"),
    ("total_assets",                    "fund.total_assets"),
    ("expense_ratio",                   "fund.expense_ratio"),
    ("five_year_avg_return",            "fund.five_year_avg_return"),
]


def _validate_summary_fields() -> None:
    """Sanity check at module load: every path in SUMMARY_FIELDS must resolve
    to a real entry in SECTIONS (or a known top-level key), so renames in
    SECTIONS don't silently break --summary projection."""
    top_level = {"symbol", "quote_type", "currency", "exchange"}
    for out_key, path in SUMMARY_FIELDS:
        if "." in path:
            section, field = path.split(".", 1)
            if section not in SECTIONS:
                raise RuntimeError(
                    f"SUMMARY_FIELDS: unknown section {section!r} in path {path!r}")
            if not any(out == field for out, _src, _conv in SECTIONS[section]):
                raise RuntimeError(
                    f"SUMMARY_FIELDS: path {path!r} not found in SECTIONS[{section!r}]")
        elif path not in top_level:
            raise RuntimeError(
                f"SUMMARY_FIELDS: top-level key {path!r} not in {top_level}")


_validate_summary_fields()


def _summarize(full: dict) -> dict:
    """Project the full nested info into a flat headline-numbers dict."""
    if "error" in full:
        return full
    out = {}
    for out_key, path in SUMMARY_FIELDS:
        if "." in path:
            section, field = path.split(".", 1)
            out[out_key] = full.get(section, {}).get(field)
        else:
            out[out_key] = full.get(path)
    # Preserve top-level metadata that lives outside SECTIONS. RESULT_META
    # covers error/error_kind/attempts; error/error_kind would mean we
    # took the early-return branch above, so in practice only `attempts`
    # flows through here on retry. Iterating RESULT_META stays drift-proof
    # if new meta fields land.
    for key in RESULT_META:
        if key in full:
            out[key] = full[key]
    return out


def fetch(symbol: str) -> dict:
    info, err_kind, attempts = with_retry(lambda: yf.Ticker(symbol).info)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }

    # quoteType is the canonical signal that Yahoo recognized the ticker.
    # Empirically (verified across EQUITY / ETF / MUTUALFUND / INDEX /
    # CRYPTOCURRENCY / FUTURE / CURRENCY) every real ticker type returns
    # quoteType populated; bogus / delisted tickers return junk payloads
    # like {"trailingPegRatio": ...} without it. Treat missing quoteType
    # as the error sentinel.
    if not info or not info.get("quoteType"):
        return {
            "symbol": symbol,
            "error": "no info returned (delisted, wrong suffix, or rate-limited)",
            "error_kind": "not_found",
            "attempts": attempts,
        }

    quote_type = safe_str(info.get("quoteType"))
    out = {
        "symbol": symbol,
        "quote_type": quote_type,
        "currency": safe_str(info.get("currency")),
        "exchange": safe_str(info.get("exchange")),
    }
    for section, fields in SECTIONS.items():
        # `fund` is fund-only metadata; omit for stocks / indexes / crypto / etc.
        if section == "fund" and quote_type not in ("ETF", "MUTUALFUND"):
            continue
        out[section] = _build_section(info, fields)
    # Surface attempts only when actually retried (success path).
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    # Compute schema counts from the source-of-truth structures so the help
    # text stays accurate when fields are added without manual sync.
    stock_sections = [k for k in SECTIONS if k != "fund"]
    fund_sections = list(SECTIONS.keys())
    stock_field_count = sum(len(SECTIONS[s]) for s in stock_sections)
    fund_field_count = sum(len(SECTIONS[s]) for s in fund_sections)
    summary_field_count = len(SUMMARY_FIELDS)

    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance company / fund metadata via yfinance.\n\n"
            "Two output modes:\n"
            f"  default     full grouped JSON per ticker — {len(stock_sections)} sections for\n"
            f"              stocks (~{stock_field_count} fields), {len(fund_sections)} sections for ETFs / mutual\n"
            f"              funds (~{fund_field_count} fields, includes `fund` section)\n"
            f"  --summary   flat {summary_field_count}-field dict per ticker for peer comparison\n"
            "              (~10x smaller than default)"
        ),
        epilog=(
            "Examples:\n"
            "  info.py AAPL\n"
            "  info.py AAPL MSFT 0700.HK\n"
            "  info.py --summary AAPL MSFT GOOGL\n"
            "  info.py --summary --format csv AAPL MSFT GOOGL  # peer-table CSV\n"
            "  info.py --format ndjson AAPL MSFT               # one JSON per line\n"
            "\n"
            "See references/info.md for the full schema, unit landmines, and\n"
            "presentation guidance."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--summary", action="store_true",
                    help=f"Project full sections to flat {summary_field_count}-field dict for peer comparison")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one JSON record per line; "
                         "csv = one row per ticker — only valid with --summary "
                         "(default mode is nested and not CSV-friendly).")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix (e.g., 0700.HK)")
    args = ap.parse_args()

    if args.format == "csv" and not args.summary:
        ap.error("--format csv requires --summary (default mode has nested "
                 "sections that don't flatten to CSV cleanly)")

    results = [fetch(s.strip().upper()) for s in args.symbols if s.strip()]
    if args.summary:
        results = [_summarize(r) for r in results]
    _emit(results, args.format, summary=args.summary)


def _emit(results: list, fmt: str, *, summary: bool) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # csv (summary mode only — main() blocks default + csv).
    import csv as _csv
    cols = [out_key for out_key, _path in SUMMARY_FIELDS] + list(RESULT_META)
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
