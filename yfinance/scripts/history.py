#!/usr/bin/env python3
"""Fetch yfinance historical OHLCV for one or more tickers and print as JSON.

See `history.py --help` for usage, modes, and examples. Output is a JSON array
on stdout, one entry per ticker; failed tickers carry an "error" field instead
of data so a single bad symbol does not poison the batch.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    RESULT_META, emit_json_or_ndjson, infer_exchange_tz, safe_float, safe_int,
    with_retry,
)

import yfinance as yf

VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h",
                   "1d", "5d", "1wk", "1mo", "3mo"}

INTRADAY = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}

# Output schema constants — used by both fetch() (to build dicts) and
# _emit() (to build CSV column lists). Single source of truth so adding
# a field stays in sync between dict construction and CSV cols.
#
# `exchange_tz` is intentionally NOT in _BASE_KEYS — it appears only on
# batch results, so single-ticker CSV stays at the original 6-col base
# schema (backward-compat for downstream consumers parsing by column
# index). _emit() injects the column dynamically when any result carries
# it, putting it right after `timezone` so batch CSV is self-describing.
_BASE_KEYS = ("symbol", "period", "start", "end", "interval", "timezone")
_PER_BAR_KEYS = ("date", "open", "high", "low", "close", "volume",
                 "dividends", "split_ratio")
_PER_EVENT_KEYS = ("date", "dividends", "split_ratio", "capital_gains")
_SUMMARY_KEYS = ("rows_count", "start_date", "end_date",
                 "start_close", "end_close", "change_abs", "change_pct",
                 "period_high", "period_high_date",
                 "period_low", "period_low_date",
                 "avg_volume", "total_dividends")
# `--metadata` projects Ticker.history_metadata into a flat dict — single
# row per ticker, no per-bar / per-event nesting. Field names normalized
# from Yahoo's camelCase to snake_case for skill-wide consistency. Two
# datetime fields are pre-converted to ISO strings (epoch siblings kept
# for raw access). The complex nested fields yfinance exposes
# (currentTradingPeriod / tradingPeriods / lastTrade) are dropped — they
# don't survive CSV flattening cleanly and aren't worth the schema noise
# for the headline metadata use case (when did this ticker start
# trading? what bar sizes does Yahoo accept? what's the exchange tz?).
_METADATA_KEYS = (
    "symbol", "currency", "exchange_name", "full_exchange_name",
    "instrument_type", "first_trade_date", "first_trade_date_epoch",
    "regular_market_time", "regular_market_time_epoch", "has_prepost",
    "gmt_offset", "timezone_short", "exchange_timezone_name",
    "data_granularity", "valid_ranges", "long_name", "short_name",
    "regular_market_price", "previous_close", "chart_previous_close",
    "fifty_two_week_high", "fifty_two_week_low",
    "regular_market_day_high", "regular_market_day_low",
    "regular_market_volume", "price_hint",
)
# Error / retry metadata cols live in helpers.RESULT_META — imported above
# for cross-script consistency.


def _fmt_index(ts, intraday: bool) -> str:
    if intraday:
        return ts.isoformat()
    return ts.strftime("%Y-%m-%d")


def _epoch_to_iso_date(v) -> str | None:
    """Epoch (seconds) → 'YYYY-MM-DD' in UTC.

    UTC is the principled choice because Yahoo emits these epochs against
    UTC; using the server's local timezone (the previous bug — naive
    `datetime.fromtimestamp(v)`) means the same metadata blob would
    decode to different dates depending on where the script runs. For
    consumers who need exchange-local dates, the IANA tz lives in
    `exchange_timezone_name` — convert downstream rather than baking
    server-tz drift into the response.
    """
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _epoch_to_iso_dt(v) -> str | None:
    """Epoch (seconds) → ISO datetime with `+00:00` offset (UTC).

    Same rationale as `_epoch_to_iso_date`: the offset is part of the
    string so consumers can parse it unambiguously. Without the offset,
    a naive ISO string would be ambiguous about which clock it
    represents.
    """
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


class _DropDuplicateNotFoundFilter(logging.Filter):
    """Suppress yfinance's batch-mode duplicate-of-not_found log lines.

    For each missing / wrong-suffix ticker in a yf.download batch, yfinance
    emits up to 4 ERROR records that all reduce to "this ticker is
    not_found" — same information our response dict already carries via
    `error_kind="not_found"`. Empirically (yfinance 1.3.x):

      "HTTP Error 404: ...Quote not found for symbol: ZZZZ..." (low-level)
      "$ZZZZ: possibly delisted; no price data found ..."     (per-ticker)
      "['ZZZZ']: possibly delisted; no price data found ..."  (batch summary)
      "1 Failed download:"                                    (count header)

    Filtering these patterns keeps the not_found duplicates out of stderr
    while preserving genuinely informative ERROR-level messages — non-
    not_found errors (HTTP 5xx, connection failures, schema-drift) and
    HTTP 404s NOT tied to a missing-symbol payload still flow through.
    """
    _DUPLICATE_PATTERNS = (
        "Failed download",
        "possibly delisted; no price data",
        "Quote not found for symbol",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._DUPLICATE_PATTERNS)


# Single instance keeps add/remove pairing trivially correct. The
# try/finally block in fetch_batch already guarantees removal, so a
# fresh-per-call instance would also work; the singleton just makes the
# intent ("this filter is owned by the batch path") obvious.
_NOT_FOUND_DUP_FILTER = _DropDuplicateNotFoundFilter()


def _build_metadata_result(symbol: str, md: dict, attempts: int) -> dict:
    """Project yfinance's Ticker.history_metadata dict into a flat,
    snake_cased shape.

    Two datetime fields (`firstTradeDate`, `regularMarketTime`) are
    pre-converted to ISO strings; raw epoch siblings (`*_epoch`) are
    preserved for callers that need to do their own arithmetic. The
    complex nested fields yfinance exposes (`currentTradingPeriod`,
    `tradingPeriods`, `lastTrade`) are intentionally dropped — they're
    DataFrames / nested dicts that don't survive CSV flattening
    cleanly, and the headline-metadata use case (when did this ticker
    start trading, what bar sizes does Yahoo accept, what's the
    exchange tz) doesn't need them.

    `timezone_short` is the Yahoo `timezone` field (e.g. "EDT") —
    renamed to disambiguate from the IANA `exchange_timezone_name`
    ("America/New_York") which is what callers usually want for
    tz_convert. Ordering of the rename: keeping both fields lets
    callers see the DST-aware short code AND the IANA zone in one
    response.
    """
    raw_first = md.get("firstTradeDate")
    raw_market = md.get("regularMarketTime")

    out = {
        "symbol": symbol,
        "currency": md.get("currency"),
        "exchange_name": md.get("exchangeName"),
        "full_exchange_name": md.get("fullExchangeName"),
        "instrument_type": md.get("instrumentType"),
        "first_trade_date": _epoch_to_iso_date(raw_first),
        "first_trade_date_epoch": safe_int(raw_first),
        "regular_market_time": _epoch_to_iso_dt(raw_market),
        "regular_market_time_epoch": safe_int(raw_market),
        "has_prepost": md.get("hasPrePostMarketData"),
        "gmt_offset": safe_int(md.get("gmtoffset")),
        "timezone_short": md.get("timezone"),
        "exchange_timezone_name": md.get("exchangeTimezoneName"),
        "data_granularity": md.get("dataGranularity"),
        "valid_ranges": md.get("validRanges"),
        "long_name": md.get("longName"),
        "short_name": md.get("shortName"),
        "regular_market_price": safe_float(md.get("regularMarketPrice")),
        "previous_close": safe_float(md.get("previousClose")),
        "chart_previous_close": safe_float(md.get("chartPreviousClose")),
        "fifty_two_week_high": safe_float(md.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": safe_float(md.get("fiftyTwoWeekLow")),
        "regular_market_day_high": safe_float(md.get("regularMarketDayHigh")),
        "regular_market_day_low": safe_float(md.get("regularMarketDayLow")),
        "regular_market_volume": safe_int(md.get("regularMarketVolume")),
        "price_hint": safe_int(md.get("priceHint")),
    }
    if attempts > 1:
        out["attempts"] = attempts
    return out


def _build_result(symbol: str, df, period: str | None, start: str | None,
                  effective_end: str | None, interval: str,
                  summary: bool, head: int | None, tail: int | None,
                  attempts: int, *,
                  output_tz: str | None,
                  exchange_tz: str | None,
                  events_only: bool = False) -> dict:
    """Project a per-ticker DataFrame into the output dict.

    output_tz     value for the response's `timezone` field; also the tz
                  intraday timestamps are emitted in. Single-ticker path
                  passes native df.index tz; batch path passes "UTC".
    exchange_tz   when set (batch path), tz_convert df to this for daily
                  date formatting so dates reflect the exchange's local
                  trading-day calendar instead of UTC. None for the
                  single-ticker path (df is already in exchange-local).

    The index conversion below makes _fmt_index downstream-tz-agnostic —
    it formats whatever tz the index already carries.
    """
    intraday = interval in INTRADAY

    # Choose the tz the index should land in before formatting:
    #   intraday → output_tz (so ISO offset matches the metadata field)
    #   daily    → exchange_tz when given, else output_tz. For single-ticker
    #              path, exchange_tz=None and output_tz IS the native tz —
    #              tz_convert to its own tz is effectively a no-op, so the
    #              pre-batch behavior is preserved.
    if df.index.tz is not None:
        target = exchange_tz if (not intraday and exchange_tz is not None) else output_tz
        if target is not None:
            df = df.tz_convert(target)

    base = {
        "symbol": symbol,
        # Reflect which window mode was used. period is set for --period
        # mode and None when --start/--end was used; start/end vice versa.
        # `end` is backfilled to today when --start was used without --end,
        # so the response is self-describing.
        "period": period,
        "start": start,
        "end": effective_end,
        "interval": interval,
        "timezone": output_tz,
    }
    # exchange_tz appears only on batch results (single-ticker has the same
    # info encoded in `timezone` already). Lets the consumer know which tz
    # the daily date strings are in when timezone="UTC" wouldn't match.
    if exchange_tz is not None:
        base["exchange_tz"] = exchange_tz
    # Surface attempts only when actually retried (success path).
    if attempts > 1:
        base["attempts"] = attempts

    if summary:
        closes = df["Close"]
        highs = df["High"]
        lows = df["Low"]
        vols = df["Volume"]
        divs = df["Dividends"] if "Dividends" in df.columns else None
        splits_col = df["Stock Splits"] if "Stock Splits" in df.columns else None

        start_close = safe_float(closes.iloc[0])
        end_close = safe_float(closes.iloc[-1])
        if start_close is not None and end_close is not None:
            change_abs = end_close - start_close
            change_pct = (change_abs / start_close * 100) if start_close else None
        else:
            change_abs = None
            change_pct = None

        hi_idx = highs.idxmax() if not highs.dropna().empty else None
        lo_idx = lows.idxmin() if not lows.dropna().empty else None

        split_events = []
        if splits_col is not None:
            real_splits = splits_col[(splits_col != 0) & splits_col.notna()]
            for ts, ratio in real_splits.items():
                split_events.append(
                    {"date": _fmt_index(ts, intraday), "ratio": float(ratio)}
                )

        base.update({
            "rows_count": len(df),
            "start_date": _fmt_index(df.index[0], intraday),
            "end_date": _fmt_index(df.index[-1], intraday),
            "start_close": start_close,
            "end_close": end_close,
            "change_abs": change_abs,
            "change_pct": change_pct,
            "period_high": safe_float(highs.max()),
            "period_high_date": _fmt_index(hi_idx, intraday) if hi_idx is not None else None,
            "period_low": safe_float(lows.min()),
            "period_low_date": _fmt_index(lo_idx, intraday) if lo_idx is not None else None,
            "avg_volume": safe_int(round(vols.mean())) if len(vols) else None,
            "total_dividends": safe_float(divs.sum()) if divs is not None else 0.0,
            "splits": split_events,
        })
        return base

    has_div = "Dividends" in df.columns
    has_split = "Stock Splits" in df.columns
    has_capgain = "Capital Gains" in df.columns

    if events_only:
        # Strip OHLCV — keep only rows where at least one corporate action
        # fired (dividend / split / capital-gain distribution). Capital
        # Gains column is fund-only; for non-funds the `capital_gains`
        # field is uniformly 0.0 (schema consistency over conditional
        # presence). Empirical: yfinance / Yahoo data rarely populates
        # Capital Gains even for funds where it should fire — see
        # references/history.md "Mode-specific caveats" for the coverage
        # warning. The COLUMN appears, the DATA is sparse.
        rows = []
        for ts, row in df.iterrows():
            div = safe_float(row["Dividends"]) if has_div else 0.0
            split = safe_float(row["Stock Splits"]) if has_split else 0.0
            capgain = safe_float(row["Capital Gains"]) if has_capgain else 0.0
            # Treat None as 0 for the predicate (NaN-fold safety).
            if (div or 0.0) == 0.0 and (split or 0.0) == 0.0 and (capgain or 0.0) == 0.0:
                continue
            rows.append({
                "date": _fmt_index(ts, intraday),
                "dividends": div or 0.0,
                "split_ratio": split or 0.0,
                "capital_gains": capgain or 0.0,
            })
        rows_total = len(rows)
        if head is not None:
            rows = rows[:head]
        elif tail is not None:
            rows = rows[-tail:]
        base["rows"] = rows
        # Surface fund-only signal so callers know when a 0.0 capital_gains
        # is "no distribution" vs "Yahoo doesn't report it for this
        # ticker type". Non-fund tickers literally don't have the column
        # in the underlying DataFrame.
        base["has_capital_gains_column"] = has_capgain
        # Truncation metadata uses the same shape as default mode
        # (`rows_truncated: {total, shown}`) so callers can read either
        # mode without branching on the schema. Only emitted when
        # truncation actually applied.
        if head is not None or tail is not None:
            base["rows_truncated"] = {"total": rows_total, "shown": len(rows)}
        return base

    # Row dict keys must match _PER_BAR_KEYS for CSV emit alignment.
    rows = []
    for ts, row in df.iterrows():
        rows.append({
            "date": _fmt_index(ts, intraday),
            "open": safe_float(row["Open"]),
            "high": safe_float(row["High"]),
            "low": safe_float(row["Low"]),
            "close": safe_float(row["Close"]),
            "volume": safe_int(row["Volume"]),
            "dividends": safe_float(row["Dividends"]) if has_div else 0.0,
            "split_ratio": safe_float(row["Stock Splits"]) if has_split else 0.0,
        })
    # CLI layer enforces head/tail mutual exclusion. Apply after fetch so
    # the truncation is purely an output projection — the underlying yfinance
    # call doesn't support row limits, so we always pull the full window.
    rows_total = len(rows)
    if head is not None:
        rows = rows[:head]
    elif tail is not None:
        rows = rows[-tail:]
    base["rows"] = rows
    if head is not None or tail is not None:
        base["rows_truncated"] = {"total": rows_total, "shown": len(rows)}
    return base


def fetch_metadata(symbol: str, interval: str = "1d") -> dict:
    """Fetch Ticker.history_metadata for one ticker.

    Implementation detail: yfinance auto-fetches a default `.history()`
    on access if no fetch has happened yet (verified empirically). We
    call `.history()` explicitly anyway so the fetch goes through
    `with_retry` and any rate-limit / network error surfaces with the
    same `error_kind` / `attempts` shape as the OHLCV path. The
    DataFrame return value is discarded — only the side-effect-populated
    `history_metadata` dict matters for this code path.

    The fetch window is hard-pinned to `period=1d` regardless of what
    the user passed via `--period` / `--start` / `--end` — the metadata
    blob is window-invariant (Yahoo returns the same `firstTradeDate` /
    `validRanges` / `currency` / etc. for any pull), so a 1-day fetch
    is the cheapest path to populate it. Honoring the user's window
    flag would just waste bandwidth; the args are still accepted by
    the CLI so the flag combination stays orthogonal across modes.
    """
    def _fetch():
        # Hard-pin the cheapest window — see docstring.
        kwargs = dict(interval=interval, auto_adjust=True,
                      actions=False, prepost=False, period="1d")
        t = yf.Ticker(symbol)
        t.history(**kwargs)
        return t

    t, err_kind, attempts = with_retry(_fetch)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    md = getattr(t, "history_metadata", None)
    if not md:
        return {
            "symbol": symbol,
            "error": "no metadata returned (delisted, wrong suffix, or rate-limited)",
            "error_kind": "not_found",
            "attempts": attempts,
        }
    return _build_metadata_result(symbol, md, attempts)


def fetch(symbol: str, period: str | None, interval: str,
          summary: bool, prepost: bool, adjust: bool = True,
          start: str | None = None, end: str | None = None,
          head: int | None = None, tail: int | None = None,
          events_only: bool = False) -> dict:
    """Single-ticker fetch via Ticker.history. Preserves native tz semantics
    (response `timezone` = exchange-local, daily dates in exchange-local).
    For multi-ticker batches, `main()` dispatches to `fetch_batch` instead.
    """
    # period vs start/end: yfinance accepts EITHER period OR start/end, not both.
    # CLI layer enforces mutual exclusion; here we trust the caller.
    # Backfill end with today if --start was given alone — yfinance's own
    # default is implicit-today, but echoing it back makes the output
    # self-describing. Use local-tz today (not UTC) to match the argparse
    # future-date guard; otherwise non-UTC users near midnight UTC see
    # cross-layer date inconsistency.
    effective_end = end
    if start is not None and end is None:
        effective_end = datetime.now().strftime("%Y-%m-%d")

    def _fetch():
        kwargs = dict(interval=interval, auto_adjust=adjust,
                      actions=True, prepost=prepost)
        if start is not None:
            kwargs["start"] = start
            if end is not None:
                kwargs["end"] = end
        else:
            kwargs["period"] = period
        return yf.Ticker(symbol).history(**kwargs)

    df, err_kind, attempts = with_retry(_fetch)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }

    if df is None or df.empty:
        return {
            "symbol": symbol,
            "error": "no data returned (delisted, wrong suffix, or rate-limited)",
            "error_kind": "not_found",
            "attempts": attempts,
        }

    # Single-ticker path keeps native df.index tz (exchange-local for the
    # ticker). exchange_tz=None tells _build_result not to fold daily dates
    # — they're already in the right calendar.
    output_tz = str(df.index.tz) if df.index.tz is not None else None
    return _build_result(
        symbol, df, period, start, effective_end, interval,
        summary, head, tail, attempts,
        output_tz=output_tz, exchange_tz=None,
        events_only=events_only,
    )


def fetch_batch(symbols: list[str], period: str | None, interval: str,
                summary: bool, prepost: bool, adjust: bool = True,
                start: str | None = None, end: str | None = None,
                head: int | None = None, tail: int | None = None,
                events_only: bool = False) -> list[dict]:
    """Multi-ticker fetch via yf.download — one HTTP request, threaded
    internally by yfinance, then sliced per ticker.

    Output dict shape matches `fetch()` plus two batch-only metadata keys:
      timezone     always "UTC" (intraday timestamps emit with +00:00 offset)
      exchange_tz  per-ticker IANA tz from infer_exchange_tz(); daily date
                   strings are folded into this tz so e.g. a 0700.HK day-bar
                   stays "2026-05-07" rather than shifting to UTC's 05-06.
                   Crypto / FX / futures / unknown indexes get "UTC" — UTC
                   is their natural daily boundary anyway.

    Per-ticker error isolation: a delisted / mistyped ticker comes back as
    an empty slice and gets a not_found error dict (with `exchange_tz` still
    populated so batch CSV stays consistent) — sibling tickers are
    unaffected. A network / sustained-rate_limit failure of the whole batch
    retries via with_retry and, if exhausted, marks every ticker with the
    batch-level error.

    Single-element list (`fetch_batch(["AAPL"])`) is supported defensively
    but is NOT semantically equivalent to `fetch("AAPL")` — it goes through
    yf.download and yields the batch schema (timezone="UTC" + exchange_tz),
    not the single-ticker schema (timezone=native, no exchange_tz). Library
    callers wanting single-ticker schema should call `fetch()` directly;
    the CLI handles this dispatch automatically based on `len(symbols)`.

    Not thread-safe: this function adds/removes a filter on the global
    `yfinance` logger for the duration of the download. Concurrent
    `fetch_batch` calls in the same process would race on filter state —
    a bare `threading.Lock` around add/remove isn't enough, because
    Python's `Logger.addFilter` deduplicates by instance: the second
    concurrent caller's add would be a no-op, and the first caller's
    remove would then strand the second caller's logs unfiltered. Fix is
    a refcount under the lock: increment on entry, only `addFilter` when
    the counter goes 0→1; decrement on exit, only `removeFilter` when it
    returns to 0. Intended for single-threaded CLI use; the docstring
    note serves as a contract for any future concurrent-orchestrator
    integration.
    """
    effective_end = end
    if start is not None and end is None:
        effective_end = datetime.now().strftime("%Y-%m-%d")

    # yf.download emits up to 4 ERROR-level "duplicate of not_found" lines
    # per bogus ticker (HTTP 404 + per-ticker "possibly delisted" + batch
    # summary + count header — see _DropDuplicateNotFoundFilter docstring
    # for the full enumeration). We surface each failure ourselves in the
    # response dict, so the duplicates just pollute stderr (and any output
    # captured via 2>&1). Filter narrowly — non-not_found ERRORs (HTTP
    # storms, API drift) still flow through. Filter is on the parent
    # `yfinance` logger, which is where these messages originate (verified
    # empirically; not a child logger like yfinance.utils, so no
    # propagation concern).
    yf_logger = logging.getLogger("yfinance")
    yf_logger.addFilter(_NOT_FOUND_DUP_FILTER)
    try:
        def _download():
            kwargs = dict(
                tickers=symbols,
                interval=interval, auto_adjust=adjust,
                actions=True, prepost=prepost,
                group_by="ticker",
                threads=True, progress=False,
            )
            if start is not None:
                kwargs["start"] = start
                if end is not None:
                    kwargs["end"] = end
            else:
                kwargs["period"] = period
            return yf.download(**kwargs)
        df_all, err_kind, attempts = with_retry(_download)
    finally:
        yf_logger.removeFilter(_NOT_FOUND_DUP_FILTER)

    if err_kind:
        # Batch-wide failure: every ticker carries the same kind. Inject
        # exchange_tz so batch CSV columns stay populated.
        return [{
            "symbol": s,
            "exchange_tz": infer_exchange_tz(s),
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        } for s in symbols]

    # Normalize the unified index to UTC so every ticker's intraday emits
    # with a +00:00 offset. Naive index (rare; shouldn't happen for
    # yfinance >=1.3 with intraday/daily) — leave alone.
    if df_all is not None and not df_all.empty and df_all.index.tz is not None:
        df_all = df_all.tz_convert("UTC")

    # Detect per-ticker presence. With group_by="ticker" + N>=2, columns are
    # MultiIndex (ticker, field); slicing df_all[sym] returns a sub-DataFrame.
    # Defensive: yf.download with N=1 returns flat columns, so handle that
    # too even though main() only routes here for N>=2.
    has_multi = (df_all is not None and not df_all.empty
                 and hasattr(df_all.columns, "levels"))

    results = []
    for sym in symbols:
        df = None
        if df_all is None or df_all.empty:
            pass
        elif has_multi:
            try:
                df_sym = df_all[sym].dropna(how="all")
                df = df_sym if not df_sym.empty else None
            except KeyError:
                df = None
        else:
            df = df_all if not df_all.empty else None

        if df is None:
            # Per-ticker failure: rate-limit at the batch level was caught
            # above, so this is almost always delisted / wrong-suffix /
            # excluded by Yahoo. Inject exchange_tz so error rows in batch
            # CSV still populate the column.
            results.append({
                "symbol": sym,
                "exchange_tz": infer_exchange_tz(sym),
                "error": "no data returned (delisted, wrong suffix, "
                         "or excluded from batch response)",
                "error_kind": "not_found",
                "attempts": attempts,
            })
            continue

        results.append(_build_result(
            sym, df, period, start, effective_end, interval,
            summary, head, tail, attempts,
            output_tz="UTC",
            exchange_tz=infer_exchange_tz(sym),
            events_only=events_only,
        ))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch historical OHLCV / event / metadata data from Yahoo Finance via yfinance.\n\n"
            "Four output modes (mutually exclusive):\n"
            "  default         full OHLCV rows over the period\n"
            "  --summary       aggregate stats only (start/end close, change_pct, period high/low)\n"
            "  --events-only   corporate-action rows only (dividends / splits / capital_gains) — no OHLCV\n"
            "  --metadata      Ticker.history_metadata snapshot (currency, exchange, first_trade_date, valid_ranges) — no rows\n\n"
            "See references/history.md for the full output schema of each mode."
        ),
        epilog=(
            "Examples:\n"
            "  history.py AAPL                                  # 1mo daily, full rows\n"
            "  history.py --period 1y AAPL MSFT                 # 1y daily, full rows\n"
            "  history.py --period 1mo --summary AAPL MSFT GOOGL  # 1mo summary, 3 tickers\n"
            "  history.py --period ytd --summary AAPL           # YTD aggregate\n"
            "  history.py --period 5y --events-only AAPL        # all dividends / splits in last 5y\n"
            "  history.py --period 10y --events-only --tail 5 VFIAX  # last 5 distributions of a fund\n"
            "  history.py --metadata AAPL MSFT 0700.HK          # currency / exchange / first_trade_date\n"
            "  history.py --period 5d --interval 1h AAPL        # intraday bars\n"
            "  history.py --period 1d --interval 5m --prepost AAPL  # extended hours\n"
            "  history.py --start 2023-01-15 --end 2023-01-22 AAPL  # explicit window\n"
            "  history.py --period 1mo --tail 10 AAPL           # last 10 rows only\n"
            "\n"
            "N≥2 tickers route through yf.download (single batched request,\n"
            "threaded internally, ~3–4× faster than the equivalent serial loop).\n"
            "Batch responses include an extra `exchange_tz` field so the\n"
            "per-ticker daily-date calendar is self-describing.\n"
            "\n"
            "Yahoo caps intraday windows: 1m ≤ 7d, sub-hour ≤ 60d, 1h ≤ 730d.\n"
            "See references/history.md for full flag/output details and\n"
            "SKILL.md for cross-cutting caveats (DST, exchanges, etc.)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Window: --period XOR (--start [--end]). Mutually exclusive group only
    # supports one argument per value, so --start/--end are validated by
    # hand below.
    ap.add_argument("--period", default=None, choices=sorted(VALID_PERIODS),
                    help="Time window (yfinance period string). "
                         "Default 1mo if neither --period nor --start is given. "
                         "Mutually exclusive with --start.")
    ap.add_argument("--start", default=None,
                    help="ISO date YYYY-MM-DD; alternative to --period for "
                         "explicit windows. Mutually exclusive with --period.")
    ap.add_argument("--end", default=None,
                    help="ISO date YYYY-MM-DD; only with --start. "
                         "Defaults to today if --start is given alone.")
    ap.add_argument("--interval", default="1d", choices=sorted(VALID_INTERVALS),
                    help="Bar size. Default %(default)s.")
    ap.add_argument("--summary", action="store_true",
                    help="Aggregate stats instead of full OHLCV rows.")
    ap.add_argument("--events-only", dest="events_only", action="store_true",
                    help="Only corporate-action rows (dividends, splits, "
                         "capital_gains) — no OHLCV. Each row has date + the "
                         "three event fields; non-event days are filtered "
                         "out. Adds a `capital_gains` column not visible in "
                         "default mode (fund-only; populated sparsely by "
                         "Yahoo). Cannot combine with --summary or "
                         "--metadata (only one output mode at a time). "
                         "--prepost and intraday --interval values "
                         "(1m / 5m / 15m / 30m / 60m / 90m / 1h) are also "
                         "rejected — corporate actions are end-of-day "
                         "events that don't appear on intraday bars, and "
                         "Yahoo's 7-60 day intraday window is too short to "
                         "capture meaningful events anyway.")
    ap.add_argument("--metadata", action="store_true",
                    help="Return Ticker.history_metadata only — currency, "
                         "exchange, instrument_type, first_trade_date, "
                         "valid_ranges, etc. Skips per-bar / per-event row "
                         "construction entirely. Cannot combine with "
                         "--summary or --events-only (only one output mode "
                         "at a time). --head / --tail / --no-adjust / "
                         "--prepost are also rejected because metadata is a "
                         "rowless, adjustment-invariant snapshot — those "
                         "flags would have no effect, so script fails fast "
                         "rather than silently ignoring them. "
                         "--period / --start / --end ARE accepted but "
                         "internally ignored — metadata is window-invariant "
                         "(same firstTradeDate / validRanges / currency for "
                         "any window), so the call hard-pins period=1d to "
                         "minimize bandwidth.")
    ap.add_argument("--prepost", action="store_true",
                    help="Include pre-market + after-hours bars (intraday only; ignored for daily+).")
    ap.add_argument("--no-adjust", dest="adjust", action="store_false", default=True,
                    help="(name is misleading: this is 'no DIVIDEND adjust'; "
                         "splits ARE still adjusted.) Pass auto_adjust=False "
                         "to yfinance — closes stay split-adjusted but are "
                         "NOT dividend-adjusted (price-return view). Default "
                         "is split+dividend adjusted (total-return view). "
                         "NEITHER mode is the raw pre-split printed price.")
    truncation = ap.add_mutually_exclusive_group()
    truncation.add_argument("--head", type=int, default=None, metavar="N",
                            help="Keep only the first N rows (default mode only). "
                                 "Mutually exclusive with --tail.")
    truncation.add_argument("--tail", type=int, default=None, metavar="N",
                            help="Keep only the last N rows (default mode only). "
                                 "Mutually exclusive with --head.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array; "
                         "ndjson = one JSON record per line; "
                         "csv = flattened rows (default mode adds symbol/period "
                         "columns; summary mode = one row per ticker). "
                         "Multi-ticker batches add an extra `exchange_tz` column "
                         "right after `timezone`; single-ticker CSV omits it.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    # Validate window flags.
    if args.period and args.start:
        ap.error("--period and --start are mutually exclusive")
    if args.end and not args.start:
        ap.error("--end requires --start")
    # Pre-validate ISO date format upfront — yfinance accepts a wider
    # range silently, but a typo like "2023/01/15" should fail loudly here
    # rather than producing a confusing yfinance error or empty window.
    parsed_dates: dict[str, datetime] = {}
    for flag, val in (("--start", args.start), ("--end", args.end)):
        if val is not None:
            try:
                parsed_dates[flag] = datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                ap.error(f"{flag}: expected YYYY-MM-DD, got {val!r}")
    # Future-date guard: only --start matters here. A --start in the
    # future means the entire window is future and yfinance silently
    # returns empty (which our `df.empty` branch then misclassifies as
    # `not_found`). --end > today is fine — yfinance clips to today's
    # available data ("from D through whatever's current"). Local-tz
    # today (not UTC) so users in non-UTC timezones don't get their
    # local "today" rejected during the early-UTC-morning window.
    today = datetime.combine(datetime.now().date(), datetime.min.time())
    if "--start" in parsed_dates and parsed_dates["--start"] > today:
        ap.error(
            f"--start is in the future "
            f"({parsed_dates['--start'].strftime('%Y-%m-%d')} > "
            f"{today.strftime('%Y-%m-%d')})"
            f" — yfinance has no data for future dates")
    # Ordering: --start must precede --end.
    if "--start" in parsed_dates and "--end" in parsed_dates:
        if parsed_dates["--start"] >= parsed_dates["--end"]:
            ap.error(
                f"--start ({args.start}) must be strictly before --end "
                f"({args.end}); for a single trading day D, use "
                f"--start D --end D+1 (end is exclusive in yfinance)")
    if not args.period and not args.start:
        args.period = "1mo"  # default window

    if args.summary and (args.head is not None or args.tail is not None):
        ap.error("--head / --tail apply to default mode only, not --summary")

    # New mode flags: only one of {--summary, --events-only, --metadata}
    # may be set. argparse can't express a 3-way mutex on store_true
    # flags via add_mutually_exclusive_group + per-mode head/tail mutex,
    # so do it by hand.
    mode_flags = sum([args.summary, args.events_only, args.metadata])
    if mode_flags > 1:
        ap.error("--summary, --events-only, and --metadata are mutually exclusive")

    # --metadata is a one-row-per-ticker projection of history_metadata.
    # head/tail don't apply (no per-row data); --no-adjust doesn't apply
    # (metadata is invariant under adjustment); --prepost doesn't apply
    # (no bars are returned). Reject explicitly so the user gets a clear
    # error rather than a silently-ignored flag.
    if args.metadata:
        if args.head is not None or args.tail is not None:
            ap.error("--head / --tail don't apply to --metadata "
                     "(metadata is one row per ticker)")
        if args.prepost:
            ap.error("--prepost doesn't apply to --metadata")
        if not args.adjust:
            ap.error("--no-adjust doesn't apply to --metadata "
                     "(metadata is invariant under price adjustment)")

    # --events-only doesn't accept --prepost (corporate actions don't
    # fire intraday — extended-hours bars contain no event data) or any
    # intraday interval (Yahoo caps intraday windows at 7-60 days, and
    # dividends / splits are end-of-day events that don't appear on
    # individual minute bars even when they do fall in window). Both
    # combinations would silently return near-empty rows and confuse
    # the user; reject explicitly with an actionable message.
    if args.events_only and args.prepost:
        ap.error("--prepost doesn't apply to --events-only "
                 "(corporate actions are end-of-day events)")
    if args.events_only and args.interval in INTRADAY:
        ap.error(
            f"--events-only requires a daily-or-coarser --interval "
            f"(got {args.interval!r}); intraday windows are too short "
            f"and corporate actions don't fire mid-session — "
            f"use --interval 1d (default) instead")

    # --prepost is meaningless on daily+ intervals (no extended-hours
    # daily bars exist). yfinance silently ignores it; we reject explicitly
    # so the user picks an intraday interval rather than wondering why
    # there are no extended-hours rows in the output.
    if args.prepost and args.interval not in INTRADAY:
        ap.error(
            f"--prepost requires an intraday --interval "
            f"(one of: {', '.join(sorted(INTRADAY))}); "
            f"daily+ intervals have no extended-hours bars to fetch")

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    # --metadata bypasses both fetch paths — it's a per-ticker
    # Ticker.history_metadata projection (one HTTP per ticker, serial).
    # yf.download doesn't reliably populate per-Ticker.history_metadata
    # state, so batching has no benefit here. For N≥2 the cost is N
    # HTTP, not 1.
    if args.metadata:
        # fetch_metadata pins period=1d internally — user-passed window
        # flags (--period / --start / --end) are accepted by argparse
        # for CLI orthogonality but ignored here, since metadata is
        # window-invariant. Only --interval is honored (it leaks into
        # `data_granularity` on the response).
        results = [fetch_metadata(s, args.interval) for s in symbols]
        _emit(results, args.format, summary=False, metadata=True)
        return
    # N=1 keeps the original Ticker.history path (preserves native tz in
    # `timezone`, no `exchange_tz` field). N>=2 routes through yf.download —
    # one HTTP request, threaded internally, dates folded back into each
    # ticker's exchange tz. See fetch_batch docstring for the schema diff.
    if len(symbols) == 1:
        results = [fetch(symbols[0], args.period, args.interval,
                         args.summary, args.prepost, args.adjust,
                         start=args.start, end=args.end,
                         head=args.head, tail=args.tail,
                         events_only=args.events_only)]
    else:
        results = fetch_batch(symbols, args.period, args.interval,
                              args.summary, args.prepost, args.adjust,
                              start=args.start, end=args.end,
                              head=args.head, tail=args.tail,
                              events_only=args.events_only)
    _emit(results, args.format, summary=args.summary,
          events_only=args.events_only)


def _emit(results: list, fmt: str, *, summary: bool,
          events_only: bool = False, metadata: bool = False) -> None:
    """Render results to stdout in the requested format.

    json     pretty-printed JSON array (default, current behavior)
    ndjson   one JSON object per line (streaming-friendly)
    csv      flattened tabular:
               summary       one row per ticker (base + summary stats)
               events_only   one row per event (base + event fields)
               metadata      one row per ticker (metadata-only schema)
               default       one row per OHLCV bar (base + per-bar fields)

    CSV column count for the OHLCV / events / summary layouts is
    conditional: single-ticker output keeps the original 6-col base
    schema (symbol/period/start/end/interval/timezone); multi-ticker
    batch output inserts `exchange_tz` right after `timezone` so
    consumers can tell which calendar daily dates belong to. The split
    matches main()'s dispatch (N=1 → fetch, N≥2 → fetch_batch with
    exchange_tz set). Metadata layout is its own schema entirely (no
    period / start / end / interval — those don't apply to a metadata
    snapshot) and uses _METADATA_KEYS directly.
    """
    if emit_json_or_ndjson(results, fmt):
        return
    # csv
    import csv as _csv
    # lineterminator='\n' — default is '\r\n' (Windows); CRs poison `wc -l` etc.
    writer = _csv.writer(sys.stdout, lineterminator="\n")

    if metadata:
        # Metadata schema: no per-bar / per-event nesting. valid_ranges is
        # a list — JSON-encode into a single cell so the row stays flat.
        # No exchange_tz column (metadata isn't tied to a daily-date
        # calendar fold; the IANA tz lives in `exchange_timezone_name`).
        import json as _json
        cols = list(_METADATA_KEYS) + list(RESULT_META)
        writer.writerow(cols)
        for r in results:
            row = []
            for c in cols:
                v = r.get(c, "")
                if isinstance(v, list):
                    v = _json.dumps(v)
                row.append(v)
            writer.writerow(row)
        return

    # Insert exchange_tz column only when at least one result carries it
    # (i.e. fetch_batch path). Keeps single-ticker CSV at the original
    # 6-col base schema for backward compat.
    base_keys = list(_BASE_KEYS)
    if any("exchange_tz" in r for r in results):
        base_keys.insert(base_keys.index("timezone") + 1, "exchange_tz")

    if summary:
        # One row per ticker; columns = base + summary stats + error/attempts.
        cols = base_keys + list(_SUMMARY_KEYS) + list(RESULT_META)
        writer.writerow(cols)
        for r in results:
            writer.writerow([r.get(c, "") for c in cols])
        return

    # default and events-only both flatten rows. The per-row column set
    # differs (_PER_BAR_KEYS vs _PER_EVENT_KEYS), but the surrounding
    # base/meta layout is identical. error rows still get a single
    # carrying row regardless of which inner schema we're using.
    #
    # events-only adds one extra per-ticker column: `has_capital_gains_column`
    # — a fund/non-fund discriminator that's per-ticker, not per-row, but
    # repeats across the rows of one ticker so consumers using `awk` /
    # pandas without GROUP BY can still see it. Insert right before
    # RESULT_META so the column ordering is base / event / discriminator /
    # error metadata.
    inner_keys = list(_PER_EVENT_KEYS) if events_only else list(_PER_BAR_KEYS)
    extra_per_ticker = ["has_capital_gains_column"] if events_only else []
    cols = base_keys + inner_keys + extra_per_ticker + list(RESULT_META)
    writer.writerow(cols)
    for r in results:
        if "error" in r:
            # One row for the failed ticker. Iterate base_keys so column
            # ordering follows the (possibly batch-extended) header —
            # symbol/exchange_tz fill via .get(); the rest collapse to ""
            # for bogus-ticker dicts. Per-bar/event cols all blank.
            writer.writerow(
                [r.get(k, "") for k in base_keys]
                + [""] * len(inner_keys)
                + [r.get(k, "") for k in extra_per_ticker]
                + [r.get(k, "") for k in RESULT_META]
            )
            continue
        meta = [r.get(c, "") for c in base_keys]
        # Pull RESULT_META cells from r — error/error_kind .get() to ""
        # (success rows don't have them); attempts present only on retry.
        # Iterating RESULT_META keeps this drift-proof if new meta added.
        meta_cells = [r.get(k, "") for k in RESULT_META]
        # Per-ticker discriminator (events-only only) — repeated across
        # this ticker's event rows.
        ticker_extras = [r.get(k, "") for k in extra_per_ticker]
        for row in r.get("rows", []):
            writer.writerow(
                meta
                + [row.get(c, "") for c in inner_keys]
                + ticker_extras
                + meta_cells
            )


if __name__ == "__main__":
    main()
