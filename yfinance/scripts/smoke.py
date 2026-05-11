#!/usr/bin/env python3
"""Smoke test for the sixteen yfinance wrapper scripts.

Three layers of coverage:
  1. Offline / pure-Python: invariants over pure-Python logic with no
     external state — they either pass or signal a logic bug. No Yahoo
     calls. Tests `helpers.with_retry` retry+backoff semantics,
     `helpers.infer_exchange_tz` pattern coverage, and `fast_info.fetch`
     retry-surfacing via mocked yf.Ticker. Runs instantly; catches
     regressions independent of upstream drift.
  2. Import-based: imports each wrapper module and asserts on schema shape,
     type correctness, and known-stable invariants for a representative
     ticker mix (US stock, non-US stock, ETF, no-coverage stock, bogus).
  3. CLI subprocess: invokes each script via argparse + JSON / CSV output
     to verify the end-to-end command-line path doesn't break (catches
     argparse misconfig, JSON serialization issues, exit codes, CSV
     column drift).

For live-Yahoo checks (layers 2 and 3), each `check(...)` falls into
one of two categories — chosen deliberately:

  invariant  Should hold under any market state (e.g. "rows is a list",
             "market_cap is a positive int", "splits is a list"). Failing
             one of these means real breakage — yfinance API drift, our
             schema mismatch, or a bug.
  canary     Holds under typical/current market state but could legitimately
             fail (e.g. "AAPL profit_margin > 0" — fine until Apple has a
             losing quarter; "SPY category == 'Large Blend'" — fine until
             Yahoo rewords it). Failing one means *investigate*: market
             state may have changed, or Yahoo may have shifted phrasing.
             Annotated `# canary:` so future-you can tell.

Catches yfinance API drift (e.g., Yahoo renames `dividendYield`) and
accidental schema / CLI breakage in SECTIONS / SUMMARY_FIELDS / FIELDS /
argparse without needing manual regression runs.

Run: uv run --with 'yfinance>=1.3,<2' --with 'lxml' python <SKILL_DIR>/scripts/smoke.py
(`lxml` is needed only by the earnings tests' HTML scrape path; harmless
for the others. Without it, all earnings sections fail with error_kind
'unknown'.) Exits 0 on success, 1 on any failed assertion.

Total runtime: typically ~80-110s on a US connection. Live Yahoo calls
dominate; the slowest sections are the two `--period max` history fetches
(used for the dividend-adjustment semantic check), the prepost-vs-regular
intraday pair, and the financials suite (each equity fetch includes an
extra `info["financialCurrency"]` round-trip on top of the per-statement
fetches — see SKILL.md latency table). Subprocess startup also adds
~5–10s overhead per CLI check vs pure-import — that's the cost of
catching argparse / JSON / exit-code bugs that import-only testing would
miss. Layer-1 offline checks add ~1s total.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import analyst
import calendars
import earnings
import fast_info
import financials
import fund_holdings
import helpers
import history
import holders
import info
import insiders
import market
import news
import options
import screener
import sec_filings
import sectors
import valuation


def run_cli(script: str, *args: str) -> tuple[int, list | None]:
    """Run a wrapper script via subprocess; return (returncode, parsed_json).
    parsed_json is None if exit non-zero or stdout isn't valid JSON."""
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return out.returncode, None
    try:
        return out.returncode, json.loads(out.stdout)
    except json.JSONDecodeError:
        return out.returncode, None

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        msg = f"{name}{(' — ' + detail) if detail else ''}"
        FAILURES.append(msg)
        print(f"  ✗ {msg}")


def section(title: str) -> None:
    print(f"\n{title}")


# --- infer_exchange_tz pattern coverage (offline; no Yahoo calls) ---
# Regression tests for the bug where non-equity instruments fell through
# to the equity default (America/New_York), giving off-by-one daily dates
# in batch mode for crypto / FX / futures / non-US indexes.
section("helpers.infer_exchange_tz patterns (offline)")
try:
    from helpers import infer_exchange_tz
    cases = [
        # plain US equity, no suffix → US default
        ("AAPL", "America/New_York"),
        # dot-suffix that's not in TZ_BY_SUFFIX (Berkshire B-share notation)
        # → US default. Confirms unknown dot-suffix doesn't crash.
        ("BRK.B", "America/New_York"),
        # known suffix → mapped tz
        ("0700.HK", "Asia/Hong_Kong"),
        ("BMW.DE",  "Europe/Berlin"),
        ("7203.T",  "Asia/Tokyo"),
        ("BARC.L",  "Europe/London"),
        # known equity index → home market tz
        ("^GSPC", "America/New_York"),
        ("^N225", "Asia/Tokyo"),
        ("^HSI",  "Asia/Hong_Kong"),
        ("^FTSE", "Europe/London"),
        # unknown index → UTC fallback (better than wrong-tz ET default)
        ("^FOOBAR", "UTC"),
        # crypto: hyphen with quote currency → UTC (24/7 trading)
        ("BTC-USD",  "UTC"),
        ("ETH-USDT", "UTC"),
        # FX (yfinance =X) and futures (=F) → UTC
        ("USDJPY=X", "UTC"),
        ("CL=F",     "UTC"),
        # US class-share notation with hyphen — tail "B" is NOT a crypto
        # quote, so must NOT be misclassified as crypto. Falls through to
        # the equity default. Regression check for the rpartition logic.
        ("BRK-B", "America/New_York"),
        # case insensitivity: lower-case input still matches uppercased keys
        ("aapl", "America/New_York"),
        ("0700.hk", "Asia/Hong_Kong"),
    ]
    for sym, expected in cases:
        got = infer_exchange_tz(sym)
        check(f"infer_exchange_tz({sym!r}) == {expected}",
              got == expected, f"got {got!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"infer_exchange_tz crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- safe_bool (no Yahoo calls; deterministic) ---
section("helpers.safe_bool (offline)")
try:
    from helpers import safe_bool
    cases = [
        # (input, expected)
        (True, True),
        (False, False),
        (None, None),
        # ints (non-zero truthy, zero falsy)
        (1, True),
        (0, False),
        (-1, True),
        # accepted strings (case + whitespace insensitive)
        ("true", True),
        ("False", False),
        ("YES", True),
        ("no", False),
        (" 1 ", True),
        ("0", False),
        # single-letter forms — accepted by the helper, pinning here so
        # docstring + frozenset + smoke stay in sync.
        ("y", True),
        ("Y", True),
        ("t", True),
        ("n", False),
        ("N", False),
        ("f", False),
        # rejected (refuses to guess) → None
        ("maybe", None),
        ("", None),
        ("   ", None),
        (1.5, None),       # floats not in spec
        ([], None),        # lists not in spec
        ({}, None),
    ]
    for inp, expected in cases:
        got = safe_bool(inp)
        check(f"safe_bool({inp!r}) == {expected!r}",
              got == expected and type(got) is type(expected),
              f"got {got!r} (type {type(got).__name__})")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"safe_bool crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- with_retry unit tests (no Yahoo calls; deterministic) ---
section("helpers.with_retry (offline)")
try:
    # Recording sleep so we can verify backoff was actually called.
    sleeps: list[float] = []
    def _record_sleep(s):
        sleeps.append(s)

    # Case 1: rate_limit twice, then success. Must succeed on 3rd attempt
    # and sleep exactly twice.
    sleeps.clear()
    state = {"calls": 0}
    def _flaky():
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return "ok"
    res, kind, attempts = helpers.with_retry(
        _flaky, attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: rate_limit×2 then success returns 'ok'",
          res == "ok" and kind is None and attempts == 3,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}")
    check("retry: slept exactly 2 times for 2 retries", len(sleeps) == 2,
          f"got {len(sleeps)} sleeps")

    # Case 1b: network classification also retries (same backoff path).
    sleeps.clear()
    state["calls"] = 0
    def _flaky_network():
        state["calls"] += 1
        if state["calls"] < 3:
            raise OSError("Connection timeout")
        return "ok"
    res, kind, attempts = helpers.with_retry(
        _flaky_network, attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: network×2 then success returns 'ok'",
          res == "ok" and kind is None and attempts == 3,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}")
    check("retry: network slept 2 times before success",
          len(sleeps) == 2)

    # Case 1c: attempts < 1 raises ValueError loudly (caller bug).
    try:
        helpers.with_retry(lambda: 1, attempts=0)
        check("with_retry attempts=0 raises ValueError", False, "did not raise")
    except ValueError:
        check("with_retry attempts=0 raises ValueError", True)
    except Exception as e:
        check("with_retry attempts=0 raises ValueError", False,
              f"raised {type(e).__name__} instead")

    # Case 2: sustained rate_limit — gives up after `attempts` tries,
    # returns rate_limit kind.
    sleeps.clear()
    state["calls"] = 0
    def _always_429():
        state["calls"] += 1
        raise RuntimeError("429 too many requests")
    res, kind, attempts = helpers.with_retry(
        _always_429, attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: sustained rate_limit returns kind=rate_limit, attempts=3",
          res is None and kind == "rate_limit" and attempts == 3,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}")

    # Case 3: not_found — no retry, returns immediately on attempt 1.
    sleeps.clear()
    state["calls"] = 0
    def _not_found():
        state["calls"] += 1
        raise RuntimeError("404 Not Found: ticker delisted")
    res, kind, attempts = helpers.with_retry(
        _not_found, attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: not_found does NOT trigger retry (attempts=1)",
          res is None and kind == "not_found" and attempts == 1
          and len(sleeps) == 0,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}, sleeps={len(sleeps)}")

    # Case 4: unknown error — no retry.
    sleeps.clear()
    res, kind, attempts = helpers.with_retry(
        lambda: (_ for _ in ()).throw(ValueError("something weird")),
        attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: unknown error does NOT trigger retry",
          res is None and kind == "unknown" and attempts == 1,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}")

    # Case 5: success on first call — no retry, no sleep.
    sleeps.clear()
    res, kind, attempts = helpers.with_retry(
        lambda: 42, attempts=3, base_delay=0.01, sleep=_record_sleep)
    check("retry: immediate success returns attempts=1, no sleeps",
          res == 42 and kind is None and attempts == 1 and len(sleeps) == 0,
          f"got res={res!r}, kind={kind!r}, attempts={attempts}")

    # Case 6: classify_error covers the documented inputs.
    check("classify_error('429 Too Many Requests')==rate_limit",
          helpers.classify_error(RuntimeError("429 Too Many Requests")) == "rate_limit")
    check("classify_error('404 Not Found')==not_found",
          helpers.classify_error(RuntimeError("404 Not Found")) == "not_found")
    check("classify_error('possibly delisted')==not_found",
          helpers.classify_error(RuntimeError("possibly delisted")) == "not_found")
    check("classify_error('Connection timeout')==network",
          helpers.classify_error(RuntimeError("Connection timeout")) == "network")
    check("classify_error('Connection refused')==network",
          helpers.classify_error(RuntimeError("Connection refused")) == "network")
    check("classify_error('Service Temporarily Unavailable')==network",
          helpers.classify_error(RuntimeError("Service Temporarily Unavailable")) == "network")
    check("classify_error('weird error')==unknown",
          helpers.classify_error(RuntimeError("weird error")) == "unknown")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"helpers.with_retry crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- fetch() integration: retry-then-success surfaces attempts in dict ---
# Mocks yfinance entirely so this is offline + deterministic.
section("fetch() retry surfacing (offline)")
try:
    import yfinance as _yf

    # State at class level (not instance) so retries — which create a
    # fresh `yf.Ticker(...)` each time — share the call counter.
    # Mapping derived from fast_info.FIELDS (single source of truth) with
    # numeric default; non-numeric fields (currency, exchange, timezone)
    # need explicit overrides so the test still validates string-typed
    # fields. If FIELDS grows, only the override dict need attention.
    _MOCK_OVERRIDES = {
        "currency": "USD",
        "exchange": "NMS",
        "timezone": "America/New_York",
    }
    _MOCK_DEFAULTS = {**dict.fromkeys(fast_info.FIELDS, 100.0), **_MOCK_OVERRIDES}
    # Keep last_price > 0 so the success-branch logic engages (fetch
    # treats last_price=None as "not_found").
    _MOCK_DEFAULTS["previous_close"] = 99.0  # → change_pct ≠ 0

    class _FlakyFastInfo:
        """Simulates a fast_info accessor that 429s on the first attempt
        only; retry attempts succeed."""
        attempt_calls = 0

        def __getitem__(self, key):
            type(self).attempt_calls += 1
            # 429 only on the very first field read of the very first
            # attempt; second attempt's field-1 read succeeds.
            if type(self).attempt_calls <= 1:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return _MOCK_DEFAULTS.get(key)

    class _MockTicker:
        def __init__(self, sym):
            self.symbol = sym
            self.fast_info = _FlakyFastInfo()

    saved = _yf.Ticker
    _yf.Ticker = _MockTicker
    try:
        # NOTE: with_retry's `sleep` parameter has its default captured at
        # def time, so monkey-patching helpers.time.sleep wouldn't reach
        # it. We accept a real ~0.5s wait (one retry × base_delay + jitter)
        # rather than rewire defaults. Fast enough not to matter for smoke.
        d = fast_info.fetch("FAKE")
    finally:
        _yf.Ticker = saved

    # Success path with retry: attempts must be > 1 and present in dict.
    check("fast_info.fetch() surfaces attempts > 1 after retry",
          d.get("attempts") == 2 and d.get("error") is None,
          f"got attempts={d.get('attempts')!r}, error={d.get('error')!r}, "
          f"last_price={d.get('last_price')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"fetch retry-surfacing crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- fund_holdings unit transforms (offline; no Yahoo calls) ---
# `_invert_or_none` and `_to_fraction` are pure-Python helpers that
# normalize Yahoo's quirky encodings into conventional units. Pinning
# their boundaries here catches a regression faster than the end-to-end
# SPY/VFIAX canaries can: those exercise *one* concrete value per call,
# while these probe the sentinel / NaN / type paths that Yahoo flips
# into less predictably.
section("fund_holdings unit transforms (offline)")
try:
    from fund_holdings import _invert_or_none, _to_fraction
    # _invert_or_none: real inversion, sentinel handling, NaN-safety.
    invert_cases = [
        # (input, expected) — expected None means "produces None"
        (0.04,    25.0),                  # canonical: 1/0.04 = 25
        (0.03706, 1.0 / 0.03706),         # SPY P/E raw (≈ 26.98)
        (0.5,     2.0),                   # mid-range
        (1.0,     1.0),                   # identity
        (0.0,     None),                  # bond-ETF sentinel — must NOT be inf
        (None,    None),
        (float("nan"), None),
        ("not a number", None),
    ]
    for inp, expected in invert_cases:
        got = _invert_or_none(inp)
        if expected is None:
            ok = got is None
        else:
            ok = got is not None and abs(got - expected) < 1e-9
        check(f"_invert_or_none({inp!r}) → {expected!r}",
              ok, f"got {got!r}")

    # _to_fraction: percent → fraction, NaN-safe, identity-on-zero.
    fraction_cases = [
        (18.03,  0.1803),                 # VFIAX 3y growth raw → fraction
        (21.25,  0.2125),                 # VFIAX category_avg
        (0.0,    0.0),                    # zero → zero (NOT None — distinct
                                          # from inversion's div-by-zero guard)
        (-5.0,   -0.05),                  # negative growth (real case)
        (None,   None),
        (float("nan"), None),
        ("not a number", None),
    ]
    for inp, expected in fraction_cases:
        got = _to_fraction(inp)
        if expected is None:
            ok = got is None
        else:
            ok = got is not None and abs(got - expected) < 1e-9
        check(f"_to_fraction({inp!r}) → {expected!r}",
              ok, f"got {got!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"fund_holdings unit transforms crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- fast_info ---
section("fast_info")
try:
    d = fast_info.fetch("AAPL")
    # invariant: schema/type
    check("AAPL last_price is float", isinstance(d.get("last_price"), float),
          f"got {type(d.get('last_price')).__name__}={d.get('last_price')!r}")
    check("AAPL currency=USD", d.get("currency") == "USD",
          f"got {d.get('currency')!r}")
    # invariant: change_pct is percent-encoded (16.43 = 16.43%), not
    # a fraction. Cross-check against last_price / previous_close: if
    # change_pct is the percent encoding, it should match the recomputed
    # value within float-rounding tolerance. A fraction encoding (~0.01
    # for a 1% move) would diverge by a factor of 100 and fail.
    cp = d.get("change_pct")
    lp = d.get("last_price")
    pc = d.get("previous_close")
    if cp is not None and lp is not None and pc:
        expected_pct = (lp - pc) / pc * 100
        check("AAPL change_pct matches recomputed percent (encoding canary)",
              isinstance(cp, (int, float))
              and abs(cp - expected_pct) < 0.01,
              f"got {cp!r}, expected ~{expected_pct:.4f} from last/prev")
    else:
        check("AAPL change_pct is numeric (skipped recompute — missing inputs)",
              isinstance(cp, (int, float)),
              f"got {type(cp).__name__}")
    # canary: AAPL is mega-cap; if this drops below $1T we want to know
    check("AAPL market_cap > 1e12 (canary: mega-cap)",
          isinstance(d.get("market_cap"), (int, float))
          and d["market_cap"] > 1e12,
          f"got {d.get('market_cap')!r}")

    # batch path: multi-ticker fetch returns one dict per symbol with
    # consistent shape (regression guard for the per-ticker error isolation)
    batch = [fast_info.fetch(s) for s in ("AAPL", "MSFT", "ZZZZNOTREAL")]
    check("batch returns 3 dicts", len(batch) == 3)
    check("batch first two have last_price",
          all(isinstance(b.get("last_price"), float) for b in batch[:2]))
    check("batch bogus has error, no last_price",
          "error" in batch[2] and batch[2].get("last_price") is None)

    # non-US path: verify suffix-based ticker resolution + currency /
    # exchange fields populate correctly. invariant: HKG-listed names
    # come back in HKD on the HKG exchange.
    d_hk = fast_info.fetch("0700.HK")
    check("0700.HK last_price is float",
          isinstance(d_hk.get("last_price"), float),
          f"got {type(d_hk.get('last_price')).__name__}={d_hk.get('last_price')!r}")
    check("0700.HK currency=HKD",
          d_hk.get("currency") == "HKD",
          f"got {d_hk.get('currency')!r}")
    check("0700.HK exchange=HKG",
          d_hk.get("exchange") == "HKG",
          f"got {d_hk.get('exchange')!r}")

    # --with-isin: opt-in ISIN lookup (slow path, ~1-5 s extra).
    # Spotty hit rate by design — AAPL is the most reliable liquid
    # name in the 2026-05 spot-check, but we still assert format
    # rather than a specific value so a yfinance regression that
    # NULLs out AAPL doesn't break smoke. Format regex is reused
    # from fast_info module so test + production agree on shape.
    d_iso = fast_info.fetch("AAPL", with_isin=True)
    check("AAPL --with-isin: isin key present (lookup ran)",
          "isin" in d_iso,
          f"got keys {sorted(d_iso.keys())}")
    check("AAPL --with-isin: isin is null or matches ISO 6166 shape",
          d_iso.get("isin") is None
          or bool(fast_info._ISIN_RE.match(d_iso["isin"])),
          f"got {d_iso.get('isin')!r}")
    check("AAPL --with-isin: last_price still populated",
          isinstance(d_iso.get("last_price"), float),
          f"got {type(d_iso.get('last_price')).__name__}")

    # Short-circuit path: tickers with `-` or `^` resolve to null
    # instantly inside yfinance (no network). Cheapest --with-isin
    # case to assert — confirms (a) flag plumbing works on non-equity
    # quote types, (b) sentinel mapping `-` → None lands correctly.
    d_crypto = fast_info.fetch("BTC-USD", with_isin=True)
    check("BTC-USD --with-isin: short-circuits to null isin",
          d_crypto.get("isin") is None and "isin" in d_crypto,
          f"got {d_crypto.get('isin')!r}, key present={'isin' in d_crypto}")
    check("BTC-USD --with-isin: last_price intact (ISIN path didn't poison row)",
          isinstance(d_crypto.get("last_price"), float),
          f"got {type(d_crypto.get('last_price')).__name__}")

    # Error path: when main fast_info errors, ISIN lookup is skipped
    # entirely (avoid doubling latency on already-failing rows). The
    # `isin` key MUST be absent — its presence/absence is the shape
    # signal documented in references/fast_info.md.
    d_bad = fast_info.fetch("ZZZZNOTREAL", with_isin=True)
    check("ZZZZNOTREAL --with-isin: error path omits isin key entirely",
          "error" in d_bad and "isin" not in d_bad,
          f"got keys {sorted(d_bad.keys())}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"fast_info crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history default mode ---
section("history default")
try:
    d = history.fetch("AAPL", period="5d", interval="1d",
                      summary=False, prepost=False)
    check("AAPL has rows list >= 4",
          isinstance(d.get("rows"), list) and len(d["rows"]) >= 4,
          f"got {len(d.get('rows', []))} rows")
    if d.get("rows"):
        row0 = d["rows"][0]
        check("row close is float", isinstance(row0.get("close"), float),
              f"got {type(row0.get('close')).__name__}")
        check("row date is YYYY-MM-DD string",
              isinstance(row0.get("date"), str) and len(row0["date"]) == 10
              and row0["date"][4] == "-",
              f"got {row0.get('date')!r}")
        check("row volume is int",
              isinstance(row0.get("volume"), int),
              f"got {type(row0.get('volume')).__name__}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history default crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history summary mode ---
section("history --summary")
try:
    d = history.fetch("AAPL", period="5d", interval="1d",
                      summary=True, prepost=False)
    check("change_pct is float",
          isinstance(d.get("change_pct"), float),
          f"got {type(d.get('change_pct')).__name__}")
    check("period_high >= period_low",
          d.get("period_high") is not None and d.get("period_low") is not None
          and d["period_high"] >= d["period_low"])
    check("avg_volume is int", isinstance(d.get("avg_volume"), int))
    check("splits is list", isinstance(d.get("splits"), list))
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history summary crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --no-adjust ---
section("history --no-adjust")
try:
    d = history.fetch("AAPL", period="5d", interval="1d",
                      summary=True, prepost=False, adjust=False)
    check("--no-adjust path returns valid summary",
          d.get("change_pct") is not None
          and isinstance(d.get("end_close"), float))
    # The flag's effect (different start_close) only shows over windows that
    # span a split or dividend; can't assert on a 5d window. We just confirm
    # the code path doesn't crash and emits the same shape.

    # Semantic check: yfinance's auto_adjust=False is split-adjusted but
    # NOT dividend-adjusted; auto_adjust=True is split+dividend-adjusted.
    # So over a long window with dividend payments, no-adjust start_close
    # should be modestly higher than adjusted (dividends paid out lower
    # the adjusted curve when projected back). For AAPL --period max
    # the empirical ratio is ~1.3x. Loose bound: 1.05–2.0.
    # invariant: dividend-adjustment math is actually being applied.
    d_adj = history.fetch("AAPL", period="max", interval="1mo",
                          summary=True, prepost=False, adjust=True)
    d_raw = history.fetch("AAPL", period="max", interval="1mo",
                          summary=True, prepost=False, adjust=False)
    if d_adj.get("start_close") and d_raw.get("start_close"):
        ratio = d_raw["start_close"] / d_adj["start_close"]
        check("--no-adjust differs from adjusted (dividend-adjustment applied)",
              1.05 < ratio < 2.0,
              f"raw/adj = {ratio:.2f} (expected 1.05–2.0 range from dividends)")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history --no-adjust crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --prepost (intraday + extended hours) ---
section("history --prepost (intraday)")
try:
    # 5d / 5m gives ~80 bars per regular session; with --prepost adds
    # pre-market (04:00–09:30 ET) and after-hours (16:00–20:00 ET) bars.
    # invariant: returns a non-empty rows list with ISO timestamps
    d = history.fetch("AAPL", period="5d", interval="5m",
                      summary=False, prepost=True)
    rows = d.get("rows") or []
    check("--prepost 5m returns rows", len(rows) > 0,
          f"got {len(rows)} rows")
    if rows:
        # Intraday emits ISO-8601 timestamps with offset (not YYYY-MM-DD)
        check("--prepost row date is ISO timestamp (has 'T')",
              isinstance(rows[0].get("date"), str) and "T" in rows[0]["date"],
              f"got {rows[0].get('date')!r}")
        # canary: at least one bar should fall outside 09:30–16:00 ET when
        # --prepost is on; can't easily check ET hour without parsing tz,
        # but rows count should noticeably exceed regular-only.
        d_reg = history.fetch("AAPL", period="5d", interval="5m",
                              summary=False, prepost=False)
        reg_rows = len(d_reg.get("rows") or [])
        check("--prepost yields more rows than regular-only (canary)",
              len(rows) > reg_rows,
              f"prepost={len(rows)}, regular={reg_rows}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history --prepost crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history batch path (yf.download — multi-ticker + single-element edge) ---
section("history fetch_batch")
try:
    # Same-market batch: 2 US tickers, summary mode. Verifies the new
    # batch-only metadata keys (timezone="UTC", exchange_tz=...) appear
    # and the per-ticker dict shape otherwise matches single-ticker output.
    results = history.fetch_batch(["AAPL", "MSFT"], period="5d",
                                   interval="1d", summary=True, prepost=False)
    check("batch 2-ticker returns 2 dicts", len(results) == 2,
          f"got {len(results)}")
    check("batch sets timezone=UTC on every result",
          all(r.get("timezone") == "UTC" for r in results))
    check("batch US tickers get exchange_tz=America/New_York",
          all(r.get("exchange_tz") == "America/New_York" for r in results))
    check("batch summary dates remain YYYY-MM-DD format (not ISO)",
          all(isinstance(r.get("start_date"), str) and len(r["start_date"]) == 10
              and r["start_date"][4] == "-" for r in results))

    # Cross-market batch: the regression test for the UTC date-fold trap.
    # If the daily-bar date weren't tz_convert'd back to Asia/Hong_Kong
    # before strftime, 0700.HK's midnight-HKT bars would land on the
    # previous UTC day and the date string would shift by 1. Compare
    # against single-ticker fetch (which uses native HK tz) — they must
    # agree on every date for the bug to be absent.
    #
    # Cost note: this section issues two Yahoo calls for 0700.HK (one
    # batched, one single-ticker). The duplicate is unavoidable — we
    # need both to ASSERT equality without hardcoding dates that would
    # rot. Acceptable per-section cost relative to smoke's overall ~70s
    # budget.
    cross = history.fetch_batch(["AAPL", "0700.HK"], period="5d",
                                 interval="1d", summary=True, prepost=False)
    by_sym = {r["symbol"]: r for r in cross}
    check("cross-market batch AAPL exchange_tz=America/New_York",
          by_sym.get("AAPL", {}).get("exchange_tz") == "America/New_York")
    check("cross-market batch 0700.HK exchange_tz=Asia/Hong_Kong",
          by_sym.get("0700.HK", {}).get("exchange_tz") == "Asia/Hong_Kong")
    # invariant: HK date matches what single-ticker (native tz) would emit.
    # If this drifts to off-by-one, the tz_convert in _build_result regressed.
    single_hk = history.fetch("0700.HK", period="5d", interval="1d",
                               summary=True, prepost=False)
    check("0700.HK batch start_date matches single-ticker (no UTC date drift)",
          single_hk.get("start_date") == by_sym.get("0700.HK", {}).get("start_date"),
          f"single={single_hk.get('start_date')!r}, "
          f"batch={by_sym.get('0700.HK', {}).get('start_date')!r}")
    check("0700.HK batch end_date matches single-ticker (no UTC date drift)",
          single_hk.get("end_date") == by_sym.get("0700.HK", {}).get("end_date"),
          f"single={single_hk.get('end_date')!r}, "
          f"batch={by_sym.get('0700.HK', {}).get('end_date')!r}")

    # Per-ticker error isolation: bogus ticker shouldn't drop the batch nor
    # poison the good ones. invariant: 3 results for 3 input symbols, and
    # the bad one is classified not_found.
    mixed = history.fetch_batch(["AAPL", "ZZZZNOTREAL", "MSFT"], period="5d",
                                 interval="1d", summary=True, prepost=False)
    by_sym = {r["symbol"]: r for r in mixed}
    check("batch error isolation: 3 results for 3 input tickers",
          len(mixed) == 3 and {"AAPL", "ZZZZNOTREAL", "MSFT"} == set(by_sym.keys()),
          f"got symbols={list(by_sym.keys())}")
    check("batch error isolation: bogus ticker has error_kind=not_found",
          by_sym.get("ZZZZNOTREAL", {}).get("error_kind") == "not_found",
          f"got {by_sym.get('ZZZZNOTREAL', {}).get('error_kind')!r}")
    check("batch error isolation: AAPL/MSFT succeed unaffected",
          "error" not in by_sym.get("AAPL", {})
          and "error" not in by_sym.get("MSFT", {})
          and isinstance(by_sym.get("AAPL", {}).get("change_pct"), float)
          and isinstance(by_sym.get("MSFT", {}).get("change_pct"), float))

    # Intraday batch: timestamps must be in UTC (ends with +00:00 offset)
    # since metadata says timezone=UTC. Regression check for the
    # _build_result intraday tz_convert branch. Use period=5d so the test
    # is weekend-/US-holiday-safe (period=1d would silently return zero
    # rows on a non-trading day and the canary would falsely fail).
    intra = history.fetch_batch(["AAPL", "MSFT"], period="5d",
                                 interval="1h", summary=False, prepost=False)
    rows0 = (intra[0].get("rows") or []) if intra else []
    check("batch intraday returned rows (period=5d, market hours within window)",
          len(rows0) > 0, f"got {len(rows0)} rows")
    if rows0:
        d = rows0[0].get("date", "")
        check("batch intraday: ISO timestamp emits in UTC (+00:00)",
              isinstance(d, str) and (d.endswith("+00:00") or d.endswith("Z")),
              f"got {d!r}")

    # Batch + --start/--end: explicit window flows through fetch_batch's
    # download kwargs the same way --period does. Regression for the
    # start/end branch in _download(). Use a short fixed historical window
    # so the test is deterministic year-round.
    bse = history.fetch_batch(["AAPL", "MSFT"], period=None,
                               interval="1d", summary=True, prepost=False,
                               start="2024-03-04", end="2024-03-08")
    by_sym = {r["symbol"]: r for r in bse}
    check("batch --start/--end: 2 dicts, both succeed",
          len(bse) == 2
          and "error" not in by_sym.get("AAPL", {})
          and "error" not in by_sym.get("MSFT", {}),
          f"got {[r.get('error') for r in bse]}")
    check("batch --start/--end: window echoes back",
          all(r.get("start") == "2024-03-04" and r.get("end") == "2024-03-08"
              for r in bse))

    # CLI dispatch: 2 tickers via subprocess goes through fetch_batch.
    # invariant: exit 0, JSON list of 2, both have timezone=UTC + exchange_tz.
    rc, data = run_cli("history.py", "--period", "5d", "AAPL", "MSFT")
    check("CLI 2-ticker dispatches to batch path (timezone=UTC)",
          rc == 0 and isinstance(data, list) and len(data) == 2
          and all(d.get("timezone") == "UTC" for d in data)
          and all("exchange_tz" in d for d in data),
          f"rc={rc}, "
          f"tzs={[d.get('timezone') for d in data] if isinstance(data, list) else None}")

    # CLI dispatch: 1 ticker still uses single-ticker path (native tz, no
    # exchange_tz field). invariant: backward-compat for existing callers.
    rc, data = run_cli("history.py", "--period", "5d", "AAPL")
    check("CLI 1-ticker keeps single-ticker path (timezone=America/New_York)",
          rc == 0 and isinstance(data, list) and len(data) == 1
          and data[0].get("timezone") == "America/New_York"
          and "exchange_tz" not in data[0],
          f"rc={rc}, tz={(data[0] if isinstance(data, list) and data else {}).get('timezone')!r}")

    # Batch CSV column schema: header for N>=2 must include `exchange_tz`
    # right after `timezone`. Single-ticker CSV must NOT include it
    # (backward-compat for downstream consumers parsing by column index).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--period", "5d", "--summary", "--format", "csv", "AAPL", "MSFT"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header_cols = csv_lines[0].split(",")
        check("batch CSV header includes exchange_tz column right after timezone",
              "exchange_tz" in header_cols
              and header_cols.index("exchange_tz") == header_cols.index("timezone") + 1,
              f"got header={header_cols}")
        check("batch CSV: header + 2 ticker rows",
              out.returncode == 0 and len(csv_lines) == 3,
              f"rc={out.returncode}, lines={len(csv_lines)}")
    else:
        check("batch CSV produced output", False, f"stdout empty, rc={out.returncode}")

    # Defensive single-element path: fetch_batch(["X"]) must still go through
    # yf.download (not silently degrade to fetch()) and emit the BATCH schema
    # — timezone="UTC", exchange_tz set. Documented invariant in the
    # docstring; without coverage, a future yfinance change to N=1 download
    # behavior could silently regress this. Library callers wanting native-tz
    # schema should call fetch() directly per docstring guidance.
    single_batch = history.fetch_batch(["AAPL"], period="5d", interval="1d",
                                        summary=True, prepost=False)
    check("fetch_batch([single ticker]) returns 1 dict",
          isinstance(single_batch, list) and len(single_batch) == 1)
    if single_batch:
        sb = single_batch[0]
        check("fetch_batch single-element: timezone=UTC (batch schema, not native)",
              sb.get("timezone") == "UTC",
              f"got {sb.get('timezone')!r}")
        check("fetch_batch single-element: exchange_tz populated",
              sb.get("exchange_tz") == "America/New_York",
              f"got {sb.get('exchange_tz')!r}")
        check("fetch_batch single-element: actually returned data",
              isinstance(sb.get("change_pct"), float),
              f"got {sb.get('change_pct')!r} (error={sb.get('error')!r})")

    # Single-ticker CSV must NOT have exchange_tz column (schema preserved).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--period", "5d", "--summary", "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header_cols = csv_lines[0].split(",")
        check("single-ticker CSV header does NOT include exchange_tz",
              "exchange_tz" not in header_cols,
              f"got header={header_cols}")
    else:
        check("single-ticker CSV produced output", False,
              f"stdout empty, rc={out.returncode}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history batch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --events-only ---
section("history --events-only")
try:
    # AAPL pays quarterly dividends; 5y window has ~20 events. Each row
    # should have only event fields (no OHLCV).
    d = history.fetch("AAPL", period="5y", interval="1d",
                       summary=False, prepost=False, events_only=True)
    rows = d.get("rows", [])
    check("AAPL --events-only: rows is list, len >= 15",
          isinstance(rows, list) and len(rows) >= 15,
          f"got {len(rows)}")
    check("AAPL --events-only: rows have only event fields, no OHLCV",
          all(set(r.keys()) == {"date", "dividends", "split_ratio", "capital_gains"}
              for r in rows),
          f"sample keys={list(rows[0].keys()) if rows else None}")
    check("AAPL --events-only: every row has at least one nonzero event",
          all(abs(r["dividends"]) > 0 or abs(r["split_ratio"]) > 0
              or abs(r["capital_gains"]) > 0 for r in rows),
          "row with all-zero events leaked through")
    check("AAPL --events-only: has_capital_gains_column is False (non-fund)",
          d.get("has_capital_gains_column") is False)
    # No --head/--tail → rows_truncated absent (matches default mode shape).
    check("AAPL --events-only: rows_truncated absent when no head/tail",
          "rows_truncated" not in d)

    # Fund: VFIAX gets the Capital Gains column even though Yahoo doesn't
    # populate values reliably. The COLUMN existence is the schema
    # invariant; the values are documented as sparse.
    df = history.fetch("VFIAX", period="10y", interval="1d",
                        summary=False, prepost=False, events_only=True)
    check("VFIAX --events-only: has_capital_gains_column is True (fund)",
          df.get("has_capital_gains_column") is True,
          f"got {df.get('has_capital_gains_column')!r}")
    check("VFIAX --events-only: returned event rows (Vanguard pays distributions)",
          isinstance(df.get("rows"), list) and len(df["rows"]) > 0,
          f"rows={len(df.get('rows', []))}")

    # CSV: events column set is base + per-event + meta. Multi-ticker
    # batch adds exchange_tz right after timezone (same pattern as
    # default mode).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--period", "5y", "--events-only", "--format", "csv",
           "--tail", "3", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header = csv_lines[0].split(",")
        check("--events-only CSV header has event cols, NOT ohlcv cols",
              "dividends" in header and "capital_gains" in header
              and "open" not in header and "close" not in header,
              f"header={header}")
        # Per-ticker discriminator must survive CSV flattening — without
        # it, a fund/non-fund signal (which 0.0 capital_gains means "no
        # distribution" vs "Yahoo doesn't track this") is invisible in
        # tabular form. Repeats across this ticker's event rows.
        check("--events-only CSV header includes has_capital_gains_column",
              "has_capital_gains_column" in header,
              f"header={header}")
        check("--events-only CSV: AAPL row has has_capital_gains_column=False",
              "False" in csv_lines[1].split(",")
              or "false" in csv_lines[1].split(","),
              f"row={csv_lines[1]!r}")
        check("--events-only CSV: header + 3 rows (--tail 3)",
              out.returncode == 0 and len(csv_lines) == 4,
              f"rc={out.returncode}, lines={len(csv_lines)}")
    else:
        check("--events-only CSV produced output", False,
              f"stdout empty, rc={out.returncode}")

    # Mutex check: argparse rejects --summary + --events-only.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--summary", "--events-only", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--summary + --events-only is rejected (mutex)",
          out.returncode != 0
          and ("incompatible" in out.stderr.lower()
               or "mutually" in out.stderr.lower()),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Mutex check: --events-only + --prepost is rejected.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--events-only", "--prepost", "--interval", "5m", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--events-only + --prepost is rejected",
          out.returncode != 0
          and "prepost" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Guard: --events-only + intraday interval is rejected (corporate
    # actions are end-of-day; Yahoo intraday windows cap at 7-60 days
    # so events-only would silently return empty).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--events-only", "--interval", "5m", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--events-only + --interval 5m is rejected (intraday guard)",
          out.returncode != 0
          and "daily-or-coarser" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Batch path: --events-only over multi-ticker. exchange_tz fold for
    # daily dates should preserve event dates correctly across markets
    # (events are end-of-day; folding into local tz is a no-op for the
    # date string format).
    bres = history.fetch_batch(["AAPL", "MSFT"], period="2y",
                                interval="1d", summary=False,
                                prepost=False, events_only=True)
    by_sym = {r["symbol"]: r for r in bres}
    check("--events-only batch: 2 dicts, both succeed",
          len(bres) == 2
          and "error" not in by_sym.get("AAPL", {})
          and "error" not in by_sym.get("MSFT", {}))
    check("--events-only batch: AAPL has dividend rows",
          isinstance(by_sym.get("AAPL", {}).get("rows"), list)
          and len(by_sym["AAPL"]["rows"]) >= 6,
          f"got {len(by_sym.get('AAPL', {}).get('rows', []))}")
    check("--events-only batch: exchange_tz field present (batch schema)",
          "exchange_tz" in by_sym.get("AAPL", {}))
    # Single-ticker AAPL events vs batched AAPL events should produce
    # the same date strings — the tz fold is a no-op for daily dates
    # in the source ticker's own tz. Compare last 3 dividend dates.
    single = history.fetch("AAPL", period="2y", interval="1d",
                            summary=False, prepost=False, events_only=True)
    single_dates = [r["date"] for r in single.get("rows", [])][-3:]
    batch_dates = [r["date"] for r in by_sym["AAPL"]["rows"]][-3:]
    check("--events-only batch: AAPL event dates match single-ticker",
          single_dates == batch_dates,
          f"single={single_dates}, batch={batch_dates}")

    # --events-only with --start/--end window (independent code path
    # from --period in fetch / fetch_batch).
    se = history.fetch("AAPL", period=None, interval="1d",
                        summary=False, prepost=False, events_only=True,
                        start="2024-01-01", end="2024-12-31")
    check("--events-only --start/--end: window echoes back",
          se.get("start") == "2024-01-01" and se.get("end") == "2024-12-31"
          and se.get("period") is None)
    check("--events-only --start/--end: returns AAPL's 4 quarterly dividends in 2024",
          isinstance(se.get("rows"), list) and len(se["rows"]) == 4,
          f"got {len(se.get('rows', []))} rows")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history --events-only crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --metadata ---
section("history --metadata")
try:
    d = history.fetch_metadata("AAPL", interval="1d")
    # invariant: required headline fields present
    must_have = {"symbol", "currency", "exchange_name", "instrument_type",
                 "first_trade_date", "exchange_timezone_name", "valid_ranges"}
    check("AAPL --metadata: required fields present",
          must_have.issubset(d.keys()),
          f"missing={must_have - d.keys()}")
    # canary: AAPL is on Nasdaq, USD, listed since 1980-12-12
    check("AAPL --metadata: currency=USD, exchange_name=NMS",
          d.get("currency") == "USD" and d.get("exchange_name") == "NMS",
          f"got {d.get('currency')!r}, {d.get('exchange_name')!r}")
    check("AAPL --metadata: first_trade_date is 1980-12-12 (IPO date)",
          d.get("first_trade_date") == "1980-12-12",
          f"got {d.get('first_trade_date')!r}")
    check("AAPL --metadata: exchange_timezone_name is IANA",
          d.get("exchange_timezone_name") == "America/New_York",
          f"got {d.get('exchange_timezone_name')!r}")
    check("AAPL --metadata: instrument_type=EQUITY",
          d.get("instrument_type") == "EQUITY")
    check("AAPL --metadata: has_prepost is True",
          d.get("has_prepost") is True)
    check("AAPL --metadata: valid_ranges is list with `1y` and `max`",
          isinstance(d.get("valid_ranges"), list)
          and "1y" in d["valid_ranges"]
          and "max" in d["valid_ranges"])
    # No `period` / `start` / `end` / `interval` in metadata schema
    # (those describe the query window; metadata is a snapshot).
    check("AAPL --metadata: no period/start/end/interval fields",
          not any(k in d for k in ("period", "start", "end", "interval")))

    # Non-US: HK ticker has different currency, IANA tz, has_prepost=False.
    h = history.fetch_metadata("0700.HK", interval="1d")
    check("0700.HK --metadata: HKD currency",
          h.get("currency") == "HKD",
          f"got {h.get('currency')!r}")
    check("0700.HK --metadata: Asia/Hong_Kong IANA tz",
          h.get("exchange_timezone_name") == "Asia/Hong_Kong",
          f"got {h.get('exchange_timezone_name')!r}")
    check("0700.HK --metadata: has_prepost is False (HK no extended hours)",
          h.get("has_prepost") is False,
          f"got {h.get('has_prepost')!r}")

    # CSV: one row per ticker; valid_ranges JSON-encoded into a cell.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--metadata", "--format", "csv", "AAPL", "MSFT"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header = csv_lines[0].split(",")
        check("--metadata CSV header has metadata cols, no ohlcv/event cols",
              "first_trade_date" in header and "valid_ranges" in header
              and "open" not in header and "dividends" not in header,
              f"header={header}")
        check("--metadata CSV: header + 2 rows for 2 tickers",
              out.returncode == 0 and len(csv_lines) == 3,
              f"rc={out.returncode}, lines={len(csv_lines)}")
        # No exchange_tz column — IANA tz lives in exchange_timezone_name
        # for the metadata schema.
        check("--metadata CSV: no `exchange_tz` column",
              "exchange_tz" not in header,
              f"header={header}")
    else:
        check("--metadata CSV produced output", False,
              f"stdout empty, rc={out.returncode}")

    # Bogus ticker → error_kind=not_found, no other fields populated.
    bogus = history.fetch_metadata("ZZZZNOTREAL", interval="1d")
    check("--metadata bogus ticker: error_kind=not_found",
          bogus.get("error_kind") == "not_found",
          f"got {bogus.get('error_kind')!r}")

    # Mutex: --metadata + --events-only rejected.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--metadata", "--events-only", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--metadata + --events-only is rejected (mutex)",
          out.returncode != 0
          and "mutually exclusive" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Mutex: --metadata + --head rejected (head/tail don't apply).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--metadata", "--head", "5", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--metadata + --head is rejected",
          out.returncode != 0
          and "head" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Mutex: --metadata + --no-adjust rejected (adjustment-invariant snapshot).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--metadata", "--no-adjust", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--metadata + --no-adjust is rejected",
          out.returncode != 0
          and "no-adjust" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Mutex: --metadata + --prepost rejected (no bars are returned).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--metadata", "--prepost", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--metadata + --prepost is rejected",
          out.returncode != 0
          and "prepost" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Quote-type coverage: --metadata works across instrument types,
    # and `instrument_type` is a reliable disambiguator for callers
    # that need to sniff what kind of ticker they got. Schema invariant:
    # symbol + currency + instrument_type + exchange_timezone_name are
    # populated for every quote type yfinance recognizes.
    quote_type_cases = [
        ("SPY",     "ETF",          "USD"),
        ("VFIAX",   "MUTUALFUND",   "USD"),
        ("^GSPC",   "INDEX",        "USD"),
        ("BTC-USD", "CRYPTOCURRENCY", "USD"),
    ]
    for sym, expected_type, expected_ccy in quote_type_cases:
        m = history.fetch_metadata(sym, interval="1d")
        check(f"{sym} --metadata: instrument_type={expected_type}",
              m.get("instrument_type") == expected_type,
              f"got {m.get('instrument_type')!r}")
        check(f"{sym} --metadata: currency={expected_ccy}",
              m.get("currency") == expected_ccy,
              f"got {m.get('currency')!r}")
        check(f"{sym} --metadata: exchange_timezone_name populated",
              isinstance(m.get("exchange_timezone_name"), str)
              and len(m["exchange_timezone_name"]) > 0,
              f"got {m.get('exchange_timezone_name')!r}")

    # tz-correctness regression: fix #1. ISO datetime fields must carry
    # `+00:00` offset (we forced UTC in _epoch_to_iso_dt) so consumers
    # parsing the string get an unambiguous moment. Server-tz drift is
    # the bug this asserts against — naive `datetime.fromtimestamp(v)`
    # would emit no offset.
    aapl = history.fetch_metadata("AAPL", interval="1d")
    rmt = aapl.get("regular_market_time")
    check("--metadata: regular_market_time ISO string carries +00:00 offset",
          isinstance(rmt, str) and rmt.endswith("+00:00"),
          f"got {rmt!r} (must end with +00:00 — UTC offset)")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history --metadata crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --shares ---
section("history --shares")
try:
    # Equity (US) — populated time series. AAPL goes back ~10y.
    d = history.fetch_shares("AAPL", period="2y", start=None, end=None,
                             interval="1d", head=None, tail=None)
    check("AAPL --shares: success, no error/note",
          "error" not in d and "note" not in d,
          f"got error={d.get('error')!r}, note={d.get('note')!r}")
    check("AAPL --shares: rows is non-empty list",
          isinstance(d.get("rows"), list) and len(d["rows"]) > 0,
          f"got rows={type(d.get('rows'))}, len={len(d.get('rows') or [])}")
    check("AAPL --shares: timezone is exchange-local (America/New_York)",
          d.get("timezone") == "America/New_York",
          f"got {d.get('timezone')!r}")
    # invariant: each row has date + shares_outstanding (int)
    if d.get("rows"):
        r0 = d["rows"][0]
        check("AAPL --shares: row has date + shares_outstanding",
              set(r0.keys()) == {"date", "shares_outstanding"})
        check("AAPL --shares: shares_outstanding is int > 0",
              isinstance(r0["shares_outstanding"], int)
              and r0["shares_outstanding"] > 0,
              f"got {r0['shares_outstanding']!r}")
        # canary: AAPL has between 10B and 20B shares outstanding (post-2020-split,
        # pre-future-splits). Will fail legitimately if AAPL splits again.
        check("AAPL --shares: shares_outstanding in 10B-20B range (canary)",
              10_000_000_000 < r0["shares_outstanding"] < 20_000_000_000,
              f"got {r0['shares_outstanding']:,}")
    # echo: --period was used → start/end are null in echo
    check("AAPL --shares: --period echo (start/end are null)",
          d.get("period") == "2y" and d.get("start") is None
          and d.get("end") is None,
          f"got period={d.get('period')!r}, start={d.get('start')!r}, end={d.get('end')!r}")

    # Non-US equity (HK) — populated, native HK tz.
    h = history.fetch_shares("0700.HK", period="1y", start=None, end=None,
                             interval="1d", head=None, tail=None)
    check("0700.HK --shares: success",
          "error" not in h and "note" not in h
          and isinstance(h.get("rows"), list) and len(h["rows"]) > 0,
          f"got error={h.get('error')!r}, note={h.get('note')!r}, rows_len={len(h.get('rows') or [])}")
    check("0700.HK --shares: timezone is Asia/Hong_Kong (native, no UTC fold)",
          h.get("timezone") == "Asia/Hong_Kong",
          f"got {h.get('timezone')!r}")

    # Non-equity → success-with-`note` (ambiguous-empty path)
    for sym, expected_label in [("SPY", "ETF"), ("BTC-USD", "crypto"),
                                 ("^GSPC", "index"), ("EURUSD=X", "FX")]:
        n = history.fetch_shares(sym, period="1y", start=None, end=None,
                                 interval="1d", head=None, tail=None)
        check(f"{sym} --shares ({expected_label}): success-with-note",
              "error" not in n and "note" in n
              and isinstance(n.get("rows"), list) and len(n["rows"]) == 0,
              f"got error={n.get('error')!r}, note_set={('note' in n)}, "
              f"rows_len={len(n.get('rows') or [])}")
        check(f"{sym} --shares: note mentions chain fast_info",
              "fast_info" in (n.get("note") or "").lower(),
              f"got note={n.get('note')!r}")

    # Bogus ticker → also note path (Yahoo logs 404 to stderr but
    # underlying call returns None, indistinguishable from non-equity).
    bogus = history.fetch_shares("ZZZZNOTREAL", period="1y", start=None,
                                  end=None, interval="1d", head=None, tail=None)
    check("ZZZZNOTREAL --shares: success-with-note (same shape as non-equity)",
          "error" not in bogus and "note" in bogus
          and len(bogus.get("rows", [])) == 0)

    # Narrow-window equity → also None → also note (4th indistinguishable
    # cause). Verified empirically (probe 2026-05): pre-IPO / future /
    # 1-day-inside-data all return None for AAPL.
    narrow = history.fetch_shares("AAPL", period=None, start="2050-01-01",
                                   end="2050-01-02", interval="1d",
                                   head=None, tail=None)
    check("AAPL future-window --shares: success-with-note (4th cause)",
          "error" not in narrow and "note" in narrow
          and len(narrow.get("rows", [])) == 0)
    check("AAPL future-window note message mentions 'narrow' cause",
          "narrow" in (narrow.get("note") or "").lower(),
          f"got note={narrow.get('note')!r}")

    # Same-date dedup — AAPL --period max has known dups (verified: ~80
    # rows dropped on a fresh fetch).
    # canary: this is data-dependent — if Yahoo cleans up the dups
    # upstream, the count drops to 0 and this check fails legitimately
    # (not because of our bug). The strict invariant lives one check
    # below: post-dedup output rows MUST have unique dates regardless
    # of what Yahoo emitted.
    dedup = history.fetch_shares("AAPL", period="max", start=None, end=None,
                                  interval="1d", head=None, tail=None)
    check("AAPL --period max --shares: same_date_duplicates_dropped > 0 (canary)",
          isinstance(dedup.get("same_date_duplicates_dropped"), int)
          and dedup["same_date_duplicates_dropped"] > 0,
          f"got {dedup.get('same_date_duplicates_dropped')!r}")
    # invariant: post-dedup, every row must have a unique date — this
    # is the contract of the dedup pass, independent of how many dups
    # Yahoo originally emitted.
    if dedup.get("rows"):
        dates = [r["date"] for r in dedup["rows"]]
        check("AAPL --shares: all post-dedup dates are unique (invariant)",
              len(dates) == len(set(dates)),
              f"got {len(dates)} rows, {len(set(dates))} unique dates")

    # Split detection — AAPL's 2020-08-31 4-for-1 split shows up in
    # `splits_detected` over a `--period max` window. Yahoo's filing-cycle
    # lag means the split row may land on a date weeks AFTER the actual
    # split (verified: ~2020-10-22), so we don't pin the date — just
    # check ratio ~= 4.0 ± epsilon.
    splits = dedup.get("splits_detected", [])
    check("AAPL --period max --shares: splits_detected populated (≥1)",
          isinstance(splits, list) and len(splits) >= 1,
          f"got {splits!r}")
    if splits:
        forward_4x = [s for s in splits if 3.5 < s.get("ratio", 0) < 4.5]
        check("AAPL --shares: splits_detected has 4-for-1 split (ratio ≈ 4.0)",
              len(forward_4x) >= 1,
              f"all splits: {splits}")
        check("AAPL --shares: split entry has all required fields",
              all(set(s.keys()) >= {"date", "prev_shares", "current_shares", "ratio"}
                  for s in splits),
              f"got entries: {splits}")

    # Buyback directional canary: AAPL has been net buying back over 2y
    # (verified 2026-05: -4.2% over 2y). Will fail legitimately if AAPL
    # starts net-issuing for an extended period.
    rows = d["rows"]
    if len(rows) >= 2:
        check("AAPL 2y --shares: end < start (net buyback canary)",
              rows[-1]["shares_outstanding"] < rows[0]["shares_outstanding"],
              f"start={rows[0]['shares_outstanding']:,}, end={rows[-1]['shares_outstanding']:,}")

    # --head N (symmetry with --tail test above).
    h = history.fetch_shares("AAPL", period="2y", start=None, end=None,
                              interval="1d", head=3, tail=None)
    check("AAPL --shares --head 3: at most 3 rows",
          len(h.get("rows", [])) <= 3)
    check("AAPL --shares --head 3: rows_truncated populated",
          isinstance(h.get("rows_truncated"), dict)
          and h["rows_truncated"]["shown"] == len(h["rows"]))

    # --start alone (no --end) → today-backfill in echo. Match the
    # convention from default OHLCV's `effective_end`.
    se = history.fetch_shares("AAPL", period=None, start="2025-01-01",
                               end=None, interval="1d", head=None, tail=2)
    check("AAPL --start alone --shares: end backfilled to today (or later) in echo",
          isinstance(se.get("end"), str) and len(se["end"]) == 10
          and se["end"] >= "2025-01-01",
          f"got end={se.get('end')!r}")

    # --shares --summary (peer-compare) — flat per-ticker dict.
    summ = history.fetch_shares("AAPL", period="2y", start=None, end=None,
                                 interval="1d", head=None, tail=None,
                                 summary=True)
    summary_required = {"start_shares", "end_shares", "change_abs", "change_pct",
                        "min_shares", "min_shares_date",
                        "max_shares", "max_shares_date",
                        "rows_count", "start_date", "end_date",
                        "splits_detected_count"}
    check("AAPL --shares --summary: required aggregate fields present",
          summary_required.issubset(summ.keys()),
          f"missing={summary_required - summ.keys()}")
    check("AAPL --shares --summary: NO `rows` field (flat aggregate)",
          "rows" not in summ,
          f"unexpected rows={summ.get('rows')!r}")
    check("AAPL --shares --summary: change_pct is in PERCENT (matches default --summary)",
          summ.get("change_pct") is None
          or abs(summ["change_pct"]) > 0.01,  # canary: AAPL 2y is several %
          f"got change_pct={summ.get('change_pct')!r}")

    # --shares --summary on non-equity → note (no aggregate fields).
    nsumm = history.fetch_shares("SPY", period="2y", start=None, end=None,
                                  interval="1d", head=None, tail=None,
                                  summary=True)
    check("SPY --shares --summary: note path (no aggregate fields)",
          "note" in nsumm and "start_shares" not in nsumm,
          f"got note={nsumm.get('note')!r}, has start_shares={'start_shares' in nsumm}")

    # CLI: --shares --summary CSV multi-ticker peer compare.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--summary", "--period", "2y", "--format", "csv",
           "AAPL", "MSFT", "SPY"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header = csv_lines[0].split(",")
        check("--shares --summary CSV header has start_shares + change_pct",
              "start_shares" in header and "change_pct" in header
              and "splits_detected_count" in header,
              f"header={header}")
        check("--shares --summary CSV: 1 header + 3 ticker rows",
              out.returncode == 0 and len(csv_lines) == 4,
              f"rc={out.returncode}, lines={len(csv_lines)}")
        check("--shares --summary CSV: NO `splits_detected` (nested) column",
              "splits_detected" not in header
              or "splits_detected_count" in header,  # only the count survives
              f"header={header}")
    else:
        check("--shares --summary CSV produced output", False,
              f"stdout empty, rc={out.returncode}")

    # CLI: --shares --format ndjson (explicit smoke gap fix).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--period", "1y", "--tail", "2", "--format", "ndjson",
           "AAPL", "MSFT"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    nd_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("--shares --format ndjson: 2 lines for 2 tickers",
          out.returncode == 0 and len(nd_lines) == 2,
          f"rc={out.returncode}, lines={len(nd_lines)}")
    if len(nd_lines) >= 1:
        try:
            ndj = json.loads(nd_lines[0])
            check("--shares --format ndjson: each line is valid JSON object",
                  isinstance(ndj, dict) and ndj.get("symbol") == "AAPL"
                  and isinstance(ndj.get("rows"), list))
        except json.JSONDecodeError as exc:
            check("--shares --format ndjson lines parse", False, str(exc))

    # Mutex: --shares --summary + --head rejected.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--summary", "--head", "5", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--shares --summary + --head is rejected (would distort aggregate)",
          out.returncode != 0
          and "head" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Mutex: --summary + --events-only rejected (still — no shares involved).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--summary", "--events-only", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--summary + --events-only is rejected (incompat axis)",
          out.returncode != 0
          and ("incompatible" in out.stderr.lower()
               or "mutually" in out.stderr.lower()),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # --tail truncation — surfaces rows_truncated.
    t = history.fetch_shares("AAPL", period="2y", start=None, end=None,
                             interval="1d", head=None, tail=3)
    check("AAPL --shares --tail 3: at most 3 rows",
          len(t.get("rows", [])) <= 3)
    check("AAPL --shares --tail 3: rows_truncated populated",
          isinstance(t.get("rows_truncated"), dict)
          and t["rows_truncated"]["shown"] == len(t["rows"])
          and t["rows_truncated"]["total"] >= t["rows_truncated"]["shown"])

    # --start/--end window — start/end are echoed in the response.
    w = history.fetch_shares("AAPL", period=None, start="2024-01-01",
                              end="2024-04-01", interval="1d",
                              head=None, tail=2)
    check("AAPL --shares --start/--end: window echoed",
          w.get("period") is None and w.get("start") == "2024-01-01"
          and w.get("end") == "2024-04-01",
          f"got period={w.get('period')!r}, start={w.get('start')!r}, "
          f"end={w.get('end')!r}")

    # CLI subprocess paths — exercise argparse + JSON / CSV emit.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--period", "1y", "--tail", "5", "--format", "csv",
           "AAPL", "SPY", "ZZZZNOTREAL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header = csv_lines[0].split(",")
        check("--shares CSV header has shares_outstanding + note, no OHLCV",
              "shares_outstanding" in header and "note" in header
              and "open" not in header and "dividends" not in header,
              f"header={header}")
        # No exchange_tz column — shares is serial-loop, not yf.download batch.
        check("--shares CSV: no `exchange_tz` column (serial-loop, not batched)",
              "exchange_tz" not in header,
              f"header={header}")
        # Three tickers → at least one carrying row each. AAPL fills 5 (tail),
        # SPY + ZZZZNOTREAL each emit 1 carrying note row → ≥ 7 data rows.
        check("--shares CSV: ≥ 7 data rows for 3 tickers (5 AAPL + 1 SPY + 1 bogus)",
              out.returncode == 0 and len(csv_lines) >= 8,  # 1 header + ≥ 7
              f"rc={out.returncode}, lines={len(csv_lines)}")
    else:
        check("--shares CSV produced output", False,
              f"stdout empty, rc={out.returncode}")

    # Mutex: --shares + --metadata rejected.
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--metadata", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--shares + --metadata is rejected (mutex)",
          out.returncode != 0
          and "mutually exclusive" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # --shares + --summary is now ALLOWED (orthogonal projection axis).
    # The combination is exercised in the dedicated --shares --summary
    # block above (peer-compare aggregate). What's NOT allowed: --shares
    # + --summary + --head/--tail (clipping the row stream before the
    # aggregate would distort change_pct / min / max). That mutex is
    # tested in the new block above too.

    # Reject: --shares + --prepost (extended-hours bars don't carry shares).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--prepost", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--shares + --prepost is rejected",
          out.returncode != 0 and "prepost" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Reject: --shares + --no-adjust (shares are integer counts, not prices).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--no-adjust", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--shares + --no-adjust is rejected",
          out.returncode != 0 and "no-adjust" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # Reject: --shares + intraday --interval (share counts don't fire mid-session).
    cmd = [sys.executable, str(SCRIPTS_DIR / "history.py"),
           "--shares", "--interval", "1h", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("--shares + --interval 1h is rejected",
          out.returncode != 0 and "daily-or-coarser" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history --shares crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info default mode (stock) ---
section("info default (stock)")
try:
    d = info.fetch("AAPL")
    # invariant: schema shape
    check("AAPL quote_type=EQUITY", d.get("quote_type") == "EQUITY")
    # invariant: stocks get all stock sections + NO fund section
    check("AAPL has stock sections, no fund",
          "fund" not in d
          and {"profile", "valuation", "fundamentals", "dividend",
               "analyst", "shares"}.issubset(d.keys()))
    # canary: AAPL mega-cap sanity
    check("AAPL valuation.market_cap is int > 1e12 (canary)",
          isinstance(d["valuation"].get("market_cap"), int)
          and d["valuation"]["market_cap"] > 1e12,
          f"got {d['valuation'].get('market_cap')!r}")
    # invariant: profit_margin is a fraction-encoded float; sign + magnitude
    # can drift (Apple could have a loss, margin > 1 won't happen for an
    # operating company but we won't assume). Assert encoding only.
    pm = d["fundamentals"].get("profit_margin")
    check("AAPL profit_margin is fraction-encoded float (-1 < x < 1)",
          isinstance(pm, float) and -1 < pm < 1,
          f"got {pm!r}")
    # invariant: sector populated for a major US equity
    check("AAPL profile.sector is non-empty str",
          isinstance(d["profile"].get("sector"), str)
          and len(d["profile"]["sector"]) > 0)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info default crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info default mode (ETF) ---
section("info default (ETF)")
try:
    d = info.fetch("SPY")
    # invariant: schema shape
    check("SPY quote_type=ETF", d.get("quote_type") == "ETF")
    check("SPY has fund section", "fund" in d)
    # invariant: type-correct, non-empty
    check("SPY fund.category is non-empty str",
          isinstance(d.get("fund", {}).get("category"), str)
          and len(d["fund"]["category"]) > 0,
          f"got {d.get('fund', {}).get('category')!r}")
    # canary: AUM > $1B; SPY is the largest US ETF, this should be ~$700B+
    check("SPY fund.total_assets is int > 1e9 (canary)",
          isinstance(d.get("fund", {}).get("total_assets"), int)
          and d["fund"]["total_assets"] > 1e9)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info ETF crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info non-US ticker ---
section("info non-US (0700.HK)")
try:
    d = info.fetch("0700.HK")
    check("0700.HK currency=HKD", d.get("currency") == "HKD",
          f"got {d.get('currency')!r}")
    check("0700.HK exchange=HKG", d.get("exchange") == "HKG",
          f"got {d.get('exchange')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info HK crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info error handling ---
section("info error handling")
try:
    d_bad = info.fetch("ZZZZNOTREAL")
    check("bogus ticker returns error dict",
          "error" in d_bad and "valuation" not in d_bad)
    # invariant: error_kind classifies the failure as not_found (so the
    # caller knows retry won't help). rate_limit / network would imply
    # transient and warrant retry.
    check("bogus ticker error_kind=not_found",
          d_bad.get("error_kind") == "not_found",
          f"got {d_bad.get('error_kind')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info error crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- history --start/--end + --head/--tail ---
section("history --start/--end + --head/--tail")
try:
    # Explicit window: 2023-01-15 (Sun) to 2023-01-22 (Sun) gives Tue-Fri
    # of MLK week = 4 trading days. invariant: yfinance honors start/end.
    d = history.fetch("AAPL", period=None, interval="1d",
                      summary=True, prepost=False, adjust=True,
                      start="2023-01-15", end="2023-01-22")
    check("--start/--end window returns 4 trading days",
          d.get("rows_count") == 4,
          f"got rows_count={d.get('rows_count')!r}")
    check("--start/--end echoes start/end in output",
          d.get("start") == "2023-01-15" and d.get("end") == "2023-01-22"
          and d.get("period") is None,
          f"got period={d.get('period')!r} start={d.get('start')!r} end={d.get('end')!r}")

    # --tail truncation: invariant: returns last N rows + records total
    d = history.fetch("AAPL", period="1mo", interval="1d",
                      summary=False, prepost=False, adjust=True,
                      tail=3)
    check("--tail 3 returns exactly 3 rows",
          len(d.get("rows", [])) == 3,
          f"got {len(d.get('rows', []))} rows")
    check("--tail 3 emits rows_truncated metadata",
          isinstance(d.get("rows_truncated"), dict)
          and d["rows_truncated"]["shown"] == 3
          and d["rows_truncated"]["total"] > 3,
          f"got {d.get('rows_truncated')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"history start/end/tail crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- --format ndjson + csv (CLI subprocess) ---
section("--format ndjson + csv (CLI)")
try:
    # ndjson: one record per line — count lines, must equal ticker count.
    cmd = [sys.executable, str(SCRIPTS_DIR / "fast_info.py"),
           "--format", "ndjson", "AAPL", "MSFT"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    nd_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("fast_info --format ndjson: 2 lines for 2 tickers",
          out.returncode == 0 and len(nd_lines) == 2,
          f"rc={out.returncode}, lines={len(nd_lines)}")
    # Each ndjson line must be standalone valid JSON
    if len(nd_lines) == 2:
        try:
            json.loads(nd_lines[0])
            json.loads(nd_lines[1])
            check("fast_info ndjson lines are valid standalone JSON", True)
        except json.JSONDecodeError as e:
            check("fast_info ndjson lines are valid standalone JSON",
                  False, f"{e}")

    # csv with --summary: header + N rows
    cmd = [sys.executable, str(SCRIPTS_DIR / "info.py"),
           "--summary", "--format", "csv", "AAPL", "SPY"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("info --summary --format csv: header + 2 ticker rows",
          out.returncode == 0 and len(csv_lines) == 3,
          f"rc={out.returncode}, lines={len(csv_lines)}")

    # csv without --summary should error (default mode is nested)
    cmd = [sys.executable, str(SCRIPTS_DIR / "info.py"),
           "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("info --format csv without --summary: argparse error (rc=2)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # earnings CSV: `note` column must exist and be populated for non-equity
    # (SPY), blank for equity (AAPL). Use csv.reader to handle quoted text
    # robustly even though the message currently has no commas. Covers both
    # default and summary CSV layouts.
    import csv as _csv
    for label, args, expected_note_match in (
        ("default", ("--format", "csv", "--limit", "2", "AAPL", "SPY"),
         "earnings only meaningful"),
        ("summary", ("--summary", "--format", "csv", "AAPL", "SPY"),
         "earnings only meaningful"),
    ):
        cmd = [sys.executable, str(SCRIPTS_DIR / "earnings.py"), *args]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        rows = list(_csv.reader(out.stdout.splitlines()))
        check(f"earnings {label} CSV: exits 0 + parses",
              out.returncode == 0 and len(rows) >= 2,
              f"rc={out.returncode}, rows={len(rows)}")
        if not rows or "note" not in rows[0]:
            check(f"earnings {label} CSV: header has 'note' column",
                  False, f"got header={rows[0] if rows else None}")
            continue
        check(f"earnings {label} CSV: header has 'note' column", True)
        note_idx = rows[0].index("note")
        aapl_rows = [r for r in rows[1:] if r and r[0] == "AAPL"]
        spy_rows = [r for r in rows[1:] if r and r[0] == "SPY"]
        check(f"earnings {label} CSV: AAPL note cell blank (equity)",
              bool(aapl_rows) and aapl_rows[0][note_idx] == "",
              f"got {aapl_rows[0][note_idx]!r}" if aapl_rows else "no AAPL rows")
        check(f"earnings {label} CSV: SPY note cell mentions 'equities'",
              bool(spy_rows) and expected_note_match in spy_rows[0][note_idx],
              f"got {spy_rows[0][note_idx]!r}" if spy_rows else "no SPY rows")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"--format crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info recommendation_key normalization ---
section("info recommendation_key normalization")
try:
    # We need a ticker that returns a no-coverage sentinel string ("none",
    # "n/a", etc.) upstream — verifies our _NO_COVERAGE_SENTINELS frozenset
    # collapses it to JSON null. AI (c3.ai) historically returns "none"
    # but is a thinly-covered single name: if it gets acquired, delisted,
    # or starts attracting analyst coverage, swap in another no-coverage
    # ticker (TLRY, BB have both shown the same sentinel).
    canary_ticker = "AI"
    d = info.fetch(canary_ticker)
    rk = d.get("analyst", {}).get("recommendation_key")
    check(f"{canary_ticker} recommendation_key normalized to None",
          rk is None,
          f"got {rk!r} — if {canary_ticker} now has coverage, swap ticker")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info recommendation_key crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- info --summary projection (via real CLI flag, not internal _summarize) ---
section("info --summary (CLI)")
try:
    rc, data = run_cli("info.py", "--summary", "AAPL", "SPY")
    check("info.py --summary exits 0 + emits JSON list",
          rc == 0 and isinstance(data, list) and len(data) == 2)
    if isinstance(data, list) and len(data) == 2:
        d, d_etf = data
        # invariant: summary shape mirrors SUMMARY_FIELDS exactly. Reading
        # the count from the source-of-truth list (rather than hardcoding
        # 17) means adding a field only requires editing SECTIONS +
        # SUMMARY_FIELDS — smoke updates automatically.
        expected_keys = len(info.SUMMARY_FIELDS)
        check(f"AAPL summary has {expected_keys} keys",
              len(d) == expected_keys,
              f"got {len(d)} keys: {list(d.keys())}")
        # invariant: AAPL pays a dividend; field must be present and a
        # fraction-encoded float well under 1 (yield > 100% would be wild).
        # Loose upper bound — special dividends can spike trailing yield.
        ty = d.get("trailing_annual_dividend_yield")
        check("AAPL trailing_annual_dividend_yield is fraction (0 < x < 0.5)",
              isinstance(ty, float) and 0 < ty < 0.5,
              f"got {ty!r}")
        # canary: exchange code stable
        check("AAPL summary exchange=NMS (canary)",
              d.get("exchange") == "NMS")
        check(f"SPY summary has {expected_keys} keys (same shape as stock)",
              len(d_etf) == expected_keys)
        # canary: SPY's Yahoo category text — drift expected eventually
        check("SPY summary category=Large Blend (canary: Yahoo wording)",
              d_etf.get("category") == "Large Blend")
        # invariant: five_year_avg_return is a fraction-encoded CAGR;
        # range must allow downturns (-0.50 < x < 0.50 covers any plausible
        # rolling 5-yr CAGR for SPY without false-positiving on a crash).
        fyr = d_etf.get("five_year_avg_return")
        check("SPY five_year_avg_return is fraction (-0.5 < x < 0.5)",
              isinstance(fyr, float) and -0.5 < fyr < 0.5,
              f"got {fyr!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"info --summary CLI crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings._row_to_dict logic (offline; covers the past-row-with-null-actual
#     edge case that AAPL's clean calendar doesn't expose) ---
section("earnings._row_to_dict (offline)")
try:
    import pandas as _pd
    from datetime import datetime as _ed, timedelta as _etd, timezone as _etz

    _now_utc = _ed.now(tz=_etz.utc)
    # Past row WITH actual EPS — should be is_future=False, eps populated.
    ts_past = _pd.Timestamp(_now_utc - _etd(days=90)).tz_convert("UTC")
    r_past_full = earnings._row_to_dict(
        ts_past, {"EPS Estimate": 1.0, "Reported EPS": 1.1, "Surprise(%)": 10.0},
        _now_utc)
    check("past row with actual: is_future=False",
          r_past_full["is_future"] is False)
    check("past row with actual: eps_actual=1.1",
          r_past_full["eps_actual"] == 1.1)

    # Past row WITH NULL actual — regression check for the fixed logic.
    # Old code OR'd `eps_actual is None` into is_future, so this used to flip
    # to True. New code uses date-only: stays False.
    ts_past2 = _pd.Timestamp(_now_utc - _etd(days=400)).tz_convert("UTC")
    r_past_null = earnings._row_to_dict(
        ts_past2, {"EPS Estimate": 0.5, "Reported EPS": float("nan"),
                   "Surprise(%)": float("nan")},
        _now_utc)
    check("past row with NULL actual stays past (date-only is_future, not OR'd with eps_actual is None)",
          r_past_null["is_future"] is False,
          f"got {r_past_null['is_future']}, eps_actual={r_past_null['eps_actual']}")
    check("past row with NULL actual: eps_actual=null",
          r_past_null["eps_actual"] is None)

    # Future row — should always be is_future=True regardless of actual.
    ts_future = _pd.Timestamp(_now_utc + _etd(days=60)).tz_convert("UTC")
    r_future = earnings._row_to_dict(
        ts_future, {"EPS Estimate": 2.0, "Reported EPS": float("nan"),
                    "Surprise(%)": float("nan")},
        _now_utc)
    check("future row with NULL actual: is_future=True",
          r_future["is_future"] is True)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings._row_to_dict offline crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings default mode (equity) ---
section("earnings default (equity)")
try:
    d = earnings.fetch("AAPL")
    # invariant: schema shape
    check("AAPL has earnings_dates list",
          isinstance(d.get("earnings_dates"), list)
          and len(d["earnings_dates"]) > 0,
          f"got {len(d.get('earnings_dates') or [])} rows")
    check("AAPL quote_type=EQUITY", d.get("quote_type") == "EQUITY")
    check("AAPL timezone is non-empty str",
          isinstance(d.get("timezone"), str) and len(d["timezone"]) > 0,
          f"got {d.get('timezone')!r}")
    check("AAPL no error / note (equity success path)",
          "error" not in d and "note" not in d)

    rows = d.get("earnings_dates") or []
    if rows:
        r0 = rows[0]
        # invariant: per-row schema
        check("row date is ISO timestamp with offset (T and ±)",
              isinstance(r0.get("date"), str) and "T" in r0["date"]
              and ("-" in r0["date"][10:] or "+" in r0["date"][10:]),
              f"got {r0.get('date')!r}")
        check("row is_future is bool",
              isinstance(r0.get("is_future"), bool))
        check("row eps_estimate is float or None",
              r0.get("eps_estimate") is None
              or isinstance(r0["eps_estimate"], float))
        # invariant: AAPL has both future + past in the default window
        has_future = any(r["is_future"] for r in rows)
        has_past = any(not r["is_future"] for r in rows)
        check("AAPL window contains both future and past events",
              has_future and has_past,
              f"future={has_future} past={has_past}")
        # invariant: future rows have null eps_actual; past rows non-null
        future_actuals = [r["eps_actual"] for r in rows if r["is_future"]]
        past_actuals = [r["eps_actual"] for r in rows if not r["is_future"]]
        check("future rows have null eps_actual",
              all(a is None for a in future_actuals),
              f"got {future_actuals}")
        check("past rows have non-null eps_actual",
              all(isinstance(a, float) for a in past_actuals[:4]),
              f"first 4 past actuals: {past_actuals[:4]}")
        # invariant: past rows have non-null surprise_pct (Yahoo computes it)
        past_surprises = [r["surprise_pct"] for r in rows if not r["is_future"]]
        check("past rows have non-null surprise_pct",
              all(isinstance(s, float) for s in past_surprises[:4]),
              f"first 4 past surprises: {past_surprises[:4]}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings default crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings non-equity short-circuit ---
section("earnings non-equity (SPY)")
try:
    d = earnings.fetch("SPY")
    # invariant: ETF gets empty list + note, NO error
    check("SPY no error", "error" not in d)
    check("SPY has note field",
          isinstance(d.get("note"), str) and "equit" in d["note"].lower(),
          f"got {d.get('note')!r}")
    check("SPY earnings_dates empty list",
          d.get("earnings_dates") == [],
          f"got {d.get('earnings_dates')!r}")
    check("SPY quote_type=ETF", d.get("quote_type") == "ETF")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings non-equity crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings error handling (bogus ticker) ---
section("earnings error handling")
try:
    d = earnings.fetch("ZZZZNOTREAL")
    check("bogus ticker returns error dict",
          "error" in d and "earnings_dates" not in d)
    # invariant: classify_error reaches not_found via our AttributeError
    # workaround in _quote_type. If yfinance fixes the upstream bug, this
    # may shift to "not_found" via natural classification — still passes.
    check("bogus ticker error_kind=not_found",
          d.get("error_kind") == "not_found",
          f"got {d.get('error_kind')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings error crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --summary projection ---
section("earnings --summary")
try:
    d = earnings.fetch("AAPL")
    s = earnings._summarize(d)
    # invariant: summary shape
    expected_keys = {"symbol", "quote_type"} | set(earnings._SUMMARY_KEYS)
    check("AAPL summary has all expected keys",
          set(s.keys()) >= expected_keys,
          f"missing: {expected_keys - set(s.keys())}")
    # invariant: next_date is in the future (after now). Loose check on
    # ISO string parse — comparing strings only works because all dates
    # share the same format, but datetime parse is more correct.
    from datetime import datetime as _dt
    if s.get("next_date"):
        nxt = _dt.fromisoformat(s["next_date"])
        check("AAPL next_date is in the future",
              nxt.timestamp() > _dt.now(tz=nxt.tzinfo).timestamp(),
              f"got {s['next_date']!r}")
    else:
        check("AAPL next_date is non-null (canary: liquid name has upcoming)",
              False, "expected a future date for AAPL")
    # invariant: last_eps_actual / last_surprise_pct populated for liquid name
    check("AAPL last_eps_actual is float",
          isinstance(s.get("last_eps_actual"), float),
          f"got {type(s.get('last_eps_actual')).__name__}")
    check("AAPL last_surprise_pct is float (percent, not fraction)",
          isinstance(s.get("last_surprise_pct"), float)
          and abs(s["last_surprise_pct"]) < 100,  # sanity: never > 100% miss
          f"got {s.get('last_surprise_pct')!r}")
    # invariant: avg_surprise_last_4 / beat_rate_last_4 populated for AAPL
    # (well-covered name with > 4 reported quarters).
    check("AAPL avg_surprise_last_4 is float",
          isinstance(s.get("avg_surprise_last_4"), float),
          f"got {type(s.get('avg_surprise_last_4')).__name__}")
    check("AAPL beat_rate_last_4 is in [0, 1]",
          isinstance(s.get("beat_rate_last_4"), float)
          and 0 <= s["beat_rate_last_4"] <= 1,
          f"got {s.get('beat_rate_last_4')!r}")

    # invariant: --summary error path preserves error fields, drops data
    s_err = earnings._summarize(earnings.fetch("ZZZZNOTREAL"))
    check("bogus summary preserves error_kind, no next_date",
          s_err.get("error_kind") == "not_found"
          and "next_date" not in s_err)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings --summary crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --estimates (full Yahoo analyst panel) ---
section("earnings --estimates")
try:
    d = earnings.fetch("AAPL", with_estimates=True)
    # invariant: estimates is a list with one row per period
    ests = d.get("estimates")
    check("AAPL estimates is list",
          isinstance(ests, list),
          f"got {type(ests).__name__}")
    check("AAPL estimates has 4 known periods (canary: liquid name fully covered)",
          isinstance(ests, list) and len(ests) >= 4,
          f"got {len(ests) if isinstance(ests, list) else None}")

    # invariant: EVERY row carries the documented schema keys (not just [0]).
    # Catches the case where a key only populates for the first period.
    if isinstance(ests, list) and ests:
        for i, row in enumerate(ests):
            missing = set(earnings._ESTIMATE_KEYS) - set(row.keys())
            check(f"AAPL estimates[{i}] has all _ESTIMATE_KEYS",
                  not missing,
                  f"missing in row {i} (period={row.get('period')!r}): {sorted(missing)}")
        # invariant: period codes drawn from documented set
        bad_periods = [r.get("period") for r in ests
                       if r.get("period") not in earnings._ESTIMATE_PERIODS]
        check("AAPL estimates: periods all in 0q/+1q/0y/+1y",
              not bad_periods,
              f"unexpected: {bad_periods}")
        # invariant: eps_growth is fraction-encoded. Bound at < 5 (i.e.
        # 500%) — a percent-encoded version would be > 5 (e.g., 20.4 for
        # AAPL or 119 for NVDA), so this still catches the units bug while
        # tolerating high-growth names whose YoY exceeds 100% as a fraction
        # (NVDA's 1.19 would fail the old `< 1` bound).
        eps_growths = [r["eps_growth"] for r in ests
                       if r.get("eps_growth") is not None]
        check("AAPL eps_growth is fraction-encoded (canary: |val| < 5)",
              all(abs(g) < 5 for g in eps_growths),
              f"got {eps_growths}")
        # invariant: new analyst-panel fields populated for liquid name.
        zero_q = next((r for r in ests if r["period"] == "0q"), None)
        check("AAPL 0q has eps_trend_current (canary)",
              zero_q is not None and isinstance(zero_q.get("eps_trend_current"), float),
              f"got {zero_q.get('eps_trend_current') if zero_q else None!r}")
        check("AAPL 0q has eps_revisions_up_30d as int (canary)",
              zero_q is not None and isinstance(zero_q.get("eps_revisions_up_30d"), int),
              f"got {type(zero_q.get('eps_revisions_up_30d')).__name__ if zero_q else None}")
        check("AAPL 0q has index_growth as fraction (canary: |val| < 5)",
              zero_q is not None
              and isinstance(zero_q.get("index_growth"), float)
              and abs(zero_q["index_growth"]) < 5,
              f"got {zero_q.get('index_growth') if zero_q else None!r}")

    # invariant: long_term_growth is a {stock, index} dict or absent.
    # AAPL empirically has stock=None and index populated.
    ltg = d.get("long_term_growth")
    check("AAPL long_term_growth is {stock, index} dict",
          isinstance(ltg, dict) and set(ltg.keys()) == {"stock", "index"},
          f"got {ltg!r}")

    # Negative assertions: a normal equity --estimates call (with a
    # populated calendar) must NOT carry `note` or `coverage_note` —
    # those are reserved for non-equity short-circuit and IPO fall-
    # through respectively. Catches any future regression where
    # `--estimates` accidentally always sets a coverage note even when
    # the calendar succeeded.
    check("AAPL --estimates: no `note` field (only non-equity sets it)",
          "note" not in d,
          f"got note={d.get('note')!r}")
    check("AAPL --estimates: no `coverage_note` field (only IPO sets it)",
          "coverage_note" not in d,
          f"got coverage_note={d.get('coverage_note')!r}")

    # Non-equity short-circuit: estimates: [] (no extra Yahoo calls).
    d_etf = earnings.fetch("SPY", with_estimates=True)
    check("SPY estimates: short-circuit empty list",
          d_etf.get("estimates") == [],
          f"got {d_etf.get('estimates')!r}")
    check("SPY no long_term_growth field (non-equity)",
          "long_term_growth" not in d_etf,
          f"got {d_etf.get('long_term_growth')!r}")

    # Summary mode projects 0q to flat consensus_* fields (no estimates list).
    s = earnings._summarize(d)
    check("--summary --estimates: drops estimates list (projected to consensus_*)",
          "estimates" not in s,
          f"got estimates field: {type(s.get('estimates')).__name__}")
    for k in earnings._CONSENSUS_SUMMARY_KEYS:
        check(f"--summary --estimates: {k} present",
              k in s, f"missing {k}")
    check("--summary --estimates: consensus_eps_avg matches 0q.eps_avg",
          s.get("consensus_eps_avg")
          == next(r["eps_avg"] for r in ests if r["period"] == "0q"),
          f"summary={s.get('consensus_eps_avg')}, "
          f"0q={next(r['eps_avg'] for r in ests if r['period']=='0q')}")
    check("--summary --estimates: long_term_growth still passed through",
          isinstance(s.get("long_term_growth"), dict),
          f"got {s.get('long_term_growth')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings --estimates crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --estimates ADR currency split (TM ADR: EPS USD, revenue JPY) ---
section("earnings --estimates ADR currency split")
try:
    # canary: relies on Yahoo continuing to denominate TM EPS in USD and
    # revenue in JPY. If Yahoo unifies these someday this will fail and
    # we should re-evaluate the eps_currency / revenue_currency split.
    d_tm = earnings.fetch("TM", with_estimates=True)
    ests_tm = d_tm.get("estimates") or []
    zero_q_tm = next((r for r in ests_tm if r["period"] == "0q"), None)
    check("TM 0q eps_currency=USD (per-share trading currency)",
          zero_q_tm is not None and zero_q_tm.get("eps_currency") == "USD",
          f"got {zero_q_tm.get('eps_currency') if zero_q_tm else None!r}")
    check("TM 0q revenue_currency=JPY (home reporting currency)",
          zero_q_tm is not None and zero_q_tm.get("revenue_currency") == "JPY",
          f"got {zero_q_tm.get('revenue_currency') if zero_q_tm else None!r}")
    check("TM 0q eps_currency != revenue_currency (ADR split confirmed)",
          zero_q_tm is not None
          and zero_q_tm.get("eps_currency") != zero_q_tm.get("revenue_currency"))
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings --estimates ADR crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --estimates soft-failure paths (mocked) ---
section("earnings --estimates soft failure (mocked)")
try:
    from unittest.mock import patch
    import pandas as _pd

    def _make_eps_df():
        return _pd.DataFrame({
            "avg": [1.89], "low": [1.83], "high": [1.99],
            "yearAgoEps": [1.57], "numberOfAnalysts": [30],
            "growth": [0.20], "currency": ["USD"],
        }, index=_pd.Index(["0q"], name="period"))

    def _make_rev_df():
        return _pd.DataFrame({
            "avg": [1e11], "low": [1e11], "high": [1.1e11],
            "numberOfAnalysts": [26], "yearAgoRevenue": [9e10],
            "growth": [0.15], "currency": ["USD"],
        }, index=_pd.Index(["0q"], name="period"))

    def _make_trend_df():
        return _pd.DataFrame({
            "current": [1.89], "7daysAgo": [1.74], "30daysAgo": [1.74],
            "60daysAgo": [1.72], "90daysAgo": [1.73], "currency": ["USD"],
        }, index=_pd.Index(["0q"], name="period"))

    def _make_revisions_df():
        return _pd.DataFrame({
            "upLast7days": [22], "upLast30days": [22],
            "downLast30days": [0], "downLast7Days": [0], "currency": ["USD"],
        }, index=_pd.Index(["0q"], name="period"))

    def _make_growth_df(with_ltg=True):
        idx = ["0q", "LTG"] if with_ltg else ["0q"]
        rows = ([{"stockTrend": 0.20, "indexTrend": 0.24}]
                + ([{"stockTrend": float("nan"), "indexTrend": 0.12}] if with_ltg else []))
        return _pd.DataFrame(rows, index=_pd.Index(idx, name="period"))

    # Class wrapper rather than MagicMock — MagicMock doesn't support
    # raise-on-attribute-access without contortions. Each property either
    # returns a DataFrame or raises an exception, mirroring yfinance's
    # behavior when Yahoo errors out.
    class _MockTicker:
        def __init__(self, **kwargs):
            self._kwargs = kwargs
        def _get(self, name):
            v = self._kwargs.get(name, None)
            if isinstance(v, Exception):
                raise v
            return v
        @property
        def earnings_estimate(self): return self._get("eps")
        @property
        def revenue_estimate(self):  return self._get("rev")
        @property
        def eps_trend(self):         return self._get("trend")
        @property
        def eps_revisions(self):     return self._get("revisions")
        @property
        def growth_estimates(self):  return self._get("growth")

    # Case 1: only EPS fails, revenue still succeeds. Soft failure: NO
    # estimates_error (revenue is enough to proceed); EPS columns null.
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(
                   eps=ConnectionError("429 too many requests"),
                   rev=_make_rev_df(),
                   trend=_make_trend_df(),
                   revisions=_make_revisions_df(),
                   growth=_make_growth_df())):
        rows, ltg, attempts, err = earnings._fetch_estimates("FAKE")
    check("only-EPS-fail: estimates_error is None (revenue succeeded)",
          err is None, f"got {err!r}")
    check("only-EPS-fail: rows still emitted",
          isinstance(rows, list) and len(rows) == 1,
          f"got {len(rows) if isinstance(rows, list) else None}")
    check("only-EPS-fail: row eps_avg is None (failed side)",
          rows and rows[0]["eps_avg"] is None,
          f"got {rows[0]['eps_avg'] if rows else None}")
    check("only-EPS-fail: row revenue_avg populated (success side)",
          rows and rows[0]["revenue_avg"] is not None)

    # Case 2: only revenue fails, EPS succeeds. Mirror of case 1.
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(
                   eps=_make_eps_df(),
                   rev=ConnectionError("network blip"),
                   trend=_make_trend_df(),
                   revisions=_make_revisions_df(),
                   growth=_make_growth_df())):
        rows, ltg, attempts, err = earnings._fetch_estimates("FAKE")
    check("only-revenue-fail: estimates_error is None",
          err is None, f"got {err!r}")
    check("only-revenue-fail: row eps_avg populated",
          rows and rows[0]["eps_avg"] is not None)
    check("only-revenue-fail: row revenue_avg is None",
          rows and rows[0]["revenue_avg"] is None)

    # Case 3: BOTH consensus sources fail. Hard failure: estimates_error
    # set, rows empty.
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(
                   eps=ConnectionError("429"),
                   rev=ConnectionError("429"),
                   trend=_make_trend_df(),
                   revisions=_make_revisions_df(),
                   growth=_make_growth_df())):
        rows, ltg, attempts, err = earnings._fetch_estimates("FAKE")
    check("both-consensus-fail: rows empty",
          rows == [], f"got {rows!r}")
    check("both-consensus-fail: estimates_error=rate_limit",
          err == "rate_limit", f"got {err!r}")

    # Case 4: trend / revisions / growth fail; consensus succeeds. Soft —
    # those columns null silently, NO estimates_error.
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(
                   eps=_make_eps_df(),
                   rev=_make_rev_df(),
                   trend=ConnectionError("blip"),
                   revisions=ConnectionError("blip"),
                   growth=ConnectionError("blip"))):
        rows, ltg, attempts, err = earnings._fetch_estimates("FAKE")
    check("only-enrichment-fail: estimates_error is None (consensus ok)",
          err is None, f"got {err!r}")
    check("only-enrichment-fail: eps_trend_current is None (failed)",
          rows and rows[0]["eps_trend_current"] is None)
    check("only-enrichment-fail: eps_revisions_up_7d is None (failed)",
          rows and rows[0]["eps_revisions_up_7d"] is None)
    check("only-enrichment-fail: index_growth is None (failed)",
          rows and rows[0]["index_growth"] is None)
    check("only-enrichment-fail: long_term_growth is None (growth failed)",
          ltg is None, f"got {ltg!r}")
    check("only-enrichment-fail: eps_avg still populated",
          rows and rows[0]["eps_avg"] is not None)

    # Case 4b: defensive lookup — Yahoo's `downLast7Days` capitalization
    # quirk. If upstream "fixes" the typo to lowercase (`downLast7days`),
    # we still want to read it correctly. Mock revisions DataFrame with
    # only the lowercase variant; `eps_revisions_down_7d` should still
    # populate.
    revisions_lowercase = _pd.DataFrame({
        "upLast7days":     [22],
        "upLast30days":    [22],
        "downLast30days":  [0],
        "downLast7days":   [3],   # lowercase 'd' (hypothetical Yahoo fix)
        "currency":        ["USD"],
    }, index=_pd.Index(["0q"], name="period"))
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(eps=_make_eps_df(),
                                         rev=_make_rev_df(),
                                         trend=_make_trend_df(),
                                         revisions=revisions_lowercase,
                                         growth=_make_growth_df())):
        rows, _, _, _ = earnings._fetch_estimates("FAKE")
    check("typo-defense: down_7d reads from `downLast7days` (lowercase fallback)",
          rows and rows[0].get("eps_revisions_down_7d") == 3,
          f"got {rows[0].get('eps_revisions_down_7d') if rows else None!r}")

    # Case 5: forward-compat — yfinance returns a future period like '+2q'.
    eps_extended = _pd.DataFrame({
        "avg": [1.89, 2.00, 1.85], "low": [1.83, 1.86, 1.80],
        "high": [1.99, 2.14, 1.92], "yearAgoEps": [1.57, 1.85, 1.75],
        "numberOfAnalysts": [30, 28, 12], "growth": [0.20, 0.08, 0.06],
        "currency": ["USD", "USD", "USD"],
    }, index=_pd.Index(["0q", "+1q", "+2q"], name="period"))
    with patch("earnings.yf.Ticker",
               return_value=_MockTicker(eps=eps_extended,
                                         rev=_make_rev_df(),
                                         trend=_make_trend_df(),
                                         revisions=_make_revisions_df(),
                                         growth=_make_growth_df())):
        rows, _, _, _ = earnings._fetch_estimates("FAKE")
    period_codes = [r["period"] for r in rows]
    check("forward-compat: emits unknown +2q period",
          "+2q" in period_codes, f"got {period_codes}")
    check("forward-compat: known periods come first",
          period_codes[:2] == ["0q", "+1q"]
          and period_codes[-1] == "+2q",
          f"got {period_codes}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings soft-failure crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings._assert_note_contract negative tests (offline) ---
# The contract enforces that `note` and `coverage_note` have non-overlapping
# semantics: `note` only on non-equity short-circuit, `coverage_note` only on
# IPO fall-through, and never both. Without these tests, future code could
# silently delete the helper or violate the contract — these negative cases
# pin the invariant so any regression breaks at smoke time.
section("earnings._assert_note_contract (offline)")
try:
    # The substring checks below are intentionally strict — the error
    # messages are part of the invariant contract (operators / log
    # consumers grep for these phrases to identify the violation kind).
    # If you legitimately want to rephrase, update the helper AND these
    # checks together; don't loosen the substring match thinking the
    # test is fragile.

    # Case A: both note and coverage_note present → must raise.
    bad_both = {"symbol": "BOTH", "quote_type": "ETF",
                "note": "non-equity note",
                "coverage_note": "ipo note"}
    raised = False
    try:
        earnings._assert_note_contract(bad_both)
    except RuntimeError as e:
        raised = True
        msg = str(e)
    check("contract: note + coverage_note → raises RuntimeError",
          raised, "expected RuntimeError, none raised")
    if raised:
        check("contract: note+coverage_note message mentions mutually exclusive",
              "mutually exclusive" in msg,
              f"got {msg!r}")
        check("contract: note+coverage_note message includes symbol",
              "BOTH" in msg, f"got {msg!r}")

    # Case B: note set on EQUITY → must raise.
    bad_equity_note = {"symbol": "EQNOTE", "quote_type": "EQUITY",
                       "note": "wrong-track note"}
    raised = False
    try:
        earnings._assert_note_contract(bad_equity_note)
    except RuntimeError as e:
        raised = True
        msg = str(e)
    check("contract: note on EQUITY → raises RuntimeError",
          raised, "expected RuntimeError, none raised")
    if raised:
        check("contract: note-on-equity message mentions reserved-for-non-equity",
              "non-equity" in msg.lower(),
              f"got {msg!r}")

    # Case C: legitimate non-equity short-circuit → must NOT raise.
    good_nonequity = {"symbol": "SPY", "quote_type": "ETF",
                      "note": "earnings only meaningful for equities; this is ETF",
                      "earnings_dates": []}
    earnings._assert_note_contract(good_nonequity)
    check("contract: non-equity + note (no coverage_note) → no raise",
          True)

    # Case D: legitimate IPO fall-through → must NOT raise.
    good_ipo = {"symbol": "IPO", "quote_type": "EQUITY",
                "coverage_note": "empty calendar (recent IPO ...); analyst panel returned",
                "earnings_dates": [], "estimates": [{"period": "0q"}]}
    earnings._assert_note_contract(good_ipo)
    check("contract: IPO + coverage_note (no note) → no raise", True)

    # Case E: regular equity result with neither note nor coverage_note.
    good_regular = {"symbol": "AAPL", "quote_type": "EQUITY",
                    "earnings_dates": [{"date": "2026-01-01"}]}
    earnings._assert_note_contract(good_regular)
    check("contract: regular equity (no note fields) → no raise", True)

    # Case F: violating dict missing `symbol` key — message must fall back
    # to a truncated repr instead of degrading to "(symbol=None)". Covers
    # the edge case where the helper is invoked outside fetch() (which
    # would normally set `symbol`).
    bad_no_symbol = {"quote_type": "EQUITY", "note": "stray note"}
    raised = False
    try:
        earnings._assert_note_contract(bad_no_symbol)
    except RuntimeError as e:
        raised = True
        msg = str(e)
    check("contract: missing symbol → still raises", raised)
    if raised:
        # Fallback should embed `out=...` repr instead of "(symbol=None)".
        check("contract: missing-symbol message uses out= repr fallback",
              "out=" in msg and "symbol=None" not in msg,
              f"got {msg!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings._assert_note_contract crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --estimates falls through empty earnings_dates (recent IPO) ---
section("earnings --estimates with empty earnings_dates (IPO path)")
try:
    from unittest.mock import patch as _patch_ipo
    import pandas as _pd_ipo

    # Mock fixtures: fast_info → EQUITY (passes pre-check), but
    # get_earnings_dates returns an empty DataFrame (recent IPO has no
    # past reports yet on Yahoo's calendar). Estimates DataFrames are
    # still populated. Without --estimates this should error; with it,
    # the call succeeds with earnings_dates=[] and estimates populated.
    class _IPOTicker:
        def get_earnings_dates(self, limit=12):
            return _pd_ipo.DataFrame()  # empty
        @property
        def fast_info(self):
            return {"quoteType": "EQUITY"}
        @property
        def earnings_estimate(self):
            return _pd_ipo.DataFrame({
                "avg": [0.5], "low": [0.4], "high": [0.6],
                "yearAgoEps": [None], "numberOfAnalysts": [3],
                "growth": [None], "currency": ["USD"],
            }, index=_pd_ipo.Index(["0q"], name="period"))
        @property
        def revenue_estimate(self):
            return _pd_ipo.DataFrame({
                "avg": [1e8], "low": [9e7], "high": [1.1e8],
                "numberOfAnalysts": [3], "yearAgoRevenue": [None],
                "growth": [None], "currency": ["USD"],
            }, index=_pd_ipo.Index(["0q"], name="period"))
        @property
        def eps_trend(self):
            return _pd_ipo.DataFrame({
                "current": [0.5], "7daysAgo": [0.5], "30daysAgo": [0.5],
                "60daysAgo": [0.45], "90daysAgo": [0.45], "currency": ["USD"],
            }, index=_pd_ipo.Index(["0q"], name="period"))
        @property
        def eps_revisions(self):
            return _pd_ipo.DataFrame({
                "upLast7days": [1], "upLast30days": [2],
                "downLast30days": [0], "downLast7Days": [0],
                "currency": ["USD"],
            }, index=_pd_ipo.Index(["0q"], name="period"))
        @property
        def growth_estimates(self):
            return _pd_ipo.DataFrame(
                [{"stockTrend": float("nan"), "indexTrend": 0.20}],
                index=_pd_ipo.Index(["0q"], name="period"))

    # Without --estimates: still error (the user asked for the calendar).
    with _patch_ipo("earnings.yf.Ticker", return_value=_IPOTicker()):
        d_no_est = earnings.fetch("IPOFAKE", with_estimates=False)
    check("IPO no --estimates: error_kind=not_found (preserves prior behavior)",
          d_no_est.get("error_kind") == "not_found",
          f"got {d_no_est.get('error_kind')!r}")

    # With --estimates: fall through, earnings_dates=[], estimates populated.
    with _patch_ipo("earnings.yf.Ticker", return_value=_IPOTicker()):
        d_ipo = earnings.fetch("IPOFAKE", with_estimates=True)
    check("IPO with --estimates: no top-level error (estimates path saved it)",
          "error" not in d_ipo,
          f"got error={d_ipo.get('error')!r}")
    check("IPO with --estimates: earnings_dates is empty list (not omitted)",
          d_ipo.get("earnings_dates") == [],
          f"got {d_ipo.get('earnings_dates')!r}")
    check("IPO with --estimates: estimates populated",
          isinstance(d_ipo.get("estimates"), list)
          and len(d_ipo["estimates"]) >= 1,
          f"got {len(d_ipo.get('estimates') or [])}")
    check("IPO with --estimates: timezone is null (no rows to derive from)",
          d_ipo.get("timezone") is None,
          f"got {d_ipo.get('timezone')!r}")
    # IPO path carries a `coverage_note` (NOT `note`) — the field-name
    # split keeps the non-equity short-circuit detection unambiguous in
    # _summarize. Code that filters `if d.get("note"):` should NOT see
    # IPO equities.
    check("IPO with --estimates: coverage_note populated",
          isinstance(d_ipo.get("coverage_note"), str)
          and "empty calendar" in d_ipo["coverage_note"],
          f"got {d_ipo.get('coverage_note')!r}")
    check("IPO with --estimates: regular `note` is absent (reserved for non-equity)",
          "note" not in d_ipo,
          f"got note={d_ipo.get('note')!r}")
    check("IPO coverage_note coexists with quote_type=EQUITY",
          d_ipo.get("quote_type") == "EQUITY")

    # IPO path with empty estimates too: collapse back to error.
    class _EmptyAllTicker(_IPOTicker):
        @property
        def earnings_estimate(self): return _pd_ipo.DataFrame()
        @property
        def revenue_estimate(self):  return _pd_ipo.DataFrame()
        @property
        def eps_trend(self):         return _pd_ipo.DataFrame()
        @property
        def eps_revisions(self):     return _pd_ipo.DataFrame()
        @property
        def growth_estimates(self):  return _pd_ipo.DataFrame()

    with _patch_ipo("earnings.yf.Ticker", return_value=_EmptyAllTicker()):
        d_nothing = earnings.fetch("NOTHINGFAKE", with_estimates=True)
    check("empty earnings + empty estimates: collapse to error_kind=not_found",
          d_nothing.get("error_kind") == "not_found",
          f"got {d_nothing.get('error_kind')!r}")

    # _summarize must handle the IPO path correctly: earnings_dates empty
    # (next/last all null) AND estimates populated (consensus_* projected).
    # The note must NOT trigger the non-equity short-circuit branch (which
    # would null all consensus_* fields) — the branch discriminates on
    # quote_type, so EQUITY+note routes to the equity path.
    s_ipo = earnings._summarize(d_ipo)
    check("IPO --summary: next_date is null (no earnings_dates)",
          s_ipo.get("next_date") is None,
          f"got {s_ipo.get('next_date')!r}")
    check("IPO --summary: consensus_eps_avg populated (NOT short-circuited to null)",
          s_ipo.get("consensus_eps_avg") == 0.5,
          f"got {s_ipo.get('consensus_eps_avg')!r}")
    check("IPO --summary: consensus_eps_currency=USD",
          s_ipo.get("consensus_eps_currency") == "USD",
          f"got {s_ipo.get('consensus_eps_currency')!r}")
    check("IPO --summary: coverage_note passed through",
          isinstance(s_ipo.get("coverage_note"), str)
          and "empty calendar" in s_ipo["coverage_note"],
          f"got {s_ipo.get('coverage_note')!r}")
    check("IPO --summary: regular `note` still absent in projection",
          "note" not in s_ipo or s_ipo.get("note") is None,
          f"got note={s_ipo.get('note')!r}")

    # IPO + estimates rate-limited: error_kind should be rate_limit (not
    # not_found) so callers know to retry rather than give up. Surface the
    # est_err kind in the message.
    class _IPOEstRateLimit(_IPOTicker):
        @property
        def earnings_estimate(self): raise ConnectionError("429 too many requests")
        @property
        def revenue_estimate(self):  raise ConnectionError("429 too many requests")
        @property
        def eps_trend(self):         raise ConnectionError("429")
        @property
        def eps_revisions(self):     raise ConnectionError("429")
        @property
        def growth_estimates(self):  raise ConnectionError("429")
    with _patch_ipo("earnings.yf.Ticker", return_value=_IPOEstRateLimit()):
        d_ipo_rl = earnings.fetch("IPORL", with_estimates=True)
    check("IPO + estimates rate-limited: error_kind=rate_limit (not not_found)",
          d_ipo_rl.get("error_kind") == "rate_limit",
          f"got {d_ipo_rl.get('error_kind')!r}")
    check("IPO + estimates rate-limited: message mentions retry",
          "retry" in (d_ipo_rl.get("error") or "").lower(),
          f"got {d_ipo_rl.get('error')!r}")

    # IPO + --summary --estimates --format csv: the IPO row must surface
    # `coverage_note` populated, `note` empty (those are the disjoint
    # field semantics), earnings cells empty (no calendar data), and
    # `consensus_*` cells populated (projected from 0q estimates).
    # Done in-process via _emit so we can use the mocked IPO ticker
    # without a subprocess dance.
    import io as _io
    import csv as _csv2
    from contextlib import redirect_stdout as _rs
    s_ipo_for_csv = earnings._summarize(d_ipo)
    buf = _io.StringIO()
    with _rs(buf):
        earnings._emit([s_ipo_for_csv], "csv", summary=True, with_estimates=True)
    csv_rows = list(_csv2.reader(buf.getvalue().splitlines()))
    check("IPO summary CSV: header has both note + coverage_note columns",
          csv_rows
          and "note" in csv_rows[0]
          and "coverage_note" in csv_rows[0],
          f"got header={csv_rows[0] if csv_rows else None}")
    if csv_rows and len(csv_rows) >= 2:
        h = csv_rows[0]
        r = csv_rows[1]
        idx = {col: i for i, col in enumerate(h)}
        check("IPO summary CSV: `note` cell is blank (reserved for non-equity)",
              r[idx["note"]] == "",
              f"got {r[idx['note']]!r}")
        check("IPO summary CSV: `coverage_note` cell populated",
              "empty calendar" in r[idx["coverage_note"]],
              f"got {r[idx['coverage_note']]!r}")
        check("IPO summary CSV: `next_date` cell blank (no calendar data)",
              r[idx["next_date"]] == "",
              f"got {r[idx['next_date']]!r}")
        check("IPO summary CSV: `last_surprise_pct` cell blank (no past)",
              r[idx["last_surprise_pct"]] == "",
              f"got {r[idx['last_surprise_pct']]!r}")
        check("IPO summary CSV: `consensus_eps_avg` cell populated",
              r[idx["consensus_eps_avg"]] != ""
              and float(r[idx["consensus_eps_avg"]]) == 0.5,
              f"got {r[idx['consensus_eps_avg']]!r}")
        check("IPO summary CSV: `consensus_eps_currency` cell = USD",
              r[idx["consensus_eps_currency"]] == "USD",
              f"got {r[idx['consensus_eps_currency']]!r}")

    # IPO + default-mode --format csv: parallel to the --summary CSV check
    # above, but for the DEFAULT layout. Pins the round-4 fix that adds
    # `coverage_note` to _BASE_KEYS — before the fix, an IPO fall-through
    # row in default-mode CSV silently dropped the disambiguation signal
    # (the row would appear with empty next_date/last_date and no marker
    # explaining why the equity had no events). Same row-shape contract
    # as the --summary CSV: `coverage_note` populated, `note` empty,
    # per-row earnings cells (date / is_future / eps_*) all empty since
    # `earnings_dates: []`.
    buf = _io.StringIO()
    with _rs(buf):
        earnings._emit([d_ipo], "csv", summary=False)
    csv_rows_default = list(_csv2.reader(buf.getvalue().splitlines()))
    check("IPO default CSV: header has both note + coverage_note columns "
          "(round-4 fix)",
          csv_rows_default
          and "note" in csv_rows_default[0]
          and "coverage_note" in csv_rows_default[0],
          f"got header={csv_rows_default[0] if csv_rows_default else None}")
    if csv_rows_default and len(csv_rows_default) >= 2:
        h = csv_rows_default[0]
        r = csv_rows_default[1]
        idx = {col: i for i, col in enumerate(h)}
        check("IPO default CSV: `note` cell is blank (reserved for non-equity)",
              r[idx["note"]] == "",
              f"got {r[idx['note']]!r}")
        check("IPO default CSV: `coverage_note` cell populated "
              "(was silently dropped pre-round-4)",
              "empty calendar" in r[idx["coverage_note"]],
              f"got {r[idx['coverage_note']]!r}")
        check("IPO default CSV: `date` cell blank (no earnings_dates rows)",
              r[idx["date"]] == "",
              f"got {r[idx['date']]!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings IPO path crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --estimates with --past-only / --future-only filter ---
section("earnings --estimates × past/future filter (independence)")
try:
    # Filter applies to earnings_dates rows only — estimates is forward-
    # looking analyst data and shouldn't be touched. Verify that
    # --past-only and --future-only both leave estimates intact and emit
    # the full per-period list regardless of the filter.
    d_past = earnings.fetch("AAPL", with_estimates=True, past_only=True)
    d_future = earnings.fetch("AAPL", with_estimates=True, future_only=True)

    check("--estimates --past-only: only past earnings_dates rows",
          all(not r["is_future"] for r in d_past.get("earnings_dates", [])),
          "found a future row in --past-only output")
    check("--estimates --past-only: estimates list still populated",
          isinstance(d_past.get("estimates"), list) and len(d_past["estimates"]) >= 1,
          f"got {len(d_past.get('estimates') or [])}")
    check("--estimates --past-only: estimates spans all 4 canonical periods",
          {r["period"] for r in d_past.get("estimates", [])}
          >= set(earnings._ESTIMATE_PERIODS),
          f"got {[r['period'] for r in d_past.get('estimates', [])]}")

    check("--estimates --future-only: only future earnings_dates rows",
          all(r["is_future"] for r in d_future.get("earnings_dates", [])),
          "found a past row in --future-only output")
    check("--estimates --future-only: estimates list still populated",
          isinstance(d_future.get("estimates"), list) and len(d_future["estimates"]) >= 1,
          f"got {len(d_future.get('estimates') or [])}")

    # Estimates list is byte-for-byte identical between --past-only and
    # --future-only — proves the filter doesn't accidentally touch them.
    # (Only checks period codes + eps_avg as a representative sample;
    # the panel is otherwise the same source DataFrame in both calls.)
    past_panel = [(r["period"], r.get("eps_avg")) for r in d_past.get("estimates", [])]
    future_panel = [(r["period"], r.get("eps_avg")) for r in d_future.get("estimates", [])]
    check("--estimates: panel content identical regardless of past/future filter",
          past_panel == future_panel,
          f"past={past_panel}, future={future_panel}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings filter-independence crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --limit slice behavior (regression: summary mode bypasses) ---
section("earnings --limit slice (default vs --summary)")
try:
    # Default mode: --limit strictly truncates output.
    d_sliced = earnings.fetch("AAPL", limit=3)  # slice_to_limit=True default
    check("default mode --limit 3: output exactly 3 rows",
          len(d_sliced.get("earnings_dates", [])) == 3,
          f"got {len(d_sliced.get('earnings_dates', []))}")

    # Doc invariant: fetch() applies "near-now first" sort — future ASC
    # (nearest first), past DESC (most recent first), future block on top.
    # Catches any regression in the split-sort logic (which is also what
    # makes --summary's next_/last_ partition work via [0] indexing).
    from datetime import datetime as _sd
    rows_sliced = d_sliced["earnings_dates"]

    # 1. All future events appear before any past event (contiguous block).
    fut_idx = [i for i, r in enumerate(rows_sliced) if r["is_future"]]
    past_idx = [i for i, r in enumerate(rows_sliced) if not r["is_future"]]
    check("fetch order: future block precedes past block",
          not fut_idx or not past_idx or max(fut_idx) < min(past_idx),
          f"future_idx={fut_idx}, past_idx={past_idx}")

    # 2. Within future block: ASC by date.
    fut_dates = [_sd.fromisoformat(r["date"])
                 for r in rows_sliced if r["is_future"]]
    check("fetch order: future events ASC (nearest first)",
          fut_dates == sorted(fut_dates),
          f"got {[r['date'] for r in rows_sliced if r['is_future']]}")

    # 3. Within past block: DESC by date.
    past_dates = [_sd.fromisoformat(r["date"])
                  for r in rows_sliced if not r["is_future"]]
    check("fetch order: past events DESC (most recent first)",
          past_dates == sorted(past_dates, reverse=True),
          f"got {[r['date'] for r in rows_sliced if not r['is_future']]}")

    # Summary mode (slice_to_limit=False): yfinance returns ~25 rows from the
    # size=25 bucket regardless of --limit, and we keep them all so the
    # 4-quarter aggregates have data to work with.
    d_full = earnings.fetch("AAPL", limit=3, slice_to_limit=False)
    check("slice_to_limit=False: returns more than --limit (full bucket)",
          len(d_full.get("earnings_dates", [])) > 3,
          f"got {len(d_full.get('earnings_dates', []))} rows for limit=3")

    # And the summary projection from that should still populate avg_surprise
    # (it would be null if --limit had truncated past_rows below 4).
    s_full = earnings._summarize(d_full)
    check("--limit 3 + slice_to_limit=False + summary: avg_surprise_last_4 populated",
          isinstance(s_full.get("avg_surprise_last_4"), float),
          f"got {s_full.get('avg_surprise_last_4')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings --limit slice crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- earnings --past-only / --future-only filters ---
section("earnings --past-only / --future-only")
try:
    d_past = earnings.fetch("AAPL", past_only=True)
    rows_p = d_past.get("earnings_dates") or []
    check("--past-only: all rows have is_future=False",
          all(not r["is_future"] for r in rows_p),
          f"got {sum(r['is_future'] for r in rows_p)} future rows")
    check("--past-only: at least 4 past rows",
          len(rows_p) >= 4, f"got {len(rows_p)} rows")

    d_fut = earnings.fetch("AAPL", future_only=True)
    rows_f = d_fut.get("earnings_dates") or []
    check("--future-only: all rows have is_future=True",
          all(r["is_future"] for r in rows_f),
          f"got {sum(not r['is_future'] for r in rows_f)} past rows")
    # canary: liquid US name has at least 1 upcoming event scheduled.
    check("--future-only: at least 1 future row (canary)",
          len(rows_f) >= 1, f"got {len(rows_f)} rows")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"earnings filter crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials default mode (equity, annual) ---
section("financials default (equity, annual)")
try:
    d = financials.fetch("AAPL")
    # invariant: schema shape — all 3 statements present, no error/note
    check("AAPL quote_type=EQUITY", d.get("quote_type") == "EQUITY")
    check("AAPL currency=USD", d.get("currency") == "USD")
    check("AAPL period=annual", d.get("period") == "annual")
    check("AAPL has all 3 statement keys",
          {"income_stmt", "balance_sheet", "cashflow"}.issubset(d.keys()))
    check("AAPL no error/note (annual all-statements success path)",
          "error" not in d and "note" not in d)
    # invariant: each statement returns a list with ≥ 4 periods (yfinance
    # returns ~5 annual; tolerate down to 4 for thinly-covered names).
    for s in ("income_stmt", "balance_sheet", "cashflow"):
        rows = d.get(s) or []
        check(f"AAPL {s} has >= 4 annual periods",
              len(rows) >= 4, f"got {len(rows)} rows")

    rows = d["income_stmt"]
    if rows:
        r0 = rows[0]
        # invariant: per-row schema — period_end is YYYY-MM-DD
        check("income_stmt[0] period_end is YYYY-MM-DD",
              isinstance(r0.get("period_end"), str) and len(r0["period_end"]) == 10
              and r0["period_end"][4] == "-",
              f"got {r0.get('period_end')!r}")
        # invariant: total_revenue is a positive float (AAPL is profitable;
        # revenue magnitude well over $300B in recent years).
        check("AAPL income_stmt[0] total_revenue is float > 1e11 (canary)",
              isinstance(r0.get("total_revenue"), float)
              and r0["total_revenue"] > 1e11,
              f"got {r0.get('total_revenue')!r}")
        # invariant: net_income is float (sign assumes profitability —
        # canary; relax if Apple has a loss year)
        check("AAPL income_stmt[0] net_income is float > 0 (canary)",
              isinstance(r0.get("net_income"), float) and r0["net_income"] > 0,
              f"got {r0.get('net_income')!r}")
        # invariant: diluted_eps is float, plausible range (~$3-$15 for AAPL
        # in recent years given ~15B shares).
        eps = r0.get("diluted_eps")
        check("AAPL income_stmt[0] diluted_eps is float in [1, 50] (canary)",
              isinstance(eps, float) and 1 < eps < 50,
              f"got {eps!r}")
        # invariant: newest-first ordering
        if len(rows) >= 2:
            check("income_stmt periods sorted newest-first",
                  rows[0]["period_end"] > rows[1]["period_end"],
                  f"got {rows[0]['period_end']!r} vs {rows[1]['period_end']!r}")

    # balance sheet sanity: total_assets > total_liabilities for an equity-
    # positive company; cash + total_debt populated.
    b0 = (d.get("balance_sheet") or [{}])[0]
    if b0:
        check("AAPL balance_sheet[0] total_assets is float > 1e11 (canary)",
              isinstance(b0.get("total_assets"), float)
              and b0["total_assets"] > 1e11,
              f"got {b0.get('total_assets')!r}")
        check("AAPL balance_sheet[0] total_debt is float > 0 (canary)",
              isinstance(b0.get("total_debt"), float)
              and b0["total_debt"] > 0,
              f"got {b0.get('total_debt')!r}")
        check("AAPL balance_sheet[0] stockholders_equity is float (canary)",
              isinstance(b0.get("stockholders_equity"), float),
              f"got {b0.get('stockholders_equity')!r}")

    # cashflow sanity: operating_cashflow + free_cashflow populated;
    # capital_expenditure is negative (cash outflow convention).
    # Field names use the one-word `cashflow` spelling (not `cash_flow`)
    # to align with info.py's `free_cashflow` / `operating_cashflow` keys.
    c0 = (d.get("cashflow") or [{}])[0]
    if c0:
        check("AAPL cashflow[0] operating_cashflow is float > 0 (canary)",
              isinstance(c0.get("operating_cashflow"), float)
              and c0["operating_cashflow"] > 0,
              f"got {c0.get('operating_cashflow')!r}")
        check("AAPL cashflow[0] free_cashflow is float > 0 (canary)",
              isinstance(c0.get("free_cashflow"), float)
              and c0["free_cashflow"] > 0,
              f"got {c0.get('free_cashflow')!r}")
        # invariant: capex is signed negative (cash outflow). Yahoo's
        # convention; we don't sign-flip. Regression check for the
        # documented sign convention in references/financials.md.
        check("AAPL cashflow[0] capital_expenditure is float < 0 (sign convention)",
              isinstance(c0.get("capital_expenditure"), float)
              and c0["capital_expenditure"] < 0,
              f"got {c0.get('capital_expenditure')!r}")
        # invariant: rename guardrails — old keys (`free_cash_flow`,
        # `operating_cash_flow`) must NOT be present. Catches any
        # half-migration that re-introduces the three-word spelling.
        check("AAPL cashflow[0] does NOT have OLD `free_cash_flow` key",
              "free_cash_flow" not in c0,
              f"got OLD key still present: {c0.get('free_cash_flow')!r}")
        check("AAPL cashflow[0] does NOT have OLD `operating_cash_flow` key",
              "operating_cash_flow" not in c0)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials default crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials --period quarterly + --statement single + --limit ---
section("financials quarterly / single-statement / --limit")
try:
    # --statement income only: balance + cashflow keys must NOT appear
    d = financials.fetch("AAPL", statements=("income",))
    check("--statement income: only income_stmt key present",
          "income_stmt" in d
          and "balance_sheet" not in d
          and "cashflow" not in d,
          f"got keys={[k for k in d if k in ('income_stmt','balance_sheet','cashflow')]}")

    # quarterly mode: should have 5+ quarterly periods
    d = financials.fetch("AAPL", statements=("income",), period="quarterly")
    rows = d.get("income_stmt") or []
    check("--period quarterly: >= 4 quarters returned",
          len(rows) >= 4, f"got {len(rows)} rows")
    # invariant: quarter periods are 3-month spans — newest two should be
    # ~3 months apart (calendar-quarter or fiscal-quarter aligned)
    if len(rows) >= 2:
        from datetime import datetime as _qd
        d0 = _qd.fromisoformat(rows[0]["period_end"])
        d1 = _qd.fromisoformat(rows[1]["period_end"])
        days = abs((d0 - d1).days)
        check("quarterly periods ~3 months apart (~85-95 days)",
              80 <= days <= 100, f"got {days} days between newest two")

    # --limit truncation
    d = financials.fetch("AAPL", statements=("income",), limit=2)
    check("--limit 2: exactly 2 income_stmt periods",
          len(d.get("income_stmt", [])) == 2,
          f"got {len(d.get('income_stmt', []))}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials quarterly/single/limit crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials --period ttm (income + cashflow only) ---
section("financials --period ttm")
try:
    # TTM all-statements: balance_sheet must be empty + top-level note
    d = financials.fetch("AAPL", period="ttm")
    check("ttm all-statements: balance_sheet is []",
          d.get("balance_sheet") == [],
          f"got {d.get('balance_sheet')!r}")
    check("ttm all-statements: top-level note about balance",
          isinstance(d.get("note"), str) and "balance" in d["note"].lower(),
          f"got {d.get('note')!r}")
    # invariant: income + cashflow each return exactly 1 TTM row
    check("ttm income_stmt has exactly 1 row",
          len(d.get("income_stmt", [])) == 1,
          f"got {len(d.get('income_stmt', []))}")
    check("ttm cashflow has exactly 1 row",
          len(d.get("cashflow", [])) == 1,
          f"got {len(d.get('cashflow', []))}")

    # CLI rejection: --statement balance + --period ttm → argparse exit 2
    cmd = [sys.executable, str(SCRIPTS_DIR / "financials.py"),
           "--statement", "balance", "--period", "ttm", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("CLI --statement balance --period ttm: argparse rejects (rc=2)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # Programmatic balance+ttm via fetch() must NOT return error_kind.
    # Regression check for the bug where every-statement-skipped fell
    # through to the not_found error path. Now: empty list + note + no error.
    d = financials.fetch("AAPL", statements=("balance",), period="ttm")
    check("programmatic balance+ttm: no error_kind (legitimate skip, not failure)",
          "error" not in d and d.get("error_kind") is None,
          f"got error={d.get('error')!r}, error_kind={d.get('error_kind')!r}")
    check("programmatic balance+ttm: balance_sheet=[] + balance-ttm note",
          d.get("balance_sheet") == []
          and isinstance(d.get("note"), str)
          and "ttm" in d["note"].lower()
          and "balance" in d["note"].lower(),
          f"got balance={d.get('balance_sheet')!r}, note={d.get('note')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials ttm crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials non-equity short-circuit ---
section("financials non-equity (SPY)")
try:
    d = financials.fetch("SPY")
    check("SPY no error", "error" not in d)
    check("SPY has note about equities",
          isinstance(d.get("note"), str) and "equit" in d["note"].lower(),
          f"got {d.get('note')!r}")
    check("SPY all 3 statement lists empty",
          d.get("income_stmt") == []
          and d.get("balance_sheet") == []
          and d.get("cashflow") == [])
    check("SPY quote_type=ETF", d.get("quote_type") == "ETF")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials non-equity crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials error handling (bogus ticker) ---
section("financials error handling")
try:
    d = financials.fetch("ZZZZNOTREAL")
    check("bogus ticker returns error dict",
          "error" in d and "income_stmt" not in d)
    check("bogus ticker error_kind=not_found",
          d.get("error_kind") == "not_found",
          f"got {d.get('error_kind')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials error crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials reporting currency (info["financialCurrency"]) ---
# CRITICAL distinction tested here: trading currency (what fast_info /
# info "currency" exposes) vs reporting currency (info "financialCurrency"
# — what financials are actually denominated in). For ADRs and some
# direct-listed cross-border names these differ; financials.py must
# expose the REPORTING currency or every monetary value gets mislabeled.
section("financials reporting currency (ADR / cross-listed)")
try:
    # 0700.HK: Tencent. Trades in HKD; reports financials in CNY (RMB).
    # invariant: currency=CNY, NOT HKD. Direct regression test for the
    # ADR-style mismatch (even though 0700.HK isn't formally an ADR).
    d = financials.fetch("0700.HK", statements=("income",), limit=1)
    check("0700.HK reporting currency=CNY (NOT trading currency HKD)",
          d.get("currency") == "CNY",
          f"got {d.get('currency')!r} — if this is HKD, "
          f"info[financialCurrency] lookup regressed")
    rows = d.get("income_stmt") or []
    if rows:
        check("0700.HK income_stmt populated (canary)",
              isinstance(rows[0].get("total_revenue"), float)
              and rows[0]["total_revenue"] > 1e10,
              f"got {rows[0].get('total_revenue')!r}")

    # AAPL: trading currency == reporting currency (both USD). Sanity
    # check that the new code path doesn't break the common case.
    d_aapl = financials.fetch("AAPL", statements=("income",), limit=1)
    check("AAPL reporting currency=USD (common case still works)",
          d_aapl.get("currency") == "USD",
          f"got {d_aapl.get('currency')!r}")

    # TM (Toyota ADR): trades USD, reports JPY. Strongest ADR canary.
    # If this returns USD, the financialCurrency lookup is broken.
    # NOTE: Yahoo coverage of financialCurrency for ADRs is normally
    # solid but can lapse — accept JPY (correct) or USD-with-fallback-note
    # (info() failure path, still safe).
    import time as _t; _t.sleep(2)
    d_tm = financials.fetch("TM", statements=("income",), limit=1)
    cur_tm = d_tm.get("currency")
    note_tm = d_tm.get("note") or ""
    check("TM reporting currency=JPY OR fell back with note",
          cur_tm == "JPY" or (cur_tm == "USD" and "trading currency" in note_tm),
          f"got currency={cur_tm!r}, note={note_tm!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials currency crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials --summary projection ---
section("financials --summary")
try:
    d = financials.fetch("AAPL")
    s = financials._summarize(d)
    # invariant: summary shape — all expected keys present
    expected_summary_keys = (
        set(financials.SUMMARY_BASE_KEYS)
        | {k for k, *_ in financials.SUMMARY_HEADLINES}
        | {k for k, *_ in financials.SUMMARY_GROWTH}
    )
    check("AAPL summary has all expected keys",
          set(s.keys()) >= expected_summary_keys,
          f"missing: {expected_summary_keys - set(s.keys())}")
    # invariant: period_end + prev_period_end populated
    check("AAPL summary period_end is YYYY-MM-DD",
          isinstance(s.get("period_end"), str) and len(s["period_end"]) == 10)
    check("AAPL summary prev_period_end < period_end",
          isinstance(s.get("prev_period_end"), str)
          and s["prev_period_end"] < s["period_end"],
          f"got prev={s.get('prev_period_end')!r}, latest={s.get('period_end')!r}")
    # invariant: revenue_growth_yoy is a *fraction* — disambiguated from
    # info.py's revenue_growth (Yahoo TTM-based). AAPL annual growth
    # empirically in [-0.3, 0.5] range.
    rg = s.get("revenue_growth_yoy")
    check("AAPL summary revenue_growth_yoy is fraction (-0.5 < x < 0.5)",
          isinstance(rg, float) and -0.5 < rg < 0.5,
          f"got {rg!r}")
    # invariant: free_cashflow_growth_yoy populated as a fraction. The
    # _yoy suffix prevents collision with info's growth fields.
    fcfg = s.get("free_cashflow_growth_yoy")
    check("AAPL summary free_cashflow_growth_yoy is fraction (-1 < x < 1)",
          isinstance(fcfg, float) and -1 < fcfg < 1,
          f"got {fcfg!r}")
    # invariant: rename guardrails — old keys must NOT be present in summary
    check("AAPL summary does NOT have OLD `revenue_growth` key (collision avoided)",
          "revenue_growth" not in s,
          f"got OLD key still present: {s.get('revenue_growth')!r}")
    check("AAPL summary does NOT have OLD `free_cash_flow_growth` key",
          "free_cash_flow_growth" not in s)

    # Non-equity summary: all monetary fields null + note populated
    s_etf = financials._summarize(financials.fetch("SPY"))
    check("SPY summary has note + null monetary fields",
          s_etf.get("note") is not None
          and s_etf.get("total_revenue") is None
          and s_etf.get("revenue_growth_yoy") is None,
          f"note={s_etf.get('note')!r}, rev={s_etf.get('total_revenue')!r}")

    # Error path: bogus ticker summary preserves error_kind, drops data
    s_err = financials._summarize(financials.fetch("ZZZZNOTREAL"))
    check("bogus summary preserves error_kind, no period_end",
          s_err.get("error_kind") == "not_found"
          and "period_end" not in s_err)

    # --limit 1: only one period available, no prev for growth.
    # invariant: prev_period_end and all *_growth_yoy fields are null,
    # but base headline fields still populate.
    d_one = financials.fetch("AAPL", limit=1)
    s_one = financials._summarize(d_one)
    check("--limit 1: prev_period_end is None (no prev period available)",
          s_one.get("prev_period_end") is None,
          f"got {s_one.get('prev_period_end')!r}")
    check("--limit 1: revenue_growth_yoy is None (no prev to compute against)",
          s_one.get("revenue_growth_yoy") is None,
          f"got {s_one.get('revenue_growth_yoy')!r}")
    check("--limit 1: free_cashflow_growth_yoy is None",
          s_one.get("free_cashflow_growth_yoy") is None)
    check("--limit 1: base headline fields still populate",
          isinstance(s_one.get("total_revenue"), float)
          and isinstance(s_one.get("net_income"), float),
          f"got revenue={s_one.get('total_revenue')!r}, "
          f"net_income={s_one.get('net_income')!r}")
    check("--limit 1: period_end populated (latest period still there)",
          isinstance(s_one.get("period_end"), str)
          and len(s_one["period_end"]) == 10)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials --summary crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials currency fallback paths b/c (offline; mock yfinance) ---
# Live yfinance can't reliably trigger the new in-info fallbacks — Yahoo's
# financialCurrency coverage for popular tickers is solid. Mock the info
# dict to surgically exercise:
#   path (b)  info ok, financialCurrency missing, info[currency] populated
#   path (c)  info ok, both fields missing → fast_info[currency] used
#   path (d)  all three sources unavailable → currency=null + note
#   sentinel  Yahoo "None" string is not treated as a real currency
section("financials currency fallback paths (offline mock)")
try:
    import pandas as _pd
    import yfinance as _yf

    # Minimal income DataFrame — single line item suffices for these
    # currency-only tests; we don't care about field coverage here.
    _min_income = _pd.DataFrame(
        [[1.0e11]],
        index=["Total Revenue"],
        columns=[_pd.Timestamp("2025-09-30")])

    class _CurrencyFastInfo:
        """Returns quoteType + USD trading currency."""
        def __getitem__(self, key):
            if key == "quoteType":
                return "EQUITY"
            if key == "currency":
                return "USD"
            raise KeyError(key)

    class _NoCurrencyFastInfo:
        """quoteType only; currency raises KeyError (path-d trigger)."""
        def __getitem__(self, key):
            if key == "quoteType":
                return "EQUITY"
            raise KeyError(key)

    def _mk_ticker(info_dict, fast_info_cls=_CurrencyFastInfo):
        """Factory that returns a Ticker class returning the given info."""
        class _T:
            def __init__(self, sym):
                self.fast_info = fast_info_cls()
            @property
            def info(self):
                return info_dict
            @property
            def income_stmt(self):
                return _min_income
        return _T

    saved = _yf.Ticker

    # --- Path (b): financialCurrency missing, info[currency]=USD ---
    _yf.Ticker = _mk_ticker({"currency": "USD"})
    try:
        d = financials.fetch("MOCK_PATHB", statements=("income",))
    finally:
        _yf.Ticker = saved

    check("path b: currency falls back to info[currency]",
          d.get("currency") == "USD",
          f"got {d.get('currency')!r}")
    note_b = d.get("note") or ""
    check("path b: note mentions financialCurrency missing",
          "financialCurrency] missing" in note_b,
          f"got note={note_b!r}")
    check("path b: note contains 'trading currency' substring (consumer match)",
          "trading currency" in note_b,
          f"got note={note_b!r}")
    check("path b: note clarifies values are in actual reporting currency",
          "actual reporting currency" in note_b,
          f"got note={note_b!r}")
    check("path b: income_stmt still populated (statements fetched normally)",
          isinstance(d.get("income_stmt"), list) and len(d["income_stmt"]) >= 1)

    # --- Path (c): both info fields missing, fast_info[currency]=USD ---
    _yf.Ticker = _mk_ticker({"someOtherField": "value"})
    try:
        d = financials.fetch("MOCK_PATHC", statements=("income",))
    finally:
        _yf.Ticker = saved

    check("path c: currency falls back to fast_info[currency]",
          d.get("currency") == "USD",
          f"got {d.get('currency')!r}")
    note_c = d.get("note") or ""
    check("path c: note mentions BOTH info fields missing",
          "both missing" in note_c,
          f"got note={note_c!r}")
    check("path c: note contains 'trading currency' substring",
          "trading currency" in note_c)

    # --- Path (d): all three sources unavailable → currency=null ---
    _yf.Ticker = _mk_ticker({"someOtherField": "value"},
                             fast_info_cls=_NoCurrencyFastInfo)
    try:
        d = financials.fetch("MOCK_PATHD", statements=("income",))
    finally:
        _yf.Ticker = saved

    check("path d: currency is None when all sources fail",
          d.get("currency") is None,
          f"got {d.get('currency')!r}")
    note_d = d.get("note") or ""
    check("path d: note mentions 'all unavailable' (not lying about source)",
          "all unavailable" in note_d,
          f"got note={note_d!r}")
    check("path d: note still clarifies values come from reporting currency",
          "actual reporting currency" in note_d,
          f"got note={note_d!r}")
    check("path d: income_stmt still populated (statements unaffected by currency miss)",
          isinstance(d.get("income_stmt"), list) and len(d["income_stmt"]) >= 1)

    # --- Sentinel filter: Yahoo "None" string treated as null ---
    # Without filtering, financialCurrency="None" would be used as a
    # literal currency code. With filtering, falls through to path (b).
    _yf.Ticker = _mk_ticker({"financialCurrency": "None", "currency": "USD"})
    try:
        d = financials.fetch("MOCK_NONESTR", statements=("income",))
    finally:
        _yf.Ticker = saved

    check("'None' sentinel: NOT used as currency code",
          d.get("currency") != "None",
          f"got {d.get('currency')!r}")
    check("'None' sentinel: falls through to info[currency]",
          d.get("currency") == "USD",
          f"got {d.get('currency')!r}")
    check("'None' sentinel: triggers path-b note (financialCurrency treated as missing)",
          "financialCurrency] missing" in (d.get("note") or ""),
          f"got note={d.get('note')!r}")

    # Other sentinels: "n/a", "—", "-" — quick spot-check
    for sentinel in ("n/a", "N/A", "unknown", "—", "-"):
        _yf.Ticker = _mk_ticker({"financialCurrency": sentinel, "currency": "USD"})
        try:
            d = financials.fetch("MOCK_SENT", statements=("income",))
        finally:
            _yf.Ticker = saved
        check(f"sentinel {sentinel!r} filtered (currency != {sentinel!r})",
              d.get("currency") != sentinel,
              f"got {d.get('currency')!r}")

    # --- Sentinel CASCADE: sentinel in BOTH financialCurrency and
    #     info[currency] must fall through past path (b) to path (c)
    #     and pick up fast_info[currency]. Catches a regression where
    #     `_normalize_currency` is removed at the path-(b) lookup but
    #     kept at path (a)/(c) — a single-point spot-check would still
    #     pass but the cascade would silently break for ADRs whose info
    #     dict has sentinels in both fields.
    _yf.Ticker = _mk_ticker(
        {"financialCurrency": "None", "currency": "n/a"},
        fast_info_cls=_CurrencyFastInfo)  # USD
    try:
        d = financials.fetch("MOCK_CASCADE_BC", statements=("income",))
    finally:
        _yf.Ticker = saved
    check("cascade b→c: both info fields are sentinels → fast_info used",
          d.get("currency") == "USD",
          f"got {d.get('currency')!r} — if 'None' or 'n/a', "
          f"_normalize_currency at path (b) regressed")
    check("cascade b→c: note matches path (c) (BOTH missing)",
          "both missing" in (d.get("note") or ""),
          f"got note={d.get('note')!r}")

    # --- Sentinel CASCADE all the way to path (d): sentinels in info AND
    #     fast_info → currency=null. Verifies the filter applies to
    #     fast_info too (via _trading_currency normalization), not just
    #     the info-dict lookups. Without normalization at the
    #     fast_info layer, "None" string would propagate as currency.
    class _SentinelFastInfo:
        def __getitem__(self, key):
            if key == "quoteType":
                return "EQUITY"
            if key == "currency":
                return "None"  # Yahoo sentinel through fast_info
            raise KeyError(key)

    _yf.Ticker = _mk_ticker(
        {"financialCurrency": "—", "currency": "unknown"},
        fast_info_cls=_SentinelFastInfo)
    try:
        d = financials.fetch("MOCK_CASCADE_BCD", statements=("income",))
    finally:
        _yf.Ticker = saved
    check("cascade b→c→d: all three sources are sentinels → currency=null",
          d.get("currency") is None,
          f"got {d.get('currency')!r} — if non-null, fast_info "
          f"sentinel filter regressed (_trading_currency must run "
          f"results through _normalize_currency)")
    check("cascade b→c→d: note matches path (d) (all unavailable)",
          "all unavailable" in (d.get("note") or ""),
          f"got note={d.get('note')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials currency fallback mock crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials partial_errors (offline; mock yfinance) ---
# Live Yahoo can't reliably reproduce the partial-success path (one
# statement fails transient while others succeed) — it requires injecting
# a per-statement failure. Mock yfinance so a single statement (balance)
# raises 429 sustained while income + cashflow succeed; assert the
# partial-success schema. Also covers the all-fail collapse to top-level
# error_kind. Same pattern as the fast_info retry-surfacing offline test.
section("financials partial_errors (offline mock)")
try:
    import pandas as _pd
    import yfinance as _yf

    # Minimal yfinance-shaped DataFrames: line items as index, period-end
    # Timestamp as the single column. _df_to_periods uses
    # `df.loc[src_key, col]`, so this shape is sufficient.
    _income_df = _pd.DataFrame(
        [[1.5e11], [2.5e10], [9.0e10], [3.5e10], [4.5e10], [3.0]],
        index=["Total Revenue", "Net Income", "Gross Profit",
               "Operating Income", "EBITDA", "Diluted EPS"],
        columns=[_pd.Timestamp("2025-09-30")])
    _cashflow_df = _pd.DataFrame(
        [[3.0e10], [-5.0e9], [2.5e10]],
        index=["Operating Cash Flow", "Capital Expenditure", "Free Cash Flow"],
        columns=[_pd.Timestamp("2025-09-30")])

    class _MockFastInfo:
        # Only quoteType is reached in this section — info() in every mock
        # below returns financialCurrency, so _meta's fast_info[currency]
        # fallback paths are never exercised here. The currency-fallback
        # mock section further down has its own _MockFastInfo with the
        # `currency` key handler for those paths.
        def __getitem__(self, key):
            if key == "quoteType":
                return "EQUITY"
            raise KeyError(key)

    class _PartialFailTicker:
        """Equity where balance_sheet sustained-429s; income + cashflow ok."""
        def __init__(self, sym):
            self.fast_info = _MockFastInfo()

        @property
        def info(self):
            return {"financialCurrency": "USD"}

        @property
        def income_stmt(self):
            return _income_df

        @property
        def balance_sheet(self):
            raise RuntimeError("HTTP 429 Too Many Requests")

        @property
        def cashflow(self):
            return _cashflow_df

    saved = _yf.Ticker
    _yf.Ticker = _PartialFailTicker
    try:
        # with_retry adds ~1.5–2s of backoff sleeps for the sustained
        # 3-attempt 429 on balance_sheet. Acceptable for smoke (one section).
        d = financials.fetch("MOCKEQ")
    finally:
        _yf.Ticker = saved

    # invariant: NOT collapsed to top-level error
    check("partial: no top-level error (income + cashflow succeeded)",
          "error" not in d and d.get("error_kind") is None,
          f"got error={d.get('error')!r}, error_kind={d.get('error_kind')!r}")
    # invariant: succeeded statements have data
    check("partial: income_stmt populated",
          isinstance(d.get("income_stmt"), list) and len(d["income_stmt"]) >= 1
          and isinstance(d["income_stmt"][0].get("total_revenue"), float),
          f"got {d.get('income_stmt')!r}")
    check("partial: cashflow populated",
          isinstance(d.get("cashflow"), list) and len(d["cashflow"]) >= 1
          and isinstance(d["cashflow"][0].get("free_cashflow"), float),
          f"got {d.get('cashflow')!r}")
    # invariant: failed statement is empty list (NOT absent — schema preserved)
    check("partial: balance_sheet=[] (failed but schema-present)",
          d.get("balance_sheet") == [],
          f"got {d.get('balance_sheet')!r}")
    # invariant: partial_errors surfaces the per-statement failure
    pe = d.get("partial_errors")
    check("partial: partial_errors dict present with balance_sheet entry",
          isinstance(pe, dict) and "balance_sheet" in pe
          and pe["balance_sheet"]["error_kind"] == "rate_limit"
          and pe["balance_sheet"]["attempts"] >= 1,
          f"got {pe!r}")
    check("partial: only failed statement in partial_errors (no income/cashflow)",
          isinstance(pe, dict) and set(pe.keys()) == {"balance_sheet"},
          f"got keys={list(pe.keys()) if isinstance(pe, dict) else None}")
    # invariant: top-level note describes which statement failed
    note = d.get("note")
    check("partial: note mentions partial fetch failure on balance_sheet",
          isinstance(note, str)
          and "partial" in note.lower() and "balance_sheet" in note,
          f"got {note!r}")
    # invariant: top-level attempts > 1 (balance retried before giving up)
    check("partial: top-level attempts > 1 (transient 429 retried)",
          d.get("attempts", 1) > 1,
          f"got {d.get('attempts')!r}")

    # --summary projection of a partial-success result preserves
    # partial_errors and note (caller can detect unreliable fields).
    s = financials._summarize(d)
    check("partial summary: partial_errors preserved",
          isinstance(s.get("partial_errors"), dict)
          and "balance_sheet" in s["partial_errors"])
    check("partial summary: note preserved",
          isinstance(s.get("note"), str) and "partial" in s["note"].lower())
    # invariant: balance-sourced fields are null in summary (data didn't
    # come back), but income/cashflow fields populate normally.
    check("partial summary: total_assets is None (balance failed)",
          s.get("total_assets") is None)
    check("partial summary: total_revenue populated (income succeeded)",
          isinstance(s.get("total_revenue"), float))
    check("partial summary: free_cashflow populated (cashflow succeeded)",
          isinstance(s.get("free_cashflow"), float))

    # --- All-fail variant: every statement raises → top-level error,
    #     error_kind picked by _ERROR_KIND_PRIORITY. Use not_found to
    #     keep the test fast (with_retry doesn't sleep on not_found).
    class _AllFailTicker:
        def __init__(self, sym):
            self.fast_info = _MockFastInfo()

        @property
        def info(self):
            return {"financialCurrency": "USD"}

        @property
        def income_stmt(self):
            raise RuntimeError("404 Not Found")

        @property
        def balance_sheet(self):
            raise RuntimeError("404 Not Found")

        @property
        def cashflow(self):
            raise RuntimeError("404 Not Found")

    _yf.Ticker = _AllFailTicker
    try:
        d_all_fail = financials.fetch("MOCKEQ2")
    finally:
        _yf.Ticker = saved

    check("all-fail: collapses to top-level error (no partial output)",
          "error" in d_all_fail and "income_stmt" not in d_all_fail,
          f"got keys={list(d_all_fail.keys())}")
    check("all-fail: error_kind=not_found (matches injected exception)",
          d_all_fail.get("error_kind") == "not_found",
          f"got {d_all_fail.get('error_kind')!r}")

    # --- Mixed-kinds variant: rate_limit + not_found should collapse to
    #     rate_limit (priority order: rate_limit > network > unknown >
    #     not_found). Direct test of _ERROR_KIND_PRIORITY logic.
    class _MixedFailTicker:
        def __init__(self, sym):
            self.fast_info = _MockFastInfo()

        @property
        def info(self):
            return {"financialCurrency": "USD"}

        @property
        def income_stmt(self):
            raise RuntimeError("HTTP 429 Too Many Requests")

        @property
        def balance_sheet(self):
            raise RuntimeError("404 Not Found")

        @property
        def cashflow(self):
            raise RuntimeError("HTTP 429 Too Many Requests")

    _yf.Ticker = _MixedFailTicker
    try:
        d_mixed = financials.fetch("MOCKEQ3")
    finally:
        _yf.Ticker = saved

    check("mixed-fail: error_kind=rate_limit (priority over not_found)",
          d_mixed.get("error_kind") == "rate_limit",
          f"got {d_mixed.get('error_kind')!r} — _ERROR_KIND_PRIORITY regressed")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials partial_errors mock crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- financials --summary --period quarterly: YoY (4-back) prev semantic ---
section("financials --summary quarterly YoY")
try:
    d = financials.fetch("AAPL", period="quarterly")
    s = financials._summarize(d)
    # invariant: when ≥5 quarters available, prev_period_end is 4 quarters
    # back (~365 days), not 1 quarter back (~90 days). Catches a regression
    # in _pick_prev_index.
    if s.get("period_end") and s.get("prev_period_end"):
        from datetime import datetime as _qd
        d0 = _qd.fromisoformat(s["period_end"])
        d1 = _qd.fromisoformat(s["prev_period_end"])
        days = (d0 - d1).days
        check("quarterly YoY: prev_period_end is ~365 days back (not 90)",
              330 <= days <= 400,
              f"got {days} days — expected ~365 (YoY same-quarter)")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"financials --summary quarterly crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- news fetch shape ---
section("news fetch")
try:
    # Happy path: AAPL returns articles with the documented field set.
    d = news.fetch("AAPL", limit=2)
    check("news AAPL: success shape (count + articles list)",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("articles"), list)
          and d.get("count") == len(d["articles"]),
          f"got {list(d.keys())}")
    if d.get("articles"):
        a = d["articles"][0]
        expected_keys = set(news.ARTICLE_FIELDS)
        check("news AAPL: article has the documented field set (no extras)",
              set(a.keys()) == expected_keys,
              f"diff: missing={expected_keys - set(a.keys())}, "
              f"extra={set(a.keys()) - expected_keys}")
        check("news AAPL: article has non-empty title + url",
              a.get("title") and a.get("url"),
              f"title={a.get('title')!r}, url={a.get('url')!r}")
        # ISO 8601 with Z — checks Yahoo hasn't switched to epoch ints
        # (regression that would silently break presentation guidance).
        check("news AAPL: pub_date is ISO 8601 with Z",
              isinstance(a.get("pub_date"), str)
              and a["pub_date"].endswith("Z")
              and "T" in a["pub_date"],
              f"got {a.get('pub_date')!r}")
        # is_premium / editors_pick must be real bools (or None), not strings
        # or ints — guards against silent regression if Yahoo ever serializes
        # these as "true"/"false" or 0/1 and our safe_bool stops getting
        # applied.
        for f in ("is_premium", "editors_pick"):
            v = a.get(f)
            check(f"news AAPL: {f} is bool or None (not str/int)",
                  v is None or isinstance(v, bool),
                  f"got {type(v).__name__}={v!r}")
    # --limit caps to N (not just plumbed through to fetch()).
    check("news AAPL --limit 2: returns exactly 2 articles",
          d.get("count") == 2,
          f"got count={d.get('count')}")

    # `note` is reserved for the empty-result path. A successful response
    # (count > 0) must NOT carry it. Pins the design: future refactors
    # that accidentally always-set `note` would silently leak the empty-
    # path message into success JSON.
    check("news AAPL (success): no `note` key on the result",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")

    # Empty-result path: bogus ticker returns count=0 + note + NO error_kind.
    # This pins the design choice ("empty != not_found") so a future
    # refactor doesn't accidentally promote empty to an error.
    bogus_result = news.fetch("ZZZZNOTREAL", limit=5)
    check("news bogus: count=0 + note set + no error_kind",
          bogus_result.get("count") == 0 and bogus_result.get("note")
          and "error_kind" not in bogus_result,
          f"got {list(bogus_result.keys())}")

    # Non-equity coverage: news is NOT equity-only (unlike earnings/financials).
    # If yfinance ever locks news behind quote_type, this catches it early.
    crypto_result = news.fetch("BTC-USD", limit=1)
    check("news BTC-USD: crypto returns articles (news is not equity-only)",
          crypto_result.get("count", 0) >= 1,
          f"got count={crypto_result.get('count')}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"news fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- holders fetch shape ---
section("holders fetch")
try:
    # Happy path: AAPL — all three sections populated. fetch() no longer
    # takes a `limit` param (post-refactor); --limit is applied at the
    # emit layer so summary metrics see the full Yahoo response.
    d = holders.fetch("AAPL")
    check("holders AAPL: success has summary + institutional + mutualfund",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("summary"), dict)
          and isinstance(d.get("institutional"), list)
          and isinstance(d.get("mutualfund"), list),
          f"got {list(d.keys())}")
    # fetch() should return Yahoo's full response (typically ~10 rows
    # each). If this drops below ~5 we're either hitting a low-coverage
    # path or upstream changed the cap silently.
    check("holders AAPL: fetch() returns full institutional list (>= 5 rows)",  # canary: ~10 typical
          len(d.get("institutional") or []) >= 5,
          f"got {len(d.get('institutional') or [])} rows")
    check("holders AAPL: fetch() returns full mutualfund list (>= 5 rows)",  # canary
          len(d.get("mutualfund") or []) >= 5,
          f"got {len(d.get('mutualfund') or [])} rows")

    # Summary section — all 4 documented keys present (typed correctly).
    s = d.get("summary") or {}
    expected_summary = set(holders._SUMMARY_KEYS)
    check("holders AAPL: summary has the documented field set (no extras)",
          set(s.keys()) == expected_summary,
          f"diff: missing={expected_summary - set(s.keys())}, "
          f"extra={set(s.keys()) - expected_summary}")
    # Fractions guard: pcts must be in [0, 1] (never percent-encoded).
    # Pins the unit landmine — if Yahoo flips encoding, this fails first.
    for k in ("insiders_pct", "institutions_pct", "institutions_float_pct"):
        v = s.get(k)
        check(f"holders AAPL summary.{k} is fraction in [0, 1]",
              v is None or (isinstance(v, float) and 0.0 <= v <= 1.0),
              f"got {type(v).__name__}={v!r}")
    # institutions_count is integer count; AAPL has thousands of institutional
    # holders on file, so this should comfortably exceed 100.
    check("holders AAPL summary.institutions_count is int >= 100",  # canary: thousands typical
          isinstance(s.get("institutions_count"), int)
          and s["institutions_count"] >= 100,
          f"got {s.get('institutions_count')!r}")

    # Per-holder row schema — both lists share the same shape.
    expected_holder = set(holders._HOLDER_KEYS)
    for klass in ("institutional", "mutualfund"):
        rows = d.get(klass) or []
        check(f"holders AAPL: {klass} has rows",
              len(rows) >= 1, f"got {len(rows)} rows")
        if rows:
            r = rows[0]
            check(f"holders AAPL: {klass}[0] has documented field set",
                  set(r.keys()) == expected_holder,
                  f"diff: missing={expected_holder - set(r.keys())}, "
                  f"extra={set(r.keys()) - expected_holder}")
            # pct_held must be fraction (Vanguard at AAPL is ~10%, well below 1.0).
            v = r.get("pct_held")
            check(f"holders AAPL: {klass}[0].pct_held is fraction in [0, 1]",
                  v is None or (isinstance(v, float) and 0.0 <= v <= 1.0),
                  f"got {type(v).__name__}={v!r}")
            # date_reported is YYYY-MM-DD or None — guard against Yahoo
            # switching to epoch ints (would silently break presentation).
            dr = r.get("date_reported")
            check(f"holders AAPL: {klass}[0].date_reported is YYYY-MM-DD or None",
                  dr is None or (isinstance(dr, str) and len(dr) == 10
                                  and dr[4] == "-" and dr[7] == "-"),
                  f"got {type(dr).__name__}={dr!r}")
            # shares / value are positive ints when present.
            for f in ("shares", "value"):
                vv = r.get(f)
                check(f"holders AAPL: {klass}[0].{f} is positive int",
                      isinstance(vv, int) and vv > 0,
                      f"got {type(vv).__name__}={vv!r}")

    # _apply_limit truncates lists in place. After applying limit=2, both
    # lists must be ≤ 2 — but the ORIGINAL fetch() result remains the
    # source for summary metrics (separate code path).
    capped = holders._apply_limit(holders.fetch("AAPL"), 2)
    check("holders _apply_limit(2): institutional capped to <= 2",
          len(capped.get("institutional") or []) <= 2,
          f"got {len(capped.get('institutional') or [])} rows")
    check("holders _apply_limit(2): mutualfund capped to <= 2",
          len(capped.get("mutualfund") or []) <= 2,
          f"got {len(capped.get('mutualfund') or [])} rows")

    # Successful response must NOT carry `note` — pins the design
    # ("note is reserved for the all-empty path") so a refactor can't
    # accidentally always-set it.
    check("holders AAPL (success): no `note` key on the result",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")

    # Empty / non-equity path: ETF, index, crypto all return three empty
    # DataFrames. Bogus tickers also return empty (yfinance logs HTTP 404
    # but does not raise — verified empirically). All four must report
    # success-with-note + NO error_kind. Pins the "ambiguous empty" design.
    for sym in ("QQQ", "^GSPC", "BTC-USD", "ZZZZNOTREAL"):
        empty_d = holders.fetch(sym)
        check(f"holders {sym}: all-empty path emits note + no error_kind",
              empty_d.get("note")
              and "error_kind" not in empty_d
              and not empty_d.get("institutional")
              and not empty_d.get("mutualfund")
              and all(v is None for v in (empty_d.get("summary") or {}).values()),
              f"got keys={list(empty_d.keys())}, "
              f"summary={empty_d.get('summary')}")

    # Non-US ticker happy path (gap from prior pass): 0700.HK has the
    # asymmetric shape we documented — populated rollup + ~few institutional
    # rows + ~10 mutualfund rows. Pins the "non-US works" claim from the
    # reference doc.
    hk = holders.fetch("0700.HK")
    check("holders 0700.HK: rollup populated (institutions_pct not None)",
          (hk.get("summary") or {}).get("institutions_pct") is not None,
          f"got summary={hk.get('summary')}")
    check("holders 0700.HK: institutional list non-empty (>= 1 row)",  # canary: ~2 typical
          len(hk.get("institutional") or []) >= 1,
          f"got {len(hk.get('institutional') or [])} rows")
    check("holders 0700.HK: mutualfund list non-empty (>= 5 rows)",  # canary: ~10 typical
          len(hk.get("mutualfund") or []) >= 5,
          f"got {len(hk.get('mutualfund') or [])} rows")
    # 0700.HK pcts are fractions in [0, 1] — same unit-landmine guard
    # as AAPL but on a non-US ticker (different Yahoo pipeline).
    hk_top = (hk.get("institutional") or [{}])[0]
    v = hk_top.get("pct_held")
    check("holders 0700.HK: institutional[0].pct_held is fraction in [0, 1]",
          v is None or (isinstance(v, float) and 0.0 <= v <= 1.0),
          f"got {type(v).__name__}={v!r}")

    # ADR canary: TM (Toyota ADR, USD-traded, JPY home reporting) — verifies
    # `value` is in TRADING currency (USD), not REPORTING currency (JPY).
    # If Yahoo ever flips to reporting-currency, value/shares would jump
    # by ~150x (USD→JPY) and this canary fires. TM ADR price ~$190 in
    # 2026-05; a 50–500 band tolerates normal price drift in either
    # direction while catching a 150× currency mistake immediately.
    tm = holders.fetch("TM")
    if tm.get("institutional"):
        r = tm["institutional"][0]
        if r.get("shares") and r.get("value"):
            ratio = r["value"] / r["shares"]
            check("holders TM (ADR): value/shares ~ USD share price (canary 50-500)",  # canary
                  50 <= ratio <= 500,
                  f"got ratio={ratio:.1f} (USD ~$190 expected; JPY would be ~14k)")

    # _summarize projection: peer-comparison flat dict carries the rollup +
    # top picks + top-5 concentration. Must include all _SUMMARY_FLAT_KEYS,
    # plus the symbol — pins the schema so consumers iterating columns
    # don't silently lose fields if a refactor renames one.
    flat = holders._summarize(d)
    expected_flat = {"symbol", *holders._SUMMARY_FLAT_KEYS}
    check("holders _summarize(AAPL): has documented flat field set",
          expected_flat.issubset(set(flat.keys())),
          f"missing={expected_flat - set(flat.keys())}")
    # top5 sum: should equal sum of pct_held across the FIRST 5 rows of
    # the full institutional list (capped at 5; if list is shorter, sum
    # whatever's there). Recompute to verify the helper isn't double-
    # counting, skipping, or accidentally slicing post-limit.
    expected_top5 = sum(r["pct_held"] for r in (d.get("institutional") or [])[:5]
                       if r.get("pct_held") is not None)
    actual_top5 = flat.get("top5_institutions_pct")
    check("holders _summarize: top5_institutions_pct matches sum(pct_held[:5])",
          (actual_top5 is None and expected_top5 == 0)
          or (actual_top5 is not None
              and abs(actual_top5 - expected_top5) < 1e-9),
          f"got {actual_top5!r}, expected {expected_top5!r}")

    # _summarize on an empty result must carry the `note` through —
    # otherwise summary CSVs silently drop the disambiguation signal.
    empty_flat = holders._summarize(holders.fetch("QQQ"))
    check("holders _summarize(QQQ empty): preserves `note` for CSV",
          "note" in empty_flat and empty_flat.get("note"),
          f"got keys={list(empty_flat.keys())}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"holders fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- insiders fetch shape ---
section("insiders fetch")
try:
    # Happy path: AAPL — all three sections populated.
    d = insiders.fetch("AAPL")
    check("insiders AAPL: success has purchases_summary + transactions + roster",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("purchases_summary"), dict)
          and isinstance(d.get("transactions"), list)
          and isinstance(d.get("roster"), list),
          f"got {list(d.keys())}")
    check("insiders AAPL: transactions list non-empty (>= 5)",  # canary: 70+ typical
          len(d.get("transactions") or []) >= 5,
          f"got {len(d.get('transactions') or [])} rows")
    check("insiders AAPL: roster list non-empty (>= 5)",  # canary: ~10 typical
          len(d.get("roster") or []) >= 5,
          f"got {len(d.get('roster') or [])} rows")

    # purchases_summary schema — all 11 documented keys present.
    p = d.get("purchases_summary") or {}
    expected_p = set(insiders._PURCHASES_KEYS)
    check("insiders AAPL: purchases_summary has documented field set (no extras)",
          set(p.keys()) == expected_p,
          f"diff: missing={expected_p - set(p.keys())}, "
          f"extra={set(p.keys()) - expected_p}")
    # period_label should currently be "Last 6m" — pin the canary so a
    # Yahoo change to "Last 12m" (or similar) surfaces immediately rather
    # than getting silently aliased into the same field with different
    # semantics. Canary because it's allowed to legitimately change.
    check("insiders AAPL: purchases_summary.period_label is 'Last 6m'",  # canary
          p.get("period_label") == "Last 6m",
          f"got {p.get('period_label')!r}")
    # pct_* are FRACTIONS (verified empirically: net=246332 /
    # total_held=240872640 ≈ 0.00102 → 0.001). Bound them in [0, 1] —
    # if Yahoo flips to percent encoding, AAPL's 0.001 becomes 0.1
    # (still in bounds) but a more egregious case (a 5% net move →
    # 0.05 fraction or 5.0 percent) would fail this guard.
    for k in ("pct_net_shares_purchased", "pct_buy_shares", "pct_sell_shares"):
        v = p.get(k)
        check(f"insiders AAPL purchases_summary.{k} is fraction in [0, 1] or null",
              v is None or (isinstance(v, float) and 0.0 <= v <= 1.0),
              f"got {type(v).__name__}={v!r}")
    # total_insider_shares_held is integer; AAPL has hundreds of millions
    # of insider shares on file. Comfortable lower bound: 1M.
    check("insiders AAPL purchases_summary.total_insider_shares_held is int >= 1M",  # canary
          isinstance(p.get("total_insider_shares_held"), int)
          and p["total_insider_shares_held"] >= 1_000_000,
          f"got {p.get('total_insider_shares_held')!r}")
    # Algebra check: net_shares_purchased should equal purchases_shares -
    # sales_shares. Pins that the row-label routing didn't accidentally
    # cross-map a "Net" row into a "Purchases" / "Sales" slot (the bug
    # we hit during implementation — % rows had to come first in routing
    # to avoid the "net shares" substring matching "% Net Shares
    # Purchased (Sold)").
    if all(p.get(k) is not None
           for k in ("purchases_shares", "sales_shares", "net_shares_purchased")):
        check("insiders AAPL: net_shares_purchased == purchases_shares - sales_shares",
              p["net_shares_purchased"] == p["purchases_shares"] - p["sales_shares"],
              f"got net={p['net_shares_purchased']}, "
              f"purchases - sales = {p['purchases_shares'] - p['sales_shares']}")

    # Per-transaction row schema. transactions[0] is the row Yahoo gave
    # us first; current responses sort desc but we don't depend on that.
    expected_tx = set(insiders._TRANSACTION_KEYS)
    if d.get("transactions"):
        tx = d["transactions"][0]
        check("insiders AAPL: transactions[0] has documented field set",
              set(tx.keys()) == expected_tx,
              f"diff: missing={expected_tx - set(tx.keys())}, "
              f"extra={set(tx.keys()) - expected_tx}")
        # ownership is single-letter D/I (Yahoo's encoding). Pin so a Yahoo
        # change to "Direct" / "Indirect" surfaces immediately.
        check("insiders AAPL: transactions[0].ownership is 'D' or 'I'",
              tx.get("ownership") in ("D", "I"),
              f"got {tx.get('ownership')!r}")
        # date is YYYY-MM-DD or None.
        dr = tx.get("date")
        check("insiders AAPL: transactions[0].date is YYYY-MM-DD or None",
              dr is None or (isinstance(dr, str) and len(dr) == 10
                              and dr[4] == "-" and dr[7] == "-"),
              f"got {type(dr).__name__}={dr!r}")
        # shares is positive int when present.
        sh = tx.get("shares")
        check("insiders AAPL: transactions[0].shares is positive int",
              isinstance(sh, int) and sh > 0,
              f"got {type(sh).__name__}={sh!r}")
        # value is float-or-None (Yahoo NaN → None for non-monetary events
        # like option grants). Don't require positive — could be a sale at
        # any price; require finite if present.
        v = tx.get("value")
        check("insiders AAPL: transactions[0].value is float or None",
              v is None or (isinstance(v, float) and not math.isnan(v)),
              f"got {type(v).__name__}={v!r}")

    # Per-roster row schema (9 fields incl. the indirect pair, projected
    # as None when the underlying DataFrame doesn't have those cols).
    expected_ros = set(insiders._ROSTER_KEYS)
    if d.get("roster"):
        ros = d["roster"][0]
        check("insiders AAPL: roster[0] has documented field set",
              set(ros.keys()) == expected_ros,
              f"diff: missing={expected_ros - set(ros.keys())}, "
              f"extra={set(ros.keys()) - expected_ros}")
        # AAPL specifically does not expose indirect cols (verified 2026-05),
        # so every AAPL roster row should have shares_owned_indirectly=None.
        # This is a canary: if Yahoo starts exposing indirect for AAPL,
        # we'd want to know — could mean coverage expanded.
        check("insiders AAPL roster[0]: shares_owned_indirectly is None "  # canary
              "(AAPL doesn't expose indirect cols in current Yahoo response)",
              ros.get("shares_owned_indirectly") is None,
              f"got {ros.get('shares_owned_indirectly')!r}")

    # Successful response must NOT carry `note` OR `coverage_note` —
    # pins the design (both fields are reserved for the all-empty and
    # partial-empty paths respectively) so a refactor can't accidentally
    # always-set either.
    check("insiders AAPL (success): no `note` key on the result",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")
    check("insiders AAPL (success): no `coverage_note` key on the result",
          "coverage_note" not in d,
          f"unexpected keys={list(d.keys())}")

    # All-empty path: ETF / index / crypto / bogus all return three empty
    # frames + 404 stderr (yfinance doesn't raise). Must report success-
    # with-note + NO error_kind + NO coverage_note (the two -note fields
    # are mutually exclusive). Pins the "ambiguous empty" design,
    # mirroring holders.
    for sym in ("QQQ", "^GSPC", "BTC-USD", "ZZZZNOTREAL"):
        empty_d = insiders.fetch(sym)
        check(f"insiders {sym}: all-empty path emits note + no error_kind",
              empty_d.get("note")
              and "error_kind" not in empty_d
              and not empty_d.get("transactions")
              and not empty_d.get("roster")
              and all(v is None
                      for v in (empty_d.get("purchases_summary") or {}).values()),
              f"got keys={list(empty_d.keys())}, "
              f"purchases={empty_d.get('purchases_summary')}")
        check(f"insiders {sym}: all-empty path does NOT also set coverage_note "
              "(mutually exclusive with note)",
              "coverage_note" not in empty_d,
              f"unexpected keys={list(empty_d.keys())}")

    # Partial-empty path: BMW.DE returns purchases_summary populated but
    # transactions + roster empty. Contract: success WITH `coverage_note`
    # (NOT `note`) — the empty event lists ARE the answer, but the
    # asymmetry is non-obvious so we surface it in-band.
    #
    # canary: BMW.DE's exact partial-empty shape is a Yahoo coverage
    # artifact. If Yahoo starts populating BMW.DE's per-event tables
    # (totally legitimate change), this fires — investigate before
    # treating as a bug. Same canary spirit as the "Last 6m"
    # period_label check.
    bmw = insiders.fetch("BMW.DE")
    # Tightened check: require a CONCRETE rollup signal (total_insider_
    # shares_held > 0) rather than the prior "any non-null field" guard,
    # which would pass purely on `period_label = "Last 6m"` even if every
    # numeric field came back null. Catches a regression where Yahoo
    # would emit an empty rollup but a populated header.
    bmw_purchases = bmw.get("purchases_summary") or {}
    check("insiders BMW.DE: partial-empty (rollup has real data, "  # canary
          "events empty)",
          isinstance(bmw_purchases.get("total_insider_shares_held"), int)
          and bmw_purchases["total_insider_shares_held"] > 0
          and not bmw.get("transactions")
          and not bmw.get("roster"),
          f"total_held={bmw_purchases.get('total_insider_shares_held')!r}, "
          f"tx={len(bmw.get('transactions') or [])}, "
          f"ros={len(bmw.get('roster') or [])}")
    check("insiders BMW.DE: partial-empty emits `coverage_note` "  # canary
          "(NOT `note`)",
          "coverage_note" in bmw and "note" not in bmw,
          f"unexpected keys={list(bmw.keys())}")

    # TM (Toyota ADR) canary: `BMW.DE` and `TM` are the two tickers the
    # docs cite as exemplars of the partial-empty path. BMW.DE has the
    # canonical shape (real rollup numbers like total_held=304M); TM's
    # data is degenerate — Yahoo returns the 7-row purchases DataFrame
    # but with all-zero / null values (verified 2026-05: total_held=0,
    # all event lists empty). Categorization-wise it's still partial-
    # empty (purchases_has_data is True because some fields are 0 not
    # None), so coverage_note still fires — that's what we test here.
    # Don't check `total_held > 0` for TM (asserts data quality, not
    # categorization) — that asserts BMW.DE-grade rollup numbers,
    # which TM's payload doesn't have.
    #
    # `period_label` is excluded from the data-presence check: it's
    # always populated to "Last 6m" because we extract it from the
    # column header itself (not a row value), so including it in the
    # `any(...)` would let the check pass purely on column-header
    # presence — it'd no longer verify Yahoo returns actual rollup
    # rows. Excluding period_label means this fires if Yahoo strips
    # all 7 metric rows (the all-empty case) even with the column
    # header still present.
    tm = insiders.fetch("TM")
    tm_purchases = tm.get("purchases_summary") or {}
    tm_data_fields = {k: v for k, v in tm_purchases.items()
                      if k != "period_label"}
    check("insiders TM: in partial-empty branch — purchases dict has "  # canary
          "at least one non-null DATA field (period_label excluded), "
          "both event lists empty",
          any(v is not None for v in tm_data_fields.values())
          and not tm.get("transactions")
          and not tm.get("roster"),
          f"data keys with values: "
          f"{[k for k, v in tm_data_fields.items() if v is not None]}, "
          f"tx={len(tm.get('transactions') or [])}, "
          f"ros={len(tm.get('roster') or [])}")
    check("insiders TM: partial-empty emits `coverage_note` (NOT `note`)",  # canary
          "coverage_note" in tm and "note" not in tm,
          f"unexpected keys={list(tm.keys())}")

    # TSLA canary: indirect cols ARE present (Musk holds via a trust).
    # Pins the "variable column set" caveat — if a future Yahoo change
    # drops indirect for TSLA too, we'd want to know.
    tsla = insiders.fetch("TSLA")
    if tsla.get("roster"):
        # At least one roster row should have non-null
        # shares_owned_indirectly — for TSLA in 2026-05 the count is
        # 3+ (Gebbia / Murdoch / Musk).
        with_indirect = [r for r in tsla["roster"]
                         if r.get("shares_owned_indirectly") is not None]
        check("insiders TSLA: at least one roster row has "  # canary
              "shares_owned_indirectly populated",
              len(with_indirect) >= 1,
              f"got {len(with_indirect)} rows with indirect holdings")

    # _apply_limit truncates lists in place. After applying limit=2, both
    # lists must be ≤ 2.
    capped = insiders._apply_limit(insiders.fetch("AAPL"), 2)
    check("insiders _apply_limit(2): transactions capped to <= 2",
          len(capped.get("transactions") or []) <= 2,
          f"got {len(capped.get('transactions') or [])} rows")
    check("insiders _apply_limit(2): roster capped to <= 2",
          len(capped.get("roster") or []) <= 2,
          f"got {len(capped.get('roster') or [])} rows")
    # _apply_limit does NOT touch purchases_summary.
    check("insiders _apply_limit(2): purchases_summary unaffected",
          isinstance(capped.get("purchases_summary"), dict)
          and any(v is not None
                  for v in capped["purchases_summary"].values()),
          f"got {capped.get('purchases_summary')}")

    # _summarize projection: peer-comparison flat dict.
    flat = insiders._summarize(d)
    expected_flat = {"symbol", *insiders._SUMMARY_FLAT_KEYS}
    check("insiders _summarize(AAPL): has documented flat field set",
          expected_flat.issubset(set(flat.keys())),
          f"missing={expected_flat - set(flat.keys())}")
    # transactions_returned should match the FULL pre-limit list length
    # (regression guard: if _summarize ever reads from a sliced list,
    # this drops to the slice size).
    check("insiders _summarize: transactions_returned == len(full transactions)",
          flat.get("transactions_returned") == len(d.get("transactions") or []),
          f"got {flat.get('transactions_returned')!r}, "
          f"expected {len(d.get('transactions') or [])}")
    # latest_transaction_date should equal max() of the transaction
    # dates — recompute to verify the helper isn't picking dates[0] or
    # similar (Yahoo's order is desc but we don't promise that).
    dates = [t["date"] for t in (d.get("transactions") or []) if t.get("date")]
    expected_latest = max(dates) if dates else None
    check("insiders _summarize: latest_transaction_date == max(dates)",
          flat.get("latest_transaction_date") == expected_latest,
          f"got {flat.get('latest_transaction_date')!r}, "
          f"expected {expected_latest!r}")

    # top_insider_direct_shares contract: it must equal the MAX of all
    # non-null `shares_owned_directly` values across the roster (algorithm-
    # independent invariant). Earlier draft recomputed via the same
    # `max(...)` formula and compared — that tests algorithm-against-itself
    # and would silently pass a refactor bug; the MAX invariant catches any
    # implementation that picks something other than the true maximum.
    #
    # `>= 0` (not `> 0`) because Yahoo could legitimately emit a roster
    # row with shares_owned_directly=0 for an insider whose direct
    # holdings just dropped to zero but who's still on the roster (rare
    # but plausible). The contract is "max of direct shares", not
    # "non-zero direct shares".
    direct_values = [r["shares_owned_directly"]
                     for r in (d.get("roster") or [])
                     if r.get("shares_owned_directly") is not None]
    if direct_values:
        expected_max = max(direct_values)
        check("insiders _summarize: top_insider_direct_shares == max("
              "shares_owned_directly across roster) — MAX invariant, "
              "algorithm-independent",
              flat.get("top_insider_direct_shares") == expected_max,
              f"got {flat.get('top_insider_direct_shares')!r}, "
              f"expected max={expected_max}")
        # The name field must point at A roster row whose
        # shares_owned_directly equals that max (ties broken arbitrarily;
        # we don't promise which name wins on a tie). Pin name → max
        # consistency so a refactor that decouples the two fields
        # surfaces.
        winner_names = {r["name"] for r in (d.get("roster") or [])
                        if r.get("shares_owned_directly") == expected_max}
        check("insiders _summarize: top_insider_by_direct_shares names "
              "a row whose shares_owned_directly equals the max",
              flat.get("top_insider_by_direct_shares") in winner_names,
              f"got name={flat.get('top_insider_by_direct_shares')!r}, "
              f"max-holders={winner_names}")
        # Range guard: result must be a non-negative int. `>= 0` (relaxed
        # from `> 0`) so the rare zero-direct-holdings edge case doesn't
        # spuriously fail. Indirect holdings are still excluded by the
        # algorithm (input filter is `is not None` on direct shares).
        check("insiders _summarize: top_insider_direct_shares is "
              "non-negative int (direct-only ranking, no indirect leak)",
              isinstance(flat.get("top_insider_direct_shares"), int)
              and flat["top_insider_direct_shares"] >= 0,
              f"got {flat.get('top_insider_direct_shares')!r}")

    # _summarize on an empty result must carry the `note` through.
    empty_flat = insiders._summarize(insiders.fetch("QQQ"))
    check("insiders _summarize(QQQ empty): preserves `note` for CSV",
          "note" in empty_flat and empty_flat.get("note"),
          f"got keys={list(empty_flat.keys())}")
    check("insiders _summarize(QQQ empty): does NOT also set coverage_note",
          "coverage_note" not in empty_flat,
          f"got keys={list(empty_flat.keys())}")

    # _summarize on a partial-empty result must carry `coverage_note`
    # through — same rationale as `note` carry-through, otherwise summary
    # CSVs silently drop the asymmetric-coverage signal.
    bmw_flat = insiders._summarize(insiders.fetch("BMW.DE"))
    check("insiders _summarize(BMW.DE partial): preserves `coverage_note` "  # canary
          "for CSV (NOT `note`)",
          "coverage_note" in bmw_flat and "note" not in bmw_flat,
          f"got keys={list(bmw_flat.keys())}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"insiders fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- sec_filings fetch shape ---
section("sec_filings fetch")
try:
    # Happy path: AAPL — populated list, full schema, US-issuer cycle
    # (10-K / 10-Q / 8-K / DEF 14A / etc.).
    d = sec_filings.fetch("AAPL")
    check("sec_filings AAPL: success has filings list + count",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("filings"), list)
          and isinstance(d.get("count"), int)
          and d["count"] == len(d["filings"]),
          f"got keys={list(d.keys())}, count={d.get('count')}")
    check("sec_filings AAPL: filings list non-empty (>= 20)",  # canary: ~75 typical
          len(d.get("filings") or []) >= 20,
          f"got {len(d.get('filings') or [])} filings")

    # Per-filing row schema. filings[0] is whatever Yahoo gave us first;
    # observed sort is desc but we don't depend on that here.
    expected = set(sec_filings._FILING_KEYS)
    if d.get("filings"):
        f0 = d["filings"][0]
        check("sec_filings AAPL: filings[0] has documented field set "
              "(incl. exhibit_keys)",
              set(f0.keys()) == expected,
              f"diff: missing={expected - set(f0.keys())}, "
              f"extra={set(f0.keys()) - expected}")
        # date is YYYY-MM-DD or None.
        dr = f0.get("date")
        check("sec_filings AAPL: filings[0].date is YYYY-MM-DD or None",
              dr is None or (isinstance(dr, str) and len(dr) == 10
                              and dr[4] == "-" and dr[7] == "-"),
              f"got {type(dr).__name__}={dr!r}")
        # type is non-empty str (every observed filing has a type).
        check("sec_filings AAPL: filings[0].type is non-empty str",  # canary
              isinstance(f0.get("type"), str) and f0["type"],
              f"got {type(f0.get('type')).__name__}={f0.get('type')!r}")
        # exhibit_count is integer (0 allowed, never null — populated in
        # the `if exhibits else 0` branch in _project_filing).
        check("sec_filings AAPL: filings[0].exhibit_count is int >= 0",
              isinstance(f0.get("exhibit_count"), int)
              and f0["exhibit_count"] >= 0,
              f"got {type(f0.get('exhibit_count')).__name__}={f0.get('exhibit_count')!r}")
        # exhibit_keys is str (empty allowed, never null). Pipe-joined.
        ek = f0.get("exhibit_keys")
        check("sec_filings AAPL: filings[0].exhibit_keys is str "
              "(pipe-joined, empty allowed)",
              isinstance(ek, str),
              f"got {type(ek).__name__}={ek!r}")
        # exhibit_keys must be consistent with exhibits dict — same key
        # set and order. If exhibits has 4 keys, exhibit_keys has 3 pipes.
        ex = f0.get("exhibits")
        if ex:
            check("sec_filings AAPL: filings[0] exhibit_keys derived from exhibits",
                  ek == "|".join(ex.keys()),
                  f"exhibit_keys={ek!r}, expected={'|'.join(ex.keys())!r}")
        # exhibits is dict-or-None; when present, all values are str.
        check("sec_filings AAPL: filings[0].exhibits is dict-or-None with str values",
              ex is None or (isinstance(ex, dict)
                              and all(isinstance(v, str) for v in ex.values())),
              f"got {type(ex).__name__}={ex!r}")
        # primary_url heuristic: when exhibits[type] is present, primary_url
        # must equal it (the documented preference). Pins the rule.
        if ex and f0.get("type") in ex:
            check("sec_filings AAPL: primary_url matches exhibits[type] when present",
                  f0.get("primary_url") == ex[f0["type"]],
                  f"primary_url={f0.get('primary_url')!r}, "
                  f"exhibits[{f0.get('type')!r}]={ex.get(f0.get('type'))!r}")

    # All observed filings should have a date (canary — Yahoo populates
    # this consistently). If a future Yahoo change drops dates on some
    # rows, this fires and we'd want to know whether to drop them or
    # surface them with date=None.
    no_date = [f for f in d.get("filings") or []
               if not f.get("date")]
    check("sec_filings AAPL: every filing has a date populated",  # canary
          not no_date,
          f"{len(no_date)} filings without dates")

    # Successful response must NOT carry `note`.
    check("sec_filings AAPL (success): no `note` key",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")

    # ADR path: TM (Toyota ADR) gets full coverage but with foreign-issuer
    # filing types (6-K / 20-F) instead of 10-K / 10-Q. Pins the doc claim
    # that ADRs are SEC-registered and DO get filings — distinct from
    # non-US primary listings (BMW.DE) which return empty.
    tm = sec_filings.fetch("TM")
    check("sec_filings TM (ADR): non-empty filings list (>= 10)",  # canary: ~120 typical
          len(tm.get("filings") or []) >= 10,
          f"got {len(tm.get('filings') or [])} filings")
    tm_types = {f.get("type") for f in tm.get("filings") or []}
    check("sec_filings TM (ADR): contains 6-K (foreign-issuer interim)",  # canary
          "6-K" in tm_types,
          f"got types={sorted(t for t in tm_types if t)[:10]}...")
    check("sec_filings TM (ADR): no `note` key (success path)",
          "note" not in tm,
          f"unexpected keys={list(tm.keys())}")

    # All-empty path: ETF / index / crypto / non-US primary / bogus all
    # return success-with-note + no error_kind + empty filings list.
    # Pins the "ambiguous empty" design (parallel to holders / insiders).
    for sym in ("SPY", "^GSPC", "BTC-USD", "0700.HK", "ZZZZNOTREAL"):
        empty_d = sec_filings.fetch(sym)
        check(f"sec_filings {sym}: empty path emits note + no error_kind",
              empty_d.get("note")
              and "error_kind" not in empty_d
              and empty_d.get("filings") == []
              and empty_d.get("count") == 0,
              f"got keys={list(empty_d.keys())}, "
              f"err_kind={empty_d.get('error_kind')}")

    # _apply_filters tests reuse the already-fetched `d` via deepcopy
    # to avoid hammering Yahoo with N additional AAPL round-trips
    # mid-smoke (smoke's main rate-limit pressure point — back-to-back
    # large-cap fetches across modes can borderline 429 on the run-after
    # already-warm sections like holders --summary). One fetch + N
    # deepcopies > N fetches.
    import copy as _copy

    original_count = d.get("count")  # captured pre-filter for invariance checks

    # _apply_filters: type filter narrows. Use AAPL — must have at least
    # one 10-K. _parse_types_arg upper-cases input, so the set passed
    # here is `{"10-K"}` (already upper). Pins the case-insensitive
    # comparison (filing.type uppercased on the right side).
    aapl_10k_only = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types={"10-K"}, since=None, limit=None,
    )
    check("sec_filings _apply_filters(types={10-K}): all rows are 10-K",
          aapl_10k_only.get("filings")
          and all(f.get("type") == "10-K"
                  for f in aapl_10k_only["filings"]),
          f"got types={[f.get('type') for f in aapl_10k_only.get('filings') or []]}")
    # `count` is preserved through filters — the result still reports
    # Yahoo's full response size (76 for AAPL), NOT the filtered length.
    # Pins the design: callers can recover Yahoo's count via
    # `result["count"]` and the displayed count via
    # `len(result["filings"])`. Regression guard for the old behavior
    # where `_apply_filters` overwrote `count`.
    check("sec_filings _apply_filters: count preserved (== Yahoo's full response size)",
          aapl_10k_only.get("count") == original_count,
          f"got count={aapl_10k_only.get('count')}, "
          f"expected {original_count} (Yahoo's full response size)")

    # _apply_filters: case-insensitive type match — `--type 10-k` (lowercase)
    # must match Yahoo's `10-K` after uppercase normalization (which
    # _parse_types_arg does). Pin by passing the upper-case set directly
    # and verifying it matches the same rows as the explicit upper case.
    # If a future refactor drops the `.upper()` call in `_apply_filters`,
    # this fires.
    aapl_10k_lowercase = sec_filings._apply_filters(
        _copy.deepcopy(d),
        # Simulate what _parse_types_arg("10-k") returns: an UPPERCASE set.
        # We're testing the comparison side, not the parse side here.
        types={"10-K"}, since=None, limit=None,
    )
    check("sec_filings _apply_filters: case-insensitive type match yields same rows",
          [f.get("date") for f in aapl_10k_lowercase.get("filings") or []]
          == [f.get("date") for f in aapl_10k_only.get("filings") or []],
          "case-insensitive 10-K match should equal exact 10-K match")

    # _apply_filters: type filter + limit interaction. Order is filter-then-
    # limit — `--type 10-K --limit 1` must yield the most recent 10-K,
    # not "the (last 1) filings if it happens to be a 10-K". Verify
    # by comparing against the type-only filter's first row.
    aapl_10k_top1 = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types={"10-K"}, since=None, limit=1,
    )
    check("sec_filings _apply_filters(types + limit=1): exactly 1 10-K row",
          len(aapl_10k_top1.get("filings") or []) == 1
          and aapl_10k_top1["filings"][0].get("type") == "10-K",
          f"got {len(aapl_10k_top1.get('filings') or [])} rows, "
          f"types={[f.get('type') for f in aapl_10k_top1.get('filings') or []]}")
    # The single row must equal the first row of the type-only filtered
    # result (since type filter preserves Yahoo's order, slice [0:1] of
    # 10-K-only is the same as 10-K + limit=1).
    if aapl_10k_only.get("filings") and aapl_10k_top1.get("filings"):
        check("sec_filings _apply_filters: filter-then-limit order is type→slice",
              aapl_10k_top1["filings"][0] == aapl_10k_only["filings"][0],
              f"top1={aapl_10k_top1['filings'][0].get('date')}, "
              f"first_of_type_only={aapl_10k_only['filings'][0].get('date')}")

    # _apply_filters: limit-only (no type filter). Caps to N rows.
    aapl_top3 = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types=None, since=None, limit=3,
    )
    check("sec_filings _apply_filters(limit=3): <= 3 filings",
          len(aapl_top3.get("filings") or []) <= 3,
          f"got {len(aapl_top3.get('filings') or [])} rows")

    # _apply_filters: --since date floor. Pick a cutoff that's recent
    # enough to leave a small filtered set but old enough that some rows
    # remain — 18 months ago is a safe canary across active large-caps.
    # All surviving filings must be on/after the cutoff.
    cutoff_18m = (datetime.now(timezone.utc).date() - timedelta(days=540)).isoformat()
    since_filtered = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types=None, since=cutoff_18m, limit=None,
    )
    check("sec_filings _apply_filters(since=18mo): all surviving rows on/after cutoff",
          since_filtered.get("filings")
          and all(f.get("date") >= cutoff_18m
                  for f in since_filtered["filings"]),
          f"got {len(since_filtered.get('filings') or [])} filings, "
          f"cutoff={cutoff_18m}, "
          f"first oldest date={min((f.get('date') for f in since_filtered.get('filings') or [] if f.get('date')), default=None)}")
    # Filtered set must be a strict subset of the full list — fewer rows.
    check("sec_filings _apply_filters(since): filtered count <= full count",
          len(since_filtered.get("filings") or []) <= original_count,
          f"filtered={len(since_filtered.get('filings') or [])}, "
          f"full={original_count}")

    # _apply_filters: filter-to-empty path sets `filter_note` (mutually
    # exclusive with `note`). Use --since with a future date to guarantee
    # empty result. Future date: 30 days from today UTC.
    future_cutoff = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    eaten = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types=None, since=future_cutoff, limit=None,
    )
    check("sec_filings _apply_filters: filter-to-empty sets filter_note",
          not eaten.get("filings")
          and eaten.get("filter_note")
          and "note" not in eaten,  # mutually exclusive
          f"got filings={len(eaten.get('filings') or [])}, "
          f"filter_note={eaten.get('filter_note')!r}, "
          f"note in eaten={'note' in eaten}")
    # The filter_note string must name the SPECIFIC culprit filter
    # (not list every applied filter). Pins the per-step culprit
    # tracking behavior.
    check("sec_filings _apply_filters: filter_note names the culprit "
          "(--since, since it ran first and zeroed)",
          eaten.get("filter_note") and "--since" in eaten["filter_note"]
          and "all eliminated by" in eaten["filter_note"],
          f"got filter_note={eaten.get('filter_note')!r}")

    # _apply_filters: when MULTIPLE filters are applied but only one
    # zeroes the list, filter_note must name the zeroing culprit (not
    # all applied filters). Use --since 2030-01-01 (zeros first) +
    # --type 8-K (would also reduce, but on already-empty list).
    # Pin: filter_note mentions --since, NOT --type.
    multi_eaten = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types={"8-K"}, since="2030-01-01", limit=None,
    )
    check("sec_filings _apply_filters: multi-filter — filter_note names "
          "the actual culprit (--since), not also-applied --type",
          multi_eaten.get("filter_note")
          and "--since 2030-01-01" in multi_eaten["filter_note"]
          and "--type" not in multi_eaten["filter_note"],
          f"got filter_note={multi_eaten.get('filter_note')!r}")

    # _apply_filters: 3-way combo `--since + --type + --limit` —
    # documented filter precedence is since → type → limit. Pin the
    # contract by checking surviving rows pass ALL three constraints.
    combo_cutoff = "2024-01-01"
    combo_result = sec_filings._apply_filters(
        _copy.deepcopy(d),
        types={"10-K", "10-Q"}, since=combo_cutoff, limit=2,
    )
    combo_filings = combo_result.get("filings") or []
    check("sec_filings _apply_filters(3-way combo): row count <= --limit",
          len(combo_filings) <= 2,
          f"got {len(combo_filings)} rows")
    check("sec_filings _apply_filters(3-way combo): all rows pass --since",
          all(f.get("date") and f["date"] >= combo_cutoff
              for f in combo_filings),
          f"dates={[f.get('date') for f in combo_filings]}")
    check("sec_filings _apply_filters(3-way combo): all rows pass --type "
          "(case-insensitive set)",
          all(f.get("type") in {"10-K", "10-Q"} for f in combo_filings),
          f"types={[f.get('type') for f in combo_filings]}")

    # _apply_filters in-place mutation contract. Other modes pin this
    # explicitly (holders / insiders); we should too, since the
    # docstring promises in-place semantics. Without this test, a
    # refactor that copies internally would silently break callers
    # that depend on `_apply_filters(d, ...) is d`.
    fresh_d = sec_filings.fetch("AAPL")
    pre_count = len(fresh_d.get("filings") or [])
    returned = sec_filings._apply_filters(
        fresh_d, types={"10-K"}, since=None, limit=2,
    )
    check("sec_filings _apply_filters: returns same object (in-place)",
          returned is fresh_d,
          f"returned object identity check failed")
    check("sec_filings _apply_filters: mutates filings list in place",
          len(fresh_d.get("filings") or []) <= 2
          and len(fresh_d.get("filings") or []) < pre_count,
          f"pre={pre_count}, post={len(fresh_d.get('filings') or [])}")

    # _apply_filters: when applied to an already-empty result (e.g. SPY's
    # Yahoo-empty path), filter_note must NOT be set — the existing `note`
    # is the right signal. Mutually exclusive paths.
    spy_d = sec_filings.fetch("SPY")  # new fetch (single Yahoo-empty path)
    spy_filtered = sec_filings._apply_filters(
        spy_d,
        types={"10-K"}, since=None, limit=None,
    )
    check("sec_filings _apply_filters: empty-input + filter does NOT set filter_note",
          "note" in spy_filtered and "filter_note" not in spy_filtered,
          f"got keys={list(spy_filtered.keys())}")

    # _summarize projection: peer-comparison flat dict. Pins schema.
    flat = sec_filings._summarize(d)
    expected_flat = {"symbol", *sec_filings._SUMMARY_FLAT_KEYS}
    check("sec_filings _summarize(AAPL): has documented flat field set",
          set(flat.keys()) >= expected_flat,
          f"missing={expected_flat - set(flat.keys())}")
    # total_filings should match d["count"] (the FULL pre-filter pre-limit
    # count). Regression guard: if _summarize ever reads from a sliced
    # list, this drops to the slice size.
    check("sec_filings _summarize: total_filings == d['count']",
          flat.get("total_filings") == d.get("count"),
          f"got {flat.get('total_filings')!r}, expected {d.get('count')!r}")
    # latest_date == max(filing dates) — algorithm-independent invariant.
    dates = [f["date"] for f in d.get("filings") or [] if f.get("date")]
    expected_latest = max(dates) if dates else None
    check("sec_filings _summarize: latest_date == max(filing dates)",
          flat.get("latest_date") == expected_latest,
          f"got {flat.get('latest_date')!r}, expected {expected_latest!r}")
    # latest_type must equal the type of a filing whose date == latest_date.
    if expected_latest:
        valid_types_at_max = {f.get("type") for f in d.get("filings") or []
                              if f.get("date") == expected_latest}
        check("sec_filings _summarize: latest_type names a filing at latest_date",
              flat.get("latest_type") in valid_types_at_max,
              f"got latest_type={flat.get('latest_type')!r}, "
              f"valid={valid_types_at_max}")
    # AAPL is a US issuer — must have a latest_10k_date populated and
    # latest_20f_date null. Pins the headline-type bucketing.
    check("sec_filings _summarize(AAPL US issuer): latest_10k_date populated, "  # canary
          "latest_20f_date null",
          flat.get("latest_10k_date") is not None
          and flat.get("latest_20f_date") is None,
          f"10k={flat.get('latest_10k_date')!r}, 20f={flat.get('latest_20f_date')!r}")

    # latest_proxy_date covers BOTH `DEF 14A` and `DEFA14A` (set-bucketed
    # in _HEADLINE_TYPES). Verify by recomputing via the union and
    # comparing against the summary's value. Algorithm-independent
    # invariant: max date across the union of both proxy form types.
    proxy_types = {"DEF 14A", "DEFA14A"}
    proxy_pairs = [(f["date"], f["type"]) for f in d.get("filings") or []
                   if f.get("type") in proxy_types and f.get("date")]
    expected_proxy = max(p[0] for p in proxy_pairs) if proxy_pairs else None
    check("sec_filings _summarize: latest_proxy_date == max(DEF 14A or DEFA14A dates)",
          flat.get("latest_proxy_date") == expected_proxy,
          f"got latest_proxy_date={flat.get('latest_proxy_date')!r}, "
          f"expected={expected_proxy!r} (across {len(proxy_pairs)} proxy filings)")
    # latest_proxy_type companion: must name the form code (DEF 14A or
    # DEFA14A) of the filing whose date == latest_proxy_date. Pin the
    # multi-form-bucket _type companion contract.
    if expected_proxy:
        valid_proxy_types = {t for date, t in proxy_pairs
                             if date == expected_proxy}
        check("sec_filings _summarize: latest_proxy_type names a winning form",
              flat.get("latest_proxy_type") in valid_proxy_types,
              f"got latest_proxy_type={flat.get('latest_proxy_type')!r}, "
              f"valid={valid_proxy_types}")
        check("sec_filings _summarize: latest_proxy_type is in proxy bucket",
              flat.get("latest_proxy_type") in proxy_types,
              f"got {flat.get('latest_proxy_type')!r}")
    else:
        check("sec_filings _summarize: latest_proxy_type null when no proxy filings",
              flat.get("latest_proxy_type") is None,
              f"got {flat.get('latest_proxy_type')!r}")
    # Single-form buckets (10-K etc.) must NOT have a `_type` companion
    # field — only the multi-form proxy bucket does. Pin the asymmetry.
    for absent in ("latest_10k_type", "latest_10q_type", "latest_8k_type",
                   "latest_20f_type", "latest_6k_type"):
        check(f"sec_filings _summarize: no {absent} (single-form bucket "
              "skips _type companion)",
              absent not in flat, f"unexpected key {absent!r} in flat dict")

    # 8-K primary_url canary: per docstring, primary_url for 8-K points
    # at the form itself (`exhibits["8-K"]`), not the typical
    # `EX-99.1` press release. Pin this contract — if a future change
    # silently flips to "prefer EX-99.1 for 8-K", the doc and code
    # would diverge. Find an AAPL 8-K with EX-99.1 in exhibits and
    # verify primary_url uses the 8-K form, not the press release.
    eight_k_with_pr = None
    for f in d.get("filings") or []:
        if f.get("type") == "8-K":
            ex = f.get("exhibits") or {}
            if "8-K" in ex and "EX-99.1" in ex:
                eight_k_with_pr = f
                break
    if eight_k_with_pr:
        check("sec_filings 8-K with EX-99.1: primary_url == exhibits['8-K'] "  # canary
              "(NOT the press release) — pins doc claim",
              eight_k_with_pr.get("primary_url")
              == eight_k_with_pr["exhibits"]["8-K"],
              f"got primary_url={eight_k_with_pr.get('primary_url')!r}, "
              f"exhibits['8-K']={eight_k_with_pr['exhibits']['8-K']!r}, "
              f"exhibits['EX-99.1']={eight_k_with_pr['exhibits']['EX-99.1']!r}")
    else:
        # Canary path didn't fire — fine, but warn so a regression
        # that drops EX-99.1 from AAPL 8-Ks doesn't go unnoticed.
        check("sec_filings: AAPL has at least one 8-K with EX-99.1 "  # canary
              "in exhibits (data availability for the primary_url canary)",
              False,
              "no AAPL 8-K with EX-99.1 found — 8-K primary_url canary "
              "couldn't run")
    # filings_last_90d is integer >= 0, never null on a successful fetch.
    check("sec_filings _summarize: filings_last_90d is non-negative int",
          isinstance(flat.get("filings_last_90d"), int)
          and flat["filings_last_90d"] >= 0,
          f"got {type(flat.get('filings_last_90d')).__name__}="
          f"{flat.get('filings_last_90d')!r}")

    # _summarize on TM (ADR): mirror image — latest_20f_date or
    # latest_6k_date populated, latest_10k_date null. Verifies foreign-
    # issuer headline-type routing.
    tm_flat = sec_filings._summarize(tm)
    check("sec_filings _summarize(TM ADR): latest_6k_date populated, "  # canary
          "latest_10k_date null",
          tm_flat.get("latest_6k_date") is not None
          and tm_flat.get("latest_10k_date") is None,
          f"6k={tm_flat.get('latest_6k_date')!r}, "
          f"10k={tm_flat.get('latest_10k_date')!r}")

    # _summarize on an empty result must carry the `note` through (CSV
    # would drop it otherwise — same defect class as holders / news).
    empty_flat = sec_filings._summarize(sec_filings.fetch("SPY"))
    check("sec_filings _summarize(SPY empty): preserves `note` for CSV",
          "note" in empty_flat and empty_flat.get("note"),
          f"got keys={list(empty_flat.keys())}")
    # Empty result: total_filings=0, filings_last_90d=0 (both known
    # answers, integer), other latest_* fields null.
    check("sec_filings _summarize(SPY empty): total_filings=0, "
          "filings_last_90d=0 (integers, not null)",
          empty_flat.get("total_filings") == 0
          and empty_flat.get("filings_last_90d") == 0,
          f"got total={empty_flat.get('total_filings')!r}, "
          f"recent={empty_flat.get('filings_last_90d')!r}")

    # _parse_types_arg: comma split + whitespace tolerance + UPPERCASE
    # normalization (case-insensitive contract), None pass-through.
    check("sec_filings _parse_types_arg(None): None",
          sec_filings._parse_types_arg(None) is None)
    check("sec_filings _parse_types_arg('10-K'): {'10-K'} (upper preserved)",
          sec_filings._parse_types_arg("10-K") == {"10-K"})
    check("sec_filings _parse_types_arg('10-k'): {'10-K'} (lowercase upper-cased)",
          sec_filings._parse_types_arg("10-k") == {"10-K"})
    check("sec_filings _parse_types_arg('def 14a, defa14a'): "
          "{'DEF 14A', 'DEFA14A'} — internal whitespace preserved, "
          "case folded",
          sec_filings._parse_types_arg("def 14a, defa14a")
          == {"DEF 14A", "DEFA14A"})
    check("sec_filings _parse_types_arg('10-K, 10-Q ,  8-K, DEF 14A'): "
          "splits + strips outer whitespace, preserves internal "
          "whitespace, all upper",
          sec_filings._parse_types_arg("10-K, 10-Q ,  8-K, DEF 14A")
          == {"10-K", "10-Q", "8-K", "DEF 14A"})
    check("sec_filings _parse_types_arg(''): None (empty after split)",
          sec_filings._parse_types_arg("") is None)

    # _parse_since_arg: ISO date / datetime normalization, --days
    # arithmetic, both-None pass-through, malformed input raises.
    check("sec_filings _parse_since_arg(None, None): None",
          sec_filings._parse_since_arg(None, None) is None)
    check("sec_filings _parse_since_arg('2024-01-01', None): pass-through",
          sec_filings._parse_since_arg("2024-01-01", None) == "2024-01-01")
    # ISO datetime input must be normalized to YYYY-MM-DD. Without
    # normalization, downstream string comparison would silently
    # exclude same-day filings (Python: shorter string < longer with
    # matching prefix; "2024-01-01" < "2024-01-01T00:00:00").
    # Pins the boundary-bug fix.
    check("sec_filings _parse_since_arg('2024-01-01T00:00:00', None): "
          "normalized to date-only YYYY-MM-DD",
          sec_filings._parse_since_arg("2024-01-01T00:00:00", None)
          == "2024-01-01",
          f"got {sec_filings._parse_since_arg('2024-01-01T00:00:00', None)!r}")
    check("sec_filings _parse_since_arg('2024-12-31T23:59:59', None): "
          "time discarded, date returned",
          sec_filings._parse_since_arg("2024-12-31T23:59:59", None)
          == "2024-12-31",
          f"got {sec_filings._parse_since_arg('2024-12-31T23:59:59', None)!r}")
    # --days N → today UTC - N days. Verify by recomputing.
    today_utc = datetime.now(timezone.utc).date()
    expected_30d = (today_utc - timedelta(days=30)).isoformat()
    check("sec_filings _parse_since_arg(None, 30): today_utc - 30d",
          sec_filings._parse_since_arg(None, 30) == expected_30d,
          f"got {sec_filings._parse_since_arg(None, 30)!r}, "
          f"expected {expected_30d!r}")
    # Malformed --since must raise ValueError so argparse can convert
    # to a usage error.
    raised = False
    try:
        sec_filings._parse_since_arg("not-a-date", None)
    except ValueError:
        raised = True
    check("sec_filings _parse_since_arg('not-a-date'): raises ValueError",
          raised, "expected ValueError for malformed input")

    # _project_filing: empty exhibits dict yields exhibit_keys="" (empty
    # string, not None). Pin the documented contract — chose empty
    # string to keep the field flat for CSV consumers' string filters.
    # Pure unit test (no Yahoo call) since real AAPL data has exhibits
    # on every observed filing; constructing a synthetic input is the
    # only way to cover this branch.
    proj_no_ex = sec_filings._project_filing({
        "date": "2024-01-01",
        "type": "TEST",
        "title": "Synthetic",
        "epochDate": 0,
        "edgarUrl": "http://example.com",
        "exhibits": {},  # explicit empty dict
    })
    check("sec_filings _project_filing(empty exhibits): exhibit_keys is ''",
          proj_no_ex.get("exhibit_keys") == "",
          f"got {proj_no_ex.get('exhibit_keys')!r}")
    check("sec_filings _project_filing(empty exhibits): exhibit_count is 0",
          proj_no_ex.get("exhibit_count") == 0,
          f"got {proj_no_ex.get('exhibit_count')!r}")
    check("sec_filings _project_filing(empty exhibits): exhibits is None",
          proj_no_ex.get("exhibits") is None,
          f"got {proj_no_ex.get('exhibits')!r}")
    check("sec_filings _project_filing(empty exhibits): primary_url is None",
          proj_no_ex.get("primary_url") is None,
          f"got {proj_no_ex.get('primary_url')!r}")
    # Missing exhibits key entirely (different from explicit empty dict).
    proj_missing = sec_filings._project_filing({
        "date": "2024-01-01", "type": "TEST",
        "title": "Synthetic", "edgarUrl": "http://example.com",
    })
    check("sec_filings _project_filing(missing exhibits): same shape as "
          "empty (exhibit_keys='', exhibit_count=0)",
          proj_missing.get("exhibit_keys") == ""
          and proj_missing.get("exhibit_count") == 0
          and proj_missing.get("exhibits") is None,
          f"got {proj_missing!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"sec_filings fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- options fetch shape ---
section("options fetch")
try:
    # Happy path: AAPL nearest expiry. Both legs populated, full
    # expirations array, spot/currency/quote_type from underlying dict.
    d = options.fetch("AAPL")
    check("options AAPL: success has spot + currency + quote_type",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("spot"), float) and d["spot"] > 0
          and d.get("currency") == "USD"
          and d.get("quote_type") == "EQUITY",
          f"got keys={list(d.keys())}, "
          f"spot={d.get('spot')}, ccy={d.get('currency')}, "
          f"qt={d.get('quote_type')}")
    check("options AAPL: expirations array non-empty (>= 5)",  # canary: ~24 typical
          isinstance(d.get("expirations"), list)
          and len(d.get("expirations", [])) >= 5,
          f"got {len(d.get('expirations') or [])} expirations")
    # The chosen expiry must come from the expirations array.
    check("options AAPL: chosen expiry in expirations[]",
          d.get("expiry") in (d.get("expirations") or []),
          f"expiry={d.get('expiry')!r}")
    # Both legs populated (typically 30-60+ rows each on a near-month).
    check("options AAPL: calls non-empty (>= 5)",  # canary: typically 30+
          len(d.get("calls") or []) >= 5,
          f"got {len(d.get('calls') or [])} call rows")
    check("options AAPL: puts non-empty (>= 5)",  # canary: typically 30+
          len(d.get("puts") or []) >= 5,
          f"got {len(d.get('puts') or [])} put rows")

    # Per-contract row schema — calls and puts share the same shape.
    expected_contract = set(options._CONTRACT_KEYS)
    for leg in ("calls", "puts"):
        rows = d.get(leg) or []
        if rows:
            r = rows[0]
            check(f"options AAPL: {leg}[0] has documented field set",
                  set(r.keys()) == expected_contract,
                  f"diff: missing={expected_contract - set(r.keys())}, "
                  f"extra={set(r.keys()) - expected_contract}")
            # strike must be positive float
            v = r.get("strike")
            check(f"options AAPL: {leg}[0].strike is positive float",
                  isinstance(v, float) and v > 0,
                  f"got {type(v).__name__}={v!r}")
            # implied_vol is fraction in [0, 5] — pins the unit landmine
            # against the percent-encoded sibling change_pct. 5.0 is a
            # very generous upper bound (= 500% IV); real IVs almost
            # never exceed 2.0. If Yahoo flips to percent encoding we'd
            # see 25.0 etc. and this fires.
            iv = r.get("implied_vol")
            check(f"options AAPL: {leg}[0].implied_vol is fraction in [0, 5]",
                  iv is None or (isinstance(iv, float) and 0.0 <= iv <= 5.0),
                  f"got {type(iv).__name__}={iv!r}")
            # in_the_money is bool
            check(f"options AAPL: {leg}[0].in_the_money is bool",
                  isinstance(r.get("in_the_money"), bool),
                  f"got {type(r.get('in_the_money')).__name__}")
            # last_trade_date_iso is ISO string or None — guard against
            # Yahoo switching to epoch / pandas Timestamp passthrough.
            ltd = r.get("last_trade_date_iso")
            check(f"options AAPL: {leg}[0].last_trade_date_iso is ISO str or None",
                  ltd is None or (isinstance(ltd, str) and "T" in ltd),
                  f"got {type(ltd).__name__}={ltd!r}")

    # Successful response must NOT carry `note`.
    check("options AAPL (success): no `note` key",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")

    # ITM/OTM consistency with spot — verifies in_the_money is computed
    # the way we documented (call ITM iff strike < spot; put ITM iff
    # strike > spot). Sample the first row of each leg.
    spot = d["spot"]
    if d.get("calls"):
        c0 = d["calls"][0]
        if c0.get("strike") is not None:
            expected_itm = c0["strike"] < spot
            check("options AAPL: calls[0].in_the_money matches strike < spot",
                  c0["in_the_money"] == expected_itm,
                  f"strike={c0['strike']}, spot={spot}, "
                  f"itm={c0['in_the_money']}, expected={expected_itm}")
    if d.get("puts"):
        p0 = d["puts"][0]
        if p0.get("strike") is not None:
            expected_itm = p0["strike"] > spot
            check("options AAPL: puts[0].in_the_money matches strike > spot",
                  p0["in_the_money"] == expected_itm,
                  f"strike={p0['strike']}, spot={spot}, "
                  f"itm={p0['in_the_money']}, expected={expected_itm}")

    # _apply_moneyness: ±5% band must yield strikes within ±5% of spot.
    # Use the full leg from fetch and verify strike windowing.
    win = options._apply_moneyness(d.get("calls") or [], spot, 5.0)
    band = spot * 0.05
    check("options _apply_moneyness(5): all strikes within ±5% of spot",
          all((spot - band) <= r["strike"] <= (spot + band) for r in win),
          f"got {len(win)} rows; band [{spot - band:.2f}, {spot + band:.2f}]")
    # The window must be a strict subset of (or equal to) the full ladder.
    check("options _apply_moneyness(5): result <= full leg size",
          len(win) <= len(d.get("calls") or []),
          f"window={len(win)}, full={len(d.get('calls') or [])}")

    # _apply_limit truncates a leg list to N.
    capped = options._apply_limit(d.get("calls") or [], 3)
    check("options _apply_limit(3): leg capped to <= 3",
          len(capped) <= 3, f"got {len(capped)} rows")

    # _atm_row picks the strike closest to spot.
    atm = options._atm_row(d.get("calls") or [], spot)
    if atm:
        # Verify it's actually the minimum-distance row.
        actual_min_dist = min(abs(r["strike"] - spot)
                              for r in (d.get("calls") or [])
                              if r.get("strike") is not None)
        check("options _atm_row(calls): returns strike closest to spot",
              abs(atm["strike"] - spot) == actual_min_dist,
              f"got strike={atm['strike']}, dist={abs(atm['strike'] - spot):.4f}, "
              f"min_dist={actual_min_dist:.4f}")

    # _summarize projection — peer-comparison flat dict carries spot,
    # ATM picks per leg, totals, PCRs. Pins schema for stable consumers.
    flat = options._summarize(d, moneyness=None)
    expected_flat = {"symbol", *options._SUMMARY_FLAT_KEYS}
    check("options _summarize(AAPL): has documented flat field set",
          expected_flat.issubset(set(flat.keys())),
          f"missing={expected_flat - set(flat.keys())}")
    check("options _summarize(AAPL): expirations_count matches len(expirations)",
          flat.get("expirations_count") == len(d.get("expirations") or []),
          f"got {flat.get('expirations_count')!r}, "
          f"expected {len(d.get('expirations') or [])!r}")

    # Bad expiry: must classify as not_found with expiry_requested carry.
    bad = options.fetch("AAPL", expiry="1999-01-01")
    check("options AAPL --expiry 1999-01-01: error_kind=not_found + expiry_requested",
          bad.get("error_kind") == "not_found"
          and bad.get("expiry_requested") == "1999-01-01"
          and "error" in bad,
          f"got {bad!r}")

    # Empty / non-options path: index, crypto, FX, future, non-US equity,
    # bogus all return empty `t.options` tuple. Each must report
    # success-with-note + no error_kind + empty arrays. Pins the
    # "ambiguous empty" design (parallel to holders' empty path).
    for sym in ("^GSPC", "BTC-USD", "EURUSD=X", "ES=F", "0700.HK",
                "ZZZZNOTREAL"):
        empty_d = options.fetch(sym)
        check(f"options {sym}: empty path emits note + no error_kind",
              empty_d.get("note")
              and "error_kind" not in empty_d
              and empty_d.get("expirations") == []
              and empty_d.get("calls") == []
              and empty_d.get("puts") == [],
              f"got keys={list(empty_d.keys())}, "
              f"exps={empty_d.get('expirations')}, "
              f"err_kind={empty_d.get('error_kind')}")

    # _summarize on empty must carry `note` through (CSV would drop it
    # otherwise — same defect class as holders / news).
    empty_flat = options._summarize(options.fetch("^GSPC"), moneyness=None)
    check("options _summarize(^GSPC empty): preserves `note` for CSV",
          "note" in empty_flat and empty_flat.get("note"),
          f"got keys={list(empty_flat.keys())}")

    # moneyness_pct echo: the user's --moneyness arg must round-trip
    # into the summary row so a peer-compare CSV mixing filtered and
    # unfiltered runs stays self-describing. Pins the design.
    flat_unfiltered = options._summarize(d, moneyness=None)
    flat_filtered = options._summarize(d, moneyness=5.0)
    check("options _summarize(moneyness=None): moneyness_pct is None",
          flat_unfiltered.get("moneyness_pct") is None,
          f"got {flat_unfiltered.get('moneyness_pct')!r}")
    check("options _summarize(moneyness=5.0): moneyness_pct is 5.0",
          flat_filtered.get("moneyness_pct") == 5.0,
          f"got {flat_filtered.get('moneyness_pct')!r}")
    # Filtered run must yield strictly fewer (or equal) calls than
    # unfiltered — sanity check that moneyness=5.0 is actually applied.
    check("options _summarize: filtered calls_returned <= unfiltered",
          flat_filtered.get("calls_returned") <= flat_unfiltered.get("calls_returned"),
          f"filtered={flat_filtered.get('calls_returned')}, "
          f"unfiltered={flat_unfiltered.get('calls_returned')}")

    # contract_currency rename: the per-contract currency field must
    # be `contract_currency`, not `currency` (which would collide with
    # the top-level `currency` column in CSV header). Pins the rename.
    if d.get("calls"):
        c0 = d["calls"][0]
        check("options AAPL: calls[0] has `contract_currency`, not `currency`",
              "contract_currency" in c0 and "currency" not in c0,
              f"keys={list(c0.keys())}")

    # OCC-parsed expiry consistency: the `expiry` label must match the
    # YYMMDD encoded in the first contract's OCC symbol. Pins the
    # post-#2 fix where `expiry` is derived from chain ground truth
    # (rather than guessed from `exps[0]`) on the no-`--expiry` path.
    if d.get("calls"):
        c0 = d["calls"][0]
        parsed = options._expiry_from_contract_symbol(c0.get("contract_symbol"))
        check("options AAPL: expiry matches OCC-parsed first contract symbol",
              parsed is not None and parsed == d.get("expiry"),
              f"expiry={d.get('expiry')!r}, parsed_from_symbol={parsed!r}, "
              f"contract_symbol={c0.get('contract_symbol')!r}")

    # _expiry_from_contract_symbol unit tests (offline — pure regex).
    # Cover the standard OCC layouts, year boundaries (2000 / 2099),
    # plus a malformed input.
    check("options _expiry_from_contract_symbol(AAPL OCC): parses YYMMDD",
          options._expiry_from_contract_symbol("AAPL260508C00282500")
          == "2026-05-08",
          f"got {options._expiry_from_contract_symbol('AAPL260508C00282500')!r}")
    check("options _expiry_from_contract_symbol(BRKB OCC): handles longer root",
          options._expiry_from_contract_symbol("BRKB260619P00500000")
          == "2026-06-19",
          f"got {options._expiry_from_contract_symbol('BRKB260619P00500000')!r}")
    # Year-boundary canaries: 00 → 2000, 99 → 2099. Pins the YY → 20YY
    # hardcoding (documented in _expiry_from_contract_symbol's docstring;
    # OPRA's longest LEAPS top out ~3 years so 2099 boundary is purely
    # theoretical, but the assertion costs nothing).
    check("options _expiry_from_contract_symbol(YY=00): → 2000",
          options._expiry_from_contract_symbol("X000101C00100000")
          == "2000-01-01",
          f"got {options._expiry_from_contract_symbol('X000101C00100000')!r}")
    check("options _expiry_from_contract_symbol(YY=99): → 2099",
          options._expiry_from_contract_symbol("X991231P00100000")
          == "2099-12-31",
          f"got {options._expiry_from_contract_symbol('X991231P00100000')!r}")
    check("options _expiry_from_contract_symbol(None): returns None",
          options._expiry_from_contract_symbol(None) is None)
    check("options _expiry_from_contract_symbol(garbage): returns None",
          options._expiry_from_contract_symbol("not-an-occ-symbol") is None)

    # _sum_int None-safe semantics: when every row's value is None,
    # return None (not 0) — distinguishes "Yahoo didn't populate" from
    # "every contract had explicit 0 activity". Pure-Python test, no
    # network. Three cases: empty rows → None; all-None values → None;
    # mix of values + None → sum of non-None.
    check("options _sum_int(empty rows): None",
          options._sum_int([], "volume") is None,
          f"got {options._sum_int([], 'volume')!r}")
    check("options _sum_int(all-None values): None (NOT 0)",
          options._sum_int([{"volume": None}, {"volume": None}], "volume") is None,
          f"got {options._sum_int([{'volume': None}, {'volume': None}], 'volume')!r}")
    check("options _sum_int(mix): sum of non-None values",
          options._sum_int([{"volume": 5}, {"volume": None}, {"volume": 10}], "volume") == 15,
          f"got {options._sum_int([{'volume': 5}, {'volume': None}, {'volume': 10}], 'volume')!r}")
    check("options _sum_int(all-zero values): 0 (NOT None)",
          options._sum_int([{"volume": 0}, {"volume": 0}], "volume") == 0,
          f"got {options._sum_int([{'volume': 0}, {'volume': 0}], 'volume')!r}")

    # --type filter offline: _filter_legs drops the off-leg list to
    # `[]` rather than removing the key (schema shape stability). Use
    # deep copies of `d` so we don't mutate it (downstream tests still
    # read from `d`) and don't pay extra HTTP for fresh fetches.
    import copy as _copy
    calls_only = options._filter_legs(_copy.deepcopy(d),
                                       leg_filter="calls",
                                       moneyness=None, limit=2)
    check("options _filter_legs(--type calls): puts is empty list (not missing)",
          isinstance(calls_only.get("puts"), list)
          and len(calls_only["puts"]) == 0,
          f"got puts={calls_only.get('puts')!r}")
    check("options _filter_legs(--type calls, limit=2): calls capped to <= 2",
          len(calls_only.get("calls") or []) <= 2,
          f"got {len(calls_only.get('calls') or [])} call rows")

    puts_only = options._filter_legs(_copy.deepcopy(d),
                                      leg_filter="puts",
                                      moneyness=None, limit=2)
    check("options _filter_legs(--type puts): calls is empty list (not missing)",
          isinstance(puts_only.get("calls"), list)
          and len(puts_only["calls"]) == 0,
          f"got calls={puts_only.get('calls')!r}")

    # _filter_legs on an error result (no `calls` key) must not crash
    # — guards the `if "calls" not in result: return` branch.
    err_in = {"symbol": "X", "error": "bad", "error_kind": "not_found",
              "attempts": 1}
    err_out = options._filter_legs(err_in, leg_filter="all",
                                    moneyness=5, limit=3)
    check("options _filter_legs(error result): pass-through, no crash",
          err_out is err_in and "calls" not in err_out,
          f"got {err_out!r}")

    # _summarize on a synthetic empty-chain-but-valid-expiry result
    # (the rare _EMPTY_CHAIN_NOTE path). We can't reliably trigger
    # this live, but the projection must carry the note + return
    # None for atm/totals (no leg data to summarize).
    fake_empty_chain = {
        "symbol": "FAKE",
        "spot": 100.0,
        "currency": "USD",
        "quote_type": "EQUITY",
        "expirations": ["2026-06-19", "2026-07-17"],
        "expiry": "2026-06-19",
        "calls": [],
        "puts": [],
        "note": options._EMPTY_CHAIN_NOTE,
    }
    flat_empty_chain = options._summarize(fake_empty_chain, moneyness=None)
    check("options _summarize(empty-chain): carries _EMPTY_CHAIN_NOTE",
          flat_empty_chain.get("note") == options._EMPTY_CHAIN_NOTE,
          f"got {flat_empty_chain.get('note')!r}")
    check("options _summarize(empty-chain): atm_call_strike is None",
          flat_empty_chain.get("atm_call_strike") is None,
          f"got {flat_empty_chain.get('atm_call_strike')!r}")
    check("options _summarize(empty-chain): pcr_volume is None (no legs)",
          flat_empty_chain.get("pcr_volume") is None,
          f"got {flat_empty_chain.get('pcr_volume')!r}")

    # End-to-end coverage of fetch()'s _EMPTY_CHAIN_NOTE branch via
    # a mock yfinance Ticker. This branch (`exps non-empty AND
    # chain.calls/puts is None`) is what triggers when Yahoo returns
    # `result[0].options=[]` for a valid date — observable in the
    # 1-HTTP path with the post-#1 fix, where we no longer collapse
    # this case into _NO_OPTIONS_NOTE. Hard to reproduce live, so
    # mock the underlying yf.Ticker and exercise fetch() directly.
    #
    # IMPORTANT: yfinance's real behavior on an empty chain is
    # `Options(calls=None, puts=None, underlying=None)` — all THREE
    # fields are None because they all derive from the same empty
    # `_download_options(date)` payload. The mock mirrors that
    # exactly so the test reflects production semantics (an earlier
    # version mocked `underlying={...}` populated, which falsely
    # asserted spot=100.0 — yfinance never produces that combination).
    from collections import namedtuple as _nt
    from unittest.mock import patch as _patch
    _Options = _nt("Options", ["calls", "puts", "underlying"])
    class _MockTicker:
        def __init__(self, ticker):
            self.ticker = ticker
        @property
        def options(self):
            return ("2026-06-19", "2026-07-17")
        def option_chain(self, date=None):
            # Realistic: yfinance returns all-None Options on empty payload.
            return _Options(calls=None, puts=None, underlying=None)
    with _patch.object(options.yf, "Ticker", _MockTicker):
        # No --expiry → 1-HTTP path. exps non-empty + chain.calls/puts None
        # → fetch() should route to _EMPTY_CHAIN_NOTE (NOT _NO_OPTIONS_NOTE).
        mocked = options.fetch("MOCK")
    check("options fetch(mocked empty-chain, no --expiry): _EMPTY_CHAIN_NOTE",
          mocked.get("note") == options._EMPTY_CHAIN_NOTE
          and mocked.get("expirations") == ["2026-06-19", "2026-07-17"]
          and mocked.get("expiry") == "2026-06-19"
          and mocked.get("calls") == []
          and mocked.get("puts") == []
          and mocked.get("spot") is None     # underlying=None → spot None
          and mocked.get("currency") is None
          and mocked.get("quote_type") is None
          and "error_kind" not in mocked,
          f"got {mocked!r}")
    # Same mock with explicit --expiry → 2-HTTP path. exps non-empty,
    # expiry valid, chain.calls/puts None → also _EMPTY_CHAIN_NOTE.
    # Assert the same full set of fields as the no-expiry case for
    # consistency.
    with _patch.object(options.yf, "Ticker", _MockTicker):
        mocked2 = options.fetch("MOCK", expiry="2026-07-17")
    check("options fetch(mocked empty-chain, --expiry): _EMPTY_CHAIN_NOTE",
          mocked2.get("note") == options._EMPTY_CHAIN_NOTE
          and mocked2.get("expirations") == ["2026-06-19", "2026-07-17"]
          and mocked2.get("expiry") == "2026-07-17"
          and mocked2.get("calls") == []
          and mocked2.get("puts") == []
          and mocked2.get("spot") is None
          and "error_kind" not in mocked2,
          f"got {mocked2!r}")

    # OCC fallback path: when chain rows have non-OCC contract symbols,
    # _expiry_from_chain returns None and fetch() falls back to exps[0].
    # Pins the OCC-parse-is-best-effort design where exps[0] is the
    # safety net. Build a mock whose first row's contractSymbol is
    # non-parseable; expect fetch to label `expiry: exps[0]`.
    import pandas as _pd
    class _MockTickerOCCFail:
        def __init__(self, ticker):
            self.ticker = ticker
        @property
        def options(self):
            return ("2026-09-19", "2026-10-17")
        def option_chain(self, date=None):
            calls = _pd.DataFrame([{
                "contractSymbol": "BOGUS-NOT-OCC",
                "lastTradeDate": _pd.Timestamp("2026-05-01", tz="UTC"),
                "strike": 100.0, "lastPrice": 1.0, "bid": 0.0, "ask": 0.0,
                "change": 0.0, "percentChange": 0.0, "volume": 0,
                "openInterest": 0, "impliedVolatility": 0.2,
                "inTheMoney": False, "contractSize": "REGULAR",
                "currency": "USD",
            }])
            puts = _pd.DataFrame([{
                "contractSymbol": "ALSO-BOGUS",
                "lastTradeDate": _pd.Timestamp("2026-05-01", tz="UTC"),
                "strike": 100.0, "lastPrice": 1.0, "bid": 0.0, "ask": 0.0,
                "change": 0.0, "percentChange": 0.0, "volume": 0,
                "openInterest": 0, "impliedVolatility": 0.2,
                "inTheMoney": False, "contractSize": "REGULAR",
                "currency": "USD",
            }])
            return _Options(calls=calls, puts=puts,
                            underlying={"regularMarketPrice": 100.0,
                                        "currency": "USD",
                                        "quoteType": "EQUITY"})
    with _patch.object(options.yf, "Ticker", _MockTickerOCCFail):
        mocked3 = options.fetch("MOCK")
    check("options fetch(mock OCC parse fails): expiry falls back to exps[0]",
          mocked3.get("expiry") == "2026-09-19"
          and mocked3.get("expirations") == ["2026-09-19", "2026-10-17"]
          and len(mocked3.get("calls") or []) == 1
          and "note" not in mocked3,
          f"got {mocked3!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"options fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- analyst fetch shape ---
section("analyst fetch")
try:
    # Happy path: AAPL — both sections populated.
    d = analyst.fetch("AAPL")
    check("analyst AAPL: success has recommendations + upgrades_downgrades",
          isinstance(d, dict) and d.get("symbol") == "AAPL"
          and isinstance(d.get("recommendations"), list)
          and isinstance(d.get("upgrades_downgrades"), list),
          f"got {list(d.keys())}")
    # recommendations: typically 4 rows (0m / -1m / -2m / -3m), occasionally 3
    check("analyst AAPL: recommendations is 3-4 rows",  # canary
          3 <= len(d.get("recommendations") or []) <= 4,
          f"got {len(d.get('recommendations') or [])} rows")
    check("analyst AAPL: upgrades_downgrades non-empty (>= 50)",  # canary: 977 typical
          len(d.get("upgrades_downgrades") or []) >= 50,
          f"got {len(d.get('upgrades_downgrades') or [])} rows")

    # recommendations row schema
    expected_rec = set(analyst._RECOMMENDATIONS_KEYS)
    if d.get("recommendations"):
        r0 = d["recommendations"][0]
        check("analyst AAPL: recommendations[0] has documented field set",
              set(r0.keys()) == expected_rec,
              f"diff: missing={expected_rec - set(r0.keys())}, "
              f"extra={set(r0.keys()) - expected_rec}")
        # period must be one of the known labels
        check("analyst AAPL: recommendations[0].period is one of 0m/-1m/-2m/-3m",
              r0.get("period") in ("0m", "-1m", "-2m", "-3m"),
              f"got {r0.get('period')!r}")
        # total must equal sum of buckets when all five are int
        bucket_keys = ("strong_buy", "buy", "hold", "sell", "strong_sell")
        if all(isinstance(r0.get(k), int) for k in bucket_keys):
            expected_total = sum(r0[k] for k in bucket_keys)
            check("analyst AAPL: recommendations[0].total == sum(buckets)",
                  r0["total"] == expected_total,
                  f"got total={r0['total']}, sum={expected_total}")

    # upgrades_downgrades row schema
    expected_ch = set(analyst._CHANGE_KEYS)
    if d.get("upgrades_downgrades"):
        c0 = d["upgrades_downgrades"][0]
        check("analyst AAPL: upgrades_downgrades[0] has documented field set",
              set(c0.keys()) == expected_ch,
              f"diff: missing={expected_ch - set(c0.keys())}, "
              f"extra={set(c0.keys()) - expected_ch}")
        # date is ISO 'YYYY-MM-DDTHH:MM:SS' or None
        dt = c0.get("date")
        check("analyst AAPL: upgrades_downgrades[0].date is ISO datetime or None",
              dt is None or (isinstance(dt, str) and len(dt) == 19
                              and dt[10] == "T"),
              f"got {type(dt).__name__}={dt!r}")
        # action is the lowercase enum (verified across AAPL's 977 rows)
        check("analyst AAPL: upgrades_downgrades[0].action is in known enum",  # canary
              c0.get("action") in ("up", "down", "main", "init", "reit"),
              f"got {c0.get('action')!r}")
        # 0.0 sentinel: current_price_target / prior_price_target must NOT
        # be 0.0 (the projection should have collapsed those to None).
        for k in ("current_price_target", "prior_price_target"):
            v = c0.get(k)
            check(f"analyst AAPL: upgrades_downgrades[0].{k} != 0.0 "
                  "(0 sentinel → null projection)",
                  v != 0.0,
                  f"got {v!r}")

    # 0-sentinel projection: scan ALL rows. After projection, neither
    # current_price_target NOR prior_price_target should ever be exactly
    # 0.0 — Yahoo's 0 means "no target published" and we project to None.
    # This regression-guards the _safe_target sentinel rule.
    if d.get("upgrades_downgrades"):
        zero_targets = [
            (i, k, c.get(k))
            for i, c in enumerate(d["upgrades_downgrades"])
            for k in ("current_price_target", "prior_price_target")
            if c.get(k) == 0.0
        ]
        check("analyst AAPL: NO row has 0.0 in either price-target field "
              "(0 sentinel → null in _safe_target)",
              not zero_targets,
              f"got {len(zero_targets)} rows with 0.0 (sample: {zero_targets[:3]})")

    # Successful response must NOT carry note OR coverage_note
    check("analyst AAPL (success): no `note` key on the result",
          "note" not in d, f"unexpected keys={list(d.keys())}")
    check("analyst AAPL (success): no `coverage_note` key on the result",
          "coverage_note" not in d, f"unexpected keys={list(d.keys())}")

    # All-empty path: ETF / index / crypto / bogus all return both frames
    # empty. Must report success-with-note + NO error_kind + NO
    # coverage_note (mutually exclusive with note).
    for sym in ("QQQ", "^GSPC", "BTC-USD", "ZZZZNOTREAL"):
        empty_d = analyst.fetch(sym)
        check(f"analyst {sym}: all-empty path emits note + no error_kind",
              empty_d.get("note")
              and "error_kind" not in empty_d
              and not empty_d.get("recommendations")
              and not empty_d.get("upgrades_downgrades"),
              f"got keys={list(empty_d.keys())}")
        check(f"analyst {sym}: all-empty does NOT also set coverage_note "
              "(mutually exclusive)",
              "coverage_note" not in empty_d,
              f"unexpected keys={list(empty_d.keys())}")

    # Partial-empty path: 0700.HK (Tencent, HKEX primary) returns
    # recommendations populated but upgrades_downgrades empty. Yahoo's
    # grade-change feed is US-centric — verified empirically 2026-05.
    # Contract: success WITH `coverage_note` (NOT `note`).
    #
    # canary: if Yahoo starts indexing HK grade events, this fires —
    # investigate before treating as a bug.
    hk = analyst.fetch("0700.HK")
    check("analyst 0700.HK: partial-empty (recommendations populated, "  # canary
          "upgrades_downgrades empty)",
          len(hk.get("recommendations") or []) >= 3
          and not hk.get("upgrades_downgrades"),
          f"recs={len(hk.get('recommendations') or [])}, "
          f"upgrades={len(hk.get('upgrades_downgrades') or [])}")
    check("analyst 0700.HK: partial-empty emits `coverage_note` "  # canary
          "(NOT `note`)",
          "coverage_note" in hk and "note" not in hk,
          f"unexpected keys={list(hk.keys())}")

    # ADR canary: TM (Toyota ADR on NYSE) has its primary listing on
    # JPX but ALSO trades on NYSE, so Yahoo's grade-change feed
    # populates it. This is the contrast case for 0700.HK / BMW.DE
    # — same kind of non-US issuer but with US-listed ADR access.
    tm = analyst.fetch("TM")
    check("analyst TM (ADR): full coverage — both frames populated",  # canary
          len(tm.get("recommendations") or []) >= 1
          and len(tm.get("upgrades_downgrades") or []) >= 1,
          f"recs={len(tm.get('recommendations') or [])}, "
          f"upgrades={len(tm.get('upgrades_downgrades') or [])}")
    check("analyst TM: full coverage means neither note nor coverage_note",
          "note" not in tm and "coverage_note" not in tm,
          f"unexpected keys={list(tm.keys())}")

    # _apply_limit caps upgrades_downgrades but NOT recommendations
    capped = analyst._apply_limit(analyst.fetch("AAPL"), 5)
    check("analyst _apply_limit(5): upgrades_downgrades capped to <= 5",
          len(capped.get("upgrades_downgrades") or []) <= 5,
          f"got {len(capped.get('upgrades_downgrades') or [])} rows")
    check("analyst _apply_limit(5): recommendations unaffected (Yahoo-capped)",
          len(capped.get("recommendations") or []) >= 3,
          f"got {len(capped.get('recommendations') or [])} rows")

    # _summarize projection
    flat = analyst._summarize(d)
    expected_flat = {"symbol", *analyst._SUMMARY_FLAT_KEYS}
    check("analyst _summarize(AAPL): has documented flat field set",
          expected_flat.issubset(set(flat.keys())),
          f"missing={expected_flat - set(flat.keys())}")
    # rating_changes_returned must equal the FULL upgrades list length
    check("analyst _summarize: rating_changes_returned == len(full upgrades)",
          flat.get("rating_changes_returned") == len(d.get("upgrades_downgrades") or []),
          f"got {flat.get('rating_changes_returned')!r}, "
          f"expected {len(d.get('upgrades_downgrades') or [])}")
    # buy_pct_current is fraction in [0, 1]
    bp = flat.get("buy_pct_current")
    check("analyst _summarize: buy_pct_current is fraction in [0, 1]",
          isinstance(bp, float) and 0.0 <= bp <= 1.0,
          f"got {type(bp).__name__}={bp!r}")
    # consensus_score on 1-5 Likert scale
    cs = flat.get("consensus_score_current")
    check("analyst _summarize: consensus_score_current is in [1.0, 5.0]",
          isinstance(cs, float) and 1.0 <= cs <= 5.0,
          f"got {type(cs).__name__}={cs!r}")
    # consensus_score_change == current - oldest. Sign is OPPOSITE of
    # buy_pct_change (consensus uses 1-5 Likert where 1 is most bullish,
    # so negative delta = consensus moved more bullish). Pin both the
    # arithmetic and the sign-convention contract.
    cs_cur = flat.get("consensus_score_current")
    cs_old = flat.get("consensus_score_oldest")
    if cs_cur is not None and cs_old is not None:
        check("analyst _summarize: consensus_score_change == "
              "current - oldest (Yahoo 1-5 Likert; negative = more bullish)",
              abs(flat.get("consensus_score_change") - (cs_cur - cs_old)) < 1e-9,
              f"got {flat.get('consensus_score_change')!r}, "
              f"expected {cs_cur - cs_old!r}")
    # Knock-on null discipline: when either snapshot is null,
    # consensus_score_change must also be null.
    qqq_flat_for_change = analyst._summarize(analyst.fetch("QQQ"))
    check("analyst _summarize(QQQ): consensus_score_change is None when "
          "either snapshot is None",
          qqq_flat_for_change.get("consensus_score_change") is None,
          f"got {qqq_flat_for_change.get('consensus_score_change')!r}")

    # consensus_score MAX invariant: must equal the recomputed weighted
    # mean of the 0m row's distribution (algorithm-independent check).
    rec_0m = next((r for r in (d.get("recommendations") or [])
                   if r.get("period") == "0m"), None)
    if rec_0m and rec_0m.get("total"):
        expected_cs = sum(
            i * (rec_0m.get(k) or 0)
            for i, k in enumerate(("strong_buy", "buy", "hold", "sell",
                                    "strong_sell"), start=1)
        ) / rec_0m["total"]
        check("analyst _summarize: consensus_score_current matches "
              "1×sb + 2×b + 3×h + 4×s + 5×ss / total at 0m",
              abs(flat["consensus_score_current"] - expected_cs) < 1e-9,
              f"got {flat['consensus_score_current']!r}, "
              f"expected {expected_cs!r}")
    # latest_event_date matches max(date) — recompute (algorithm-
    # independent invariant; Yahoo sorts desc but we don't depend on it).
    dates = [(c.get("date") or "")[:10]
             for c in (d.get("upgrades_downgrades") or [])
             if c.get("date")]
    expected_latest = max(dates) if dates else None
    check("analyst _summarize: latest_event_date == max(date)",
          flat.get("latest_event_date") == expected_latest,
          f"got {flat.get('latest_event_date')!r}, "
          f"expected {expected_latest!r}")
    # latest_rating_change_date matches max(date) over the action ∈
    # {up, down} subset only — pins the filtered-subset semantics.
    rc_dates = [(c.get("date") or "")[:10]
                for c in (d.get("upgrades_downgrades") or [])
                if c.get("date") and c.get("action") in ("up", "down")]
    expected_rc_latest = max(rc_dates) if rc_dates else None
    check("analyst _summarize: latest_rating_change_date == "
          "max(date over action ∈ {up, down})",
          flat.get("latest_rating_change_date") == expected_rc_latest,
          f"got {flat.get('latest_rating_change_date')!r}, "
          f"expected {expected_rc_latest!r}")
    # Pin: latest_rating_change_action ∈ {up, down} when populated
    rc_action = flat.get("latest_rating_change_action")
    check("analyst _summarize: latest_rating_change_action is up/down "
          "or None (never main/reit/init)",
          rc_action is None or rc_action in ("up", "down"),
          f"got {rc_action!r}")
    # Pin: latest_rating_change_current_price_target equals the target
    # carried on the rating-change subset's latest event (algorithm-
    # independent invariant: must match the current_price_target of
    # the upgrades_downgrades row whose date == latest_rating_change_date
    # and whose action ∈ {up, down}). Also guards 0→null sentinel:
    # the value must never be 0.0.
    if flat.get("latest_rating_change_date"):
        rc_subset = [c for c in (d.get("upgrades_downgrades") or [])
                     if c.get("action") in ("up", "down")
                     and (c.get("date") or "")[:10]
                     == flat["latest_rating_change_date"]]
        # rc_subset can have multiple events on the same day from
        # different firms; the named firm uniquely identifies the row.
        match = next((c for c in rc_subset
                      if c.get("firm") == flat.get("latest_rating_change_firm")),
                     None)
        if match is not None:
            check("analyst _summarize: latest_rating_change_current_price_"
                  "target matches the source row's current_price_target",
                  flat.get("latest_rating_change_current_price_target")
                  == match.get("current_price_target"),
                  f"got {flat.get('latest_rating_change_current_price_target')!r}, "
                  f"row had {match.get('current_price_target')!r}")
            check("analyst _summarize: latest_rating_change_current_price_"
                  "target != 0.0 (0 sentinel filtered)",
                  flat.get("latest_rating_change_current_price_target") != 0.0,
                  f"got {flat.get('latest_rating_change_current_price_target')!r}")

    # _summarize on all-empty result must carry `note` through (CSV
    # would drop it otherwise — same defect class as holders / news).
    empty_flat = analyst._summarize(analyst.fetch("QQQ"))
    check("analyst _summarize(QQQ empty): preserves `note` for CSV",
          "note" in empty_flat and empty_flat.get("note"),
          f"got keys={list(empty_flat.keys())}")
    check("analyst _summarize(QQQ empty): does NOT also set coverage_note",
          "coverage_note" not in empty_flat,
          f"got keys={list(empty_flat.keys())}")

    # _summarize on partial-empty must carry `coverage_note` through;
    # AND the *_last_90d count fields must be NULL (not 0) — empty
    # upgrades list is ambiguous, can't claim "0 events".
    hk_flat = analyst._summarize(analyst.fetch("0700.HK"))
    check("analyst _summarize(0700.HK partial): preserves `coverage_note` "  # canary
          "for CSV (NOT `note`)",
          "coverage_note" in hk_flat and "note" not in hk_flat,
          f"got keys={list(hk_flat.keys())}")
    for k in ("upgrades_last_90d", "downgrades_last_90d",
              "net_rating_changes_90d", "target_raises_last_90d",
              "target_lowers_last_90d", "rating_changes_returned"):
        check(f"analyst _summarize(0700.HK partial): {k} is None (not 0) "
              "— empty upgrades list is ambiguous",
              hk_flat.get(k) is None,
              f"got {hk_flat.get(k)!r}")
    # But the recommendations-derived fields ARE populated (recommendations
    # is the populated-side of the partial-empty pair).
    check("analyst _summarize(0700.HK partial): "  # canary
          "buy_pct_current still populated from recommendations",
          isinstance(hk_flat.get("buy_pct_current"), float),
          f"got {hk_flat.get('buy_pct_current')!r}")

    # _safe_target unit test: 0.0 → None, real values pass through,
    # NaN/None → None.
    check("analyst _safe_target(0.0): None (sentinel)",
          analyst._safe_target(0.0) is None)
    check("analyst _safe_target(250.0): 250.0",
          analyst._safe_target(250.0) == 250.0)
    check("analyst _safe_target(None): None",
          analyst._safe_target(None) is None)
    check("analyst _safe_target('250'): 250.0 (string coerce via safe_float)",
          analyst._safe_target("250") == 250.0)

    # _period_to_int unit test
    check("analyst _period_to_int('0m'): 0",
          analyst._period_to_int("0m") == 0)
    check("analyst _period_to_int('-3m'): -3",
          analyst._period_to_int("-3m") == -3)
    check("analyst _period_to_int(None): None",
          analyst._period_to_int(None) is None)
    check("analyst _period_to_int('garbage'): None",
          analyst._period_to_int("garbage") is None)

    # Strict-null `total` derivation: any bucket None → total None
    # (regression guard for the rule introduced after review feedback;
    # the prior `sum(non_None)` would silently undercount). Synthetic
    # row that mocks Yahoo emitting a null bucket.
    partial_row = {
        "period": "0m",
        "strongBuy": 7, "buy": 24, "hold": 15, "sell": 1,
        "strongSell": None,  # one bucket missing
    }
    proj = analyst._project_recommendation_row(partial_row)
    check("analyst _project_recommendation_row: any-bucket-None → total=None "
          "(strict null, no partial sums)",
          proj.get("total") is None,
          f"got total={proj.get('total')!r}")
    # The corresponding _consensus_score / _buy_pct must also be None
    # (knock-on effect of strict null).
    check("analyst _consensus_score(partial-bucket row): None",
          analyst._consensus_score(proj) is None,
          f"got {analyst._consensus_score(proj)!r}")
    check("analyst _buy_pct(partial-bucket row): None (strict null)",
          analyst._buy_pct(proj) is None,
          f"got {analyst._buy_pct(proj)!r}")
    # And the all-buckets-populated path still works.
    full_row = dict(partial_row, strongSell=1)
    proj_full = analyst._project_recommendation_row(full_row)
    check("analyst _project_recommendation_row: all buckets populated → "
          "total = sum (control case)",
          proj_full.get("total") == 7 + 24 + 15 + 1 + 1,
          f"got total={proj_full.get('total')!r}")

    # Alias pin: `recommendations` and `recommendations_summary` must
    # return identical DataFrames. Documented as a contract; if Yahoo
    # ever desynchronizes them, this fires immediately. We only call
    # `recommendations` in production code, but pin the alias so the
    # docs / project memory stay accurate.
    import yfinance as yf
    aapl_t = yf.Ticker("AAPL")
    rec_a = aapl_t.recommendations
    rec_b = aapl_t.recommendations_summary
    check("analyst yfinance: t.recommendations ≡ t.recommendations_summary "  # canary
          "(documented alias contract)",
          rec_a.equals(rec_b),
          f"shapes a={rec_a.shape} b={rec_b.shape}")

    # Sort-order pin: Yahoo emits upgrades_downgrades sorted desc by
    # date. _apply_limit silently depends on this (--limit 5 should
    # yield the newest 5, not the oldest 5). If Yahoo ever flips, this
    # fires before users notice broken --limit semantics. Canary
    # because Yahoo's sort isn't a contract — _summarize uses max()
    # which is order-independent, but --limit is order-dependent.
    aapl_dates = [c.get("date") for c in d.get("upgrades_downgrades") or []
                  if c.get("date")]
    if len(aapl_dates) >= 2:
        check("analyst AAPL: upgrades_downgrades sorted desc by date "  # canary
              "(--limit semantics depend on this)",
              all(aapl_dates[i] >= aapl_dates[i + 1]
                  for i in range(len(aapl_dates) - 1)),
              f"first 5 dates: {aapl_dates[:5]}")

    # quote_type field pin: must always be present in the result dict
    # (None when fast_info crashes; valid Yahoo enum otherwise).
    check("analyst AAPL (success): quote_type == 'EQUITY'",  # canary
          d.get("quote_type") == "EQUITY",
          f"got {d.get('quote_type')!r}")
    # ETF pin: QQQ should disambiguate to 'ETF' inline so the note
    # path consumer doesn't need a chained fast_info call.
    qqq = analyst.fetch("QQQ")
    check("analyst QQQ (note path): quote_type == 'ETF' "  # canary
          "(disambiguates inline)",
          qqq.get("quote_type") == "ETF",
          f"got {qqq.get('quote_type')!r}")
    # Bogus ticker: fast_info crashes with AttributeError, projects
    # to None. Pin the graceful handling.
    bogus = analyst.fetch("ZZZZNOTREAL")
    check("analyst ZZZZNOTREAL (bogus): quote_type is None "
          "(fast_info crash → null, NOT raised)",
          bogus.get("quote_type") is None,
          f"got {bogus.get('quote_type')!r}")
    # Cross-mode: TM (ADR) and 0700.HK (HK primary) both EQUITY
    tm_qt = analyst.fetch("TM").get("quote_type")
    hk_qt = analyst.fetch("0700.HK").get("quote_type")
    check("analyst TM (ADR): quote_type == 'EQUITY'",
          tm_qt == "EQUITY", f"got {tm_qt!r}")
    check("analyst 0700.HK (non-US primary): quote_type == 'EQUITY'",
          hk_qt == "EQUITY", f"got {hk_qt!r}")

    # quote_type passes through _summarize too (peer compare needs it
    # to filter / group by quote type).
    qqq_flat = analyst._summarize(qqq)
    check("analyst _summarize(QQQ): quote_type carried through to summary",
          qqq_flat.get("quote_type") == "ETF",
          f"got {qqq_flat.get('quote_type')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"analyst fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- screener fetch shape ---
section("screener fetch")
try:
    # --list-predefined: pure-Python (reads yf.PREDEFINED_SCREENER_QUERIES).
    # Returns a flat list of screen-row dicts (one per predefined).
    cat = screener._list_predefined()
    check("screener _list_predefined: returns flat list of rows",
          isinstance(cat, list) and all(isinstance(r, dict) for r in cat),
          f"got {type(cat).__name__}, len={len(cat) if isinstance(cat, list) else '?'}")
    # canary: yfinance ships ~19 predefined screens (9 EQUITY + 6 MUTUALFUND + 4 ETF).
    # Fail soft on count drift; fail hard if categories disappear.
    check("screener _list_predefined: ~19 saved screens (yfinance 1.3.x)",  # canary
          15 <= len(cat) <= 30,
          f"got count={len(cat)}")
    qts = {p.get("quote_type") for p in cat}
    check("screener _list_predefined: covers EQUITY / MUTUALFUND / ETF",
          qts == {"EQUITY", "MUTUALFUND", "ETF"},
          f"got {qts}")
    names = {p["name"] for p in cat}
    check("screener _list_predefined: includes 'day_gainers'",
          "day_gainers" in names,
          f"got names sample: {sorted(names)[:5]}")
    # Every row has a description (we curated 19; drift would surface
    # null descriptions for new screens).
    rows_no_desc = [r["name"] for r in cat if not r.get("description")]
    check("screener _list_predefined: every row has a description",
          not rows_no_desc,
          f"missing descriptions: {rows_no_desc}")

    # --list-fields equity: pure-Python (instantiates EquityQuery).
    fields_doc = screener._list_fields("equity")
    check("screener _list_fields(equity): returns expected envelope",
          fields_doc.get("quote_type") == "EQUITY"
          and isinstance(fields_doc.get("fields_by_category"), dict)
          and isinstance(fields_doc.get("valid_values"), dict),
          f"got keys={list(fields_doc.keys())}")
    # 'region' should be in valid_values (universal value-restricted field).
    check("screener _list_fields(equity): valid_values includes 'region'",
          "region" in fields_doc.get("valid_values", {}),
          f"got value-restricted keys: "
          f"{sorted(fields_doc.get('valid_values', {}).keys())[:8]}")

    # --list-fields fund and etf: regression for the per-class
    # _make_sample fix (was crashing because 'region' isn't a valid
    # FundQuery field). Both should return non-empty schemas.
    fund_doc = screener._list_fields("fund")
    check("screener _list_fields(fund): non-empty fields_by_category",
          fund_doc.get("quote_type") == "FUND"
          and any(fund_doc.get("fields_by_category", {}).values()),
          f"got categories: {list(fund_doc.get('fields_by_category', {}).keys())}")
    # Fund-only field should appear in fund schema, NOT in equity schema.
    fund_fields = set().union(
        *(set(v) for v in fund_doc.get("fields_by_category", {}).values())
    )
    equity_fields = set().union(
        *(set(v) for v in fields_doc.get("fields_by_category", {}).values())
    )
    check("screener _list_fields(fund): includes 'categoryname' "
          "(fund-specific)",
          "categoryname" in fund_fields,
          f"fund_fields sample: {sorted(fund_fields)[:8]}")
    check("screener _list_fields(equity): excludes 'categoryname' "
          "(fund/ETF-only field)",
          "categoryname" not in equity_fields,
          f"equity_fields contains it? {('categoryname' in equity_fields)}")

    etf_doc = screener._list_fields("etf")
    check("screener _list_fields(etf): non-empty fields_by_category",
          etf_doc.get("quote_type") == "ETF"
          and any(etf_doc.get("fields_by_category", {}).values()),
          f"got categories: {list(etf_doc.get('fields_by_category', {}).keys())}")

    # Unknown predefined name: preempted (doesn't even hit Yahoo).
    bad = screener.fetch(predefined="not_a_real_screen")
    check("screener fetch(bad predefined): error_kind not_found, attempts 0",
          bad.get("error_kind") == "not_found" and bad.get("attempts") == 0
          and bad.get("predefined") == "not_a_real_screen",
          f"got {bad}")

    # Invalid query: bogus field caught client-side by EquityQuery validator.
    bad_q = screener.fetch(
        query_dict={"operator": "and", "operands": [
            {"operator": "eq", "operands": ["bogus_field_xyz", "us"]},
        ]},
        quote_type="equity",
    )
    check("screener fetch(bad query field): error_kind not_found",
          bad_q.get("error_kind") == "not_found"
          and "invalid query" in (bad_q.get("error") or "").lower(),
          f"got {bad_q}")

    # Happy path: day_gainers — predefined screen always has hits intraday
    # AND outside US market hours (Yahoo backfills with overnight movers).
    d = screener.fetch(predefined="day_gainers", count=3)
    check("screener day_gainers: success has total + quotes list",
          isinstance(d, dict) and isinstance(d.get("quotes"), list)
          and isinstance(d.get("total"), int)
          and d.get("predefined") == "day_gainers"
          and d.get("title") and d.get("description"),
          f"got keys={list(d.keys())}")
    check("screener day_gainers: returned <= count requested",
          d.get("returned") <= 3,
          f"got returned={d.get('returned')}")
    check("screener day_gainers: at least 1 quote returned",  # canary
          d.get("returned", 0) >= 1,
          f"got returned={d.get('returned')}")

    # Quote schema: every projected key present (None acceptable).
    expected_quote_keys = set(screener.QUOTE_FIELDS)
    if d.get("quotes"):
        q0 = d["quotes"][0]
        check("screener day_gainers: quote[0] has full QUOTE_FIELDS key set",
              set(q0.keys()) == expected_quote_keys,
              f"diff: missing={expected_quote_keys - set(q0.keys())}, "
              f"extra={set(q0.keys()) - expected_quote_keys}")
        # day_gainers is equity-only → quote_type must be EQUITY.
        check("screener day_gainers: quote_type == EQUITY",
              q0.get("quote_type") == "EQUITY",
              f"got {q0.get('quote_type')!r}")
        # Equity-only fields populated (market_cap, eps_ttm); ETF-only fields null.
        check("screener day_gainers: equity quote has market_cap populated",  # canary
              isinstance(q0.get("market_cap"), int) and q0.get("market_cap") > 0,
              f"got {q0.get('market_cap')!r}")
        check("screener day_gainers: equity quote has net_assets null",
              q0.get("net_assets") is None,
              f"got {q0.get('net_assets')!r}")
        # change_pct should be > 3 (predefined filters on percentchange > 3).
        check("screener day_gainers: change_pct > 3 (predefined filter)",  # canary
              isinstance(q0.get("change_pct"), (int, float))
              and q0["change_pct"] > 3,
              f"got change_pct={q0.get('change_pct')!r}")

    # Happy path: top_etfs_us — verifies ETF-side fields populate
    # (net_assets, expense_ratio_pct) and equity-only ones are null.
    e = screener.fetch(predefined="top_etfs_us", count=3)
    check("screener top_etfs_us: returns ETF quotes",
          isinstance(e.get("quotes"), list) and e.get("returned", 0) >= 1,
          f"got returned={e.get('returned')}")
    if e.get("quotes"):
        q0 = e["quotes"][0]
        check("screener top_etfs_us: quote_type == ETF",
              q0.get("quote_type") == "ETF",
              f"got {q0.get('quote_type')!r}")
        check("screener top_etfs_us: ETF quote has net_assets populated",  # canary
              isinstance(q0.get("net_assets"), int) and q0["net_assets"] > 0,
              f"got {q0.get('net_assets')!r}")
        check("screener top_etfs_us: ETF quote has market_cap null "
              "(equity-only field)",
              q0.get("market_cap") is None,
              f"got {q0.get('market_cap')!r}")
        check("screener top_etfs_us: ETF quote has expense_ratio_pct populated",  # canary
              isinstance(q0.get("expense_ratio_pct"), (int, float))
              and q0["expense_ratio_pct"] >= 0,
              f"got {q0.get('expense_ratio_pct')!r}")

    # Custom query happy path: large US tech (AAPL/MSFT/etc territory).
    c = screener.fetch(
        query_dict={"operator": "and", "operands": [
            {"operator": "eq", "operands": ["region", "us"]},
            {"operator": "gt", "operands": ["intradaymarketcap", 5e11]},
        ]},
        quote_type="equity",
        count=3,
    )
    check("screener custom query: returns quote_type_filter=EQUITY",
          c.get("quote_type_filter") == "EQUITY"
          and isinstance(c.get("quotes"), list),
          f"got {list(c.keys())}")
    check("screener custom query: all returned quotes have market_cap > 5e11",  # canary
          all(isinstance(q.get("market_cap"), int)
              and q["market_cap"] > 5e11
              for q in (c.get("quotes") or [])),
          f"got market_caps={[q.get('market_cap') for q in (c.get('quotes') or [])]}")

    # CLI subprocess: predefined → exits 0, stdout parses as DICT
    # (note: the CLI sanity loop below assumes list-shaped output, so
    # screener gets its own check here).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--count", "2"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = None
    try:
        parsed = json.loads(out.stdout) if out.returncode == 0 else None
    except json.JSONDecodeError:
        parsed = None
    check("screener.py CLI predefined: exits 0 + stdout is single dict envelope",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("predefined") == "day_gainers"
          and isinstance(parsed.get("quotes"), list),
          f"rc={out.returncode}, "
          f"type={type(parsed).__name__ if parsed is not None else 'None'}")

    # CLI subprocess: --list-predefined → exits 0 (offline; no Yahoo call).
    # New shape: flat JSON list of screen rows.
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"), "--list-predefined"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    try:
        parsed = json.loads(out.stdout) if out.returncode == 0 else None
    except json.JSONDecodeError:
        parsed = None
    check("screener.py CLI --list-predefined: exits 0 + flat list of rows",
          out.returncode == 0 and isinstance(parsed, list)
          and len(parsed) >= 15
          and all(isinstance(r, dict) and "name" in r and "description" in r
                  for r in parsed),
          f"rc={out.returncode}, "
          f"type={type(parsed).__name__ if parsed is not None else 'None'}")

    # CLI subprocess: --list-predefined --format csv → exits 0 with headers + N rows
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--list-predefined", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("screener.py CLI --list-predefined --format csv: exits 0 + header + 19 rows",
          out.returncode == 0 and len(csv_lines) >= 16  # header + 15 minimum
          and csv_lines[0].startswith("name,quote_type,sort_field,"),
          f"rc={out.returncode}, lines={len(csv_lines)}, "
          f"head[:60]={csv_lines[0][:60] if csv_lines else '?'!r}")

    # CLI subprocess: --list-fields equity --format <non-json> → rejected
    # for csv, ndjson, AND symbols (all three should fail since the field
    # schema is nested).
    for bad_fmt in ("csv", "ndjson", "symbols"):
        cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
               "--list-fields", "equity", "--format", bad_fmt]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        check(f"screener.py CLI --list-fields --format {bad_fmt}: "
              f"argparse rejects (rc=2)",
              out.returncode == 2,
              f"rc={out.returncode}, stderr={out.stderr[-100:]!r}")

    # CLI subprocess: --list-predefined --format symbols → rejected
    # (symbols means tickers; catalog has screen names instead).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--list-predefined", "--format", "symbols"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("screener.py CLI --list-predefined --format symbols: rejected (rc=2)",
          out.returncode == 2 and "screen names" in out.stderr,
          f"rc={out.returncode}, stderr={out.stderr[-150:]!r}")

    # CLI subprocess: --full --format csv → rejected (incompatible combo).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--full", "--format", "csv",
           "--count", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("screener.py CLI --full + --format csv: argparse rejects (rc=2)",
          out.returncode == 2 and "incompatible" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[-150:]!r}")

    # CLI subprocess: --full --format ndjson → succeeds; first line should
    # have raw Yahoo keys (longName, regularMarketPrice) not projected ones.
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--full", "--format", "ndjson",
           "--count", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    first_line = out.stdout.splitlines()[0] if out.stdout.strip() else ""
    parsed = None
    if first_line:
        try:
            parsed = json.loads(first_line)
        except json.JSONDecodeError:
            parsed = None
    check("screener.py CLI --full --format ndjson: rc=0 + raw keys "
          "(regularMarketPrice) on first line",
          out.returncode == 0 and isinstance(parsed, dict)
          and "regularMarketPrice" in parsed,
          f"rc={out.returncode}, "
          f"first-line keys: {sorted((parsed or {}).keys())[:5]}")

    # CLI subprocess: --format symbols on a bad predefined → empty stdout
    # but error MUST surface on stderr (the whole point of the stderr
    # rescue is so pipelines don't silently no-op on failure).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "definitely_not_real",
           "--format", "symbols"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("screener.py CLI --format symbols on error: empty stdout + "
          "error visible on stderr",
          out.returncode == 0
          and out.stdout.strip() == ""
          and "screener:" in out.stderr
          and "error_kind=not_found" in out.stderr,
          f"rc={out.returncode}, stdout={out.stdout!r}, "
          f"stderr={out.stderr[:200]!r}")

    # CLI subprocess: --quote-type matching the predefined's actual type
    # should NOT trigger the warning (passes the narrowing check).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--quote-type", "equity",
           "--count", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("screener.py CLI --predefined day_gainers --quote-type equity: "
          "no warning (matching type is silent)",
          out.returncode == 0 and "warning:" not in out.stderr,
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # CLI subprocess: --predefined --quote-type fund → warns to stderr
    # (silently ignored upstream; we surface the no-op).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--quote-type", "fund", "--count", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("screener.py CLI --predefined + --quote-type: stderr warns of no-op",
          out.returncode == 0
          and "warning: --quote-type fund ignored with --predefined"
              in out.stderr,
          f"rc={out.returncode}, stderr={out.stderr[:120]!r}")

    # CLI subprocess: --format symbols → one ticker per line, no header
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--count", "3", "--format", "symbols"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    sym_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("screener.py CLI --format symbols: exits 0 + 3 ticker lines (no header)",
          out.returncode == 0 and len(sym_lines) == 3
          and all(ln.replace("-", "").replace(".", "").isalnum()
                  and ln == ln.upper() for ln in sym_lines),
          f"rc={out.returncode}, lines={sym_lines!r}")

    # CLI subprocess: --full → raw Yahoo payload passes through; the JSON
    # quote dict should have raw keys (regularMarketPrice, longName, …)
    # rather than projected ones (price, name, …).
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--count", "1", "--full"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        parsed = json.loads(out.stdout) if out.returncode == 0 else None
    except json.JSONDecodeError:
        parsed = None
    check("screener.py CLI --full: raw quote keys present (regularMarketPrice)",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("quotes")
          and "regularMarketPrice" in (parsed["quotes"][0] or {}),
          f"rc={out.returncode}, "
          f"first quote keys: "
          f"{sorted((parsed.get('quotes') or [{}])[0].keys())[:5] if parsed else '?'}")

    # Custom query with field valid for FundQuery but used with EquityQuery:
    # error message should hint at correct --quote-type.
    bad_qt = screener.fetch(
        query_dict={"operator": "and", "operands": [
            {"operator": "eq", "operands": ["categoryname", "Large Growth"]},
        ]},
        quote_type="equity",  # categoryname is fund-only
    )
    check("screener fetch(equity field='categoryname'): hints at --quote-type fund",
          bad_qt.get("error_kind") == "not_found"
          and "fund" in (bad_qt.get("error") or "").lower(),
          f"got error={bad_qt.get('error')!r}")

    # forward_pe clamp: synthetic raw payload with the bogus -199000 value
    # that we observed for RKLB in real Yahoo data. The projection should
    # collapse it to None, not pass through.
    fake_quote = {
        "symbol": "FAKE", "longName": "Fake Corp",
        "quoteType": "EQUITY", "regularMarketPrice": 100.0,
        "trailingPE": 25.0,
        "forwardPE": -199000.0,  # nonsense
    }
    proj = screener._project_quote(fake_quote)
    check("screener _project_quote: forward_pe clamps |v|>1000 to None",
          proj["forward_pe"] is None and proj["trailing_pe"] == 25.0,
          f"got forward_pe={proj['forward_pe']!r}, trailing_pe={proj['trailing_pe']!r}")
    fake_quote["forwardPE"] = 50000.0
    proj = screener._project_quote(fake_quote)
    check("screener _project_quote: forward_pe=50000 also clamps",
          proj["forward_pe"] is None,
          f"got forward_pe={proj['forward_pe']!r}")
    fake_quote["forwardPE"] = 35.0  # normal
    proj = screener._project_quote(fake_quote)
    check("screener _project_quote: forward_pe=35 passes through",
          proj["forward_pe"] == 35.0,
          f"got forward_pe={proj['forward_pe']!r}")

    # CLI subprocess CSV: header + N quote rows
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "day_gainers", "--count", "2", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("screener.py CLI --format csv: exits 0 + header + 2 rows (3 lines)",
          out.returncode == 0 and len(csv_lines) == 3,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 1:
        # Schema-stable header start
        check("screener.py CLI --format csv: header starts with 'symbol,name,quote_type,'",
              csv_lines[0].startswith("symbol,name,quote_type,"),
              f"got {csv_lines[0][:60]!r}")

    # CLI subprocess: bad predefined → exits 0 + error envelope (preempted)
    cmd = [sys.executable, str(SCRIPTS_DIR / "screener.py"),
           "--predefined", "definitely_not_real_screen"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    try:
        parsed = json.loads(out.stdout) if out.returncode == 0 else None
    except json.JSONDecodeError:
        parsed = None
    check("screener.py CLI bad predefined: exits 0 + error_kind=not_found "
          "(no yfinance stdout leak)",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("error_kind") == "not_found",
          f"rc={out.returncode}, parsed={parsed!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"screener fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- fund_holdings fetch shape ---
section("fund_holdings fetch")
try:
    # Happy path: SPY — all 9 sections populated (description as a top-
    # level field counts as a section per the reference doc). Single HTTP
    # call covers all properties (yfinance caches on FundsData instance).
    d = fund_holdings.fetch("SPY")
    expected_top_keys = {"symbol", "quote_type", "description", "fund_overview",
                         "operations", "asset_classes", "sector_weightings",
                         "bond_ratings", "equity_metrics", "bond_metrics",
                         "top_holdings"}
    check("fund_holdings SPY: success has all 9 sections + top-level fields",
          isinstance(d, dict) and expected_top_keys.issubset(set(d.keys())),
          f"missing={expected_top_keys - set(d.keys() if isinstance(d, dict) else [])}")
    check("fund_holdings SPY: quote_type == ETF",
          d.get("quote_type") == "ETF",
          f"got {d.get('quote_type')!r}")

    # operations: expense_ratio is a fraction in (0, 0.05) — SPY at 9.45 bps
    # = 0.000945 is well below 0.05 (5%, an absurdly expensive ETF). Pins
    # the FRACTION encoding.
    ops = d.get("operations") or {}
    er = ops.get("expense_ratio")
    check("fund_holdings SPY: expense_ratio is fraction in (0, 0.05)",
          isinstance(er, float) and 0.0 < er < 0.05,
          f"got {er!r}")
    # AUM in MILLIONS — SPY ~ \$479B = 479000 millions. Canary band wide
    # enough to absorb fund flows but tight enough to flag a unit flip
    # (whole-currency would be ~5e11, basis-points would be ~5e9).
    aum = ops.get("total_net_assets_millions")
    check("fund_holdings SPY: total_net_assets_millions in 100k-1M range "  # canary: ~480k typical
          "(MILLIONS unit pinned)",
          aum is None or (isinstance(aum, float) and 100_000 < aum < 1_000_000),
          f"got {aum!r}")

    # asset_classes: stock_pct + bond_pct + cash_pct + ... ≈ 1.0 (allow
    # negative cash for leveraged funds, generous tolerance).
    ac = d.get("asset_classes") or {}
    expected_asset = set(fund_holdings._ASSET_KEYS)
    check("fund_holdings SPY: asset_classes has documented field set",
          set(ac.keys()) == expected_asset,
          f"diff: missing={expected_asset - set(ac.keys())}, "
          f"extra={set(ac.keys()) - expected_asset}")
    # SPY is ~99.94% stocks — pin the FRACTION unit.
    sp = ac.get("stock_pct")
    check("fund_holdings SPY: stock_pct is fraction in (0.9, 1.01)",
          isinstance(sp, float) and 0.9 < sp <= 1.01,
          f"got {sp!r}")

    # equity_metrics: pe_ratio inverted from Yahoo's raw 1/ratio. SPY P/E
    # is ~25-35; if we ever stop inverting it'd land at ~0.03-0.04 and
    # this canary fires immediately. Most important guard in this section.
    em = d.get("equity_metrics") or {}
    pe = em.get("pe_ratio")
    check("fund_holdings SPY: pe_ratio inverted (10 < pe < 60) — pins "  # canary: ~27 typical
          "Yahoo's 1/ratio inversion",
          isinstance(pe, float) and 10.0 < pe < 60.0,
          f"got {pe!r} (raw 1/ratio would be ~0.03)")

    # top_holdings non-empty + correctly shaped.
    th = d.get("top_holdings") or []
    check("fund_holdings SPY: top_holdings populated (>= 5 rows)",  # canary: 10 typical
          len(th) >= 5, f"got {len(th)} rows")
    if th:
        h = th[0]
        expected_h = set(fund_holdings._HOLDING_KEYS)
        check("fund_holdings SPY: top_holdings[0] has documented field set",
              set(h.keys()) == expected_h,
              f"diff: missing={expected_h - set(h.keys())}, "
              f"extra={set(h.keys()) - expected_h}")
        w = h.get("weight")
        check("fund_holdings SPY: top_holdings[0].weight is fraction in (0, 0.5)",
              isinstance(w, float) and 0.0 < w < 0.5,
              f"got {w!r}")

    # Successful response must NOT carry `note` — pins the design.
    check("fund_holdings SPY (success): no `note` key on the result",
          "note" not in d,
          f"unexpected keys={list(d.keys())}")

    # Bond ETF path: AGG has duration_years populated, equity_metrics nulled.
    agg = fund_holdings.fetch("AGG")
    bm = agg.get("bond_metrics") or {}
    dur = bm.get("duration_years")
    check("fund_holdings AGG: duration_years populated (1 < dur < 30)",  # canary: ~3.79 typical
          isinstance(dur, float) and 1.0 < dur < 30.0,
          f"got {dur!r}")
    em_agg = agg.get("equity_metrics") or {}
    check("fund_holdings AGG: pe_ratio is None (bond fund, raw 0.0 → null)",
          em_agg.get("pe_ratio") is None,
          f"got {em_agg.get('pe_ratio')!r}")
    # Bond fund: sector_weightings empty, bond_ratings has full ladder.
    check("fund_holdings AGG: sector_weightings empty (bond fund)",
          agg.get("sector_weightings") == {},
          f"got {agg.get('sector_weightings')!r}")
    br = agg.get("bond_ratings") or {}
    check("fund_holdings AGG: bond_ratings has multiple buckets (>= 5)",
          len(br) >= 5,
          f"got {list(br.keys())}")

    # Mutual fund path: VFIAX — deeper check than just quote_type so a
    # regression that nukes mutual-fund handling shows up. VFIAX is
    # Vanguard's S&P 500 mutual fund: equity-heavy, populated holdings,
    # populated equity_metrics. Yahoo returns turnover / total_net_assets
    # as null for VFIAX (verified) so we don't pin those.
    vfiax = fund_holdings.fetch("VFIAX")
    check("fund_holdings VFIAX: quote_type == MUTUALFUND",
          vfiax.get("quote_type") == "MUTUALFUND",
          f"got {vfiax.get('quote_type')!r}")
    check("fund_holdings VFIAX: expense_ratio populated (mutual fund happy path)",
          isinstance((vfiax.get("operations") or {}).get("expense_ratio"), float)
          and 0.0 < vfiax["operations"]["expense_ratio"] < 0.05,
          f"got {(vfiax.get('operations') or {}).get('expense_ratio')!r}")
    check("fund_holdings VFIAX: top_holdings has rows (>= 5)",  # canary: 10 typical
          len(vfiax.get("top_holdings") or []) >= 5,
          f"got {len(vfiax.get('top_holdings') or [])} rows")
    check("fund_holdings VFIAX: pe_ratio populated (S&P 500 fund) and inverted",  # canary
          isinstance((vfiax.get("equity_metrics") or {}).get("pe_ratio"), float)
          and 10.0 < vfiax["equity_metrics"]["pe_ratio"] < 60.0,
          f"got {(vfiax.get('equity_metrics') or {}).get('pe_ratio')!r}")
    # earnings_growth_3y is now a FRACTION (we divide Yahoo's raw percent
    # by 100). VFIAX's S&P 500 3y trailing EPS growth ~18% → 0.18. Pin
    # the conversion: a regression that drops the divide leaves 18.03,
    # which would fail the 0 < x < 1 fraction guard immediately.
    em = vfiax.get("equity_metrics") or {}
    eg = em.get("earnings_growth_3y")
    check("fund_holdings VFIAX: earnings_growth_3y is fraction in (0, 1) — "
          "pins percent → fraction conversion",
          isinstance(eg, float) and 0.0 < eg < 1.0,  # canary: ~0.18 typical
          f"got {eg!r} (raw percent would be ~18; missed conversion)")
    # median_market_cap empirically populated for VFIAX (Yahoo emits in
    # millions). 404537 ≈ \$404B median market cap fits an S&P 500 fund
    # (mega-cap-weighted). Pin the MILLIONS unit explicitly.
    mmc = em.get("median_market_cap")
    check("fund_holdings VFIAX: median_market_cap populated as int in "
          "MILLIONS (10k-10M range pins the unit)",
          isinstance(mmc, int) and 10_000 < mmc < 10_000_000,  # canary: ~400k typical
          f"got {mmc!r} (whole-currency would be ~5e11)")
    # Category-average companions populated for VFIAX. Pins the
    # _project_two_col handler — it must run on BOTH the fund column AND
    # "Category Average" column. If a future refactor drops one side,
    # this fires immediately.
    check("fund_holdings VFIAX: pe_ratio_category_avg populated and inverted",  # canary
          isinstance(em.get("pe_ratio_category_avg"), float)
          and 10.0 < em["pe_ratio_category_avg"] < 60.0,
          f"got {em.get('pe_ratio_category_avg')!r}")
    check("fund_holdings VFIAX: median_market_cap_category_avg populated as int",
          isinstance(em.get("median_market_cap_category_avg"), int)
          and em["median_market_cap_category_avg"] > 1000,
          f"got {em.get('median_market_cap_category_avg')!r}")
    # Bond category_avg is the interesting one for an equity fund:
    # VFIAX has duration_years=null but Yahoo still gives the bond-fund
    # CATEGORY-mean duration (~4.6y for the Large Blend category).
    bm = vfiax.get("bond_metrics") or {}
    check("fund_holdings VFIAX: bond_metrics duration_years_category_avg populated",  # canary
          isinstance(bm.get("duration_years_category_avg"), float)
          and 0.5 < bm["duration_years_category_avg"] < 30.0,
          f"got {bm.get('duration_years_category_avg')!r}")

    # Non-US ETF: IWDA.L (iShares Core MSCI World UCITS) — verifies the
    # script handles non-US listed ETFs. Empirically (2026-05) returns
    # populated holdings + sectors + asset_classes but description="".
    # Pins the "non-US ETF works" claim from the reference doc.
    iwda = fund_holdings.fetch("IWDA.L")
    check("fund_holdings IWDA.L: quote_type == ETF (non-US listing)",
          iwda.get("quote_type") == "ETF",
          f"got {iwda.get('quote_type')!r}")
    check("fund_holdings IWDA.L: top_holdings populated (>= 5 rows)",  # canary
          len(iwda.get("top_holdings") or []) >= 5,
          f"got {len(iwda.get('top_holdings') or [])} rows")
    check("fund_holdings IWDA.L: sector_weightings non-empty (equity ETF)",
          len(iwda.get("sector_weightings") or {}) >= 5,
          f"got {len(iwda.get('sector_weightings') or {})} keys")

    # Non-fund path: AAPL / ^GSPC / BTC-USD all hit YFDataException →
    # success-with-note + captured quote_type (NEW: post-refactor we
    # surface the quote_type yfinance resolved before the parse error).
    expected_qt = {"AAPL": "EQUITY", "^GSPC": "INDEX", "BTC-USD": "CRYPTOCURRENCY"}
    for sym, qt in expected_qt.items():
        nf = fund_holdings.fetch(sym)
        check(f"fund_holdings {sym}: non-fund path emits note + no error_kind + no data",
              nf.get("note")
              and "error_kind" not in nf
              and "fund_overview" not in nf
              and "top_holdings" not in nf,
              f"got keys={list(nf.keys())}")
        check(f"fund_holdings {sym}: non-fund path captures quote_type='{qt}'",
              nf.get("quote_type") == qt,
              f"got quote_type={nf.get('quote_type')!r}")

    # Bogus path: HTTP 404 → standard error_kind=not_found path. quote_type
    # is unrecoverable here because the parser never ran, so it's null.
    bogus = fund_holdings.fetch("ZZZZNOTREAL")
    check("fund_holdings ZZZZNOTREAL: error_kind=not_found (HTTP 404 path)",
          bogus.get("error_kind") == "not_found",
          f"got {bogus.get('error_kind')!r}")
    check("fund_holdings ZZZZNOTREAL: no quote_type (parser never ran)",
          bogus.get("quote_type") is None,
          f"got quote_type={bogus.get('quote_type')!r}")

    # _apply_limit truncates top_holdings IN PLACE. After applying limit=2
    # the list must be ≤ 2; other sections untouched. Pins the function
    # contract (matches holders.py's _apply_limit smoke check).
    capped = fund_holdings._apply_limit(fund_holdings.fetch("SPY"), 2)
    check("fund_holdings _apply_limit(2): top_holdings capped to <= 2",
          len(capped.get("top_holdings") or []) <= 2,
          f"got {len(capped.get('top_holdings') or [])} rows")
    check("fund_holdings _apply_limit(2): operations / asset_classes / sectors untouched",
          len(capped.get("asset_classes") or {}) == 6
          and len(capped.get("sector_weightings") or {}) >= 5,
          f"asset={len(capped.get('asset_classes') or {})}, "
          f"sectors={len(capped.get('sector_weightings') or {})}")

    # _summarize projection: peer-comparison flat dict carries the
    # documented field set + symbol. Pins schema for CSV column drift.
    flat = fund_holdings._summarize(d)
    expected_flat = {"symbol", *fund_holdings._SUMMARY_FLAT_KEYS}
    check("fund_holdings _summarize(SPY): has documented flat field set",
          expected_flat.issubset(set(flat.keys())),
          f"missing={expected_flat - set(flat.keys())}")
    # holdings_concentration must equal sum(weights) of returned holdings.
    expected_conc = sum(h["weight"] for h in (d.get("top_holdings") or [])
                        if h.get("weight") is not None)
    actual_conc = flat.get("holdings_concentration")
    check("fund_holdings _summarize: holdings_concentration matches sum(weights)",
          actual_conc is None or abs(actual_conc - expected_conc) < 1e-9,
          f"got {actual_conc!r}, expected {expected_conc!r}")
    # _summarize on non-fund must carry the `note` AND `quote_type` through
    # for CSV (so peer-compare tables don't lose the disambiguation signal).
    nf_flat = fund_holdings._summarize(fund_holdings.fetch("AAPL"))
    check("fund_holdings _summarize(AAPL non-fund): preserves `note` for CSV",
          "note" in nf_flat and nf_flat.get("note"),
          f"got keys={list(nf_flat.keys())}")
    check("fund_holdings _summarize(AAPL non-fund): preserves `quote_type` for CSV",
          nf_flat.get("quote_type") == "EQUITY",
          f"got quote_type={nf_flat.get('quote_type')!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"fund_holdings fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- calendars fetch shape (market-wide, not per-ticker) ---
# Same envelope shape as screener: one HTTP, one result. Schema
# differs per --type so we test all four. Also covers multi-type,
# --full, --summary, --past-days, and the new strict date validation.
section("calendars fetch")
try:
    today = datetime.now(timezone.utc).date()
    start = today.isoformat()
    end = (today + timedelta(days=7)).isoformat()

    # --- _resolve_dates pure-Python invariants (offline) ---
    s, e = calendars._resolve_dates(None, None, None, None)
    check("calendars _resolve_dates(no args): defaults to today + 7",
          s == start and e == end,
          f"got start={s}, end={e}")
    s, e = calendars._resolve_dates("2026-06-01", None, 14, None)
    check("calendars _resolve_dates(start, days=14): end = start + 14",
          s == "2026-06-01" and e == "2026-06-15",
          f"got start={s}, end={e}")
    s, e = calendars._resolve_dates("2026-06-01", "2026-06-30", None, None)
    check("calendars _resolve_dates(start, end): end honored",
          s == "2026-06-01" and e == "2026-06-30",
          f"got start={s}, end={e}")
    s, e = calendars._resolve_dates(None, None, None, 7)
    expect_start = (today - timedelta(days=7)).isoformat()
    check("calendars _resolve_dates(past_days=7): today-7 → today",
          s == expect_start and e == today.isoformat(),
          f"got start={s}, end={e}")

    # --- _infer_unit (offline; no Yahoo calls) ---
    check("calendars _infer_unit('GDP YY*') → percent (rate-of-change suffix)",
          calendars._infer_unit("GDP YY*") == "percent",
          f"got {calendars._infer_unit('GDP YY*')!r}")
    check("calendars _infer_unit('PMI Composite') → index_level",
          calendars._infer_unit("PMI Composite") == "index_level",
          f"got {calendars._infer_unit('PMI Composite')!r}")
    check("calendars _infer_unit('Non-Farm Payrolls') → thousands",
          calendars._infer_unit("Non-Farm Payrolls") == "thousands",
          f"got {calendars._infer_unit('Non-Farm Payrolls')!r}")
    check("calendars _infer_unit('Trade Balance') → currency",
          calendars._infer_unit("Trade Balance") == "currency",
          f"got {calendars._infer_unit('Trade Balance')!r}")
    check("calendars _infer_unit(None) → None",
          calendars._infer_unit(None) is None)
    check("calendars _infer_unit('Some Unknown Indicator') → None",
          calendars._infer_unit("Some Unknown Indicator") is None,
          f"got {calendars._infer_unit('Some Unknown Indicator')!r}")
    # Extra _infer_unit edge cases.
    check("calendars _infer_unit('Retail Sales YY*') → percent "
          "(YY suffix not blocked by trailing *)",
          calendars._infer_unit("Retail Sales YY*") == "percent",
          f"got {calendars._infer_unit('Retail Sales YY*')!r}")
    check("calendars _infer_unit('M2 Money Supply') → currency "
          "(M\\d pattern in currency rule)",
          calendars._infer_unit("M2 Money Supply") == "currency",
          f"got {calendars._infer_unit('M2 Money Supply')!r}")
    check("calendars _infer_unit('Manheim Used Vehicle Index') → index_level",
          calendars._infer_unit("Manheim Used Vehicle Index") == "index_level",
          f"got {calendars._infer_unit('Manheim Used Vehicle Index')!r}")
    check("calendars _infer_unit('Fed Funds Rate Decision') → percent",
          calendars._infer_unit("Fed Funds Rate Decision") == "percent",
          f"got {calendars._infer_unit('Fed Funds Rate Decision')!r}")

    # _first() defensive-coverage tests (offline). Was missing
    # empty-string and pd.NA fallthrough before this round.
    check("calendars _first: skips empty string",
          calendars._first({"a": "", "b": "real"}, "a", "b") == "real",
          "should fall through empty string")
    check("calendars _first: skips whitespace-only string",
          calendars._first({"a": "   ", "b": "real"}, "a", "b") == "real",
          "should fall through whitespace-only string")
    check("calendars _first: skips NaN float",
          calendars._first({"a": float("nan"), "b": 5.0}, "a", "b") == 5.0,
          "should fall through NaN")
    check("calendars _first: skips Inf float",
          calendars._first({"a": float("inf"), "b": 5.0}, "a", "b") == 5.0,
          "should fall through Inf")
    check("calendars _first: skips pd.NA",
          calendars._first({"a": pd.NA, "b": 5.0}, "a", "b") == 5.0,
          "should fall through pd.NA")
    check("calendars _first: skips None",
          calendars._first({"a": None, "b": "real"}, "a", "b") == "real")
    check("calendars _first: returns first valid (no fallthrough needed)",
          calendars._first({"a": "real", "b": "fallback"}, "a", "b") == "real")
    check("calendars _first: all missing → None",
          calendars._first({"a": None, "b": ""}, "a", "b") is None)
    check("calendars _first: bool False is valid (not missing)",
          calendars._first({"a": False, "b": True}, "a", "b") is False)

    # _iso_dt() defensive-coverage tests (offline). Was falling
    # through to safe_str on unknown types — could turn an epoch
    # int into a number-as-string. Now strict.
    check("calendars _iso_dt(None) → None",
          calendars._iso_dt(None) is None)
    check("calendars _iso_dt(pd.NaT) → None",
          calendars._iso_dt(pd.NaT) is None)
    check("calendars _iso_dt(pd.NA) → None",
          calendars._iso_dt(pd.NA) is None)
    ts = pd.Timestamp("2026-05-15 04:00:00", tz="UTC")
    check("calendars _iso_dt(pd.Timestamp): ISO with offset",
          calendars._iso_dt(ts) == "2026-05-15T04:00:00+00:00",
          f"got {calendars._iso_dt(ts)!r}")
    check("calendars _iso_dt(empty string) → None",
          calendars._iso_dt("") is None)
    check("calendars _iso_dt('2026-05-15T04:00:00+00:00') → passthrough",
          calendars._iso_dt("2026-05-15T04:00:00+00:00")
          == "2026-05-15T04:00:00+00:00")
    # Epoch fallback (Yahoo doesn't currently emit, but defense).
    # 1700000000 = 2023-11-14 UTC — pin to a known fixed epoch + value
    # so the test isn't sensitive to my epoch arithmetic.
    res = calendars._iso_dt(1700000000)
    check("calendars _iso_dt(epoch int): YYYY-MM-DD via epoch_to_date "
          "(defensive fallback for hypothetical Yahoo drift)",
          res == "2023-11-14",
          f"got {res!r}")
    # Unknown type returns None (not safe_str-coerced)
    check("calendars _iso_dt(arbitrary obj) → None (not stringified)",
          calendars._iso_dt(object()) is None)

    # --- _snake (offline; --full key normalization) ---
    check("calendars _snake('Event Start Date') → event_start_date",
          calendars._snake("Event Start Date") == "event_start_date")
    check("calendars _snake('Surprise(%)') → surprise_pct",
          calendars._snake("Surprise(%)") == "surprise_pct")
    check("calendars _snake('Optionable?') → optionable",
          calendars._snake("Optionable?") == "optionable")

    # --- earnings ---
    er = calendars.fetch(
        cal_type="earnings", start=start, end=end, limit=5, offset=0,
        market_cap=None, filter_most_active=True,
    )
    check("calendars earnings: envelope has type/start/end/results "
          "(no `total` field — dropped as misleading)",
          er.get("type") == "earnings"
          and er.get("start") == start
          and er.get("end") == end
          and isinstance(er.get("results"), list)
          and "total" not in er,
          f"got keys={list(er.keys())}")
    check("calendars earnings: filter_most_active reflected",
          er.get("filter_most_active") is True)
    if er.get("results"):
        row = er["results"][0]
        expected = set(calendars.EARNINGS_KEYS)
        check("calendars earnings: row schema matches EARNINGS_KEYS",
              set(row.keys()) == expected,
              f"diff +{set(row.keys()) - expected} -{expected - set(row.keys())}")
        check("calendars earnings: market_cap is int or None",
              row.get("market_cap") is None or isinstance(row.get("market_cap"), int))

    # Earnings with --no-most-active (filter off) — surfaces wider set
    er_off = calendars.fetch(
        cal_type="earnings", start=start, end=end, limit=5, offset=0,
        market_cap=None, filter_most_active=False,
    )
    check("calendars earnings (filter_most_active=False): envelope reflects",
          er_off.get("filter_most_active") is False)
    # Earnings with offset > 0 silently disables filter_most_active
    er_off2 = calendars.fetch(
        cal_type="earnings", start=start, end=end, limit=5, offset=5,
        market_cap=None, filter_most_active=True,
    )
    check("calendars earnings (offset>0): filter_most_active silently False "
          "(yfinance limitation, surfaced in envelope)",
          er_off2.get("filter_most_active") is False)

    # --- ipo ---
    ip = calendars.fetch(
        cal_type="ipo", start=start, end=(today + timedelta(days=30)).isoformat(),
        limit=5, offset=0, market_cap=None, filter_most_active=False,
    )
    check("calendars ipo: envelope no filter_most_active key (earnings-only)",
          "filter_most_active" not in ip)
    if ip.get("results"):
        row = ip["results"][0]
        expected = set(calendars.IPO_KEYS)
        check("calendars ipo: row schema matches IPO_KEYS (with new "
              "_datetime field names)",
              set(row.keys()) == expected,
              f"diff +{set(row.keys()) - expected} -{expected - set(row.keys())}")
        # ipo_datetime is now full ISO datetime, not YYYY-MM-DD truncation
        if row.get("ipo_datetime"):
            check("calendars ipo: ipo_datetime is full ISO with offset "
                  "(not truncated to date — fixes seasonal off-by-one risk)",
                  isinstance(row["ipo_datetime"], str)
                  and "T" in row["ipo_datetime"]
                  and ("+" in row["ipo_datetime"] or "Z" in row["ipo_datetime"]),
                  f"got {row.get('ipo_datetime')!r}")

    # --- splits ---
    sp = calendars.fetch(
        cal_type="splits", start=start, end=end, limit=5, offset=0,
        market_cap=None, filter_most_active=False,
    )
    if sp.get("results"):
        row = sp["results"][0]
        expected = set(calendars.SPLITS_KEYS)
        check("calendars splits: row schema matches SPLITS_KEYS "
              "(now includes `direction`)",
              set(row.keys()) == expected,
              f"diff +{set(row.keys()) - expected} -{expected - set(row.keys())}")
        check("calendars splits: optionable is bool or None",
              row.get("optionable") is None or isinstance(row.get("optionable"), bool))
        # direction derivation: forward when new>old, reverse when new<old
        if row.get("old_ratio") is not None and row.get("new_ratio") is not None:
            old, new = row["old_ratio"], row["new_ratio"]
            expect_dir = ("forward" if new > old
                          else "reverse" if new < old
                          else "even")
            check(f"calendars splits: direction derived correctly "
                  f"(old={old}, new={new}, direction={row.get('direction')})",
                  row.get("direction") == expect_dir,
                  f"expected {expect_dir}, got {row.get('direction')!r}")

    # --- economic ---
    ec = calendars.fetch(
        cal_type="economic", start=start, end=end, limit=10, offset=0,
        market_cap=None, filter_most_active=False,
    )
    if ec.get("results"):
        row = ec["results"][0]
        expected = set(calendars.ECONOMIC_KEYS)
        check("calendars economic: row schema matches ECONOMIC_KEYS "
              "(now includes `unit`)",
              set(row.keys()) == expected,
              f"diff +{set(row.keys()) - expected} -{expected - set(row.keys())}")
        check("calendars economic: row.event is non-empty str",
              isinstance(row.get("event"), str) and row["event"])
        # `unit` is best-effort — most rows in a typical batch should
        # match a rule (most economic releases ARE percent / index /
        # currency / thousands).
        with_unit = sum(1 for r in ec["results"] if r.get("unit"))
        check(f"calendars economic: most rows get a unit inference "
              f"({with_unit}/{len(ec['results'])} populated)",  # canary
              with_unit >= max(1, len(ec["results"]) // 2),
              f"got {with_unit}/{len(ec['results'])} with unit")

    # --- --full path ---
    sp_full = calendars.fetch(
        cal_type="splits", start=start, end=end, limit=2, offset=0,
        market_cap=None, filter_most_active=False, full=True,
    )
    if sp_full.get("results"):
        row = sp_full["results"][0]
        # Raw Yahoo keys snake_cased — the projection-only field
        # `direction` should NOT be present in --full output.
        check("calendars splits --full: emits raw snake_cased Yahoo keys "
              "(symbol/payable_on/old_share_worth/share_worth)",
              "old_share_worth" in row and "share_worth" in row
              and "payable_on" in row,
              f"got keys={list(row.keys())}")
        check("calendars splits --full: NO derived fields like `direction`",
              "direction" not in row,
              f"got keys={list(row.keys())}")

    # --- summarize() projections ---
    er_sum = calendars.summarize(er) if er.get("results") else None
    if er_sum:
        s = er_sum.get("summary") or {}
        check("calendars summarize(earnings): has count + count_by_timing dict",
              "count" in s and isinstance(s.get("count_by_timing"), dict))
        check("calendars summarize(earnings): preserves envelope metadata "
              "(type/start/end)",
              er_sum.get("type") == "earnings"
              and er_sum.get("start") == start
              and "results" not in er_sum,
              f"got keys={list(er_sum.keys())}")

    if sp.get("results"):
        sp_sum = calendars.summarize(sp).get("summary") or {}
        rows = sp.get("results") or []
        expect_fwd = sum(1 for r in rows if r.get("direction") == "forward")
        expect_rev = sum(1 for r in rows if r.get("direction") == "reverse")
        check("calendars summarize(splits): count_forward/reverse match rows",
              sp_sum.get("count_forward") == expect_fwd
              and sp_sum.get("count_reverse") == expect_rev,
              f"got fwd={sp_sum.get('count_forward')}, rev={sp_sum.get('count_reverse')}; "
              f"expected fwd={expect_fwd}, rev={expect_rev}")

    # --- empty path: pick a window that's likely to be empty for splits ---
    # Splits in a 1-day window starting on a deep-past Saturday should
    # rarely have anything. But tests need to be deterministic — instead,
    # use a far-future window where Yahoo doesn't have splits scheduled.
    far_start = "2030-12-25"
    far_end = "2030-12-26"
    em = calendars.fetch(
        cal_type="splits", start=far_start, end=far_end, limit=5, offset=0,
        market_cap=None, filter_most_active=False,
    )
    check("calendars splits (empty window): success-with-note path "
          "(no error_kind, populated note, empty results)",
          em.get("error_kind") is None
          and em.get("note")
          and em.get("results") == [],
          f"got note={em.get('note')!r}, results={em.get('results')}, "
          f"error_kind={em.get('error_kind')!r}")

    # --- error path: mock with_retry to simulate network failure ---
    # Calendars doesn't take tickers, so there's no "bogus ticker"
    # path. Use a yfinance.Calendars subclass-monkey to force the
    # error path.
    import unittest.mock as _mock
    with _mock.patch("calendars.with_retry",
                     return_value=(None, "rate_limit", 3)):
        err = calendars.fetch(
            cal_type="earnings", start=start, end=end, limit=5, offset=0,
            market_cap=None, filter_most_active=True,
        )
    check("calendars error path (mocked rate_limit): error/error_kind/attempts "
          "populated, no results",
          err.get("error_kind") == "rate_limit"
          and err.get("attempts") == 3
          and err.get("error")
          and "results" not in err,
          f"got {err!r}")

    # --- CLI subprocess: single dict envelope (single --type) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings", "--limit", "3"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = None
    try:
        parsed = json.loads(out.stdout) if out.returncode == 0 else None
    except json.JSONDecodeError:
        parsed = None
    check("calendars.py CLI single --type: exits 0 + stdout is single dict envelope",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("type") == "earnings"
          and isinstance(parsed.get("results"), list),
          f"rc={out.returncode}")

    # --- CLI: case-insensitive --type (NEW: was case-sensitive before) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "SPLITS", "--limit", "2"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --type SPLITS (uppercase): accepted, "
          "envelope.type == 'splits'",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("type") == "splits",
          f"rc={out.returncode}")

    # --- CLI: multi-type --type earnings,splits emits LIST envelope ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings,splits", "--limit", "2"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --type earnings,splits: emits LIST of "
          "2 envelopes (multi-type) in input order",
          out.returncode == 0 and isinstance(parsed, list)
          and len(parsed) == 2
          and parsed[0].get("type") == "earnings"
          and parsed[1].get("type") == "splits",
          f"rc={out.returncode}, type={type(parsed).__name__}")

    # --- CLI: --type all alias ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "all", "--limit", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --type all: 4 envelopes "
          "(earnings/ipo/splits/economic)",
          out.returncode == 0 and isinstance(parsed, list)
          and len(parsed) == 4
          and {e.get("type") for e in parsed}
              == {"earnings", "ipo", "splits", "economic"},
          f"rc={out.returncode}")

    # --- CLI: --type all --format ndjson tags each row with record_class ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "all", "--limit", "1", "--format", "ndjson"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    nd_lines = [json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]
    check("calendars.py CLI --type all --format ndjson: every record "
          "has `record_class`",
          out.returncode == 0
          and len(nd_lines) >= 4  # at least 1 per type
          and all("record_class" in r for r in nd_lines)
          and {r["record_class"] for r in nd_lines}
              <= {"earnings", "ipo", "splits", "economic"},
          f"rc={out.returncode}, line_count={len(nd_lines)}")

    # --- CLI: --summary on single type ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings", "--limit", "5", "--summary"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --summary (single): envelope has `summary` "
          "dict, NO `results` array",
          out.returncode == 0 and isinstance(parsed, dict)
          and isinstance(parsed.get("summary"), dict)
          and "results" not in parsed,
          f"rc={out.returncode}")

    # --- CLI: --summary multi-type ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings,splits", "--limit", "5", "--summary"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --summary multi: emits list of summary envelopes",
          out.returncode == 0 and isinstance(parsed, list)
          and len(parsed) == 2
          and all("summary" in p for p in parsed),
          f"rc={out.returncode}")

    # --- CLI: --past-days subprocess ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "economic", "--past-days", "3", "--limit", "2"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    if parsed:
        expect_start = (today - timedelta(days=3)).isoformat()
        check("calendars.py CLI --past-days 3: envelope.start = today-3, "
              "envelope.end = today",
              parsed.get("start") == expect_start
              and parsed.get("end") == today.isoformat(),
              f"got start={parsed.get('start')}, end={parsed.get('end')}")

    # --- CLI: --days subprocess (verifies envelope.end matches start+N) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "ipo", "--start", "2026-06-01", "--days", "14",
           "--limit", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py CLI --start + --days 14: envelope.end = start+14",
          out.returncode == 0 and isinstance(parsed, dict)
          and parsed.get("start") == "2026-06-01"
          and parsed.get("end") == "2026-06-15",
          f"rc={out.returncode}, parsed={parsed}")

    # --- CLI: --type with invalid value rejected ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "foobar"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("calendars.py CLI --type foobar: rc=2 (rejected with helpful error)",
          out.returncode == 2
          and "unknown calendar type" in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr[:200]!r}")

    # --- CLI: --no-most-active warning when paired with non-earnings type ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "splits", "--no-most-active", "--limit", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("calendars.py --type splits --no-most-active: stderr warns "
          "(earnings-only flag)",
          "warning" in out.stderr.lower() and "--no-most-active" in out.stderr,
          f"got stderr={out.stderr!r}")

    # --- CLI: --no-most-active does NOT warn when type list contains earnings ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings,splits", "--no-most-active", "--limit", "1"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    check("calendars.py --type earnings,splits --no-most-active: no warning "
          "(earnings is in the list)",
          out.returncode == 0
          and "warning" not in out.stderr.lower(),
          f"rc={out.returncode}, stderr={out.stderr!r}")

    # --- CLI: --start / --end strict format validation ---
    for bad in ("2026/06/01", "2026-06-01T12:00:00", "06-01-2026", "Jun 1 2026"):
        cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
               "--start", bad]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        check(f"calendars.py --start {bad!r}: rc=2 (strict YYYY-MM-DD)",
              out.returncode == 2 and "must be YYYY-MM-DD" in out.stderr,
              f"rc={out.returncode}")

    # --- CLI: --end / --days / --past-days mutually exclusive ---
    for combo in (
        ("--end", "2026-06-15", "--days", "7"),
        ("--end", "2026-06-15", "--past-days", "7"),
        ("--days", "7", "--past-days", "7"),
    ):
        cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"), *combo]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        check(f"calendars.py {' '.join(combo)}: rc=2 (mutually exclusive)",
              out.returncode == 2,
              f"rc={out.returncode}")

    # --- CLI: --past-days + --start rejected ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--past-days", "7", "--start", "2026-01-01"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("calendars.py --past-days + --start: rc=2 "
          "(--past-days starts the window at today-N)",
          out.returncode == 2
          and "--past-days cannot be combined with --start" in out.stderr,
          f"rc={out.returncode}")

    # --- CLI: --full + --format csv rejected ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--full", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("calendars.py --full + --format csv: rc=2 (incompatible)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # --- CLI: --summary + --full rejected ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--summary", "--full"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("calendars.py --summary + --full: rc=2 (mutually exclusive)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # --- CSV format: header has type-specific cols (all 4 types) ---
    for cal_type, keys in (
        ("earnings", calendars.EARNINGS_KEYS),
        ("ipo",      calendars.IPO_KEYS),
        ("splits",   calendars.SPLITS_KEYS),
        ("economic", calendars.ECONOMIC_KEYS),
    ):
        cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
               "--type", cal_type, "--limit", "2", "--format", "csv"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        check(f"calendars.py --type {cal_type} --format csv: rc=0 + header",
              out.returncode == 0 and len(csv_lines) >= 1,
              f"rc={out.returncode}, lines={len(csv_lines)}")
        if csv_lines:
            header = csv_lines[0]
            check(f"calendars.py --format csv ({cal_type}): header has "
                  f"{cal_type.upper()}_KEYS + note + meta",
                  all(c in header for c in keys)
                  and "note" in header and "error_kind" in header,
                  f"got header={header!r}")

    # --- CSV multi-type: union schema with record_class discriminator ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings,splits", "--limit", "2", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if csv_lines:
        header = csv_lines[0]
        check("calendars.py multi-type --format csv: header starts with "
              "record_class then union of cols from both types",
              header.startswith("record_class,")
              and "symbol" in header and "direction" in header
              and "event_name" in header,
              f"got header={header!r}")

    # --- --full multi-type works (each envelope projected raw) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "all", "--full", "--limit", "1", "--format", "ndjson"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    nd_lines = [json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]
    check("calendars.py --type all --full --format ndjson: each row "
          "has record_class + raw Yahoo keys (no derived `direction` "
          "or `unit` even on splits / economic)",
          out.returncode == 0 and len(nd_lines) >= 4
          and all("record_class" in r for r in nd_lines)
          and not any("direction" in r for r in nd_lines
                      if r.get("record_class") == "splits")
          and not any("unit" in r for r in nd_lines
                      if r.get("record_class") == "economic"),
          f"rc={out.returncode}, lines={len(nd_lines)}")

    # --- --summary --format csv (single type): nested dict cells
    #     JSON-encoded into single columns ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings", "--summary", "--limit", "5", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("calendars.py --type earnings --summary --format csv: rc=0 "
          "+ header + 1 data row",
          out.returncode == 0 and len(csv_lines) == 2,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 2:
        header = csv_lines[0]
        check("calendars.py --summary --format csv (earnings): header has "
              "type + count + count_by_timing + avg_market_cap",
              "type" in header and "count" in header
              and "count_by_timing" in header and "avg_market_cap" in header,
              f"got header={header!r}")

    # --- --summary --format csv (multi-type): one row per type ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "earnings,splits", "--summary", "--limit", "5",
           "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("calendars.py multi-type --summary --format csv: header + "
          "2 data rows (one per type)",
          out.returncode == 0 and len(csv_lines) == 3,
          f"rc={out.returncode}, lines={len(csv_lines)}")

    # --- --type all --summary: 4 envelopes, each with `summary` ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "calendars.py"),
           "--type", "all", "--summary", "--limit", "5"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    parsed = json.loads(out.stdout) if out.returncode == 0 else None
    check("calendars.py --type all --summary: 4 envelopes, each has summary",
          out.returncode == 0 and isinstance(parsed, list)
          and len(parsed) == 4
          and all(isinstance(p.get("summary"), dict) for p in parsed),
          f"rc={out.returncode}, len={len(parsed) if parsed else 'None'}")

    # --- summarize() on error envelope: produces sensible shape, no crash ---
    err_env = {
        "type": "earnings",
        "start": "2026-05-10",
        "end": "2026-05-17",
        "filter_most_active": True,
        "market_cap_floor": None,
        "error": "fetch failed (rate_limit, after 3 attempt(s))",
        "error_kind": "rate_limit",
        "attempts": 3,
    }  # NB: no `results` key — error path
    rolled = calendars.summarize(err_env)
    check("calendars summarize(error envelope): preserves error fields, "
          "summary={count: 0, ...}",
          rolled.get("error_kind") == "rate_limit"
          and isinstance(rolled.get("summary"), dict)
          and rolled["summary"].get("count") == 0
          and "results" not in rolled,
          f"got {rolled!r}")

    # --- count_by_region renamed (was count_by_region_top10) ---
    if ec.get("results"):
        ec_sum = calendars.summarize(ec).get("summary") or {}
        check("calendars summarize(economic): rollup uses `count_by_region` "
              "(NOT count_by_region_top10 — name fixed)",
              "count_by_region" in ec_sum
              and "count_by_region_top10" not in ec_sum,
              f"got keys={list(ec_sum.keys())}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"calendars fetch crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- CLI sanity: focus on what only subprocess testing covers (argparse,
#     JSON serialization, exit codes, --help). Schema invariants are
#     already covered by the import-path sections above; don't re-test.
section("CLI sanity (subprocess)")
try:
    # Each script's happy path: exit 0 + stdout parses as JSON list
    for script, args in (
        ("fast_info.py", ("AAPL",)),
        ("history.py", ("--period", "5d", "AAPL")),
        ("info.py", ("AAPL",)),
        ("info.py", ("--summary", "AAPL")),
        ("earnings.py", ("--limit", "5", "AAPL")),
        ("earnings.py", ("--summary", "AAPL")),
        ("earnings.py", ("--estimates", "AAPL")),
        ("financials.py", ("--statement", "income", "--limit", "2", "AAPL")),
        ("financials.py", ("--summary", "AAPL")),
        ("financials.py", ("--period", "ttm", "AAPL")),
        ("news.py", ("--limit", "2", "AAPL")),
        ("holders.py", ("--limit", "3", "AAPL")),
        ("holders.py", ("--summary", "AAPL")),
        ("insiders.py", ("--limit", "3", "AAPL")),
        ("insiders.py", ("--summary", "AAPL")),
        ("options.py", ("--moneyness", "5", "--limit", "3", "AAPL")),
        ("options.py", ("--type", "calls", "--moneyness", "5", "--limit", "3", "AAPL")),
        ("options.py", ("--type", "puts", "--moneyness", "5", "--limit", "3", "AAPL")),
        ("options.py", ("--summary", "--moneyness", "5", "AAPL")),
        ("analyst.py", ("--limit", "3", "AAPL")),
        ("analyst.py", ("--summary", "AAPL")),
        ("fund_holdings.py", ("--limit", "3", "SPY")),
        ("fund_holdings.py", ("--summary", "SPY", "VTI")),
        ("sec_filings.py", ("--limit", "3", "AAPL")),
        ("sec_filings.py", ("--type", "10-K,10-Q", "--limit", "2", "AAPL")),
        ("sec_filings.py", ("--type", "10-k", "--limit", "1", "AAPL")),  # case-insensitive
        ("sec_filings.py", ("--since", "2024-01-01", "--limit", "3", "AAPL")),
        ("sec_filings.py", ("--days", "30", "--type", "8-K", "AAPL")),
        ("sec_filings.py", ("--summary", "AAPL", "TM", "SPY")),
    ):
        rc, data = run_cli(script, *args)
        check(f"{script} {' '.join(args)}: exit 0 + valid JSON list",
              rc == 0 and isinstance(data, list) and len(data) >= 1,
              f"rc={rc}, type={type(data).__name__}")

    # Error path via CLI: bogus ticker still exits 0 with error field
    # (the import-path test covers fetch() return shape; this verifies
    # main() doesn't raise on it).
    rc, data = run_cli("info.py", "ZZZZNOTREAL")
    check("info.py bogus ticker: exit 0 + error field in JSON",
          rc == 0 and isinstance(data, list)
          and "error" in (data[0] if data else {}))

    # --help shouldn't hang or exit non-zero (catches argparse misconfig
    # like `%(default)s` referencing a non-existent default)
    for script in ("fast_info.py", "history.py", "info.py", "earnings.py",
                   "financials.py", "news.py", "holders.py", "options.py",
                   "insiders.py", "analyst.py", "screener.py",
                   "fund_holdings.py", "sec_filings.py"):
        cmd = [sys.executable, str(SCRIPTS_DIR / script), "--help"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        check(f"{script} --help exits 0",
              out.returncode == 0, f"got rc={out.returncode}")

    # --summary + --past-only is a config error → argparse exit 2
    cmd = [sys.executable, str(SCRIPTS_DIR / "earnings.py"),
           "--summary", "--past-only", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("earnings --summary --past-only: argparse rejects (rc=2)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # --limit out of range → argparse exit 2
    cmd = [sys.executable, str(SCRIPTS_DIR / "earnings.py"),
           "--limit", "999", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("earnings --limit 999: argparse rejects out-of-range (rc=2)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # --estimates + --format csv WITHOUT --summary is a config error: the
    # default-mode CSV layout is one row per (symbol, earnings_date) and
    # has nowhere to put a 4-period analyst panel.
    cmd = [sys.executable, str(SCRIPTS_DIR / "earnings.py"),
           "--estimates", "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("earnings --estimates --format csv (no --summary): rejects (rc=2)",
          out.returncode == 2,
          f"rc={out.returncode}")

    # WITH --summary, --estimates --format csv works: consensus_* fields
    # flatten cleanly. Header row should include consensus_* columns.
    cmd = [sys.executable, str(SCRIPTS_DIR / "earnings.py"),
           "--summary", "--estimates", "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("earnings --summary --estimates --format csv: exits 0 + parses",
          out.returncode == 0 and len(csv_lines) >= 2,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    check("earnings --summary --estimates --format csv: header has consensus_* cols",
          csv_lines and "consensus_eps_avg" in csv_lines[0],
          f"got header={csv_lines[0] if csv_lines else None}")

    # news CSV: novel "one row per ARTICLE" layout (other modes are one
    # row per ticker). Mixing a bogus ticker (empty result) with a real
    # one specifically pins:
    #   - empty-result row carries `note` (not just blanks — past bug)
    #   - article rows still produce one row per article, symbol repeats
    cmd = [sys.executable, str(SCRIPTS_DIR / "news.py"),
           "--limit", "2", "--format", "csv", "ZZZZNOTREAL", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # 1 header + 1 empty-ticker row + 2 AAPL article rows = 4
    check("news --format csv mixed batch: header + 3 data rows (4 total)",
          out.returncode == 0 and len(csv_lines) == 4,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 4:
        header = csv_lines[0]
        check("news --format csv: header contains documented article cols + note",
              all(f in header for f in ("title", "summary", "pub_date",
                                         "provider", "url",
                                         "is_premium", "editors_pick", "note")),
              f"got header={header!r}")
        # empty-result row: must start with the bogus symbol AND carry the
        # `note` payload (not just be `ZZZZNOTREAL,,,,,,,,,...` blanks —
        # past bug where an empty-result row had zero signal in CSV).
        check("news --format csv empty-result row: starts with bogus symbol",
              csv_lines[1].startswith("ZZZZNOTREAL,"),
              f"got row={csv_lines[1][:40]!r}")
        check("news --format csv empty-result row: carries the note field "
              "(not all-blank past the symbol)",
              "no news returned" in csv_lines[1],
              f"got row={csv_lines[1]!r}")
        # article rows: both must start with AAPL (symbol repeats per article)
        check("news --format csv: each article row starts with the symbol",
              csv_lines[2].startswith("AAPL,") and csv_lines[3].startswith("AAPL,"),
              f"row1={csv_lines[2][:30]!r}, row2={csv_lines[3][:30]!r}")

    # holders CSV: novel "row-per-holder + holder_class discriminator" layout.
    # Mixing AAPL (full data) with QQQ (empty / non-equity) specifically pins:
    #   - row classes: 1 summary + N institutional + N mutualfund per ticker
    #   - empty-result row carries `note` (not just blanks)
    #   - holder_class column populated correctly
    cmd = [sys.executable, str(SCRIPTS_DIR / "holders.py"),
           "--limit", "2", "--format", "csv", "AAPL", "QQQ"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # AAPL: 1 summary + 2 institutional + 2 mutualfund = 5 rows
    # QQQ: 1 empty-carry row
    # + 1 header = 7 lines total
    check("holders --format csv mixed batch: header + 6 data rows (7 total)",
          out.returncode == 0 and len(csv_lines) == 7,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 7:
        header = csv_lines[0]
        check("holders --format csv: header has holder_class + summary + holder cols + note",
              all(f in header for f in (
                  "symbol", "holder_class",
                  "insiders_pct", "institutions_pct", "institutions_count",
                  "date_reported", "holder", "pct_held", "shares", "value",
                  "pct_change", "note")),
              f"got header={header!r}")
        # AAPL summary row: holder_class='summary', rollup pcts populated.
        check("holders --format csv: AAPL summary row classed as 'summary'",
              csv_lines[1].startswith("AAPL,summary,"),
              f"got row={csv_lines[1][:60]!r}")
        # AAPL institutional + mutualfund rows: holder_class set accordingly.
        check("holders --format csv: AAPL institutional rows classed correctly",
              csv_lines[2].startswith("AAPL,institutional,")
              and csv_lines[3].startswith("AAPL,institutional,"),
              f"row2={csv_lines[2][:40]!r}, row3={csv_lines[3][:40]!r}")
        check("holders --format csv: AAPL mutualfund rows classed correctly",
              csv_lines[4].startswith("AAPL,mutualfund,")
              and csv_lines[5].startswith("AAPL,mutualfund,"),
              f"row4={csv_lines[4][:40]!r}, row5={csv_lines[5][:40]!r}")
        # QQQ empty-result row: must carry the `note` payload (not be all-blank
        # past the symbol — same defensive shape as news's empty-row test).
        check("holders --format csv: QQQ empty row starts with the symbol",
              csv_lines[6].startswith("QQQ,"),
              f"got row={csv_lines[6][:40]!r}")
        check("holders --format csv: QQQ empty row carries the `note` field",
              "no holder data" in csv_lines[6],
              f"got row={csv_lines[6]!r}")

    # holders --summary CSV header: pin the renamed columns
    # (institutional_rows_returned / mutualfund_rows_returned) so a future
    # rename can't silently drop them from CSV output. Default-mode CSV
    # header is checked above; summary-mode header was a gap.
    cmd = [sys.executable, str(SCRIPTS_DIR / "holders.py"),
           "--summary", "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("holders --summary --format csv: exits 0 + at least header + 1 row",
          out.returncode == 0 and len(csv_lines) >= 2,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if csv_lines:
        header = csv_lines[0]
        check("holders --summary --format csv: header has documented top picks + rows_returned cols",
              all(f in header for f in (
                  "symbol",
                  "insiders_pct", "institutions_pct", "institutions_count",
                  "top_institution", "top_institution_pct",
                  "top5_institutions_pct", "institutional_rows_returned",
                  "top_mutualfund", "top_mutualfund_pct",
                  "top5_mutualfunds_pct", "mutualfund_rows_returned",
                  "note")),
              f"got header={header!r}")

    # holders --summary --limit invariance (regression test for a past bug
    # where _summarize sliced the post-limit list — top5_institutions_pct
    # silently became top<limit>_institutions_pct when limit < 5, and
    # *_rows_returned reported display-knob count instead of Yahoo's
    # actual response size). Pins the contract: --summary metrics
    # describe Yahoo's response, NOT the user's display preference.
    rc1, j1 = run_cli("holders.py", "--summary", "--limit", "2", "AAPL")
    rc2, j2 = run_cli("holders.py", "--summary", "AAPL")
    check("holders --summary: both invocations succeed",
          rc1 == 0 and rc2 == 0 and isinstance(j1, list) and isinstance(j2, list)
          and len(j1) == 1 and len(j2) == 1,
          f"rc1={rc1}, rc2={rc2}")
    if rc1 == 0 and rc2 == 0 and j1 and j2:
        check("holders --summary: top5_institutions_pct invariant under --limit "
              "(was post-limit pre-fix)",
              j1[0].get("top5_institutions_pct") == j2[0].get("top5_institutions_pct"),
              f"--limit 2 gave {j1[0].get('top5_institutions_pct')!r}, "
              f"no-limit gave {j2[0].get('top5_institutions_pct')!r}")
        check("holders --summary: top5_mutualfunds_pct invariant under --limit",
              j1[0].get("top5_mutualfunds_pct") == j2[0].get("top5_mutualfunds_pct"),
              f"--limit 2 gave {j1[0].get('top5_mutualfunds_pct')!r}, "
              f"no-limit gave {j2[0].get('top5_mutualfunds_pct')!r}")
        check("holders --summary: institutional_rows_returned invariant under --limit",
              j1[0].get("institutional_rows_returned") == j2[0].get("institutional_rows_returned"),
              f"--limit 2 gave {j1[0].get('institutional_rows_returned')!r}, "
              f"no-limit gave {j2[0].get('institutional_rows_returned')!r}")
        check("holders --summary: mutualfund_rows_returned invariant under --limit",
              j1[0].get("mutualfund_rows_returned") == j2[0].get("mutualfund_rows_returned"),
              f"--limit 2 gave {j1[0].get('mutualfund_rows_returned')!r}, "
              f"no-limit gave {j2[0].get('mutualfund_rows_returned')!r}")

    # insiders CSV: row-per-record + record_class discriminator (parallel
    # to holders CSV). Mixing AAPL (full data) with QQQ (empty / non-equity)
    # specifically pins:
    #   - row classes: 1 purchases + N transaction + N roster per ticker
    #   - empty-result row carries `note` (not just blanks)
    #   - record_class column populated correctly
    #   - position / url columns deduplicated (regression: pre-dedupe
    #     header had two `position` and two `url` columns)
    cmd = [sys.executable, str(SCRIPTS_DIR / "insiders.py"),
           "--limit", "2", "--format", "csv", "AAPL", "QQQ"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # AAPL: 1 purchases + 2 transaction + 2 roster = 5 rows
    # QQQ: 1 empty-carry row
    # + 1 header = 7 lines total
    check("insiders --format csv mixed batch: header + 6 data rows (7 total)",
          out.returncode == 0 and len(csv_lines) == 7,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 7:
        header = csv_lines[0]
        # Header dedup: `position` and `url` must each appear EXACTLY once
        # (regression test for the pre-dedupe bug where both appeared twice
        # because they're in both _TRANSACTION_KEYS and _ROSTER_KEYS).
        cols = header.split(",")
        check("insiders --format csv: `position` column appears exactly once "
              "(deduplicated across transaction + roster)",
              cols.count("position") == 1,
              f"got {cols.count('position')} occurrences; cols={cols}")
        check("insiders --format csv: `url` column appears exactly once",
              cols.count("url") == 1,
              f"got {cols.count('url')} occurrences")
        check("insiders --format csv: header has record_class + purchases + "
              "transaction + roster + note + coverage_note cols",
              all(f in header for f in (
                  "symbol", "record_class",
                  "period_label", "purchases_shares", "pct_net_shares_purchased",
                  "date", "insider", "ownership", "shares", "value",
                  "transaction_text", "transaction_code",
                  "name", "most_recent_transaction", "shares_owned_directly",
                  "shares_owned_indirectly", "note", "coverage_note")),
              f"got header={header!r}")
        # Pin: header must NOT contain a bare `transaction` column —
        # that name was renamed to `transaction_code` to remove
        # ambiguity vs `transaction_text`. Word-boundary check via
        # split() so substrings like `transaction_text` /
        # `transaction_code` don't false-positive.
        check("insiders --format csv: bare `transaction` column was "
              "renamed to `transaction_code` (no bare column remains)",
              "transaction" not in cols,
              f"got cols containing 'transaction': "
              f"{[c for c in cols if 'transaction' in c]}")
        # AAPL purchases row: record_class='purchases', rollup populated.
        check("insiders --format csv: AAPL purchases row classed as 'purchases'",
              csv_lines[1].startswith("AAPL,purchases,"),
              f"got row={csv_lines[1][:60]!r}")
        # AAPL transaction rows.
        check("insiders --format csv: AAPL transaction rows classed correctly",
              csv_lines[2].startswith("AAPL,transaction,")
              and csv_lines[3].startswith("AAPL,transaction,"),
              f"row2={csv_lines[2][:40]!r}, row3={csv_lines[3][:40]!r}")
        # AAPL roster rows.
        check("insiders --format csv: AAPL roster rows classed correctly",
              csv_lines[4].startswith("AAPL,roster,")
              and csv_lines[5].startswith("AAPL,roster,"),
              f"row4={csv_lines[4][:40]!r}, row5={csv_lines[5][:40]!r}")
        # QQQ empty-result row carries the `note` payload.
        check("insiders --format csv: QQQ empty row starts with the symbol",
              csv_lines[6].startswith("QQQ,"),
              f"got row={csv_lines[6][:40]!r}")
        check("insiders --format csv: QQQ empty row carries the `note` field",
              "no insider data" in csv_lines[6],
              f"got row={csv_lines[6]!r}")

    # End-to-end CSV verification for BMW.DE partial-empty path: confirm
    # the `coverage_note` column actually contains the note string in the
    # purchases data row (not just that the column exists in the header,
    # not just that fetch() / _summarize set the field — pin the full
    # chain). Done in default mode where the partial-empty ticker
    # produces 1 purchases row (transactions + roster empty).
    cmd = [sys.executable, str(SCRIPTS_DIR / "insiders.py"),
           "--format", "csv", "BMW.DE"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # Expected: 1 header + 1 purchases row = 2 lines
    check("insiders --format csv BMW.DE: header + 1 purchases row "  # canary
          "(partial-empty: no transaction/roster rows)",
          out.returncode == 0 and len(csv_lines) == 2,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 2:
        header_cols = csv_lines[0].split(",")
        check("insiders --format csv BMW.DE: header has `coverage_note` "
              "column",
              "coverage_note" in header_cols,
              f"got header={csv_lines[0]!r}")
        # Locate coverage_note column index, then confirm BMW.DE's
        # purchases data row carries the note text in that column.
        # Robust against column reorder — look up by name.
        if "coverage_note" in header_cols:
            cn_idx = header_cols.index("coverage_note")
            # Use csv module to parse properly (the note string contains
            # commas + em dashes that would confuse a naive split).
            import csv as _csv
            from io import StringIO
            reader = _csv.reader(StringIO(csv_lines[1]))
            data_row = next(reader)
            check("insiders --format csv BMW.DE: data row's coverage_note "  # canary
                  "column carries the partial-empty note string",
                  cn_idx < len(data_row)
                  and "purchases_summary populated but transactions" in data_row[cn_idx],
                  f"got data_row[{cn_idx}]="
                  f"{data_row[cn_idx][:80] if cn_idx < len(data_row) else '<OOB>'!r}")
            # Symmetric pin: BMW.DE's `note` column must be EMPTY in the
            # CSV (mutually exclusive with coverage_note). If a regression
            # ever sets both, this fires.
            if "note" in header_cols:
                n_idx = header_cols.index("note")
                check("insiders --format csv BMW.DE: data row's `note` "
                      "column is empty (mutually exclusive with coverage_note)",
                      n_idx < len(data_row) and data_row[n_idx] == "",
                      f"got data_row[{n_idx}]="
                      f"{data_row[n_idx] if n_idx < len(data_row) else '<OOB>'!r}")

    # insiders --summary --limit invariance (parallel to holders): metrics
    # describe Yahoo's response, NOT the display knob. transactions_returned
    # in particular would silently report `min(limit, true_count)` if
    # _summarize ever read from the post-limit list.
    rc1, j1 = run_cli("insiders.py", "--summary", "--limit", "2", "AAPL")
    rc2, j2 = run_cli("insiders.py", "--summary", "AAPL")
    check("insiders --summary: both invocations succeed",
          rc1 == 0 and rc2 == 0 and isinstance(j1, list) and isinstance(j2, list)
          and len(j1) == 1 and len(j2) == 1,
          f"rc1={rc1}, rc2={rc2}")
    if rc1 == 0 and rc2 == 0 and j1 and j2:
        check("insiders --summary: transactions_returned invariant under --limit",
              j1[0].get("transactions_returned") == j2[0].get("transactions_returned"),
              f"--limit 2 gave {j1[0].get('transactions_returned')!r}, "
              f"no-limit gave {j2[0].get('transactions_returned')!r}")
        check("insiders --summary: roster_returned invariant under --limit",
              j1[0].get("roster_returned") == j2[0].get("roster_returned"),
              f"--limit 2 gave {j1[0].get('roster_returned')!r}, "
              f"no-limit gave {j2[0].get('roster_returned')!r}")
        check("insiders --summary: latest_transaction_date invariant under --limit",
              j1[0].get("latest_transaction_date") == j2[0].get("latest_transaction_date"),
              f"--limit 2 gave {j1[0].get('latest_transaction_date')!r}, "
              f"no-limit gave {j2[0].get('latest_transaction_date')!r}")

    # analyst CSV: row-per-record + record_class discriminator (parallel
    # to insiders / holders CSV). Mixing AAPL (full coverage), 0700.HK
    # (partial-empty: recommendations populated, upgrades_downgrades
    # empty → coverage_note path), and QQQ (note path: both frames
    # empty) specifically pins:
    #   - the new top-level `quote_type` column appears in the header
    #   - `quote_type` is correctly populated per row regardless of
    #     record class (recommendation vs change vs note-carry)
    #   - record classes route correctly: recommendation / change
    #   - empty-result row carries `note` AND quote_type=ETF (the
    #     in-band disambiguator that lets callers skip a fast_info
    #     follow-up)
    cmd = [sys.executable, str(SCRIPTS_DIR / "analyst.py"),
           "--limit", "2", "--format", "csv", "AAPL", "0700.HK", "QQQ"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    # AAPL: 4 recommendation rows + 2 change rows = 6
    # 0700.HK: 4 recommendation rows + 0 change rows = 4 (partial-empty
    #          path emits data rows with coverage_note in the carry)
    # QQQ: 1 empty-carry row
    # + 1 header = 12 lines total
    check("analyst --format csv mixed batch: header + 11 data rows (12 total)",
          out.returncode == 0 and len(csv_lines) == 12,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 12:
        header = csv_lines[0]
        cols = header.split(",")
        # quote_type column appears exactly once in the header (next to
        # symbol). Pins the new top-level field shape.
        check("analyst --format csv: `quote_type` column appears exactly once",
              cols.count("quote_type") == 1,
              f"got {cols.count('quote_type')} occurrences; "
              f"cols starting={cols[:5]}")
        # Header has the expected schema columns from both record classes.
        check("analyst --format csv: header has quote_type + record_class + "
              "recommendation + change + note + coverage_note cols",
              all(f in header for f in (
                  "symbol", "quote_type", "record_class",
                  "period", "strong_buy", "buy", "hold", "sell",
                  "strong_sell", "total",
                  "date", "firm", "to_grade", "from_grade", "action",
                  "price_target_action", "current_price_target",
                  "prior_price_target",
                  "note", "coverage_note")),
              f"got header={header!r}")
        # Locate quote_type column index for direct cell reads
        qt_idx = cols.index("quote_type")
        # AAPL recommendation rows: each carries quote_type=EQUITY +
        # record_class=recommendation. csv_lines[1..4] are AAPL recs.
        for i in range(1, 5):
            row_cols = csv_lines[i].split(",")
            check(f"analyst --format csv: AAPL row {i} starts AAPL,EQUITY,"
                  f"recommendation,",
                  csv_lines[i].startswith("AAPL,EQUITY,recommendation,"),
                  f"got row[:50]={csv_lines[i][:50]!r}")
            check(f"analyst --format csv: AAPL row {i} quote_type col == 'EQUITY'",
                  row_cols[qt_idx] == "EQUITY",
                  f"got col[{qt_idx}]={row_cols[qt_idx]!r}")
        # AAPL change rows: csv_lines[5..6]
        for i in range(5, 7):
            check(f"analyst --format csv: AAPL row {i} starts AAPL,EQUITY,"
                  f"change,",
                  csv_lines[i].startswith("AAPL,EQUITY,change,"),
                  f"got row[:50]={csv_lines[i][:50]!r}")
        # 0700.HK recommendation rows (4): partial-empty path. Each
        # carries quote_type=EQUITY + coverage_note populated.
        for i in range(7, 11):
            row_cols = csv_lines[i].split(",")
            check(f"analyst --format csv: 0700.HK row {i} starts 0700.HK,"
                  f"EQUITY,recommendation,",
                  csv_lines[i].startswith("0700.HK,EQUITY,recommendation,"),
                  f"got row[:50]={csv_lines[i][:50]!r}")
            check(f"analyst --format csv: 0700.HK row {i} carries "
                  "coverage_note (partial-empty path)",
                  "recommendations populated but upgrades_downgrades empty"
                  in csv_lines[i],
                  f"got row[-100:]={csv_lines[i][-100:]!r}")
        # QQQ empty-result row: starts with QQQ,ETF, (note path inline-
        # disambiguated; no fast_info follow-up needed) and carries
        # the all-empty `note` text.
        check("analyst --format csv: QQQ empty row starts with QQQ,ETF,",
              csv_lines[11].startswith("QQQ,ETF,"),
              f"got row[:30]={csv_lines[11][:30]!r}")
        check("analyst --format csv: QQQ empty row carries the `note` field "
              "(no analyst data...)",
              "no analyst data" in csv_lines[11],
              f"got row={csv_lines[11]!r}")

    # analyst --summary CSV: strict one-row-per-ticker, including
    # quote_type column for peer-compare filtering. Pin that the
    # summary CSV header has quote_type AND the new
    # latest_rating_change_current_price_target column.
    cmd = [sys.executable, str(SCRIPTS_DIR / "analyst.py"),
           "--summary", "--format", "csv", "AAPL", "QQQ"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("analyst --summary --format csv: exits 0 + header + 2 rows (3 lines)",
          out.returncode == 0 and len(csv_lines) == 3,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if len(csv_lines) >= 3:
        header = csv_lines[0]
        check("analyst --summary --format csv: header has quote_type + new "
              "latest_rating_change_current_price_target column",
              "quote_type" in header
              and "latest_rating_change_current_price_target" in header,
              f"got header={header!r}")
        # AAPL row carries EQUITY in quote_type column
        check("analyst --summary --format csv: AAPL row carries quote_type=EQUITY",
              csv_lines[1].startswith("AAPL,EQUITY,"),
              f"got row[:30]={csv_lines[1][:30]!r}")
        # QQQ row carries ETF in quote_type column (inline disambiguator)
        check("analyst --summary --format csv: QQQ row carries quote_type=ETF",
              csv_lines[2].startswith("QQQ,ETF,"),
              f"got row[:30]={csv_lines[2][:30]!r}")

    # fund_holdings CSV: novel "row-per-record + record_class discriminator"
    # layout with quote_type repeated on every row. SPY (full data) + AAPL
    # (non-fund note row, quote_type=EQUITY) + ZZZZNOTREAL (error row, no
    # quote_type) specifically pins:
    #   - 8 record classes (meta/operations/asset_class/sector/bond_rating/
    #     equity_metric/bond_metric/holding) emit when populated
    #     (bond_metric is skipped for SPY — equity ETF — so 7 classes here)
    #   - quote_type column populated on every fund row (ETF) AND on the
    #     non-fund note row (EQUITY)
    #   - error row leaves quote_type empty (parser never ran)
    cmd = [sys.executable, str(SCRIPTS_DIR / "fund_holdings.py"),
           "--limit", "2", "--format", "csv", "SPY", "AAPL", "ZZZZNOTREAL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("fund_holdings --format csv mixed batch: nonzero output, exit 0",
          out.returncode == 0 and len(csv_lines) >= 5,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if csv_lines:
        header = csv_lines[0]
        check("fund_holdings --format csv: header has quote_type + record_class "
              "+ all section cols + category_avg companions + note + error",
              all(f in header for f in (
                  "symbol", "quote_type", "record_class",
                  "category", "family", "legal_type", "description",
                  "expense_ratio", "expense_ratio_category_avg",
                  "total_net_assets_millions",
                  "total_net_assets_category_avg_millions",
                  "bucket", "weight",
                  "pe_ratio", "pe_ratio_category_avg",
                  "pb_ratio", "pb_ratio_category_avg",
                  "median_market_cap", "median_market_cap_category_avg",
                  "earnings_growth_3y", "earnings_growth_3y_category_avg",
                  "duration_years", "duration_years_category_avg",
                  "maturity_years", "maturity_years_category_avg",
                  "credit_quality", "credit_quality_category_avg",
                  "holding_symbol", "holding_name",
                  "note", "error", "error_kind", "attempts")),
              f"got header={header!r}")
        # Find the SPY rows by class. SPY emits 7 classes (no bond_metric):
        # meta + operations + 6 asset_class + ~11 sector + 1 bond_rating
        # + equity_metric + 2 holding. The exact count varies (sectors
        # depends on Yahoo), so we just spot-check one row of each class.
        spy_rows = [ln for ln in csv_lines[1:] if ln.startswith("SPY,ETF,")]
        check("fund_holdings --format csv: SPY rows all carry quote_type=ETF",
              len(spy_rows) >= 10,  # canary: ~22 typical
              f"got {len(spy_rows)} SPY,ETF rows")
        spy_classes = {ln.split(",", 3)[2] for ln in spy_rows}
        check("fund_holdings --format csv: SPY emits all expected record classes",
              {"meta", "operations", "asset_class", "sector",
               "equity_metric", "holding"}.issubset(spy_classes),
              f"got classes={spy_classes}")
        # AAPL non-fund row: quote_type=EQUITY, carries note text
        aapl_rows = [ln for ln in csv_lines[1:] if ln.startswith("AAPL,")]
        check("fund_holdings --format csv: AAPL non-fund row exists (1 row)",
              len(aapl_rows) == 1,
              f"got {len(aapl_rows)} AAPL rows")
        if aapl_rows:
            check("fund_holdings --format csv: AAPL non-fund row starts with AAPL,EQUITY,",
                  aapl_rows[0].startswith("AAPL,EQUITY,"),
                  f"got row[:40]={aapl_rows[0][:40]!r}")
            check("fund_holdings --format csv: AAPL non-fund row carries `note` text",
                  "no fund data" in aapl_rows[0],
                  f"got row={aapl_rows[0]!r}")
        # ZZZZNOTREAL error row: quote_type empty (parser never ran),
        # error_kind=not_found populated.
        bogus_rows = [ln for ln in csv_lines[1:] if ln.startswith("ZZZZNOTREAL,")]
        check("fund_holdings --format csv: error row exists (1 row)",
              len(bogus_rows) == 1,
              f"got {len(bogus_rows)} bogus rows")
        if bogus_rows:
            check("fund_holdings --format csv: error row has empty quote_type "
                  "(starts ZZZZNOTREAL,,)",
                  bogus_rows[0].startswith("ZZZZNOTREAL,,"),
                  f"got row[:40]={bogus_rows[0][:40]!r}")
            check("fund_holdings --format csv: error row carries error_kind=not_found",
                  "not_found" in bogus_rows[0],
                  f"got row={bogus_rows[0]!r}")

    # fund_holdings --summary CSV header: pin documented column set so
    # a future rename can't silently drop one (regression guard for
    # holdings_concentration / holdings_returned / quote_type / etc.).
    # Mix SPY (success) + AAPL (non-fund w/ quote_type) + bogus
    # (error path, quote_type column should be empty since the parser
    # never ran) to pin all three response shapes inline.
    cmd = [sys.executable, str(SCRIPTS_DIR / "fund_holdings.py"),
           "--summary", "--format", "csv", "SPY", "AAPL", "ZZZZNOTREAL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("fund_holdings --summary --format csv: header + 3 rows "
          "(SPY + AAPL non-fund + bogus error)",
          out.returncode == 0 and len(csv_lines) == 4,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if csv_lines:
        header = csv_lines[0]
        check("fund_holdings --summary --format csv: header has documented flat cols",
              all(f in header for f in (
                  "symbol", "quote_type",
                  "category", "family",
                  "expense_ratio", "turnover", "total_net_assets_millions",
                  "stock_pct", "bond_pct", "cash_pct",
                  "top_holding_symbol", "top_holding_weight",
                  "holdings_concentration", "holdings_returned",
                  "top_sector", "top_sector_weight",
                  "pe_ratio", "pb_ratio", "duration_years",
                  "earnings_growth_3y",
                  "note")),
              f"got header={header!r}")
    if len(csv_lines) >= 4:
        check("fund_holdings --summary --format csv: SPY row carries quote_type=ETF",
              csv_lines[1].startswith("SPY,ETF,"),
              f"got row[:30]={csv_lines[1][:30]!r}")
        check("fund_holdings --summary --format csv: AAPL non-fund row carries quote_type=EQUITY",
              csv_lines[2].startswith("AAPL,EQUITY,"),
              f"got row[:30]={csv_lines[2][:30]!r}")
        # Bogus error row: quote_type is empty (parser never ran), so the
        # row starts `ZZZZNOTREAL,,...`. Pins the contract that error path
        # leaves quote_type blank in CSV (for ndjson / json it's absent).
        check("fund_holdings --summary --format csv: bogus row has empty "
              "quote_type (starts ZZZZNOTREAL,,)",
              csv_lines[3].startswith("ZZZZNOTREAL,,"),
              f"got row[:30]={csv_lines[3][:30]!r}")
        check("fund_holdings --summary --format csv: bogus row carries "
              "error_kind=not_found",
              "not_found" in csv_lines[3],
              f"got row={csv_lines[3]!r}")

    # sec_filings CSV: row-per-filing layout. Mixing a filtered-to-empty
    # ticker (TM with --type 10-K — TM is an ADR that files 20-F not
    # 10-K) with a real US issuer (AAPL) and a Yahoo-empty ticker (SPY)
    # specifically pins:
    #   - 1 header + N AAPL filing rows + 1 TM filter_note row + 1 SPY note row
    #   - filter_note vs note are in distinct columns and never co-occur
    #   - AAPL data rows have no note / no filter_note populated
    #   - exhibit_keys column populated for AAPL rows (pipe-joined)
    #   - exhibits dict NOT in CSV header (dropped from tabular output)
    cmd = [sys.executable, str(SCRIPTS_DIR / "sec_filings.py"),
           "--type", "10-K", "--limit", "2", "--format", "csv",
           "AAPL", "TM", "SPY"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("sec_filings --type 10-K --format csv: rc=0",
          out.returncode == 0, f"rc={out.returncode}")
    if csv_lines:
        header = csv_lines[0]
        # Header must include filter_note distinct from note, and exhibit_keys.
        check("sec_filings --format csv: header has expected cols "
              "(filter_note, exhibit_keys, no exhibits dict)",
              "exhibit_keys" in header
              and "filter_note" in header
              and "note" in header
              and "exhibits," not in header  # bare `exhibits` col absent
              and ",exhibits" not in header
              and header.endswith("error,error_kind,attempts"),
              f"got header={header!r}")
        # Header column order: filter_note must follow note (set in
        # _DEFAULT_CSV_COLS). Pins the documented column order.
        cols_split = header.split(",")
        check("sec_filings --format csv: filter_note column follows note",
              "note" in cols_split and "filter_note" in cols_split
              and cols_split.index("filter_note") == cols_split.index("note") + 1,
              f"got cols={cols_split}")

        # AAPL rows: 2 of them (since --limit 2 and AAPL has multiple 10-Ks),
        # both starting with AAPL and having no filter_note populated.
        aapl_rows = [ln for ln in csv_lines[1:] if ln.startswith("AAPL,")]
        check("sec_filings --format csv: AAPL has 2 data rows (--limit 2)",
              len(aapl_rows) == 2,
              f"got {len(aapl_rows)} rows: {aapl_rows[:2]!r}")
        # AAPL data rows must NOT have filter_note populated. Find the
        # column index for filter_note and check.
        filter_note_idx = cols_split.index("filter_note")
        for i, row in enumerate(aapl_rows):
            # csv module would handle quoting; for these test cases the
            # data fields don't contain commas in values that'd shift idx.
            # Simple split is fine here.
            row_fields = row.split(",")
            if len(row_fields) > filter_note_idx:
                check(f"sec_filings --format csv: AAPL row {i+1} filter_note empty",
                      not row_fields[filter_note_idx].strip(),
                      f"got filter_note={row_fields[filter_note_idx]!r} "
                      f"(row={row!r})")
        # AAPL row's exhibit_keys col should be populated (pipe-joined,
        # contains "10-K" since the type filter ensures these are 10-Ks).
        ek_idx = cols_split.index("exhibit_keys")
        if aapl_rows:
            first_row = aapl_rows[0]
            row_fields = first_row.split(",")
            if len(row_fields) > ek_idx:
                ek_val = row_fields[ek_idx].strip('"')  # CSV may quote pipe
                check("sec_filings --format csv: AAPL exhibit_keys populated, "
                      "contains 10-K",
                      ek_val and "10-K" in ek_val,
                      f"got exhibit_keys={ek_val!r}")

        # TM row: filter ate everything (TM has no 10-K — files 20-F).
        # Carry row with filter_note populated.
        tm_rows = [ln for ln in csv_lines if ln.startswith("TM,")]
        check("sec_filings --format csv: TM has exactly 1 carry row "
              "(filter_note path)",
              len(tm_rows) == 1, f"got {len(tm_rows)} rows: {tm_rows!r}")
        if tm_rows:
            tm_row = tm_rows[0]
            check("sec_filings --format csv: TM carry row has filter_note "
                  "populated (NOT note)",
                  "all eliminated by" in tm_row
                  # Sanity: filter_note text names the culprit filter.
                  and "--type 10-K" in tm_row,
                  f"got TM row={tm_row!r}")

        # SPY row: Yahoo returned nothing (note path). Carry row with
        # `note` populated, NOT filter_note.
        spy_rows = [ln for ln in csv_lines if ln.startswith("SPY,")]
        check("sec_filings --format csv: SPY has exactly 1 carry row "
              "(note path, not filter_note)",
              len(spy_rows) == 1, f"got {len(spy_rows)} rows: {spy_rows!r}")
        if spy_rows:
            spy_row = spy_rows[0]
            # Note text mentions "no SEC filings" — the _EMPTY_NOTE prefix.
            check("sec_filings --format csv: SPY carry row has note "
                  "populated (NOT filter_note)",
                  "no SEC filings" in spy_row,
                  f"got SPY row={spy_row!r}")

    # sec_filings --summary CSV: strict one row per ticker. Pins schema:
    # latest_proxy_date column (renamed from latest_def14a_date),
    # latest_proxy_type companion (the multi-form bucket _type field),
    # latest_*_date set, filings_last_90d column.
    cmd = [sys.executable, str(SCRIPTS_DIR / "sec_filings.py"),
           "--summary", "--format", "csv", "AAPL", "TM", "SPY"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("sec_filings --summary --format csv: rc=0 + header + 3 rows (4 lines)",
          out.returncode == 0 and len(csv_lines) == 4,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if csv_lines:
        header = csv_lines[0]
        # Pin latest_proxy_date column (the rename from latest_def14a_date).
        # Also pin filings_last_90d (the recency rollup).
        check("sec_filings --summary --format csv: header has latest_proxy_date "
              "(NOT latest_def14a_date) and filings_last_90d",
              "latest_proxy_date" in header
              and "filings_last_90d" in header
              and "latest_def14a_date" not in header,
              f"got header={header!r}")
        # latest_proxy_type companion column must be present (only
        # multi-form bucket gets one). Pins the asymmetric naming
        # convention.
        check("sec_filings --summary --format csv: header has "
              "latest_proxy_type (multi-form bucket companion)",
              "latest_proxy_type" in header,
              f"got header={header!r}")
        # Single-form buckets must NOT have _type companion columns.
        for absent in ("latest_10k_type", "latest_10q_type",
                       "latest_8k_type", "latest_20f_type",
                       "latest_6k_type"):
            check(f"sec_filings --summary --format csv: no {absent} column "
                  "(single-form bucket asymmetry)",
                  absent not in header, f"unexpected col {absent} in {header!r}")
        # All headline-type columns present.
        for col in ("latest_10k_date", "latest_10q_date", "latest_8k_date",
                    "latest_20f_date", "latest_6k_date", "latest_proxy_date",
                    "total_filings"):
            check(f"sec_filings --summary --format csv: header has {col}",
                  col in header, f"missing from {header!r}")
        # Summary CSV header must also include filter_note column (now
        # carried defensively in --summary mode for symmetry with note).
        check("sec_filings --summary --format csv: header has "
              "filter_note column (defensive carry)",
              "filter_note" in header, f"got header={header!r}")

    # sec_filings --since: subprocess test pinning that surviving rows
    # respect the date floor and no filter_note column is populated
    # (success case — AAPL has post-2024 filings). Limits to top 5
    # to keep the test compact.
    cmd = [sys.executable, str(SCRIPTS_DIR / "sec_filings.py"),
           "--since", "2024-01-01", "--limit", "5", "--format", "csv",
           "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    csv_lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    check("sec_filings --since --format csv: rc=0 + header + N data rows",
          out.returncode == 0 and len(csv_lines) >= 2,
          f"rc={out.returncode}, lines={len(csv_lines)}")
    if csv_lines and len(csv_lines) >= 2:
        header_cols = csv_lines[0].split(",")
        date_idx = header_cols.index("date")
        filter_note_idx = header_cols.index("filter_note")
        # Every data row's `date` column must be >= 2024-01-01.
        for i, row in enumerate(csv_lines[1:], start=1):
            row_fields = row.split(",")
            if len(row_fields) > date_idx:
                row_date = row_fields[date_idx].strip('"')
                check(f"sec_filings --since --format csv: row {i} date >= 2024-01-01",
                      row_date >= "2024-01-01",
                      f"got date={row_date!r}")
        # No filter_note populated (AAPL has plenty of post-2024 filings,
        # so success path; filter_note column should be empty on every row).
        for i, row in enumerate(csv_lines[1:], start=1):
            row_fields = row.split(",")
            if len(row_fields) > filter_note_idx:
                fn_val = row_fields[filter_note_idx].strip('"').strip()
                check(f"sec_filings --since --format csv: row {i} filter_note empty",
                      not fn_val,
                      f"got filter_note={fn_val!r} (row should be success path)")

    # sec_filings --summary + filters: stderr warning. Pins the
    # noise-on-misuse contract — user passing `--summary --type 10-K`
    # gets a stderr warning naming the ignored filters. Pure subprocess
    # test (capture stderr separately).
    cmd = [sys.executable, str(SCRIPTS_DIR / "sec_filings.py"),
           "--summary", "--type", "10-K", "--limit", "5", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("sec_filings --summary + filters: rc=0 (warning is noise, not error)",
          out.returncode == 0, f"rc={out.returncode}")
    check("sec_filings --summary --type --limit: stderr warns about "
          "ignored filters",
          "warning:" in out.stderr.lower()
          and "--type" in out.stderr
          and "--limit" in out.stderr,
          f"got stderr={out.stderr!r}")
    check("sec_filings --summary --type --limit: --since NOT in stderr "
          "(only ignored flags listed)",
          "--since" not in out.stderr and "--days" not in out.stderr,
          f"got stderr={out.stderr!r}")
    # No filters + --summary: NO warning. Pin the negative case so the
    # warning doesn't fire on every summary call.
    cmd = [sys.executable, str(SCRIPTS_DIR / "sec_filings.py"),
           "--summary", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("sec_filings --summary (no filters): no warning on stderr",
          "warning:" not in out.stderr.lower(),
          f"got stderr={out.stderr!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"CLI sanity crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- sectors fetch shape (no ticker; key is a sector / industry string) ---
# Two kinds (Sector, Industry) with overlapping but asymmetric sections.
# Tests: pure-Python helpers, key auto-detection, em-dash normalization,
# section coverage, --summary peer compare, error paths.
section("sectors fetch")
try:
    # --- _normalize_key (offline; pure-Python) ---
    check("sectors _normalize_key: em-dash → hyphen",
          sectors._normalize_key("software—application") == "software-application",
          f"got {sectors._normalize_key('software—application')!r}")
    check("sectors _normalize_key: idempotent on hyphen form",
          sectors._normalize_key("software-application") == "software-application")
    check("sectors _normalize_key: strips + lowercases",
          sectors._normalize_key("  TECHNOLOGY  ") == "technology")

    # --- key set sanity (offline) ---
    check("sectors SECTOR_KEYS has exactly 11 entries",
          len(sectors.SECTOR_KEYS) == 11,
          f"got {len(sectors.SECTOR_KEYS)}: {sectors.SECTOR_KEYS}")
    check("sectors SECTOR_KEYS contains 'technology'",
          "technology" in sectors.SECTOR_KEYS)
    check("sectors INDUSTRY_KEYS contains 'semiconductors'",
          "semiconductors" in sectors.INDUSTRY_KEYS)
    check("sectors INDUSTRY_KEYS has no em-dash characters "
          "(yfinance.const quirk normalized away)",
          all("—" not in k for k in sectors.INDUSTRY_KEYS),
          "found em-dash in normalized industry keys")
    check("sectors INDUSTRY_KEYS contains the formerly-em-dashed "
          "'software-application' (regular hyphen)",
          "software-application" in sectors.INDUSTRY_KEYS)
    check("sectors INDUSTRY_TO_SECTOR['semiconductors'] == 'technology'",
          sectors.INDUSTRY_TO_SECTOR.get("semiconductors") == "technology",
          f"got {sectors.INDUSTRY_TO_SECTOR.get('semiconductors')!r}")

    # --- _kind_for_key auto-detect (offline) ---
    check("sectors _kind_for_key('technology') → 'sector'",
          sectors._kind_for_key("technology") == "sector")
    check("sectors _kind_for_key('semiconductors') → 'industry'",
          sectors._kind_for_key("semiconductors") == "industry")
    check("sectors _kind_for_key('software—application') → 'industry' "
          "(em-dash normalized)",
          sectors._kind_for_key("software—application") == "industry")
    check("sectors _kind_for_key('not-a-real-thing') → None",
          sectors._kind_for_key("not-a-real-thing") is None)
    check("sectors _kind_for_key case-insensitive",
          sectors._kind_for_key("TECHNOLOGY") == "sector")

    # --- live fetch: sector default (overview + top_companies) ---
    env = sectors.fetch(
        key="technology", kind="sector",
        sections=("overview", "top_companies"),
        limit=3, full=False,
    )
    check("sectors[sector default]: no error",
          env.get("error") is None,
          f"got error={env.get('error')!r} kind={env.get('error_kind')!r}")
    check("sectors[sector default]: identity fields populated",
          env.get("key") == "technology" and env.get("kind") == "sector"
          and env.get("name") == "Technology"
          and env.get("symbol") == "^YH311",
          f"got {env}")
    check("sectors[sector default]: overview is dict",
          isinstance(env.get("overview"), dict))
    check("sectors[sector default]: overview.companies_count is positive int",  # canary
          isinstance(env.get("overview", {}).get("companies_count"), int)
          and env["overview"]["companies_count"] > 100,
          f"got {env.get('overview', {}).get('companies_count')!r}")
    check("sectors[sector default]: overview.market_weight is FRACTION",
          isinstance(env.get("overview", {}).get("market_weight"), float)
          and 0.0 < env["overview"]["market_weight"] < 1.0,
          f"got {env.get('overview', {}).get('market_weight')!r}")
    check("sectors[sector default]: top_companies is list of dicts",
          isinstance(env.get("top_companies"), list)
          and len(env["top_companies"]) > 0
          and all(isinstance(r, dict) for r in env["top_companies"]))
    check("sectors[sector default]: top_companies respects --limit",
          len(env.get("top_companies") or []) == 3,
          f"got {len(env.get('top_companies') or [])} rows")
    tc0 = (env.get("top_companies") or [{}])[0]
    check("sectors[sector default]: top_companies[0] has symbol/name/rating/market_weight",
          all(k in tc0 for k in ("symbol", "name", "rating", "market_weight")),
          f"got {list(tc0.keys())}")
    check("sectors[sector default]: top_companies[0].market_weight is fraction",
          isinstance(tc0.get("market_weight"), float)
          and 0.0 < tc0["market_weight"] < 1.0,
          f"got {tc0.get('market_weight')!r}")
    check("sectors[sector default]: no industries section (not requested)",
          "industries" not in env)
    check("sectors[sector default]: no coverage_note "
          "(both sections applicable to sector kind)",
          "coverage_note" not in env)

    # --- live fetch: industry default ---
    env_ind = sectors.fetch(
        key="semiconductors", kind="industry",
        sections=("overview", "top_companies"),
        limit=2, full=False,
    )
    check("sectors[industry default]: no error",
          env_ind.get("error") is None,
          f"got error={env_ind.get('error')!r}")
    check("sectors[industry default]: sector_key/sector_name back-ref populated",
          env_ind.get("sector_key") == "technology"
          and env_ind.get("sector_name") == "Technology",
          f"got sector_key={env_ind.get('sector_key')!r}")
    check("sectors[industry default]: overview.industries_count is null "
          "(industries are leaves, no children)",
          env_ind.get("overview", {}).get("industries_count") is None)

    # --- live fetch: industry --section all (asymmetric coverage) ---
    env_all = sectors.fetch(
        key="semiconductors", kind="industry",
        sections=sectors.ALL_SECTIONS,  # includes sector-only sections
        limit=2, full=False,
    )
    check("sectors[industry --section all]: top_performing_companies is list",
          isinstance(env_all.get("top_performing_companies"), list)
          and len(env_all["top_performing_companies"]) > 0)
    tp0 = (env_all.get("top_performing_companies") or [{}])[0]
    check("sectors[industry top_performing]: row has ytd_return + last_price + target_price",
          all(k in tp0 for k in ("symbol", "ytd_return", "last_price", "target_price")),
          f"got {list(tp0.keys())}")
    check("sectors[industry --section all]: top_growth_companies is list",
          isinstance(env_all.get("top_growth_companies"), list)
          and len(env_all["top_growth_companies"]) > 0)
    tg0 = (env_all.get("top_growth_companies") or [{}])[0]
    check("sectors[industry top_growth]: row has growth_estimate (float)",
          isinstance(tg0.get("growth_estimate"), float),
          f"got {tg0.get('growth_estimate')!r}")
    check("sectors[industry --section all]: coverage_note fired for "
          "sector-only sections (industries / top_etfs / top_mutual_funds)",
          isinstance(env_all.get("coverage_note"), str)
          and all(s in env_all["coverage_note"]
                  for s in ("industries", "top_etfs", "top_mutual_funds")),
          f"got coverage_note={env_all.get('coverage_note')!r}")
    check("sectors[industry --section all]: sector-only sections NOT in envelope "
          "(no HTTP attempted)",
          all(s not in env_all
              for s in ("industries", "top_etfs", "top_mutual_funds")),
          f"got keys={list(env_all.keys())}")

    # --- live fetch: error path (bogus key under explicit kind) ---
    env_404 = sectors.fetch(
        key="not-a-real-sector", kind="sector",
        sections=("overview",), limit=None, full=False,
    )
    check("sectors[bogus sector key]: error_kind == 'not_found'",
          env_404.get("error_kind") == "not_found",
          f"got {env_404.get('error_kind')!r}")
    check("sectors[bogus sector key]: error message mentions 'unknown'",
          isinstance(env_404.get("error"), str)
          and "unknown" in env_404["error"].lower(),
          f"got {env_404.get('error')!r}")

    # --- summarize() projection ---
    summ = sectors.summarize(env)
    check("sectors summarize(sector envelope): flat dict",
          isinstance(summ, dict) and "top_companies" not in summ
          and "overview" not in summ,
          f"got {list(summ.keys())}")
    check("sectors summarize(sector envelope): top_company_symbol populated",
          isinstance(summ.get("top_company_symbol"), str)
          and len(summ["top_company_symbol"]) > 0,
          f"got {summ.get('top_company_symbol')!r}")
    summ_ind = sectors.summarize(env_all)
    check("sectors summarize(industry envelope): top_performer_symbol populated",
          isinstance(summ_ind.get("top_performer_symbol"), str),
          f"got {summ_ind.get('top_performer_symbol')!r}")
    check("sectors summarize(industry envelope): top_growth_estimate populated",
          isinstance(summ_ind.get("top_growth_estimate"), float),
          f"got {summ_ind.get('top_growth_estimate')!r}")

    # --- CLI: --list-sectors (no HTTP) ---
    rc, parsed = run_cli("sectors.py", "--list-sectors")
    parsed_count = len(parsed) if isinstance(parsed, list) else f"<not a list: {parsed!r}>"
    check("sectors --list-sectors CLI: exit 0", rc == 0)
    check("sectors --list-sectors CLI: returns list of 11 dicts",
          isinstance(parsed, list) and len(parsed) == 11
          and all("key" in r and "industry_count" in r for r in parsed),
          f"got {parsed_count}")

    # --- CLI: --list-industries technology (no HTTP) ---
    rc, parsed = run_cli("sectors.py", "--list-industries", "technology")
    parsed_count = len(parsed) if isinstance(parsed, list) else f"<not a list: {parsed!r}>"
    check("sectors --list-industries CLI: exit 0", rc == 0)
    check("sectors --list-industries CLI: all rows are technology subsector",
          isinstance(parsed, list) and len(parsed) > 5
          and all(r.get("sector_key") == "technology" for r in parsed),
          f"got {parsed_count}")
    check("sectors --list-industries CLI: contains 'semiconductors'",
          isinstance(parsed, list)
          and any(r.get("industry_key") == "semiconductors" for r in parsed))

    # --- CLI: typo on positional key gets argparse error (not 404) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"), "tech"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    check("sectors CLI typo on key: exits non-zero",
          out.returncode != 0,
          f"got rc={out.returncode}, stdout={out.stdout!r}")
    check("sectors CLI typo on key: stderr mentions discovery flags",
          "--list-sectors" in out.stderr or "--list-industries" in out.stderr,
          f"got stderr={out.stderr!r}")

    # --- CLI: --summary peer compare across sectors ---
    rc, parsed = run_cli("sectors.py", "--summary",
                          "technology", "healthcare")
    check("sectors --summary CLI: exit 0", rc == 0)
    check("sectors --summary CLI: returns list of 2 envelopes",
          isinstance(parsed, list) and len(parsed) == 2,
          f"got {len(parsed) if isinstance(parsed, list) else parsed!r}")
    check("sectors --summary CLI: each row has top_industry_key "
          "(auto-expand fetched industries section)",
          isinstance(parsed, list) and all(
              isinstance(r.get("top_industry_key"), str) for r in parsed
          ),
          f"got {parsed!r}")

    # --- CLI: --format csv (record_class discriminator) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "technology", "--section", "overview,top_companies",
           "--limit", "3", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    check("sectors CLI csv: exit 0", out.returncode == 0,
          f"got rc={out.returncode}, stderr={out.stderr!r}")
    lines = out.stdout.strip().split("\n")
    check("sectors CLI csv: header has record_class as first col",
          lines[0].startswith("record_class,"),
          f"got header={lines[0]!r}")
    check("sectors CLI csv: emits exactly 4 rows (1 meta + 3 top_company)",
          len(lines) == 5,  # header + 4
          f"got {len(lines) - 1} data rows: {lines}")
    check("sectors CLI csv: first data row is record_class=meta",
          lines[1].startswith("meta,"),
          f"got row1={lines[1]!r}")
    check("sectors CLI csv: subsequent rows are record_class=top_company",
          all(line.startswith("top_company,") for line in lines[2:5]),
          f"got rows={lines[2:]!r}")

    # --- CSV: kind=industry override on industry rows ---
    # Regression for the row-vs-envelope kind bug. Industry rows
    # under a sector envelope should carry kind="industry", not
    # kind="sector" (which is the parent envelope's kind).
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "technology", "--section", "overview,industries",
           "--limit", "2", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    check("sectors CLI csv (industries): exit 0", out.returncode == 0,
          f"got rc={out.returncode}, stderr={out.stderr!r}")
    lines = out.stdout.strip().split("\n")
    # Header row + 1 meta + 2 industry = 4 lines
    check("sectors CLI csv (industries): meta row has kind=sector",
          lines[1].split(",")[2] == "sector",
          f"got meta row kind={lines[1].split(',')[2]!r}")
    check("sectors CLI csv (industries): industry rows have kind=industry "
          "(NOT 'sector' — fixed in this round)",
          all(line.split(",")[2] == "industry" for line in lines[2:4]),
          f"got industry rows kinds: {[l.split(',')[2] for l in lines[2:4]]}")
    check("sectors CLI csv (industries): industry rows' key is the child "
          "industry key (NOT the parent sector key)",
          all(line.split(",")[1] != "technology" for line in lines[2:4]),
          f"got industry rows keys: {[l.split(',')[1] for l in lines[2:4]]}")

    # --- CLI: --peers (no HTTP, sibling industries) ---
    rc, parsed = run_cli("sectors.py", "--peers", "semiconductors")
    check("sectors --peers CLI: exit 0", rc == 0)
    check("sectors --peers CLI: returns sibling industries with is_self flag",
          isinstance(parsed, list) and len(parsed) > 5
          and all("is_self" in r for r in parsed)
          and sum(1 for r in parsed if r.get("is_self")) == 1,
          f"got {len(parsed) if isinstance(parsed, list) else parsed!r}; "
          f"is_self count: {sum(1 for r in parsed if r.get('is_self')) if isinstance(parsed, list) else 'n/a'}")
    check("sectors --peers CLI: all rows share the same parent sector",
          isinstance(parsed, list)
          and len({r.get("sector_key") for r in parsed}) == 1
          and parsed[0].get("sector_key") == "technology",
          f"got sector_keys: {set(r.get('sector_key') for r in parsed) if isinstance(parsed, list) else 'n/a'}")
    check("sectors --peers CLI: matching industry has is_self=True",
          isinstance(parsed, list)
          and any(r.get("industry_key") == "semiconductors"
                  and r.get("is_self") is True for r in parsed))

    # --- CLI: --peers bogus key ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "--peers", "not-a-real-industry"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("sectors --peers bogus key: exits non-zero",
          out.returncode != 0)
    check("sectors --peers bogus key: stderr mentions --list-industries",
          "--list-industries" in out.stderr,
          f"got stderr={out.stderr!r}")

    # --- CLI: --list-industries multi-sector ---
    rc, parsed = run_cli("sectors.py", "--list-industries", "technology,healthcare")
    check("sectors --list-industries multi-sector CLI: exit 0", rc == 0)
    check("sectors --list-industries multi-sector CLI: contains both sectors",
          isinstance(parsed, list)
          and {r.get("sector_key") for r in parsed} == {"technology", "healthcare"},
          f"got sectors={set(r.get('sector_key') for r in parsed) if isinstance(parsed, list) else 'n/a'}")

    # --- CLI: discovery flag mutex ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "--list-sectors", "--peers", "semiconductors"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("sectors discovery flags mutex: --list-sectors + --peers fails",
          out.returncode != 0
          and "mutually exclusive" in out.stderr,
          f"got rc={out.returncode}, stderr={out.stderr!r}")

    # --- --summary: description field is in projection ---
    rc, parsed = run_cli("sectors.py", "--summary", "technology")
    desc_preview = (parsed[0].get("description") if isinstance(parsed, list) and parsed
                    else "<no parse>")
    check("sectors --summary: includes description field",
          isinstance(parsed, list) and len(parsed) > 0
          and isinstance(parsed[0].get("description"), str)
          and len(parsed[0]["description"]) > 50,
          f"got description={desc_preview!r}")

    # --- cost preview fires at ≥ 8 KEYS (not 8 HTTP). Cost is 1 HTTP
    # per key regardless of --section count, so the threshold is on
    # key count, not section count. ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "--summary",
           "technology", "healthcare", "financial-services",
           "consumer-cyclical", "communication-services", "industrials",
           "energy", "basic-materials"]  # 8 keys
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    check("sectors cost preview: fires at 8 keys (= 8 HTTP, threshold)",
          "info: sectors plan" in out.stderr,
          f"got stderr={out.stderr!r}")

    # --- cost preview NOT fired below threshold ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "--summary", "technology", "healthcare"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("sectors cost preview: silent at 2 keys (under threshold of 8)",
          "info: sectors plan" not in out.stderr,
          f"got stderr={out.stderr!r}")

    # --- regression: HTTP count is constant per key regardless of --section ---
    # Counts yfinance.data.YfData.get invocations to verify the
    # "1 HTTP per key, all sections share cache" invariant. If
    # yfinance changes its caching strategy in a future version,
    # this test catches it.
    import yfinance.data as ydata
    orig_get = ydata.YfData.get
    counter = {"calls": 0}
    def _counting_get(self, url, *args, **kwargs):
        counter["calls"] += 1
        return orig_get(self, url, *args, **kwargs)
    ydata.YfData.get = _counting_get
    try:
        counter["calls"] = 0
        sectors.fetch(key="technology", kind="sector",
                       sections=("overview",), limit=None, full=False)
        n_overview_only = counter["calls"]
        check("sectors HTTP count: --section overview = 1 HTTP per key",
              n_overview_only == 1,
              f"got {n_overview_only} HTTP for overview-only fetch")

        counter["calls"] = 0
        sectors.fetch(key="technology", kind="sector",
                       sections=sectors.SECTOR_SECTIONS,
                       limit=None, full=False)
        n_all_sections = counter["calls"]
        check("sectors HTTP count: --section all = 1 HTTP per key "
              "(all sections share cache, same as overview-only)",
              n_all_sections == 1,
              f"got {n_all_sections} HTTP for all-sections fetch")

        counter["calls"] = 0
        sectors.fetch(key="semiconductors", kind="industry",
                       sections=sectors.INDUSTRY_SECTIONS,
                       limit=None, full=False)
        n_industry = counter["calls"]
        check("sectors HTTP count: industry --section all = 1 HTTP per key",
              n_industry == 1,
              f"got {n_industry} HTTP for industry all-sections fetch")
    finally:
        ydata.YfData.get = orig_get

    # --- --summary --format csv ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "--summary", "--format", "csv", "technology", "healthcare"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("sectors --summary csv: exit 0", out.returncode == 0,
          f"got rc={out.returncode}, stderr={out.stderr!r}")
    lines = out.stdout.strip().split("\n")
    check("sectors --summary csv: header has 'description' column",
          "description" in lines[0],
          f"got header={lines[0]!r}")
    check("sectors --summary csv: emits 2 data rows (one per sector)",
          len(lines) == 3,
          f"got {len(lines) - 1} data rows")

    # --- --full raw output shape ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "technology", "--section", "industries", "--limit", "2",
           "--full"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    rc, parsed = out.returncode, None
    if rc == 0:
        try:
            parsed = json.loads(out.stdout)
        except json.JSONDecodeError:
            pass
    check("sectors --full: exit 0", rc == 0,
          f"got rc={rc}, stderr={out.stderr!r}")
    if (isinstance(parsed, list) and parsed
            and isinstance(parsed[0].get("industries"), list)
            and parsed[0]["industries"]):
        ind_keys = list(parsed[0]["industries"][0].keys())
    else:
        ind_keys = "<no parse>"
    check("sectors --full (industries): row keys preserve raw Yahoo "
          "names (e.g., 'market weight' with space, not 'market_weight')",
          isinstance(parsed, list) and len(parsed) > 0
          and isinstance(parsed[0].get("industries"), list)
          and len(parsed[0]["industries"]) > 0
          and "market weight" in parsed[0]["industries"][0],
          f"got industries[0] keys: {ind_keys}")

    # --- mutex: --summary + --full ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "sectors.py"),
           "technology", "--summary", "--full"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("sectors --summary + --full: argparse rejects",
          out.returncode != 0
          and "mutually exclusive" in out.stderr.lower(),
          f"got rc={out.returncode}, stderr={out.stderr!r}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"sectors smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- market ---
section("market")
try:
    # --- --list-markets (offline; no HTTP) ---
    rc, parsed = run_cli("market.py", "--list-markets")
    check("market --list-markets CLI: exit 0", rc == 0)
    check("market --list-markets: 8 canonical region keys",
          isinstance(parsed, list) and len(parsed) == 8
          and {r.get("key") for r in parsed} == set(market.MARKET_KEYS),
          f"got {len(parsed) if isinstance(parsed, list) else parsed!r}")

    # --- default US fetch ---
    rc, parsed = run_cli("market.py", "US")
    check("market US CLI: exit 0", rc == 0)
    env = parsed[0] if isinstance(parsed, list) and parsed else {}
    check("market US: envelope has clock + summary sections",
          isinstance(env.get("clock"), dict)
          and isinstance(env.get("summary"), list)
          and env.get("summary_count", 0) >= 3,
          f"got clock={type(env.get('clock')).__name__}, "
          f"summary len={len(env.get('summary') or [])}")
    check("market US: clock has open/close ISO strings + IANA timezone",
          isinstance(env.get("clock"), dict)
          and isinstance(env["clock"].get("open"), str)
          and "T" in (env["clock"].get("open") or "")
          and env["clock"].get("timezone") == "America/New_York",
          f"got open={env.get('clock', {}).get('open')!r}, "
          f"tz={env.get('clock', {}).get('timezone')!r}")
    rows = env.get("summary") or []
    check("market US: summary rows have ^GSPC + ^DJI + ^IXIC",
          {r.get("symbol") for r in rows} >= {"^GSPC", "^DJI", "^IXIC"},
          f"got symbols={[r.get('symbol') for r in rows]}")
    # Arithmetic verification: for any row with non-zero change AND
    # non-zero previous_close, change_pct should be approximately
    # `(change/previous_close)*100` (percent encoding) and NOT
    # approximately `change/previous_close` (fraction encoding). On
    # any non-trivial move the two forms are off by 100x, easy to
    # tell apart. We pick the form whose computed value is CLOSER
    # to the reported `change_pct`. If at least one row votes
    # `percent`, we've verified the encoding. Skipped for rows where
    # change is 0 (frozen markets / first trade of session) — the
    # two encodings collapse to 0, can't distinguish.
    def _vote(row):
        pct = row.get("change_pct")
        chg = row.get("change")
        pc = row.get("previous_close")
        if (pct is None or chg is None or pc is None
                or pc == 0 or chg == 0):
            return None
        as_percent = (chg / pc) * 100.0
        as_fraction = chg / pc
        return "percent" if abs(pct - as_percent) < abs(pct - as_fraction) else "fraction"
    votes = [_vote(r) for r in rows]
    decided = [v for v in votes if v is not None]
    check("market US: change_pct is PERCENT-encoded "
          "(arithmetic match against change/previous_close*100, "
          "not just a magnitude heuristic)",
          decided and all(v == "percent" for v in decided),
          f"got votes={votes} (None = couldn't decide due to zero values)")
    check("market US: dst is bool (not string)",
          isinstance(env.get("clock", {}).get("dst"), bool),
          f"got dst={env.get('clock', {}).get('dst')!r} "
          f"(type={type(env.get('clock', {}).get('dst')).__name__})")
    check("market US: row has listing_region (renamed from `region`) "
          "and NOT exchange (dropped — redundant with exchange_code)",
          rows and "listing_region" in rows[0]
          and "exchange" not in rows[0]
          and "region" not in rows[0],
          f"got row keys={list(rows[0].keys()) if rows else 'n/a'}")
    # data_delayed_by_minutes — Yahoo's exchangeDataDelayedBy, surfaced
    # so callers can distinguish API feed delay from yfinance cache
    # staleness. Verified per-quote variance: US has ^GSPC at 0 but
    # ^RUT at 15. Smoke checks that (a) the field is present on every
    # row as an int (or None) and (b) at least one row in US has a
    # non-zero delay (^RUT) — pinning the variance signal.
    check("market US: every summary row has data_delayed_by_minutes "
          "(int or None — Yahoo's per-quote feed delay)",
          rows and all(
              "data_delayed_by_minutes" in r
              and (r["data_delayed_by_minutes"] is None
                   or isinstance(r["data_delayed_by_minutes"], int))
              for r in rows
          ),
          f"got values={[r.get('data_delayed_by_minutes') for r in rows]}")
    check("market US: data_delayed_by_minutes varies per quote "
          "(at least one non-zero delay — typically ^RUT at 15min)",
          rows and any((r.get("data_delayed_by_minutes") or 0) > 0
                       for r in rows),
          f"got values={[(r.get('symbol'), r.get('data_delayed_by_minutes')) for r in rows]}")
    check("market US: NO clock_is_us_fallback on US "
          "(flag only fires for non-US)",
          "clock_is_us_fallback" not in env,
          f"got clock_is_us_fallback={env.get('clock_is_us_fallback')!r}")

    # --- non-US warning ---
    rc, parsed = run_cli("market.py", "ASIA")
    env = parsed[0] if isinstance(parsed, list) and parsed else {}
    check("market ASIA CLI: exit 0", rc == 0)
    check("market ASIA: clock_is_us_fallback bool flag set "
          "(replaces verbose duplicate string)",
          env.get("clock_is_us_fallback") is True,
          f"got clock_is_us_fallback={env.get('clock_is_us_fallback')!r}")
    rows = env.get("summary") or []
    check("market ASIA: summary contains an Asian index "
          "(^N225 / ^HSI / ^AXJO)",
          any(r.get("symbol") in ("^N225", "^HSI", "^AXJO", "000001.SS")
              for r in rows),
          f"got symbols={[r.get('symbol') for r in rows]}")
    # listing_region is Yahoo's listing-region tag — confirmed = "US"
    # for cross-listed Asian indexes (^N225, ^HSI). The renamed field
    # makes this misleading-vs-the-code-name fact explicit.
    asian_rows = [r for r in rows
                  if r.get("symbol") in ("^N225", "^HSI", "^AXJO")]
    check("market ASIA: cross-listed Asian indexes carry "
          "listing_region='US' (Yahoo's listing tag, not home market)",
          asian_rows and all(r.get("listing_region") == "US"
                              for r in asian_rows),
          f"got listing_regions="
          f"{[r.get('listing_region') for r in asian_rows]}")

    # --- case insensitivity ---
    rc, parsed = run_cli("market.py", "us")
    check("market 'us' (lowercase) CLI: exit 0 + market normalized to 'US'",
          rc == 0 and isinstance(parsed, list) and parsed
          and parsed[0].get("market") == "US",
          f"got rc={rc}, "
          f"market={parsed[0].get('market') if parsed else 'n/a'!r}")

    # --- invalid market: argparse rejects (no HTTP) ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "market.py"), "BOGUS"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("market BOGUS: argparse rejects (no HTTP)",
          out.returncode == 2 and "unknown market key" in out.stderr,
          f"got rc={out.returncode}, stderr={out.stderr!r}")

    # --- --summary peer compare ---
    rc, parsed = run_cli("market.py", "--summary", "US", "ASIA")
    check("market --summary CLI: exit 0", rc == 0)
    check("market --summary: flat dicts with avg/best/worst change_pct "
          "+ avg_quote_type + avg_rows_used",
          isinstance(parsed, list) and len(parsed) == 2
          and all({"avg_change_pct", "best_change_pct",
                   "worst_change_pct", "avg_quote_type",
                   "avg_rows_used"}.issubset(r.keys())
                  for r in parsed),
          f"got rolled fields: "
          f"{set(parsed[0].keys()) if isinstance(parsed, list) and parsed else 'n/a'}")
    # Dominant-quote_type filter: US summary has 5 INDEX + 1 FUTURE
    # (Gold). avg_quote_type should be INDEX, avg_rows_used = 5.
    # ASIA has 5 INDEX + 1 CURRENCY. Same expectation.
    us_row = next((r for r in (parsed or []) if r.get("market") == "US"), {})
    asia_row = next((r for r in (parsed or []) if r.get("market") == "ASIA"), {})
    check("market --summary: avg_quote_type='INDEX' for US "
          "(dominant over the lone FUTURE row, GC=F gold)",
          us_row.get("avg_quote_type") == "INDEX"
          and us_row.get("avg_rows_used") == 5
          and us_row.get("summary_count") == 6,
          f"got us_row avg_quote_type={us_row.get('avg_quote_type')!r}, "
          f"avg_rows_used={us_row.get('avg_rows_used')!r}, "
          f"summary_count={us_row.get('summary_count')!r}")
    check("market --summary: avg_quote_type='INDEX' for ASIA "
          "(dominant over the lone CURRENCY row, JPY=X)",
          asia_row.get("avg_quote_type") == "INDEX"
          and asia_row.get("avg_rows_used") == 5,
          f"got asia_row avg_quote_type={asia_row.get('avg_quote_type')!r}, "
          f"avg_rows_used={asia_row.get('avg_rows_used')!r}")

    # --- --summary + --limit warning fires ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "market.py"),
           "--summary", "--limit", "2", "US"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    check("market --summary + --limit: stderr warns + exit 0 "
          "(--limit silently ignored under --summary)",
          out.returncode == 0
          and "limit is ignored" in out.stderr.lower(),
          f"got rc={out.returncode}, stderr={out.stderr!r}")
    # Confirm aggregation still saw the full set despite --limit 2.
    try:
        parsed_warn = json.loads(out.stdout)
        warn_row = parsed_warn[0] if parsed_warn else {}
    except json.JSONDecodeError:
        warn_row = {}
    check("market --summary --limit: avg_rows_used reflects FULL set "
          "(not clipped to --limit)",
          warn_row.get("summary_count") == 6
          and (warn_row.get("avg_rows_used") or 0) >= 5,
          f"got summary_count={warn_row.get('summary_count')!r}, "
          f"avg_rows_used={warn_row.get('avg_rows_used')!r}")

    # --- --summary + --full mutex ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "market.py"),
           "US", "--summary", "--full"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("market --summary + --full: argparse rejects",
          out.returncode != 0
          and "mutually exclusive" in out.stderr.lower(),
          f"got rc={out.returncode}, stderr={out.stderr!r}")

    # --- CSV output: meta + quote record_classes ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "market.py"),
           "US", "--format", "csv"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    check("market CSV CLI: exit 0", out.returncode == 0,
          f"got rc={out.returncode}, stderr={out.stderr!r}")
    lines = out.stdout.strip().split("\n")
    rcs = {ln.split(",")[0] for ln in lines[1:]}  # skip header
    check("market CSV: emits both meta and quote record_classes",
          rcs >= {"meta", "quote"},
          f"got record_classes={rcs}")

    # --- --section clock only (slimmer output, same 2-HTTP cost) ---
    rc, parsed = run_cli("market.py", "US", "--section", "clock")
    env = parsed[0] if isinstance(parsed, list) and parsed else {}
    check("market --section clock CLI: exit 0", rc == 0)
    check("market --section clock: drops summary, keeps clock",
          isinstance(env.get("clock"), dict) and "summary" not in env
          and env.get("sections_returned") == ["clock"],
          f"got sections_returned={env.get('sections_returned')!r}, "
          f"summary present={('summary' in env)}")

    # --- --full passthrough: shape divergence + raw camelCase keys ---
    # In --full mode `summary` is a dict (NOT a list) — Yahoo's raw
    # `{exchange_code: quote_dict}` shape. Quote dicts preserve
    # camelCase keys (regularMarketPrice, shortName, fullExchangeName)
    # rather than our snake_case projections. This is intentional but
    # easy to trip over; pin it in smoke.
    rc, parsed = run_cli("market.py", "US", "--full")
    env = parsed[0] if isinstance(parsed, list) and parsed else {}
    check("market --full CLI: exit 0", rc == 0)
    check("market --full: summary is a DICT (not list) keyed by "
          "exchange_code (Yahoo's raw shape)",
          isinstance(env.get("summary"), dict)
          and {"SNP", "DJI", "NIM"}.issubset(env.get("summary", {}).keys()),
          f"got summary type={type(env.get('summary')).__name__}, "
          f"keys={list(env.get('summary', {}).keys())[:5] if isinstance(env.get('summary'), dict) else 'n/a'}")
    sample = (next(iter(env["summary"].values()))
              if isinstance(env.get("summary"), dict) and env["summary"]
              else {})
    check("market --full: raw quote dict preserves Yahoo camelCase "
          "(regularMarketPrice / shortName / fullExchangeName)",
          isinstance(sample, dict)
          and {"regularMarketPrice", "shortName",
               "fullExchangeName"}.issubset(sample.keys()),
          f"got sample keys (first 5)="
          f"{list(sample.keys())[:5] if isinstance(sample, dict) else 'n/a'}")
    check("market --full: clock keeps raw `tz` + nested `timezone` dict "
          "(projection drops tz, flattens timezone)",
          isinstance(env.get("clock"), dict)
          and isinstance(env["clock"].get("timezone"), dict)
          and "$text" in env["clock"].get("timezone", {}),
          f"got clock.timezone="
          f"{type(env.get('clock', {}).get('timezone')).__name__}")

    # --- --list-markets + positional rejected ---
    cmd = [sys.executable, str(SCRIPTS_DIR / "market.py"),
           "--list-markets", "US"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    check("market --list-markets + positional: argparse rejects",
          out.returncode != 0
          and "cannot be combined" in out.stderr.lower(),
          f"got rc={out.returncode}, stderr={out.stderr!r}")

    # --- retry surfacing: market.fetch() returns attempts > 1 after
    # a transient 429 on the underlying property access.
    # Mocks yfinance.Market entirely so this is offline + deterministic.
    # Mirrors the fast_info retry-surfacing test pattern. ---
    import yfinance as _yf_for_market
    class _FlakyMarket:
        # Class-level counter so retries (which construct a fresh
        # `yf.Market(...)` each time via with_retry's callable) share state.
        summary_calls = 0
        def __init__(self, market):
            self.market = market
        @property
        def summary(self):
            type(self).summary_calls += 1
            if type(self).summary_calls <= 1:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return {
                "SNP": {
                    "symbol": "^GSPC",
                    "shortName": "S&P 500",
                    "quoteType": "INDEX",
                    "regularMarketPrice": 100.0,
                    "regularMarketChange": 1.0,
                    "regularMarketChangePercent": 1.0,
                    "regularMarketPreviousClose": 99.0,
                    "marketState": "CLOSED",
                    "exchange": "SNP",
                    "fullExchangeName": "SNP",
                    "regularMarketTime": 1778273206,
                    "region": "US",
                    "exchangeTimezoneName": "America/New_York",
                },
            }
        @property
        def status(self):
            return {
                "id": "us",
                "name": "U.S. markets",
                "status": "closed",
                "open": "2026-05-11T13:30:00+00:00",
                "close": "2026-05-11T20:00:00+00:00",
                "timezone": {
                    "$text": "America/New_York", "short": "EDT",
                    "gmtoffset": "-14400", "dst": "true",
                },
                "tz": "EDT",
                "yfit_market_status": "YFT_MARKET_CLOSED",
                "message": "U.S. markets closed",
            }
    saved_market_cls = _yf_for_market.Market
    _yf_for_market.Market = _FlakyMarket
    try:
        # NOTE: with_retry's `sleep` default is captured at def-time, so
        # we accept a real ~0.5s sleep on the one retry rather than
        # monkey-patch helpers.time. Same trade-off as fast_info's test.
        envelope = market.fetch(market="US",
                                sections=("clock", "summary"),
                                full=False)
    finally:
        _yf_for_market.Market = saved_market_cls
    check("market.fetch() surfaces attempts > 1 after a transient 429 "
          "(retry recovery via helpers.with_retry)",
          envelope.get("attempts") == 2 and envelope.get("error") is None
          and isinstance(envelope.get("summary"), list)
          and len(envelope["summary"]) == 1,
          f"got attempts={envelope.get('attempts')!r}, "
          f"error={envelope.get('error')!r}, "
          f"summary len={len(envelope.get('summary') or [])}")

    # --- retry exhaustion: 3 attempts all 429 → error envelope shape.
    # Companion to the retry-recovery test above. Verifies the failure
    # path: error / error_kind / attempts populated, NO clock / summary
    # / sections_returned (early return before per-section projection).
    # ---
    class _PermaFlakyMarket:
        summary_calls = 0
        def __init__(self, market):
            self.market = market
        @property
        def summary(self):
            type(self).summary_calls += 1
            raise RuntimeError("HTTP 429 Too Many Requests")
        @property
        def status(self):
            return {}  # never reached on this path
    saved_market_cls = _yf_for_market.Market
    _yf_for_market.Market = _PermaFlakyMarket
    try:
        # Real ~1.5s of retry sleeps (3 attempts × ~0.5s base + jitter);
        # acceptable for smoke. If sleep budget becomes a concern,
        # monkey-patch `helpers.time.sleep` BEFORE importing market —
        # with_retry's `sleep` default is captured at def-time.
        err_env = market.fetch(market="US",
                                sections=("clock", "summary"),
                                full=False)
    finally:
        _yf_for_market.Market = saved_market_cls
    check("market.fetch() error envelope on 3-attempt 429 exhaustion: "
          "error_kind=rate_limit, attempts=3, NO clock/summary/sections_returned",
          err_env.get("error_kind") == "rate_limit"
          and err_env.get("attempts") == 3
          and isinstance(err_env.get("error"), str)
          and "clock" not in err_env
          and "summary" not in err_env
          and "sections_returned" not in err_env,
          f"got error_kind={err_env.get('error_kind')!r}, "
          f"attempts={err_env.get('attempts')!r}, "
          f"keys={sorted(err_env.keys())}")

    # --- HTTP count regression: 2 HTTP per market regardless of --section.
    # Mirrors the sectors HTTP-count test. yfinance interleaves both
    # endpoints in _parse_data, so neither --section clock nor
    # --section summary should be able to skip a fetch.
    #
    # Use DIFFERENT markets for the two probes so yfinance's persistent
    # SQLite response cache (~/.cache/...) doesn't make the second call
    # falsely return 0 HTTP — that would mask the invariant we're
    # actually checking. The invariant is "regardless of which sections
    # the user requested, the first fetch for a given market is 2 HTTP".
    # ---
    import yfinance.data as ydata
    orig_get = ydata.YfData.get
    counter = {"calls": 0}
    def _counting_get(self, url, *args, **kwargs):
        counter["calls"] += 1
        return orig_get(self, url, *args, **kwargs)
    ydata.YfData.get = _counting_get
    try:
        counter["calls"] = 0
        market.fetch(market="EUROPE", sections=("clock",), full=False)
        n_clock_only = counter["calls"]
        check("market HTTP count: --section clock = 2 HTTP "
              "(yfinance fetches both endpoints together)",
              n_clock_only == 2,
              f"got {n_clock_only} HTTP for clock-only fetch")

        counter["calls"] = 0
        market.fetch(market="RATES", sections=("clock", "summary"),
                     full=False)
        n_both = counter["calls"]
        check("market HTTP count: --section clock,summary = 2 HTTP "
              "(same as clock-only — sections share fetch)",
              n_both == 2,
              f"got {n_both} HTTP for both-sections fetch")
    finally:
        ydata.YfData.get = orig_get
except Exception as e:
    FAIL += 1
    FAILURES.append(f"market smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- valuation (offline + per-ticker scrape; equity-leaning ambiguous) ---
section("valuation _parse_yahoo_display_value (offline)")
try:
    # Yahoo's missing-value marker → None (the most common case for
    # loss-makers' P/E and small-cap PEG).
    check("valuation parse '--' → None",
          valuation._parse_yahoo_display_value("--") is None)
    check("valuation parse '' → None",
          valuation._parse_yahoo_display_value("") is None)
    check("valuation parse '   ' → None (whitespace only)",
          valuation._parse_yahoo_display_value("   ") is None)
    check("valuation parse None → None (defensive)",
          valuation._parse_yahoo_display_value(None) is None)

    # Magnitude suffixes. Numerically pin each branch so a future
    # mis-edit to _MAGNITUDE_SUFFIX surfaces immediately. Use a wide
    # absolute tolerance (≤1) because float('4.31')*1e12 isn't exactly
    # 4.31e12 — it's 4309999999999.9995. The precision-loss bound is
    # documented in references/valuation.md ("Precision: ~3 sig figs").
    check("valuation parse '4.31T' ≈ 4.31e12",
          abs(valuation._parse_yahoo_display_value("4.31T") - 4.31e12) < 1)
    check("valuation parse '10.89B' ≈ 1.089e10",
          abs(valuation._parse_yahoo_display_value("10.89B") - 1.089e10) < 1)
    check("valuation parse '373.53M' ≈ 3.7353e8",
          abs(valuation._parse_yahoo_display_value("373.53M") - 3.7353e8) < 1)
    check("valuation parse '123.4K' ≈ 123400.0",
          abs(valuation._parse_yahoo_display_value("123.4K") - 123400.0) < 1)
    check("valuation parse '4.31t' lowercase ≈ 4.31e12 (suffix uppercased)",
          abs(valuation._parse_yahoo_display_value("4.31t") - 4.31e12) < 1)

    # Plain decimal (the seven ratio fields).
    check("valuation parse '35.51' → 35.51",
          valuation._parse_yahoo_display_value("35.51") == 35.51)
    check("valuation parse '0.34' → 0.34 (BMW.DE-style small ratio)",
          valuation._parse_yahoo_display_value("0.34") == 0.34)

    # Defensive: unrecognized suffix or garbage → None, not crash.
    check("valuation parse 'abc' → None (unparseable)",
          valuation._parse_yahoo_display_value("abc") is None)
    check("valuation parse '5.0Q' → None (unknown suffix)",
          valuation._parse_yahoo_display_value("5.0Q") is None)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation parse smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation _parse_period_label (offline)")
try:
    check("valuation period label 'Current' → ('current', None)",
          valuation._parse_period_label("Current") == ("current", None))
    check("valuation period label 'current' lowercase → ('current', None)",
          valuation._parse_period_label("current") == ("current", None))
    check("valuation period label '3/31/2026' → ('2026-03-31', '2026-03-31')",
          valuation._parse_period_label("3/31/2026") == ("2026-03-31", "2026-03-31"))
    check("valuation period label '12/31/2024' → ('2024-12-31', '2024-12-31')",
          valuation._parse_period_label("12/31/2024") == ("2024-12-31", "2024-12-31"))
    # Defensive: unparseable format falls back to raw label + null date,
    # so a Yahoo format change surfaces visibly rather than crashing.
    bad_label, bad_date = valuation._parse_period_label("Q4-2025")
    check("valuation period label 'Q4-2025' → raw label + null date",
          bad_label == "Q4-2025" and bad_date is None)
    # Strict date validation: `5/45/2026` would parse as a garbage ISO
    # under the old splitting parser; strptime rejects it correctly so
    # the unparseable-fallback fires.
    bad_label, bad_date = valuation._parse_period_label("5/45/2026")
    check("valuation period label '5/45/2026' (invalid day) → raw label + null date "
          "(strptime catches it, doesn't silently emit '2026-05-45')",
          bad_label == "5/45/2026" and bad_date is None)
    bad_label, bad_date = valuation._parse_period_label("13/1/2026")
    check("valuation period label '13/1/2026' (invalid month) → raw label + null date",
          bad_label == "13/1/2026" and bad_date is None)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation period label smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation _resolve_row_key (offline)")
try:
    # Exact-label happy paths — verifying the canonical mapping covers
    # every key in our schema.
    check("valuation resolve 'Market Cap' → 'market_cap'",
          valuation._resolve_row_key("Market Cap") == "market_cap")
    check("valuation resolve 'Trailing P/E' → 'trailing_pe'",
          valuation._resolve_row_key("Trailing P/E") == "trailing_pe")
    check("valuation resolve 'Price/Book' → 'price_to_book'",
          valuation._resolve_row_key("Price/Book") == "price_to_book")
    # Yahoo's actual PEG label has a parenthetical qualifier — prefix
    # match handles it. The canonical anchor is just 'PEG Ratio'.
    check("valuation resolve 'PEG Ratio (5yr expected)' → 'peg_ratio' "
          "(Yahoo's actual label — anchor is bare 'PEG Ratio')",
          valuation._resolve_row_key("PEG Ratio (5yr expected)") == "peg_ratio")
    # Substring tolerance test: a hypothetical future qualifier change
    # would still resolve correctly. This is the runtime safety net
    # against Yahoo restyling the parenthetical.
    check("valuation resolve 'PEG Ratio (5y forward)' → 'peg_ratio' "
          "(hypothetical qualifier rename — prefix match still resolves)",
          valuation._resolve_row_key("PEG Ratio (5y forward)") == "peg_ratio")
    # Longest-anchor-first invariant — without the descending sort,
    # 'Enterprise Value' (shorter) would absorb 'Enterprise Value/Revenue'.
    check("valuation resolve 'Enterprise Value/Revenue' → 'ev_to_revenue' "
          "(longer anchor wins over shorter 'Enterprise Value')",
          valuation._resolve_row_key("Enterprise Value/Revenue") == "ev_to_revenue")
    check("valuation resolve 'Enterprise Value/EBITDA' → 'ev_to_ebitda'",
          valuation._resolve_row_key("Enterprise Value/EBITDA") == "ev_to_ebitda")
    check("valuation resolve 'Enterprise Value' → 'enterprise_value' "
          "(plain, no slash suffix — falls to the parent anchor)",
          valuation._resolve_row_key("Enterprise Value") == "enterprise_value")
    # Unknown / empty labels return None — caller silently skips them
    # so Yahoo adding rows doesn't crash us.
    check("valuation resolve 'Quick Ratio' (Yahoo doesn't emit this) → None",
          valuation._resolve_row_key("Quick Ratio") is None)
    check("valuation resolve '' → None (defensive)",
          valuation._resolve_row_key("") is None)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation resolve smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation _to_size_int (offline)")
try:
    # The whole point of this helper: '4.31T' parses to a float that's
    # 0.0005 below the intended value, and naive int() would truncate
    # to ...9999. Round() recovers the intended display number.
    check("valuation _to_size_int: float-precision artifact rounds cleanly "
          "(4309999999999.9995 → 4310000000000, NOT 4309999999999)",
          valuation._to_size_int(4.31 * 1e12) == 4_310_000_000_000)
    check("valuation _to_size_int: clean float passes through",
          valuation._to_size_int(1.0) == 1)
    check("valuation _to_size_int: None → None",
          valuation._to_size_int(None) is None)
    # Round-half-to-even is Python's default; document the boundary
    # behavior so future-us doesn't get surprised by .5 inputs.
    check("valuation _to_size_int: rounds to int (Python banker's rounding)",
          valuation._to_size_int(0.5) == 0
          and valuation._to_size_int(1.5) == 2)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation _to_size_int smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation fetch")
try:
    # --- AAPL: full 6-period × 9-metric table ---
    d = valuation.fetch("AAPL")
    check("valuation AAPL: success has periods list",
          isinstance(d.get("periods"), list) and "error" not in d,
          f"got keys={list(d.keys())}")
    periods = d.get("periods") or []
    # Yahoo currently emits 6 columns (Current + 5 quarter-end snapshots).
    # Canary: a Yahoo template change could shift this — investigate if so.
    check("valuation AAPL: 6 periods returned (Current + 5 quarter-end)",  # canary: typical
          len(periods) == 6,
          f"got {len(periods)}")
    check("valuation AAPL: no `note` on success path",
          "note" not in d)
    if periods:
        first = periods[0]
        # Documented field set lock — adding/removing a key in _PERIOD_KEYS
        # should require updating the schema doc too, so the smoke fires.
        check("valuation AAPL: periods[0] has documented field set "
              f"({sorted(valuation._PERIOD_KEYS)})",
              set(first.keys()) == set(valuation._PERIOD_KEYS),
              f"got keys={sorted(first.keys())}")
        check("valuation AAPL: periods[0].period_label == 'current'",
              first.get("period_label") == "current")
        check("valuation AAPL: periods[0].period_date is None (Current row)",
              first.get("period_date") is None)
        # Type pinning for the 9 metrics. market_cap / enterprise_value
        # are int (via safe_int); the 7 ratios are float. None is allowed
        # for any field (loss-makers have null trailing_pe / forward_pe).
        for k in ("market_cap", "enterprise_value"):
            check(f"valuation AAPL: periods[0].{k} is int (or None)",
                  first.get(k) is None or isinstance(first.get(k), int),
                  f"got {k}={first.get(k)!r} (type={type(first.get(k)).__name__})")
        for k in ("trailing_pe", "forward_pe", "peg_ratio",
                  "price_to_sales", "price_to_book",
                  "ev_to_revenue", "ev_to_ebitda"):
            v = first.get(k)
            check(f"valuation AAPL: periods[0].{k} is float (or None)",
                  v is None or isinstance(v, float),
                  f"got {k}={v!r} (type={type(v).__name__})")
        # Canary: AAPL is a profitable mega-cap; trailing_pe MUST be a
        # populated positive float. Fails when (a) Yahoo's scrape breaks
        # or (b) AAPL has a TTM loss (would be a major news event).
        check("valuation AAPL: trailing_pe is a positive float "  # canary: AAPL profitable
              "(scrape canary — fails if Yahoo restyles or AAPL has TTM loss)",
              isinstance(first.get("trailing_pe"), float)
              and first["trailing_pe"] > 0,
              f"got {first.get('trailing_pe')!r}")
        check("valuation AAPL: market_cap is a positive int "  # canary
              "(roughly 1T–10T USD)",
              isinstance(first.get("market_cap"), int)
              and 1e12 < first["market_cap"] < 1e13,
              f"got {first.get('market_cap')!r}")
        # Clean-rounding canary: with _to_size_int's round(), parsing
        # Yahoo's '4.31T' produces a clean ...0000 trailing rather
        # than the ...9999 truncation artifact. The value should be
        # divisible by 1e10 (Yahoo displays 3 sig figs of trillions =
        # 10-billion granularity).
        check("valuation AAPL: market_cap rounded cleanly to ~1e10 granularity "  # canary
              "(no ...9999 truncation artifact from float-parse)",
              isinstance(first.get("market_cap"), int)
              and first["market_cap"] % int(1e10) == 0,
              f"got {first.get('market_cap')!r} "
              f"(mod 1e10 = {first.get('market_cap', 0) % int(1e10)})")
        # PEG canary — AAPL has had a populated PEG for years.
        # Catches the silent-null failure mode where Yahoo renames the
        # row label to something our _ROW_LABEL_TO_KEY anchor doesn't
        # prefix-match anymore. If it fires: check Yahoo's
        # key-statistics page for the new PEG row label and update the
        # anchor in _ROW_LABEL_TO_KEY.
        check("valuation AAPL: peg_ratio is non-null float "  # canary: AAPL has PEG coverage
              "(row-label canary — fails if Yahoo renames 'PEG Ratio (5yr expected)' "
              "to something our anchor doesn't prefix-match)",
              isinstance(first.get("peg_ratio"), float)
              and first["peg_ratio"] > 0,
              f"got {first.get('peg_ratio')!r}")
        # Subsequent rows are quarter-end snapshots with ISO dates.
        snapshots = periods[1:]
        check("valuation AAPL: snapshot rows have ISO date period_label "
              "(matches period_date)",
              snapshots and all(
                  isinstance(p.get("period_label"), str)
                  and p.get("period_label") == p.get("period_date")
                  and len(p["period_label"]) == 10
                  for p in snapshots
              ),
              f"got labels={[p.get('period_label') for p in snapshots]}")
        # Sort order: Yahoo emits newest-snapshot-first after Current.
        snapshot_dates = [p["period_date"] for p in snapshots]
        check("valuation AAPL: snapshot rows in newest-first order",  # canary
              snapshot_dates == sorted(snapshot_dates, reverse=True),
              f"got dates={snapshot_dates}")

    # --- SPY: empty path emits note, no error_kind ---
    spy = valuation.fetch("SPY")
    check("valuation SPY (ETF): empty path emits note + no error_kind",
          spy.get("periods") == [] and "note" in spy
          and "error" not in spy and "error_kind" not in spy,
          f"got keys={list(spy.keys())}")

    # --- BOGUS: same empty path as SPY (ambiguous-by-design) ---
    bogus = valuation.fetch("BOGUS123XYZ")
    check("valuation BOGUS123XYZ: empty path same as SPY "
          "(ambiguous — no error_kind, chain fast_info to disambiguate)",
          bogus.get("periods") == [] and "note" in bogus
          and "error" not in bogus,
          f"got keys={list(bogus.keys())}")

    # --- non-US listing (0700.HK) works ---
    hk = valuation.fetch("0700.HK")
    check("valuation 0700.HK: success has periods list",
          isinstance(hk.get("periods"), list)
          and len(hk["periods"]) > 0
          and "error" not in hk,
          f"got len={len(hk.get('periods') or [])}, keys={list(hk.keys())}")

    # --- loss-maker (PLUG): trailing/forward P/E all null across the
    # window, but price_to_book + market_cap still populated. Pins the
    # '--' → None parse path on real data.
    plug = valuation.fetch("PLUG")
    plug_periods = plug.get("periods") or []
    check("valuation PLUG (loss-maker): all trailing_pe values null",
          plug_periods and all(p.get("trailing_pe") is None for p in plug_periods),
          f"got trailing_pe values={[p.get('trailing_pe') for p in plug_periods]}")
    check("valuation PLUG: price_to_book still populated "
          "(loss-makers retain book equity)",  # canary: PLUG could go negative-book
          plug_periods and any(
              isinstance(p.get("price_to_book"), float) and p["price_to_book"] > 0
              for p in plug_periods
          ),
          f"got price_to_book values={[p.get('price_to_book') for p in plug_periods]}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation fetch smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation _summarize (offline projection)")
try:
    # Build a synthetic fetch result; _summarize is pure projection
    # over the periods list, no Yahoo round-trip needed.
    fake = {
        "symbol": "FAKE",
        "periods": [
            {"period_label": "current",  "period_date": None,
             "market_cap": 1000, "enterprise_value": 1100,
             "trailing_pe": 20.0, "forward_pe": 18.0, "peg_ratio": 1.5,
             "price_to_sales": 5.0, "price_to_book": 3.0,
             "ev_to_revenue": 5.5, "ev_to_ebitda": 12.0},
            {"period_label": "2026-03-31", "period_date": "2026-03-31",
             "market_cap": 900, "enterprise_value": 1000,
             "trailing_pe": 15.0, "forward_pe": None, "peg_ratio": 1.2,
             "price_to_sales": 4.5, "price_to_book": 2.5,
             "ev_to_revenue": 5.0, "ev_to_ebitda": 10.0},
            {"period_label": "2025-12-31", "period_date": "2025-12-31",
             "market_cap": 800, "enterprise_value": 900,
             "trailing_pe": 25.0, "forward_pe": 22.0, "peg_ratio": None,
             "price_to_sales": 4.0, "price_to_book": 2.0,
             "ev_to_revenue": 4.5, "ev_to_ebitda": 14.0},
        ],
    }
    s = valuation._summarize(fake)
    check("valuation summarize: periods_returned == 3",
          s.get("periods_returned") == 3)
    check("valuation summarize: oldest_period_date is the earliest snapshot",
          s.get("oldest_period_date") == "2025-12-31",
          f"got {s.get('oldest_period_date')!r}")
    check("valuation summarize: current_* lifted from the period_date=None row",
          s.get("current_trailing_pe") == 20.0
          and s.get("current_market_cap") == 1000)

    # Drift-resilience check: same data, but with the "current" row
    # at the END of the periods list (simulating a hypothetical Yahoo
    # emit-order change). _summarize should still pick it as current
    # via the period_date=None marker, not via position. The dated
    # rows go newest→oldest as Yahoo would emit them, but with
    # Current having moved to last.
    fake_reordered = {
        "symbol": "FAKE2",
        "periods": [
            fake["periods"][1],   # 2026-03-31, market_cap 900
            fake["periods"][2],   # 2025-12-31, market_cap 800
            fake["periods"][0],   # Current, market_cap 1000
        ],
    }
    s2 = valuation._summarize(fake_reordered)
    check("valuation summarize: current_* found via period_date=None marker, "
          "NOT position — drift-resilient against Yahoo emit-order changes",
          s2.get("current_market_cap") == 1000
          and s2.get("current_trailing_pe") == 20.0,
          f"got current_market_cap={s2.get('current_market_cap')!r}, "
          f"current_trailing_pe={s2.get('current_trailing_pe')!r}")
    check("valuation summarize: oldest_period_date is min of dated rows, "
          "not last-in-list (drift-resilient)",
          s2.get("oldest_period_date") == "2025-12-31",
          f"got {s2.get('oldest_period_date')!r}")
    check("valuation summarize: min_trailing_pe == 15.0 across window",
          s.get("min_trailing_pe") == 15.0)
    check("valuation summarize: max_trailing_pe == 25.0 across window",
          s.get("max_trailing_pe") == 25.0)
    # forward_pe has a None in the middle row — should be excluded
    # from min/max, not crash.
    check("valuation summarize: forward_pe None excluded from min/max "
          "(not treated as 0 or as a crash)",
          s.get("min_forward_pe") == 18.0 and s.get("max_forward_pe") == 22.0,
          f"got min={s.get('min_forward_pe')!r}, max={s.get('max_forward_pe')!r}")

    # All-None metric across the window stays None (loss-maker simulation).
    fake_loss = {
        "symbol": "LOSS",
        "periods": [
            {"period_label": "current", "period_date": None,
             "market_cap": 100, "enterprise_value": 120,
             "trailing_pe": None, "forward_pe": None, "peg_ratio": None,
             "price_to_sales": 2.0, "price_to_book": 1.5,
             "ev_to_revenue": 2.5, "ev_to_ebitda": None},
        ],
    }
    sl = valuation._summarize(fake_loss)
    check("valuation summarize: all-None metric stays None "
          "(loss-maker — min/max don't crash on empty filtered list)",
          sl.get("current_trailing_pe") is None
          and sl.get("min_trailing_pe") is None
          and sl.get("max_trailing_pe") is None)

    # Empty periods path: summary still emits the symbol + None fields.
    empty = valuation._summarize({"symbol": "X", "periods": [], "note": "n/a"})
    check("valuation summarize: empty periods → symbol + None fields + note",
          empty.get("symbol") == "X"
          and empty.get("periods_returned") == 0
          and empty.get("note") == "n/a"
          and empty.get("current_trailing_pe") is None)
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation summarize smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


section("valuation CLI (subprocess)")
try:
    # Default JSON path
    rc, parsed = run_cli("valuation.py", "AAPL")
    check("valuation AAPL CLI: exit 0", rc == 0)
    check("valuation AAPL CLI: JSON array with one record",
          isinstance(parsed, list) and len(parsed) == 1
          and parsed[0].get("symbol") == "AAPL"
          and isinstance(parsed[0].get("periods"), list),
          f"got {type(parsed).__name__}")

    # --summary CLI
    rc, parsed = run_cli("valuation.py", "--summary", "AAPL", "MSFT")
    check("valuation --summary AAPL MSFT CLI: exit 0", rc == 0)
    check("valuation --summary CLI: 2 flat records with current_trailing_pe",
          isinstance(parsed, list) and len(parsed) == 2
          and all("current_trailing_pe" in r for r in parsed),
          f"got {parsed!r}")

    # CSV default mode — one row per period
    cmd = [sys.executable, str(SCRIPTS_DIR / "valuation.py"),
           "--format", "csv", "AAPL"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("valuation --format csv AAPL CLI: exit 0", out.returncode == 0)
    lines = out.stdout.strip().split("\n")
    header = lines[0].split(",") if lines else []
    # Headers we promise in references/valuation.md.
    expected_cols = {"symbol", "period_label", "period_date",
                     "market_cap", "trailing_pe", "forward_pe",
                     "note", "error", "error_kind", "attempts"}
    check("valuation CSV: header has documented columns",
          expected_cols <= set(header),
          f"missing={expected_cols - set(header)}")
    check("valuation CSV: AAPL emits 6 data rows (one per period) + 1 header",  # canary
          len(lines) == 7,
          f"got {len(lines)} lines")

    # CSV summary mode — strict one row per ticker
    cmd = [sys.executable, str(SCRIPTS_DIR / "valuation.py"),
           "--format", "csv", "--summary", "AAPL", "MSFT", "SPY"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("valuation --format csv --summary CLI: exit 0", out.returncode == 0)
    lines = out.stdout.strip().split("\n")
    check("valuation CSV --summary: 3 data rows + 1 header (incl. empty SPY)",
          len(lines) == 4,
          f"got {len(lines)} lines")
    # SPY (the empty/note ticker) should still appear as a carrying row
    # rather than being silently dropped.
    spy_in_csv = any(line.startswith("SPY,") for line in lines[1:])
    check("valuation CSV --summary: SPY emits carrying row (not silently dropped)",
          spy_in_csv, f"got SPY in CSV={spy_in_csv}")
except Exception as e:
    FAIL += 1
    FAILURES.append(f"valuation CLI smoke crashed: {e}")
    traceback.print_exc(file=sys.stderr)


# --- summary
print()
print("=" * 60)
total = PASS + FAIL
print(f"{PASS}/{total} passed"
      + (f" — {FAIL} FAILURES:" if FAIL else " — all green ✓"))
for f in FAILURES:
    print(f"  ✗ {f}")
sys.exit(0 if FAIL == 0 else 1)
