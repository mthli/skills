#!/usr/bin/env python3
"""Fetch yfinance financial statements for one or more tickers and print as JSON.

See `financials.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed tickers carry an "error" field instead of data so a
single bad symbol does not poison the batch. Field schema lives in the
*_FIELDS / SUMMARY_* constants below.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, safe_str, with_retry,
)

import yfinance as yf


# Financials only meaningful for actual operating companies. ETFs / indexes
# / crypto / futures / FX have no income statement, balance sheet, or cash
# flow. Pre-check quote_type via fast_info (cheap — see SKILL.md latency
# table) and short-circuit non-equities so we skip the more expensive
# financials fetch entirely.
_EQUITY_QUOTE_TYPES = frozenset({"EQUITY"})

# CLI choices.
STATEMENTS = ("income", "balance", "cashflow")
PERIODS = ("annual", "quarterly", "ttm")

# Statement+period → yfinance Ticker property. (balance, ttm) is intentionally
# absent: balance sheets are point-in-time snapshots, "trailing twelve months"
# doesn't apply. CLI rejects --statement balance + --period ttm; --statement
# all + --period ttm short-circuits balance to [] with a top-level note.
_YF_PROP = {
    ("income",   "annual"):    "income_stmt",
    ("income",   "quarterly"): "quarterly_income_stmt",
    ("income",   "ttm"):       "ttm_income_stmt",
    ("balance",  "annual"):    "balance_sheet",
    ("balance",  "quarterly"): "quarterly_balance_sheet",
    ("cashflow", "annual"):    "cashflow",
    ("cashflow", "quarterly"): "quarterly_cashflow",
    ("cashflow", "ttm"):       "ttm_cashflow",
}

# Output JSON key for each statement (matches yfinance annual property names
# so users coming from the library find familiar keys).
_OUTPUT_KEY = {
    "income":   "income_stmt",
    "balance":  "balance_sheet",
    "cashflow": "cashflow",
}

_BALANCE_TTM_NOTE = (
    "balance sheet has no TTM concept (point-in-time snapshot); "
    "balance_sheet omitted from this period"
)

# (output_key, yahoo_line_item) — declarative schema, curated subset of the
# 30–70 line items yfinance exposes per statement. Iteration order is part
# of the public output contract; consumers rely on it. Add fields with one
# line. Yahoo line items NOT in our schema are dropped from the output —
# users who need a missing field should add it here rather than reading
# yfinance directly.
INCOME_FIELDS: list[tuple[str, str]] = [
    ("total_revenue",                     "Total Revenue"),
    ("cost_of_revenue",                   "Cost Of Revenue"),
    ("gross_profit",                      "Gross Profit"),
    ("research_and_development",          "Research And Development"),
    ("selling_general_and_administration", "Selling General And Administration"),
    ("operating_expense",                 "Operating Expense"),
    ("operating_income",                  "Operating Income"),
    ("ebitda",                            "EBITDA"),
    ("ebit",                              "EBIT"),
    ("interest_expense",                  "Interest Expense"),
    ("pretax_income",                     "Pretax Income"),
    ("tax_provision",                     "Tax Provision"),
    ("net_income",                        "Net Income"),
    ("diluted_eps",                       "Diluted EPS"),
    ("basic_eps",                         "Basic EPS"),
    ("diluted_average_shares",            "Diluted Average Shares"),
]

BALANCE_FIELDS: list[tuple[str, str]] = [
    ("total_assets",                                     "Total Assets"),
    ("current_assets",                                   "Current Assets"),
    ("cash_and_cash_equivalents",                        "Cash And Cash Equivalents"),
    ("cash_cash_equivalents_and_short_term_investments", "Cash Cash Equivalents And Short Term Investments"),
    ("receivables",                                      "Receivables"),
    ("inventory",                                        "Inventory"),
    ("net_ppe",                                          "Net PPE"),
    ("total_non_current_assets",                         "Total Non Current Assets"),
    ("current_liabilities",                              "Current Liabilities"),
    ("accounts_payable",                                 "Accounts Payable"),
    ("long_term_debt",                                   "Long Term Debt"),
    ("total_debt",                                       "Total Debt"),
    ("net_debt",                                         "Net Debt"),
    ("total_liabilities",                                "Total Liabilities Net Minority Interest"),
    ("retained_earnings",                                "Retained Earnings"),
    ("stockholders_equity",                              "Stockholders Equity"),
    ("working_capital",                                  "Working Capital"),
    ("ordinary_shares_number",                           "Ordinary Shares Number"),
]

CASHFLOW_FIELDS: list[tuple[str, str]] = [
    # `net_income` here is from the cashflow statement's reconciliation row
    # ("Net Income From Continuing Operations") — usually equal to income
    # statement net_income but can differ for companies with discontinued
    # operations. Kept under the same output key for cross-statement
    # readability; document the source in references/financials.md.
    ("net_income",                  "Net Income From Continuing Operations"),
    ("depreciation_and_amortization", "Depreciation And Amortization"),
    ("stock_based_compensation",    "Stock Based Compensation"),
    ("change_in_working_capital",   "Change In Working Capital"),
    # `operating_cashflow` / `free_cashflow` use the one-word `cashflow`
    # spelling to match info.py's `free_cashflow` / `operating_cashflow`
    # keys (which mirror Yahoo's camelCase `freeCashflow`). Yahoo's
    # DataFrame index uses three-word "Free Cash Flow" / "Operating Cash
    # Flow" but the public skill-wide convention is the one-word form.
    ("operating_cashflow",          "Operating Cash Flow"),
    ("capital_expenditure",         "Capital Expenditure"),
    ("investing_cashflow",          "Investing Cash Flow"),
    ("issuance_of_debt",            "Issuance Of Debt"),
    ("repayment_of_debt",           "Repayment Of Debt"),
    ("repurchase_of_capital_stock", "Repurchase Of Capital Stock"),
    ("cash_dividends_paid",         "Cash Dividends Paid"),
    ("financing_cashflow",          "Financing Cash Flow"),
    ("beginning_cash_position",     "Beginning Cash Position"),
    ("end_cash_position",           "End Cash Position"),
    ("free_cashflow",               "Free Cash Flow"),
]

# Yahoo line items deliberately NOT in the curated schema — listed here so
# future maintainers know what was considered and rejected, not just missed.
# To expose any of these, add a new (output_key, yahoo_label) entry to the
# corresponding *_FIELDS list above.
#
#   income (dropped):    Tax Effect Of Unusual Items, Tax Rate For Calcs,
#                        Normalized EBITDA, Normalized Income, Reconciled
#                        Cost Of Revenue, Reconciled Depreciation,
#                        Net Interest Income, Interest Income, Operating
#                        Revenue (≈ Total Revenue for most companies),
#                        Diluted NI Availto Com Stockholders, Net Income
#                        Common Stockholders (= Net Income for non-pref-stock
#                        companies), Net Income Including Noncontrolling
#                        Interests, Other Income Expense, etc. Reasoning:
#                        these are reconciliation / normalization rows
#                        useful for analysts but not for headline numbers.
#   balance (dropped):   Treasury Shares Number, Ordinary Shares Number is
#                        kept; Share Issued, Tangible Book Value, Invested
#                        Capital, Capital Stock, Common Stock, Net Tangible
#                        Assets, Capital Lease Obligations (kept inside
#                        Total Debt). Most are derivable from kept fields
#                        or are accounting-detail rather than headline.
#   cashflow (dropped):  Net Other Financing Charges, Net Other Investing
#                        Changes, Net Investment Purchase And Sale, Sale Of
#                        Investment, Purchase Of Investment, Net Business
#                        Purchase And Sale, Purchase Of Business, Net PPE
#                        Purchase And Sale, Purchase Of PPE, Cash Flow From
#                        Continuing * (= the *_cashflow rows we keep), Long
#                        Term Debt Issuance vs Long Term Debt Payments
#                        (kept aggregate Issuance/Repayment Of Debt instead),
#                        Net Common Stock Issuance, Common Stock Issuance,
#                        Common Stock Payments, Net Short Term Debt Issuance,
#                        Net Long Term Debt Issuance, Common Stock Dividend
#                        Paid (= Cash Dividends Paid for most companies).
#                        Reasoning: aggregate / less-granular versions are
#                        already in the schema.

_FIELDS_BY_STATEMENT = {
    "income":   INCOME_FIELDS,
    "balance":  BALANCE_FIELDS,
    "cashflow": CASHFLOW_FIELDS,
}


# Summary-mode projection — flat per-ticker dict for peer comparison.
# Drawn from the latest period of each statement; growth fields computed
# from latest vs. prev period (period 4 back for quarterly when available
# = YoY same-quarter, period 1 back otherwise).
SUMMARY_BASE_KEYS = ("symbol", "quote_type", "currency", "period",
                     "period_end", "prev_period_end")

# (summary_key, statement, statement_field) — pulled from the latest period
# of the given statement.
SUMMARY_HEADLINES: list[tuple[str, str, str]] = [
    ("total_revenue",             "income",   "total_revenue"),
    ("gross_profit",              "income",   "gross_profit"),
    ("operating_income",          "income",   "operating_income"),
    ("net_income",                "income",   "net_income"),
    ("ebitda",                    "income",   "ebitda"),
    ("diluted_eps",               "income",   "diluted_eps"),
    ("total_assets",              "balance",  "total_assets"),
    ("total_liabilities",         "balance",  "total_liabilities"),
    ("stockholders_equity",       "balance",  "stockholders_equity"),
    ("cash_and_cash_equivalents", "balance",  "cash_and_cash_equivalents"),
    ("total_debt",                "balance",  "total_debt"),
    ("operating_cashflow",        "cashflow", "operating_cashflow"),
    ("free_cashflow",             "cashflow", "free_cashflow"),
    ("capital_expenditure",       "cashflow", "capital_expenditure"),
]

# (summary_key, statement, statement_field) — period-over-period growth as a
# fraction. Fraction (matching info.py's `revenue_growth`), not percent: a
# 16% jump is `0.16`. None when prev is missing/zero/negative.
#
# `_yoy` suffix disambiguates from `info.fundamentals.revenue_growth` (which
# is Yahoo's TTM-based revenueGrowth, not period-bounded) — both fields can
# legitimately appear on the same ticker with different values; the suffix
# tells callers this one is computed period-over-period from the financials
# fetch. For quarterly with <5 quarters available the fallback is
# sequential QoQ (1 period back); the suffix is still `_yoy` even in that
# fallback case — check `prev_period_end` to know the actual window.
SUMMARY_GROWTH: list[tuple[str, str, str]] = [
    ("revenue_growth_yoy",        "income",   "total_revenue"),
    ("net_income_growth_yoy",     "income",   "net_income"),
    ("free_cashflow_growth_yoy",  "cashflow", "free_cashflow"),
]


def _validate_summary_schema() -> None:
    """Module-load sanity: every (statement, field) referenced in
    SUMMARY_HEADLINES / SUMMARY_GROWTH must exist in the corresponding
    *_FIELDS schema. Catches typos and renames before any Yahoo round-trip."""
    for src in (SUMMARY_HEADLINES, SUMMARY_GROWTH):
        for out_key, statement, field in src:
            fields = _FIELDS_BY_STATEMENT.get(statement)
            if fields is None:
                raise RuntimeError(
                    f"summary refers to unknown statement {statement!r} "
                    f"(out_key={out_key!r})")
            if not any(out == field for out, _src in fields):
                raise RuntimeError(
                    f"summary refers to {statement}.{field} which is not in "
                    f"{statement.upper()}_FIELDS (out_key={out_key!r})")


_validate_summary_schema()


def _trading_currency(symbol: str) -> tuple[str | None, int]:
    """Pull trading currency via fast_info, with retry on transient errors.

    Returns (currency_or_None, attempts). Used as the soft-fallback whenever
    the reporting-currency path fails. Wrapped in `with_retry` so a single
    transient 429 / network blip on fast_info doesn't silently null out the
    currency field — without this, the soft-fallback was strictly weaker
    than the primary path. The result is also normalized through
    `_normalize_currency` so Yahoo's "None" / "n/a" string sentinels
    collapse to None like the info-derived paths.

    KeyError / AttributeError (terminal — Yahoo really doesn't have currency
    for this ticker) get classified as `unknown` by classify_error and
    short-circuit; `with_retry` never retries those, so they cost only one
    attempt regardless.
    """
    def _f():
        return yf.Ticker(symbol).fast_info["currency"]
    res, kind, attempts = with_retry(_f)
    return (_normalize_currency(res) if not kind else None), attempts


# Note prefixes for the soft-fallback paths in `_meta`.
#
# IMPORTANT framing: yfinance's financial-statement endpoints return values
# in the company's actual reporting currency (pulled from official filings;
# Yahoo doesn't FX-convert). What can be wrong on a fallback path is the
# `currency` *label* we attach — never the values themselves. The notes are
# worded so a reader understands "the label may be misleading" rather than
# "the numbers may be in either currency, you can't tell" (which would
# undermine confidence in correct data). All four notes contain the
# substring "trading currency" so consumers can detect any fallback with
# `if "trading currency" in note`.
_NOTE_INFO_FAILED = (
    "reporting_currency lookup failed ({reason}); `currency` field falls "
    "back to trading currency. yfinance statement values are in the "
    "actual reporting currency — the label may not match the values' "
    "true denomination for ADRs / cross-listed names."
)
_NOTE_FINCUR_MISSING = (
    "info[financialCurrency] missing; `currency` field falls back to "
    "trading currency from info. yfinance statement values are in the "
    "actual reporting currency — the label may not match the values' "
    "true denomination for ADRs / cross-listed names."
)
_NOTE_BOTH_MISSING = (
    "info[financialCurrency] and info[currency] both missing; `currency` "
    "field falls back to fast_info trading currency. yfinance statement "
    "values are in the actual reporting currency — the label may not "
    "match the values' true denomination for ADRs / cross-listed names."
)
_NOTE_ALL_UNAVAILABLE = (
    "info[financialCurrency], info[currency], and fast_info trading "
    "currency all unavailable; `currency` field is null. yfinance "
    "statement values are still in the actual reporting currency, but "
    "no label could be determined — verify externally for cross-listed names."
)


# Yahoo no-coverage sentinels for currency fields. Same set as info.py's
# `_NO_COVERAGE_SENTINELS` (intentionally duplicated rather than imported
# across sibling scripts). Yahoo occasionally returns one of these as a
# string instead of JSON null; without filtering, "None" would propagate
# as a literal currency code.
_NO_CURRENCY_SENTINELS = frozenset({"none", "n/a", "na", "unknown", "—", "-"})


def _normalize_currency(v):
    """Like `safe_str` but also collapses Yahoo's no-coverage string sentinels
    to None. Used at every currency lookup point so a stray "None" / "n/a"
    from Yahoo doesn't masquerade as a real currency code."""
    s = safe_str(v)
    if s is None:
        return None
    return None if s.lower() in _NO_CURRENCY_SENTINELS else s


def _meta(symbol: str) -> tuple[str | None, str | None, str | None,
                                str | None, int]:
    """Two-stage meta lookup: quote_type via fast_info, then reporting
    currency via info (equities only).

    Returns (quote_type, currency, note, error_kind, attempts).

    - `currency` is the **reporting currency** for equities (the currency
      used in the financial statements — `info["financialCurrency"]`),
      with soft fallback to fast_info trading currency when info fails
      OR exists-but-is-missing-the-field. For non-equities, currency is
      whatever fast_info returns (financials are short-circuited anyway).
    - `note` is populated on every soft-fallback path with text containing
      the substring "trading currency" so consumers can detect any
      "the value you're seeing is the trading currency, not reporting"
      situation by simple string match. Three distinct fallback paths,
      three different reasons (see `_NOTE_*` constants):
        a) info() entirely failed (transient 429 / network)
        b) info() succeeded but `financialCurrency` field is absent/null
           (rare but real — Yahoo coverage gap, more common for thinly-
           covered foreign listings)
        c) info() succeeded but BOTH `financialCurrency` and `currency`
           are absent (very rare — degraded info payload)
    - `error_kind` is None on success; populated only when the cheap
      quote_type check itself failed (bogus / delisted / rate-limited).
      `info` failures are soft (handled via fallback + note) and don't
      surface as `error_kind`.

    Latency: see the cost / latency table in `SKILL.md` (single source of
    truth for per-stage timing across all five modes in this skill).
    """
    def _qt():
        try:
            return yf.Ticker(symbol).fast_info["quoteType"]
        except AttributeError as exc:
            # yfinance internal bug for delisted/bogus tickers — reraise so
            # classify_error sees a not_found-classifiable string.
            raise RuntimeError(f"not found: {exc}") from exc

    qt_res, qt_kind, qt_attempts = with_retry(_qt)
    if qt_kind:
        return None, None, None, qt_kind, qt_attempts
    qt = safe_str(qt_res)
    if qt is None:
        return None, None, None, "not_found", qt_attempts

    # Non-equity: skip the expensive info() round-trip. Trading currency is
    # adequate (financials get short-circuited anyway). _trading_currency
    # already normalizes through `_normalize_currency`.
    if qt not in _EQUITY_QUOTE_TYPES:
        cur, ta = _trading_currency(symbol)
        return qt, cur, None, None, max(qt_attempts, ta)

    # Equity: reach for the reporting currency. ADRs (PBR, BABA, TM) trade
    # in USD but report in BRL/CNY/JPY. Even direct foreign listings can
    # diverge — e.g., 0700.HK trades in HKD but Tencent reports in CNY.
    # Without `financialCurrency`, every monetary field would be wrongly
    # labelled with the trading currency.
    info_dict, info_kind, info_attempts = with_retry(
        lambda: yf.Ticker(symbol).info or {})
    attempts = max(qt_attempts, info_attempts)

    # Path (a): info() entirely failed → fast_info fallback + note.
    if info_kind or not info_dict:
        cur, ta = _trading_currency(symbol)
        attempts = max(attempts, ta)
        return (qt, cur,
                _NOTE_INFO_FAILED.format(reason=info_kind or "empty info"),
                None, attempts)

    # Primary: info[financialCurrency] populated → use it, no note needed.
    # _normalize_currency filters Yahoo's "None"/"n/a" string sentinels.
    fc = _normalize_currency(info_dict.get("financialCurrency"))
    if fc:
        return qt, fc, None, None, attempts

    # Path (b): info ok, financialCurrency missing → info[currency] + note.
    info_cur = _normalize_currency(info_dict.get("currency"))
    if info_cur:
        return qt, info_cur, _NOTE_FINCUR_MISSING, None, attempts

    # Path (c): info ok but both fields empty → fast_info + note. If even
    # fast_info comes back empty (rare; Yahoo really has nothing), fall to
    # _NOTE_ALL_UNAVAILABLE so the note doesn't lie about which source we
    # ended up using.
    cur, ta = _trading_currency(symbol)
    attempts = max(attempts, ta)
    note = _NOTE_BOTH_MISSING if cur is not None else _NOTE_ALL_UNAVAILABLE
    return qt, cur, note, None, attempts


def _df_to_periods(df: Any, fields: list[tuple[str, str]],
                   limit: int | None) -> list[dict]:
    """Convert a yfinance financials DataFrame to a list of period dicts.

    DataFrame layout (yfinance 1.3.x):
      - index = line item names (CamelCase strings)
      - columns = pandas Timestamps (period end dates, newest first)
      - values = float64, NaN for missing

    Output: one dict per column, ordered newest-first to match yfinance's
    natural column order. Each dict has `period_end` (YYYY-MM-DD) plus all
    fields from the schema (null when the line item is missing for that
    period). All numeric values are raw floats (no rounding) per skill
    convention; consumers format for display.
    """
    if df is None or getattr(df, "empty", True):
        return []
    periods: list[dict] = []
    for col in df.columns:
        period_end = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
        row: dict = {"period_end": period_end}
        for out_key, src_key in fields:
            if src_key in df.index:
                row[out_key] = safe_float(df.loc[src_key, col])
            else:
                row[out_key] = None
        periods.append(row)
    if limit is not None:
        periods = periods[:limit]
    return periods


def _growth(latest: float | None, prev: float | None) -> float | None:
    """YoY/QoQ growth as a fraction. None when either side is missing or
    prev is non-positive (negative-prev produces sign-confused ratios; we
    surface null and let the caller decide rather than mislead with a
    misleading number)."""
    if latest is None or prev is None or prev <= 0:
        return None
    return (latest - prev) / prev


def _pick_prev_index(period: str, periods_count: int) -> int | None:
    """Index in the period list to use as 'prev' for growth comparisons.

    Annual: 1 (year-over-year).
    Quarterly: 4 if available (YoY same-quarter, the analyst convention),
               else 1 (sequential QoQ — note seasonality in references).
    TTM: no prev (single period).
    Returns None when no usable prev exists.
    """
    if period == "ttm":
        return None
    if period == "quarterly" and periods_count > 4:
        return 4
    if periods_count > 1:
        return 1
    return None


def _summarize(full: dict) -> dict:
    """Project the full financials dict into a flat headline-numbers dict.

    Latest period of each statement → SUMMARY_HEADLINES.
    Latest vs prev (per `_pick_prev_index`) → SUMMARY_GROWTH.
    Non-equity short-circuit and error dicts pass through with nulls.
    """
    if "error" in full:
        out = {"symbol": full["symbol"]}
        for key in RESULT_META:
            if key in full:
                out[key] = full[key]
        return out

    base = {
        "symbol":     full.get("symbol"),
        "quote_type": full.get("quote_type"),
        "currency":   full.get("currency"),
        "period":     full.get("period"),
    }

    # Non-equity short-circuit pass-through: preserve note + null all fields.
    if "note" in full and not any(full.get(_OUTPUT_KEY[s]) for s in STATEMENTS):
        base["note"] = full["note"]
        base["period_end"] = None
        base["prev_period_end"] = None
        for key, *_ in SUMMARY_HEADLINES:
            base[key] = None
        for key, *_ in SUMMARY_GROWTH:
            base[key] = None
        for key in RESULT_META:
            if key in full:
                base[key] = full[key]
        return base

    # Determine latest + prev period end across statements. We use income's
    # period range as the canonical reference (income is required for any
    # equity with financial coverage). If income is missing, fall back to
    # whichever statement has data — keeps summary working for partial
    # responses (e.g., TTM where only income+cashflow exist, or partial
    # fetch failures where one statement errored).
    period = full.get("period")
    period_lists = {s: full.get(_OUTPUT_KEY[s]) or [] for s in STATEMENTS}
    reference = (period_lists["income"] or period_lists["cashflow"]
                 or period_lists["balance"])
    latest_end = reference[0]["period_end"] if reference else None
    prev_idx = _pick_prev_index(period, len(reference)) if reference else None
    prev_end = reference[prev_idx]["period_end"] if prev_idx is not None else None
    base["period_end"] = latest_end
    base["prev_period_end"] = prev_end

    def _latest(statement: str, field: str) -> float | None:
        rows = period_lists[statement]
        return rows[0].get(field) if rows else None

    def _prev(statement: str, field: str) -> float | None:
        rows = period_lists[statement]
        if prev_idx is None or prev_idx >= len(rows):
            return None
        return rows[prev_idx].get(field)

    for out_key, statement, field in SUMMARY_HEADLINES:
        base[out_key] = _latest(statement, field)
    for out_key, statement, field in SUMMARY_GROWTH:
        base[out_key] = _growth(_latest(statement, field),
                                _prev(statement, field))

    # `note` for partial-period cases (balance unavailable for TTM, partial
    # fetch failures, currency soft-fallback) propagates so consumers can
    # flag mixed-data summaries.
    if "note" in full:
        base["note"] = full["note"]
    # `partial_errors` flows through too — summary callers may want to know
    # which statements were skipped due to transient failures.
    if "partial_errors" in full:
        base["partial_errors"] = full["partial_errors"]
    for key in RESULT_META:
        if key in full:
            base[key] = full[key]
    return base


# Worst-error precedence — used to pick a single error_kind when ALL
# requested statements failed. rate_limit beats network beats unknown beats
# not_found because it's the most actionable (caller should back off and
# retry); not_found is the least actionable and the weakest signal.
_ERROR_KIND_PRIORITY = ("rate_limit", "network", "unknown", "not_found")


def fetch(symbol: str, *, statements: tuple[str, ...] = STATEMENTS,
          period: str = "annual", limit: int | None = None) -> dict:
    """Fetch financial statements for one ticker.

    Flow (per-stage costs in SKILL.md cost / latency table):
      1. Cheap quote_type pre-check via fast_info. Bogus / delisted tickers
         exit here with `error_kind=not_found`. Non-equity tickers (ETF /
         INDEX / etc.) short-circuit with empty statement lists + `note`.
      2. For equities, additional `info` fetch to get the **reporting
         currency** (`info["financialCurrency"]`); soft-falls-back to
         trading currency with a `note` when info fails.
      3. For each requested (statement, period), fetch the matching yfinance
         property and convert to a list of period dicts. Per-statement errors
         are collected into `partial_errors` rather than failing the whole
         ticker — a transient 429 on balance shouldn't drop a successful
         income+cashflow fetch.

    Returns one of:
      - equity success / partial-success:  {symbol, quote_type, currency,
                                            period, income_stmt: [...],
                                            balance_sheet: [...],
                                            cashflow: [...],
                                            partial_errors?, note?,
                                            attempts?}
      - non-equity short-circuit:          {symbol, quote_type, currency,
                                            period, note, income_stmt: [],
                                            balance_sheet: [], cashflow: []}
      - all-fetched-statements-failed:     {symbol, error, error_kind,
                                            attempts}
      - bogus / delisted / not found:      {symbol, error, error_kind,
                                            attempts}
    """
    qt, cur, meta_note, qt_err, qt_attempts = _meta(symbol)
    if qt_err:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({qt_err}, after {qt_attempts} attempt(s))",
            "error_kind": qt_err,
            "attempts": qt_attempts,
        }
    if qt is None:
        return {
            "symbol": symbol,
            "error": "no quote_type returned (delisted, wrong suffix, or rate-limited)",
            "error_kind": "not_found",
            "attempts": qt_attempts,
        }
    if qt not in _EQUITY_QUOTE_TYPES:
        out: dict = {
            "symbol": symbol,
            "quote_type": qt,
            "currency": cur,
            "period": period,
            "note": f"financials only meaningful for equities; this is {qt}",
        }
        for s in statements:
            out[_OUTPUT_KEY[s]] = []
        if qt_attempts > 1:
            out["attempts"] = qt_attempts
        return out

    out = {
        "symbol": symbol,
        "quote_type": qt,
        "currency": cur,
        "period": period,
    }

    ticker = yf.Ticker(symbol)
    max_attempts = qt_attempts
    notes: list[str] = []
    if meta_note:
        notes.append(meta_note)

    # Tracking for the partial-success error model. `attempted` counts
    # statements actually dispatched to Yahoo (not balance+ttm skips);
    # `any_data` flips True the first time we get a non-empty period list;
    # `statement_errors` accumulates per-statement transient failures so
    # they surface alongside the data that did succeed.
    attempted = 0
    any_data = False
    statement_errors: dict[str, dict] = {}

    for s in statements:
        if s == "balance" and period == "ttm":
            out[_OUTPUT_KEY[s]] = []
            notes.append(_BALANCE_TTM_NOTE)
            continue
        attempted += 1
        prop = _YF_PROP[(s, period)]

        def _f(prop=prop):
            return getattr(ticker, prop)

        df, err_kind, attempts = with_retry(_f)
        max_attempts = max(max_attempts, attempts)
        if err_kind:
            out[_OUTPUT_KEY[s]] = []
            statement_errors[_OUTPUT_KEY[s]] = {
                "error_kind": err_kind, "attempts": attempts,
            }
            continue
        periods = _df_to_periods(df, _FIELDS_BY_STATEMENT[s], limit)
        if periods:
            any_data = True
        out[_OUTPUT_KEY[s]] = periods

    # Decide whether this is a ticker-level failure or a partial success.
    # If at least one statement was actually attempted and EVERY one of
    # those failed transient-error-style, escalate to a top-level error
    # (the caller should back off the whole ticker rather than read empty
    # data + scattered partial_errors). Otherwise, even a single successful
    # fetch keeps the result alive.
    if attempted > 0 and not any_data:
        if statement_errors and len(statement_errors) == attempted:
            kinds = [e["error_kind"] for e in statement_errors.values()]
            worst = next((k for k in _ERROR_KIND_PRIORITY if k in kinds),
                         kinds[0])
            return {
                "symbol": symbol,
                "error": (f"fetch failed on all {attempted} statement(s) "
                          f"({worst}, after up to {max_attempts} attempt(s))"),
                "error_kind": worst,
                "attempts": max_attempts,
            }
        if not statement_errors:
            # All attempted fetches succeeded but returned empty data — low
            # coverage, recent IPO, or a financial-services name with
            # non-standard schema. Treat as not_found rather than emitting
            # an empty schema with no signal.
            return {
                "symbol": symbol,
                "error": "no financials returned (low coverage, recent IPO, or rate-limited)",
                "error_kind": "not_found",
                "attempts": max_attempts,
            }
        # else: mix of empty + errored — fall through to partial output

    if statement_errors:
        out["partial_errors"] = statement_errors
        notes.append("partial fetch failure on: "
                     + ", ".join(sorted(statement_errors.keys())))
    if notes:
        out["note"] = "; ".join(notes)
    if max_attempts > 1:
        out["attempts"] = max_attempts
    return out


def main() -> None:
    statement_field_count = sum(len(v) for v in _FIELDS_BY_STATEMENT.values())
    summary_field_count = (
        len(SUMMARY_BASE_KEYS) + len(SUMMARY_HEADLINES) + len(SUMMARY_GROWTH)
    )

    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch annual / quarterly / TTM financial statements (income, balance,\n"
            "cashflow) from Yahoo Finance.\n\n"
            "Two output modes:\n"
            f"  default     full per-statement period lists; ~{statement_field_count} curated\n"
            "              fields total across the three statements\n"
            f"  --summary   flat {summary_field_count}-field dict per ticker (latest-period headline\n"
            "              numbers + period-over-period growth) for peer comparison\n\n"
            "Equity-only: ETFs, indexes, crypto, FX, futures get empty statement\n"
            "lists with a `note` (not an error) — the quote_type pre-check skips\n"
            "the financials fetch for them.\n\n"
            "Balance sheet has no TTM concept; --statement balance --period ttm is\n"
            "rejected, --statement all --period ttm omits balance with a note.\n\n"
            "`currency` in the output is the REPORTING currency (the currency the\n"
            "statements are denominated in — e.g., BRL for PBR, JPY for TM,\n"
            "CNY for 0700.HK). May differ from the ticker's trading currency for\n"
            "ADRs and cross-listed names; see references/financials.md."
        ),
        epilog=(
            "Examples:\n"
            "  financials.py AAPL                                    # all 3 statements, annual\n"
            "  financials.py --period quarterly AAPL                 # all 3, quarterly\n"
            "  financials.py --statement income AAPL                 # just income, annual\n"
            "  financials.py --period ttm --statement income AAPL    # TTM income\n"
            "  financials.py --summary AAPL MSFT GOOGL               # peer headline + YoY growth\n"
            "  financials.py --summary --period quarterly AAPL MSFT  # quarterly summary (YoY)\n"
            "  financials.py --limit 3 AAPL                          # only 3 most recent periods\n"
            "  financials.py --summary --format csv AAPL MSFT GOOGL  # peer CSV table\n"
            "\n"
            "See references/financials.md for the full schema, growth-fraction\n"
            "convention, currency notes, and presentation guidance."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--statement", default="all",
                    choices=("all",) + STATEMENTS,
                    help="Which statement(s) to fetch. `all` (default) returns "
                         "income, balance, and cashflow.")
    ap.add_argument("--period", default="annual", choices=PERIODS,
                    help="Reporting period. annual (default), quarterly, "
                         "or ttm (trailing twelve months — income + cashflow only).")
    ap.add_argument("--limit", type=int, metavar="N", default=None,
                    help="Truncate each statement to the N most recent periods. "
                         "Default keeps all periods yfinance returns "
                         "(~5 annual, ~5–7 quarterly, 1 ttm).")
    ap.add_argument("--summary", action="store_true",
                    help=f"Project to flat {summary_field_count}-field dict for peer comparison.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one JSON record per line; "
                         "csv = one row per ticker — only valid with --summary "
                         "(default mode is nested per-statement period lists "
                         "and not CSV-friendly).")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive. Equity-only "
                         "(ETFs/indexes/crypto get empty lists + note).")
    args = ap.parse_args()

    if args.statement == "balance" and args.period == "ttm":
        ap.error("--statement balance is not available with --period ttm "
                 "(balance sheets are point-in-time snapshots; TTM doesn't apply)")

    if args.format == "csv" and not args.summary:
        ap.error("--format csv requires --summary (default mode has nested "
                 "per-statement period lists that don't flatten to CSV cleanly)")

    if args.limit is not None and args.limit < 1:
        ap.error(f"--limit must be >= 1, got {args.limit}")

    statements: tuple[str, ...] = (
        STATEMENTS if args.statement == "all" else (args.statement,)
    )

    results = [
        fetch(s.strip().upper(), statements=statements,
              period=args.period, limit=args.limit)
        for s in args.symbols if s.strip()
    ]
    if args.summary:
        results = [_summarize(r) for r in results]
    _emit(results, args.format)


def _emit(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # csv (summary mode only — main() blocks default + csv).
    import csv as _csv
    cols = list(SUMMARY_BASE_KEYS) + ["note"] \
        + [k for k, *_ in SUMMARY_HEADLINES] \
        + [k for k, *_ in SUMMARY_GROWTH] \
        + list(RESULT_META)
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
