"""Tests for render_history_html.py — payload building + drift guards.

The renderer duplicates scan.py's validated-pocket constant and the Sig
tier vocabulary numerically (it must stay stdlib-only while scan.py imports
yfinance at module level); the drift-guard tests pin the files together.
The ledger tests cover backtest_outcomes.py's --write-ledger classification,
since the two halves only work as a pair.

Run (from this directory):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    --with 'numpy>=1.24,<3' --with pytest pytest test_render_html.py
"""
import csv
from pathlib import Path

import pytest

import backtest_outcomes as bt
import render_history_html as rh
import scan


# ------------------------------------------------------------ drift guards

def test_pocket_constant_matches_scan():
    assert rh.VALIDATED_BASE_WEEKS == scan.VALIDATED_BASE_WEEKS


def test_sig_order_covers_every_glyph_scan_emits():
    """Drive scan.py's own classifier into each of its four branches, in
    descending order of urgency; every glyph it can return must have a slot
    in the renderer's ordinal ramp, and the ramp must run the other way
    (palest = furthest from firing). A new tier added to scan.py fails here
    instead of silently rendering as the palest 📊 shade."""
    breakout = {"to_pivot_pct": 0.5, "is_breakout_day": True,
                "today_vol_ratio": scan.BREAKOUT_VOL_CONFIRM_RATIO + 0.1,
                "vol_dryup_ratio": 0.8}
    imminent = {"to_pivot_pct": scan.IMMINENT_TO_PIVOT_PCT + 0.5,
                "is_breakout_day": False, "today_vol_ratio": 1.0,
                "vol_dryup_ratio": scan.IMMINENT_VOL_DRYUP_RATIO - 0.05}
    coiled = {"to_pivot_pct": scan.COILED_TO_PIVOT_PCT + 0.5,
              "is_breakout_day": False, "today_vol_ratio": 1.0,
              "vol_dryup_ratio": 1.2}
    forming = {"to_pivot_pct": scan.COILED_TO_PIVOT_PCT - 5,
               "is_breakout_day": False, "today_vol_ratio": 1.0,
               "vol_dryup_ratio": 1.2}
    got = [
        scan.classify_signal(breakout, scan.IMMINENT_BB_PCTILE - 5),
        scan.classify_signal(imminent, scan.IMMINENT_BB_PCTILE - 5),
        scan.classify_signal(coiled, scan.COILED_BB_PCTILE - 5),
        scan.classify_signal(forming, 50),
    ]
    assert got == list(reversed(rh.SIG_ORDER)), got


def test_ledger_convention_matches_backtest_defaults():
    """The dashed backtest reference lines are quoted for one resolution
    convention; if backtest_outcomes.py's defaults move, the ledger it
    writes stops being comparable to them."""
    assert rh.LEDGER_CONVENTION == {
        "horizon": str(bt.DEFAULT_HORIZON),
        "stop_pct": str(bt.DEFAULT_STOP_PCT),
        "entry": bt.DEFAULT_ENTRY,
    }


def test_renderer_reads_every_ledger_column_it_needs():
    needed = {"start_run_id", "ticker", "outcome", "days_to_trigger",
              "gap_pct", "ret_h", "trade_ret_pct", "fellback5"}
    assert needed <= set(bt.LEDGER_COLS)


def test_sig_order_covers_real_history():
    """Guard against a glyph in the tracked history that the ramp has no
    slot for (e.g. an older scan version's vocabulary)."""
    hist = Path(__file__).resolve().parent.parent / "state" / "history.csv"
    if not hist.exists():
        pytest.skip("no tracked history.csv")
    with open(hist, newline="") as f:
        seen = {r["signal"].strip().rstrip("*") for r in csv.DictReader(f)}
    assert seen <= set(rh.SIG_ORDER), seen - set(rh.SIG_ORDER)


# ------------------------------------------------------------ fixtures

DAYS6 = ["20260701", "20260702", "20260703",
         "20260706", "20260707", "20260708"]


def _row(rid, t, bw="8", tp="-4.0", sig="📊", score="50", width="12.0"):
    return {"run_id": rid, "ticker": t, "base_weeks": bw, "to_pivot_pct": tp,
            "signal": sig, "base_score": score, "width_pct": width,
            "bb_pctile": "20", "vol_dryup_ratio": "0.8",
            "rs_slope_pct_per_wk": "0.5", "pivot_price": "100", "rank": "1"}


def _led(outcome, tr=None, dtt=None, ret_h=None, gap=None, fb=None):
    return {"outcome": outcome, "trade_ret_pct": "" if tr is None else str(tr),
            "days_to_trigger": "" if dtt is None else str(dtt),
            "ret_h": "" if ret_h is None else str(ret_h),
            "gap_pct": "" if gap is None else str(gap),
            "fellback5": "" if fb is None else str(fb),
            "horizon": "20", "stop_pct": "8.0", "entry": "touch"}


def _fixture():
    rows = [
        # AAA: one 3-day episode, a ⭐ pocket base (24wk), triggered +6%.
        _row(DAYS6[0], "AAA", bw="24", tp="-3.0", sig="⏳"),
        _row(DAYS6[1], "AAA", bw="24", tp="-1.5", sig="🔥"),
        _row(DAYS6[2], "AAA", bw="24", tp="0.5", sig="🚀"),
        # BBB: TWO episodes (gap at index 2) — short base, so baseline.
        _row(DAYS6[0], "BBB", bw="8"),
        _row(DAYS6[1], "BBB", bw="8"),
        _row(DAYS6[3], "BBB", bw="9"),
        # CCC: single day on the latest run, 20wk → today's pocket.
        _row(DAYS6[5], "CCC", bw="20", tp="-0.5", sig="🔥"),
        # DDD: live streak into the latest run, short base.
        _row(DAYS6[3], "DDD"), _row(DAYS6[4], "DDD"), _row(DAYS6[5], "DDD"),
    ]
    outcomes = {
        (DAYS6[0], "AAA"): _led("TRIGGERED", tr=6.0, dtt=2, ret_h=6.0,
                                gap=0.4, fb=0),
        (DAYS6[0], "BBB"): _led("FADED", ret_h=-2.0),
        (DAYS6[3], "BBB"): _led("BROKE_DOWN", ret_h=-9.0),
        (DAYS6[3], "DDD"): _led("TRIGGERED", tr=-8.0, dtt=1, ret_h=-8.0,
                                gap=1.0, fb=1),
    }
    return rows, outcomes


# ------------------------------------------------------------ payload

def test_episode_splitting_and_pocket_flags():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    aaa = next(s for s in p["series"] if s["t"] == "AAA")
    assert [pt["k"] for pt in aaa["pts"]] == [1, 2, 3]
    assert [pt["p"] for pt in aaa["pts"]] == [1, 1, 1]   # 24wk ≥ 20 always
    assert [pt["s"] for pt in aaa["pts"]] == [1, 2, 3]   # ⏳ → 🔥 → 🚀
    assert len(aaa["eps"]) == 1                          # unbroken run
    # BBB's gap splits it into two bets, each with its own ledger row.
    bbb = next(s for s in p["series"] if s["t"] == "BBB")
    assert len(bbb["eps"]) == 2
    assert [e["oc"] for e in bbb["eps"]] == ["FADED", "BROKE_DOWN"]
    assert [pt["e"] for pt in bbb["pts"]] == [0, 0, 1]
    # Short bases never enter the pocket.
    assert all(pt["p"] == 0 for pt in bbb["pts"])


def test_longest_live_base_reads_todays_row_not_the_max():
    """Bases reset, so a name can carry a long base in history and a short
    one now. The KPI claims 'now', so it must read the latest row."""
    rows, outcomes = _fixture()
    # EEE ran a 30-week base early, then reset to 7 weeks and is still
    # listed today. AAA's 24wk base is NOT live (last seen on day 2).
    rows += [_row(DAYS6[0], "EEE", bw="30"), _row(DAYS6[1], "EEE", bw="30"),
             _row(DAYS6[5], "EEE", bw="7")]
    p = rh.build_payload(rows, outcomes, {})
    eee = next(s for s in p["summary"] if s["t"] == "EEE")
    assert eee["mbw"] == 30                      # roster column is the max
    # CCC (20wk today) beats EEE's current 7wk and AAA's stale 24wk.
    assert p["kpi"]["longestLive"] == {"t": "CCC", "wk": 20}


def test_longest_live_is_none_when_nothing_has_base_weeks():
    p = rh.build_payload([{**_row(DAYS6[0], "AAA"), "base_weeks": ""}], {}, {})
    assert p["kpi"]["longestLive"] is None


def test_approach_line_splits_where_days_go_missing():
    """The compact (d0, tps) encoding rebuilds x from the index, so a hole
    punched by a missing to_pivot_pct must break the line rather than shift
    everything after it one day left."""
    rows = [
        _row(DAYS6[0], "AAA", tp="-5.0"),
        {**_row(DAYS6[1], "AAA"), "to_pivot_pct": ""},   # hole
        _row(DAYS6[2], "AAA", tp="-1.0"),
        _row(DAYS6[3], "AAA", tp="-0.5"),
    ]
    p = rh.build_payload(rows, {}, {})
    segs = [a for a in p["approach"] if a["t"] == "AAA"]
    assert len(segs) == 2
    assert (segs[0]["d0"], segs[0]["tps"]) == (0, [-5.0])
    assert (segs[1]["d0"], segs[1]["tps"]) == (2, [-1.0, -0.5])
    # The grid still shows all four days; only the trajectory splits.
    assert len(p["series"][0]["pts"]) == 4


def test_payload_carries_no_fields_the_page_never_reads():
    """Dead payload weight is ~50 bytes per roster row and misleads the next
    reader into thinking something consumes it."""
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    assert set(p["summary"][0]) == {
        "t", "sec", "days", "eps", "exp", "trate", "mbw", "pd", "wd", "tp",
        "last", "lastD", "st"}
    assert set(p["series"][0]) == {"t", "pts", "days", "exp", "eps"}
    assert set(p["series"][0]["eps"][0]) == {"oc", "tr", "dtt", "gap", "fb"}


def test_approach_lines_are_run_length_encoded():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    aaa = next(a for a in p["approach"] if a["t"] == "AAA")
    assert aaa["d0"] == 0 and aaa["tps"] == [-3.0, -1.5, 0.5]
    assert aaa["pk"] == 1 and aaa["oc"] == "TRIGGERED"
    # One line per episode, not per ticker.
    bbb = [a for a in p["approach"] if a["t"] == "BBB"]
    assert len(bbb) == 2
    assert bbb[0]["d0"] == 0 and len(bbb[0]["tps"]) == 2
    assert bbb[1]["d0"] == 3 and len(bbb[1]["tps"]) == 1


def test_cohort_stacks_and_pocket_counts():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    # Day 0: AAA ⏳ + BBB 📊 → [📊, ⏳, 🔥, 🚀].
    assert p["cohort"]["perDay"][0] == [1, 1, 0, 0]
    # Day 2: AAA alone, breaking out.
    assert p["cohort"]["perDay"][2] == [0, 0, 0, 1]
    # Latest day: CCC 🔥 + DDD 📊.
    assert p["cohort"]["perDay"][-1] == [1, 0, 1, 0]
    # AAA is a pocket name on days 0-2; CCC on the last day.
    assert p["cohort"]["pocket"] == [1, 1, 1, 0, 0, 1]


def test_pocket_vs_base_expectancy_lines():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    pk = p["pocket"]
    # Only AAA (24wk) is a pocket trade: +6.0 from day 0 onward.
    assert pk["pkt"][0] == 6.0 and pk["pkt"][-1] == 6.0
    assert pk["pktN"][-1] == 1
    # Baseline: only DDD has a realized trade (−8.0), starting day 3.
    # BBB's episodes never triggered, so they have no trade to average.
    assert pk["base"][0] is None
    assert pk["base"][3] == -8.0 and pk["baseN"][-1] == 1
    assert pk["refPkt"] == rh.BACKTEST_POCKET_TRADE
    assert pk["minWeeks"] == rh.VALIDATED_BASE_WEEKS


def test_summary_and_kpi():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    aaa = next(s for s in p["summary"] if s["t"] == "AAA")
    assert aaa["days"] == 3 and aaa["eps"] == 1 and aaa["pd"] == 3
    assert aaa["mbw"] == 24 and aaa["exp"] == 6.0
    assert aaa["tp"] == 0.5          # closest approach, not the latest
    assert aaa["trate"] == 100
    bbb = next(s for s in p["summary"] if s["t"] == "BBB")
    assert bbb["eps"] == 2 and bbb["trate"] == 0 and bbb["exp"] is None
    k = p["kpi"]
    assert k["latest"] == {"n": 2, "hot": 1, "sig": [1, 0, 1, 0]}
    assert k["todayPocket"] == ["CCC"]
    assert k["episodes"] == {"n": 4, "trig": 2, "faded": 1, "broke": 1,
                             "rate": 50}
    assert k["pexp"] == {"v": 6.0, "n": 1}
    assert k["bexp"] == {"v": -8.0, "n": 1}
    assert k["exp"] == -1.0          # (6.0 − 8.0) / 2
    assert k["tracked"] == 4


def test_grid_and_roster_share_one_row_order():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    grid_order = [s["t"] for s in p["series"]]
    roster_order = [s["t"] for s in p["summary"]]
    assert grid_order == roster_order
    # Longest base first, so validated names cluster at the top.
    assert grid_order[0] == "AAA"


def test_row_order_ties_break_the_same_way_the_roster_sorts():
    """The page re-sorts the roster with its own comparator and only falls
    back to this order once every key ties (JS sort is stable), so the keys
    must line up: max base weeks desc, days desc, last-seen desc. Two names
    identical on the first two keys must be ordered by last-seen here, NOT
    by ticker name."""
    rows = [
        # ZZZ and AAA: same max base weeks, same days listed; ZZZ was seen
        # more recently, so it must sort above AAA despite the name order.
        _row(DAYS6[0], "AAA", bw="12"), _row(DAYS6[1], "AAA", bw="12"),
        _row(DAYS6[1], "ZZZ", bw="12"), _row(DAYS6[2], "ZZZ", bw="12"),
    ]
    p = rh.build_payload(rows, {}, {})
    assert [s["t"] for s in p["summary"]] == ["ZZZ", "AAA"]
    assert [s["t"] for s in p["series"]] == ["ZZZ", "AAA"]


def test_names_without_base_weeks_sink_like_the_roster_sinks_nulls():
    rows = [{**_row(DAYS6[0], "AAA"), "base_weeks": ""},
            _row(DAYS6[0], "BBB", bw="6")]
    p = rh.build_payload(rows, {}, {})
    assert [s["t"] for s in p["summary"]] == ["BBB", "AAA"]


def test_windowing_keeps_summary_full():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {}, days_window=3)
    assert p["window"] == {"total": 6, "shown": 3}
    assert len(p["days"]) == 3 and len(p["cohort"]["perDay"]) == 3
    # AAA's episode (days 0-2) predates the window → out of the charts,
    # still in the roster.
    assert not any(s["t"] == "AAA" for s in p["series"])
    assert not any(a["t"] == "AAA" for a in p["approach"])
    assert any(s["t"] == "AAA" for s in p["summary"])
    ddd = next(s for s in p["series"] if s["t"] == "DDD")
    assert [pt["d"] for pt in ddd["pts"]] == [0, 1, 2]
    # Cumulative lines keep their full-history value inside the window.
    assert p["pocket"]["pkt"][-1] == 6.0


def test_missing_ledger_leaves_everything_in_flight():
    rows, _ = _fixture()
    p = rh.build_payload(rows, {}, {})
    assert all(e["oc"] is None for s in p["series"] for e in s["eps"])
    k = p["kpi"]
    assert k["episodes"]["n"] == 0 and k["episodes"]["rate"] is None
    assert k["exp"] is None and k["pexp"]["v"] is None
    # The page still renders its setup story without any outcomes.
    assert k["latest"]["n"] == 2 and k["todayPocket"] == ["CCC"]


def test_convention_mismatch_warns(capsys):
    _, outcomes = _fixture()
    off = dict(outcomes)
    k = (DAYS6[0], "AAA")
    off[k] = {**off[k], "stop_pct": "5.0"}
    rh.check_ledger_convention(off)
    assert "different" in capsys.readouterr().err


def test_asterisked_signal_keeps_its_tier():
    """A trailing '*' marks a freshly-resolved base; the tier is the glyph."""
    rows = [_row(DAYS6[0], "AAA", sig="🔥*")]
    p = rh.build_payload(rows, {}, {})
    assert p["series"][0]["pts"][0]["s"] == rh.SIG_ORDER.index("🔥")


# ------------------------------------------------------------ ledger

class _Ep:
    def __init__(self, ticker, start, end):
        self.ticker, self.start_day, self.end_day = ticker, start, end


def test_pending_set_is_driven_by_ledger_state_not_by_age():
    """A trade completes 20 sessions AFTER its trigger, which can fall long
    after the name left the list — so an age window would skip exactly the
    rows still ripening. Pending = no row, censored, or no trade result."""
    import scan
    eps = [_Ep("NEW", "20260701", "20260703"),     # never resolved
           _Ep("CENS", "20260601", "20260603"),    # resolved but truncated
           _Ep("OPEN", "20260501", "20260503"),    # classified, trade pending
           _Ep("DONE", "20260401", "20260403")]    # complete, oldest of all
    ledger = {
        ("20260601", "CENS"): {"outcome": "TRIGGERED", "censored": "1",
                               "trade_ret_pct": "3.0"},
        ("20260501", "OPEN"): {"outcome": "TRIGGERED", "censored": "0",
                               "trade_ret_pct": ""},
        ("20260401", "DONE"): {"outcome": "TRIGGERED", "censored": "0",
                               "trade_ret_pct": "5.0"},
    }
    got = [e.ticker for e in scan.pending_episodes(eps, ledger)]
    # DONE drops out no matter how old; the oldest incomplete row stays in.
    assert got == ["NEW", "CENS", "OPEN"]


def test_pending_set_empties_once_everything_is_scored():
    import scan
    eps = [_Ep("AAA", "20260701", "20260703")]
    ledger = {("20260701", "AAA"): {"outcome": "FADED", "censored": "0",
                                    "trade_ret_pct": "-2.0"}}
    assert scan.pending_episodes(eps, ledger) == []


# ------------------------------------------------- scan-side ledger refresh

def _synthetic_bars(tickers, sessions, price=100.0):
    """A group_by='ticker' MultiIndex frame shaped like fetch_bars returns."""
    import pandas as pd
    idx = pd.bdate_range("2026-07-01", periods=sessions)
    frames = {}
    for t in tickers:
        frames[(t, "Open")] = [price] * sessions
        frames[(t, "High")] = [price] * sessions
        frames[(t, "Low")] = [price] * sessions
        frames[(t, "Close")] = [price] * sessions
        frames[(t, "Volume")] = [1e6] * sessions
    df = pd.DataFrame(frames, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def _history_csv(path, rows):
    cols = ["run_id", "run_date", "ticker", "rank", "score_rank", "base_score",
            "base_weeks", "width_pct", "bb_pctile", "vol_dryup_ratio",
            "rs_slope_pct_per_wk", "to_pivot_pct", "pivot_price", "signal"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _bar_row(rid, t, pivot, to_pivot):
    return {"run_id": rid, "run_date": f"{rid}T00:00:00+00:00", "ticker": t,
            "rank": 1, "score_rank": 1, "base_score": 50, "base_weeks": 24,
            "width_pct": 12.0, "bb_pctile": 20, "vol_dryup_ratio": 0.8,
            "rs_slope_pct_per_wk": 0.5, "to_pivot_pct": to_pivot,
            "pivot_price": pivot, "signal": "📊"}


def test_refresh_outcomes_writes_and_reports(tmp_path, monkeypatch, capsys):
    """Drives the real integration point end to end on synthetic bars: the
    scan-side path is otherwise only exercised by a live run, and it sits
    behind a broad except that would swallow a regression."""
    import pandas as pd
    import scan
    hist, ledger_path = tmp_path / "history.csv", tmp_path / "outcomes.csv"
    # AAA is in the universe and its episode ended; ZZZ left the universe, so
    # this run holds no bars for it and it must be reported as unreachable.
    _history_csv(hist, [
        _bar_row("20260701", "AAA", 100.0, -2.0),
        _bar_row("20260702", "AAA", 100.0, -1.0),
        _bar_row("20260701", "ZZZ", 100.0, -2.0),
    ])
    monkeypatch.setattr(scan, "HISTORY_FILE", hist)
    monkeypatch.setattr(bt, "OUTCOMES_CSV", ledger_path)
    bars = _synthetic_bars(["AAA"], 30, price=98.0)   # never reaches the pivot
    spy = pd.Series(100.0, index=bars.index)

    stats = scan.refresh_outcomes(bars, spy)
    assert stats["no_bars"] == ["ZZZ"]
    assert stats["new"] == 1 and stats["rewritten"] == 1
    assert ledger_path.exists()
    with open(ledger_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert [(r["ticker"], r["outcome"]) for r in rows] == [("AAA", "FADED")]

    err = capsys.readouterr().err
    assert "newly scored" in err
    assert "ZZZ" in err and "--write-ledger" in err   # names what is stuck


def test_refresh_outcomes_is_quiet_when_nothing_moves(tmp_path, monkeypatch,
                                                      capsys):
    """A line printed every run regardless of progress is noise, and noise is
    what hides the failure message."""
    import pandas as pd
    import scan
    hist, ledger_path = tmp_path / "history.csv", tmp_path / "outcomes.csv"
    _history_csv(hist, [_bar_row("20260701", "AAA", 100.0, -2.0)])
    monkeypatch.setattr(scan, "HISTORY_FILE", hist)
    monkeypatch.setattr(bt, "OUTCOMES_CSV", ledger_path)
    bars = _synthetic_bars(["AAA"], 30, price=98.0)
    spy = pd.Series(100.0, index=bars.index)

    scan.refresh_outcomes(bars, spy)          # first pass writes + reports
    capsys.readouterr()
    stats = scan.refresh_outcomes(bars, spy)  # second pass changes nothing
    assert stats["complete"] == 0 and stats["new"] == 0
    assert capsys.readouterr().err == ""


def test_refresh_outcomes_no_ops_without_history(tmp_path, monkeypatch):
    import pandas as pd
    import scan
    monkeypatch.setattr(scan, "HISTORY_FILE", tmp_path / "missing.csv")
    stats = scan.refresh_outcomes(pd.DataFrame(), pd.Series(dtype=float))
    assert stats == {"complete": 0, "new": 0, "rewritten": 0, "open": 0,
                     "mismatch": 0, "no_bars": []}


def _out(**kw):
    o = bt.Outcome(ep=_Ep(kw.pop("t", "AAA"), kw.pop("s", DAYS6[0]),
                          kw.pop("e", DAYS6[2])),
                   watch_days=3, triggered=kw.pop("triggered", False),
                   censored=kw.pop("censored", False))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_ledger_classifies_the_three_finished_states():
    outs = [
        _out(t="TRG", triggered=True, days_to_trigger=2, trade_ret=6.0,
             ret_h=6.0, gap_pct=0.4, fellback5=False, stop_hit=False),
        _out(t="FAD", no_trigger_drift=-2.0),
        _out(t="BRK", broke_down=True, no_trigger_drift=-9.0),
    ]
    rows = {r["ticker"]: r for r in bt.ledger_rows(outs, 20, 8.0, "touch")}
    assert rows["TRG"]["outcome"] == "TRIGGERED"
    assert rows["FAD"]["outcome"] == "FADED"
    assert rows["BRK"]["outcome"] == "BROKE_DOWN"
    assert rows["TRG"]["trade_ret_pct"] == 6.0
    assert rows["TRG"]["fellback5"] == 0
    # A never-triggered episode records its drift in the same column.
    assert rows["FAD"]["ret_h"] == -2.0 and rows["FAD"]["trade_ret_pct"] is None


def test_ledger_excludes_undecided_episodes():
    """No trigger yet AND the watch window ran past the data edge = in
    flight. Writing it would freeze a live episode as a failure."""
    outs = [_out(t="LIVE", censored=True),
            _out(t="TRIGCENS", triggered=True, censored=True,
                 days_to_trigger=1, ret5=2.0)]
    tickers = {r["ticker"] for r in bt.ledger_rows(outs, 20, 8.0, "touch")}
    assert tickers == {"TRIGCENS"}   # triggered-but-young still records


def test_ledger_empty_input_is_a_noop(tmp_path):
    path = tmp_path / "outcomes.csv"
    assert bt.write_ledger([], path) == 0
    assert not path.exists()          # nothing resolved, nothing created
    seeded = bt.ledger_rows([_out(t="AAA", triggered=True, trade_ret=1.0)],
                            20, 8.0, "touch")
    bt.write_ledger(seeded, path)
    before = path.read_bytes()
    assert bt.write_ledger([], path) == 1
    assert path.read_bytes() == before  # untouched, not rewritten


def test_ledger_records_its_convention():
    rows = bt.ledger_rows([_out(triggered=True, trade_ret=1.0)], 10, 5.0,
                          "close")
    assert rows[0]["horizon"] == 10
    assert rows[0]["stop_pct"] == 5.0
    assert rows[0]["entry"] == "close"


def test_ledger_upsert_replaces_and_preserves(tmp_path):
    path = tmp_path / "outcomes.csv"
    first = bt.ledger_rows([
        _out(t="AAA", triggered=True, trade_ret=1.0),
        _out(t="OLD", no_trigger_drift=-1.0),
    ], 20, 8.0, "touch")
    assert bt.write_ledger(first, path) == 2
    # Re-resolve AAA only (OLD's ticker had no price data this run).
    again = bt.ledger_rows([_out(t="AAA", triggered=True, trade_ret=2.0)],
                           20, 8.0, "touch")
    assert bt.write_ledger(again, path) == 2
    with open(path, newline="") as f:
        got = {r["ticker"]: r for r in csv.DictReader(f)}
    assert got["AAA"]["trade_ret_pct"] == "2.0"   # replaced
    assert got["OLD"]["outcome"] == "FADED"       # preserved
