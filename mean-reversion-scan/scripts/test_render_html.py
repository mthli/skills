"""Tests for render_history_html.py — payload building + drift guards.

The renderer duplicates scan.py's validated-pocket / breadth constants
numerically (it must stay stdlib-only while scan.py imports yfinance at
module level); the drift-guard tests pin the two files together.

Run (from this directory):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    --with 'numpy>=1.24,<3' --with pytest pytest test_render_html.py
"""
import render_history_html as rh
import scan


# ------------------------------------------------------------ drift guards

def test_pocket_constants_match_scan():
    assert rh.VALIDATED_MIN_SCORE == scan.VALIDATED_MIN_SCORE
    assert rh.VALIDATED_MAX_STREAK == scan.VALIDATED_MAX_STREAK


def test_breadth_constants_match_scan():
    assert rh.BREADTH_THIN_MAX == scan.BREADTH_THIN_MAX
    assert rh.BREADTH_WASHOUT_MIN == scan.BREADTH_WASHOUT_MIN


def test_target_window_matches_scan():
    assert rh.TARGET_WINDOW_DAYS == scan.TARGET_WINDOW_DAYS


def test_outcome_schema_matches_scan():
    # The payload joins history and ledger on (run_id, ticker) and reads
    # these resolution fields — if scan.py's ledger schema changes, the
    # renderer must follow.
    assert scan.OUTCOMES_COLS == ["run_id", "ticker", "outcome",
                                  "days_to_resolve", "result_pct"]


# ------------------------------------------------------------ fixtures

# The run-day space is whatever days appear in rows (real history always
# has ≥1 signal per scan day). Six days here: AAA/BBB/DDD cover 01-03,
# EEE/CCC cover 06-08 → indices 0-5, and with the default 5-day target
# window every index ≥ 1 without a ledger row reads OPEN.
DAYS6 = ["20260701", "20260702", "20260703",
         "20260706", "20260707", "20260708"]


def _row(rid, t, score="45", rsi="2.0", sig="🟢"):
    return {"run_id": rid, "ticker": t, "score": score, "rsi2": rsi,
            "signal": sig}


def _o(outcome, days, pct):
    return {"outcome": outcome, "days_to_resolve": days, "result_pct": pct}


def _fixture():
    rows = [
        # AAA: 3-day spell from day 1; score 45 → pocket on days 1-2 only.
        # Day 3 has no ledger row → OPEN (still inside the target window).
        _row(DAYS6[0], "AAA"), _row(DAYS6[1], "AAA"), _row(DAYS6[2], "AAA"),
        # BBB: one-day, score below the pocket floor.
        _row(DAYS6[0], "BBB", score="30"),
        # CCC: signals on the last day — no outcome yet → OPEN.
        _row(DAYS6[5], "CCC"),
        # DDD: day-1 signal with no ledger row → UNRESOLVED (index 0 is the
        # only day outside the 5-day window in a 6-day space).
        _row(DAYS6[0], "DDD"),
        # EEE: 3-day live streak into the latest run → stuck.
        _row(DAYS6[3], "EEE"), _row(DAYS6[4], "EEE"), _row(DAYS6[5], "EEE"),
    ]
    outcomes = {
        (DAYS6[0], "AAA"): _o("WON", "1", "2.0"),
        (DAYS6[1], "AAA"): _o("LOST", "2", "-3.0"),
        (DAYS6[0], "BBB"): _o("EXPIRED", "5", "-0.5"),
    }
    return rows, outcomes


# ------------------------------------------------------------ payload

def test_streak_pocket_and_outcome_cats():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    aaa = next(s for s in p["series"] if s["t"] == "AAA")
    ks = [pt["k"] for pt in aaa["pts"]]
    ps = [pt["p"] for pt in aaa["pts"]]
    os_ = [pt["o"] for pt in aaa["pts"]]
    assert ks == [1, 2, 3]
    assert ps == [1, 1, 0]          # day 3 of the spell falls out of the pocket
    # Day-3 signal has no ledger row but is still inside the target window.
    assert os_ == ["W", "L", "O"]
    ccc = next(s for s in p["series"] if s["t"] == "CCC")
    assert ccc["pts"][0]["o"] == "O"   # latest-day signal is in flight
    ddd = next(s for s in p["series"] if s["t"] == "DDD")
    assert ddd["pts"][0]["o"] == "U"


def test_breadth_stacks():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    # Day 1: AAA won, BBB expired, DDD unresolved → [W, L, E, O, U].
    assert p["breadth"]["perDay"][0] == [1, 0, 1, 0, 1]
    # Latest day: CCC + EEE both open.
    assert p["breadth"]["perDay"][-1] == [0, 0, 0, 2, 0]
    assert p["breadth"]["thin"] == rh.BREADTH_THIN_MAX


def test_summary_stats():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    aaa = next(s for s in p["summary"] if s["t"] == "AAA")
    assert aaa["days"] == 3 and aaa["spells"] == 1 and aaa["pd"] == 2
    assert (aaa["w"], aaa["l"], aaa["e"]) == (1, 1, 0)
    assert aaa["win"] == 50 and aaa["exp"] == -0.5 and aaa["tot"] == -1.0
    assert aaa["st"] == "O" and aaa["stk"] == 0   # not on the latest run
    eee = next(s for s in p["summary"] if s["t"] == "EEE")
    assert eee["stk"] == 3 and eee["spells"] == 1


def test_pocket_lines_and_kpi():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {})
    pk = p["pocket"]
    # Pocket: AAA day1 +2.0, day2 −3.0 → cumulative mean 2.0 then −0.5.
    assert pk["pkt"][0] == 2.0 and pk["pkt"][1] == -0.5
    assert pk["pkt"][-1] == -0.5 and pk["pktN"][-1] == 2
    # Rest: BBB −0.5 from day 1 onward.
    assert pk["base"][0] == -0.5 and pk["baseN"][-1] == 1
    k = p["kpi"]
    assert k["resolved"] == {"n": 3, "w": 1, "l": 1, "e": 1, "rate": 50}
    assert k["exp"] == -0.5
    assert k["pexp"] == {"v": -0.5, "n": 2}
    assert k["latestBreadth"] == {"n": 2, "tier": "thin"}
    # CCC signals on the latest day with score 45 on day 1 of its spell;
    # EEE is on day 3 (stuck), which the pocket excludes by definition.
    assert k["todayPocket"] == ["CCC"]
    assert k["stuck"] == ["EEE"]
    assert k["tracked"] == 5


def test_grid_points_carry_price_levels():
    rows, outcomes = _fixture()
    # A signal row carrying the levels the ledger resolves against.
    rows.append({**_row(DAYS6[5], "FFF"), "last_close": "100.0",
                 "target_price": "104.0", "stop_price": "96.5"})
    p = rh.build_payload(rows, outcomes, {})
    fff = next(s for s in p["series"] if s["t"] == "FFF")["pts"][0]
    assert (fff["px"], fff["tg"], fff["sp"]) == (100.0, 104.0, 96.5)
    assert fff["tt"] == 4.0          # the bounce the target demanded
    # A row without the levels (pre-schema history) leaves the block blank
    # rather than inventing a target.
    aaa = next(s for s in p["series"] if s["t"] == "AAA")["pts"][0]
    assert aaa["px"] is None and aaa["tg"] is None and aaa["tt"] is None


def test_windowing_keeps_summary_full():
    rows, outcomes = _fixture()
    p = rh.build_payload(rows, outcomes, {}, days_window=3)
    assert p["window"] == {"total": 6, "shown": 3}
    assert len(p["days"]) == 3 and len(p["breadth"]["perDay"]) == 3
    # AAA's spell (days 1-3) predates the window → out of the charts,
    # still in the roster.
    assert not any(s["t"] == "AAA" for s in p["series"])
    assert any(s["t"] == "AAA" for s in p["summary"])
    # Windowed pts are re-based to the window origin.
    eee = next(s for s in p["series"] if s["t"] == "EEE")
    assert [pt["d"] for pt in eee["pts"]] == [0, 1, 2]
    # Cumulative pocket line keeps its full-history value inside the window.
    assert p["pocket"]["pkt"][-1] == -0.5


# ------------------------------------------------------------ sector panel

def test_sector_edge_mean_interval_and_order():
    # Two sectors, both over the cutoff. Tech's values average +2.0.
    buckets = {"Technology": [1.0, 2.0, 3.0] * 4,
               "Energy": [-1.0, 0.0, -2.0] * 4}
    tallies = {"Technology": [8, 2, 2], "Energy": [3, 6, 3]}
    out = rh.sector_edge(buckets, tallies, 10)
    assert [r["s"] for r in out["rows"]] == ["Technology", "Energy"]
    tech = out["rows"][0]
    assert tech["exp"] == 2.0 and tech["n"] == 12
    assert (tech["w"], tech["l"], tech["e"]) == (8, 2, 2)
    # Interval brackets the mean and is symmetric about it.
    assert tech["lo"] < 2.0 < tech["hi"]
    assert round(tech["hi"] - 2.0, 6) == round(2.0 - tech["lo"], 6)
    # All-signals average pools both sectors: (2.0 * 12 + -1.0 * 12) / 24.
    assert out["all"] == 0.5 and out["allN"] == 24
    assert out["folded"] == {"secs": 0, "n": 0, "names": []}


def test_sector_edge_folds_thin_sectors_without_dropping_them():
    buckets = {"Technology": [1.0] * 10, "Utilities": [5.0, 6.0],
               "Real Estate": [9.0]}
    out = rh.sector_edge(buckets, {}, 10)
    assert [r["s"] for r in out["rows"]] == ["Technology"]
    # Folded sectors stay countable — never silently dropped.
    assert out["folded"] == {"secs": 2, "n": 3,
                             "names": ["Real Estate", "Utilities"]}
    # ...and still count toward the all-signals average.
    assert out["allN"] == 13


def test_sector_edge_single_sample_has_no_interval():
    out = rh.sector_edge({"Energy": [4.0]}, {}, 1)
    assert out["rows"][0]["exp"] == 4.0
    assert out["rows"][0]["lo"] is None and out["rows"][0]["hi"] is None


def test_sector_edge_empty():
    out = rh.sector_edge({}, {}, 10)
    assert out["rows"] == [] and out["all"] is None and out["allN"] == 0


def test_payload_sector_panel_excludes_untagged():
    rows, outcomes = _fixture()
    # AAA tagged, BBB left out of the cache → its resolved signal is
    # untagged, not a sector called "Unknown".
    p = rh.build_payload(rows, outcomes, {"AAA": {"sector": "Technology"}})
    se = p["sectorEdge"]
    assert se["untagged"] == 1                     # BBB's expired signal
    assert all(r["s"] != "Unknown" for r in se["rows"])
    # AAA has 2 resolved, under the cutoff → folded, and no bar is drawn.
    assert se["rows"] == []
    assert se["folded"]["names"] == ["Technology"]
    assert se["minN"] == rh.MIN_SECTOR_N


def test_missing_ledger_renders_open_or_unresolved():
    rows, _ = _fixture()
    p = rh.build_payload(rows, {}, {})
    cats = {pt["o"] for s in p["series"] for pt in s["pts"]}
    assert cats <= {"O", "U"}
    assert p["kpi"]["resolved"]["n"] == 0
    assert p["kpi"]["exp"] is None and p["kpi"]["pexp"]["v"] is None
