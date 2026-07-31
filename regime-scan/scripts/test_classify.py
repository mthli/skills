"""Pure-logic tests for the regime classifier — no network.

Run: uv run --with pandas --with numpy --with pytest pytest test_classify.py
"""
import scan


def _spy(vs_200=8.0, slope=1.5, above_50=True, above_200=True, from_high=-0.5):
    return {"last": 600.0, "above_50": above_50, "above_200": above_200,
            "vs_200_pct": vs_200, "ma200_slope_pct": slope,
            "from_high_pct": from_high}


def _metrics(**over):
    """A healthy RISK-ON baseline; override fields per test."""
    m = {
        "spy_trend": _spy(),
        "qqq_trend": _spy(vs_200=12.0),
        "breadth_50_pct": 70.0,
        "breadth_200_pct": 68.0,
        "nhnl": {"nh": 20, "nl": 2, "n": 100, "nhnl_pct": 18.0},
        "rsp_spy_pct": 1.2,
        "vix": 14.0,
        "vix_5d_pct": -3.0,
        "vix_term": 0.90,
        "credit_pct": 0.8,
        "def_off_pct": -1.5,
        "lookback": 20,
        "n_breadth": 100,
    }
    m.update(over)
    return m


def test_healthy_is_green():
    c = scan.classify(_metrics())
    assert c["state"] == "🟢"
    assert c["n_flags"] == 0
    assert c["score"] > 5
    assert c["uptrend_intact"] is True


def test_trend_broken_is_red_regardless_of_internals():
    # Price below 200DMA → gate off → RISK-OFF even if other layers look ok.
    c = scan.classify(_metrics(spy_trend=_spy(vs_200=-2.0, above_200=False)))
    assert c["state"] == "🔴"
    assert "gate" in c["reason"] or "200DMA" in c["reason"]
    # Divergence flags only fire while the uptrend is intact.
    assert c["n_flags"] == 0


def test_breadth_divergence_flag_fires_near_highs():
    # Index pinned near its high but only 42% of names above 50DMA → divergence.
    c = scan.classify(_metrics(spy_trend=_spy(from_high=-1.0),
                               breadth_50_pct=42.0))
    assert any("Breadth divergence" in f for f in c["flags"])
    assert c["state"] in ("🟡", "🔴")


def test_narrowing_flag_on_negative_rsp_spy():
    c = scan.classify(_metrics(rsp_spy_pct=-2.0))
    assert any("Narrowing" in f for f in c["flags"])


def test_credit_and_rotation_flags():
    c = scan.classify(_metrics(credit_pct=-1.0, def_off_pct=2.0))
    assert any("Credit weakening" in f for f in c["flags"])
    assert any("Defensive rotation" in f for f in c["flags"])


def test_vix_backwardation_flag():
    c = scan.classify(_metrics(vix=28.0, vix_term=1.08))
    assert any("inversion" in f for f in c["flags"])


def test_many_flags_force_risk_off_despite_intact_price():
    # Price still above a rising 200DMA, but four internals broke → 🔴 (late top).
    c = scan.classify(_metrics(
        spy_trend=_spy(from_high=-1.0),
        breadth_50_pct=40.0, rsp_spy_pct=-2.0, credit_pct=-1.0,
        def_off_pct=2.0, vix=26.0, vix_term=1.05))
    assert c["uptrend_intact"] is True
    assert c["n_flags"] >= 4
    assert c["state"] == "🔴"


def test_two_flags_is_caution():
    c = scan.classify(_metrics(rsp_spy_pct=-2.0, credit_pct=-1.0))
    assert c["n_flags"] == 2
    assert c["state"] == "🟡"


def test_neutral_tape_with_no_flags_stays_green():
    # Trend up, every other signal neutral (score +1), zero divergence flags →
    # must NOT be downgraded to CAUTION on a merely-low score alone.
    c = scan.classify(_metrics(
        # QQQ < 50DMA → neutral vote
        qqq_trend=_spy(vs_200=2.0, above_50=False),
        breadth_50_pct=52.0, breadth_200_pct=52.0,
        nhnl={"nh": 5, "nl": 5, "n": 100, "nhnl_pct": 0.0},
        rsp_spy_pct=0.0, vix=20.0, vix_term=0.97,
        credit_pct=0.0, def_off_pct=0.0, vix_5d_pct=-3.0))
    assert c["score"] == 1
    assert c["n_flags"] == 0
    assert c["state"] == "🟢"


def test_weak_breadth_alone_is_caution():
    # No divergence flags (not near the high, so the breadth-divergence flag
    # can't fire) and a healthy score — but breadth_50 < 45 trips the
    # weak-breadth CAUTION branch on its own.
    c = scan.classify(_metrics(spy_trend=_spy(from_high=-5.0),
                               breadth_50_pct=44.0))
    assert c["n_flags"] == 0
    assert c["state"] == "🟡"


def test_net_bearish_score_is_caution_even_without_flags_or_weak_breadth():
    # Isolate the score safety net: 0 flags, breadth_50 healthy (≥45 so the
    # b50<45 branch can't fire), but bearish votes from non-flag signals (QQQ
    # broken, breadth_200 weak, NH−NL negative) net to score -2 → still 🟡.
    c = scan.classify(_metrics(
        qqq_trend=_spy(vs_200=-3.0, above_50=False,
                       above_200=False),  # -1, no flag
        breadth_50_pct=55.0,                                  # neutral, ≥45, no flag
        breadth_200_pct=38.0,                                 # -1, no flag
        nhnl={"nh": 2, "nl": 20, "n": 100, "nhnl_pct": -18.0},  # -1, no flag
        vix=20.0, vix_term=0.97,
        rsp_spy_pct=0.0, credit_pct=0.0, def_off_pct=0.0, vix_5d_pct=-3.0))
    assert c["n_flags"] == 0
    assert c["score"] == -2
    assert c["state"] == "🟡"


def test_missing_data_abstains_not_crashes():
    # Every optional metric None → signals abstain (vote 0), no exception.
    c = scan.classify(_metrics(
        breadth_50_pct=None, breadth_200_pct=None, nhnl=None,
        rsp_spy_pct=None, vix=None, vix_5d_pct=None, vix_term=None,
        credit_pct=None, def_off_pct=None))
    assert "state" in c
    assert c["n_flags"] == 0


def test_vote_helper():
    assert scan._vote(70, 60, 40) == 1
    assert scan._vote(30, 60, 40) == -1
    assert scan._vote(50, 60, 40) == 0
    assert scan._vote(None, 60, 40) == 0
    # higher_is_bull=False (e.g. VIX: low is bullish)
    assert scan._vote(12, 17, 25, higher_is_bull=False) == 1
    assert scan._vote(30, 17, 25, higher_is_bull=False) == -1


def test_new_high_low_counts_ties_as_highs():
    import pandas as pd
    rising = pd.Series(range(1, 301), dtype=float)       # ends at its max
    falling = pd.Series(range(300, 0, -1), dtype=float)  # ends at its min
    res = scan.new_high_low({"UP": rising, "DOWN": falling})
    assert res["nh"] == 1 and res["nl"] == 1
    assert res["nhnl_pct"] == 0.0


def test_pct_above_ma():
    import pandas as pd
    above = pd.Series([1.0] * 199 + [100.0])   # last bar spikes above its MA
    below = pd.Series([100.0] * 199 + [1.0])   # last bar craters below its MA
    assert scan.pct_above_ma({"A": above, "B": below}, 50) == 50.0


def test_load_breadth_universe_skips_comments(tmp_path):
    # The generated file carries a `#` provenance header; the loader must drop
    # comment + blank lines and strip whitespace, leaving only tickers.
    f = tmp_path / "u.txt"
    f.write_text("# regime-scan breadth universe\n# generated 2026-Q2\n"
                 "AAPL\n\n  MSFT  \n# trailing comment\nNVDA\n")
    orig = scan.BREADTH_UNIVERSE_FILE
    scan.BREADTH_UNIVERSE_FILE = f
    try:
        assert scan.load_breadth_universe() == ["AAPL", "MSFT", "NVDA"]
    finally:
        scan.BREADTH_UNIVERSE_FILE = orig


def test_rotation_flag_requires_deepening_when_prior_known():
    # Level above deadband but stable vs 5 sessions ago: no flag (the
    # 2026-07-31 retune — as a level alarm it was on 83% of days).
    c = scan.classify(_metrics(def_off_pct=4.0, def_off_prior_pct=3.8))
    assert not any("Defensive rotation" in f for f in c["flags"])
    # Same level, deepening by more than ROTATION_WIDENING_PP: flag fires
    # and says so.
    c2 = scan.classify(_metrics(def_off_pct=4.0, def_off_prior_pct=2.0))
    assert any("Defensive rotation: deepening" in f for f in c2["flags"])
    # Deepening fast but still below the level deadband: no flag.
    c3 = scan.classify(_metrics(def_off_pct=0.4, def_off_prior_pct=-2.0))
    assert not any("Defensive rotation" in f for f in c3["flags"])
    # Prior unavailable (short data): level-only fallback still fires.
    c4 = scan.classify(_metrics(def_off_pct=4.0))
    assert any("Defensive rotation" in f for f in c4["flags"])


def _hist(states):
    import pandas as pd
    return pd.DataFrame({
        "run_id": [f"202607{i:02d}" for i in range(1, len(states) + 1)],
        "state": states,
    })


def test_confirm_state_filters_single_day_whipsaw():
    h = _hist(["RISK-ON", "RISK-ON", "CAUTION"])  # yesterday flipped to CA
    # Today back to RISK-ON: the 1-day CAUTION blip never confirms.
    conf, flipped = scan.confirm_state(h, "20260704", "RISK-ON")
    assert (conf, flipped) == ("RISK-ON", False)
    # Today CAUTION again: 2nd consecutive day confirms the flip.
    conf2, flipped2 = scan.confirm_state(h, "20260704", "CAUTION")
    assert (conf2, flipped2) == ("CAUTION", False)


def test_confirm_state_first_day_flip_is_watch_not_act():
    h = _hist(["RISK-ON", "RISK-ON", "RISK-ON"])
    conf, flipped = scan.confirm_state(h, "20260704", "CAUTION")
    assert (conf, flipped) == ("RISK-ON", True)


def test_confirm_state_folds_variants_and_seeds_from_empty():
    import pandas as pd
    h = _hist(["RISK-OFF (internals)", "RISK-OFF"])
    conf, _ = scan.confirm_state(h, "20260703", "RISK-OFF (internals)")
    assert conf == "RISK-OFF"
    # First-ever reading seeds the confirmed state.
    empty = pd.DataFrame(columns=["run_id", "state"])
    assert scan.confirm_state(empty, "20260701", "CAUTION") == ("CAUTION",
                                                                False)


def test_confirm_state_rerun_same_day_excluded_from_history():
    # Today's run_id already in history (re-run): the stored row must not
    # double-count today.
    h = _hist(["RISK-ON", "CAUTION"])          # 20260702 = today, saved CA
    conf, flipped = scan.confirm_state(h, "20260702", "CAUTION")
    assert (conf, flipped) == ("RISK-ON", True)  # CA has persisted 1 day only


def test_def_off_at_shift_and_short_data():
    import pandas as pd
    import pytest
    idx = pd.bdate_range("2026-01-01", periods=40)
    flat = pd.Series(100.0, index=idx)
    # Offensives jump +10% on day 35 and hold: the current 20-session
    # window sees the jump, the 5-shifted window (ending day 34) doesn't.
    off = pd.Series([100.0] * 35 + [110.0] * 5, index=idx)
    macro = {t: off for t in scan.SECTOR_OFFENSIVE} | \
            {t: flat for t in scan.SECTOR_DEFENSIVE}
    assert scan.def_off_at(macro, 20, 0) == pytest.approx(-10.0)
    assert scan.def_off_at(macro, 20, 5) == pytest.approx(0.0)
    # Clipping below lookback+1 sessions → None, not a crash.
    short = {t: off.iloc[-22:] for t in (scan.SECTOR_OFFENSIVE
                                         + scan.SECTOR_DEFENSIVE)}
    assert scan.def_off_at(short, 20, 5) is None
    # One whole side missing → None.
    assert scan.def_off_at({t: off for t in scan.SECTOR_OFFENSIVE},
                           20, 0) is None
