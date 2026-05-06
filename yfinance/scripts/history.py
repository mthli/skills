#!/usr/bin/env python3
"""Fetch yfinance historical OHLCV for one or more tickers and print as JSON.

Usage:
    history.py [--period PERIOD] [--interval INTERVAL] [--summary] [--prepost] SYMBOL [SYMBOL ...]

Defaults: --period 1mo  --interval 1d

Default mode emits each ticker's full OHLCV rows. With --summary, each ticker
collapses to one aggregate object: rows_count, start/end date and close,
change_abs, change_pct, period high/low (with dates), avg_volume,
total_dividends, splits.

With --prepost, intraday rows include pre-market and after-hours bars (ignored
for daily+ intervals).

Output: JSON array on stdout, one entry per ticker. Failed tickers carry an
"error" field instead of data so a single bad symbol does not poison the batch.
"""
from __future__ import annotations

import argparse
import json
import math

import yfinance as yf

VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h",
                   "1d", "5d", "1wk", "1mo", "3mo"}

INTRADAY = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}


def _safe_float(v):
    if v is None:
        return None
    f = float(v)
    return None if math.isnan(f) else f


def _safe_int(v):
    if v is None:
        return None
    f = float(v)
    return None if math.isnan(f) else int(f)


def _fmt_index(ts, intraday: bool) -> str:
    if intraday:
        return ts.isoformat()
    return ts.strftime("%Y-%m-%d")


def fetch(symbol: str, period: str, interval: str, summary: bool, prepost: bool) -> dict:
    try:
        df = yf.Ticker(symbol).history(
            period=period, interval=interval,
            auto_adjust=True, actions=True, prepost=prepost,
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"}

    if df is None or df.empty:
        return {
            "symbol": symbol,
            "error": "no data returned (delisted, wrong suffix, or rate-limited)",
        }

    intraday = interval in INTRADAY
    tz_name = str(df.index.tz) if df.index.tz is not None else None
    base = {
        "symbol": symbol,
        "period": period,
        "interval": interval,
        "timezone": tz_name,
    }

    if summary:
        closes = df["Close"]
        highs = df["High"]
        lows = df["Low"]
        vols = df["Volume"]
        divs = df["Dividends"] if "Dividends" in df.columns else None
        splits_col = df["Stock Splits"] if "Stock Splits" in df.columns else None

        start_close = _safe_float(closes.iloc[0])
        end_close = _safe_float(closes.iloc[-1])
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
            "period_high": _safe_float(highs.max()),
            "period_high_date": _fmt_index(hi_idx, intraday) if hi_idx is not None else None,
            "period_low": _safe_float(lows.min()),
            "period_low_date": _fmt_index(lo_idx, intraday) if lo_idx is not None else None,
            "avg_volume": _safe_int(round(vols.mean())) if len(vols) else None,
            "total_dividends": _safe_float(divs.sum()) if divs is not None else 0.0,
            "splits": split_events,
        })
        return base

    rows = []
    has_div = "Dividends" in df.columns
    has_split = "Stock Splits" in df.columns
    for ts, row in df.iterrows():
        rows.append({
            "date": _fmt_index(ts, intraday),
            "open": _safe_float(row["Open"]),
            "high": _safe_float(row["High"]),
            "low": _safe_float(row["Low"]),
            "close": _safe_float(row["Close"]),
            "volume": _safe_int(row["Volume"]),
            "dividends": _safe_float(row["Dividends"]) if has_div else 0.0,
            "split_ratio": _safe_float(row["Stock Splits"]) if has_split else 0.0,
        })
    base["rows"] = rows
    return base


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch yfinance historical OHLCV.")
    ap.add_argument("--period", default="1mo", choices=sorted(VALID_PERIODS))
    ap.add_argument("--interval", default="1d", choices=sorted(VALID_INTERVALS))
    ap.add_argument("--summary", action="store_true",
                    help="Output aggregate stats instead of full rows.")
    ap.add_argument("--prepost", action="store_true",
                    help="Include pre-market and after-hours bars (intraday only).")
    ap.add_argument("symbols", nargs="+")
    args = ap.parse_args()

    results = [
        fetch(s.strip().upper(), args.period, args.interval, args.summary, args.prepost)
        for s in args.symbols if s.strip()
    ]
    print(json.dumps(results, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
