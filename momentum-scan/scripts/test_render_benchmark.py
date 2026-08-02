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
import pathlib

import pandas as pd
import pytest

import compute_benchmark as cb
import render_history_html as rh
import scan

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


def test_the_file_on_disk_stays_diff_friendly(tmp_path):
    # The file is tracked and the daily scan rewrites it whole, so a
    # compact one-line dump would turn every commit into an unreadable
    # rewrite. One value per line keeps a run's change to what it is.
    out = tmp_path / "benchmark.json"
    payload = {"top_n": 30, **bench()}
    cb.write_curves(payload, out)
    text = out.read_text()
    assert json.loads(text) == payload
    assert text.count("\n") > len(payload["board"])
    # ...and lands whole: the nightly job commits whatever it finds.
    assert not list(out.parent.glob("*.tmp"))


def test_price_start_follows_the_history_it_is_given():
    # A fixed start silently degrades to "no bars" (flat board + a
    # warning per day) the moment history reaches back past it.
    assert cb.default_start("20260514") == "2026-04-12"
    assert cb.default_start("20250102") == "2024-12-01"


def test_price_start_clears_the_atr_warmup():
    # The run-up is sized for the price block, not the curves: too short
    # and the first run-days of the whole history carry no stop, which
    # reads as "this name had no risk" rather than as missing data.
    start = pd.Timestamp(cb.default_start("20260514"))
    sessions = len(pd.bdate_range(start, cb.ts_of("20260514")))
    assert sessions > cb.ATR_PERIOD_DAYS + 1


def test_load_boards_keeps_only_the_displayed_top_n(tmp_path):
    csv = tmp_path / "history.csv"
    csv.write_text(
        "run_id,ticker,rank\n"
        "20260701,AAA,1\n20260701,BBB,31\n20260702,BBB,2\n")
    days, boards = cb.load_boards(csv, top_n=30)
    assert days == ["20260701", "20260702"]
    assert boards == {"20260701": ["AAA"], "20260702": ["BBB"]}


# --------------------------------------------------- scan.py auto-refresh

def test_scan_refreshes_for_the_board_it_just_displayed(monkeypatch, tmp_path):
    monkeypatch.setattr(scan, "BENCHMARK_FILE", tmp_path / "benchmark.json")
    seen = {}
    monkeypatch.setattr(cb, "refresh",
                        lambda **kw: seen.update(kw) or {"days": DAYS3,
                                                         "board": [100.0] * 3,
                                                         "spy": [100.0] * 3,
                                                         "qqq": [100.0] * 3})
    scan.refresh_benchmark(10)
    assert seen["top_n"] == 10
    assert seen["history"] == scan.HISTORY_FILE
    assert seen["out"] == scan.BENCHMARK_FILE


@pytest.mark.parametrize("err", [OSError("connection reset"),
                                 RuntimeError("no price series for SPY"),
                                 SystemExit("no price series for SPY")])
def test_a_failed_refresh_never_fails_the_scan(monkeypatch, capsys, err):
    # SystemExit is the one that got through: it isn't an Exception, and
    # this call sits between a finished scan and its report.
    def boom(**kw):
        raise err
    monkeypatch.setattr(cb, "refresh", boom)
    scan.refresh_benchmark(30)
    assert "benchmark refresh failed" in capsys.readouterr().err


def test_a_missing_index_is_an_error_the_caller_can_catch():
    # Only the CLI turns it into an exit; refresh() is library code.
    assert not issubclass(RuntimeError, SystemExit)
    src = (pathlib.Path(cb.__file__).read_text()
           .split("def refresh(")[1].split("\ndef ")[0])
    assert "raise RuntimeError" in src and "raise SystemExit" not in src


def test_the_run_leaves_a_differently_sized_board_alone(monkeypatch, tmp_path,
                                                        capsys):
    # benchmark.json is tracked and the nightly job commits what it finds,
    # so an ad-hoc --top-n 10 scan must not quietly replace the curves the
    # default dashboard draws.
    f = tmp_path / "benchmark.json"
    f.write_text(json.dumps({"top_n": 30, **bench()}))
    monkeypatch.setattr(scan, "BENCHMARK_FILE", f)
    monkeypatch.setattr(cb, "refresh",
                        lambda **kw: pytest.fail("should not have refreshed"))
    scan.refresh_benchmark(10)
    assert "top-30" in capsys.readouterr().err
    assert json.loads(f.read_text())["top_n"] == 30


def test_a_history_too_short_to_draw_says_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cb, "refresh", lambda **kw: None)
    scan.refresh_benchmark(30)
    assert capsys.readouterr().err == ""


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


# ----------------------------------------------- price block → hover card

BD = pd.bdate_range("2026-06-01", periods=20)


def ohlc(closes: list[float], spread=2.0) -> pd.DataFrame:
    sp = spread if isinstance(spread, list) else [spread] * len(closes)
    return pd.DataFrame({"High": [c + s / 2 for c, s in zip(closes, sp)],
                         "Low": [c - s / 2 for c, s in zip(closes, sp)],
                         "Close": closes}, index=BD)


# A jagged series for the ATR guard below. A steady one will not do: with
# a constant true range every smoothing and every period agree, so the
# guard would pass against a wrong implementation.
WOBBLY = [100.0, 104, 99, 107, 103, 96, 105, 113, 108, 101,
          117, 112, 124, 118, 109, 128, 121, 133, 126, 138]
WOBBLY_SPREAD = [3, 7, 2, 9, 4, 11, 5, 8, 3, 12, 6, 4, 10, 7, 13, 5, 9, 3, 11, 6]


def day(i: int) -> str:
    return BD[i].strftime("%Y%m%d")


def test_the_atr_matches_the_one_the_cli_printed():
    # The whole reason the dashboard computes its own ATR is that it has
    # to land on the stop scan.py published that morning. Same bars, same
    # number — or the page is confidently wrong.
    frame = ohlc(WOBBLY, WOBBLY_SPREAD)
    px = cb.build_prices([day(19)], ["AAA"], {"AAA": frame})
    multi = frame.copy()
    multi.columns = pd.MultiIndex.from_product([["AAA"], frame.columns])
    ref = scan.compute_atrs(multi, ["AAA"])["AAA"]
    assert px["AAA"]["a"][0] == pytest.approx(ref["atr"], abs=1e-4)
    assert px["AAA"]["c"][0] == pytest.approx(ref["last_close"], abs=1e-4)


def test_the_atr_guard_can_actually_fail():
    # Pins the fixture, not the code: if WOBBLY ever flattens out, the
    # guard above keeps passing while silently checking nothing. Wilder's
    # smoothing and a shorter period are the two ways the implementations
    # could plausibly drift apart, so the data has to separate them.
    frame = ohlc(WOBBLY, WOBBLY_SPREAD)
    prev = frame["Close"].shift(1)
    tr = pd.concat([frame["High"] - frame["Low"],
                    (frame["High"] - prev).abs(),
                    (frame["Low"] - prev).abs()], axis=1).max(axis=1)
    sma = tr.rolling(cb.ATR_PERIOD_DAYS).mean().iloc[-1]
    wilder = tr.ewm(alpha=1 / cb.ATR_PERIOD_DAYS, adjust=False).mean().iloc[-1]
    shorter = tr.rolling(cb.ATR_PERIOD_DAYS - 4).mean().iloc[-1]
    assert abs(sma - wilder) > 1.0
    assert abs(sma - shorter) > 0.5


def test_the_interval_high_spans_the_days_between_runs():
    closes = [100.0] * 20
    closes[8] = 200.0   # before the first run-day: not this spell's peak
    closes[17] = 130.0  # a session the scan skipped, but the trade held
    px = cb.build_prices([day(16), day(18)], ["AAA"], {"AAA": ohlc(closes)})
    assert px["AAA"]["h"] == [100.0, 130.0]


def test_a_run_day_off_the_end_of_the_series_is_a_hole():
    px = cb.build_prices([day(5), "20261231"], ["AAA"],
                         {"AAA": ohlc([100.0] * 20)})
    assert [v is None for v in px["AAA"]["c"]] == [False, True]
    assert px["AAA"]["a"][1] is None and px["AAA"]["h"][1] is None


def test_the_atr_period_and_default_multiplier_match_the_scan():
    # Two scripts derive the same stop: compute_benchmark supplies the
    # ATR, render_history_html the multiplier the page opens on. Either
    # drifting from scan.py makes the dashboard disagree with the CLI.
    assert cb.ATR_PERIOD_DAYS == scan.ATR_PERIOD_DAYS
    assert rh.ATR_STOP_MULT_DEFAULT == scan.ATR_STOP_MULT_DEFAULT


# -------------------------------------------- episodes → entry / exit rows

D5 = [f"2026070{i}" for i in range(1, 6)]


def priced(days=D5, c=(10.0, 12.0, 11.0, 20.0, 22.0),
           h=(10.0, 15.0, 11.0, 20.0, 25.0), a=(1.0,) * 5,
           ticker="AAA") -> dict:
    b = bench(days=days, board=[100.0] * len(days), spy=[100.0] * len(days),
              qqq=[100.0] * len(days))
    b["atr_period"] = 14
    b["px"] = {ticker: {"c": list(c), "a": list(a), "h": list(h)}}
    return b


def row(d: str, ticker: str, rank: int) -> dict:
    return {"run_id": d, "ticker": ticker, "rank": str(rank), "score": "5.0",
            "return_pct": "50.0", "max_dd_pct": "-5.0"}


def history(ranks: dict[str, int], ticker="AAA") -> list[dict]:
    """One row per (day, rank); days absent from `ranks` have no row for
    `ticker`. A filler name holds every run-day open regardless, the way
    a real board does — without it a day the subject skipped would vanish
    from run_ids entirely and its spell would look unbroken."""
    return ([row(d, ticker, r) for d, r in ranks.items()]
            + [row(d, "ZFILL", 1) for d in D5])


def series_of(rows, bench_, ticker="AAA", top_n=30):
    p = rh.build_payload(rows, {}, top_n=top_n, days_window=0, bench=bench_)
    return next(s for s in p["series"] if s["t"] == ticker)


def test_each_spell_is_measured_against_its_own_entry():
    # Left and came back: the second spell's cost basis is 20, not 10.
    s = series_of(history(dict(zip(D5, [1, 1, 40, 1, 1]))), priced())
    assert s["eps"][0]["c"] == 10.0 and s["eps"][1]["c"] == 20.0
    assert [p.get("e") for p in s["pts"]] == [0, 0, 0, 1, 1]


def test_the_dropout_cell_settles_the_trade():
    # Day 3 is below the cutoff: under the one validated exit that close
    # IS the sale, so the cell carries the result instead of going grey.
    s = series_of(history(dict(zip(D5, [1, 1, 40, 1, 1]))), priced())
    assert s["pts"][2]["x"] == 1 and s["pts"][2]["c"] == 11.0
    assert s["eps"][0]["xc"] == 11.0 and s["eps"][0]["xl"] == "07-03"
    # A sold position has no stop to draw.
    assert "a" not in s["pts"][2] and "pk" not in s["pts"][2]
    # The open spell has no exit at all.
    assert "xc" not in s["eps"][1]


def test_the_grey_tail_stays_attached_to_the_trade_it_followed():
    # Days after the sale are the case FOR selling the dropout, so they
    # keep the exit on screen instead of going blank.
    s = series_of(history(dict(zip(D5, [1, 1, 40, 45, 50]))), priced())
    assert [p.get("x") for p in s["pts"]] == [None, None, 1, 2, 2]
    assert all(p["e"] == 0 for p in s["pts"][2:])


def test_a_cell_before_the_first_spell_belongs_to_no_trade():
    # Nothing was bought yet, so there is no cost basis to measure from.
    s = series_of(history(dict(zip(D5, [40, 1, 1, 1, 1]))), priced())
    assert "e" not in s["pts"][0] and "x" not in s["pts"][0]
    assert s["pts"][0]["c"] == 10.0


def test_the_peak_runs_from_entry_and_resets_each_spell():
    s = series_of(history(dict(zip(D5, [1, 1, 40, 1, 1]))), priced())
    assert [p.get("pk") for p in s["pts"]] == [10.0, 15.0, None, 20.0, 25.0]


def test_a_name_that_left_the_scan_settles_on_its_last_listed_day():
    # No row on day 3 means no cell to hover there, so the last day on
    # the board carries the result instead.
    s = series_of(history({D5[0]: 1, D5[1]: 1, D5[3]: 1, D5[4]: 1}), priced())
    assert s["pts"][1]["xl"] == 1
    assert s["eps"][0]["xc"] == 11.0
    # ...and a spell that ends by a cell going grey does not, since that
    # cell says it itself.
    s2 = series_of(history(dict(zip(D5, [1, 1, 40, 1, 1]))), priced())
    assert all("xl" not in p for p in s2["pts"])
    # Nor when the dropout day itself has no row but a later grey cell
    # does: that cell carries the sale, so the note would be a duplicate
    # sitting one column to its left.
    s3 = series_of(history({D5[0]: 1, D5[1]: 1, D5[3]: 40, D5[4]: 1}), priced())
    assert all("xl" not in p for p in s3["pts"])
    assert [p.get("x") for p in s3["pts"]] == [None, None, 2, None]


def test_a_name_the_benchmark_has_no_prices_for_carries_no_episodes():
    # Empty episodes would be kilobytes of labels the page never reads.
    s = series_of(history(dict(zip(D5, [1] * 5))), priced(ticker="ZZZ"))
    assert "eps" not in s
    assert all("c" not in p and "e" not in p for p in s["pts"])


def test_a_benchmark_that_stops_early_leaves_the_later_days_blank():
    # Borrowing the last covered day's price for every day after it would
    # read as a flat position rather than as missing data.
    s = series_of(history(dict(zip(D5, [1] * 5))),
                  priced(days=D5[:3], c=(10.0, 12.0, 11.0),
                         h=(10.0, 15.0, 11.0), a=(1.0,) * 3))
    assert [p.get("c") for p in s["pts"]] == [10.0, 12.0, 11.0, None, None]


@pytest.mark.parametrize("lang", ["en", "zh", "zht", "ja", "ko"])
def test_every_language_has_the_price_strings(lang):
    # The price block reads eight T keys; a missing one renders as
    # "undefined" in exactly one language, which no one running the
    # default English page would ever see.
    block = rh.HTML_TEMPLATE.split(f"  {lang}: {{")[1].split("\n  },")[0]
    for key in ("entryPx", "closePx", "sellPx", "stopPx", "trailPx",
                "stopNote", "trailHit", "settled"):
        assert f"{key}:" in block, f"{lang} is missing {key}"


@pytest.mark.parametrize("lang", ["en", "zh", "zht", "ja", "ko"])
def test_every_language_has_the_panel_strings(lang):
    # The panel reads T.benchX for its title, note, series label and the
    # two caveats; a missing key renders as "undefined" in that language.
    block = rh.HTML_TEMPLATE.split(f"  {lang}: {{")[1].split("\n  },")[0]
    for key in ("benchTitle", "benchNote", "benchBoard", "benchGap",
                "benchCov", "benchCovNote", "benchStale"):
        assert f"{key}:" in block, f"{lang} is missing {key}"
