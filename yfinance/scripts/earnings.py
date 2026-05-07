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
    RESULT_META, emit_json_or_ndjson, safe_float, safe_int, safe_str, with_retry,
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
_SUMMARY_CSV_COLS = (_SUMMARY_BASE_KEYS
                    + ("note", "coverage_note")
                    + _SUMMARY_KEYS
                    + RESULT_META)

# `--estimates` schema. Yahoo's analyst panel surfaces five DataFrames, all
# indexed by period code ('0q' = current quarter / upcoming report; '+1q' =
# next quarter; '0y' = current fiscal year; '+1y' = next fiscal year):
#
#   earnings_estimate   per-period EPS consensus  (avg/low/high + analysts)
#   revenue_estimate    per-period revenue consensus
#   eps_trend           how the EPS consensus has drifted over the last 90 days
#   eps_revisions       count of upward / downward EPS revisions in 7d/30d
#   growth_estimates    indexTrend (sector benchmark) + LTG (long-term row)
#
# All five merge per period into one flat dict. Each fetch is independent
# and retried separately — partial failures null out only the affected
# columns. Empirical: # analysts can differ between EPS and revenue panels
# (rev usually narrower coverage), so each side carries its own count.
#
# Currency is SPLIT into eps_currency / revenue_currency: for ADRs (TM, PBR)
# Yahoo reports per-share EPS in trading currency (USD) but revenue in the
# home reporting currency (JPY/BRL). A unified `currency` field would lie.
# eps_trend / eps_revisions also carry currency that empirically matches the
# EPS panel — fold into eps_currency. growth_estimates has no currency col.
_ESTIMATE_PERIODS = ("0q", "+1q", "0y", "+1y")
_ESTIMATE_KEYS = (
    "period",
    # consensus EPS (currency = eps_currency)
    "eps_currency",
    "eps_avg", "eps_low", "eps_high",
    "eps_year_ago", "eps_growth", "eps_analysts",
    # consensus revenue (currency = revenue_currency; may differ from eps_currency for ADRs)
    "revenue_currency",
    "revenue_avg", "revenue_low", "revenue_high",
    "revenue_year_ago", "revenue_growth", "revenue_analysts",
    # EPS estimate trend — same denomination as eps_*, lets you see how
    # the consensus has moved (current vs 7/30/60/90 days ago).
    "eps_trend_current",
    "eps_trend_7d_ago", "eps_trend_30d_ago",
    "eps_trend_60d_ago", "eps_trend_90d_ago",
    # EPS revisions — counts (not currency-denominated). Up minus down ≈
    # analyst-momentum signal.
    "eps_revisions_up_7d", "eps_revisions_up_30d",
    "eps_revisions_down_7d", "eps_revisions_down_30d",
    # Sector / index benchmark growth for the same period — pair with
    # eps_growth to see "this stock vs its sector" (growth_estimates table's
    # stockTrend column is empirically identical to earnings_estimate.growth,
    # so we drop it; indexTrend is the value-add field).
    "index_growth",
)

# Top-level (per-ticker, not per-period) — long-term growth comes from
# growth_estimates' `LTG` row. Structurally not a quarterly/annual period.
# Often null for stocks that don't have analyst LTG coverage.
_LONG_TERM_GROWTH_KEY = "long_term_growth"

# Summary-mode flat projection of 0q (current quarter, the about-to-report
# one). Lifts the most-cited consensus numbers to top-level fields so peer-
# comparison tables work without unpacking nested lists. Suffixed `_yoy` on
# growth fields to match the financials --summary convention.
_CONSENSUS_SUMMARY_KEYS = (
    "consensus_eps_avg",
    "consensus_eps_low", "consensus_eps_high",
    "consensus_eps_growth_yoy",
    "consensus_eps_analysts",
    "consensus_eps_currency",
    "consensus_revenue_avg",
    "consensus_revenue_growth_yoy",
    "consensus_revenue_analysts",
    "consensus_revenue_currency",
)


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


def _assert_note_contract(out: dict) -> None:
    """Runtime invariants on the two note fields. Non-overlapping
    semantics is what lets `_summarize` use the simple `if "note" in
    full:` check to detect the non-equity short-circuit branch — if a
    future bug ever lets `note` appear on an equity, or both note fields
    appear together, summary would silently misroute the result. Fail
    fast here at the fetch() boundary instead.

      - `note` is reserved for the non-equity short-circuit
        (quote_type ∈ ETF / INDEX / CRYPTOCURRENCY / FUTURE / CURRENCY).
      - `coverage_note` is reserved for the IPO fall-through
        (equity with empty earnings_dates but populated estimates).
      - The two are mutually exclusive — the IPO path is only reached
        on equities, which never run through the non-equity branch.

    Raises RuntimeError on violation, matching the convention in
    `_validate_summary_keys` for similar contract-violation messages.
    Embedding only the symbol (not the full `out` dict) keeps the error
    short — the full dict can be many KB once estimates are populated,
    and the symbol is enough to find the offending call site. If
    `symbol` is missing for some reason (the contract assumes fetch()
    sets it; if the helper is ever called from elsewhere it might not
    be), fall back to a truncated repr of `out` so the error stays
    diagnostic instead of degrading to `(symbol=None)`.
    """
    sym = out.get("symbol")
    where = (f"symbol={sym!r}" if sym is not None
             else f"out={repr(out)[:100]!s}")
    if "note" in out and "coverage_note" in out:
        raise RuntimeError(
            "earnings.fetch invariant violated: `note` and `coverage_note` "
            f"are mutually exclusive ({where}); non-equity short-circuit "
            "and IPO fall-through cannot coexist")
    if "note" in out and out.get("quote_type") == "EQUITY":
        raise RuntimeError(
            "earnings.fetch invariant violated: `note` set on an EQUITY "
            f"result ({where}); equity fall-through should use "
            "`coverage_note` instead — `note` is reserved for non-equity "
            "short-circuit only")


def _coalesce_present(row, *keys):
    """Return `row.get(k)` for the first `k` in `keys` whose `.get()` is
    not None. Used to defend against upstream column-name drift — e.g.
    Yahoo's `downLast7Days` vs `downLast7days` typo where any future
    "fix" would silently null only one field. `pd.Series.get(missing)`
    returns None; `.get(present_with_NaN)` returns the NaN (which
    safe_int / safe_float handle separately), so the first-non-None
    walk correctly skips missing keys without skipping zeros or NaN
    values that the caller wants to surface.
    """
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return None


def _fetch_estimates(symbol: str) -> tuple[list, dict | None, int, str | None]:
    """Fetch the full Yahoo analyst panel: consensus EPS, consensus revenue,
    EPS estimate trend (90-day drift), EPS revisions counts, and sector /
    index growth comparison + LTG.

    Five Yahoo property reads, each retried independently. Reuses one
    Ticker object so they share yfinance's per-instance session / cookie
    setup rather than re-bootstrapping per property — exact savings
    aren't measured but at minimum avoids redundant init overhead.

    Soft-failure model: if BOTH consensus sources (`earnings_estimate` AND
    `revenue_estimate`) fail, returns empty rows + estimates_error — these
    are the headline numbers. Trend / revisions / growth failures null out
    only their own columns; treated as enrichment, not core. Rationale:
    a transient 429 on `eps_revisions` shouldn't poison a successful
    consensus fetch — the user can still answer the main "what's expected"
    question without revision counts.

    Forward-compat: emits every period yfinance returns, not just the
    canonical _ESTIMATE_PERIODS set. If Yahoo adds `+2q` someday, it
    appears as a row with the raw period string; consumers iterating
    `period` already handle it.

    Returns:
      (rows, long_term_growth, max_attempts, estimates_error)
        rows: list[dict] keyed by `_ESTIMATE_KEYS`. One entry per period
              (4 typical, more if upstream extends). Empty when both
              consensus sources fail.
        long_term_growth: dict {"stock": float|null, "index": float|null}
                          from growth_estimates' LTG row, or None when no
                          LTG data.
        max_attempts: max retries across all five fetches.
        estimates_error: kind of the consensus-side failure when both
                         eps_estimate and revenue_estimate failed; None
                         otherwise (including when only enrichment
                         sources failed).
    """
    ticker = yf.Ticker(symbol)
    eps_df, eps_kind, eps_attempts = with_retry(lambda: ticker.earnings_estimate)
    rev_df, rev_kind, rev_attempts = with_retry(lambda: ticker.revenue_estimate)
    trend_df, _, trend_attempts = with_retry(lambda: ticker.eps_trend)
    revisions_df, _, revisions_attempts = with_retry(lambda: ticker.eps_revisions)
    growth_df, _, growth_attempts = with_retry(lambda: ticker.growth_estimates)
    max_attempts = max(eps_attempts, rev_attempts, trend_attempts,
                       revisions_attempts, growth_attempts)

    eps_ok = eps_kind is None and eps_df is not None and not eps_df.empty
    rev_ok = rev_kind is None and rev_df is not None and not rev_df.empty
    trend_ok = trend_df is not None and not trend_df.empty
    revisions_ok = revisions_df is not None and not revisions_df.empty
    growth_ok = growth_df is not None and not growth_df.empty

    if not eps_ok and not rev_ok:
        # Two failure modes hide behind "not ok": (a) the call raised and
        # with_retry classified the exception (eps_kind / rev_kind set), or
        # (b) the call succeeded but returned an empty DataFrame (kinds
        # are None — Yahoo gave us silence for this ticker). Map (b) to
        # `not_found` rather than `unknown`: empty-but-no-error means
        # "no data exists," which is semantically a not_found, not a
        # mystery failure. (a) preserves the classified kind. Prefer
        # EPS-side over revenue's when both raised.
        if eps_kind is None and rev_kind is None:
            return [], None, max_attempts, "not_found"
        return [], None, max_attempts, (eps_kind or rev_kind)

    # Forward-compat: collect every period present across the panels (excluding
    # LTG, which lives in growth_estimates as a separate row and is surfaced
    # at top level). Canonical periods first in their documented order; any
    # future additions trail in insertion order.
    seen = []
    for df, ok in ((eps_df, eps_ok), (rev_df, rev_ok), (trend_df, trend_ok),
                   (revisions_df, revisions_ok), (growth_df, growth_ok)):
        if not ok:
            continue
        for p in df.index:
            if p == "LTG":
                continue
            if p not in seen:
                seen.append(p)
    ordered = ([p for p in _ESTIMATE_PERIODS if p in seen]
               + [p for p in seen if p not in _ESTIMATE_PERIODS])

    rows = []
    for period in ordered:
        # `.loc[period]` raises KeyError if missing — guard each side's index
        # so partial-coverage tickers don't blow up the whole batch.
        eps_row = eps_df.loc[period] if eps_ok and period in eps_df.index else None
        rev_row = rev_df.loc[period] if rev_ok and period in rev_df.index else None
        trend_row = trend_df.loc[period] if trend_ok and period in trend_df.index else None
        rev_count_row = revisions_df.loc[period] if revisions_ok and period in revisions_df.index else None
        growth_row = growth_df.loc[period] if growth_ok and period in growth_df.index else None

        # EPS currency: prefer earnings_estimate, fall back to eps_trend
        # (empirically same value for ADRs — both report the per-share
        # trading currency, e.g. USD for TM). Revenue currency is its own
        # source — for ADRs this is JPY/BRL/etc. while EPS stays USD.
        eps_currency = None
        if eps_row is not None:
            eps_currency = safe_str(eps_row.get("currency"))
        if eps_currency is None and trend_row is not None:
            eps_currency = safe_str(trend_row.get("currency"))
        revenue_currency = safe_str(rev_row.get("currency")) if rev_row is not None else None

        rows.append({
            "period": period,
            "eps_currency": eps_currency,
            "eps_avg":      safe_float(eps_row.get("avg")) if eps_row is not None else None,
            "eps_low":      safe_float(eps_row.get("low")) if eps_row is not None else None,
            "eps_high":     safe_float(eps_row.get("high")) if eps_row is not None else None,
            "eps_year_ago": safe_float(eps_row.get("yearAgoEps")) if eps_row is not None else None,
            "eps_growth":   safe_float(eps_row.get("growth")) if eps_row is not None else None,
            "eps_analysts": safe_int(eps_row.get("numberOfAnalysts")) if eps_row is not None else None,
            "revenue_currency": revenue_currency,
            "revenue_avg":      safe_float(rev_row.get("avg")) if rev_row is not None else None,
            "revenue_low":      safe_float(rev_row.get("low")) if rev_row is not None else None,
            "revenue_high":     safe_float(rev_row.get("high")) if rev_row is not None else None,
            "revenue_year_ago": safe_float(rev_row.get("yearAgoRevenue")) if rev_row is not None else None,
            "revenue_growth":   safe_float(rev_row.get("growth")) if rev_row is not None else None,
            "revenue_analysts": safe_int(rev_row.get("numberOfAnalysts")) if rev_row is not None else None,
            "eps_trend_current": safe_float(trend_row.get("current")) if trend_row is not None else None,
            "eps_trend_7d_ago":  safe_float(trend_row.get("7daysAgo")) if trend_row is not None else None,
            "eps_trend_30d_ago": safe_float(trend_row.get("30daysAgo")) if trend_row is not None else None,
            "eps_trend_60d_ago": safe_float(trend_row.get("60daysAgo")) if trend_row is not None else None,
            "eps_trend_90d_ago": safe_float(trend_row.get("90daysAgo")) if trend_row is not None else None,
            # eps_revisions has a quirky column-name shape: three keys are
            # lowercase `days` (`upLast7days` / `upLast30days` / `downLast30days`)
            # but the fourth is capital `Days` (`downLast7Days`) — Yahoo typo
            # preserved upstream. Use _coalesce to also accept the
            # consistent-lowercase variant in case Yahoo "fixes" the typo
            # someday; otherwise `down_7d` would silently start nulling out
            # while the other three kept working — invisible regression.
            "eps_revisions_up_7d":    safe_int(rev_count_row.get("upLast7days")) if rev_count_row is not None else None,
            "eps_revisions_up_30d":   safe_int(rev_count_row.get("upLast30days")) if rev_count_row is not None else None,
            "eps_revisions_down_7d":  safe_int(_coalesce_present(rev_count_row, "downLast7Days", "downLast7days")) if rev_count_row is not None else None,
            "eps_revisions_down_30d": safe_int(rev_count_row.get("downLast30days")) if rev_count_row is not None else None,
            # growth_estimates.stockTrend is empirically identical to
            # earnings_estimate.growth (same YoY consensus growth) — drop the
            # duplicate. indexTrend is the value-add: same-period sector or
            # benchmark growth, so eps_growth / index_growth gives "stock vs
            # market" with one number each.
            "index_growth": safe_float(growth_row.get("indexTrend")) if growth_row is not None else None,
        })

    # LTG (long-term growth) lives only in growth_estimates as a separate row
    # — structurally distinct from the quarterly/annual periods, so surface
    # at top level. Often null for stocks without analyst LTG coverage
    # (we saw AAPL's stockTrend=NaN with only indexTrend populated).
    long_term_growth = None
    if growth_ok and "LTG" in growth_df.index:
        ltg_row = growth_df.loc["LTG"]
        stock = safe_float(ltg_row.get("stockTrend"))
        index = safe_float(ltg_row.get("indexTrend"))
        if stock is not None or index is not None:
            long_term_growth = {"stock": stock, "index": index}

    return rows, long_term_growth, max_attempts, None


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

    # `note` exclusively marks the non-equity short-circuit (ETF / INDEX /
    # CRYPTOCURRENCY / etc.) where `estimates` is always []. IPO fall-
    # through uses `coverage_note` instead — pass it through unchanged
    # but don't trigger the short-circuit branch, because IPO equities
    # have populated estimates and we want consensus_* projection to run.
    if "note" in full:
        base["note"] = full["note"]
        for k in _SUMMARY_KEYS:
            base[k] = None
        # `estimates` was always [] for non-equity; null all consensus_*
        # in the same flat shape so the schema is consistent across paths.
        if "estimates" in full:
            for k in _CONSENSUS_SUMMARY_KEYS:
                base[k] = None
        if _LONG_TERM_GROWTH_KEY in full:
            base[_LONG_TERM_GROWTH_KEY] = full[_LONG_TERM_GROWTH_KEY]
        if "estimates_error" in full:
            base["estimates_error"] = full["estimates_error"]
        for key in RESULT_META:
            if key in full:
                base[key] = full[key]
        return base
    # IPO fall-through: surfaces `coverage_note` in summary so consumers
    # of the projected dict still have the explanatory string. The rest
    # of the function below produces the right shape naturally — empty
    # `rows` makes next/last null, and consensus_* projects from 0q if
    # `estimates` is present.
    if "coverage_note" in full:
        base["coverage_note"] = full["coverage_note"]

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
    # Project the 0q (current quarter / about-to-report) estimate row to
    # flat top-level consensus_* fields. Drops the full 4-period list —
    # for the entire panel use default mode + --estimates. Keeps summary's
    # "small flat dict for peer comparison" promise intact: ~9 extra
    # fields × 1 row vs ~24 fields × 4 rows.
    if "estimates" in full:
        zero_q = next((r for r in full["estimates"] if r.get("period") == "0q"), None)
        for k in _CONSENSUS_SUMMARY_KEYS:
            base[k] = None
        if zero_q is not None:
            base["consensus_eps_avg"]            = zero_q.get("eps_avg")
            base["consensus_eps_low"]            = zero_q.get("eps_low")
            base["consensus_eps_high"]           = zero_q.get("eps_high")
            base["consensus_eps_growth_yoy"]     = zero_q.get("eps_growth")
            base["consensus_eps_analysts"]       = zero_q.get("eps_analysts")
            base["consensus_eps_currency"]       = zero_q.get("eps_currency")
            base["consensus_revenue_avg"]        = zero_q.get("revenue_avg")
            base["consensus_revenue_growth_yoy"] = zero_q.get("revenue_growth")
            base["consensus_revenue_analysts"]   = zero_q.get("revenue_analysts")
            base["consensus_revenue_currency"]   = zero_q.get("revenue_currency")
    if _LONG_TERM_GROWTH_KEY in full:
        base[_LONG_TERM_GROWTH_KEY] = full[_LONG_TERM_GROWTH_KEY]
    if "estimates_error" in full:
        base["estimates_error"] = full["estimates_error"]
    for key in RESULT_META:
        if key in full:
            base[key] = full[key]
    return base


def _validate_summary_keys() -> None:
    """Module-load sanity check: `_summarize` must populate every key in
    its expected set for each code path. Catches the case where someone
    adds a key to `_SUMMARY_KEYS` / `_CONSENSUS_SUMMARY_KEYS` but forgets
    to set it in `_summarize` — a silent schema drift that smoke
    integration tests don't always catch because per-key existence isn't
    asserted on every code path. Mirrors the pattern in `info.py`
    (`_validate_summary_fields`).
    """
    cases = (
        # equity earnings_dates only (no --estimates)
        ({"symbol": "X", "quote_type": "EQUITY", "earnings_dates": []},
         "equity-empty", _SUMMARY_KEYS),
        # non-equity short-circuit (no --estimates)
        ({"symbol": "X", "quote_type": "ETF", "note": "n/a",
          "earnings_dates": []}, "non-equity", _SUMMARY_KEYS),
        # equity with --estimates: consensus_* must all be present
        ({"symbol": "X", "quote_type": "EQUITY", "earnings_dates": [],
          "estimates": []}, "equity-with-estimates",
         _SUMMARY_KEYS + _CONSENSUS_SUMMARY_KEYS),
        # non-equity with --estimates: consensus_* still present (all null)
        ({"symbol": "X", "quote_type": "ETF", "note": "n/a",
          "earnings_dates": [], "estimates": []},
         "non-equity-with-estimates",
         _SUMMARY_KEYS + _CONSENSUS_SUMMARY_KEYS),
        # IPO fall-through: EQUITY with `coverage_note` (NOT `note`) and
        # populated estimates. Must NOT short-circuit — consensus_* should
        # project from the 0q row, not be all-null like the non-equity
        # branch. The presence of `coverage_note` instead of `note` is
        # what keeps it out of the short-circuit.
        ({"symbol": "X", "quote_type": "EQUITY",
          "coverage_note": "ipo coverage note",
          "earnings_dates": [], "estimates": [{"period": "0q"}]},
         "ipo-fall-through",
         _SUMMARY_KEYS + _CONSENSUS_SUMMARY_KEYS),
    )
    for sample, label, expected in cases:
        out = _summarize(sample)
        missing = set(expected) - set(out.keys())
        if missing:
            raise RuntimeError(
                f"_summarize ({label}) missed expected keys: {sorted(missing)}")


_validate_summary_keys()


def fetch(symbol: str, limit: int = LIMIT_DEFAULT,
          past_only: bool = False, future_only: bool = False,
          *, slice_to_limit: bool = True,
          with_estimates: bool = False) -> dict:
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
      4. If `with_estimates=True` and equity: two extra calls for
         `earnings_estimate` + `revenue_estimate` (~+1s combined). Independent
         of `past_only` / `future_only` — these are forward-looking analyst
         consensus, not part of the past/future earnings_dates timeline.

    `slice_to_limit=False` is set by `main()` in `--summary` mode so the
    full bucket-sized fetch reaches `_summarize` for aggregate computation
    (avg_surprise_last_4 etc.). See `--limit` help text for per-mode semantic.

    Returns one of:
      - equity success:   {symbol, quote_type, timezone, earnings_dates: [...],
                           estimates?: [...], long_term_growth?: {...},
                           estimates_error?: ..., attempts?}
      - non-equity short: {symbol, quote_type, note, earnings_dates: [],
                           estimates?: [], attempts?}
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
        if with_estimates:
            # Mirror earnings_dates: empty list, not omitted, so callers don't
            # have to test for key existence based on the flag.
            out["estimates"] = []
        if qt_attempts > 1:
            out["attempts"] = qt_attempts
        _assert_note_contract(out)
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

    # Decide whether the empty-earnings_dates path is fatal. Without
    # `--estimates` it is — the user asked for the calendar and we got
    # nothing usable. With `--estimates` it isn't necessarily: a recent
    # IPO can have analyst consensus coverage (the panel) before any past
    # reports exist, so fall through and let the estimates fetch decide.
    earnings_empty = df is None or df.empty
    if earnings_empty and not with_estimates:
        return {
            "symbol": symbol,
            "error": ("no earnings dates returned (low coverage, recent IPO, "
                      "or rate-limited; for recent-IPO equities try --estimates "
                      "to fetch the analyst panel even when the calendar is empty)"),
            "error_kind": "not_found",
            "attempts": attempts,
        }

    if earnings_empty:
        rows = []
        tz_name = None  # no rows to derive a tz from
    else:
        # df.index is tz-aware (per yfinance/base.py — it explicitly tz_localizes
        # each row to the per-row timezone). Single timezone string is the tz
        # of the first row; in practice all rows share one tz per ticker.
        first_tz = df.index[0].tzinfo
        tz_name = str(first_tz) if first_tz is not None else None

        now_utc = datetime.now(tz=timezone.utc)
        rows = [_row_to_dict(ts, row, now_utc) for ts, row in df.iterrows()]

        # "Near-now first" sort: future events ASC (nearest upcoming at the
        # top of the future block), then past events DESC (most recent past
        # below the future block, oldest at the very bottom). Both halves
        # converge on the now-boundary in the middle. Rationale: small
        # `--limit` values keep the most useful rows — `--limit 3` for a
        # typical equity gives the next earnings event plus the 2 most
        # recent reported quarters, instead of 3 distant future estimates
        # (which would happen under a flat DESC sort).
        #
        # Parse to tz-aware datetime: ISO lex compare is only safe within
        # a single tz offset, and DST-spanning windows mix `-04:00` /
        # `-05:00` for ET-listed tickers. Sort cost is trivial (≤100 rows).
        _key = lambda r: datetime.fromisoformat(r["date"])
        future_rows = sorted([r for r in rows if r["is_future"]], key=_key)
        past_rows = sorted([r for r in rows if not r["is_future"]],
                           key=_key, reverse=True)
        rows = future_rows + past_rows

        # Post-fetch projection (filters are mutually exclusive at CLI).
        # Filter first, then truncate — `--past-only --limit 8` should
        # give 8 past events, not "filter 8 events down to whatever's past".
        if past_only:
            rows = [r for r in rows if not r["is_future"]]
        elif future_only:
            rows = [r for r in rows if r["is_future"]]

        # Honor --limit strictly. yfinance's `limit` param maps to a page-
        # size bucket (25/50/100) and does NOT truncate the returned df —
        # so without this slice, `--limit 5` could return ~25 rows. Slice
        # here so the CLI semantic ("max rows in output") matches user
        # expectations.
        #
        # `slice_to_limit=False` is set by main() in --summary mode: there
        # we want as many rows as yfinance returned for computing aggregates
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

    # Step 4 (optional): full Yahoo analyst panel — EPS + revenue consensus,
    # estimate trend, revisions, sector growth, long-term growth. Five
    # property reads on a shared Ticker.
    est_attempts = 1
    if with_estimates:
        est_rows, ltg, est_attempts, est_err = _fetch_estimates(symbol)
        out["estimates"] = est_rows
        if ltg is not None:
            out[_LONG_TERM_GROWTH_KEY] = ltg
        # Self-documenting flag for the IPO fall-through: the empty
        # earnings_dates list isn't a bug — analyst coverage exists but
        # the calendar / scrape returned nothing. Uses a separate field
        # name (`coverage_note`) instead of overloading `note`, which is
        # reserved for the non-equity short-circuit. This makes consumers
        # that filter on `if d.get("note")` only see non-equity rows;
        # consumers that care about IPO-style fall-through opt in by
        # checking `coverage_note` explicitly.
        if earnings_empty and est_rows:
            out["coverage_note"] = ("empty calendar (recent IPO or low "
                                    "coverage); analyst panel returned")
        if est_err is not None:
            # Soft failure: keep the earnings_dates payload, surface the
            # estimates failure separately so it doesn't poison the whole
            # call. `error_kind` semantics (rate_limit / network / etc.)
            # match the top-level `error_kind`.
            out["estimates_error"] = est_err

        # If both halves came back empty there's genuinely no data for this
        # ticker — collapse to a top-level error so the caller doesn't
        # mistake "Yahoo returned silence" for "we asked and got an answer".
        # The earnings-empty path is suppressed only when we expected
        # estimates to compensate; if estimates didn't, surface the
        # estimates failure kind (rate_limit / network / not_found) so
        # callers can decide whether to retry.
        if earnings_empty and not est_rows:
            kind = est_err or "not_found"
            # "delisted" is intentionally absent — those tickers exit at
            # the quote_type pre-check above and never reach this branch.
            # When est_err pinpoints rate_limit / network, surface that
            # alone rather than the generic "no data" prose; the cause is
            # the throttle, not low coverage, and the user should retry.
            # Calendar SUCCEEDED-but-empty by the time we reach this branch
            # (rate-limited calendar would have exited earlier via err_kind).
            # Phrase the rate_limit / network cases so the calendar's
            # success-but-empty status and the estimates side's failure
            # are clearly two different things — "and" rather than implying
            # both endpoints hit the same problem.
            if kind == "rate_limit":
                base_msg = ("earnings calendar returned no rows AND "
                            "analyst estimates rate-limited (429); retry later")
            elif kind == "network":
                base_msg = ("earnings calendar returned no rows AND "
                            "analyst estimates hit a network error; retry shortly")
            else:
                base_msg = ("no earnings dates and no analyst estimates "
                            "returned (low coverage, or both Yahoo endpoints empty)")
            return {
                "symbol": symbol,
                "error": base_msg,
                "error_kind": kind,
                "attempts": max(qt_attempts, attempts, est_attempts),
            }

    # Surface attempts only when actually retried. Use `max` (not sum) across
    # the underlying calls (quote_type pre-check + earnings scrape +
    # estimates fetches) so the field carries the same "max retries seen in
    # any single yfinance call" semantic as the other three modes — an
    # `attempts: 3` here means the same as in fast_info / history / info.
    max_attempts = max(qt_attempts, attempts, est_attempts)
    if max_attempts > 1:
        out["attempts"] = max_attempts
    _assert_note_contract(out)
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
            "  earnings.py --estimates AAPL                  # + analyst consensus EPS / revenue\n"
            "  earnings.py --summary --estimates AAPL MSFT   # peer compare incl. forward consensus\n"
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
    ap.add_argument("--estimates", action="store_true",
                    help="Attach the full Yahoo analyst panel: consensus EPS "
                         "+ revenue (avg / low / high / # analysts), 90-day "
                         "estimate trend, 7d/30d revision counts, sector "
                         "growth comparison, long-term growth. Per-period "
                         "rows for 0q (current Q) / +1q / 0y (current FY) / "
                         "+1y. Adds ~1.5–3s per equity (5 extra Yahoo calls "
                         "on a shared Ticker). Equity-only — non-equity "
                         "tickers get `estimates: []` via the same short-"
                         "circuit as `earnings_dates`. Independent of "
                         "--past-only / --future-only (estimates are forward-"
                         "looking). In --summary mode, projects 0q to flat "
                         "`consensus_*` fields (CSV-compatible); in default "
                         "mode emits the full panel.")
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

    if args.estimates and args.format == "csv" and not args.summary:
        # Default-mode CSV emits one row per (symbol, earnings_date) — there's
        # nowhere to put a 4-period analyst-panel list per ticker. Summary-
        # mode CSV is fine: the consensus_* fields already flatten to extra
        # columns. Refuse default-mode CSV; allow summary CSV.
        ap.error("--estimates --format csv requires --summary "
                 "(default-mode CSV's per-row layout doesn't fit the "
                 "4-period analyst panel; --summary projects 0q to flat "
                 "consensus_* columns that fit cleanly)")

    results = [
        fetch(s.strip().upper(), limit=args.limit,
              past_only=args.past_only, future_only=args.future_only,
              slice_to_limit=not args.summary,
              with_estimates=args.estimates)
        for s in args.symbols if s.strip()
    ]
    if args.summary:
        results = [_summarize(r) for r in results]
    _emit(results, args.format, summary=args.summary,
          with_estimates=args.estimates)


def _emit(results: list, fmt: str, *, summary: bool,
          with_estimates: bool = False) -> None:
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
        # Insert consensus_* before RESULT_META at the tail so the meta
        # columns (error / error_kind / attempts) stay rightmost — matches
        # the convention used elsewhere. long_term_growth is a dict and
        # doesn't fit a single CSV cell; intentionally dropped from CSV
        # (still in JSON / NDJSON output).
        cols = list(_SUMMARY_CSV_COLS)
        if with_estimates:
            meta_count = len(RESULT_META)
            cols = (cols[:-meta_count]
                    + list(_CONSENSUS_SUMMARY_KEYS)
                    + cols[-meta_count:])
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
