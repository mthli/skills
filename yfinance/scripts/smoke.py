#!/usr/bin/env python3
"""Smoke test for the five yfinance wrapper scripts.

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
import subprocess
import sys
import traceback
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import earnings
import fast_info
import financials
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
                   "financials.py"):
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
