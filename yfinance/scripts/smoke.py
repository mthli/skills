#!/usr/bin/env python3
"""Smoke test for the four yfinance wrapper scripts.

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

Total runtime: roughly 60-90s (last measured 2026-05: ~70s, US connection).
Live Yahoo calls dominate; the slowest parts are the two `--period max`
history fetches (used for the dividend-adjustment semantic check) and the
prepost-vs-regular intraday pair. Subprocess startup also adds ~5–10s
overhead per CLI check vs pure-import — that's the cost of catching
argparse / JSON / exit-code bugs that import-only testing would miss.
Layer-1 offline checks add ~1s total.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import earnings
import fast_info
import helpers
import history
import info


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
    for script in ("fast_info.py", "history.py", "info.py", "earnings.py"):
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
except Exception as e:
    FAIL += 1
    FAILURES.append(f"CLI sanity crashed: {e}")
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
