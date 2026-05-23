#!/usr/bin/env python3
"""Fetch yfinance Ticker.sec_filings for one or more tickers and print as
JSON / NDJSON / CSV.

See `sec_filings.py --help` for usage. Output is a JSON array on stdout,
one entry per ticker; failed / non-applicable tickers carry an "error" or
a "note" field instead of filings so a single bad symbol does not poison
the batch. Field schema lives in the *_KEYS / *_CSV_COLS constants below.
"""
from __future__ import annotations
import yfinance as yf
from helpers import (
    RESULT_META, emit_json_or_ndjson, epoch_to_date, safe_int, safe_str,
    with_retry,
)

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Yahoo's `sec_filings` covers SEC-registered securities. Empirically
# verified (2026-05) to return:
#   - non-empty list  for US-listed equities (AAPL, MSFT) and ADRs (TM)
#   - empty dict {}   for non-US primary listings (BMW.DE, 0700.HK), ETFs
#                     (SPY), mutual funds (VFIAX), indexes (^GSPC), crypto
#                     (BTC-USD), and bogus / delisted tickers
#
# yfinance's underlying call logs an HTTP 404 to its internal logger for
# the empty cases but does NOT raise — so with_retry sees success and the
# script must handle both `[]` and `{}` as "empty" downstream.
#
# An all-empty result is genuinely ambiguous — we can't distinguish
# "non-US primary listing" (BMW.DE) from "ETF" (SPY) from "crypto"
# (BTC-USD) from "bogus" (ZZZZNOTREAL) without an extra fast_info
# round-trip. Emit success-with-note rather than paying that pre-check;
# callers who need disambiguation can chain fast_info themselves.
#
# No `coverage_note` partial-empty path here — the SEC-filings endpoint
# is binary (filings exist or they don't). ADRs (verified TM) get full
# 6-K / 20-F coverage because they're SEC-registered foreign issuers,
# distinct from non-US primary listings which aren't SEC-registered at all.
_EMPTY_NOTE = (
    "no SEC filings (Yahoo's sec_filings endpoint covers SEC-registered "
    "securities; non-US primary listings / ETFs / mutual funds / indexes / "
    "crypto / FX / futures and bogus tickers all return empty — call "
    "fast_info to disambiguate. ADRs like TM ARE covered (6-K / 20-F).)"
)

# Per-filing fields, in emit order.
#
# Dropped from the raw payload:
#   epochDate    redundant with `date` (we project epoch → YYYY-MM-DD)
#   maxAge       Yahoo-internal cache hint, no consumer use
#
# Added projections (computed from `exhibits`):
#   primary_url    URL of the main filing document (exhibits[type] when
#                  present, else first exhibit value, else None) — the
#                  "click here to read the filing" surface. NB: for 8-K
#                  filings this points at the 8-K form itself (a thin
#                  SEC wrapper) rather than the typical EX-99.1 press
#                  release that contains the substantive announcement.
#                  Pull `exhibits["EX-99.1"]` directly when you want
#                  the press release.
#   exhibit_count  len(exhibits) — number of attached documents
#   exhibit_keys   pipe-joined list of exhibit keys (e.g.
#                  "10-Q|EX-31.1|EX-31.2|EX-32.1") — flat surface for
#                  CSV consumers that can't read the nested `exhibits`
#                  dict. Order matches the dict's insertion order
#                  (Yahoo's emission order, primary doc first).
#
# `exhibits` itself is preserved as a nested dict (variable keys per
# filing type — typical 10-Q has `{10-Q, EX-31.1, EX-31.2, EX-32.1}`,
# typical 8-K has `{8-K, EX-99.1}`). Kept in JSON / NDJSON output but
# dropped from CSV (a nested dict has no clean tabular projection;
# `primary_url` + `exhibit_count` + `exhibit_keys` carry the headline
# signals).
_FILING_KEYS = (
    "date", "type", "title",
    "edgar_url", "primary_url", "exhibit_count", "exhibit_keys",
    "exhibits",  # dict — JSON-only; dropped from CSV cols below
)

# Default-mode CSV columns. Drops the nested `exhibits` dict (no clean
# tabular shape); keeps the flat projections (`primary_url`,
# `exhibit_count`, `exhibit_keys`) so CSV consumers still get the
# actionable URL + size + per-exhibit-key signals. Empty / errored /
# filtered-to-empty tickers emit a single carry row with `note` /
# `filter_note` + meta.
_DEFAULT_CSV_COLS = (
    "symbol",
    *(k for k in _FILING_KEYS if k != "exhibits"),
    "note", "filter_note", *RESULT_META,
)

# Per-ticker fields that carry through to every CSV row for that ticker
# (data rows AND the empty-result row). Same convention as news.py /
# holders.py — see SKILL.md "`note` field convention" for the cross-mode
# contract. `filter_note` is sec_filings-specific: it's set when the
# ticker fetched successfully but `--type` / `--since` / `--days` ate
# every row (mutually exclusive with `note`, which is reserved for "no
# data from Yahoo at all").
_CSV_CARRY_KEYS = (*RESULT_META, "note", "filter_note")

# --- summary mode ---
#
# Headline filing types we surface as `latest_*_date` fields. Picked to
# cover both US-issuer (10-K / 10-Q / 8-K) and foreign-issuer ADR
# (20-F / 6-K) reporting cycles so peer-compare across mixed cohorts
# (e.g. AAPL + TM) doesn't silently drop ADRs to all-None summary rows.
# Proxy bucket (DEF 14A + DEFA14A) included as a governance-cycle signal.
#
# Each entry maps a SET of Yahoo form types to one summary field — most
# headline buckets are 1:1 (single Yahoo form) but proxy is 1:many
# (`DEF 14A` is the formal annual proxy; `DEFA14A` is supplemental
# materials filed in the same cycle, often near record-date or M&A
# vote). Bucketing both into `latest_proxy_date` means consumers asking
# "when did X last file proxy materials" get the most recent of either,
# which is what the question usually means. If a stricter "definitive
# proxy only" date is needed, fall back to default mode + `--type
# "DEF 14A"`.
#
# Order is the emit order of the `latest_*_date` fields in the flat dict.
_HEADLINE_TYPES = (
    (frozenset({"10-K"}),              "latest_10k_date"),
    (frozenset({"10-Q"}),              "latest_10q_date"),
    (frozenset({"8-K"}),               "latest_8k_date"),
    (frozenset({"20-F"}),              "latest_20f_date"),
    (frozenset({"6-K"}),               "latest_6k_date"),
    (frozenset({"DEF 14A", "DEFA14A"}), "latest_proxy_date"),
)

# Recency window for the `filings_last_90d` count. 90d covers the standard
# 10-Q + 8-K cadence for a typical large-cap quarter (~5–10 filings) and
# is short enough to flag genuine activity bumps (M&A, mat. events) rather
# than just regulatory baseline.
_RECENCY_WINDOW_DAYS = 90

# Build the per-bucket field list. Each headline bucket emits a
# `latest_*_date` field; multi-form buckets (currently only `proxy`,
# which holds {DEF 14A, DEFA14A}) ALSO emit a `latest_*_type` companion
# naming which Yahoo form code won — so consumers can distinguish "the
# annual definitive proxy" from "supplemental materials" within the
# bucket. Single-form buckets (10-K, 10-Q, ...) skip the companion
# because the type is always the bucket constant — redundant.
#
# Naming convention asymmetry intentional: form-specific buckets keep
# their familiar form name (`latest_10k_date`); multi-form buckets get
# a category name (`latest_proxy_date`) plus a `_type` companion to
# recover the form. Documented in references/sec_filings.md.


def _build_headline_fields() -> tuple[str, ...]:
    fields: list[str] = []
    for type_bucket, field_name in _HEADLINE_TYPES:
        fields.append(field_name)
        if len(type_bucket) > 1:
            fields.append(field_name.replace("_date", "_type"))
    return tuple(fields)


_HEADLINE_TYPE_FIELDS = _build_headline_fields()

_SUMMARY_FLAT_KEYS = (
    "total_filings",
    "latest_date",
    "latest_type",
    *_HEADLINE_TYPE_FIELDS,
    f"filings_last_{_RECENCY_WINDOW_DAYS}d",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS,
                     "note", "filter_note", *RESULT_META)


def _project_filing(item: dict) -> dict:
    """Pull the LLM-relevant fields out of one Yahoo filing dict.

    Robust to missing keys and non-dict shapes (Yahoo has been observed
    to occasionally emit list elements that are dicts of dicts; we guard
    `.get` access throughout).

    `date` strategy: prefer Yahoo's pre-formatted `date` string when it's
    a clean YYYY-MM-DD, else fall back to converting `epochDate`. Both
    fields are always present in observed payloads — the fallback is
    insurance against a future Yahoo change that drops one or the other.
    """
    raw_date = safe_str(item.get("date"))
    # Heuristic: a clean YYYY-MM-DD is exactly 10 chars with dashes at 4/7.
    # Anything else (timestamp, "N/A", etc.) falls through to epoch decode.
    if raw_date and len(raw_date) == 10 and raw_date[4] == "-" and raw_date[7] == "-":
        date = raw_date
    else:
        date = epoch_to_date(item.get("epochDate"))

    exhibits_raw = item.get("exhibits") or {}
    if not isinstance(exhibits_raw, dict):
        exhibits_raw = {}
    # Normalize values to strings; drop any non-URL entries silently.
    exhibits = {safe_str(k): safe_str(v)
                for k, v in exhibits_raw.items()
                if safe_str(k) and safe_str(v)}

    filing_type = safe_str(item.get("type"))
    # primary_url heuristic: prefer exhibits[type] (the main filing doc by
    # convention — verified across AAPL / MSFT / TM 2026-05 payloads),
    # else the first exhibit value, else None. The `next(iter(...))` path
    # gives us the first-inserted exhibit on Python 3.7+ (insertion-ordered
    # dicts) — Yahoo lists exhibits with the primary doc first in observed
    # payloads, so this is a sensible fallback.
    primary_url = None
    if filing_type and filing_type in exhibits:
        primary_url = exhibits[filing_type]
    elif exhibits:
        primary_url = next(iter(exhibits.values()))

    # `exhibit_keys`: pipe-joined string of exhibit keys in dict insertion
    # order (= Yahoo's emission order, primary doc first). Empty string
    # when no exhibits — chose empty over None so CSV consumers can do
    # exact-match string filters (`exhibit_keys` containing `EX-99.1`)
    # without nullable-string handling.
    exhibit_keys = "|".join(exhibits.keys()) if exhibits else ""

    return {
        "date": date,
        "type": filing_type,
        "title": safe_str(item.get("title")),
        "edgar_url": safe_str(item.get("edgarUrl")),
        "primary_url": primary_url,
        "exhibit_count": len(exhibits) if exhibits else 0,
        "exhibit_keys": exhibit_keys,
        "exhibits": exhibits or None,
    }


def fetch(symbol: str) -> dict:
    """Fetch SEC filings for `symbol` and project to JSON-friendly dicts.

    No `--limit` / `--type` filtering applied here — callers slice via
    `_apply_filters` (default mode) or read from full list (`--summary`),
    matching the holders / insiders pattern: filtering describes the
    display, summary metrics describe Yahoo's response.
    """
    raw, err_kind, attempts = with_retry(lambda: yf.Ticker(symbol).sec_filings)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    # yfinance returns either a list (US-listed equity / ADR with filings)
    # or an empty dict `{}` (everything else). Treat both `[]` and `{}` as
    # "empty" — the empty case is genuinely ambiguous; emit success-with-
    # note rather than guessing the cause.
    is_empty = (not raw) or (isinstance(raw, dict) and not raw)
    if is_empty:
        out = {
            "symbol": symbol,
            "count": 0,
            "filings": [],
            "note": _EMPTY_NOTE,
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out

    # Defensive: if Yahoo ever emits a non-list non-empty shape, bail to
    # the empty-with-note path rather than crashing on iteration.
    if not isinstance(raw, list):
        out = {
            "symbol": symbol,
            "count": 0,
            "filings": [],
            "note": (f"unexpected sec_filings shape (got "
                     f"{type(raw).__name__}, expected list) — possible "
                     f"yfinance API drift"),
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out

    filings = [_project_filing(it) for it in raw if isinstance(it, dict)]
    out = {
        "symbol": symbol,
        "count": len(filings),
        "filings": filings,
    }
    if attempts > 1:
        out["attempts"] = attempts
    return out


def _apply_filters(result: dict, *, types: set[str] | None,
                   since: str | None, limit: int | None) -> dict:
    """Filter and truncate the filings list IN-PLACE.

    **Mutates `result`.** Same contract as `holders._apply_limit` /
    `insiders._apply_limit`: the dict object is modified directly; the
    return value is the same object (for fluent chaining). If you need
    to preserve the original, deep-copy first.

    Filter precedence: `since` first (date floor), then `types` (form
    classification), then `limit` (slice). Date and type filters are
    intersected (a filing must satisfy both); `limit` then truncates.
    So `--since 2024-01-01 --type 10-K --limit 1` yields the most
    recent 10-K filed on or after 2024-01-01.

    `count` is **NOT mutated** — it stays = `len(filings_before_filter)`
    (Yahoo's response size, set by `fetch()`). Use `len(result["filings"])`
    if you need the displayed count. Rationale: `count` is the only
    place the user can recover Yahoo's original response size in default
    mode (summary mode lifts it to `total_filings`); mutating it would
    silently lose that signal once filters apply.

    `filter_note` is set when the input had filings but filters reduced
    them to zero (mutually exclusive with `note`, which is reserved for
    "Yahoo returned no data at all"). Lets CSV / human consumers
    distinguish "TM has no 10-K because it's an ADR" from "TM has no
    SEC coverage at all". The note text names the **specific filter
    that took the count from positive to zero** — the first
    list-zeroing step wins; subsequent filters running on an
    already-empty list don't get blamed.

    `types` matching is **case-insensitive**: callers pass uppercase
    via `_parse_types_arg`; this function uppercases each filing's
    `type` for comparison. Yahoo emits mixed case (`10-K`, `DEF 14A`)
    but case-fold matching avoids the silent-zero-results footgun
    when users type `--type 10-k`.

    Used only by the default-mode emit path. `--summary` mode never
    calls this — its flat metrics are computed from the full pre-filter
    list so they describe Yahoo's response, not the display knob. (Same
    invariant as holders / insiders.) No-op when all args are None.
    """
    if "filings" not in result:
        return result
    filings = result["filings"]
    original_count = len(filings)

    # Track which filter step took the count from positive to zero.
    # The `filter_note` text names only that culprit (rather than every
    # applied filter), so users see the exact knob that ate their
    # data — useful for debugging "I expected results but got nothing"
    # in chained filters like `--since X --type Y`.
    filter_culprit: str | None = None
    prev_count = original_count

    if since is not None:
        # Lexicographic compare on YYYY-MM-DD is correct (zero-padded).
        # Filings missing a date are dropped — they can't be confirmed
        # to be on/after the cutoff. Defensive for a future Yahoo
        # change that drops the date field on some rows.
        filings = [f for f in filings
                   if f.get("date") and f["date"] >= since]
        if prev_count > 0 and not filings:
            filter_culprit = f"--since {since}"
        prev_count = len(filings)

    if types is not None:
        # Case-insensitive: uppercase both sides. `types` is already
        # uppercase from `_parse_types_arg`; we uppercase each filing's
        # `type` here. Filings with no type are dropped (can't classify).
        filings = [f for f in filings
                   if f.get("type") and f["type"].upper() in types]
        if prev_count > 0 and not filings:
            filter_culprit = f"--type {','.join(sorted(types))}"
        prev_count = len(filings)

    if limit is not None:
        # `--limit >= 1` (validated in main); slicing a non-empty list
        # by `[:N]` keeps at least one row. So limit can never be the
        # culprit that zeroes the count — no culprit-tracking branch.
        filings = filings[:limit]
    result["filings"] = filings

    # `filter_note`: only fires when filters ate everything from a
    # non-empty starting set. (If `filings` was already empty before
    # filtering, the caller's `note` already carries the right signal.)
    # The culprit is guaranteed to be set in this branch — only since
    # / type can reduce a non-empty list to empty, and at least one
    # of them must have done so for us to get here.
    if original_count > 0 and not filings:
        result["filter_note"] = (
            f"{original_count} filings fetched, all eliminated by "
            f"{filter_culprit} (filings exist; filter excluded everything)"
        )
    return result


def _summarize(full: dict) -> dict:
    """Flat per-ticker projection for peer comparison.

    Reads from the FULL (pre-filter, pre-limit) filings list so all flat
    metrics describe Yahoo's response, not the display knob — same
    invariant as holders / insiders.

    Carries `note` / `error` / `error_kind` / `attempts` through unchanged
    so empty / failed tickers still surface in summary CSVs rather than
    collapsing to all-None rows that look identical to a real low-coverage
    ticker.
    """
    out = {"symbol": full["symbol"],
           **{k: None for k in _SUMMARY_FLAT_KEYS}}
    out["total_filings"] = full.get("count", 0)
    filings = full.get("filings") or []

    # Recency rollup: count filings within the last N days. Computed even
    # when `filings` is empty so empty tickers get integer 0 (a known
    # answer) rather than null (unknown) — matches the holders /
    # insiders convention where `*_returned` counts are 0 not None for
    # empty cases. Uses today in UTC — a 1-day timezone smear at the
    # edge is fine for this bucket count.
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=_RECENCY_WINDOW_DAYS)).isoformat()
    out[f"filings_last_{_RECENCY_WINDOW_DAYS}d"] = sum(
        1 for f in filings if f.get("date") and f["date"] >= cutoff
    )

    if filings:
        # Most-recent overall: scan all filings rather than trusting Yahoo's
        # sort order. Observed sort is desc but we don't promise that — a
        # silent sort flip shouldn't make latest_date go backwards.
        with_date = [f for f in filings if f.get("date")]
        if with_date:
            top = max(with_date, key=lambda f: f["date"])
            out["latest_date"] = top["date"]
            out["latest_type"] = top.get("type")

        # Per-headline-type latest dates. For each bucket, pick the
        # filing with the max date among filings whose type is in the
        # bucket. None if no filing matches — common for ADRs (no
        # 10-K / 10-Q / 8-K) and US issuers (no 20-F / 6-K). Most
        # buckets are 1:1 (single Yahoo form); `latest_proxy_date`
        # collects {DEF 14A, DEFA14A} — see _HEADLINE_TYPES rationale.
        # For multi-form buckets we also surface a `latest_*_type`
        # companion (the form code that won) so consumers can tell
        # which member of the bucket is the latest.
        for type_bucket, field_name in _HEADLINE_TYPES:
            bucket_filings = [(f["date"], f["type"]) for f in filings
                              if f.get("type") in type_bucket and f.get("date")]
            if bucket_filings:
                top_date, top_type = max(bucket_filings, key=lambda p: p[0])
                out[field_name] = top_date
                if len(type_bucket) > 1:
                    out[field_name.replace("_date", "_type")] = top_type

    # Carry-through for the result-level signaling fields. `filter_note`
    # shouldn't normally appear in a `full` dict passed to _summarize
    # (the main path runs _summarize on un-filtered fetch results), but
    # carrying it through defensively means a caller who manually
    # composes filter + summarize doesn't silently lose the signal.
    for k in ("note", "filter_note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def _parse_types_arg(arg: str | None) -> set[str] | None:
    """Parse the `--type` arg into a set of UPPERCASE filing-type strings.

    Comma-separated. **Case-insensitive**: each token is upper-cased so
    user input `10-k`, `10-K`, `Def 14a` all match Yahoo's emission
    (`10-K`, `DEF 14A`). Whitespace around commas is stripped;
    whitespace WITHIN a type is preserved (`DEF 14A` is two tokens
    internally and stays so after upper-casing).

    Returns `None` for None / empty / whitespace-only inputs so
    `_apply_filters` skips the type-filter branch entirely (faster than
    matching against an empty set).
    """
    if arg is None:
        return None
    types = {t.strip().upper() for t in arg.split(",") if t.strip()}
    return types or None


def _parse_since_arg(arg: str | None, days: int | None) -> str | None:
    """Resolve `--since` / `--days` to a single YYYY-MM-DD cutoff string.

    Mutually exclusive at the argparse level — only one will be set.
    Returns `None` when both are None (no date filter). For `--days N`,
    cutoff = today (UTC) - N days. For `--since`, the input is parsed
    as ISO date or datetime and **normalized to YYYY-MM-DD** before
    return — without normalization, an input like `2024-01-01T00:00:00`
    would be returned verbatim and the downstream string comparison
    `"2024-01-01" >= "2024-01-01T00:00:00"` would silently exclude
    same-day filings (Python: shorter string < longer string with
    matching prefix). Discard the time component, keep only the
    calendar date.

    Why UTC for `--days N`: Yahoo emits filing dates as exchange-local
    YYYY-MM-DD strings (US filings use ET, ADRs in their home tz). UTC
    today is within ±1 day of any exchange's local today. A 1-day
    smear at the cutoff edge is fine for "filings in the last N days"
    — the metric is a rough recency signal, not a precise cohort.
    Same convention as `_summarize`'s `filings_last_90d`.

    Raises ValueError on bad input — caller (argparse) surfaces as
    a usage error.
    """
    if days is not None:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        return cutoff.isoformat()
    if arg is not None:
        # Parse as either ISO date or ISO datetime, then normalize to
        # the calendar date string. `fromisoformat` accepts both forms
        # since Python 3.7+. Discarding the time component fixes the
        # boundary-exclusion bug that bit `--since 2024-01-01T00:00:00`.
        return datetime.fromisoformat(arg).date().isoformat()
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch Yahoo Finance SEC filings for one or more tickers.\n\n"
            "One section per ticker — the `filings` list. Each filing has\n"
            "date / type / title / edgar_url / primary_url / exhibit_count\n"
            "/ exhibit_keys / exhibits dict. Yahoo returns up to ~75-120\n"
            "filings going back ~4 years for active issuers; use --since\n"
            "or --days for a date floor, --type to narrow by form, and\n"
            "--limit to cap rows.\n\n"
            "Coverage: SEC-registered securities only. US-listed equities\n"
            "(AAPL: 10-K/10-Q/8-K/...) and ADRs (TM: 6-K/20-F) get full\n"
            "coverage. Non-US primary listings (BMW.DE, 0700.HK), ETFs,\n"
            "mutual funds, indexes, crypto, FX, futures, and bogus tickers\n"
            "all return empty (success-with-`note`, ambiguous — chain\n"
            "fast_info to disambiguate). No `coverage_note` partial-empty\n"
            "path: the SEC-filings endpoint is binary.\n\n"
            "CSV default-mode output is one row per FILING (symbol col\n"
            "repeats). The nested `exhibits` dict is dropped from CSV;\n"
            "`primary_url` + `exhibit_count` + `exhibit_keys` (pipe-joined\n"
            "list of exhibit keys, e.g. '10-Q|EX-31.1|EX-31.2|EX-32.1')\n"
            "carry the actionable signals. Empty tickers (Yahoo returned\n"
            "nothing) emit a single carry row with `note`. Filtered-to-\n"
            "empty tickers (Yahoo returned data, --type/--since/--days\n"
            "ate every row) emit a single carry row with `filter_note`\n"
            "instead — mutually exclusive with `note`, distinguishes\n"
            "'TM has no 10-K because it's an ADR' from 'TM has no SEC\n"
            "coverage at all'.\n\n"
            "UNITS: dates are YYYY-MM-DD strings (UTC-derived from epoch\n"
            "fallback). exhibit_count is integer. primary_url is a string\n"
            "(Yahoo CDN URL — direct doc link, no auth). NB: for 8-K\n"
            "filings, primary_url points at the 8-K form itself (a thin\n"
            "SEC wrapper); the substantive press release is usually at\n"
            "exhibits['EX-99.1'] — pull that directly when you want the\n"
            "announcement text."
        ),
        epilog=(
            "Examples:\n"
            "  sec_filings.py AAPL                                  # all filings, full schema\n"
            "  sec_filings.py --limit 5 AAPL                        # most recent 5\n"
            "  sec_filings.py --type 10-K,10-Q AAPL                 # quarterly + annual only (case-insensitive)\n"
            "  sec_filings.py --type 8-K --limit 10 TSLA            # last 10 events\n"
            "  sec_filings.py --since 2024-01-01 AAPL               # filings on/after 2024-01-01\n"
            "  sec_filings.py --days 30 --type 8-K AAPL TSLA NVDA   # 8-Ks in last 30 days\n"
            "  sec_filings.py --summary AAPL MSFT NVDA              # peer compare\n"
            "  sec_filings.py --summary --format csv AAPL TM        # mixed US issuer + ADR\n"
            "  sec_filings.py --format csv AAPL                     # one row per filing\n"
            "\n"
            "See references/sec_filings.md for the field schema, presentation\n"
            "guidance, and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap filings per ticker (default: keep all, "
                         "Yahoo returns up to ~75-120 going back ~4 "
                         "years for active issuers). Applied AFTER "
                         "--since and --type, so `--since 2024-01-01 "
                         "--type 10-K --limit 1` yields the most recent "
                         "10-K filed on or after 2024-01-01. "
                         "Ignored in --summary mode (warns on stderr) — "
                         "the flat metrics (total_filings, latest_*_date, "
                         "filings_last_90d) are computed from Yahoo's full "
                         "response and stay invariant under --limit by "
                         "design.")
    ap.add_argument("--type", default=None, metavar="T1[,T2,...]",
                    help="Comma-separated filing types to include. "
                         "**Case-insensitive** — `--type 10-k` and "
                         "`--type 10-K` both match Yahoo's `10-K`. "
                         "Whitespace inside a type is preserved "
                         "(`DEF 14A` is two tokens internally). Default: "
                         "include all. Common types: 10-K (annual), "
                         "10-Q (quarterly), 8-K (event), DEF 14A (proxy), "
                         "SC 13G/A (ownership), 20-F (ADR annual), 6-K "
                         "(ADR interim). Ignored in --summary mode "
                         "(warns on stderr) for the same reason as "
                         "--limit.")
    # --since / --days are mutually exclusive — both express a date floor
    # but in different units (ISO date vs. day count). Mixing them would
    # need a precedence rule that would surprise someone; the argparse
    # group rejects the combo at parse time with a usage error.
    since_group = ap.add_mutually_exclusive_group()
    since_group.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                             help="Include only filings dated on or after "
                                  "this date. Accepts ISO date or datetime "
                                  "(`2024-01-01` or `2024-01-01T00:00:00`); "
                                  "the time component is discarded and "
                                  "the date normalized to YYYY-MM-DD. "
                                  "Lexicographic compare on the normalized "
                                  "date — exact and tz-agnostic. Ignored "
                                  "in --summary mode (warns on stderr).")
    since_group.add_argument("--days", type=int, default=None, metavar="N",
                             help="Include only filings dated within the "
                                  "last N days (UTC today minus N days, "
                                  "inclusive). Convenience shortcut for "
                                  "--since computed from today. Mutually "
                                  "exclusive with --since. Ignored in "
                                  "--summary mode (warns on stderr).")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: total_filings + "
                         "latest_date / latest_type + per-headline-type "
                         "latest dates (10-K, 10-Q, 8-K, 20-F, 6-K, DEF "
                         "14A) + filings_last_90d recency count. Useful "
                         "for peer-comparison tables; ~10× smaller than "
                         "default. Same network cost as default mode "
                         "(post-fetch projection); use it to save "
                         "context tokens, not time.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = default mode emits one row per FILING (the "
                         "nested exhibits dict is dropped; primary_url + "
                         "exhibit_count carry the headline signals). "
                         "--summary csv emits strict one-row-per-ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")
    if args.days is not None and args.days < 1:
        ap.error("--days must be >= 1")
    types = _parse_types_arg(args.type)
    try:
        since = _parse_since_arg(args.since, args.days)
    except ValueError as e:
        ap.error(f"--since must be a YYYY-MM-DD date ({e})")

    results = [fetch(s.strip().upper())
               for s in args.symbols if s.strip()]

    if args.summary:
        # _summarize reads from the full pre-filter pre-limit list, so
        # --type / --since / --days / --limit are no-ops in this branch
        # by design — the metrics describe Yahoo's response, not the
        # display knobs. Warn on stderr (not error) if the user passed
        # any of those alongside --summary, since this is almost
        # certainly a logic mistake (they probably wanted default mode).
        # Same noise convention as screener.py's --predefined +
        # --quote-type warning.
        ignored = []
        if args.type is not None:
            ignored.append("--type")
        if args.since is not None:
            ignored.append("--since")
        if args.days is not None:
            ignored.append("--days")
        if args.limit is not None:
            ignored.append("--limit")
        if ignored:
            print(f"warning: --summary ignores {', '.join(ignored)} "
                  f"(summary metrics describe Yahoo's full response, "
                  f"not the display knobs); drop --summary if you "
                  f"want filtered output.",
                  file=sys.stderr)
        results = [_summarize(r) for r in results]
        _emit_summary(results, args.format)
    else:
        results = [_apply_filters(r, types=types, since=since, limit=args.limit)
                   for r in results]
        _emit_default(results, args.format)


def _emit_default(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # CSV: row-per-filing. The nested `exhibits` dict is dropped (no
    # clean tabular shape); `primary_url` + `exhibit_count` +
    # `exhibit_keys` carry the actionable signals. Three empty paths
    # all emit a single carry row instead of data rows:
    #   - error path                            → carries `error*` cols
    #   - Yahoo-empty path (`note` set)         → carries `note` col
    #   - filter-to-empty path (`filter_note`)  → carries `filter_note`
    # `note` and `filter_note` are mutually exclusive at the result
    # level (set by `fetch()` and `_apply_filters` respectively).
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        filings = r.get("filings") or []
        carry = {k: r[k] for k in _CSV_CARRY_KEYS if k in r}
        if not filings:
            writer.writerow([{"symbol": symbol, **carry}.get(c, "")
                            for c in cols])
            continue
        for f in filings:
            # Drop `exhibits` dict before merge (it's not in cols anyway,
            # but explicit drop keeps the row dict tidy and avoids ever
            # leaking a dict object through the CSV writer's str()).
            row_data = {k: v for k, v in f.items() if k != "exhibits"}
            writer.writerow([{"symbol": symbol, **row_data, **carry}.get(c, "")
                             for c in cols])


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
