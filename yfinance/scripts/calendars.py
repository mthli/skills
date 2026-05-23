#!/usr/bin/env python3
"""Fetch a Yahoo Finance market calendar — earnings, IPOs, splits, or
economic events — over a date range.

See `calendars.py --help` for usage. Unlike the per-ticker wrappers
(which iterate N tickers), calendars is **market-wide**: one HTTP call
returns all events in the date window. Output is a SINGLE envelope
dict — same shape as `screener.py` (one call → one result, not a
list of per-ticker records).

Modes (one per invocation):

  --type earnings   companies reporting earnings (default; defaults to
                    Yahoo's "most active" filter to avoid drowning in
                    micro-caps — disable with --no-most-active or
                    raise the floor with --market-cap)
  --type ipo        upcoming / recent IPOs
  --type splits     stock splits (forward and reverse)
  --type economic   macro / central-bank events (CPI, FOMC, GDP, ...)

Date window: defaults to today + 7 days. Override with --start /
--end (ISO dates) or --days N (today through today+N).
"""
from __future__ import annotations
import yfinance as yf
import pandas as pd
from helpers import (
    RESULT_META,
    epoch_to_date,
    safe_bool,
    safe_float,
    safe_int,
    safe_str,
    with_retry,
)

import argparse
import csv as _csv
import json as _json
import math
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Per-type schema. Keys define both the JSON projection order and the
# CSV column order. Datetimes are preserved as ISO-with-offset strings;
# dates as YYYY-MM-DD; numerics as safe_float / safe_int (NaN/Inf → None).
#
# Yahoo's column labels (source-of-truth: yfinance/calendars.py
# PREDEFINED_CALENDARS) are renamed here to snake_case + semantic names:
#   "Marketcap"       → market_cap         (was "Market Cap (Intraday)")
#   "Event Start Date" → event_start_datetime
#   "Reported EPS"    → eps_actual         (matches earnings.py field)
#   "Surprise(%)"     → surprise_pct       (PERCENT, matches earnings.py)
#   "Date"            → ipo_date           (semantic name)
#   "Price"           → offer_price        (semantic name)
#   "Action"          → action             (status: Expected / Priced / etc.)
#   "For"             → period             (e.g. "Mar", "Q1")
#   "Last"            → prior              (clearer name)
#   "Revised"         → prior_revised
#   "Payable On"      → payable_date
#   "Old Share Worth" → old_ratio
#   "Share Worth"     → new_ratio
EARNINGS_KEYS = (
    "symbol", "company", "market_cap",
    "event_name", "event_start_datetime", "timing",
    "eps_estimate", "eps_actual", "surprise_pct",
)
IPO_KEYS = (
    "symbol", "company", "exchange",
    "filing_datetime", "ipo_datetime", "amended_datetime",
    "price_from", "price_to", "offer_price",
    "currency", "shares", "action",
)
SPLITS_KEYS = (
    "symbol", "company", "payable_datetime",
    "optionable", "old_ratio", "new_ratio", "direction",
)
ECONOMIC_KEYS = (
    "event", "region", "event_time", "period",
    "actual", "expected", "prior", "prior_revised", "unit",
)

KEYS_BY_TYPE = {
    "earnings": EARNINGS_KEYS,
    "ipo": IPO_KEYS,
    "splits": SPLITS_KEYS,
    "economic": ECONOMIC_KEYS,
}


def _iso_dt(v):
    """pd.Timestamp / NaT / None / epoch-int → ISO-with-offset string or None.

    Yahoo emits all calendar timestamps with explicit tz (UTC in
    observed payloads), so we preserve the offset rather than
    stripping to a naive datetime — keeps the same convention as
    `history.py` intraday and `earnings.py` next_date.

    NB: we used to truncate IPO / splits / filing dates to YYYY-MM-DD
    on the theory that Yahoo's `04:00 UTC` was "midnight ET decoration."
    Probe (2026-05) confirmed 100% of those timestamps come back as
    exactly `04:00 UTC` — but that's `midnight EDT` (UTC-4) during
    DST. In EST (winter, UTC-5) the same encoding lands at `23:00 EST`
    on the *previous* calendar day, so a naive `strftime('%Y-%m-%d')`
    on the UTC timestamp would silently be off-by-one. Preserving the
    full datetime sidesteps that — the consumer can localize to the
    appropriate market tz before truncating if they need a date.

    Type handling: we explicitly accept (a) `pd.Timestamp` (current
    Yahoo shape), (b) ISO-formatted strings (defensive — pass
    through), and (c) epoch ints/floats (defensive — Yahoo could
    plausibly drift to this). Anything else returns None rather than
    `safe_str`'ing a number into a misleading-looking string.
    """
    if v is None:
        return None
    # pd.isna handles NaT, np.nan, pd.NA, None all in one. Skip on
    # bool to avoid pd.isna(False) → False but pd.isna treating bool
    # arrays oddly; bool isn't a datetime anyway.
    if not isinstance(v, bool):
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, str):
        return v if v else None
    # Epoch int/float fallback. Yahoo doesn't currently emit these for
    # calendars, but if they ever do, this routes through helpers'
    # epoch_to_date for a YYYY-MM-DD (we lose tz fidelity but don't
    # silently emit a number-as-string).
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return epoch_to_date(v)
    return None


def _first(rec: dict, *keys):
    """Return the first "really populated" value among `keys` from `rec`.

    Defensive against yfinance's PREDEFINED_CALENDARS rename map
    drifting upstream (e.g. if `"Market Cap (Intraday)" → "Marketcap"`
    rename were removed, our projector would silently return None
    without this fallback chain).

    Treats as "missing" (and falls through to the next key):
      - None
      - NaN / Inf floats
      - empty / whitespace-only strings
      - pandas NA / NaT (caught via pd.isna)
    """
    for k in keys:
        v = rec.get(k)
        if v is None:
            continue
        # Don't run pd.isna on bools — accepts but is semantically
        # confusing, and bools aren't valid fallthrough triggers anyway.
        if not isinstance(v, bool):
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                pass
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _project_earnings(rec: dict) -> dict:
    return {
        "symbol":               safe_str(rec.get("Symbol")),
        "company":              safe_str(rec.get("Company")),
        # Yahoo's raw label is "Market Cap (Intraday)" but yfinance's
        # rename map collapses it to "Marketcap". Fall back to the raw
        # label if the rename ever drops upstream.
        "market_cap":           safe_int(_first(rec, "Marketcap",
                                                "Market Cap (Intraday)",
                                                "Market Cap")),
        "event_name":           safe_str(rec.get("Event Name")),
        "event_start_datetime": _iso_dt(rec.get("Event Start Date")),
        "timing":               safe_str(rec.get("Timing")),
        "eps_estimate":         safe_float(rec.get("EPS Estimate")),
        "eps_actual":           safe_float(rec.get("Reported EPS")),
        # yfinance's rename map is "Surprise (%)" → "Surprise(%)".
        # Fall back to the raw label if that drops.
        "surprise_pct":         safe_float(_first(rec, "Surprise(%)",
                                                  "Surprise (%)")),
    }


def _project_ipo(rec: dict) -> dict:
    return {
        "symbol":              safe_str(rec.get("Symbol")),
        "company":             safe_str(rec.get("Company")),
        # yfinance renames "Exchange Short Name" → "Exchange". Fall back.
        "exchange":            safe_str(_first(rec, "Exchange",
                                               "Exchange Short Name")),
        "filing_datetime":     _iso_dt(rec.get("Filing Date")),
        "ipo_datetime":        _iso_dt(rec.get("Date")),
        "amended_datetime":    _iso_dt(rec.get("Amended Date")),
        "price_from":          safe_float(rec.get("Price From")),
        "price_to":             safe_float(rec.get("Price To")),
        "offer_price":         safe_float(rec.get("Price")),
        "currency":            safe_str(rec.get("Currency")),
        "shares":              safe_int(rec.get("Shares")),
        "action":              safe_str(rec.get("Action")),
    }


def _project_splits(rec: dict) -> dict:
    old = safe_float(rec.get("Old Share Worth"))
    new = safe_float(rec.get("Share Worth"))
    # Derived split direction. Forward = post-split share count > pre
    # (price per share goes DOWN, share count goes UP); reverse =
    # consolidation (KQ Korean reverse splits dominate observed
    # payloads). Even (1:1) is theoretically possible if both ratios
    # are equal; null when either side is missing.
    if old is None or new is None:
        direction = None
    elif new > old:
        direction = "forward"
    elif new < old:
        direction = "reverse"
    else:
        direction = "even"
    return {
        "symbol":           safe_str(rec.get("Symbol")),
        "company":          safe_str(rec.get("Company")),
        "payable_datetime": _iso_dt(rec.get("Payable On")),
        # yfinance renames "Optionable?" → "Optionable" (drops the `?`).
        "optionable":       safe_bool(_first(rec, "Optionable",
                                             "Optionable?")),
        "old_ratio":        old,
        "new_ratio":        new,
        "direction":        direction,
    }


# Best-effort unit inference for economic events. Yahoo doesn't ship a
# per-row unit, so we read the event-name string with a small set of
# heuristics. Misses are fine — `unit: null` just means "consult the
# event name and don't infer arithmetic semantics." Add more patterns
# here as new event-name shapes show up; keep ordered most-specific
# first.
_UNIT_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # Rate-of-change suffixes. "YY" / "MM" / "QQ" indicate year-over-
    # year / month-over-month / quarter-over-quarter percentage changes.
    (re.compile(r"\b(YY|MM|QQ)\b", re.IGNORECASE), "percent"),
    # Index-level releases — PMI / sentiment / confidence indicators.
    # All come back as raw index levels (~50 = expansion threshold for
    # PMI; ~100 baseline for confidence indices).
    (re.compile(r"\b(PMI|Sentiment|Confidence|Conditions|Expectations|"
                r"Index|ISM|ZEW|IFO|Tankan|Manheim)\b", re.IGNORECASE),
     "index_level"),
    # Inflation / price indicators not caught by rate-of-change suffix.
    # Prefer percent encoding when the headline is a CPI / PPI / Infl
    # variant — Yahoo nearly always serves these as percent regardless
    # of the suffix.
    (re.compile(r"\b(CPI|PPI|Infl(?:ation)?|Wage|Earnings|Yield)\b",
                re.IGNORECASE), "percent"),
    # Counts in thousands — payrolls / unemployment claims / hiring.
    (re.compile(r"\b(Payrolls|Claims|NFP|Hiring|Vacancies|Job(?:less)?)\b",
                re.IGNORECASE), "thousands"),
    # Currency-denominated balances / aggregates.
    (re.compile(r"\b(Trade Balance|Current Account|Budget|Deficit|"
                r"Reserves|Money Supply|M\d|Loan)\b", re.IGNORECASE),
     "currency"),
    # Rate decisions — central-bank policy rates, all percentage.
    (re.compile(r"\b(Rate Decision|Policy Rate|Funds Rate|Repo|Bank Rate)\b",
                re.IGNORECASE), "percent"),
    # GDP without YY/MM/QQ — usually annualized level or rate (ambiguous;
    # but most observed GDP releases are percent).
    (re.compile(r"\bGDP\b", re.IGNORECASE), "percent"),
)


def _infer_unit(event_name: str | None) -> str | None:
    """Best-effort unit classification from the event name string.

    Returns one of: `percent`, `index_level`, `thousands`, `currency`,
    or `None` (no rule matched — consult the event name).
    """
    if not event_name:
        return None
    for pattern, unit in _UNIT_RULES:
        if pattern.search(event_name):
            return unit
    return None


def _project_economic(rec: dict) -> dict:
    event = safe_str(rec.get("Event"))
    return {
        "event":         event,
        "region":        safe_str(rec.get("Region")),
        "event_time":    _iso_dt(rec.get("Event Time")),
        "period":        safe_str(rec.get("For")),
        "actual":        safe_float(rec.get("Actual")),
        "expected":      safe_float(rec.get("Expected")),
        "prior":         safe_float(rec.get("Last")),
        "prior_revised": safe_float(rec.get("Revised")),
        "unit":          _infer_unit(event),
    }


PROJECTOR_BY_TYPE = {
    "earnings": _project_earnings,
    "ipo":      _project_ipo,
    "splits":   _project_splits,
    "economic": _project_economic,
}


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_dates(
    start: str | None, end: str | None,
    days: int | None, past_days: int | None,
) -> tuple[str, str]:
    """Resolve --start / --end / --days / --past-days into (start, end).

    Modes (mutually exclusive at the argparse level):
      none          → today through today+7 (matches yfinance default)
      --start       → start through start+7
      --start --end → exact window
      --start --days N → start through start+N
      --past-days N → today-N through today (retrospective scan)

    Both endpoints are YYYY-MM-DD; Yahoo's filter is inclusive on both
    ends.
    """
    today = datetime.now(timezone.utc).date()
    if past_days is not None:
        # Retrospective convenience: ignore --start / --end / --days
        # (argparse should reject combinations upstream, but defensive).
        return (today - timedelta(days=past_days)).isoformat(), today.isoformat()
    if start is None:
        start = today.isoformat()
    if days is not None:
        end = (datetime.fromisoformat(start).date()
               + timedelta(days=days)).isoformat()
    elif end is None:
        end = (datetime.fromisoformat(start).date()
               + timedelta(days=7)).isoformat()
    return start, end


def _snake(label: str) -> str:
    """Yahoo column label → snake_case key for --full output.
    "Event Start Date" → "event_start_date", "Surprise(%)" → "surprise_pct",
    "Optionable?" → "optionable".
    """
    s = label.replace("(%)", "_pct").replace("?", "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    return s


def fetch(
    *,
    cal_type: str,
    start: str,
    end: str,
    limit: int,
    offset: int,
    market_cap: float | None,
    filter_most_active: bool,
    full: bool = False,
) -> dict:
    """Run a single Calendars call. Returns an envelope dict — one
    call, one result. Errors classified via with_retry; YFException /
    network / 429 → error_kind populated, results omitted.

    NB: yfinance silently drops `filter_most_active` when offset > 0
    (its source code: `if filter_most_active and not offset:`). We
    surface that in the envelope's `filter_most_active` field so the
    user sees what was actually applied.
    """
    cal = yf.Calendars(start=start, end=end)

    def _call() -> "pd.DataFrame":
        # All yfinance calendar getters share the same kw shape EXCEPT
        # earnings, which adds market_cap + filter_most_active.
        if cal_type == "earnings":
            return cal.get_earnings_calendar(
                market_cap=market_cap,
                filter_most_active=filter_most_active,
                limit=limit,
                offset=offset,
            )
        if cal_type == "ipo":
            return cal.get_ipo_info_calendar(limit=limit, offset=offset)
        if cal_type == "splits":
            return cal.get_splits_calendar(limit=limit, offset=offset)
        if cal_type == "economic":
            return cal.get_economic_events_calendar(limit=limit, offset=offset)
        raise ValueError(f"unknown calendar type: {cal_type!r}")

    # yfinance's Calendars warns on incomplete-boundary; we always pass
    # both, so suppress just to keep stderr clean for piped consumers.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        df, err_kind, attempts = with_retry(_call)

    out: dict = {
        "type": cal_type,
        "start": start,
        "end": end,
    }
    if cal_type == "earnings":
        # `filter_most_active` is silently dropped by yfinance when
        # offset > 0 — surface the effective state, not the requested.
        out["filter_most_active"] = filter_most_active and offset == 0
        out["market_cap_floor"] = safe_float(market_cap)

    if err_kind:
        out["error"] = (
            f"fetch failed ({err_kind}, after {attempts} attempt(s))"
        )
        out["error_kind"] = err_kind
        out["attempts"] = attempts
        return out

    if attempts > 1:
        out["attempts"] = attempts

    # Convert DataFrame → list of records. The DataFrame index carries
    # the primary id column (Symbol for earnings/ipo/splits, Event for
    # economic) — reset_index lifts it to a regular column so the
    # projector can pick it up uniformly.
    if df is None or df.empty:
        out["returned"] = 0
        out["offset"] = offset
        out["note"] = (
            f"no {cal_type} events in {start} → {end} "
            f"(window may be too narrow, or filters too restrictive)"
        )
        out["results"] = []
        return out

    records = df.reset_index().to_dict("records")
    # Yahoo's calendar endpoint doesn't return a separate "total
    # available" count (unlike screener), so we don't try to fake one
    # — `returned` is the only honest count we have.
    out["returned"] = len(records)
    out["offset"] = offset
    if full:
        # Raw Yahoo columns, snake_cased but no projection. Datetime
        # cells still get ISO-stringified so the JSON is serializable.
        out["results"] = [
            {_snake(k): (_iso_dt(v) if isinstance(v, pd.Timestamp)
                         else (None if pd.isna(v) and not isinstance(v, bool)
                               else v))
             for k, v in r.items()}
            for r in records
        ]
    else:
        project = PROJECTOR_BY_TYPE[cal_type]
        out["results"] = [project(r) for r in records]
    return out


# --- Per-type --summary projections. Each takes the list of records
# from fetch() (already projected via _project_*) and returns a flat
# dict suitable for peer comparison. Mirrors the per-type CSV
# discriminator: each row is "this is the {type} rollup."
def _summarize_earnings(rows: list[dict]) -> dict:
    timing_counts: dict[str, int] = {}
    for r in rows:
        t = r.get("timing") or "unknown"
        timing_counts[t] = timing_counts.get(t, 0) + 1
    market_caps = [r["market_cap"] for r in rows if r.get("market_cap")]
    return {
        "count":                 len(rows),
        "count_with_estimate":   sum(1 for r in rows if r.get("eps_estimate") is not None),
        "count_reported":        sum(1 for r in rows if r.get("eps_actual") is not None),
        "count_by_timing":       timing_counts,
        "avg_market_cap":        (sum(market_caps) / len(market_caps)) if market_caps else None,
        "max_market_cap":        max(market_caps) if market_caps else None,
        "min_market_cap":        min(market_caps) if market_caps else None,
    }


def _summarize_ipo(rows: list[dict]) -> dict:
    action_counts: dict[str, int] = {}
    for r in rows:
        a = r.get("action") or "unknown"
        action_counts[a] = action_counts.get(a, 0) + 1
    exchanges = sorted({r["exchange"] for r in rows if r.get("exchange")})
    return {
        "count":                  len(rows),
        "count_by_action":        action_counts,
        "unique_exchanges":       exchanges,
        "count_with_offer_price": sum(1 for r in rows if r.get("offer_price") is not None),
        "count_with_price_range": sum(1 for r in rows
                                      if r.get("price_from") is not None
                                      and r.get("price_to") is not None),
    }


def _summarize_splits(rows: list[dict]) -> dict:
    return {
        "count":                len(rows),
        "count_forward":        sum(1 for r in rows if r.get("direction") == "forward"),
        "count_reverse":        sum(1 for r in rows if r.get("direction") == "reverse"),
        "count_even":           sum(1 for r in rows if r.get("direction") == "even"),
        "count_with_options":   sum(1 for r in rows if r.get("optionable") is True),
    }


def _summarize_economic(rows: list[dict]) -> dict:
    region_counts: dict[str, int] = {}
    unit_counts: dict[str, int] = {}
    for r in rows:
        reg = r.get("region") or "unknown"
        region_counts[reg] = region_counts.get(reg, 0) + 1
        unit = r.get("unit") or "unknown"
        unit_counts[unit] = unit_counts.get(unit, 0) + 1
    # Cap region rollup to top 10 (descending by count) for compact
    # peer compare. When fewer than 10 regions are present we just
    # emit them all — the `_top10` suffix would be misleading, hence
    # the plain `count_by_region` name. `unique_regions` carries the
    # full list (sorted alphabetically) for callers that want every
    # region without count weighting.
    top_regions = dict(sorted(region_counts.items(),
                              key=lambda x: -x[1])[:10])
    return {
        "count":               len(rows),
        "count_by_region":     top_regions,
        "unique_regions":      sorted(region_counts.keys()),
        "count_by_unit":       unit_counts,
        "count_with_actual":   sum(1 for r in rows if r.get("actual") is not None),
    }


SUMMARIZER_BY_TYPE = {
    "earnings": _summarize_earnings,
    "ipo":      _summarize_ipo,
    "splits":   _summarize_splits,
    "economic": _summarize_economic,
}


def summarize(envelope: dict) -> dict:
    """Replace `results` with a flat per-type rollup. Keeps envelope
    metadata (type, start, end, offset, error / note) intact so peer
    comparison across types still carries window context."""
    cal_type = envelope.get("type", "earnings")
    rows = envelope.get("results") or []
    out = {k: v for k, v in envelope.items() if k != "results"}
    out["summary"] = SUMMARIZER_BY_TYPE[cal_type](rows)
    return out


def _emit_single_csv(result: dict) -> None:
    """One envelope → CSV. Cols = type-specific schema + note + meta.
    Used for default mode when there's only one type."""
    cal_type = result.get("type", "earnings")
    keys = KEYS_BY_TYPE.get(cal_type, EARNINGS_KEYS)
    rows = result.get("results") or []
    cols = [*keys, "note", *RESULT_META]
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    if not rows:
        carry = {k: result.get(k, "") for k in ("note", *RESULT_META)}
        writer.writerow([carry.get(c, "") for c in cols])
        return
    for row in rows:
        writer.writerow([row.get(c, "") if c in keys else "" for c in cols])


def _emit_multi_csv(envelopes: list[dict]) -> None:
    """Multi-type → CSV with `record_class` discriminator + union of
    all type schemas. Empty cells where a column isn't applicable to
    a given record's type."""
    # Union of cols, in a stable order: earnings → ipo → splits →
    # economic, with a `record_class` discriminator first.
    seen: list[str] = []
    for cal_type in ("earnings", "ipo", "splits", "economic"):
        for k in KEYS_BY_TYPE[cal_type]:
            if k not in seen:
                seen.append(k)
    cols = ["record_class", *seen, "note", *RESULT_META]
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for env in envelopes:
        cal_type = env.get("type", "earnings")
        rows = env.get("results") or []
        if not rows:
            # Empty / errored envelope still gets one carry row so the
            # caller sees note / error per type, not a silent gap.
            carry: dict = {"record_class": cal_type}
            for c in ("note", *RESULT_META):
                carry[c] = env.get(c, "")
            writer.writerow([carry.get(c, "") for c in cols])
            continue
        for row in rows:
            r = {"record_class": cal_type, **row}
            writer.writerow([r.get(c, "") for c in cols])


def _emit_single(result: dict, fmt: str) -> None:
    """Emit a single-type envelope in the requested format."""
    if fmt == "json":
        # Single envelope dict — same shape as screener.py (one call,
        # one result). NOT wrapped in a list.
        print(_json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return

    rows = result.get("results") or []
    cal_type = result.get("type", "earnings")

    if fmt == "ndjson":
        if not rows:
            # No data — emit a single envelope-summary line so consumers
            # see error / note / metadata rather than parse-empty stdout.
            carry = {k: v for k, v in result.items() if k != "results"}
            # Tag with record_class so it's symmetric with multi-mode.
            carry.setdefault("record_class", cal_type)
            print(_json.dumps(carry, default=str, ensure_ascii=False))
            return
        for row in rows:
            tagged = {"record_class": cal_type, **row}
            print(_json.dumps(tagged, default=str, ensure_ascii=False))
        return

    # csv
    _emit_single_csv(result)


def _emit_multi(envelopes: list[dict], fmt: str) -> None:
    """Emit a multi-type result (list of envelopes) in the requested
    format. JSON: list of envelopes. NDJSON / CSV: per-record with
    `record_class` discriminator."""
    if fmt == "json":
        print(_json.dumps(envelopes, indent=2, default=str, ensure_ascii=False))
        return

    if fmt == "ndjson":
        for env in envelopes:
            cal_type = env.get("type", "earnings")
            rows = env.get("results") or []
            if not rows:
                carry = {k: v for k, v in env.items() if k != "results"}
                carry.setdefault("record_class", cal_type)
                print(_json.dumps(carry, default=str, ensure_ascii=False))
                continue
            for row in rows:
                tagged = {"record_class": cal_type, **row}
                print(_json.dumps(tagged, default=str, ensure_ascii=False))
        return

    # csv
    _emit_multi_csv(envelopes)


def _emit_summary(envelopes: list[dict], fmt: str) -> None:
    """--summary path: emit one rollup dict per envelope. Single-type
    stays single-dict in JSON; multi-type becomes a list of dicts.
    NDJSON: one rollup per line. CSV: one row per type with all rollup
    keys flattened (nested counts dicts go in via JSON-encoded string)."""
    rolled = [summarize(env) for env in envelopes]

    if fmt == "json":
        if len(rolled) == 1:
            print(_json.dumps(rolled[0], indent=2,
                  default=str, ensure_ascii=False))
        else:
            print(_json.dumps(rolled, indent=2, default=str, ensure_ascii=False))
        return

    if fmt == "ndjson":
        for r in rolled:
            print(_json.dumps(r, default=str, ensure_ascii=False))
        return

    # csv: each rollup type has different keys; emit one row per type.
    # Nested dicts (count_by_timing, count_by_action, etc.) get
    # JSON-encoded into a single cell so the row stays flat. Less
    # ergonomic than per-type CSV but keeps the multi-type shape
    # consistent with the rest of the skill's CSV conventions.
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    # Compute union of all summary keys across rolled envelopes,
    # plus envelope metadata (type / start / end / etc.).
    meta_keys = ["type", "start", "end", "offset",
                 "filter_most_active", "market_cap_floor",
                 "note", *RESULT_META]
    summary_keys: list[str] = []
    for r in rolled:
        for k in (r.get("summary") or {}).keys():
            if k not in summary_keys:
                summary_keys.append(k)
    cols = [*meta_keys, *summary_keys]
    writer.writerow(cols)
    for r in rolled:
        s = r.get("summary") or {}
        row = []
        for c in cols:
            if c in summary_keys:
                v = s.get(c, "")
                if isinstance(v, (dict, list)):
                    v = _json.dumps(v, default=str, ensure_ascii=False)
                row.append(v)
            else:
                row.append(r.get(c, ""))
        writer.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch a Yahoo Finance market calendar — earnings, IPOs,\n"
            "splits, or economic events — over a date window. Single\n"
            "envelope per call (one HTTP, one result), NOT per-ticker.\n\n"
            "Default window: today through today+7. Default type:\n"
            "earnings (with Yahoo's most-active filter on, mirroring\n"
            "yfinance.Calendars defaults — disable with --no-most-active\n"
            "to see the full firehose, or raise the floor with\n"
            "--market-cap)."
        ),
        epilog=(
            "Examples:\n"
            "  # Earnings this week (default — most-active filter on)\n"
            "  calendars.py\n"
            "\n"
            "  # Earnings next 14 days, large caps only\n"
            "  calendars.py --days 14 --market-cap 10e9\n"
            "\n"
            "  # All earnings (disable most-active filter)\n"
            "  calendars.py --no-most-active --limit 100\n"
            "\n"
            "  # Upcoming IPOs in the next 30 days\n"
            "  calendars.py --type ipo --days 30 --limit 50\n"
            "\n"
            "  # Stock splits this week\n"
            "  calendars.py --type splits\n"
            "\n"
            "  # Economic events this week (CPI / FOMC / GDP / ...)\n"
            "  calendars.py --type economic --limit 50\n"
            "\n"
            "  # Specific date range\n"
            "  calendars.py --start 2026-06-01 --end 2026-06-15\n"
            "\n"
            "See references/calendars.md for the field schemas, units,\n"
            "and the most-active-filter caveats. SKILL.md covers\n"
            "cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Custom type for --type so we can: (1) accept case-insensitive
    # input (matches sec_filings --type convention), (2) accept
    # comma-separated multi-type for "what's happening this week"
    # rollups, (3) accept the alias `all` for all four types.
    valid_types = ("earnings", "ipo", "splits", "economic")

    def parse_types(raw: str) -> list[str]:
        if raw.lower().strip() == "all":
            return list(valid_types)
        out: list[str] = []
        for t in raw.split(","):
            t = t.strip().lower()
            if not t:
                continue
            if t not in valid_types:
                raise argparse.ArgumentTypeError(
                    f"unknown calendar type {t!r}; choose from "
                    f"{', '.join(valid_types)} (case-insensitive, "
                    f"comma-separated for multi; or `all`)"
                )
            if t not in out:  # de-dup
                out.append(t)
        if not out:
            raise argparse.ArgumentTypeError(
                "--type cannot be empty"
            )
        return out

    ap.add_argument(
        "--type", default=["earnings"], type=parse_types,
        help=(
            "Calendar type(s) to fetch. Case-insensitive. Accepts a\n"
            "single type, a comma-separated list, or `all` (alias for\n"
            "earnings,ipo,splits,economic).\n\n"
            "  earnings (default) — upcoming + recent earnings\n"
            "    announcements with EPS estimate / actual / surprise.\n"
            "  ipo — IPO calendar (filing / pricing / amendment dates,\n"
            "    price range, share count, action status).\n"
            "  splits — stock splits (forward and reverse, with\n"
            "    old:new ratio + derived `direction` field).\n"
            "  economic — macro events (CPI, FOMC, GDP, jobs, etc.)\n"
            "    with consensus / actual / prior + best-effort `unit`.\n\n"
            "Multi-type: each type is a separate HTTP call. JSON output\n"
            "becomes a list of envelopes; CSV / NDJSON tag each record\n"
            "with a `record_class` discriminator."
        ),
    )

    # Date window. Three mutually exclusive flavors:
    #   1. Explicit --start [+ --end | --days]
    #   2. --past-days N (today-N → today retrospective)
    #   3. nothing       (today → today+7 default)
    ap.add_argument(
        "--start", default=None, metavar="YYYY-MM-DD",
        help="Window start (inclusive). Default: today (UTC).",
    )
    end_group = ap.add_mutually_exclusive_group()
    end_group.add_argument(
        "--end", default=None, metavar="YYYY-MM-DD",
        help="Window end (inclusive). Default: --start + 7 days.",
    )
    end_group.add_argument(
        "--days", type=int, default=None, metavar="N",
        help=(
            "Window size in days from --start. Convenience alternative\n"
            "to --end. Mutually exclusive with --end and --past-days."
        ),
    )
    end_group.add_argument(
        "--past-days", dest="past_days", type=int, default=None, metavar="N",
        help=(
            "Retrospective scan: window = (today - N) through today.\n"
            "Useful for 'who reported last week' / 'IPOs in the past\n"
            "30 days' / 'recent macro releases'. When set, ignores\n"
            "--start (the window starts at today-N). Mutually\n"
            "exclusive with --end and --days."
        ),
    )

    ap.add_argument(
        "--limit", type=int, default=25, metavar="N",
        help=(
            "Max records to return (Yahoo caps at 100). Default 25.\n"
            "yfinance's underlying Calendars default is 12 (which is\n"
            "tiny for the question shapes calendars handles); we lift\n"
            "it slightly so the default invocation surfaces enough\n"
            "rows for a meaningful 'this week' scan."
        ),
    )
    ap.add_argument(
        "--offset", type=int, default=0, metavar="N",
        help=(
            "Pagination offset (default 0). NB: with --type earnings,\n"
            "passing offset > 0 silently disables Yahoo's most-active\n"
            "filter (yfinance / Yahoo limitation). The envelope's\n"
            "`filter_most_active` field reports the effective state."
        ),
    )

    # Earnings-only filters. Both are silently ignored for other types
    # (with a stderr warning) — we don't ap.error because the user may
    # be cycling --type via shell loop.
    ap.add_argument(
        "--market-cap", type=float, default=None, metavar="USD",
        help=(
            "Market-cap floor in USD (e.g. 1e10 for $10B). Earnings\n"
            "type only; filters Yahoo's data AND the most-active\n"
            "prescreen. Ignored for ipo / splits / economic with a\n"
            "stderr warning."
        ),
    )
    ap.add_argument(
        "--no-most-active", action="store_true",
        help=(
            "Disable Yahoo's most-active filter on earnings (default ON\n"
            "in yfinance — gives you ~200 most-active US tickers'\n"
            "earnings only; disable to see the full firehose). Ignored\n"
            "for non-earnings types with a stderr warning."
        ),
    )

    ap.add_argument(
        "--summary", action="store_true",
        help=(
            "Emit a per-type rollup (counts / aggregates) instead of\n"
            "the full event list. Earnings: count_with_estimate /\n"
            "count_reported / count_by_timing / avg_market_cap. IPO:\n"
            "count_by_action / unique_exchanges / count_with_offer_price.\n"
            "Splits: count_forward / count_reverse / count_with_options.\n"
            "Economic: count_by_region (top 10) / count_with_actual /\n"
            "count_by_unit. Pairs naturally with multi-type --type all\n"
            "for a 'what's happening this week' digest."
        ),
    )

    ap.add_argument(
        "--full", action="store_true",
        help=(
            "Emit the raw Yahoo column names (snake_cased but otherwise\n"
            "untouched) instead of the curated projection. Useful when\n"
            "Yahoo serves a field that isn't in our schema (e.g. the\n"
            "raw `Action` enum on IPO has variants we may not have\n"
            "documented). Incompatible with --format csv (raw keys\n"
            "would break column stability across calls — argparse\n"
            "rejects the combo); use --format ndjson with --full."
        ),
    )

    ap.add_argument(
        "--format", default="json", choices=("json", "ndjson", "csv"),
        help=(
            "Output format. json (default) = single envelope dict\n"
            "(metadata + results array); ndjson = one record per line\n"
            "(envelope dropped; on error / empty emits a single\n"
            "envelope-summary line); csv = one row per record (cols\n"
            "= type-specific schema + note + meta; on error / empty\n"
            "emits a single carry row). Multi-type: JSON emits a list\n"
            "of envelopes; CSV / NDJSON tag each record with a\n"
            "`record_class` discriminator (one of earnings / ipo /\n"
            "splits / economic)."
        ),
    )

    args = ap.parse_args()

    if args.limit < 1 or args.limit > 100:
        ap.error("--limit must be between 1 and 100 (Yahoo limit)")
    if args.offset < 0:
        ap.error("--offset must be >= 0")
    if args.days is not None and args.days < 1:
        ap.error("--days must be >= 1")
    if args.past_days is not None and args.past_days < 1:
        ap.error("--past-days must be >= 1")
    if args.full and args.format == "csv":
        ap.error(
            "--full is incompatible with --format csv (raw Yahoo keys "
            "would break CSV column stability across calls); use "
            "--format ndjson with --full, or drop --full for the "
            "projected CSV"
        )
    if args.summary and args.full:
        ap.error(
            "--summary and --full are mutually exclusive (--summary "
            "emits aggregates over projected fields; --full emits "
            "raw Yahoo rows — pick one)"
        )

    # Strict YYYY-MM-DD validation. Earlier we relied on
    # datetime.fromisoformat which accepts things like
    # `2026-06-01T12:00:00` — the help text said "YYYY-MM-DD" but the
    # parser was lenient. Tighten with a regex so the user gets a
    # clear error on accidentally-rich strings.
    for fld, val in (("--start", args.start), ("--end", args.end)):
        if val is None:
            continue
        if not _ISO_DATE_RE.match(val):
            ap.error(f"{fld} must be YYYY-MM-DD (got {val!r})")
        try:
            datetime.fromisoformat(val)
        except ValueError:
            ap.error(f"{fld} is not a real date (got {val!r})")

    # --past-days isn't compatible with --start (would ignore it
    # silently). Surface as a clean argparse error.
    if args.past_days is not None and args.start is not None:
        ap.error("--past-days cannot be combined with --start "
                 "(--past-days starts the window at today-N)")

    start, end = _resolve_dates(
        args.start, args.end, args.days, args.past_days,
    )

    # `args.type` is now a list (single or multi). Earnings-only flags
    # apply per-call: warn only if the user passed them but no earnings
    # type is present.
    types = args.type
    if "earnings" not in types:
        if args.market_cap is not None:
            print(
                f"warning: --market-cap ignored (no `earnings` in --type "
                f"{','.join(types)} — earnings-only filter)",
                file=sys.stderr,
            )
        if args.no_most_active:
            print(
                f"warning: --no-most-active ignored (no `earnings` in "
                f"--type {','.join(types)} — earnings-only filter)",
                file=sys.stderr,
            )

    # Fetch each type. Each is a separate HTTP call (or 2 for earnings
    # with default most-active filter). Order = user input order.
    envelopes = [
        fetch(
            cal_type=t,
            start=start,
            end=end,
            limit=args.limit,
            offset=args.offset,
            market_cap=args.market_cap if t == "earnings" else None,
            filter_most_active=(t == "earnings" and not args.no_most_active),
            full=args.full,
        )
        for t in types
    ]

    if args.summary:
        _emit_summary(envelopes, args.format)
    elif len(envelopes) == 1:
        _emit_single(envelopes[0], args.format)
    else:
        _emit_multi(envelopes, args.format)


if __name__ == "__main__":
    main()
