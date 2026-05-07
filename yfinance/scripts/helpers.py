"""Shared utilities for the yfinance wrapper scripts.

Requires Python 3.9+ (PEP 604 unions, lowercase `tuple[]` subscripts);
the import-time check below raises a friendly RuntimeError on older
interpreters.

NaN/Inf-safe converters: every converter returns `None` for missing /
non-numeric / NaN / Inf inputs so callers can serialize to JSON without
per-call defensive code.

  safe_float / safe_int   strict numeric coercion (try/except, NaN/Inf → None)
  safe_str                strip + empty-string → None
  denan                   pass-through for non-floats; NaN/Inf floats → None
                          (used by fast_info where the .fast_info object
                          returns mixed types)
  epoch_to_date           Unix epoch seconds → 'YYYY-MM-DD' (UTC)

Lookups (no Yahoo round-trip):

  TZ_BY_SUFFIX            Yahoo suffix → IANA tz (e.g. "HK" → "Asia/Hong_Kong").
  INDEX_TZ                `^FOO` index symbol → home-market IANA tz; UTC
                          fallback for unmapped indexes.
  CRYPTO_QUOTES           quote-currency codes that mark a hyphen-separated
                          ticker as crypto (e.g. `BTC-USD` → tail "USD").
  infer_exchange_tz       single entry point: applies the decision tree
                          (index / FX / futures / crypto / suffixed equity /
                          plain) to pick the right tz for daily-bar date
                          formatting. Used by history.py's batch path.

Network helpers:

  classify_error          Map a Yahoo / yfinance exception or message string
                          to one of {rate_limit, not_found, network, unknown}.
                          Used to (a) decide whether retry makes sense and
                          (b) populate `error_kind` in error dicts so the
                          caller (model) can decide what to do.
  with_retry              Run a callable with exponential backoff on
                          rate_limit / network classifications. Returns
                          (result, error_kind, attempts).

Output helpers:

  RESULT_META             ("error", "error_kind", "attempts") — per-result
                          metadata fields shared across all wrapper scripts'
                          CSV emits so adding a meta field flows everywhere.
  emit_json_or_ndjson     Shared json/ndjson writer for the four wrapper
                          scripts' --format flag. Returns True if `fmt` is
                          'json' or 'ndjson' (and emits accordingly), False
                          otherwise — caller treats False as the CSV path
                          (the only other value argparse permits).
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from typing import Callable, TypeVar

# Minimum Python check — `tuple[X, Y]` subscript and PEP 604 unions need 3.9+.
# `from __future__ import annotations` lazy-evaluates annotations on 3.7+, so
# the script itself parses fine, but `tuple[X | None, ...]` would fail at
# runtime if anything calls `typing.get_type_hints` on with_retry. Cleanest
# guarantee: refuse to import on < 3.9 with a friendly message.
if sys.version_info < (3, 9):
    raise RuntimeError(
        f"yfinance skill requires Python 3.9+ "
        f"(running {sys.version_info.major}.{sys.version_info.minor}). "
        f"Use `uv run` (auto-picks a recent Python) or upgrade your interpreter."
    )


def safe_float(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def safe_int(v):
    """Coerce to int via float; NaN/Inf/non-numeric → None.

    Note: silently truncates fractional parts (`safe_int("1.7") == 1`,
    not 2; `safe_int(-1.9) == -1`, not -2). Harmless for the yfinance
    fields we apply this to — all conceptually integer counts that
    Yahoo sometimes emits as floats:

      profile:      fullTimeEmployees
      valuation:    marketCap, enterpriseValue
      fundamentals: totalRevenue, totalCash, totalDebt,
                    freeCashflow, operatingCashflow
      analyst:      numberOfAnalystOpinions
      shares:       sharesOutstanding, floatShares, sharesShort
      fund:         totalAssets

    Revisit if applied to a field where rounding direction matters
    (e.g., a field that can be a fraction with semantic meaning).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f)


def safe_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def denan(v):
    """Pass-through for non-floats; NaN/Inf floats → None."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def epoch_to_date(ts):
    """Unix epoch seconds → 'YYYY-MM-DD' (UTC). None / 0 / non-numeric → None."""
    if ts is None or ts == 0:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


# Yahoo ticker suffix → IANA tz. Used by history.py's batch path (yf.download)
# to convert the unified-UTC index back to each ticker's exchange-local tz
# before formatting daily-bar dates — without this, a 0700.HK day-bar at
# midnight HKT is 16:00 prev-day UTC, and strftime would print the wrong
# calendar date. Covers the exchanges enumerated in SKILL.md's exchange
# table; add suffixes here when expanding coverage.
TZ_BY_SUFFIX = {
    "HK": "Asia/Hong_Kong",
    "SZ": "Asia/Shanghai",
    "SS": "Asia/Shanghai",
    "L":  "Europe/London",
    "T":  "Asia/Tokyo",
    "KS": "Asia/Seoul",
    "KQ": "Asia/Seoul",       # KOSDAQ
    "DE": "Europe/Berlin",    # Xetra
    "F":  "Europe/Berlin",    # Frankfurt floor
    "PA": "Europe/Paris",
    "AS": "Europe/Amsterdam",
    "BR": "Europe/Brussels",
    "MI": "Europe/Rome",
    "MC": "Europe/Madrid",
    "SW": "Europe/Zurich",
    "ST": "Europe/Stockholm",
    "OL": "Europe/Oslo",
    "CO": "Europe/Copenhagen",
    "HE": "Europe/Helsinki",
    "TO": "America/Toronto",
    "V":  "America/Toronto",  # TSX Venture
    "AX": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    "NS": "Asia/Kolkata",
    "BO": "Asia/Kolkata",
    "SI": "Asia/Singapore",
    "BK": "Asia/Bangkok",
    "JK": "Asia/Jakarta",
    "TW": "Asia/Taipei",
    "MX": "America/Mexico_City",
    "SA": "America/Sao_Paulo",
    "BA": "America/Argentina/Buenos_Aires",
}

# Major equity indexes (Yahoo `^FOO` notation): explicit tz so daily-bar
# dates land on the home market's calendar rather than UTC. Without this
# map a `^N225` day-bar (midnight Asia/Tokyo = 15:00 prev-day UTC) would
# strftime to the wrong calendar date — same off-by-one trap that motivated
# the per-suffix fold for `0700.HK`. Indexes not in this map fall back to
# UTC (consistent with crypto/FX/futures); add explicit entries when a
# specific index needs its home tz.
INDEX_TZ = {
    # US
    "^GSPC": "America/New_York",   # S&P 500
    "^DJI":  "America/New_York",   # Dow
    "^IXIC": "America/New_York",   # Nasdaq Composite
    "^NDX":  "America/New_York",   # Nasdaq 100
    "^RUT":  "America/New_York",   # Russell 2000
    "^VIX":  "America/New_York",   # VIX
    # Asia
    "^N225":  "Asia/Tokyo",        # Nikkei 225
    "^TPX":   "Asia/Tokyo",        # TOPIX
    "^HSI":   "Asia/Hong_Kong",    # Hang Seng
    "^HSCE":  "Asia/Hong_Kong",    # HSCEI (H-shares)
    "^SSEC":  "Asia/Shanghai",     # Shanghai Composite
    "^SZSC":  "Asia/Shanghai",     # Shenzhen Composite
    "^STI":   "Asia/Singapore",    # Straits Times
    "^KS11":  "Asia/Seoul",        # KOSPI
    "^TWII":  "Asia/Taipei",       # Taiwan Weighted
    "^BSESN": "Asia/Kolkata",      # BSE Sensex
    "^NSEI":  "Asia/Kolkata",      # NIFTY 50
    # Europe
    "^FTSE":     "Europe/London",
    "^FTMC":     "Europe/London",  # FTSE 250
    "^GDAXI":    "Europe/Berlin",  # DAX
    "^FCHI":     "Europe/Paris",   # CAC 40
    "^STOXX50E": "Europe/Paris",   # Euro Stoxx 50
    "^AEX":      "Europe/Amsterdam",
    "^IBEX":     "Europe/Madrid",
    # Americas (non-US)
    "^GSPTSE": "America/Toronto",      # TSX Composite
    "^BVSP":   "America/Sao_Paulo",    # Bovespa
    "^MXX":    "America/Mexico_City",  # IPC
    # Oceania
    "^AXJO": "Australia/Sydney",   # ASX 200
}

# Quote-currency codes that mark a hyphen-separated ticker as crypto
# (e.g., `BTC-USD`, `ETH-USDT`). Crypto trades 24/7, so UTC is the natural
# daily-bar boundary. Conservatively scoped — `BRK-B` (US class share)
# tail "B" isn't here, so it falls through to the equity default.
CRYPTO_QUOTES = frozenset({
    "USD", "USDT", "USDC", "BUSD", "DAI",
    "EUR", "GBP", "JPY", "CNY", "KRW",
    "BTC", "ETH",
})


def infer_exchange_tz(symbol: str) -> str:
    """Infer the tz used to format daily date strings for `symbol`.

    Decision tree (first match wins):
      1. ^FOO            → INDEX_TZ map; UTC if not in map
      2. FOO=X / FOO=F   → UTC (FX / futures, no single home market)
      3. FOO-QUOTE       → UTC when QUOTE ∈ CRYPTO_QUOTES (24/7 trading)
      4. FOO.SUFFIX      → TZ_BY_SUFFIX; America/New_York if suffix unknown
      5. plain ticker    → America/New_York (US equity default)

    Used by history.py's batch path to fold per-ticker daily dates back to
    each instrument's natural trading-day calendar. The equity-suffix
    fallback to ET is wrong for unmapped non-US suffixes (silently produces
    off-by-one dates) — surface that in smoke if it shows up and add the
    suffix to TZ_BY_SUFFIX.
    """
    sym = symbol.upper()
    if sym.startswith("^"):
        return INDEX_TZ.get(sym, "UTC")
    if sym.endswith("=X") or sym.endswith("=F"):
        return "UTC"
    if "-" in sym:
        head, _, tail = sym.rpartition("-")
        if head and tail in CRYPTO_QUOTES:
            return "UTC"
    if "." in sym:
        suffix = sym.rsplit(".", 1)[1]
        return TZ_BY_SUFFIX.get(suffix, "America/New_York")
    return "America/New_York"


def classify_error(exc: Exception | None = None, msg: str = "") -> str:
    """Heuristic mapping of yfinance / Yahoo exceptions to a stable enum.

    Returns one of: rate_limit, not_found, network, unknown. Yahoo's HTTP
    errors land in different yfinance exception classes depending on
    which internal code path raised them; classifying by text content is
    more robust than exception-type matching.
    """
    text = ((str(exc) if exc else "") + " " + msg).lower()
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return "rate_limit"
    if "404" in text or "not found" in text or "delisted" in text \
            or "no data found" in text or "no quote" in text:
        return "not_found"
    # "timed out" (with space) is what stdlib socket / TimeoutError str()
    # actually produces; "timeout" (one word) is the substring most user
    # docs reference. Match both.
    if ("timeout" in text or "timed out" in text or "connection" in text
            or "temporarily unavailable" in text):
        return "network"
    return "unknown"


T = TypeVar("T")


# Per-result metadata fields populated by the fetch() error / retry paths
# in every wrapper script. Centralized so all three (and any future modes)
# stay consistent — adding a meta field auto-flows into all CSV cols.
RESULT_META = ("error", "error_kind", "attempts")


def emit_json_or_ndjson(results: list, fmt: str) -> bool:
    """Shared JSON / NDJSON output for the three wrapper scripts' --format
    flag. Returns True if `fmt` was handled (json or ndjson), False if it's
    'csv' so the caller can run its own per-mode CSV emit. Keeps the JSON
    paths in one place — only CSV varies per mode.
    """
    if fmt == "json":
        print(json.dumps(results, indent=2, default=str, ensure_ascii=False))
        return True
    if fmt == "ndjson":
        for r in results:
            print(json.dumps(r, default=str, ensure_ascii=False))
        return True
    return False


def with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T | None, str | None, int]:
    """Run `fn()` with exponential backoff + jitter on transient errors.

    Backs off `base_delay × 2^(attempt-1) + uniform(0, base_delay/2)`
    seconds on rate_limit / network classifications; gives up immediately
    on not_found / unknown (retry won't help). Jitter avoids
    thundering-herd when multiple tickers hit the same 429.

    Returns `(result, error_kind, attempts_used)` — `error_kind` is None
    on success, set to the final classification on failure. Never raises;
    always returns a tuple.

    `sleep` is a parameter so unit tests can inject a recording mock
    without actually sleeping.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    last_kind: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(), None, attempt
        except Exception as exc:
            last_kind = classify_error(exc)
            if last_kind in ("rate_limit", "network") and attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                delay += random.uniform(0, base_delay / 2)
                sleep(delay)
                continue
            return None, last_kind, attempt
    # Defensive — loop exits via return; this is unreachable.
    return None, last_kind, attempts
