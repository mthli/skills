"""Tests for the ⭐ pocket panel's matched-horizon index benchmark.

compute_benchmark.py owns the matching (same entry day, same number of
sessions held); render_history_html.py turns the per-day averages into
the running line drawn beside the pocket. They meet at
state/benchmark.json, so both sides are pinned here.

Run (from this directory):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    --with 'numpy>=1.24,<3' --with pytest pytest test_benchmark.py
"""
import json
import pathlib

import pandas as pd
import pytest

import compute_benchmark as cb
import render_history_html as rh
import scan

DAYS = ["20260701", "20260702", "20260703", "20260706", "20260707"]


def series(closes: list[float], days: list[str] = DAYS) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes}, index=[cb.ts_of(d) for d in days])


# --------------------------------------------------------- matching rules

def test_each_signal_gets_its_own_holding_length():
    # +10%/session index. A signal that took 1 session to resolve is
    # compared against 1 session, one that took 3 against 3 — averaging a
    # fixed horizon instead would flatter or punish the whole ledger.
    spy = series([100.0, 110.0, 121.0, 133.1, 146.41])
    assert cb.matched_return(spy["Close"], "20260701", 1) == pytest.approx(10.0)
    assert cb.matched_return(spy["Close"], "20260701", 3) == pytest.approx(33.1)


def test_a_window_running_past_the_data_is_dropped_not_clipped():
    spy = series([100.0, 110.0, 121.0, 133.1, 146.41])
    assert cb.matched_return(spy["Close"], "20260707", 1) is None
    assert cb.matched_return(spy["Close"], "20260514", 1) is None  # pre-history


def test_days_are_averaged_per_signal_day():
    # Two signals on day 1 (+10% and +21% windows), one on day 2 (+10%).
    sig = [("20260701", 1), ("20260701", 2), ("20260702", 1)]
    prices = {"SPY": series([100.0, 110.0, 121.0, 133.1, 146.41]),
              "QQQ": series([100.0] * 5)}
    c = cb.build_curves(sig, prices)
    assert c["days"] == ["20260701", "20260702"]
    assert c["n"] == [2, 1]
    assert c["spy"] == [pytest.approx(15.5, abs=1e-3), pytest.approx(10.0)]
    assert c["qqq"] == [0.0, 0.0]


def test_unmatched_signals_are_reported_not_silently_averaged(capsys):
    sig = [("20260701", 1), ("20260707", 3)]  # the second runs off the end
    prices = {"SPY": series([100.0, 110.0, 121.0, 133.1, 146.41]),
              "QQQ": series([100.0] * 5)}
    c = cb.build_curves(sig, prices)
    assert c["n"] == [1, 0]
    assert "1 signal(s) had no matching index window" in capsys.readouterr().err


def test_load_signals_skips_rows_with_no_horizon(tmp_path):
    f = tmp_path / "outcomes.csv"
    f.write_text("run_id,ticker,outcome,days_to_resolve,result_pct\n"
                 "20260701,AAA,WON,2,3.1\n"
                 "20260702,BBB,OPEN,,\n")
    assert cb.load_signals(f) == [("20260701", 2)]


def test_the_file_on_disk_stays_diff_friendly(tmp_path):
    out = tmp_path / "benchmark.json"
    payload = {"days": DAYS[:2], "n": [2, 1], "spy": [1.0, 2.0],
               "qqq": [0.5, 0.25]}
    cb.write_curves(payload, out)
    text = out.read_text()
    assert json.loads(text) == payload
    assert text.count("\n") > len(payload["days"])
    assert not list(out.parent.glob("*.tmp"))  # landed whole


# ------------------------------------------------------- panel input side

def bench(days=DAYS[:3], n=(2, 0, 2), spy=(1.0, None, 3.0),
          qqq=(0.5, None, 1.5)) -> dict:
    return {"days": list(days), "n": list(n), "spy": list(spy),
            "qqq": list(qqq)}


def test_the_line_is_a_running_mean_over_signals_not_days():
    # Day 1: two signals at +1.0. Day 3: two more at +3.0 → the line reads
    # +2.0, not +2.0 by luck: weighting by day would give the same here, so
    # make the counts differ.
    lines = rh.bench_lines(bench(n=(1, 0, 3), spy=(1.0, None, 5.0)), DAYS[:3])
    assert lines["spy"] == [1.0, 1.0, 4.0]  # (1 + 15) / 4
    assert lines["n"] == [1, 1, 4]


def test_days_without_resolved_signals_carry_the_line_forward():
    lines = rh.bench_lines(bench(), DAYS[:3] + ["20260706"])
    assert lines["spy"] == [1.0, 1.0, 2.0, 2.0]


def test_a_benchmark_that_covers_nothing_is_refused(capsys):
    assert rh.bench_lines(bench(), ["20260801", "20260804"]) is None
    assert "covers none" in capsys.readouterr().err


def test_missing_and_malformed_files_leave_the_panel_alone(tmp_path, capsys):
    assert rh.load_benchmark(tmp_path / "nope.json") is None
    bad = tmp_path / "benchmark.json"
    bad.write_text(json.dumps({"days": DAYS[:2], "n": [1, 1], "spy": [1.0]}))
    assert rh.load_benchmark(bad) is None
    err = capsys.readouterr().err
    assert "compute_benchmark.py" in err


def test_the_window_trims_without_rebasing():
    # The lines are all-time running means, like the pocket lines they sit
    # beside; re-basing them to the window would compare two different
    # things on one axis.
    lines = rh.bench_lines(bench(n=(1, 0, 3), spy=(1.0, None, 5.0)), DAYS[:3])
    assert rh.bench_window(lines, 2)["spy"] == [4.0]


# --------------------------------------------------- scan.py auto-refresh

def test_a_failed_refresh_never_fails_the_scan(monkeypatch, capsys):
    def boom(**kw):
        raise SystemExit("no price series for SPY")
    monkeypatch.setattr(cb, "refresh", boom)
    scan.refresh_benchmark()
    assert "benchmark refresh failed" in capsys.readouterr().err


def test_the_refresh_reads_the_ledger_it_just_wrote(monkeypatch):
    seen = {}
    monkeypatch.setattr(cb, "refresh", lambda **kw: seen.update(kw) or None)
    scan.refresh_benchmark()
    assert seen["outcomes"] == scan.OUTCOMES_FILE
    assert seen["out"] == scan.BENCHMARK_FILE


# ------------------------------------------------------ wiring drift guards

def test_every_exit_after_the_ledger_write_refreshes():
    # This is the bug this test exists for: a bulk edit once put the flag
    # returns in the function BELOW main(), so main() fell off its end
    # returning None and the standard scan never refreshed at all.
    import ast
    src = pathlib.Path(scan.__file__).read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if getattr(n, "name", "") == "main")
    flag = next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and "benchmark_after" in ast.unparse(n.targets[0])
                and "no_benchmark" in ast.unparse(n.value))
    after = [n for n in ast.walk(fn)
             if isinstance(n, ast.Return) and n.lineno > flag]
    assert after, "main() has no exit after the ledger write"
    # Falling off the end returns None and silently skips the refresh —
    # no Return node to inspect, so check the last statement itself.
    last = fn.body[-1]
    assert isinstance(last, ast.Return) and "benchmark_after" in \
        ast.unparse(last.value), "main() can fall off its end"
    for r in after:
        assert r.value is not None and "benchmark_after" in ast.unparse(r.value), \
            f"main() line {r.lineno} exits without the refresh flag"
    # ...and the flag never leaked into a function that has no such name.
    for n in tree.body:
        if getattr(n, "name", "") in ("main",):
            continue
        body = "\n".join(src.split("\n")[getattr(n, "lineno", 1) - 1:
                                         getattr(n, "end_lineno", 1)])
        assert "benchmark_after" not in body, \
            f"{getattr(n, 'name', '?')} references main()'s flag"


@pytest.mark.parametrize("lang", ["en", "zh", "zht", "ja", "ko"])
def test_every_language_has_the_benchmark_strings(lang):
    # A missing key renders as "undefined" in that language only, which is
    # exactly the kind of thing nobody notices in a language they don't read.
    block = rh.HTML_TEMPLATE.split(f"\n  {lang}: {{")[1].split("\n  },")[0]
    for key in ("pkBench", "pkVsMkt", "pkBenchNote", "pkNote"):
        assert f"{key}:" in block, f"{lang} is missing {key}"
