"""Unit tests for grade_outcomes.py's pure logic: ledger schema validation
and the mechanical replay of entry/stop plans on synthetic bars. No
network, no prices.

Run:
  uv run --with 'pandas>=2' --with 'numpy>=1.24,<3' --with 'pytest' \
    pytest test_grade_outcomes.py
"""

import pandas as pd
import pytest

from grade_outcomes import Pick, resolve, resolve_fill, validate_ledger


def bars(n=30, price=100.0, overrides=None):
    """Flat OHLC tape starting 2026-07-01; overrides = {day_index: dict}."""
    idx = pd.bdate_range("2026-07-01", periods=n)
    df = pd.DataFrame({c: [price] * n for c in
                       ("Open", "High", "Low", "Close")}, index=idx)
    for day, vals in (overrides or {}).items():
        for col, v in vals.items():
            df.loc[idx[day], col] = v
    return df


def finalist(entry_type="market", level=None, stop=95.0, valid=10,
             spot=100.0):
    return Pick(run_date="2026-07-01", regime_state="RISK-ON",
                ticker="TST", role="finalist", tags=["momentum"],
                spot=spot, verdict="✅", entry_type=entry_type,
                entry_level=level, valid_sessions=valid, stop=stop,
                size="normal")


# ---------------------------------------------------------------- fills

def test_market_fills_at_next_open():
    p = finalist("market")
    resolve_fill(p, bars(overrides={1: {"Open": 101.0}}), run_pos=0)
    assert p.filled and p.fill_px == 101.0


def test_pullback_limit_fills_at_level_or_better():
    p = finalist("pullback-limit", level=95.0, stop=90.0)
    b = bars(overrides={2: {"Low": 94.0, "Open": 96.0}})
    resolve_fill(p, b, run_pos=0)
    assert p.fill_px == 95.0
    # A gap-down open through the limit fills at the (better) open —
    # stop kept well below so the fill isn't itself a gap-through-stop.
    p2 = finalist("pullback-limit", level=95.0, stop=85.0)
    resolve_fill(p2, bars(overrides={2: {"Low": 92.0, "Open": 93.0}}),
                 run_pos=0)
    assert p2.fill_px == 93.0


def test_pullback_limit_unfilled_after_window():
    p = finalist("pullback-limit", level=95.0, valid=3)
    resolve_fill(p, bars(), run_pos=0)   # price never dips to 95
    assert p.filled is False and p.status == "unfilled"


def test_pivot_stop_fills_and_gap_skips():
    p = finalist("pivot-stop", level=105.0)
    resolve_fill(p, bars(overrides={2: {"High": 106.0, "Open": 104.0}}),
                 run_pos=0)
    assert p.fill_px == 105.0
    # Open within the 3% chase band: fill at the open.
    p2 = finalist("pivot-stop", level=105.0)
    resolve_fill(p2, bars(overrides={2: {"High": 108.5, "Open": 108.0}}),
                 run_pos=0)
    assert p2.fill_px == 108.0
    # Open beyond 105 × 1.03 = 108.15: the no-chase rule skips it.
    p3 = finalist("pivot-stop", level=105.0)
    resolve_fill(p3, bars(overrides={2: {"High": 110.0, "Open": 109.0}}),
                 run_pos=0)
    assert p3.filled is False and p3.status == "gap_skip"


def test_stop_hit_with_gap_pays_real_slippage():
    p = finalist("market", stop=95.0)
    b = bars(overrides={5: {"Low": 92.0, "Open": 93.0}})
    resolve_fill(p, b, run_pos=0)   # fill at day-1 open = 100
    assert p.stopped and p.status == "graded"
    assert not p.ambiguous          # stop day ≠ fill day
    assert p.realized_r == pytest.approx((93 - 100) / (100 - 95))  # −1.4R


def test_fill_through_stop_is_gap_through_stop_not_graded():
    # Crash gap: market fill opens below the stop — untradable per plan.
    p = finalist("market", stop=95.0)
    resolve_fill(p, bars(overrides={1: {"Open": 93.0, "Low": 92.0}}),
                 run_pos=0)
    assert p.filled is False and p.status == "gap_through_stop"
    assert p.realized_r is None and p.fill_px is None


def test_same_bar_fill_and_stop_double_touch_pessimistic():
    # Limit fills at 95, same bar's Low pierces the 90 stop → stopped
    # −1R and flagged ambiguous.
    p = finalist("pullback-limit", level=95.0, stop=90.0)
    resolve_fill(p, bars(overrides={2: {"Open": 97.0, "Low": 89.0}}),
                 run_pos=0)
    assert p.stopped and p.ambiguous and p.status == "graded"
    assert p.realized_r == pytest.approx(-1.0)


def test_unstopped_marks_at_horizon():
    p = finalist("market", stop=95.0)
    b = bars(n=30)
    b.iloc[21:, :] = 110.0          # fill at day 1; +20 sessions = day 21
    resolve_fill(p, b, run_pos=0)
    assert p.stopped is False
    assert p.realized_r == pytest.approx((110 - 100) / 5)
    assert p.fill_fwd20 == pytest.approx(10.0)


def test_pending_when_horizon_not_elapsed():
    p = finalist("market", stop=95.0)
    resolve_fill(p, bars(n=6), run_pos=0)
    assert p.filled and p.status == "pending" and p.realized_r is None


# ---------------------------------------------------------------- resolve

def test_resolve_forward_windows_and_spy():
    b = bars(n=30)
    b.iloc[5:, :] = 102.0
    spy = bars(n=30)
    p = finalist("market")
    resolve(p, b, spy)
    assert p.fwd[5] == pytest.approx(2.0)
    assert p.spy_fwd[5] == pytest.approx(0.0)


def test_resolve_unit_mismatch_guard():
    p = finalist("market", spot=50.0)   # series trades at 100
    resolve(p, bars(), bars())
    assert p.status == "unit_mismatch" and not p.fwd


# ---------------------------------------------------------------- schema

GOOD = {
    "run_date": "2026-07-31",
    "regime": {"state": "RISK-ON", "score": 6},
    "picks": [
        {"ticker": "ELV", "role": "finalist", "tags": ["momentum"],
         "spot": 412.5, "verdict": "✅",
         "entry": {"type": "pullback-limit", "level": 405.0},
         "stop": 389.0, "size": "normal"},
        {"ticker": "MU", "role": "rejected", "tags": ["mr-pocket"],
         "spot": 739.0, "reason": "earnings in 2 days"},
    ],
}


def test_validate_accepts_good_ledger():
    assert validate_ledger(GOOD) == []


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d["picks"][0].pop("stop"), "stop is required"),
    (lambda d: d["picks"][0].update(stop=500.0), "must sit below"),
    (lambda d: d["picks"][0]["entry"].update(type="market"), "omit level"),
    (lambda d: d["picks"][0].update(tags=["uoa"]), "subset"),
    (lambda d: d["picks"][1].update(role="watch"), "role must be"),
    (lambda d: d.update(run_date="July 31"), "YYYY-MM-DD"),
])
def test_validate_rejects_bad_ledgers(mutate, fragment):
    import copy
    doc = copy.deepcopy(GOOD)
    mutate(doc)
    errs = validate_ledger(doc)
    assert errs and any(fragment in e for e in errs)
