#!/usr/bin/env python3
"""Smoke test for the ten yfinance wrapper scripts.

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
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import analyst
import earnings
import fast_info
import financials
import helpers
import history
import holders
import info
import insiders
import news
import options


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
                   "insiders.py", "analyst.py"):
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
