#!/usr/bin/env python3
"""Browse Yahoo Finance's sector / industry hierarchy.

See `sectors.py --help` for usage. Unlike the per-ticker wrappers,
sectors works on Yahoo's curated hierarchy:

    11 sectors  →  ~150 industries  →  companies / ETFs / funds

One mode covers both `Sector` and `Industry` because their APIs are
~80% overlapping (overview, top_companies, research_reports). The
`--kind` flag picks which class to instantiate; `auto` (default)
infers from the key — sector keys are a fixed set of 11 known to
yfinance.const.

Sections (all opt-in via --section, default = overview,top_companies):

  Sector-only:     industries, top_etfs, top_mutual_funds
  Industry-only:   top_performing_companies, top_growth_companies
  Common:          overview, top_companies, research_reports

**Cost is 1 HTTP per key, regardless of --section count.** All
sections share one cached Yahoo endpoint per `yf.Sector(key)` /
`yf.Industry(key)` instance (verified 2026-05). `--section` only
affects projection / output cost, not network cost — `--section all`
costs the same network-wise as `--section overview`.

Output is a list of envelopes (one per key), same shape as the per-
ticker wrappers. NDJSON / CSV use a `record_class` discriminator to
pull the multi-section payload into a flat row stream — same pattern
as `fund_holdings.py` and `calendars.py --type all`.
"""
from __future__ import annotations
from yfinance.const import SECTOR_INDUSTY_MAPPING_LC
import yfinance as yf
import pandas as pd
from helpers import (
    RESULT_META,
    safe_float,
    safe_int,
    safe_str,
    with_retry,
)

import argparse
import contextlib
import csv as _csv
import io
import json as _json
import sys
import warnings
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Canonical key sets, lifted from yfinance.const so we stay aligned with
# Yahoo's accepted vocabulary. Sector keys are 11 fixed values; industry
# keys are ~150. Used for: (1) --kind auto inference, (2) --list-sectors
# / --list-industries enumeration, (3) early validation so a typo gets a
# clean argparse error rather than a 404 after a network round-trip.
#
# yfinance.const quirk: a handful of industry keys carry a unicode
# em-dash (U+2014, e.g. `software—application`), but Yahoo's actual
# industry endpoint requires a regular hyphen (`software-application`).
# Verified 2026-05: `Industry('software—application').overview` → None;
# `Industry('software-application').overview` → real data. Normalize on
# both ingest (const → API form) and on user input (paste-tolerant).
def _normalize_key(k: str) -> str:
    """Em-dash → hyphen + strip + lowercase. Idempotent."""
    return k.replace("—", "-").strip().lower()


SECTOR_KEYS = tuple(sorted(_normalize_key(k)
                    for k in SECTOR_INDUSTY_MAPPING_LC))
INDUSTRY_KEYS = tuple(sorted({
    _normalize_key(ind)
    for inds in SECTOR_INDUSTY_MAPPING_LC.values() for ind in inds
}))
INDUSTRY_TO_SECTOR = {
    _normalize_key(ind): _normalize_key(sec)
    for sec, inds in SECTOR_INDUSTY_MAPPING_LC.items()
    for ind in inds
}


# Per-kind valid sections. Common sections (`overview`, `top_companies`,
# `research_reports`) live in both; the asymmetric sections live in
# only one.
SECTOR_SECTIONS = (
    "overview", "top_companies", "industries",
    "top_etfs", "top_mutual_funds", "research_reports",
)
INDUSTRY_SECTIONS = (
    "overview", "top_companies",
    "top_performing_companies", "top_growth_companies",
    "research_reports",
)
ALL_SECTIONS = tuple(sorted(set(SECTOR_SECTIONS) | set(INDUSTRY_SECTIONS)))
DEFAULT_SECTIONS = ("overview", "top_companies")


def _kind_for_key(key: str) -> str | None:
    """Auto-infer kind from a key. Returns 'sector', 'industry', or None
    (unknown — caller should error out with a hint). Normalizes em-dash
    → hyphen so a paste from Yahoo's UI works."""
    k = _normalize_key(key)
    if k in SECTOR_KEYS:
        return "sector"
    if k in INDUSTRY_KEYS:
        return "industry"
    return None


def _project_overview(raw: dict | None) -> dict | None:
    """Project Yahoo's overview dict to a stable schema. Yahoo emits
    market_weight as a fraction (0.318 = 31.8%) — preserve as-is so
    the unit convention matches `holders.summary` etc.
    """
    if not raw:
        return None
    return {
        "companies_count":   safe_int(raw.get("companies_count")),
        "industries_count":  safe_int(raw.get("industries_count")),
        "market_cap":        safe_int(raw.get("market_cap")),
        "market_weight":     safe_float(raw.get("market_weight")),
        "employee_count":    safe_int(raw.get("employee_count")),
        "message_board_id":  safe_str(raw.get("message_board_id")),
        "description":       safe_str(raw.get("description")),
    }


def _df_to_records(df, *, index_name: str, limit: int | None,
                   col_renames: dict[str, str] | None = None,
                   numeric_cols: tuple[str, ...] = ()) -> list[dict]:
    """DataFrame → list of records, with `index_name` lifted into the row.

    yfinance returns these section DataFrames with the symbol / key as
    the index — we reset_index so it's just another column. Column names
    use spaces ("market weight", "ytd return") which we snake_case via
    `col_renames` for stable JSON keys. Numeric columns are coerced
    through safe_float to drop NaN/Inf.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    df = df.reset_index()
    if limit is not None:
        df = df.head(limit)
    renames = {"index": index_name}
    renames.update(col_renames or {})
    df = df.rename(columns=renames)
    records = df.to_dict("records")
    out: list[dict] = []
    for r in records:
        clean: dict = {}
        for k, v in r.items():
            if k in numeric_cols:
                clean[k] = safe_float(v)
            elif isinstance(v, float):
                clean[k] = safe_float(v)
            else:
                clean[k] = safe_str(v) if v is not None else None
        out.append(clean)
    return out


def _project_top_companies(df, limit: int | None) -> list[dict]:
    return _df_to_records(
        df, index_name="symbol", limit=limit,
        col_renames={"market weight": "market_weight"},
        numeric_cols=("market_weight",),
    )


def _project_industries(df, limit: int | None) -> list[dict]:
    # `industries` DataFrame index is the industry key, not a symbol.
    return _df_to_records(
        df, index_name="key", limit=limit,
        col_renames={"market weight": "market_weight"},
        numeric_cols=("market_weight",),
    )


def _project_top_performing(df, limit: int | None) -> list[dict]:
    return _df_to_records(
        df, index_name="symbol", limit=limit,
        col_renames={
            "ytd return":   "ytd_return",
            "last price":   "last_price",
            "target price": "target_price",
        },
        numeric_cols=("ytd_return", "last_price", "target_price"),
    )


def _project_top_growth(df, limit: int | None) -> list[dict]:
    # `growth estimate` is a multiple in observed payloads (MU = 5.84
    # ≈ 584% YoY off a low base); we preserve the raw value rather than
    # asserting a fraction/percent. See references/sectors.md units.
    return _df_to_records(
        df, index_name="symbol", limit=limit,
        col_renames={
            "ytd return":      "ytd_return",
            "growth estimate": "growth_estimate",
        },
        numeric_cols=("ytd_return", "growth_estimate"),
    )


def _project_top_funds(raw: dict | None, limit: int | None) -> list[dict]:
    """Yahoo returns top_etfs / top_mutual_funds as `{symbol: name}`
    dicts. Convert to a list of records for shape consistency with the
    other sections. `name` can be None (verified in observed payloads —
    Yahoo sometimes omits the name for less-popular funds).
    """
    if not raw or not isinstance(raw, dict):
        return []
    items = list(raw.items())
    if limit is not None:
        items = items[:limit]
    return [
        {"symbol": safe_str(sym), "name": safe_str(name)}
        for sym, name in items
    ]


def _project_research_reports(raw: list | None, limit: int | None) -> list[dict]:
    """Yahoo's research_reports is a list of mixed-key dicts. We project
    a stable subset; unknown fields are dropped (use --full for raw).
    """
    if not raw or not isinstance(raw, list):
        return []
    if limit is not None:
        raw = raw[:limit]
    return [
        {
            "id":                  safe_str(r.get("id")),
            "title":               safe_str(r.get("reportTitle")) or safe_str(r.get("headHtml")),
            "provider":            safe_str(r.get("provider")),
            "report_type":         safe_str(r.get("reportType")),
            "report_date":         safe_str(r.get("reportDate")),
            "investment_rating":   safe_str(r.get("investmentRating")),
            "target_price":        safe_float(r.get("targetPrice")),
            "target_price_status": safe_str(r.get("targetPriceStatus")),
        }
        for r in raw
    ]


def _fetch_section(obj, section: str, limit: int | None, *, full: bool):
    """Pull one section off a Sector / Industry instance. Returns
    `(value, error_kind, attempts)` from with_retry.

    Verified 2026-05: yfinance caches the underlying Yahoo response
    on the Sector / Industry instance after the first property
    access, so subsequent section accesses on the SAME instance are
    CPU-only (DataFrame parsing / dict projection) — no network. The
    `with_retry` wrapper here is still useful for the very first
    access (which IS network) but is a near-no-op for subsequent
    sections; the first access is always `overview` from the
    validation probe in `fetch()`, so by the time we get here the
    response is cached.

    For sections that yfinance returns as DataFrames, we run the
    projector inside the retry callable so an empty / None payload
    routes through the same retry path as a network error. The
    `--full` path skips projection and returns the raw object.
    """
    def _call():
        return getattr(obj, section)
    raw, err_kind, attempts = with_retry(_call)
    if err_kind:
        return None, err_kind, attempts
    if full:
        # Raw DataFrames get reset_index + to_dict for JSON-serializability;
        # raw dicts / lists pass through.
        if isinstance(raw, pd.DataFrame):
            if raw.empty:
                return [], None, attempts
            return raw.reset_index().to_dict("records"), None, attempts
        return raw, None, attempts
    # Project to stable schema.
    if section == "overview":
        return _project_overview(raw), None, attempts
    if section == "top_companies":
        return _project_top_companies(raw, limit), None, attempts
    if section == "industries":
        return _project_industries(raw, limit), None, attempts
    if section == "top_performing_companies":
        return _project_top_performing(raw, limit), None, attempts
    if section == "top_growth_companies":
        return _project_top_growth(raw, limit), None, attempts
    if section in ("top_etfs", "top_mutual_funds"):
        return _project_top_funds(raw, limit), None, attempts
    if section == "research_reports":
        return _project_research_reports(raw, limit), None, attempts
    raise ValueError(f"unknown section: {section!r}")


def fetch(
    *,
    key: str,
    kind: str,
    sections: tuple[str, ...],
    limit: int | None,
    full: bool,
) -> dict:
    """Fetch a single (kind, key) → envelope dict.

    **Cost: 1 HTTP per key, regardless of section count.** Verified
    2026-05: yfinance's Sector / Industry classes hit one Yahoo
    endpoint per key (`/v1/finance/sectors/<key>`) and cache the
    response on the instance — all sections (`overview`,
    `top_companies`, `industries`, `top_etfs`, `top_growth_companies`,
    ...) share that one HTTP. So `--section overview` and
    `--section all` cost the same network-wise. `--section` only
    affects projection / output cost (DataFrame parsing per requested
    section), not network cost.

    The overview probe at the top of the loop is both the key-validity
    check (Yahoo returns None for unknown keys) AND the single Yahoo
    fetch the rest of the sections inherit. If `overview_raw` is None
    we short-circuit with `error_kind: not_found`.

    Per-section errors are isolated: if a projector raises during
    DataFrame parsing for one section but everything else succeeds,
    the envelope has that section: null + a per-section error in
    `section_errors`, not a whole-envelope failure.
    """
    out: dict = {"key": key, "kind": kind}

    # Suppress yfinance's stderr 404 prints — we surface the same info
    # via error_kind. yfinance uses both `warnings.warn` AND raw
    # `print(..., file=sys.stderr)` for its 404 path, so we need both
    # `catch_warnings` and a stderr redirect.
    with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore", UserWarning)

        # `yf.Sector(key)` / `yf.Industry(key)` only stash the key —
        # the HTTP fires lazily on first property access. So construction
        # never raises (verified 2026-05); the validity check is the
        # overview probe below.
        obj = yf.Sector(key) if kind == "sector" else yf.Industry(key)

        # Probe overview first to validate the key exists. Yahoo returns
        # None for unknown keys (also prints to stderr — suppressed
        # above); we surface that as `not_found` and skip remaining
        # sections to save round-trips.
        overview_raw, err_kind, attempts = with_retry(lambda: obj.overview)
        if err_kind:
            out["error"] = (
                f"fetch failed ({err_kind}, after {attempts} attempt(s))"
            )
            out["error_kind"] = err_kind
            out["attempts"] = attempts
            return out
        if overview_raw is None:
            out["error"] = (
                f"unknown {kind} key {key!r} (Yahoo returned no data)"
            )
            out["error_kind"] = "not_found"
            out["attempts"] = attempts
            return out

        # Identity fields. Cheap properties that don't trigger HTTP
        # (already populated from the overview fetch's response).
        out["name"] = safe_str(getattr(obj, "name", None))
        out["symbol"] = safe_str(getattr(obj, "symbol", None))
        if kind == "industry":
            # Back-reference to parent sector. yfinance reads these
            # from the same overview blob — no additional HTTP.
            out["sector_key"] = safe_str(getattr(obj, "sector_key", None))
            out["sector_name"] = safe_str(getattr(obj, "sector_name", None))

        if attempts > 1:
            out["attempts"] = attempts

        # Coverage note for asymmetric sections: e.g. user asked
        # `--section industries` on a `kind=industry` (industries are
        # children of sectors, not other industries). Drop them from
        # the fetch list entirely (don't attempt the HTTP call — the
        # property doesn't exist on the wrong class) and surface via
        # `coverage_note`. Mirrors `analyst.coverage_note` shape.
        applicable_sections = (
            SECTOR_SECTIONS if kind == "sector" else INDUSTRY_SECTIONS
        )
        invalid_for_kind = [
            s for s in sections if s not in applicable_sections]
        if invalid_for_kind:
            out["coverage_note"] = (
                f"section(s) {invalid_for_kind} not applicable to "
                f"kind={kind} (sector-only: industries / top_etfs / "
                f"top_mutual_funds; industry-only: top_performing_companies "
                f"/ top_growth_companies); skipped"
            )

        # Per-section fetch. Overview is reused from the probe above to
        # save one HTTP. Other sections fire only when requested AND
        # applicable to the kind.
        section_errors: dict[str, str] = {}
        for sect in sections:
            if sect not in applicable_sections:
                continue
            if sect == "overview":
                out["overview"] = (
                    overview_raw if full else _project_overview(overview_raw)
                )
                continue
            value, sect_err, sect_attempts = _fetch_section(
                obj, sect, limit, full=full,
            )
            if sect_err:
                section_errors[sect] = (
                    f"{sect_err} after {sect_attempts} attempt(s)"
                )
                out[sect] = None
            else:
                out[sect] = value

        if section_errors:
            out["section_errors"] = section_errors

    return out


def summarize(env: dict) -> dict:
    """Flat per-key dict for peer compare. Keeps identity + the cheap
    rollup signals; drops the long lists. Designed for cross-key
    side-by-side rendering (sector vs sector or industry vs industry)."""
    if env.get("error"):
        # Carry identity + error meta so peer compare doesn't drop
        # failed rows silently.
        return {
            k: v for k, v in env.items()
            if k in ("key", "kind", "name", "symbol",
                     "sector_key", "sector_name",
                     "error", "error_kind", "attempts",
                     "coverage_note")
        }
    overview = env.get("overview") or {}
    top_companies = env.get("top_companies") or []
    industries = env.get("industries") or []
    top_perf = env.get("top_performing_companies") or []
    top_growth = env.get("top_growth_companies") or []
    top_etfs = env.get("top_etfs") or []
    top_mfs = env.get("top_mutual_funds") or []

    flat: dict = {
        "key":                  env.get("key"),
        "kind":                 env.get("kind"),
        "name":                 env.get("name"),
        "symbol":               env.get("symbol"),
        "sector_key":           env.get("sector_key"),
        "sector_name":          env.get("sector_name"),
        "companies_count":      overview.get("companies_count"),
        "industries_count":     overview.get("industries_count"),
        "market_cap":           overview.get("market_cap"),
        "market_weight":        overview.get("market_weight"),
        "employee_count":       overview.get("employee_count"),
        # Description is the most distinctive cross-sector signal in
        # peer compare ("what does this sector cover?"). Long but
        # worth keeping for a digest. Drop in the consumer if you
        # need a tighter row.
        "description":          overview.get("description"),
        "top_company_symbol":   (top_companies[0].get("symbol")
                                 if top_companies else None),
        "top_company_weight":   (top_companies[0].get("market_weight")
                                 if top_companies else None),
        "top_companies_returned": len(top_companies),
    }
    if env.get("kind") == "sector":
        flat["top_industry_key"] = (industries[0].get("key")
                                    if industries else None)
        flat["top_industry_weight"] = (industries[0].get("market_weight")
                                       if industries else None)
        flat["top_etf_symbol"] = top_etfs[0].get(
            "symbol") if top_etfs else None
        flat["top_mutual_fund_symbol"] = (top_mfs[0].get("symbol")
                                          if top_mfs else None)
    else:  # industry
        flat["top_performer_symbol"] = (top_perf[0].get("symbol")
                                        if top_perf else None)
        flat["top_performer_ytd_return"] = (top_perf[0].get("ytd_return")
                                            if top_perf else None)
        flat["top_growth_symbol"] = (top_growth[0].get("symbol")
                                     if top_growth else None)
        flat["top_growth_estimate"] = (top_growth[0].get("growth_estimate")
                                       if top_growth else None)

    for meta in ("coverage_note", "section_errors", "attempts"):
        if meta in env:
            flat[meta] = env[meta]
    return flat


# CSV record_class taxonomy. Each emitted row is tagged with which
# section it came from so the multi-section payload can flatten to a
# row stream without losing context.
RECORD_CLASSES = (
    "meta", "top_company", "industry",
    "top_performer", "top_growth_company",
    "top_etf", "top_mutual_fund",
    "research_report",
)

# Union of all per-record cols across record_classes. Stable order so
# CSV consumers can rely on column positions. `record_class` is the
# discriminator; subsequent cols are populated only when applicable to
# that record_class (empty otherwise).
CSV_COLS = (
    "record_class",
    # identity (every row carries these — same pattern as fund_holdings)
    "key", "kind", "name", "symbol",
    # sector back-ref (industry rows only)
    "sector_key", "sector_name",
    # overview fields (record_class=meta)
    "companies_count", "industries_count", "market_cap", "market_weight",
    "employee_count", "description",
    # top_company / industry / top_etf / top_mutual_fund row fields
    "rating",                  # top_company-specific
    "fund_name",               # top_etf / top_mutual_fund
    # top_performer / top_growth fields
    "ytd_return", "last_price", "target_price", "growth_estimate",
    # research_report fields
    "report_id", "report_title", "report_provider", "report_type",
    "report_date", "report_investment_rating",
    "report_target_price", "report_target_price_status",
    # carry: notes / errors
    "note", "coverage_note", *RESULT_META,
)


def _emit_csv(envelopes: list[dict]) -> None:
    """Default-mode CSV. One row per record (meta, holdings, etc.) with
    `record_class` discriminator. Empty / errored envelopes still emit
    a single carry row tagged `record_class=meta` so they aren't dropped.
    """
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(CSV_COLS)
    for env in envelopes:
        identity = {
            "key": env.get("key"),
            "kind": env.get("kind"),
            "name": env.get("name"),
            "symbol": env.get("symbol"),
            "sector_key": env.get("sector_key"),
            "sector_name": env.get("sector_name"),
        }
        carry = {
            "coverage_note": env.get("coverage_note"),
            **{k: env.get(k) for k in RESULT_META},
        }

        # If the envelope errored or has no sections at all, emit a
        # single meta carry row so callers see identity + error.
        if env.get("error") or not any(
            env.get(s) for s in ALL_SECTIONS
        ):
            row = {"record_class": "meta", **identity, **carry}
            ov = env.get("overview") or {}
            for k in ("companies_count", "industries_count", "market_cap",
                      "market_weight", "employee_count", "description"):
                row[k] = ov.get(k)
            writer.writerow([row.get(c, "") if row.get(c) is not None
                             else "" for c in CSV_COLS])
            continue

        # Meta row (overview).
        ov = env.get("overview") or {}
        row = {"record_class": "meta", **identity, **carry,
               "companies_count":  ov.get("companies_count"),
               "industries_count": ov.get("industries_count"),
               "market_cap":       ov.get("market_cap"),
               "market_weight":    ov.get("market_weight"),
               "employee_count":   ov.get("employee_count"),
               "description":      ov.get("description")}
        writer.writerow([row.get(c, "") if row.get(c) is not None
                         else "" for c in CSV_COLS])

        # Per-section rows. Map each section to its record_class +
        # field-rename so they all funnel through the same CSV_COLS.
        section_to_rc = {
            "top_companies":             "top_company",
            "industries":                "industry",
            "top_performing_companies":  "top_performer",
            "top_growth_companies":      "top_growth_company",
            "top_etfs":                  "top_etf",
            "top_mutual_funds":          "top_mutual_fund",
        }
        for sect, rc in section_to_rc.items():
            for r in env.get(sect) or []:
                row = {"record_class": rc, **identity}
                # `industries` rows under a sector envelope ARE
                # industries — override identity.key (parent sector
                # key) with the row's own industry key, AND override
                # identity.kind ("sector", from the parent envelope)
                # with "industry" so CSV consumers can filter on
                # `kind=industry` to pick out the child industry rows.
                # Without the kind override, the row would carry
                # `record_class=industry` BUT `kind=sector` — easy
                # gotcha for downstream filtering.
                if rc == "industry":
                    row["key"] = r.get("key")
                    row["kind"] = "industry"
                    row["name"] = r.get("name")
                    row["symbol"] = r.get("symbol")
                    row["market_weight"] = r.get("market_weight")
                elif rc == "top_company":
                    row["symbol"] = r.get("symbol")
                    row["name"] = r.get("name")
                    row["rating"] = r.get("rating")
                    row["market_weight"] = r.get("market_weight")
                elif rc in ("top_etf", "top_mutual_fund"):
                    row["symbol"] = r.get("symbol")
                    row["fund_name"] = r.get("name")
                elif rc == "top_performer":
                    row["symbol"] = r.get("symbol")
                    row["name"] = r.get("name")
                    row["ytd_return"] = r.get("ytd_return")
                    row["last_price"] = r.get("last_price")
                    row["target_price"] = r.get("target_price")
                elif rc == "top_growth_company":
                    row["symbol"] = r.get("symbol")
                    row["name"] = r.get("name")
                    row["ytd_return"] = r.get("ytd_return")
                    row["growth_estimate"] = r.get("growth_estimate")
                writer.writerow([row.get(c, "") if row.get(c) is not None
                                 else "" for c in CSV_COLS])

        # research_reports (separate loop because field names diverge).
        for r in env.get("research_reports") or []:
            row = {"record_class": "research_report", **identity,
                   "report_id":                  r.get("id"),
                   "report_title":               r.get("title"),
                   "report_provider":            r.get("provider"),
                   "report_type":                r.get("report_type"),
                   "report_date":                r.get("report_date"),
                   "report_investment_rating":   r.get("investment_rating"),
                   "report_target_price":        r.get("target_price"),
                   "report_target_price_status": r.get("target_price_status")}
            writer.writerow([row.get(c, "") if row.get(c) is not None
                             else "" for c in CSV_COLS])


def _emit_summary_csv(envelopes: list[dict]) -> None:
    """--summary CSV. One row per key with the flat rollup fields. Union
    of all keys across envelopes (sector + industry rollups have
    different fields)."""
    rolled = [summarize(env) for env in envelopes]
    cols: list[str] = []
    for r in rolled:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in rolled:
        writer.writerow([
            (_json.dumps(v, default=str, ensure_ascii=False)
             if isinstance(v, (dict, list)) else (v if v is not None else ""))
            for v in (r.get(c) for c in cols)
        ])


def _emit(envelopes: list[dict], fmt: str, summary: bool) -> None:
    if summary:
        rolled = [summarize(env) for env in envelopes]
        if fmt == "json":
            print(_json.dumps(rolled, indent=2, default=str, ensure_ascii=False))
            return
        if fmt == "ndjson":
            for r in rolled:
                print(_json.dumps(r, default=str, ensure_ascii=False))
            return
        _emit_summary_csv(envelopes)
        return

    if fmt == "json":
        print(_json.dumps(envelopes, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        # Flatten each envelope into per-section records, tagged with
        # record_class + identity. Same shape as fund_holdings.py NDJSON.
        for env in envelopes:
            identity = {
                "key": env.get("key"),
                "kind": env.get("kind"),
                "name": env.get("name"),
                "symbol": env.get("symbol"),
                "sector_key": env.get("sector_key"),
                "sector_name": env.get("sector_name"),
            }
            # Always emit a meta line so empty / errored envelopes
            # aren't silently dropped.
            meta = {"record_class": "meta", **identity}
            if env.get("overview"):
                meta.update(env["overview"])
            for k in ("coverage_note", "section_errors", *RESULT_META):
                if k in env:
                    meta[k] = env[k]
            print(_json.dumps(meta, default=str, ensure_ascii=False))

            section_to_rc = {
                "top_companies":             "top_company",
                "industries":                "industry",
                "top_performing_companies":  "top_performer",
                "top_growth_companies":      "top_growth_company",
                "top_etfs":                  "top_etf",
                "top_mutual_funds":          "top_mutual_fund",
                "research_reports":          "research_report",
            }
            for sect, rc in section_to_rc.items():
                for r in env.get(sect) or []:
                    # Dict-spread merge order is load-bearing: `**r`
                    # AFTER `**identity` lets the row's own `key` (if
                    # present, e.g. `industries` rows carry the child
                    # industry key) override the parent envelope's
                    # `key`. Don't reorder. For `industry` record_class,
                    # also override `kind` → "industry" so CSV / NDJSON
                    # consumers can filter on `kind=industry` to pick
                    # out the child industry rows (parent envelope's
                    # `kind` is "sector"); same fix as the CSV path.
                    tagged = {"record_class": rc, **identity, **r}
                    if rc == "industry":
                        tagged["kind"] = "industry"
                    print(_json.dumps(tagged, default=str, ensure_ascii=False))
        return
    _emit_csv(envelopes)


def _list_sectors(fmt: str) -> None:
    """--list-sectors path: dump the 11 sector keys + child industry
    counts. Pure local lookup from yfinance.const — no HTTP."""
    rows = [
        {
            "key": k,
            "industry_count": len(SECTOR_INDUSTY_MAPPING_LC[k]),
        }
        for k in SECTOR_KEYS
    ]
    if fmt == "json":
        print(_json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        for r in rows:
            print(_json.dumps(r, default=str, ensure_ascii=False))
        return
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["key", "industry_count"])
    for r in rows:
        writer.writerow([r["key"], r["industry_count"]])


def _list_industries(filter_sector: str | None, fmt: str) -> None:
    """--list-industries path. Optionally scoped to one or more sectors
    (comma-separated). Pure local lookup, no HTTP."""
    if filter_sector:
        # Comma-separated multi-sector filter. Parse + normalize +
        # validate per token so a typo on the third sector fails
        # cleanly instead of silently dropping it.
        wanted: list[str] = []
        for raw in filter_sector.split(","):
            sec = _normalize_key(raw)
            if not sec:
                continue
            if sec not in SECTOR_KEYS:
                print(
                    f"unknown sector key {raw!r}; valid: "
                    f"{', '.join(SECTOR_KEYS)}",
                    file=sys.stderr,
                )
                sys.exit(2)
            if sec not in wanted:  # de-dup
                wanted.append(sec)
        pairs = [
            (sec, _normalize_key(ind))
            for sec in wanted
            for ind in SECTOR_INDUSTY_MAPPING_LC[sec]
        ]
    else:
        pairs = [
            (sec, _normalize_key(ind))
            for sec in SECTOR_KEYS
            for ind in SECTOR_INDUSTY_MAPPING_LC[sec]
        ]
    rows = [{"sector_key": sec, "industry_key": ind} for sec, ind in pairs]
    if fmt == "json":
        print(_json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        for r in rows:
            print(_json.dumps(r, default=str, ensure_ascii=False))
        return
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["sector_key", "industry_key"])
    for r in rows:
        writer.writerow([r["sector_key"], r["industry_key"]])


def _peers(industry_key: str, fmt: str) -> None:
    """--peers path. Given an industry key, list its sibling industries
    within the same sector (including itself, marked). Pure local
    lookup from yfinance.const — no HTTP. Cheaper than the chained
    `sectors.py <industry> --section overview` → `sectors.py
    <parent_sector> --section industries` two-step (which is 2 HTTP).
    Trade-off: weights aren't included here (chain to `--section
    industries` if you need market_weight per sibling)."""
    ind = _normalize_key(industry_key)
    if ind not in INDUSTRY_KEYS:
        print(
            f"unknown industry key {industry_key!r}; "
            f"use --list-industries to discover canonical keys",
            file=sys.stderr,
        )
        sys.exit(2)
    parent_sec = INDUSTRY_TO_SECTOR[ind]
    siblings = [_normalize_key(s)
                for s in SECTOR_INDUSTY_MAPPING_LC[parent_sec]]
    rows = [
        {
            "sector_key": parent_sec,
            "industry_key": s,
            "is_self": s == ind,
        }
        for s in siblings
    ]
    if fmt == "json":
        print(_json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        for r in rows:
            print(_json.dumps(r, default=str, ensure_ascii=False))
        return
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["sector_key", "industry_key", "is_self"])
    for r in rows:
        writer.writerow([r["sector_key"], r["industry_key"], r["is_self"]])


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Browse Yahoo Finance's sector / industry hierarchy.\n\n"
            "11 sectors → ~150 industries → companies / ETFs / funds.\n"
            "One mode handles both Sector and Industry classes via\n"
            "--kind (auto-inferred from the key by default).\n\n"
            "Sections fetch independently (each is a separate HTTP\n"
            "call) — default pulls overview + top_companies; opt into\n"
            "more via --section."
        ),
        epilog=(
            "Examples:\n"
            "  # Default: overview + top companies for the technology sector\n"
            "  sectors.py technology\n"
            "\n"
            "  # Industry, full sections (autodetect kind)\n"
            "  sectors.py semiconductors --section all\n"
            "\n"
            "  # Sector with industries + ETFs + mutual funds\n"
            "  sectors.py technology --section overview,industries,top_etfs,top_mutual_funds\n"
            "\n"
            "  # Peer compare across sectors\n"
            "  sectors.py --summary technology healthcare financial-services\n"
            "\n"
            "  # Compare industries within a sector\n"
            "  sectors.py --summary semiconductors software-infrastructure consumer-electronics\n"
            "\n"
            "  # Discovery: list sector keys (no HTTP)\n"
            "  sectors.py --list-sectors\n"
            "\n"
            "  # Discovery: list industries within one or more sectors (no HTTP)\n"
            "  sectors.py --list-industries technology\n"
            "  sectors.py --list-industries technology,healthcare\n"
            "\n"
            "  # Discovery: sibling industries of an industry (no HTTP)\n"
            "  sectors.py --peers semiconductors\n"
            "\n"
            "See references/sectors.md for field schema, units (market\n"
            "weights are FRACTIONS), and per-section coverage notes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument(
        "keys", nargs="*",
        help=(
            "Sector or industry keys (e.g. `technology`, `semiconductors`).\n"
            "Use --list-sectors / --list-industries to discover canonical\n"
            "keys. Multiple keys → multiple envelopes."
        ),
    )
    ap.add_argument(
        "--kind", default="auto", choices=("auto", "sector", "industry"),
        help=(
            "Which class to instantiate. `auto` (default) infers from the\n"
            "key — sector keys are a fixed set of 11 known to yfinance.\n"
            "Pass explicitly to disambiguate or override the inference."
        ),
    )

    valid_sections = ALL_SECTIONS

    def parse_sections(raw: str) -> tuple[str, ...]:
        if raw.lower().strip() == "all":
            return valid_sections
        out: list[str] = []
        for s in raw.split(","):
            s = s.strip().lower()
            if not s:
                continue
            if s not in valid_sections:
                raise argparse.ArgumentTypeError(
                    f"unknown section {s!r}; choose from {', '.join(valid_sections)} "
                    f"(comma-separated, or `all`)"
                )
            if s not in out:
                out.append(s)
        if not out:
            raise argparse.ArgumentTypeError("--section cannot be empty")
        return tuple(out)

    # Sentinel so we can detect "user didn't pass --section" later
    # and auto-expand for --summary. argparse default= can't tell us
    # whether the user explicitly passed the default.
    ap.add_argument(
        "--section", default=None, type=parse_sections,
        help=(
            "Sections to fetch (comma-separated, case-insensitive, or\n"
            "`all`). Default: overview,top_companies.\n\n"
            "Common: overview, top_companies, research_reports.\n"
            "Sector-only: industries, top_etfs, top_mutual_funds.\n"
            "Industry-only: top_performing_companies, top_growth_companies.\n\n"
            "**Cost is 1 HTTP per key regardless of --section count**\n"
            "(verified 2026-05 — yfinance caches the response on the\n"
            "Sector / Industry instance and all section properties\n"
            "share it). So `--section all` costs the same network-wise\n"
            "as `--section overview`; --section only affects projection\n"
            "/ output cost (DataFrame parsing per requested section).\n"
            "Sections not applicable to the kind are skipped with a\n"
            "`coverage_note`. Auto-expands to all-applicable for the\n"
            "kind when --summary is set without explicit --section."
        ),
    )

    ap.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help=(
            "Cap the row count of each list section (top_companies /\n"
            "industries / top_performing_companies / top_growth_companies\n"
            "/ top_etfs / top_mutual_funds / research_reports). Default:\n"
            "no cap (Yahoo returns ~5–50 per list depending on section)."
        ),
    )

    ap.add_argument(
        "--summary", action="store_true",
        help=(
            "Emit a flat per-key dict (identity + headline counts +\n"
            "top_company_symbol / top_industry_key / top_etf_symbol etc.)\n"
            "instead of the full per-section payload. Designed for peer\n"
            "compare across sectors or across industries."
        ),
    )

    ap.add_argument(
        "--full", action="store_true",
        help=(
            "Emit raw Yahoo payload (DataFrames as records, dicts as-is)\n"
            "instead of the curated projection. Useful when Yahoo serves\n"
            "a field not in our schema. Mutually exclusive with --summary."
        ),
    )

    ap.add_argument(
        "--list-sectors", dest="list_sectors", action="store_true",
        help=(
            "List the 11 valid sector keys + their child industry counts.\n"
            "No HTTP — pulls from yfinance.const. Mutually exclusive\n"
            "with positional keys."
        ),
    )
    ap.add_argument(
        "--list-industries", dest="list_industries", nargs="?",
        const="__ALL__", default=None, metavar="SECTOR[,SECTOR...]",
        help=(
            "List industry keys, optionally filtered to one or more\n"
            "sectors (comma-separated, e.g. `--list-industries\n"
            "technology,healthcare`). No HTTP — pulls from\n"
            "yfinance.const. Mutually exclusive with positional keys."
        ),
    )
    ap.add_argument(
        "--peers", dest="peers", default=None, metavar="INDUSTRY",
        help=(
            "Given an industry key, list its sibling industries within\n"
            "the same sector (with `is_self` flag on the matching row).\n"
            "No HTTP — pulls from yfinance.const. Mutually exclusive\n"
            "with positional keys / --list-* flags. Use this to discover\n"
            "peer industries for cross-industry compare; chain into\n"
            "`sectors.py --summary KEY1 KEY2 ...` for the actual data."
        ),
    )

    ap.add_argument(
        "--format", default="json", choices=("json", "ndjson", "csv"),
        help=(
            "Output format. json (default) = list of envelope dicts;\n"
            "ndjson = one record per line with `record_class` discriminator\n"
            "(meta / top_company / industry / top_performer / top_growth_company\n"
            "/ top_etf / top_mutual_fund / research_report); csv = same\n"
            "discriminator in tabular form (cols = union of all per-record\n"
            "fields)."
        ),
    )

    args = ap.parse_args()

    if args.summary and args.full:
        ap.error("--summary and --full are mutually exclusive")

    # Discovery paths short-circuit. argparse can't enforce mutex with
    # nargs="?" cleanly, so guard at the application level.
    discovery_flags = {
        "--list-sectors":    args.list_sectors,
        "--list-industries": args.list_industries is not None,
        "--peers":           args.peers is not None,
    }
    enabled = [name for name, on in discovery_flags.items() if on]
    if len(enabled) > 1:
        ap.error(
            f"discovery flags are mutually exclusive: {', '.join(enabled)}")
    if enabled and args.keys:
        ap.error(f"{enabled[0]} cannot be combined with positional keys")

    if args.list_sectors:
        _list_sectors(args.format)
        return
    if args.list_industries is not None:
        sec = None if args.list_industries == "__ALL__" else args.list_industries
        _list_industries(sec, args.format)
        return
    if args.peers is not None:
        _peers(args.peers, args.format)
        return

    if not args.keys:
        ap.error(
            "no keys given; pass one or more sector/industry keys (e.g. "
            "`technology`, `semiconductors`), or use --list-sectors / "
            "--list-industries to discover canonical keys"
        )

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    # Resolve kind per-key. With `--kind auto` (the default) each key is
    # inferred; with explicit `--kind` all keys use that kind. Unknown
    # keys under `auto` are a clean argparse error rather than a
    # network 404. _normalize_key absorbs em-dash → hyphen + casing
    # so `Software—Application` resolves cleanly.
    resolved: list[tuple[str, str]] = []
    for k in args.keys:
        kk = _normalize_key(k)
        if args.kind == "auto":
            inferred = _kind_for_key(kk)
            if inferred is None:
                ap.error(
                    f"unknown sector/industry key {k!r}; use "
                    f"--list-sectors or --list-industries to discover "
                    f"canonical keys, or pass --kind sector|industry to "
                    f"force an interpretation"
                )
            resolved.append((kk, inferred))
        else:
            resolved.append((kk, args.kind))

    # Resolve section list per (key, kind). Three cases:
    #   - explicit --section: respect verbatim (coverage_note will fire
    #     if the user passed inapplicable sections — surfaces the mistake)
    #   - default with --summary: auto-expand to all sections applicable
    #     to THIS key's kind (sector_sections for sectors, industry_sections
    #     for industries) so the rollup dict has populated top_industry /
    #     top_etf / top_performer fields without firing a noisy
    #     coverage_note for inapplicable cross-kind sections
    #   - default without --summary: overview + top_companies (2 HTTP)
    def _sections_for(kind: str) -> tuple[str, ...]:
        if args.section is not None:
            return args.section
        if args.summary:
            return SECTOR_SECTIONS if kind == "sector" else INDUSTRY_SECTIONS
        return DEFAULT_SECTIONS

    # Cost preview on stderr. Verified 2026-05: yfinance's Sector /
    # Industry classes hit ONE Yahoo endpoint per key
    # (`/v1/finance/sectors/<key>` or `/v1/finance/industries/<key>`)
    # and cache the response in the instance — so all sections (overview,
    # top_companies, industries, top_etfs, top_growth_companies, ...)
    # share that single HTTP regardless of --section count. Net cost is
    # **1 HTTP per key**, NOT per section. Warn at ≥ 8 keys (a digit-
    # threshold heuristic — single-digit fetches are fine; double-digit
    # starts feeling slow and increases 429 risk).
    plan = [(k, kind, _sections_for(kind)) for k, kind in resolved]
    if len(plan) >= 8:
        print(
            f"info: sectors plan = {len(plan)} key(s) × 1 HTTP each "
            f"(all sections share one cached endpoint per key); "
            f"~{len(plan) * 1.5:.0f}-{len(plan) * 3:.0f}s typical, "
            f"longer if Yahoo rate-limits. --section affects projection "
            f"cost only, not network cost.",
            file=sys.stderr,
        )

    envelopes = [
        fetch(
            key=k,
            kind=kind,
            sections=sections,
            limit=args.limit,
            full=args.full,
        )
        for k, kind, sections in plan
    ]

    _emit(envelopes, args.format, args.summary)


if __name__ == "__main__":
    main()
