#!/usr/bin/env python3
"""Fetch yfinance analyst data (recommendations time series + grade-change
event log with embedded price-target moves) for one or more tickers and
print as JSON / NDJSON / CSV.

See `analyst.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed / non-applicable tickers carry an "error" or a
"note" / "coverage_note" field instead of data so a single bad symbol
does not poison the batch. Field schema lives in the *_KEYS / *_CSV_COLS
constants below.
"""
from __future__ import annotations
import yfinance as yf
from helpers import (
    RESULT_META, emit_json_or_ndjson, safe_float, safe_int, safe_str, with_retry,
)

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# `Ticker.recommendations` and `Ticker.recommendations_summary` are
# verified ALIASES (2026-05): both return the identical 4-row × 6-col
# DataFrame. We only call `recommendations` to avoid a redundant module
# fetch. `Ticker.upgrades_downgrades` is the separate per-event feed.
#
# Coverage shape (verified 2026-05):
#   - Equities (US + non-US):  recommendations populated.
#   - upgrades_downgrades:     populated for US-listed equities AND ADRs
#                              (TM); 404 → empty for non-US primary
#                              listings (0700.HK, BMW.DE) — Yahoo's
#                              grade-change feed is US-centric.
#   - ETFs / indexes / crypto / bogus: BOTH empty (404 on each).
#
# This gives three result classes — same shape as `insiders.py`:
#   1. All-empty              → ambiguous (non-equity / bogus / no
#                              coverage). Surface `note`.
#   2. Partial-empty          → recommendations populated, upgrades
#                              empty. Asymmetric Yahoo coverage for
#                              non-US issuers; the empty event list
#                              IS the answer. Surface `coverage_note`.
#   3. Full / near-full       → neither field set.
_EMPTY_NOTE = (
    "no analyst data (Yahoo's analyst endpoints cover equities; ETFs / "
    "indexes / crypto / FX / futures and bogus tickers all return empty "
    "frames — call fast_info to disambiguate)"
)
_COVERAGE_NOTE_PARTIAL = (
    "recommendations populated but upgrades_downgrades empty — typical "
    "for non-US primary listings (Yahoo's grade-change feed is US-centric; "
    "ADRs like TM still get full coverage). The empty event list is "
    "asymmetric Yahoo coverage, not a fetch failure"
)


# --- recommendations row schema ---------------------------------------------
#
# `Ticker.recommendations` is a 3- or 4-row DataFrame (one row per period
# label: `0m`, `-1m`, `-2m`, `-3m` — most-recent first; some tickers have
# only 3 rows of history). Six columns: `period`, `strongBuy`, `buy`,
# `hold`, `sell`, `strongSell`. Each cell is the COUNT of analysts in
# that bucket as of that period.
#
# We add `total` (sum of the five bucket counts) so consumers don't have
# to recompute it for every row. Computed inline; if any bucket is None
# we still sum the non-None values rather than degrading total to None,
# because partial buckets are vanishingly rare and a usable approximate
# total is friendlier than a null.
_RECOMMENDATIONS_KEYS = (
    "period",
    "strong_buy",
    "buy",
    "hold",
    "sell",
    "strong_sell",
    "total",
)

# --- upgrades_downgrades row schema -----------------------------------------
#
# `Ticker.upgrades_downgrades` is a date-indexed DataFrame (index name
# `GradeDate`, dtype datetime64[s], naive — no tz info). Eight columns
# in our projection (Yahoo emits 7 + the index):
#
#   GradeDate          → date (ISO 'YYYY-MM-DDTHH:MM:SS')
#   Firm               → firm
#   ToGrade            → to_grade  (e.g., "Outperform")
#   FromGrade          → from_grade
#   Action             → action     LOWERCASE enum: up / down / main /
#                                    init / reit (verified 2026-05 across
#                                    AAPL's 977 rows)
#   priceTargetAction  → price_target_action   CAPITALIZED enum: Raises /
#                                    Lowers / Maintains / Announces /
#                                    Adjusts / "" (empty for old
#                                    pre-2018 rows). Yahoo's
#                                    inconsistency, not ours — we pass
#                                    case through.
#   currentPriceTarget → current_price_target  (float, trading currency)
#   priorPriceTarget   → prior_price_target    (same)
#
# 0.0 in either price-target field is treated as Yahoo's "no target"
# sentinel (verified empirically: AAPL 'init' rows + many old pre-2018
# rows have 0.0 in both — a genuine $0 target on an analyst-covered
# equity is implausible). Projected to None to dodge the "100% increase
# from 0" trap downstream.
_CHANGE_KEYS = (
    "date",
    "firm",
    "to_grade",
    "from_grade",
    "action",
    "price_target_action",
    "current_price_target",
    "prior_price_target",
)

# Default-mode CSV columns. `record_class` discriminates row classes —
# same pattern as insiders' / holders' / options' default-mode CSVs.
# Section-specific columns are populated only on rows of that class;
# `symbol` repeats. Empty / errored tickers emit a single row carrying
# symbol + note + meta.
_DEFAULT_CSV_COLS = tuple(dict.fromkeys((
    "symbol", "quote_type", "record_class",
    *_RECOMMENDATIONS_KEYS,
    *_CHANGE_KEYS,
    "note", "coverage_note", *RESULT_META,
)))

# Summary-mode flat projection. Lifts the 0m / oldest snapshot of the
# recommendations time series to top level + adds 90-day rating-change
# rollups + the latest event for peer-compare tables. Reads from the
# FULL upgrades_downgrades list so 90d counts describe Yahoo's response,
# not the display knob — same invariant as insiders / holders.
#
# The "oldest" period is whichever row has the most-negative period
# integer (-3m if Yahoo gave 4 rows; -2m if only 3). `oldest_period`
# echoes the source so consumers can tell the comparison window.
_SUMMARY_FLAT_KEYS = (
    "quote_type",
    "total_analysts_current",
    "total_analysts_oldest",
    "oldest_period",
    "buy_pct_current",
    "buy_pct_oldest",
    "buy_pct_change",
    "consensus_score_current",
    "consensus_score_oldest",
    "consensus_score_change",
    "rating_changes_returned",
    "upgrades_last_90d",
    "downgrades_last_90d",
    "net_rating_changes_90d",
    "target_raises_last_90d",
    "target_lowers_last_90d",
    # Latest EVENT (any action — most recent regardless of type;
    # frequently `main` / `reit` since ~90% of rows are non-rating-
    # changing target tweaks). Renamed from `latest_change_*` because
    # the prior name implied "rating change" which is semantically
    # narrower than what this field captures.
    "latest_event_date",
    "latest_event_firm",
    "latest_event_action",
    "latest_event_to_grade",
    "latest_event_current_price_target",
    # Latest actual RATING CHANGE (filtered to action ∈ {up, down}).
    # New field set for the narrow "when did anyone last move their
    # rating up or down" question. NULL when no up/down events exist
    # in the upgrades_downgrades list (e.g. a stock with consensus-
    # only target adjustments and no rating moves).
    "latest_rating_change_date",
    "latest_rating_change_firm",
    "latest_rating_change_action",
    "latest_rating_change_from_grade",
    "latest_rating_change_to_grade",
    "latest_rating_change_current_price_target",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS,
                     "note", "coverage_note", *RESULT_META)


def _fetch_payloads(symbol: str):
    """Fetch all three Yahoo payloads — recommendations DataFrame,
    upgrades_downgrades DataFrame, and quote_type (via fast_info) —
    inside one retry-wrapped closure.

    Materializes all three reads inside the closure so any exception
    falls inside `with_retry`'s try/except. Same pattern as insiders /
    holders. Empirically (2026-05) yfinance issues separate
    `quoteSummary` requests per module group, so this is ~3 HTTP per
    ticker (recommendations + upgrades_downgrades + fast_info). Each
    is cheap (~0.1-0.3s warm) so total latency is ~0.5-1s on the hot
    path.

    `t.fast_info["quoteType"]` can crash on bogus / delisted tickers
    with an `AttributeError` from yfinance's internals — caught locally
    and projected to None rather than letting it kill the whole result
    (network / rate-limit failures still propagate to with_retry for
    proper backoff).
    """
    def _f():
        t = yf.Ticker(symbol)
        rec = t.recommendations
        ud = t.upgrades_downgrades
        try:
            qt = t.fast_info["quoteType"]
        except (AttributeError, KeyError, TypeError):
            qt = None
        return (rec, ud, qt)
    return with_retry(_f)


def _ts_to_iso(ts) -> str | None:
    """pandas Timestamp / NaT / None → 'YYYY-MM-DDTHH:MM:SS' or None.
    GradeDate is naive datetime64[s] in observed payloads (no tz). We
    don't tack on a fake `Z` / +00:00 — Yahoo's source tz is
    undocumented and inventing one would lie. Consumers wanting wall
    clock vs UTC behavior should treat the value as opaque-to-tz."""
    if ts is None:
        return None
    try:
        if ts != ts:  # NaT-like
            return None
    except TypeError:
        pass
    try:
        return ts.strftime("%Y-%m-%dT%H:%M:%S")
    except (AttributeError, ValueError):
        return None


def _safe_target(v):
    """Project a price-target field. 0.0 → None (Yahoo's 'no target'
    sentinel)."""
    f = safe_float(v)
    if f is None or f == 0.0:
        return None
    return f


def _project_recommendation_row(row) -> dict:
    sb = safe_int(row.get("strongBuy"))
    b = safe_int(row.get("buy"))
    h = safe_int(row.get("hold"))
    s = safe_int(row.get("sell"))
    ss = safe_int(row.get("strongSell"))
    # Strict null: any bucket missing → total is null. A partial sum
    # would look like a real total but exclude unknown buckets — safer
    # to surface "unknown" than a confidently-wrong number. In practice
    # all five buckets are populated together (verified across AAPL /
    # NVDA / TM / 0700.HK / BMW.DE in 2026-05); the strict rule only
    # bites pathological Yahoo responses.
    if any(v is None for v in (sb, b, h, s, ss)):
        total = None
    else:
        total = sb + b + h + s + ss
    return {
        "period": safe_str(row.get("period")),
        "strong_buy": sb,
        "buy": b,
        "hold": h,
        "sell": s,
        "strong_sell": ss,
        "total": total,
    }


def _project_recommendations(df) -> list[dict]:
    if df is None or df.empty:
        return []
    rows = df.to_dict(orient="records")
    return [_project_recommendation_row(r) for r in rows]


def _project_change_row(date, row) -> dict:
    return {
        "date": _ts_to_iso(date),
        "firm": safe_str(row.get("Firm")),
        "to_grade": safe_str(row.get("ToGrade")),
        "from_grade": safe_str(row.get("FromGrade")),
        "action": safe_str(row.get("Action")),
        "price_target_action": safe_str(row.get("priceTargetAction")),
        "current_price_target": _safe_target(row.get("currentPriceTarget")),
        "prior_price_target": _safe_target(row.get("priorPriceTarget")),
    }


def _project_changes(df) -> list[dict]:
    """Project upgrades_downgrades (date-indexed DataFrame) → list of dicts.

    Yahoo's order is desc-by-date in observed payloads (2026-05) but
    we don't depend on that — `_summarize`'s `latest_change_*` recomputes
    via `max()`.
    """
    if df is None or df.empty:
        return []
    out = []
    for date, row in df.iterrows():
        out.append(_project_change_row(date, row.to_dict()))
    return out


def _consensus_score(rec: dict) -> float | None:
    """Weighted mean rating: 1×strong_buy + 2×buy + 3×hold + 4×sell +
    5×strong_sell, divided by total. Lower is more bullish (1.0 =
    unanimous strong_buy, 5.0 = unanimous strong_sell). Approximates
    Yahoo's `info.recommendationMean` field without needing an extra
    info-mode HTTP — same encoding so consumers can compare directly."""
    total = rec.get("total")
    if not total:
        return None
    weighted = 0
    for i, k in enumerate(("strong_buy", "buy", "hold", "sell", "strong_sell"),
                          start=1):
        v = rec.get(k)
        if v is None:
            return None
        weighted += i * v
    return weighted / total


def _buy_pct(rec: dict) -> float | None:
    """(strong_buy + buy) / total — FRACTION (matches the rest of the
    skill's fraction-encoded percentages: info margins, holders.pct_held,
    etc.). 0.65 means 65% of analysts rate buy-or-better.

    Strict null: returns None if `total`, `strong_buy`, or `buy` is
    missing. Matches `_consensus_score`'s null discipline — substituting
    0 for None would produce a confidently-wrong fraction (under-counts
    the "buy" side when the count is unknown). In practice all buckets
    are populated together so this only bites pathological responses."""
    total = rec.get("total")
    if not total:
        return None
    sb = rec.get("strong_buy")
    b = rec.get("buy")
    if sb is None or b is None:
        return None
    return (sb + b) / total


def _period_to_int(period: str | None) -> int | None:
    """`-3m` → -3, `0m` → 0. Used only to find oldest / current rows
    without depending on Yahoo's row order."""
    if not period:
        return None
    p = period.replace("m", "").strip()
    try:
        return int(p)
    except ValueError:
        return None


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for peer comparison.

    Reads from the FULL (pre-limit) upgrades_downgrades list so 90d
    counts describe Yahoo's response, not the display knob — same
    invariant as insiders / holders / options.

    Carries `note` / `coverage_note` / `error` / `error_kind` /
    `attempts` through unchanged so tickers in the all-empty /
    partial-empty / failed states still surface in summary CSVs.
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}

    recs = full.get("recommendations") or []
    if recs:
        # Pair each row with its parsed period int so we can pick
        # current/oldest order-independently. Drop rows with unparseable
        # periods rather than guessing.
        with_period = [(r, _period_to_int(r.get("period"))) for r in recs]
        with_period = [(r, p) for r, p in with_period if p is not None]
        if with_period:
            current = max(with_period, key=lambda x: x[1])[0]
            oldest = min(with_period, key=lambda x: x[1])[0]
            out["total_analysts_current"] = current.get("total")
            out["total_analysts_oldest"] = oldest.get("total")
            out["oldest_period"] = oldest.get("period")
            out["buy_pct_current"] = _buy_pct(current)
            out["buy_pct_oldest"] = _buy_pct(oldest)
            if (out["buy_pct_current"] is not None
                    and out["buy_pct_oldest"] is not None):
                out["buy_pct_change"] = (
                    out["buy_pct_current"] - out["buy_pct_oldest"])
            out["consensus_score_current"] = _consensus_score(current)
            out["consensus_score_oldest"] = _consensus_score(oldest)
            # Sign convention is OPPOSITE of buy_pct_change: consensus
            # uses Yahoo's 1-5 Likert (1=strong_buy, 5=strong_sell), so
            # current - oldest < 0 means consensus moved MORE bullish
            # over the window. Doc this loudly because the sign flip
            # vs buy_pct_change is a footgun.
            if (out["consensus_score_current"] is not None
                    and out["consensus_score_oldest"] is not None):
                out["consensus_score_change"] = (
                    out["consensus_score_current"]
                    - out["consensus_score_oldest"])

    changes = full.get("upgrades_downgrades") or []
    # Empty-list path: leave 90d counts as None rather than 0. Empty
    # CAN mean "Yahoo doesn't index this ticker's grade changes"
    # (partial-empty path for non-US primary listings) — reporting
    # `upgrades_last_90d: 0` would be a confidently-wrong claim. With
    # at least one row, 0 means "no upgrades in the window" (which IS
    # a real signal).
    if changes:
        out["rating_changes_returned"] = len(changes)
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=90)
        upgrades = downgrades = raises = lowers = 0
        for c in changes:
            d = c.get("date")
            if not d or len(d) < 10:
                continue
            try:
                cd = datetime.strptime(d[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if cd < cutoff:
                continue
            if c.get("action") == "up":
                upgrades += 1
            elif c.get("action") == "down":
                downgrades += 1
            ptas = c.get("price_target_action")
            if ptas == "Raises":
                raises += 1
            elif ptas == "Lowers":
                lowers += 1
        out["upgrades_last_90d"] = upgrades
        out["downgrades_last_90d"] = downgrades
        out["net_rating_changes_90d"] = upgrades - downgrades
        out["target_raises_last_90d"] = raises
        out["target_lowers_last_90d"] = lowers
        # Latest EVENT (any action) — recompute via max() (don't
        # depend on Yahoo's desc sort).
        latest = max(changes, key=lambda c: c.get("date") or "")
        d_iso = latest.get("date") or ""
        out["latest_event_date"] = d_iso[:10] or None
        out["latest_event_firm"] = latest.get("firm")
        out["latest_event_action"] = latest.get("action")
        out["latest_event_to_grade"] = latest.get("to_grade")
        out["latest_event_current_price_target"] = latest.get(
            "current_price_target")
        # Latest RATING CHANGE (filter to action ∈ {up, down}). Returns
        # null fields when no rating changes exist in Yahoo's history
        # — common for stocks with target-only revisions.
        rating_changes = [c for c in changes
                          if c.get("action") in ("up", "down")]
        if rating_changes:
            latest_rc = max(rating_changes,
                            key=lambda c: c.get("date") or "")
            rc_iso = latest_rc.get("date") or ""
            out["latest_rating_change_date"] = rc_iso[:10] or None
            out["latest_rating_change_firm"] = latest_rc.get("firm")
            out["latest_rating_change_action"] = latest_rc.get("action")
            out["latest_rating_change_from_grade"] = latest_rc.get(
                "from_grade")
            out["latest_rating_change_to_grade"] = latest_rc.get("to_grade")
            out["latest_rating_change_current_price_target"] = (
                latest_rc.get("current_price_target"))

    # Carry quote_type + ambiguity / error fields uniformly. quote_type
    # is always set on success (incl. coverage_note / note paths) and
    # absent only on hard fetch failure — `if k in full` handles both
    # shapes without a separate explicit set above.
    for k in ("quote_type", "note", "coverage_note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def _apply_limit(result: dict, limit: int | None) -> dict:
    """Truncate upgrades_downgrades list IN-PLACE.

    **Mutates `result`.** Mirrors insiders._apply_limit's contract.
    No-op when `limit is None`. Does not affect `recommendations`
    (which is already capped at 3-4 rows by Yahoo).
    """
    if limit is None:
        return result
    if "upgrades_downgrades" in result:
        result["upgrades_downgrades"] = result["upgrades_downgrades"][:limit]
    return result


def fetch(symbol: str) -> dict:
    """Fetch the full Yahoo analyst payload for `symbol`. No `--limit`
    — callers slice via `_apply_limit` (default mode) or read from full
    list (`--summary`)."""
    result, err_kind, attempts = _fetch_payloads(symbol)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    rec_df, ud_df, quote_type = result
    recommendations = _project_recommendations(rec_df)
    changes = _project_changes(ud_df)
    # quote_type comes second so it's adjacent to symbol in JSON output —
    # consumers scanning the head of a result can disambiguate
    # equity / ETF / index / crypto without reading further. Set even
    # when None so the field is shape-stable (callers can rely on the
    # key always existing).
    out = {
        "symbol": symbol,
        "quote_type": safe_str(quote_type),
        "recommendations": recommendations,
        "upgrades_downgrades": changes,
    }
    # Three result classes — `note` and `coverage_note` are mutually
    # exclusive at the result level:
    #
    # 1. All-empty (no recommendations AND no upgrades_downgrades) —
    #    ambiguous: non-equity / bogus / no-coverage. Caller chains
    #    fast_info to disambiguate. → `note`.
    # 2. Partial-empty (recommendations populated AND
    #    upgrades_downgrades empty) — verified for non-US primary
    #    listings (0700.HK, BMW.DE) where Yahoo's grade-change feed
    #    is US-centric. The empty list IS the answer for these
    #    tickers, not a transient gap. → `coverage_note`.
    # 3. Anything else (full coverage, OR upgrades populated but
    #    recommendations empty — not yet observed) → neither field.
    if not recommendations and not changes:
        out["note"] = _EMPTY_NOTE
    elif recommendations and not changes:
        out["coverage_note"] = _COVERAGE_NOTE_PARTIAL
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance analyst data for one or more tickers.\n\n"
            "Two sections per ticker:\n"
            "  recommendations      — 3-4 row time series (0m / -1m / -2m /\n"
            "                         -3m) of analyst rating distribution:\n"
            "                         strong_buy / buy / hold / sell /\n"
            "                         strong_sell counts, plus a derived\n"
            "                         `total`. From Ticker.recommendations.\n"
            "  upgrades_downgrades  — per-event grade-change feed: firm,\n"
            "                         from/to grade, action (up/down/main/\n"
            "                         init/reit), embedded price-target\n"
            "                         change (raise/lower/etc + current/\n"
            "                         prior target). Goes back to ~2012\n"
            "                         for major US large-caps (AAPL: 977\n"
            "                         events; NVDA: 981). From\n"
            "                         Ticker.upgrades_downgrades.\n\n"
            "COMPLEMENTARY TO `info` AND `earnings --estimates`. `info`'s\n"
            "analyst section has the static current consensus (target_mean_\n"
            "price, recommendation_key, num_analyst_opinions). `earnings\n"
            "--estimates` has the underlying EPS / revenue forecasts that\n"
            "drive those targets. THIS mode adds the time series of how\n"
            "consensus has shifted (recommendations 0m vs -3m) and the\n"
            "per-event log of WHO changed their mind WHEN (upgrades_\n"
            "downgrades).\n\n"
            "EQUITY-FOCUSED (same shape as insiders / holders / options):\n"
            "ETFs / indexes / crypto / FX / futures all return both frames\n"
            "empty (success-with-`note`, ambiguous — chain fast_info to\n"
            "disambiguate). Non-US primary listings (0700.HK, BMW.DE) get\n"
            "recommendations populated but upgrades_downgrades empty —\n"
            "surfaced via a separate `coverage_note` field (mutually\n"
            "exclusive with `note`); ADRs (TM) get full coverage. Distinguish:\n"
            "`note` = no data, ambiguous; `coverage_note` = real data,\n"
            "asymmetric Yahoo coverage.\n\n"
            "CSV default-mode output uses a `record_class` column with\n"
            "values `recommendation` / `change` as a row-class\n"
            "discriminator, with the symbol col repeating across a\n"
            "ticker's rows.\n\n"
            "UNITS: buy_pct_current / buy_pct_oldest / buy_pct_change in\n"
            "--summary are FRACTIONS (0.65 = 65%, NOT percent-encoded) —\n"
            "matches info margins, holders.pct_held, etc. consensus_score_*\n"
            "is on the 1-5 Yahoo scale (1=strong_buy, 5=strong_sell, lower\n"
            "is more bullish), comparable to info.analyst.recommendation_\n"
            "mean. Price targets are floats in the ticker's TRADING\n"
            "currency (USD for AAPL, also for ADRs like TM). 0.0 is\n"
            "treated as Yahoo's 'no target' sentinel and projected to null."
        ),
        epilog=(
            "Examples:\n"
            "  analyst.py AAPL                               # full sections\n"
            "  analyst.py --summary AAPL MSFT GOOGL          # peer rollup\n"
            "  analyst.py --limit 20 AAPL                    # top 20 events\n"
            "  analyst.py --format csv --summary AAPL MSFT\n"
            "  analyst.py --format csv AAPL                  # one row per record\n"
            "                                                # (recommendation/change)\n"
            "\n"
            "See references/analyst.md for the field schema, presentation\n"
            "guidance, and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap upgrades_downgrades rows per ticker. Default: "
                         "keep all (Yahoo returns the full history, often "
                         "900+ rows for major US large-caps going back to "
                         "~2012). Does not affect the recommendations time "
                         "series (which Yahoo already caps at 3-4 rows). "
                         "Silently ignored in --summary mode — the flat "
                         "metrics (rating_changes_returned, *_last_90d) "
                         "are computed from Yahoo's full response and stay "
                         "invariant under --limit by design.")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: current/oldest "
                         "snapshot of the recommendations distribution + "
                         "buy_pct change + consensus_score change + 90-day "
                         "rating-change rollups (upgrades / downgrades / "
                         "target raises / lowers) + the latest event. "
                         "Useful for peer-comparison tables. Same network "
                         "cost as default mode (post-fetch projection).")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = default mode emits one row per RECORD (with "
                         "a `record_class` discriminator: recommendation / "
                         "change), with the symbol col repeating; "
                         "--summary csv emits strict one-row-per-ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    results = [fetch(s.strip().upper())
               for s in args.symbols if s.strip()]

    if args.summary:
        # _summarize reads from full pre-limit lists; --limit is a no-op
        # here by design (metric describes Yahoo's response, not display).
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
        quote_type = r.get("quote_type", "")
        # `note` and `coverage_note` are mutually exclusive at the result
        # level (set in fetch()); included together here so a single
        # `carry` dict serves both the short-circuit row (note path)
        # and the per-record data rows (coverage_note path, which still
        # has recommendation rows to emit).
        carry = {k: r.get(k, "")
                 for k in ("note", "coverage_note", *RESULT_META) if k in r}
        # Errored or all-empty: one carry row, no record data.
        # coverage_note is NOT a short-circuit — partial-empty tickers
        # still have recommendation rows to emit.
        if "error" in r or "note" in r:
            row = {"symbol": symbol, "quote_type": quote_type, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        for rec in r.get("recommendations", []):
            row = {"symbol": symbol, "quote_type": quote_type,
                   "record_class": "recommendation", **rec, **carry}
            writer.writerow([row.get(c, "") for c in cols])
        for ch in r.get("upgrades_downgrades", []):
            row = {"symbol": symbol, "quote_type": quote_type,
                   "record_class": "change", **ch, **carry}
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
