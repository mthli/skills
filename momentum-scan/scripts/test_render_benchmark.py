"""Tests for the board-vs-index benchmark: curve math + payload windowing.

compute_benchmark.py owns the arithmetic (no lookahead, equal weight,
coverage) and render_history_html.py owns the alignment to the displayed
window; the two meet at state/benchmark.json, so both sides are pinned
here.

Run (from this directory):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    --with 'numpy>=1.24,<3' --with pytest pytest test_render_benchmark.py
"""
import json

import pandas as pd
import pytest

import compute_benchmark as cb
import render_history_html as rh

DAYS3 = ["20260701", "20260702", "20260703"]


def bars(closes: list[float], days: list[str] = DAYS3) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes},
                        index=[cb.ts_of(d) for d in days])


# ------------------------------------------------------------ curve math

def test_board_is_equal_weight_of_yesterdays_board():
    # AAA doubles on day 2 while BBB is flat: yesterday's board (both
    # names) means +50% for the day, not AAA's +100%.
    boards = {"20260701": ["AAA", "BBB"], "20260702": ["AAA"],
              "20260703": ["AAA"]}
    prices = {"AAA": bars([10.0, 20.0, 20.0]), "BBB": bars([10.0, 10.0, 5.0]),
              "SPY": bars([100.0, 100.0, 100.0]),
              "QQQ": bars([100.0, 100.0, 100.0])}
    c = cb.build_curves(DAYS3, boards, prices)
    assert c["board"] == [100.0, 150.0, 150.0]


def test_todays_board_is_never_applied_to_todays_move():
    # BBB is added to the board only on day 2 — after its -50% day. A
    # lookahead bug would drag the curve down on day 2.
    boards = {"20260701": ["AAA"], "20260702": ["AAA", "BBB"],
              "20260703": ["AAA", "BBB"]}
    prices = {"AAA": bars([10.0, 10.0, 10.0]), "BBB": bars([10.0, 5.0, 5.0]),
              "SPY": bars([100.0] * 3), "QQQ": bars([100.0] * 3)}
    c = cb.build_curves(DAYS3, boards, prices)
    assert c["board"] == [100.0, 100.0, 100.0]


def test_indices_track_their_own_closes():
    boards = {d: ["AAA"] for d in DAYS3}
    prices = {"AAA": bars([10.0] * 3), "SPY": bars([100.0, 110.0, 121.0]),
              "QQQ": bars([50.0, 45.0, 45.0])}
    c = cb.build_curves(DAYS3, boards, prices)
    assert c["spy"] == [100.0, 110.0, 121.0]
    assert c["qqq"] == [100.0, 90.0, 90.0]


def test_unpriced_holding_drops_out_and_is_counted():
    # CCC has no series at all: the day's mean is AAA alone, and coverage
    # records the hole instead of letting it pass as a full board.
    boards = {d: ["AAA", "CCC"] for d in DAYS3}
    prices = {"AAA": bars([10.0, 11.0, 11.0]), "SPY": bars([100.0] * 3),
              "QQQ": bars([100.0] * 3)}
    c = cb.build_curves(DAYS3, boards, prices)
    assert c["board"] == [100.0, 110.0, 110.0]
    assert c["coverage"] == [None, [1, 2], [1, 2]]


def test_gap_between_run_days_is_held_not_skipped():
    # No scan on 07-02; the position is still held across it, so the
    # 07-01 → 07-03 move belongs to the curve in full.
    days = ["20260701", "20260703"]
    prices = {"AAA": bars([10.0, 11.0, 12.0]), "SPY": bars([100.0] * 3),
              "QQQ": bars([100.0] * 3)}
    c = cb.build_curves(days, {d: ["AAA"] for d in days}, prices)
    assert c["board"] == [100.0, 120.0]


def test_stale_member_bars_are_caught_even_when_the_indices_are_fresh():
    # The exact shape the shared cache produces: SPY rides along on every
    # chunk request, so it can be a day newer than the member bars around
    # it. Left unchecked the indices advance on the last day while the
    # board is carried flat — underperformance the board never had.
    boards = {d: ["AAA"] for d in DAYS3}
    fresh = {"SPY": bars([100.0, 101.0, 102.0]),
             "QQQ": bars([100.0, 101.0, 102.0])}
    stale = {"AAA": bars([10.0, 11.0], DAYS3[:2]), **fresh}
    assert cb.covers_last_day(DAYS3, boards, stale) is False
    assert cb.covers_last_day(DAYS3, boards, {"AAA": bars([10.0] * 3),
                                              **fresh}) is True
    # ...and what it protects against, if it ever stops firing.
    c = cb.build_curves(DAYS3, boards, stale)
    assert c["board"][-1] == c["board"][-2] and c["coverage"][-1] == [0, 1]


def test_a_missing_index_bar_carries_that_index_flat():
    boards = {d: ["AAA"] for d in DAYS3}
    prices = {"AAA": bars([10.0] * 3), "SPY": bars([100.0] * 3),
              "QQQ": bars([100.0, 110.0], DAYS3[:2])}
    c = cb.build_curves(DAYS3, boards, prices)
    assert c["qqq"] == [100.0, 110.0, 110.0]


def test_price_start_follows_the_history_it_is_given():
    # A fixed start silently degrades to "no bars" (flat board + a
    # warning per day) the moment history reaches back past it.
    assert cb.default_start("20260514") == "2026-04-30"
    assert cb.default_start("20250102") == "2024-12-19"


def test_load_boards_keeps_only_the_displayed_top_n(tmp_path):
    csv = tmp_path / "history.csv"
    csv.write_text(
        "run_id,ticker,rank\n"
        "20260701,AAA,1\n20260701,BBB,31\n20260702,BBB,2\n")
    days, boards = cb.load_boards(csv, top_n=30)
    assert days == ["20260701", "20260702"]
    assert boards == {"20260701": ["AAA"], "20260702": ["BBB"]}


# ------------------------------------------------- payload → panel input

def bench(days=DAYS3, board=(100.0, 110.0, 121.0), spy=(100.0, 100.0, 100.0),
          qqq=(100.0, 105.0, 105.0), top_n=30, cov=None) -> dict:
    return {"top_n": top_n, "fills": "close", "days": list(days),
            "board": list(board), "spy": list(spy), "qqq": list(qqq),
            "coverage": cov if cov is not None else [None] + [[30, 30]] * (len(days) - 1)}


def test_window_rebases_every_line_to_the_first_shown_day():
    # Window starts on day 2, where board stands at 110: the panel must
    # read "+10% since then", not "+21% since a day that isn't drawn".
    out = rh.window_benchmark(bench(), DAYS3, win_start=1, top_n=30)
    assert out["board"] == [100.0, 110.0]
    assert out["qqq"] == [100.0, 100.0]
    assert out["asOf"] is None


def test_a_stale_file_says_so_on_stderr_too(capsys):
    # The page names the last covered day, but whoever ran the render is
    # looking at a terminal, not the page.
    rh.window_benchmark(bench(), DAYS3 + ["20260706"], 0, top_n=30)
    err = capsys.readouterr().err
    assert "20260703" in err and "compute_benchmark.py" in err


def test_days_outside_the_benchmark_become_holes():
    # The scan ran on 07-06 but the benchmark predates it: that day is a
    # hole in every line, and asOf names the last day actually covered.
    run_ids = DAYS3 + ["20260706"]
    out = rh.window_benchmark(bench(), run_ids, win_start=0, top_n=30)
    assert out["board"] == [100.0, 110.0, 121.0, None]
    assert out["cov"][-1] is None
    assert out["asOf"] == "07-03"


def test_top_n_mismatch_omits_the_panel(capsys):
    assert rh.window_benchmark(bench(top_n=10), DAYS3, 0, top_n=30) is None
    assert "top-10" in capsys.readouterr().err


def test_no_overlap_omits_the_panel(capsys):
    out = rh.window_benchmark(bench(), ["20260801", "20260804"], 0, top_n=30)
    assert out is None
    assert "covers none" in capsys.readouterr().err


def test_missing_file_and_broken_schema_are_survivable(tmp_path, capsys):
    assert rh.load_benchmark(tmp_path / "nope.json") is None
    broken = tmp_path / "benchmark.json"
    broken.write_text(json.dumps({"days": DAYS3, "board": [100.0]}))
    assert rh.load_benchmark(broken) is None
    err = capsys.readouterr().err
    assert "compute_benchmark.py" in err and "'qqq', 'spy'" in err


def test_curve_shorter_than_its_days_is_refused(tmp_path, capsys):
    # Pairing a value with the wrong date is worse than no panel.
    short = tmp_path / "benchmark.json"
    b = bench()
    b["qqq"] = b["qqq"][:-1]
    short.write_text(json.dumps(b))
    assert rh.load_benchmark(short) is None
    assert "out of step" in capsys.readouterr().err


def test_payload_carries_the_panel_input():
    rows = [{"run_id": d, "ticker": "AAA", "rank": "1", "score": "5.0",
             "return_pct": "50.0", "max_dd_pct": "-5.0"} for d in DAYS3]
    p = rh.build_payload(rows, {}, top_n=30, days_window=0, bench=bench())
    assert p["bench"]["board"] == [100.0, 110.0, 121.0]
    assert rh.build_payload(rows, {}, 30, 0, None)["bench"] is None


@pytest.mark.parametrize("lang", ["en", "zh", "zht", "ja", "ko"])
def test_every_language_has_the_panel_strings(lang):
    # The panel reads T.benchX for its title, note, series label and the
    # two caveats; a missing key renders as "undefined" in that language.
    block = rh.HTML_TEMPLATE.split(f"  {lang}: {{")[1].split("\n  },")[0]
    for key in ("benchTitle", "benchNote", "benchBoard", "benchGap",
                "benchCov", "benchCovNote", "benchStale"):
        assert f"{key}:" in block, f"{lang} is missing {key}"
