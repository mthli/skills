#!/usr/bin/env python3
"""Fetch yfinance fund-level data (top holdings, sector / asset / bond
weightings, equity / bond metrics, fund operations) for one or more
ETFs / mutual funds and print as JSON / NDJSON / CSV.

See `fund_holdings.py --help` for usage. Output is a JSON array on
stdout, one entry per ticker. Non-fund tickers (equity / index / crypto
/ FX / future) carry a `note` plus the resolved `quote_type` instead
of data — the underlying yfinance call raises YFDataException, but the
parser sets `_quote_type` BEFORE that exception, so we capture it
inline and the caller doesn't need a fast_info chain for
disambiguation. Bogus tickers carry "error" / "error_kind: not_found"
via the standard retry path; on that path `quote_type` is null
(parser never ran). Field schema lives in the *_KEYS / *_CSV_COLS
constants below.
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
from yfinance.exceptions import YFDataException


# Yahoo's funds_data endpoint covers ETFs and mutual funds. Empirically
# verified (2026-05) to raise YFDataException("<SYM>: No Fund data found.")
# for: equity (AAPL), index (^GSPC), cryptocurrency (BTC-USD), currency
# (EURUSD=X), future (ES=F). Bogus / delisted symbols raise HTTPError 404
# at fetch time (before the parser runs) — we route those through the
# standard `not_found` error path instead of the note path so callers
# can distinguish "valid symbol, just not a fund" from "ticker doesn't
# exist". The `quote_type` is captured on the non-fund path too: yfinance's
# parser sets `_quote_type` BEFORE the KeyError that triggers
# YFDataException (verified 2026-05 against AAPL / ^GSPC / BTC-USD /
# EURUSD=X / ES=F), so we can surface the resolved asset type without an
# extra fast_info round-trip.
_NON_FUND_NOTE = (
    "no fund data — Yahoo's funds endpoint covers ETFs and mutual funds "
    "only. See `quote_type` for the resolved asset type; use info for "
    "equity fundamentals, history for index / crypto / FX / future "
    "price data."
)
# `_NonFundResult` (the sentinel that pairs with `_NON_FUND_NOTE`) is
# defined just before `_fetch_funds` below — colocated with the only
# code path that constructs / inspects it.

# Field schema. Weights / asset percentages / expense ratios / growth
# fields are FRACTIONS (0.000945 = 0.0945% expense ratio). P/E / P/B /
# P/S / P/CF in equity_metrics are conventional MULTIPLES (>1) — we
# invert from Yahoo's raw 1/ratio. AUM (`total_net_assets_millions`)
# and `median_market_cap` are in MILLIONS of fund-reporting currency.
# See references/fund_holdings.md "Units" for the full table.
_OPERATIONS_KEYS = (
    "expense_ratio",                # fraction (FUND vs CATEGORY pair)
    "expense_ratio_category_avg",
    "turnover",                     # fraction (annual)
    "turnover_category_avg",
    "total_net_assets_millions",    # MILLIONS of fund-reporting currency
    "total_net_assets_category_avg_millions",
)

# Asset-class composition (sums to ~1.0 for most funds; can be slightly
# negative for cash positions in funds that use leverage / shorts —
# verified for VFIAX, cash = -0.0003).
_ASSET_KEYS = (
    "stock_pct",
    "bond_pct",
    "cash_pct",
    "preferred_pct",
    "convertible_pct",
    "other_pct",
)
# Yahoo's raw key → our canonical key. The renames drop the redundant
# "Position" suffix and add "_pct" to make the fraction encoding obvious.
_ASSET_RAW_TO_KEY = {
    "stockPosition":       "stock_pct",
    "bondPosition":        "bond_pct",
    "cashPosition":        "cash_pct",
    "preferredPosition":   "preferred_pct",
    "convertiblePosition": "convertible_pct",
    "otherPosition":       "other_pct",
}

# equity_holdings + bond_holdings rows. Each label has a (key, handler)
# tuple; the handler runs on both the fund's own column AND the
# "Category Average" column. The category_avg flat-key is
# `<key>_category_avg`. **VFIAX is the empirical witness for the
# both-columns-populated case** (2026-05): equity_holdings
# Price/Earnings raw 0.03874 + cat avg 0.04351; bond_holdings Duration
# raw <NA> + cat avg 4.6 years. SPY's category_avg cells were all
# <NA> in the same probe — equity / mutual / mixed funds vary in how
# completely Yahoo populates this column.
#
# Per-handler unit story (the surprising ones):
#  _invert_or_none — Yahoo's raw is 1/ratio. SPY priceToEarnings=0.03706
#                    inverts to P/E=27.0 (matches Yahoo Finance website).
#                    Bond ETFs return 0.0 (sentinel, → None to dodge 1/0).
#  safe_int        — median_market_cap raw is in MILLIONS of fund-reporting
#                    currency (verified VFIAX = 404537.56 ≈ \$404B median
#                    market cap, plausible for an S&P 500 fund's mega-cap-
#                    weighted distribution). Truncates fractional component
#                    per helpers.safe_int convention for count fields.
#  _to_fraction    — Yahoo emits 3y earnings growth in PERCENT
#                    (verified VFIAX = 18.03 → ≈18% S&P 500 3y trailing
#                    EPS growth). We divide by 100 to match info /
#                    financials --summary fraction conventions, so callers
#                    can read it the same way as info.fund.three_year_avg_return.
#  safe_float      — bond duration / maturity / credit_quality. Duration /
#                    maturity are in YEARS; credit_quality is opaque
#                    (Yahoo doesn't document the scale).
_EQUITY_METRICS_KEYS = (
    "pe_ratio", "pe_ratio_category_avg",
    "pb_ratio", "pb_ratio_category_avg",
    "ps_ratio", "ps_ratio_category_avg",
    "pcf_ratio", "pcf_ratio_category_avg",
    "median_market_cap", "median_market_cap_category_avg",
    "earnings_growth_3y", "earnings_growth_3y_category_avg",
)
_BOND_METRICS_KEYS = (
    "duration_years", "duration_years_category_avg",
    "maturity_years", "maturity_years_category_avg",
    "credit_quality", "credit_quality_category_avg",
)
# `_EQUITY_ROW_TO_FIELD` / `_BOND_ROW_TO_FIELD` are defined further down
# the file — they reference `_invert_or_none` / `_to_fraction`, which
# need to be in scope first. See `_project_equity_metrics` /
# `_project_bond_metrics` for the consumer side.

# Per-holding schema (top_holdings DataFrame). `weight` is the
# Holding Percent — FRACTION (0.0785 = 7.85% of fund AUM).
_HOLDING_KEYS = ("symbol", "name", "weight")

# Default-mode CSV: row-per-record with a `record_class` discriminator
# similar to holders.py's `holder_class`. Eight classes:
#   meta            — single row carrying description + fund_overview + meta
#   operations      — single row, expense ratio / turnover / AUM
#   asset_class     — one row per asset class (stock / bond / cash / ...)
#   sector          — one row per sector (tech / health / ...) or empty
#                     for bond funds
#   bond_rating     — one row per rating bucket (us_government / aaa / ...)
#   equity_metric   — single row, PE / PB / PS / PCF / market cap / growth
#   bond_metric     — single row, duration / maturity / credit quality
#   holding         — one row per top holding (up to --limit, default 10)
# Empty-data tickers (note / error) emit a single carry row.
_DEFAULT_CSV_COLS = (
    "symbol", "quote_type", "record_class",
    # fund_overview + description carried on the meta row
    "category", "family", "legal_type", "description",
    # operations row
    *_OPERATIONS_KEYS,
    # generic key/value cols used by asset_class / sector / bond_rating rows
    "bucket", "weight",
    # equity_metric + bond_metric flat columns
    *_EQUITY_METRICS_KEYS, *_BOND_METRICS_KEYS,
    # holding row cols (`weight` reused above)
    "holding_symbol", "holding_name",
    "note", *RESULT_META,
)

# Summary-mode flat projection. Designed for ETF peer comparison —
# expense ratio, AUM, top-holding concentration, top sector, key
# multiples, plus a duration field for bond-ETF peer compare.
_SUMMARY_FLAT_KEYS = (
    "quote_type",
    "category", "family",
    "expense_ratio", "turnover",
    "total_net_assets_millions",
    "stock_pct", "bond_pct", "cash_pct",
    "top_holding_symbol", "top_holding_weight",
    "holdings_concentration",     # sum of weights across holdings_returned rows;
                                  # NB Yahoo caps at 10, but bond ETFs often
                                  # return 0–1, so this is "returned-rows
                                  # concentration" not always "top-10". Read
                                  # alongside `holdings_returned` to know N.
    "holdings_returned",          # how many top_holdings rows we got (≤10)
    "top_sector", "top_sector_weight",
    "pe_ratio", "pb_ratio",
    "duration_years",             # bond-ETF peer compare
    "earnings_growth_3y",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS, "note", *RESULT_META)


class _NonFundResult:
    """Sentinel returned by `_fetch_funds`'s retry-wrapped closure when
    the symbol is valid but not a fund (YFDataException from yfinance's
    parser). Carries the `quote_type` that yfinance resolved before the
    parse error raised — saves the caller a fast_info chain for
    disambiguation. `__slots__` because we make one of these per
    non-fund ticker and never mutate them."""
    __slots__ = ("quote_type",)

    def __init__(self, quote_type: str | None):
        self.quote_type = quote_type


def _fetch_funds(symbol: str):
    """Trigger Yahoo's funds_data fetch + return the FundsData object.

    Single HTTP request — yfinance's FundsData object fetches all four
    quoteSummary modules (quoteType, summaryProfile, topHoldings,
    fundProfile) on first property access, then serves the rest from
    cached attributes. We touch `description` first to drive the fetch,
    then read the rest from the now-populated cache.

    Returns a `_NonFundResult(quote_type=...)` instance when
    YFDataException is raised (valid symbol, just not a fund — equity
    / index / crypto / FX / future). YFDataException's message ("No
    Fund data found.") doesn't pattern-match helpers.classify_error's
    not_found heuristic, so without this catch it would surface as
    `error_kind=unknown` and burn retries needlessly. The `quote_type`
    is recoverable post-exception because yfinance's parser sets
    `_quote_type` BEFORE the KeyError raises — we recover it via a
    `fd.quote_type()` call on the same object after the catch. Other
    exceptions (HTTPError 404 for bogus, timeouts, 429s) propagate to
    with_retry's classifier as normal.
    """
    def _f():
        t = yf.Ticker(symbol)
        fd = t.funds_data
        try:
            # Drives the underlying HTTP. Cached for subsequent reads.
            _ = fd.description
        except YFDataException:
            # quote_type was set by the parser before KeyError; surface
            # it so the caller doesn't need a follow-up fast_info call.
            try:
                qt = fd.quote_type()
            except Exception:
                qt = None
            return _NonFundResult(safe_str(qt))
        return fd
    return with_retry(_f)


def _project_operations(df) -> dict:
    """fund_operations DataFrame (3 rows × 2 cols [<sym>, Category Average])
    → flat dict. Idx label is in the first row's "Attributes" col."""
    out = {k: None for k in _OPERATIONS_KEYS}
    if df is None or df.empty:
        return out
    sym_col = df.columns[0]   # The fund's own column (e.g. "SPY")
    cat_col = "Category Average"

    def _get(label, col):
        try:
            return safe_float(df.loc[label, col])
        except (KeyError, AttributeError):
            return None

    out["expense_ratio"]                          = _get("Annual Report Expense Ratio", sym_col)
    out["expense_ratio_category_avg"]             = _get("Annual Report Expense Ratio", cat_col)
    out["turnover"]                               = _get("Annual Holdings Turnover",    sym_col)
    out["turnover_category_avg"]                  = _get("Annual Holdings Turnover",    cat_col)
    out["total_net_assets_millions"]              = _get("Total Net Assets",            sym_col)
    out["total_net_assets_category_avg_millions"] = _get("Total Net Assets",            cat_col)
    return out


def _project_asset_classes(d) -> dict:
    """asset_classes raw dict → renamed flat dict (see _ASSET_RAW_TO_KEY)."""
    out = {k: None for k in _ASSET_KEYS}
    if not isinstance(d, dict):
        return out
    for raw_key, our_key in _ASSET_RAW_TO_KEY.items():
        if raw_key in d:
            out[our_key] = safe_float(d[raw_key])
    return out


def _invert_or_none(v):
    """Yahoo's price-multiple encoding is 1/ratio; invert to conventional
    P/E, P/B, P/S, P/CF. 0.0 (seen on bond ETFs — Yahoo's "no equity
    component" sentinel) → None instead of inf."""
    f = safe_float(v)
    if f is None or f == 0.0:
        return None
    return 1.0 / f


def _to_fraction(v):
    """Yahoo emits some growth fields in PERCENT (e.g. equity_holdings
    `threeYearEarningsGrowth.raw` = 18.03 for VFIAX, verified 2026-05).
    We divide by 100 so callers see fractions matching `info.fund.*_return`
    and `financials --summary.*_growth_yoy` conventions (`0.1803` =
    18.03% YoY). NaN/Inf-safe via `safe_float`."""
    f = safe_float(v)
    return None if f is None else f / 100.0


# Row label in Yahoo's DataFrame → (canonical key, value handler).
# Handler runs on BOTH the fund's column and "Category Average" column,
# emitting `<key>` and `<key>_category_avg` respectively.
_EQUITY_ROW_TO_FIELD = {
    "Price/Earnings":          ("pe_ratio",           _invert_or_none),
    "Price/Book":              ("pb_ratio",           _invert_or_none),
    "Price/Sales":             ("ps_ratio",           _invert_or_none),
    "Price/Cashflow":          ("pcf_ratio",          _invert_or_none),
    "Median Market Cap":       ("median_market_cap",  safe_int),
    "3 Year Earnings Growth":  ("earnings_growth_3y", _to_fraction),
}
_BOND_ROW_TO_FIELD = {
    "Duration":        ("duration_years",  safe_float),
    "Maturity":        ("maturity_years",  safe_float),
    "Credit Quality":  ("credit_quality",  safe_float),
}


def _project_two_col(df, row_to_field, all_keys) -> dict:
    """Generic project for the equity_holdings / bond_holdings shape:
    indexed DataFrame with two columns [<sym>, "Category Average"].
    Surfaces both columns via `<key>` and `<key>_category_avg`, applying
    the handler to each. Missing label or column → field stays None."""
    out = {k: None for k in all_keys}
    if df is None or df.empty:
        return out
    sym_col = df.columns[0]
    cat_col = "Category Average"
    for label, (key, handler) in row_to_field.items():
        for col, suffix in ((sym_col, ""), (cat_col, "_category_avg")):
            try:
                out[f"{key}{suffix}"] = handler(df.loc[label, col])
            except (KeyError, AttributeError):
                pass
    return out


def _project_equity_metrics(df) -> dict:
    return _project_two_col(df, _EQUITY_ROW_TO_FIELD, _EQUITY_METRICS_KEYS)


def _project_bond_metrics(df) -> dict:
    return _project_two_col(df, _BOND_ROW_TO_FIELD, _BOND_METRICS_KEYS)


def _project_holdings(df) -> list[dict]:
    """top_holdings DataFrame → list of {symbol, name, weight}.
    DataFrame is indexed by Symbol; columns are Name + Holding Percent."""
    if df is None or df.empty:
        return []
    rows = df.reset_index().to_dict(orient="records")
    out = []
    for r in rows:
        out.append({
            "symbol": safe_str(r.get("Symbol")),
            "name":   safe_str(r.get("Name")),
            "weight": safe_float(r.get("Holding Percent")),
        })
    return out


def _project_dict_weights(d) -> dict:
    """sector_weightings / bond_ratings — both shaped as flat dicts of
    {bucket: fraction}. Pass through, NaN/Inf-safe. Empty dict for bond
    ETFs (sector_weightings) or pure-equity ETFs (bond_ratings beyond
    us_government:0.0)."""
    if not isinstance(d, dict):
        return {}
    return {k: safe_float(v) for k, v in d.items()}


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for ETF peer comparison.

    Carries note / error / error_kind / attempts through unchanged so
    non-fund / failed tickers still surface in summary CSVs.
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}

    out["quote_type"] = full.get("quote_type")
    overview = full.get("fund_overview") or {}
    out["category"] = overview.get("category")
    out["family"]   = overview.get("family")

    ops = full.get("operations") or {}
    out["expense_ratio"]              = ops.get("expense_ratio")
    out["turnover"]                   = ops.get("turnover")
    out["total_net_assets_millions"]  = ops.get("total_net_assets_millions")

    assets = full.get("asset_classes") or {}
    out["stock_pct"] = assets.get("stock_pct")
    out["bond_pct"]  = assets.get("bond_pct")
    out["cash_pct"]  = assets.get("cash_pct")

    holdings = full.get("top_holdings") or []
    out["holdings_returned"] = len(holdings)
    if holdings:
        out["top_holding_symbol"] = holdings[0].get("symbol")
        out["top_holding_weight"] = holdings[0].get("weight")
        weights = [h.get("weight") for h in holdings if h.get("weight") is not None]
        out["holdings_concentration"] = sum(weights) if weights else None

    sectors = full.get("sector_weightings") or {}
    if sectors:
        items = [(k, v) for k, v in sectors.items() if v is not None]
        if items:
            top = max(items, key=lambda kv: kv[1])
            out["top_sector"]        = top[0]
            out["top_sector_weight"] = top[1]

    em = full.get("equity_metrics") or {}
    out["pe_ratio"]            = em.get("pe_ratio")
    out["pb_ratio"]            = em.get("pb_ratio")
    out["earnings_growth_3y"]  = em.get("earnings_growth_3y")

    bm = full.get("bond_metrics") or {}
    out["duration_years"] = bm.get("duration_years")

    for k in ("note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def _apply_limit(result: dict, limit: int | None) -> dict:
    """Truncate top_holdings to `limit` rows IN-PLACE. Mutates `result`."""
    if limit is None:
        return result
    if "top_holdings" in result:
        result["top_holdings"] = result["top_holdings"][:limit]
    return result


def fetch(symbol: str) -> dict:
    """Fetch the full Yahoo funds_data payload for `symbol`. No `--limit` —
    callers slice via `_apply_limit` (default mode) or read from full
    list (`--summary`).

    Three-way result shape:
      success           — all sections populated; no note / error.
      non-fund          — YFDataException caught, success-with-note.
      failure           — bogus / network / 429; standard error envelope.
    """
    fd, err_kind, attempts = _fetch_funds(symbol)
    if isinstance(fd, _NonFundResult):
        out = {
            "symbol": symbol,
            "quote_type": fd.quote_type,
            "note": _NON_FUND_NOTE,
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }

    # Successful fetch — pull all 9 cached FundsData properties (description,
    # fund_overview, fund_operations, asset_classes, top_holdings,
    # equity_holdings, bond_holdings, bond_ratings, sector_weightings)
    # plus the quote_type method. Every read after the first is free.
    out = {
        "symbol":            symbol,
        "quote_type":        safe_str(fd.quote_type()),
        "description":       safe_str(fd.description),
        "fund_overview": {
            "category":   safe_str(fd.fund_overview.get("categoryName")),
            "family":     safe_str(fd.fund_overview.get("family")),
            "legal_type": safe_str(fd.fund_overview.get("legalType")),
        },
        "operations":        _project_operations(fd.fund_operations),
        "asset_classes":     _project_asset_classes(fd.asset_classes),
        "sector_weightings": _project_dict_weights(fd.sector_weightings),
        "bond_ratings":      _project_dict_weights(fd.bond_ratings),
        "equity_metrics":    _project_equity_metrics(fd.equity_holdings),
        "bond_metrics":      _project_bond_metrics(fd.bond_holdings),
        "top_holdings":      _project_holdings(fd.top_holdings),
    }
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance fund-level data for one or more ETFs /\n"
            "mutual funds.\n\n"
            "Nine sections per ticker (single HTTP call covers all):\n"
            "  fund_overview     — category / family / legal_type\n"
            "  description       — fund prospectus prose (can be empty for\n"
            "                      some non-US ETFs)\n"
            "  operations        — expense ratio, turnover, AUM (millions)\n"
            "  asset_classes     — stock/bond/cash/preferred/conv/other %\n"
            "  sector_weightings — per-sector % (empty for bond funds)\n"
            "  bond_ratings      — per-credit-rating % (mostly empty for\n"
            "                      pure-equity funds)\n"
            "  equity_metrics    — PE / PB / PS / PCF (INVERTED from Yahoo's\n"
            "                      raw 1/ratio encoding) + median market cap\n"
            "                      + 3y earnings growth\n"
            "  bond_metrics      — duration / maturity (years) + credit quality\n"
            "  top_holdings      — up to 10 positions with symbol / name / weight\n\n"
            "Equity / index / crypto / FX / future tickers raise YFDataException\n"
            "in yfinance and surface here as success-with-note. The resolved\n"
            "`quote_type` is captured inline (yfinance's parser sets it BEFORE\n"
            "the parse error raises, so we recover it without an extra round-\n"
            "trip). Bogus / delisted tickers route through the standard\n"
            "`error_kind: not_found` path; on that path `quote_type` is null\n"
            "(parser never ran).\n\n"
            "UNITS: all weights / asset percentages / expense ratios /\n"
            "growth fields are FRACTIONS (0.000945 = 0.0945%). P/E / P/B /\n"
            "P/S / P/CF are conventional multiples (>1) — we INVERT Yahoo's\n"
            "raw `1/ratio` encoding so SPY's raw 0.03706 becomes 26.98.\n"
            "earnings_growth_3y is also unit-normalized — we divide Yahoo's\n"
            "raw percent by 100 to match info / financials conventions.\n"
            "total_net_assets and median_market_cap are in MILLIONS of\n"
            "fund-reporting currency. duration_years / maturity_years are\n"
            "in years. See references/fund_holdings.md for the full table."
        ),
        epilog=(
            "Examples:\n"
            "  fund_holdings.py SPY                                    # all sections\n"
            "  fund_holdings.py --summary SPY VTI QQQ                  # peer compare\n"
            "  fund_holdings.py --limit 5 SPY                          # top 5 holdings only\n"
            "  fund_holdings.py --format ndjson SPY VTI                # one record per line\n"
            "  fund_holdings.py --format csv --summary SPY VTI QQQ     # peer-compare CSV\n"
            "  fund_holdings.py --format csv SPY                       # row-per-record\n"
            "                                                          # (record_class discriminator)\n"
            "\n"
            "See references/fund_holdings.md for the field schema, presentation guidance,\n"
            "and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap top_holdings rows per ticker. Default: keep "
                         "all (Yahoo returns up to 10). Does not affect "
                         "the other sections. Silently ignored in --summary "
                         "mode — holdings_concentration / holdings_returned "
                         "are computed from Yahoo's full response.")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection for ETF peer compare: "
                         "expense_ratio, AUM, top-holding, top-sector, "
                         "PE / PB, duration. ~10× smaller than default.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one record per line; csv (default mode) "
                         "= one row per RECORD with `record_class` "
                         "discriminator (meta / operations / asset_class / "
                         "sector / bond_rating / equity_metric / bond_metric "
                         "/ holding) and `quote_type` repeated on every row "
                         "(self-describing for downstream filters); --summary "
                         "csv = strict one-row-per-ticker, also with a "
                         "`quote_type` column.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="ETF / mutual fund ticker(s); case-insensitive.")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    results = [fetch(s.strip().upper())
               for s in args.symbols if s.strip()]

    if args.summary:
        results = [_summarize(r) for r in results]
        _emit_summary(results, args.format)
    else:
        results = [_apply_limit(r, args.limit) for r in results]
        _emit_default(results, args.format)


def _emit_default(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        # `quote_type` and the carry fields (`note` / `error` / `error_kind`
        # / `attempts`) get repeated on every row of a ticker so each row
        # is self-describing — a downstream filter on `quote_type=ETF`
        # works on holding rows as well as the meta row.
        base = {"symbol": symbol, "quote_type": r.get("quote_type", "")}
        carry = {k: r.get(k, "") for k in ("note", *RESULT_META) if k in r}
        # Errored or non-fund: one carry row, no data sections.
        if "error" in r or "note" in r:
            row = {**base, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        # meta row — description + fund_overview
        overview = r.get("fund_overview") or {}
        meta_row = {
            **base, "record_class": "meta",
            "category":    overview.get("category"),
            "family":      overview.get("family"),
            "legal_type":  overview.get("legal_type"),
            "description": r.get("description"),
            **carry,
        }
        writer.writerow([meta_row.get(c, "") for c in cols])
        # operations row
        ops = r.get("operations") or {}
        if any(v is not None for v in ops.values()):
            row = {**base, "record_class": "operations", **ops, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # asset_class rows
        for bucket, weight in (r.get("asset_classes") or {}).items():
            row = {**base, "record_class": "asset_class",
                   "bucket": bucket, "weight": weight, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # sector rows
        for bucket, weight in (r.get("sector_weightings") or {}).items():
            row = {**base, "record_class": "sector",
                   "bucket": bucket, "weight": weight, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # bond_rating rows
        for bucket, weight in (r.get("bond_ratings") or {}).items():
            row = {**base, "record_class": "bond_rating",
                   "bucket": bucket, "weight": weight, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # equity_metric row
        em = r.get("equity_metrics") or {}
        if any(v is not None for v in em.values()):
            row = {**base, "record_class": "equity_metric", **em, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # bond_metric row
        bm = r.get("bond_metrics") or {}
        if any(v is not None for v in bm.values()):
            row = {**base, "record_class": "bond_metric", **bm, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        # holding rows
        for h in r.get("top_holdings") or []:
            row = {**base, "record_class": "holding",
                   "holding_symbol": h.get("symbol"),
                   "holding_name":   h.get("name"),
                   "weight":         h.get("weight"),
                   **carry}
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
