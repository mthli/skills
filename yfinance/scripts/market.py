#!/usr/bin/env python3
"""Market-wide pulse: market clock + featured-quote summary per region.

Wraps `yfinance.Market(market)` for the 8 canonical regions:

    US, GB, ASIA, EUROPE, RATES, COMMODITIES, CURRENCIES, CRYPTOCURRENCIES

Two sections per market:

  clock     market open/close + status string. **yfinance quirk:** the
            underlying `markettime` endpoint always returns the U.S.
            clock regardless of `market` arg (verified 2026-05). For
            non-US regions we surface it verbatim and add a
            `clock_is_us_fallback: true` flag so callers can branch
            programmatically; per-region open/closed lives on each
            summary row's `market_state`.

  summary   Yahoo's curated representative quotes for the region (US =
            6 indexes incl. ^GSPC / ^DJI / ^IXIC; ASIA = 6 incl. SSE /
            Hang Seng / ASX; CRYPTOCURRENCIES = 1 featured pair, etc.).
            Sparse on purpose — Yahoo decides what's "featured." If
            you need the full crypto / FX universe use `screener` /
            `fast_info`.

Cost: 2 HTTP per market (one for `markettime`, one for
`quote/marketSummary`). N markets = 2N HTTP, serial.

Doesn't take a ticker — keys are region strings. Distinct from
`calendars` (event timeline) and `screener` (filter predicates).
"""
from __future__ import annotations
from helpers import (
    RESULT_META,
    safe_bool,
    safe_float,
    safe_int,
    safe_str,
    with_retry,
)
import yfinance as yf

import argparse
import contextlib
import csv as _csv
import io
import json as _json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Canonical region keys accepted by `yf.Market(<key>)`. Sourced from
# yfinance docs (no `yfinance.const` enum exists for these). Hardcoded
# so an unknown key gets a clean argparse error rather than a 503 +
# silent partial response (yfinance's failure mode for bad markets).
MARKET_KEYS = (
    "US", "GB", "ASIA", "EUROPE",
    "RATES", "COMMODITIES", "CURRENCIES", "CRYPTOCURRENCIES",
)
_MARKET_KEYS_LC = {k.lower(): k for k in MARKET_KEYS}

ALL_SECTIONS = ("clock", "summary")
DEFAULT_SECTIONS = ALL_SECTIONS  # cheap; both come from independent endpoints


def _normalize_market(raw: str) -> str | None:
    """User input → canonical key. Lowercase / whitespace tolerant.
    Returns None for unknown keys."""
    return _MARKET_KEYS_LC.get(raw.strip().lower())


def _iso(v) -> str | None:
    """datetime → ISO 8601 string; pre-stringified passes through; other → None.
    yfinance's Market.status surfaces `open` / `close` as datetime.datetime
    objects (after its internal `fromisoformat` parse) — we re-stringify
    so the JSON output is human-readable.

    Defensive: if a datetime ever comes through naive (no tzinfo),
    `.isoformat()` would silently produce a string with no UTC offset
    (`"2026-05-11T13:30:00"` instead of `"2026-05-11T13:30:00+00:00"`),
    which downstream consumers can't tell apart from a real local-time
    string. yfinance currently returns aware datetimes (verified 2026-05),
    but if that flips we attach UTC so the output remains self-describing.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    s = safe_str(v)
    return s


def _project_clock(raw: dict | None) -> dict | None:
    """yfinance `Market.status` → stable schema. Drops `tz`
    (datetime.timezone object — not JSON-serializable; the IANA name is
    already on `timezone.$text`)."""
    if not raw or not isinstance(raw, dict):
        return None
    tz_block = raw.get("timezone") or {}
    # `gmtoffset` is observed as a string of seconds ("-14400" = -4h
    # EDT, verified 2026-05). yfinance's own internal parser
    # (`Market._parse_data`) divides by 1000 then constructs a
    # timedelta — that yfinance code path looks like it expects ms,
    # but the upstream Yahoo response is seconds. We surface seconds
    # under that name; defend against a future yfinance schema flip
    # by auto-detecting ms-encoded magnitudes (real tz offsets max
    # at ±14h = ±50400s, so abs > 86400 means we got ms, not s).
    gmt_off = safe_int(tz_block.get("gmtoffset"))
    if gmt_off is not None and abs(gmt_off) > 86400:
        gmt_off = gmt_off // 1000
    return {
        "id":                  safe_str(raw.get("id")),
        "name":                safe_str(raw.get("name")),
        "status":              safe_str(raw.get("status")),
        "yfit_market_status":  safe_str(raw.get("yfit_market_status")),
        "message":             safe_str(raw.get("message")),
        "open":                _iso(raw.get("open")),
        "close":               _iso(raw.get("close")),
        "timezone":            safe_str(tz_block.get("$text")),
        "tz_short":            safe_str(tz_block.get("short") or raw.get("tz")),
        "gmt_offset_seconds":  gmt_off,
        "dst":                 safe_bool(tz_block.get("dst")),
    }


# Yahoo's `regularMarketTime` is epoch seconds. Convert to ISO with UTC
# offset so the row is self-describing; the per-quote `exchangeTimezoneName`
# tells callers the natural local tz if they want to fold it.
def _epoch_to_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


def _project_summary_row(exchange_code: str, raw: dict) -> dict:
    """One Yahoo-curated quote row → projected dict.

    Yahoo's `regularMarketChangePercent` is **percent-encoded** (verified:
    SNP raw 0.84257036 = 0.84% matches change/prev_close ratio × 100).
    Matches `fast_info.change_pct` convention — see SKILL.md unit notes.

    `listing_region` is Yahoo's listing-region tag, NOT the home market
    of the underlying instrument: ^N225 (Tokyo) and ^HSI (Hong Kong)
    both come back tagged "US" because Yahoo treats them as US-listed
    quote feeds. For the actual home market read `exchange_timezone`
    or the symbol prefix.
    """
    return {
        "exchange_code":            safe_str(exchange_code),
        "symbol":                   safe_str(raw.get("symbol")),
        "short_name":               safe_str(raw.get("shortName")),
        "quote_type":               safe_str(raw.get("quoteType")),
        # `exchange` field dropped: in observed data it always equals
        # `exchange_code` (the dict key Yahoo uses to index this row).
        # `full_exchange_name` is the only distinct exchange string
        # ("Shanghai" vs key "SHH") so we keep that one.
        "full_exchange_name":       safe_str(raw.get("fullExchangeName")),
        "price":                    safe_float(raw.get("regularMarketPrice")),
        "previous_close":           safe_float(raw.get("regularMarketPreviousClose")),
        "change":                   safe_float(raw.get("regularMarketChange")),
        "change_pct":               safe_float(raw.get("regularMarketChangePercent")),
        "regular_market_time":      _epoch_to_iso(raw.get("regularMarketTime")),
        # API-side feed delay in minutes (0 = real-time; 10/15/20 = delayed
        # quote). Distinct from yfinance's SQLite response cache (separate
        # source of staleness — see references/market.md). Per-quote, NOT
        # per-region: US has ^GSPC at 0 but ^RUT at 15.
        "data_delayed_by_minutes":  safe_int(raw.get("exchangeDataDelayedBy")),
        "market_state":             safe_str(raw.get("marketState")),
        "listing_region":           safe_str(raw.get("region")),
        "exchange_timezone":        safe_str(raw.get("exchangeTimezoneName")),
    }


def _project_summary(raw: dict | None) -> list[dict]:
    """yfinance `Market.summary` is `{exchange_code: quote_dict}`. Convert
    to a list of records, preserving Yahoo's insertion order (which mirrors
    the on-screen ranking).

    `--limit` is NOT applied here — it clips at output time so
    `summarize()` aggregates over the full set."""
    if not raw or not isinstance(raw, dict):
        return []
    return [_project_summary_row(code, q) for code, q in raw.items()
            if isinstance(q, dict)]


def fetch(
    *,
    market: str,
    sections: tuple[str, ...],
    full: bool,
) -> dict:
    """Fetch a single market → envelope dict.

    **Cost: 2 HTTP per market** (one for `markettime`, one for
    `quote/marketSummary`). yfinance's `Market._parse_data` fires both
    on first property access and caches them on the instance, so
    accessing `.status` and `.summary` in succession costs the
    documented 2 HTTP regardless of access order.

    Section selection only affects projection / output cost — both
    HTTP fire as soon as we touch either property because they're
    interleaved in `_parse_data` ("Fetch both to ensure they are at
    the same time" per yfinance source). Don't try to skip one to
    save a round-trip; you can't.

    **`--limit` is intentionally NOT applied here.** Limit clips at
    output emission time so `summarize()` aggregates over the full
    Yahoo-curated set, not a user-truncated subset. Otherwise
    `--limit 1 --summary` would compute avg/best/worst over 1 row
    and produce a meaningless rollup.
    """
    out: dict = {"market": market}

    # Suppress yfinance's stderr error logs for transient parse failures
    # — we surface the same info via error_kind / partial-empty payloads.
    with contextlib.redirect_stderr(io.StringIO()):
        # Construction never raises (verified 2026-05); HTTP fires
        # lazily on first property access. So the validity check is
        # the property-access probe below.
        obj = yf.Market(market)

        # Probe summary first. yfinance's `_parse_data` fires both
        # endpoints together, so this single `with_retry` covers the
        # full 2-HTTP fetch. Subsequent `obj.status` access is from
        # the cached `self._status` — no network.
        summary_raw, err_kind, attempts = with_retry(lambda: obj.summary)
        if err_kind:
            out["error"] = (
                f"fetch failed ({err_kind}, after {attempts} attempt(s))"
            )
            out["error_kind"] = err_kind
            out["attempts"] = attempts
            return out

        if attempts > 1:
            out["attempts"] = attempts

        # Per-section projection. `obj.status` access only fires when
        # the user requested `clock` — even though the underlying HTTP
        # already happened (yfinance interleaves both fetches), we
        # gate the access so a defensive `obj.status` failure can't
        # add a `section_errors[clock]` field to an envelope that
        # didn't ask for clock.
        if "clock" in sections:
            try:
                status_raw = obj.status
            except Exception as exc:
                # Defensive — _parse_data shouldn't fail status if
                # summary succeeded, but if it does we surface partial
                # coverage rather than the whole envelope.
                status_raw = None
                out["section_errors"] = {"clock": f"unknown error: {exc!r}"}
            out["clock"] = (
                status_raw if full else _project_clock(status_raw)
            )
            # yfinance quirk flag: `markettime` endpoint always returns
            # the US clock regardless of `market` arg. Verified 2026-05
            # against ASIA / EUROPE / COMMODITIES / etc — every one
            # returned `name="U.S. markets"` and EDT timestamps. Surface
            # via a bool so callers can branch programmatically; the
            # full explanation lives in references/market.md to keep
            # this output compact (especially for CSV / NDJSON where
            # the long string was repeated per row).
            if market.upper() != "US" and out.get("clock"):
                out["clock_is_us_fallback"] = True

        if "summary" in sections:
            out["summary"] = (
                summary_raw if full else _project_summary(summary_raw)
            )
            if not full:
                out["summary_count"] = len(out["summary"])

    out["sections_returned"] = list(sections)
    return out


def summarize(env: dict) -> dict:
    """Flat per-market dict for peer compare.

    Identity + clock fields + region-health metrics (avg / best /
    worst change_pct) + the top-of-list quote. Drops the long lists.

    **Avg/best/worst are computed over the dominant `quote_type`,
    not all rows.** Yahoo's curated summaries mix categories — ASIA
    has 5 INDEX rows + 1 CURRENCY pair (USD/JPY); RATES has
    INDEX (^TYX yield) + FUTURE (ZN=F T-Note); etc. Averaging an
    index daily return with an FX move is dimensionally meaningless,
    so we pick the most-populous `quote_type` and aggregate within
    it. `avg_quote_type` echoes which type fed the avg so the
    output is self-describing; `avg_rows_used` reports how many
    rows. For mixed regions where every type has equal counts,
    Counter.most_common breaks the tie by insertion order (which
    is Yahoo's natural ranking).

    The `top_*` fields still reflect `summary[0]` regardless of
    quote_type, since "top" is Yahoo's editorial ranking.
    """
    if env.get("error"):
        return {
            k: v for k, v in env.items()
            if k in ("market", "error", "error_kind", "attempts")
        }

    clock = env.get("clock") or {}
    rows = env.get("summary") or []

    # Dominant quote_type for avg/best/worst.
    qt_counts = Counter(
        r.get("quote_type") for r in rows
        if isinstance(r.get("quote_type"), str)
    )
    dominant_qt = qt_counts.most_common(1)[0][0] if qt_counts else None
    relevant = (
        [r for r in rows if r.get("quote_type") == dominant_qt]
        if dominant_qt else []
    )
    pcts = [
        r.get("change_pct") for r in relevant
        if isinstance(r.get("change_pct"), (int, float))
    ]
    avg_pct = (sum(pcts) / len(pcts)) if pcts else None
    best_pct = max(pcts) if pcts else None
    worst_pct = min(pcts) if pcts else None

    flat: dict = {
        "market":            env.get("market"),
        "clock_status":      clock.get("status"),
        "clock_open":        clock.get("open"),
        "clock_close":       clock.get("close"),
        "clock_timezone":    clock.get("timezone"),
        "summary_count":     len(rows),
        "top_symbol":        rows[0].get("symbol") if rows else None,
        "top_short_name":    rows[0].get("short_name") if rows else None,
        "top_quote_type":    rows[0].get("quote_type") if rows else None,
        "top_price":         rows[0].get("price") if rows else None,
        "top_change_pct":    rows[0].get("change_pct") if rows else None,
        "top_market_state":  rows[0].get("market_state") if rows else None,
        "avg_change_pct":    avg_pct,
        "best_change_pct":   best_pct,
        "worst_change_pct":  worst_pct,
        "avg_quote_type":    dominant_qt,
        "avg_rows_used":     len(relevant),
    }

    for meta in ("clock_is_us_fallback", "section_errors", "attempts"):
        if meta in env:
            flat[meta] = env[meta]
    return flat


# CSV record_class taxonomy. `meta` rows carry identity + clock fields;
# `quote` rows carry one summary row each. Mirrors sectors.py / fund_holdings.py.
RECORD_CLASSES = ("meta", "quote")

CSV_COLS = (
    "record_class",
    # identity (every row)
    "market",
    # clock fields (record_class=meta)
    "clock_status", "clock_name", "clock_open", "clock_close",
    "clock_timezone", "clock_tz_short", "clock_message",
    # quote fields (record_class=quote)
    "exchange_code", "symbol", "short_name", "quote_type",
    "full_exchange_name",
    "price", "previous_close", "change", "change_pct",
    "regular_market_time", "data_delayed_by_minutes",
    "market_state", "listing_region", "exchange_timezone",
    # carry: flags + meta
    "clock_is_us_fallback", *RESULT_META,
)


def _clip_summary(env: dict, limit: int | None) -> dict:
    """Apply --limit to a default-mode envelope's `summary` list at
    output time (not at fetch time — see fetch() docstring). Returns
    a shallow copy so the original envelope is unmodified (matters
    when summarize() is called over the same data afterwards in
    multi-format pipelines)."""
    if limit is None:
        return env
    s = env.get("summary")
    if isinstance(s, list) and len(s) > limit:
        clipped = dict(env)
        clipped["summary"] = s[:limit]
        clipped["summary_count"] = limit
        return clipped
    return env


def _emit_csv(envelopes: list[dict]) -> None:
    """Default-mode CSV. One meta row per envelope (clock fields) +
    one quote row per summary entry. record_class discriminator."""
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(CSV_COLS)
    for env in envelopes:
        identity = {"market": env.get("market")}
        carry = {
            "clock_is_us_fallback": env.get("clock_is_us_fallback"),
            **{k: env.get(k) for k in RESULT_META},
        }

        # Errored envelope: emit a single meta carry row so it isn't
        # silently dropped from the table.
        if env.get("error"):
            row = {"record_class": "meta", **identity, **carry}
            writer.writerow([
                row.get(c, "") if row.get(c) is not None else ""
                for c in CSV_COLS
            ])
            continue

        # Meta row (clock).
        clock = env.get("clock") or {}
        row = {
            "record_class": "meta",
            **identity,
            **carry,
            "clock_status":     clock.get("status"),
            "clock_name":       clock.get("name"),
            "clock_open":       clock.get("open"),
            "clock_close":      clock.get("close"),
            "clock_timezone":   clock.get("timezone"),
            "clock_tz_short":   clock.get("tz_short"),
            "clock_message":    clock.get("message"),
        }
        writer.writerow([
            row.get(c, "") if row.get(c) is not None else ""
            for c in CSV_COLS
        ])

        # Quote rows (summary).
        for q in env.get("summary") or []:
            row = {"record_class": "quote", **identity}
            # Quote rows carry the per-quote fields; clock columns
            # stay empty for these rows. record_class discriminates.
            for k in (
                "exchange_code", "symbol", "short_name", "quote_type",
                "full_exchange_name",
                "price", "previous_close", "change", "change_pct",
                "regular_market_time", "data_delayed_by_minutes",
                "market_state", "listing_region", "exchange_timezone",
            ):
                row[k] = q.get(k)
            writer.writerow([
                row.get(c, "") if row.get(c) is not None else ""
                for c in CSV_COLS
            ])


def _emit_summary_csv(envelopes: list[dict]) -> None:
    """--summary CSV. One row per market. Union of all keys across
    envelopes (errored rows have a different field subset)."""
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


def _emit(envelopes: list[dict], fmt: str, summary: bool,
          limit: int | None) -> None:
    """Dispatch to the right format. `--limit` clips at this stage
    (not in fetch), so default-mode emission shrinks but `--summary`
    aggregation still sees the full Yahoo-curated set."""
    if summary:
        # --summary ignores --limit; aggregation needs full data.
        # Caller already warned on stderr if both were passed.
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

    # Default-mode emission: clip per-envelope summary to --limit.
    # --full bypasses (raw dict, not list).
    clipped = [_clip_summary(env, limit) for env in envelopes]

    if fmt == "json":
        print(_json.dumps(clipped, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        # Flatten each envelope to a record_class-tagged stream.
        # `clock_*` fields use a `clock_` prefix when flattened from
        # the nested dict (for namespace safety — `status` is too
        # generic at the top level); top-level scalars like
        # `summary_count` stay as-is. See references/market.md.
        for env in clipped:
            identity = {"market": env.get("market")}
            meta = {"record_class": "meta", **identity}
            if env.get("clock"):
                meta.update({f"clock_{k}": v for k, v in env["clock"].items()})
            for k in ("clock_is_us_fallback", "summary_count",
                      "section_errors", *RESULT_META):
                if k in env:
                    meta[k] = env[k]
            print(_json.dumps(meta, default=str, ensure_ascii=False))
            for q in env.get("summary") or []:
                tagged = {"record_class": "quote", **identity, **q}
                print(_json.dumps(tagged, default=str, ensure_ascii=False))
        return
    _emit_csv(clipped)


def _list_markets(fmt: str) -> None:
    """--list-markets path: dump the 8 canonical region keys. No HTTP."""
    descriptions = {
        "US":              "U.S. equity indexes (S&P, Dow, Nasdaq, Russell, ...)",
        "GB":              "U.K. — FTSE AIM + currencies + commodities (sparse)",
        "ASIA":            "Asia indexes (SSE / Nikkei / Hang Seng / ASX / ...)",
        "EUROPE":          "Europe indexes (FTSE / CAC / DAX / ...)",
        "RATES":           "U.S. Treasury yield indexes (^TYX, ...)",
        "COMMODITIES":     "Energy + metals futures (BZ=F / CL=F / GC=F / ...)",
        "CURRENCIES":      "Featured FX pair (Yahoo's curated single quote)",
        "CRYPTOCURRENCIES": "Featured crypto pair (Yahoo's curated single quote)",
    }
    rows = [{"key": k, "description": descriptions[k]} for k in MARKET_KEYS]
    if fmt == "json":
        print(_json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        return
    if fmt == "ndjson":
        for r in rows:
            print(_json.dumps(r, default=str, ensure_ascii=False))
        return
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["key", "description"])
    for r in rows:
        writer.writerow([r["key"], r["description"]])


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Market-wide pulse: market clock + featured-quote summary\n"
            "across 8 canonical regions (US, GB, ASIA, EUROPE, RATES,\n"
            "COMMODITIES, CURRENCIES, CRYPTOCURRENCIES).\n\n"
            "Two sections per market: clock (open/close, status string)\n"
            "and summary (Yahoo's curated representative quotes — 1–6\n"
            "per region; sparse on purpose).\n\n"
            "Cost: 2 HTTP per market (markettime + marketSummary).\n"
            "Doesn't take a ticker."
        ),
        epilog=(
            "Examples:\n"
            "  # Default: US clock + featured indexes\n"
            "  market.py US\n"
            "\n"
            "  # Multiple regions (one envelope per market)\n"
            "  market.py US ASIA EUROPE\n"
            "\n"
            "  # All 8 regions, peer-compare flat dict\n"
            "  market.py --summary US GB ASIA EUROPE RATES COMMODITIES CURRENCIES CRYPTOCURRENCIES\n"
            "\n"
            "  # Discovery: list canonical keys (no HTTP)\n"
            "  market.py --list-markets\n"
            "\n"
            "  # Just the clock (skip summary projection — same 2-HTTP cost,\n"
            "  # smaller output)\n"
            "  market.py US --section clock\n"
            "\n"
            "  # CSV: one quote row per featured index across regions\n"
            "  market.py US ASIA EUROPE --format csv\n"
            "\n"
            "  # `--summary` + `--limit` is a no-op pair: --limit would clip\n"
            "  # rows BEFORE the avg/best/worst aggregate (1-row rollup is\n"
            "  # meaningless), so the wrapper warns on stderr and ignores it.\n"
            "  market.py --summary --limit 2 US     # stderr: \"--limit is ignored ...\"\n"
            "\n"
            "Caveat: yfinance's Market.status (`clock` section) returns the\n"
            "U.S. clock for all regions — this is a Yahoo `markettime`\n"
            "endpoint behavior, not a wrapper bug. Non-US envelopes carry a\n"
            "`clock_is_us_fallback: true` flag so callers can branch\n"
            "programmatically. For per-region open/closed, read each summary\n"
            "row's `market_state` field. See references/market.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument(
        "markets", nargs="*",
        help=(
            "Region keys (e.g. US, ASIA, COMMODITIES). Case-insensitive.\n"
            "Multiple keys → multiple envelopes (one per market).\n"
            "Use --list-markets to enumerate canonical keys."
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
                    f"unknown section {s!r}; choose from "
                    f"{', '.join(valid_sections)} (comma-separated, or `all`)"
                )
            if s not in out:
                out.append(s)
        if not out:
            raise argparse.ArgumentTypeError("--section cannot be empty")
        return tuple(out)

    ap.add_argument(
        "--section", default=None, type=parse_sections,
        help=(
            "Sections to emit (comma-separated, case-insensitive, or `all`).\n"
            "Default: clock,summary.\n\n"
            "**Section selection only affects PROJECTION / OUTPUT cost.**\n"
            "yfinance fetches both endpoints together (`_parse_data`\n"
            "is interleaved to keep them time-aligned), so all 2 HTTP fire\n"
            "regardless of --section. Use this to slim the output, not\n"
            "save round-trips."
        ),
    )

    ap.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help=(
            "Cap the number of summary rows per market in the OUTPUT.\n"
            "Default: no cap. Yahoo only returns ~1–6 rows per region\n"
            "anyway (CURRENCIES / CRYPTOCURRENCIES often = 1; US / ASIA\n"
            "= 6); --limit is mostly useful when chaining via CSV.\n\n"
            "**Ignored under --summary** — the rollup (avg / best /\n"
            "worst change_pct) aggregates over the full Yahoo-curated\n"
            "set, since clipping before averaging would yield a\n"
            "meaningless rollup. A stderr warning fires if both are\n"
            "passed."
        ),
    )

    ap.add_argument(
        "--summary", action="store_true",
        help=(
            "Emit a flat per-market dict (clock_status + summary_count +\n"
            "top_symbol / top_change_pct + avg/best/worst change_pct)\n"
            "instead of full envelopes. Designed for cross-region peer\n"
            "compare ('which region is greenest').\n\n"
            "**Avg / best / worst aggregate over the dominant `quote_type`\n"
            "in each region**, not all rows — Yahoo's curated summaries\n"
            "mix INDEX + FUTURE + CURRENCY rows (ASIA = 5 INDEX + 1\n"
            "USD/JPY; RATES = 1 INDEX + 1 FUTURE; etc.) and averaging\n"
            "across types is dimensionally meaningless. `avg_quote_type`\n"
            "echoes which type fed the avg; `avg_rows_used` reports\n"
            "the count. See references/market.md for the worked example."
        ),
    )

    ap.add_argument(
        "--full", action="store_true",
        help=(
            "Emit raw yfinance payload (status dict + summary {code: quote}\n"
            "dict) instead of the projected schema. Datetime objects in\n"
            "`open` / `close` are serialized via JSON's `default=str`.\n"
            "Mutually exclusive with --summary."
        ),
    )

    ap.add_argument(
        "--list-markets", dest="list_markets", action="store_true",
        help=(
            "List the 8 canonical region keys + one-line descriptions.\n"
            "No HTTP. Mutually exclusive with positional keys."
        ),
    )

    ap.add_argument(
        "--format", default="json", choices=("json", "ndjson", "csv"),
        help=(
            "Output format. json (default) = list of envelope dicts;\n"
            "ndjson = one record per line with `record_class` discriminator\n"
            "(meta / quote); csv = same discriminator in tabular form."
        ),
    )

    args = ap.parse_args()

    if args.summary and args.full:
        ap.error("--summary and --full are mutually exclusive")

    if args.list_markets:
        if args.markets:
            ap.error("--list-markets cannot be combined with positional keys")
        _list_markets(args.format)
        return

    if not args.markets:
        ap.error(
            "no market keys given; pass one or more region keys "
            f"({', '.join(MARKET_KEYS)}), or use --list-markets to "
            "enumerate."
        )

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    # --summary aggregates over the full Yahoo-curated set so avg /
    # best / worst stay meaningful — --limit is intentionally ignored
    # when --summary is set. Tell the user instead of silently dropping.
    if args.summary and args.limit is not None:
        print(
            "info: --limit is ignored when --summary is set "
            "(rollup needs the full row set; clip downstream if you "
            "need a slimmer output).",
            file=sys.stderr,
        )

    # Normalize + validate. Reject unknown keys at argparse-time so a typo
    # doesn't cost a 2-HTTP probe + mangled response.
    resolved: list[str] = []
    for raw in args.markets:
        norm = _normalize_market(raw)
        if norm is None:
            ap.error(
                f"unknown market key {raw!r}; valid keys: "
                f"{', '.join(MARKET_KEYS)} (case-insensitive)"
            )
        if norm not in resolved:  # de-dup
            resolved.append(norm)

    sections = args.section if args.section is not None else DEFAULT_SECTIONS

    # Cost preview at ≥ 4 markets (= ≥ 8 HTTP). Mirrors sectors.py's
    # ≥ 8-key warning but scaled for market.py's 2-HTTP-per-key cost.
    if len(resolved) >= 4:
        print(
            f"info: market plan = {len(resolved)} region(s) × 2 HTTP each = "
            f"{len(resolved) * 2} HTTP total; ~{len(resolved) * 1.5:.0f}–"
            f"{len(resolved) * 3:.0f}s typical, longer if Yahoo rate-limits.",
            file=sys.stderr,
        )

    envelopes = [
        fetch(market=m, sections=sections, full=args.full)
        for m in resolved
    ]

    _emit(envelopes, args.format, args.summary, args.limit)


if __name__ == "__main__":
    main()
