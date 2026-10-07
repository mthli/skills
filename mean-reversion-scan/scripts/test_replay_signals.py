"""Unit tests for replay_signals.py's pure logic. The load-bearing one: the
per-session panel must reproduce scan.py's score_tickers +
filter_vol_collapse + attach_atr_stops_all on the same bars, session by
session; a replay of a different pick list would answer a different
question. Also spells, trade resolution and the two portfolio builders.
No network.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    --with 'pytest' pytest test_replay_signals.py
"""

import numpy as np
import pandas as pd
import pytest

import scan
from replay_signals import (SCAN, TH, W, ew_open, resolve, slotted, spells,
                            ticker_panel, trade_daily_returns)

N_DAYS = 320


def synthetic_bars(seed: int = 3) -> dict[str, pd.DataFrame]:
    """Uptrends with sharp 3-4 session pullbacks (RSI(2) fires inside the
    trend filter), a downtrend (fails it), and a buyout pin (vol-collapse)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-02", periods=N_DAYS)
    closes = {}
    for k, (drift, vol) in enumerate([(.002, .012), (.0015, .02), (.003, .008), (.001, .015)]):
        r = rng.normal(drift, vol, N_DAYS)
        for start in rng.choice(np.arange(230, N_DAYS - 5), 6, replace=False):
            r[start:start + 3] -= rng.uniform(.01, .03)
        r[0] = 0
        closes[f"UP{k}"] = 50 * np.exp(np.cumsum(r))
    closes["DOWN"] = 80 * np.exp(np.cumsum(np.r_[0, rng.normal(-.002, .015, N_DAYS - 1)]))
    pin = 40 * np.exp(np.cumsum(np.r_[0, rng.normal(.003, .02, N_DAYS - 1)]))
    pin[-35:] = pin[-36] * 1.3 * (1 + np.cumsum(rng.normal(-.0002, .0006, 35)))
    pin[-3:] *= 1 - np.array([.001, .002, .003])        # drifts down off the deal price
    closes["PIN"] = pin
    C = pd.DataFrame(closes, index=idx)
    spread = np.abs(rng.normal(0, .01, C.shape))
    H = C * (1 + spread)
    L = C * (1 - spread[::-1])
    return dict(C=C, H=H, L=L)


def scan_picks(b: dict[str, pd.DataFrame], end: int) -> dict[str, dict]:
    C, H, L = (b[k].iloc[:end + 1] for k in ("C", "H", "L"))
    closes = {t: C[t] for t in C.columns}
    picks = scan.score_tickers(closes, TH)
    kept, _ = scan.filter_vol_collapse(picks, closes, SCAN.vol_collapse_ratio)
    bars = pd.concat({t: pd.DataFrame({"High": H[t], "Low": L[t], "Close": C[t]})
                      for t in C.columns}, axis=1)
    scan.attach_atr_stops_all(kept, bars, SCAN.atr_stop_mult)
    return {p["ticker"]: p for p in kept}


def panel_picks(b: dict[str, pd.DataFrame], end: int) -> dict[str, dict]:
    out = {}
    for t in b["C"].columns:
        p = ticker_panel(b["C"][t], b["H"][t], b["L"][t])
        if p["pick"].iloc[end]:
            out[t] = {k: float(v.iloc[end]) for k, v in p.items() if k != "pick"}
    return out


ENDS = list(range(240, N_DAYS))


def test_synthetic_panel_exercises_every_branch():
    # If a seed change stops the fixture firing signals, tripping
    # vol-collapse, or failing the trend filter, the parity test would
    # quietly stop covering those paths.
    b = synthetic_bars()
    fired = [scan_picks(b, e) for e in ENDS]
    assert sum(len(f) for f in fired) >= 30
    assert not any("DOWN" in f for f in fired)
    collapsed = [e for e, f in zip(ENDS, fired) if "PIN" not in f and any(
        p["ticker"] == "PIN" for p in scan.score_tickers(
            {t: b["C"][t].iloc[:e + 1] for t in b["C"].columns}, TH))]
    assert collapsed, "vol-collapse never removed PIN from a pick list"
    assert {p["signal"] for f in fired for p in f.values()} >= {"🔵", "🟢", "🟡"}


@pytest.mark.parametrize("end", ENDS)
def test_panel_matches_scan_session_by_session(end):
    b = synthetic_bars()
    theirs, mine = scan_picks(b, end), panel_picks(b, end)
    assert sorted(mine) == sorted(theirs)
    for t, p in theirs.items():
        m = mine[t]
        assert m["score"] == pytest.approx(p["score"], abs=0.11)
        assert m["rsi2"] == pytest.approx(p["rsi2"], abs=0.006)
        assert m["dist5"] == pytest.approx(p["dist_5dma_pct"], abs=0.006)
        assert m["dist200"] == pytest.approx(p["dist_200dma_pct"], abs=0.006)
        assert m["freq"] == p["freq_60d"]
        assert m["target"] == pytest.approx(p["target_price"], abs=0.006)
        assert m["stop"] == pytest.approx(p["stop_price"], abs=0.006)


def test_spells_count_consecutive_sessions():
    pick = pd.DataFrame({"A": [1, 1, 0, 1, 1, 1], "B": [0, 1, 1, 1, 0, 0]}, dtype=bool)
    assert spells(pick)["A"].tolist() == [1, 2, 0, 1, 2, 3]
    assert spells(pick)["B"].tolist() == [0, 1, 2, 3, 0, 0]


def one_trade(highs, lows, opens, closes, entry_close=100.0, target=103.0, stop=90.0):
    """A single ticker whose signal fires on bar 0, with the window after it."""
    n = len(closes) + 1
    C = np.array([[entry_close] + list(closes)]).T
    H = np.array([[entry_close] + list(highs)]).T
    L = np.array([[entry_close] + list(lows)]).T
    O = np.array([[entry_close] + list(opens)]).T
    spy = np.linspace(100, 101, n)
    sig = pd.DataFrame(dict(i=[0], j=[0], target=[target], stop=[stop], score=[50.0]))
    return sig, O, H, L, C, spy


def test_resolve_target_first_and_gap_fill():
    pad = [100.0] * (W + 1)
    sig, O, H, L, C, spy = one_trade(
        highs=[101, 105] + pad[2:], lows=[99, 99] + pad[2:],
        opens=[100, 104] + pad[2:], closes=[100, 102] + pad[2:])
    out = resolve(sig, O, H, L, C, spy, spy, "close").iloc[0]
    assert out.outcome == "WON" and out.exit_day == 2
    assert out.result == pytest.approx(3.0)
    assert out.gap == pytest.approx(4.0)          # gapped open 104 beats the 103 limit


def test_resolve_same_day_double_touch_is_won_and_stop_gaps_down():
    pad = [100.0] * (W + 1)
    sig, O, H, L, C, spy = one_trade(
        highs=[104] + pad[1:], lows=[85] + pad[1:], opens=[100] + pad[1:], closes=[95] + pad[1:])
    assert resolve(sig, O, H, L, C, spy, spy, "close").iloc[0].outcome == "WON"
    sig, O, H, L, C, spy = one_trade(
        highs=[100, 99] + pad[2:], lows=[95, 80] + pad[2:], opens=[100, 85] + pad[2:],
        closes=[96, 82] + pad[2:])
    out = resolve(sig, O, H, L, C, spy, spy, "close").iloc[0]
    assert out.outcome == "LOST" and out.result == pytest.approx(-10.0)
    assert out.gap == pytest.approx(-15.0)


def test_resolve_expired_and_next_open_skip():
    flat = [101.0] * (W + 1)
    sig, O, H, L, C, spy = one_trade(highs=flat, lows=[99.0] * (W + 1), opens=flat,
                                     closes=[100.0] * (W - 1) + [101.5, 101.0])
    out = resolve(sig, O, H, L, C, spy, spy, "close").iloc[0]
    assert out.outcome == "EXPIRED" and out.exit_day == W
    assert out.result == pytest.approx(1.5)
    sig, O, H, L, C, spy = one_trade(highs=[106] + flat[1:], lows=[99.0] * (W + 1),
                                     opens=[104] + flat[1:], closes=flat)
    assert resolve(sig, O, H, L, C, spy, spy, "next-open").iloc[0].outcome == "SKIP"


def test_daily_returns_compound_to_the_trade_and_pay_cost():
    C = np.array([[100.0], [102.0], [99.0], [101.0]])
    tr = pd.DataFrame(dict(i=[0], j=[0], exit_day=[3], entry_px=[100.0], exit_px=[103.0]))
    (k0, r), = trade_daily_returns(tr, C, cost_bps=10)
    assert k0 == 1
    assert r.tolist() == pytest.approx([.02, 99 / 102 - 1, 103 / 99 - 1 - .001])


def test_ew_open_and_slots():
    daily = [(1, np.array([.01, .01])), (2, np.array([.03])), (2, np.array([-.02]))]
    r, n = ew_open(daily, 4)
    assert r.tolist() == pytest.approx([0, .01, (.01 + .03 - .02) / 3, 0])
    assert n.tolist() == [0, 1, 3, 0]
    tr = pd.DataFrame(dict(i=[0, 1, 1], score=[50.0, 60.0, 45.0], ticker=["A", "B", "C"]))
    idle = np.full(4, .001)
    r, n = slotted(tr, daily, 4, 2, idle)
    # Day 2: slot 1 held by trade 0, one free slot goes to the higher score (trade 1).
    assert n.tolist() == [0, 1, 2, 0]
    assert r[1] == pytest.approx(.01 / 2 + .5 * .001)
    assert r[2] == pytest.approx((.01 + .03) / 2)


def test_slots_hold_one_position_per_ticker():
    # A's day-1 signal is still open on day 2, so A's day-2 signal is
    # skipped even with a free slot, and the slot goes to C.
    daily = [(1, np.array([.01, .01])), (2, np.array([.03])), (2, np.array([-.02]))]
    tr = pd.DataFrame(dict(i=[0, 1, 1], score=[50.0, 60.0, 45.0], ticker=["A", "A", "C"]))
    r, n = slotted(tr, daily, 4, 3, np.zeros(4))
    assert n.tolist() == [0, 1, 2, 0]
    assert r[2] == pytest.approx((.01 - .02) / 3)
