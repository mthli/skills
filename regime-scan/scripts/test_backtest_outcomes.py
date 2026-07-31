"""Unit tests for backtest_outcomes.py's pure logic: history parsing,
state-spell / confirmation-rule math, flag-type extraction, and forward
window attachment on synthetic bars. No network.

Run:
  uv run --with 'pandas>=2' --with 'numpy>=1.24,<3' --with 'pytest' \
    pytest test_backtest_outcomes.py
"""

import pandas as pd
import pytest

from backtest_outcomes import (attach_forward, confirmed_states, flag_type,
                               load_history, spells, transitions)

HDR = ("run_id,run_date,state,score,n_bull,n_bear,n_flags,spy_vs_200_pct,"
       "ma200_slope_pct,breadth_50_pct,breadth_200_pct,rsp_spy_pct,"
       "nhnl_pct,vix,vix_5d_pct,vix_term,credit_pct,def_off_pct,flags")


def row(run_id, state, score, flags=""):
    return (f'{run_id},2026-07-01T00:00:00+00:00,{state},{score},5,1,1,'
            f'10,1.5,55,60,-0.2,3,16,,0.9,0.1,-2.0,"{flags}"')


def test_load_history_dedups_folds_variants_and_splits_flags(tmp_path):
    p = tmp_path / "history.csv"
    p.write_text("\n".join([
        HDR,
        row("20260701", "RISK-ON", 6),
        row("20260701", "RISK-ON", 7),                 # same-day rerun wins
        row("20260702", "RISK-OFF (internals)", 3,
            "Defensive rotation: defensives outran offensives over 20d "
            "by +4.4pp ; VIX 5-day spike +24%"),
    ]) + "\n")
    rs = load_history(p)
    assert [r.run_day for r in rs] == ["20260701", "20260702"]
    assert rs[0].score == 7
    assert rs[1].state == "RISK-OFF"
    assert rs[1].label == "RISK-OFF (internals)"
    assert len(rs[1].flags) == 2


def test_flag_type_with_and_without_colon():
    assert flag_type("Defensive rotation: defensives outran offensives "
                     "over 20d by +4.4pp") == "Defensive rotation"
    assert flag_type("VIX 5-day spike +24%") == "VIX 5-day spike"
    assert flag_type("Vol-curve inversion: VIX>VIX3M (1.02) — acute "
                     "stress") == "Vol-curve inversion"


def test_spells_and_transitions():
    states = ["A", "A", "B", "A", "A", "A", "C"]
    assert spells(states) == [("A", 2), ("B", 1), ("A", 3), ("C", 1)]
    assert transitions(states) == 3


def test_confirmed_states_filters_whipsaws():
    #        raw:  ON ON CA ON ON CA CA CA ON
    states = ["ON", "ON", "CA", "ON", "ON", "CA", "CA", "CA", "ON"]
    conf2 = confirmed_states(states, 2)
    # 1-day CA blip never confirms; the 3-day CA spell confirms on its
    # 2nd day; the trailing 1-day ON doesn't confirm.
    assert conf2 == ["ON", "ON", "ON", "ON", "ON", "ON", "CA", "CA", "CA"]
    assert transitions(conf2) == 1
    assert confirmed_states(states, 1) == states


def test_confirmed_states_lag_is_n_minus_1():
    states = ["ON"] * 3 + ["CA"] * 5
    conf3 = confirmed_states(states, 3)
    assert conf3.index("CA") == states.index("CA") + 2


def test_attach_forward_windows():
    from backtest_outcomes import Reading
    idx = pd.bdate_range("2026-07-01", periods=25)
    spy = pd.DataFrame({"Close": [100.0] * 5 + [105.0] * 20,
                        "Open": 100.0, "High": 110.0, "Low": 95.0},
                       index=idx)
    r = Reading(run_day="20260701", label="RISK-ON", state="RISK-ON",
                score=6, flags=[])
    attach_forward([r], spy)
    assert r.fwd[5] == pytest.approx(5.0)   # close idx0 → idx5
    assert r.fwd[20] == pytest.approx(5.0)
    # A reading the series doesn't cover gets no windows.
    r2 = Reading(run_day="20250101", label="RISK-ON", state="RISK-ON",
                 score=6, flags=[])
    attach_forward([r2], spy)
    assert r2.fwd == {}
