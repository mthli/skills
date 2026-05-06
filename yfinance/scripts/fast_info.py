#!/usr/bin/env python3
"""Fetch yfinance fast_info for one or more tickers and print results as JSON.

Usage:
    fast_info.py SYMBOL [SYMBOL ...]

Output:
    JSON array on stdout, one entry per ticker. Failed tickers carry an
    "error" field instead of price fields so a single bad symbol does not
    poison the whole batch.
"""
from __future__ import annotations

import json
import math
import sys

import yfinance as yf

FIELDS = [
    "last_price",
    "previous_close",
    "open",
    "day_high",
    "day_low",
    "last_volume",
    "currency",
    "market_cap",
    "exchange",
    "timezone",
    "shares",
    "fifty_day_average",
    "two_hundred_day_average",
    "year_high",
    "year_low",
]


def _denan(v):
    """Turn NaN floats into None so the result is valid JSON."""
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def fetch(symbol: str) -> dict:
    try:
        info = yf.Ticker(symbol).fast_info
        out = {"symbol": symbol}
        for f in FIELDS:
            try:
                out[f] = _denan(info[f])
            except (KeyError, AttributeError, TypeError):
                out[f] = None
        if out.get("last_price") is None:
            out["error"] = "no quote returned (delisted, wrong suffix, or rate-limited)"
        else:
            prev = out.get("previous_close")
            if prev:
                out["change_abs"] = out["last_price"] - prev
                out["change_pct"] = (out["last_price"] - prev) / prev * 100
        return out
    except Exception as e:
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: fast_info.py SYMBOL [SYMBOL ...]", file=sys.stderr)
        sys.exit(2)
    results = [fetch(s.strip().upper()) for s in sys.argv[1:] if s.strip()]
    print(json.dumps(results, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
