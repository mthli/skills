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
import pathlib
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


def _led(outcome, tr=None, dtt=None, ret_h=None, gap=None, fb=None,
         ret5=None, ret10=None):
    return {"outcome": outcome, "trade_ret_pct": "" if tr is None else str(tr),
            "days_to_trigger": "" if dtt is None else str(dtt),
            "ret_h": "" if ret_h is None else str(ret_h),
            "gap_pct": "" if gap is None else str(gap),
            "fellback5": "" if fb is None else str(fb),
            "ret5": "" if ret5 is None else str(ret5),
            "ret10": "" if ret10 is None else str(ret10),
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
    assert set(p["series"][0]["eps"][0]) == {"oc", "tr", "dtt", "gap", "fb",
                                             "od"}


# ------------------------------------------------------------ sector panel

def test_sector_edge_mean_interval_and_order():
    buckets = {"Financial Services": [2.0, 3.0, 4.0] * 4,
               "Energy": [-3.0, -5.0, -4.0] * 4}
    tallies = {"Financial Services": [12, 6, 0], "Energy": [12, 8, 4]}
    out = rh.sector_edge(buckets, tallies, 10)
    assert [r["s"] for r in out["rows"]] == ["Financial Services", "Energy"]
    fs = out["rows"][0]
    assert fs["exp"] == 3.0 and fs["n"] == 12
    assert (fs["trig"], fs["fade"], fs["broke"]) == (12, 6, 0)
    assert fs["lo"] < 3.0 < fs["hi"]
    assert round(fs["hi"] - 3.0, 6) == round(3.0 - fs["lo"], 6)
    assert out["all"] == -0.5 and out["allN"] == 24
    assert out["folded"] == {"secs": 0, "n": 0, "names": []}


def test_sector_edge_folds_thin_sectors_without_dropping_them():
    buckets = {"Technology": [1.0] * 10, "Utilities": [5.0, 6.0],
               "Real Estate": [9.0]}
    out = rh.sector_edge(buckets, {}, 10)
    assert [r["s"] for r in out["rows"]] == ["Technology"]
    # Folded sectors stay countable — never silently dropped...
    assert out["folded"] == {"secs": 2, "n": 3,
                             "names": ["Real Estate", "Utilities"]}
    # ...and still count toward the all-trades average.
    assert out["allN"] == 13


def test_sector_edge_single_sample_has_no_interval():
    out = rh.sector_edge({"Energy": [4.0]}, {}, 1)
    assert out["rows"][0]["exp"] == 4.0
    assert out["rows"][0]["lo"] is None and out["rows"][0]["hi"] is None


def test_sector_edge_empty():
    out = rh.sector_edge({}, {}, 10)
    assert out["rows"] == [] and out["all"] is None and out["allN"] == 0


def test_sector_panel_counts_episodes_and_excludes_untagged():
    rows, outcomes = _fixture()
    # AAA/BBB tagged; DDD left out of the cache → its trade is untagged.
    p = rh.build_payload(rows, outcomes, {
        "AAA": {"sector": "Technology"}, "BBB": {"sector": "Energy"}})
    se = p["sectorEdge"]
    assert se["untagged"] == 1                  # DDD's −8% trade
    assert all(r["s"] != "Unknown" for r in se["rows"])
    # Everything is under the cutoff, so nothing draws — but the trades are
    # still pooled into the average and counted as folded.
    assert se["rows"] == []
    assert se["folded"]["names"] == ["Energy", "Technology"]
    assert se["all"] == 6.0 and se["allN"] == 1     # only AAA has a trade
    assert se["minN"] == rh.MIN_SECTOR_N


def test_all_faded_sector_is_still_counted_as_folded():
    """A sector whose bases never fired has no trade return, so it has no
    bar — but it must not disappear from the folded count as if it had
    never been scanned."""
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {
        "AAA": {"sector": "Technology"}, "BBB": {"sector": "Energy"}})
    folded = p["sectorEdge"]["folded"]
    # BBB's two episodes both ended without a trade (FADED, BROKE_DOWN).
    assert folded["names"] == ["Energy", "Technology"]
    assert folded["secs"] == 2
    # Energy contributes no trades, so only AAA's +6% is in the pool.
    assert folded["n"] == 1


def test_open_trade_day_is_clamped_to_what_the_ledger_proves():
    """A triggered episode with no trade result is a trade still running, and
    the tooltip counts its sessions. The count is a weekday count (the scan's
    run days are not a market calendar), so ret5 / ret10 — written only once
    those bars print — pin it to the right band."""
    # 20260701 → 20260731 is 23 weekdays; minus 1 for days_to_trigger = 22,
    # well past every band, so each case lands on its band's ceiling.
    latest = "20260731"
    assert rh.open_trade_day(_led("TRIGGERED", dtt=1), "20260701", latest, 20) == 4
    assert rh.open_trade_day(_led("TRIGGERED", dtt=1, ret5=1.0),
                             "20260701", latest, 20) == 9
    assert rh.open_trade_day(_led("TRIGGERED", dtt=1, ret5=1.0, ret10=2.0),
                             "20260701", latest, 20) == 19
    # A fresh trigger sits inside the first band on its own count.
    assert rh.open_trade_day(_led("TRIGGERED", dtt=1), "20260727", latest, 20) == 3
    # Resolved trades and non-triggers have nothing to count.
    assert rh.open_trade_day(_led("TRIGGERED", tr=6.0, dtt=1),
                             "20260701", latest, 20) is None
    assert rh.open_trade_day(_led("FADED"), "20260701", latest, 20) is None
    assert rh.open_trade_day({}, "20260701", latest, 20) is None


def test_ledger_horizon_reads_the_rows_not_the_constant():
    assert rh.ledger_horizon({("a", "T"): _led("FADED")}) == 20
    off = _led("FADED") | {"horizon": "40"}
    assert rh.ledger_horizon({("a", "T"): off}) == 40
    assert rh.ledger_horizon({}) == int(rh.LEDGER_CONVENTION["horizon"])


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


def test_terminal_rows_without_a_trade_are_final_not_pending():
    """A FADED / BROKE_DOWN row's watch window fully elapsed, so its empty
    trade_ret_pct IS the outcome, not a gap. Reading it as pending would
    re-resolve every such episode on every run for the life of the history —
    and once its ticker left the universe, name it on stderr daily as
    fixable by --write-ledger, which cannot change it. The same empty
    column on a TRIGGERED row means the horizon is still running; that one
    must stay in."""
    eps = [_Ep("FADE", "20260601", "20260603"),
           _Ep("BRK", "20260601", "20260603"),
           _Ep("TRIG", "20260601", "20260603")]
    ledger = {
        ("20260601", "FADE"): {"outcome": "FADED", "censored": "0",
                               "trade_ret_pct": ""},
        ("20260601", "BRK"): {"outcome": "BROKE_DOWN", "censored": "0",
                              "trade_ret_pct": ""},
        ("20260601", "TRIG"): {"outcome": "TRIGGERED", "censored": "0",
                               "trade_ret_pct": ""},
    }
    got = [e.ticker for e in scan.pending_episodes(eps, ledger)]
    assert got == ["TRIG"]


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
                     "mismatch": [], "no_bars": []}


def test_refresh_outcomes_names_unit_mismatches(tmp_path, monkeypatch,
                                                capsys):
    """An episode whose pivot is in pre-split units is unscoreable by EVERY
    path (the by-ticker rebuild re-downloads the same re-adjusted bars);
    silence would let the ledger quietly underreport while looking
    complete."""
    import pandas as pd
    import scan
    hist, ledger_path = tmp_path / "history.csv", tmp_path / "outcomes.csv"
    _history_csv(hist, [_bar_row("20260701", "AAA", 100.0, -2.0)])
    monkeypatch.setattr(scan, "HISTORY_FILE", hist)
    monkeypatch.setattr(bt, "OUTCOMES_CSV", ledger_path)
    # Bars at half the implied scan-day close = a 2:1 split re-adjustment.
    bars = _synthetic_bars(["AAA"], 30, price=49.0)
    spy = pd.Series(100.0, index=bars.index)

    stats = scan.refresh_outcomes(bars, spy)
    assert stats["mismatch"] == ["AAA"]
    assert not ledger_path.exists()
    err = capsys.readouterr().err
    assert "units" in err and "AAA" in err


def _out(**kw):
    o = bt.Outcome(ep=_Ep(kw.pop("t", "AAA"), kw.pop("s", DAYS6[0]),
                          kw.pop("e", DAYS6[2])),
                   watch_days=3, triggered=kw.pop("triggered", False),
                   censored=kw.pop("censored", False))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


# ------------------------------------------------ dead-series resolution

def _one_ticker_bars(closes, start="2026-07-01"):
    import pandas as pd
    idx = pd.bdate_range(start, periods=len(closes))
    c = [float(v) for v in closes]
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c,
                         "Volume": [1e6] * len(c)}, index=idx)


def _episode(t, days, pivot=100.0):
    return bt.Episode(ticker=t, appearances=[
        bt.Appearance(run_day=d, pivot=pivot, score=50.0, signal="📊",
                      bb_pctile=20.0, vol_dryup=0.8, rs_slope=0.5,
                      to_pivot=-2.0, base_weeks=24.0, width_pct=12.0,
                      rank=1)
        for d in days])


def test_series_has_ended_heuristic():
    import pandas as pd
    calendar = pd.bdate_range("2026-07-01", periods=40)
    assert not bt.series_has_ended(_one_ticker_bars([100] * 40), calendar)
    # A few sessions behind is feed lag, not death.
    assert not bt.series_has_ended(_one_ticker_bars([100] * 35), calendar)
    assert bt.series_has_ended(_one_ticker_bars([100] * 30), calendar)
    assert not bt.series_has_ended(_one_ticker_bars([]), calendar)


def test_series_end_forces_exit_instead_of_pending_forever():
    """A ticker that stops trading mid-horizon (delisting / acquisition
    close) will never grow the bars that complete its trade. The position
    is force-exited at the final bar — real money, and a terminal ledger
    row — rather than left pending forever, where the scan would name it on
    stderr daily as fixable by --write-ledger, which could never fix it."""
    import pandas as pd
    calendar = pd.bdate_range("2026-07-01", periods=40)
    spy = pd.DataFrame({"Close": [100.0] * 40}, index=calendar)
    # Triggers on day 2 (close 103 >= pivot 100), then the series stops.
    bars = _one_ticker_bars([98, 98, 103, 103, 104, 105, 105, 106, 106, 106])
    ep = _episode("DEAD", ["20260701", "20260702"])
    assert bt.series_has_ended(bars, calendar)

    o = bt.resolve_episode(ep, bars, spy, calendar, 20, 8.0, "touch",
                           series_ended=True)
    assert o.triggered and not o.censored and not o.stop_hit
    assert o.trade_ret == pytest.approx(106 / 103 * 100 - 100, abs=0.01)
    row = bt.ledger_rows([o], 20, 8.0, "touch")[0]
    assert row["outcome"] == "TRIGGERED"
    assert row["trade_ret_pct"] is not None
    led = {("20260701", "DEAD"): {k: "" if v is None else str(v)
                                  for k, v in row.items()}}
    assert scan.pending_episodes([_Ep("DEAD", "20260701", "20260702")],
                                 led) == []
    # Same bars WITHOUT the flag = data-edge semantics: still waiting.
    o2 = bt.resolve_episode(ep, bars, spy, calendar, 20, 8.0, "touch")
    assert o2.triggered and o2.trade_ret is None


def test_series_end_fades_an_untriggered_watch():
    """The buy-stop never filled and the series ended: the order dies
    unfilled — a terminal FADED, not a censoring that waits forever."""
    import pandas as pd
    calendar = pd.bdate_range("2026-07-01", periods=40)
    spy = pd.DataFrame({"Close": [100.0] * 40}, index=calendar)
    bars = _one_ticker_bars([95, 95, 95])   # never reaches the pivot
    ep = _episode("GONE", ["20260701", "20260702", "20260703"])

    o = bt.resolve_episode(ep, bars, spy, calendar, 20, 8.0, "touch",
                           series_ended=True)
    assert not o.triggered and not o.censored
    assert bt.ledger_rows([o], 20, 8.0, "touch")[0]["outcome"] == "FADED"
    # Without the flag the same window reads as cut off by the data edge.
    o2 = bt.resolve_episode(ep, bars, spy, calendar, 20, 8.0, "touch")
    assert o2.censored


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


def test_ledger_int_columns_survive_upsert_as_ints(tmp_path):
    """Once a None joins an int column pandas floats it, and the second
    upsert writes "1.0" where the first wrote "1" — but consumers compare
    these strings literally (scan.py's pending_episodes reads
    censored == "1")."""
    path = tmp_path / "outcomes.csv"
    bt.write_ledger(bt.ledger_rows([
        _out(t="AAA", triggered=True, trade_ret=1.0, days_to_trigger=2,
             fellback5=True, stop_hit=False),
        _out(t="FAD", no_trigger_drift=-1.0),   # None in every flag column
    ], 20, 8.0, "touch"), path)
    bt.write_ledger(bt.ledger_rows(
        [_out(t="BBB", triggered=True, trade_ret=2.0, days_to_trigger=1,
              fellback5=False, stop_hit=False)], 20, 8.0, "touch"), path)
    with open(path, newline="") as f:
        got = {r["ticker"]: r for r in csv.DictReader(f)}
    assert got["AAA"]["fellback5"] == "1"
    assert got["AAA"]["days_to_trigger"] == "2"
    assert got["BBB"]["stop_hit"] == "0"
    assert got["FAD"]["days_to_trigger"] == ""
    assert all(r["censored"] == "0" for r in got.values())
    assert all(r["horizon"] == "20" for r in got.values())


# ------------------------------------------------- matched-SPY control line

def test_the_ledger_carries_what_the_control_line_needs():
    # The panel's SPY line is a ledger column, not a second fetch: the
    # resolver already has SPY open when it decides the exit.
    assert {"exit_day", "spy_trade"} <= set(bt.LEDGER_COLS)
    assert "exit_day" in bt.INT_LEDGER_COLS


def test_the_exit_day_is_the_stop_day_when_a_trade_stops_out():
    # Half these trades stop out before the horizon; matching the index to
    # 20 sessions anyway would answer a different question.
    import ast
    src = pathlib.Path(bt.__file__).read_text()
    block = src.split("out.stop_hit = bool(")[1].split("out.trade_ret_cut")[0]
    assert "stop_k" in block and "min(horizon" in block
    assert "index_trade_return(spy, trigger_ts, out.exit_day)" in src


@pytest.mark.parametrize("lang", ["en", "zh", "zht", "ja", "ko"])
def test_every_language_has_the_control_line_strings(lang):
    block = rh.HTML_TEMPLATE.split(f"\n  {lang}: {{")[1].split("\n  },")[0]
    for key in ("pkBench", "pkVsMkt", "pkBenchNote", "pkNote"):
        assert f"{key}:" in block, f"{lang} is missing {key}"


def test_a_trade_missing_one_index_sits_out_both_control_lines():
    # The tooltip subtracts each index from the same pocket number, so the
    # two lines have to cover the same trades; a run whose QQQ fetch failed
    # writes spy_trade alone, and half a pair must not enter one line.
    src = pathlib.Path(rh.__file__).read_text()
    block = src.split("vals = {k: _f(led.get(f\"{k}_trade\"))")[1][:400]
    assert "all(v is not None for v in vals.values())" in block
