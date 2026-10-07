"""Unit tests for replay_bases.py's pure logic. The load-bearing one: the
per-session pick list must reproduce scan.py's compute_rs_ratings +
score_tickers + filter_vol_collapse on the same bars, session by session;
a replay of a different watchlist would answer a different question. Also
episode building, the canonical trade, and the slot portfolio. No network.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    --with 'pytest' pytest test_replay_bases.py
"""

import numpy as np
import pandas as pd
import pytest

import scan
from replay_bases import (SCAN, HORIZON, STOP_PCT, episodes, resolve, scan_panel,
                          slotted, trade_daily_returns)

N_DAYS = 340


def synthetic_panel(seed: int = 7):
    """Leaders that trend for a year and then base (some break out late),
    laggards that fail RS or the trend template, a same-issuer pair, and a
    buyout pin that bases on a collapsed volatility."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-02", periods=N_DAYS)
    C, V = {}, {}

    def leader(drift, noise, band, breakout_at=None):
        r = rng.normal(drift, noise, N_DAYS)
        r[0] = 0
        p = 40 * np.exp(np.cumsum(r))
        lvl = p[249]
        x = 0.0
        for i in range(250, N_DAYS):            # mean-reverting base under a ceiling
            x = 0.8 * x + rng.normal(0, band)
            p[i] = lvl * (1 + min(x, band * 1.5) - band)
        if breakout_at:
            p[breakout_at:] *= 1.12
        return p

    for k, (d, nz, band, bo) in enumerate([(.004, .012, .03, None), (.005, .015, .04, 318),
                                           (.0045, .010, .025, None), (.006, .018, .05, 330),
                                           (.004, .011, .02, 300), (.0035, .009, .035, None)]):
        C[f"LEAD{k}"] = leader(d, nz, band, bo)
    for k in range(6):
        C[f"LAG{k}"] = 30 * np.exp(np.cumsum(np.r_[0, rng.normal(.0002, .016, N_DAYS - 1)]))
    base = leader(.0042, .012, .03)
    C["GOOG"] = base * (1 + rng.normal(0, .002, N_DAYS))
    C["GOOGL"] = base * (1 + rng.normal(0, .002, N_DAYS))
    pin = leader(.005, .015, .04)
    pin[290:] = pin[289] * 1.2 * (1 + np.cumsum(rng.normal(0, .0003, N_DAYS - 290)))
    C["PIN"] = pin
    C = pd.DataFrame(C, index=idx)
    for t in C.columns:
        v = rng.lognormal(np.log(2e6), 0.3, N_DAYS)
        v[250:] *= rng.uniform(0.5, 1.0)        # some dry up during the base
        V[t] = v
    V = pd.DataFrame(V, index=idx)
    spy = pd.Series(400 * np.exp(np.cumsum(np.r_[0, rng.normal(.0004, .008, N_DAYS - 1)])), index=idx)
    return C, V, spy


def scan_picks(C, V, spy, end):
    closes = {t: C[t].iloc[:end + 1].dropna() for t in C.columns}
    vols = {t: V[t].iloc[:end + 1].dropna() for t in V.columns}
    rs = scan.compute_rs_ratings(closes)
    picks = scan.score_tickers(
        closes, vols, spy.iloc[:end + 1], rs,
        min_base_weeks=SCAN.min_base_weeks, max_base_weeks=SCAN.max_base_weeks,
        max_base_width_pct=SCAN.max_base_width, max_to_52w_high_pct=SCAN.max_to_52w_high,
        min_rs_rating=SCAN.min_rs_rating, min_base_score=SCAN.min_base_score,
        smoothness_band_pct=SCAN.smoothness_band_pct)
    kept, excluded = scan.filter_vol_collapse(picks, closes, scan.VOL_COLLAPSE_LOOKBACK_MONTHS,
                                              SCAN.vol_collapse_ratio)
    return {p["ticker"]: p for p in kept}, {p["ticker"] for p in excluded}


@pytest.fixture(scope="module")
def panel():
    C, V, spy = synthetic_panel()
    universe = pd.DataFrame(True, index=C.index, columns=C.columns)
    return C, V, spy, scan_panel(C, V, spy, universe)


ENDS = list(range(255, N_DAYS))


def test_synthetic_panel_exercises_every_branch(panel):
    # If a seed change stops the fixture listing names, breaking out of a
    # base (mode 3), tripping the dedup or the vol-collapse filter, the
    # parity test would quietly stop covering those paths.
    C, V, spy, mine = panel
    theirs = [scan_picks(C, V, spy, e) for e in ENDS]
    assert sum(len(k) for k, _ in theirs) >= 60
    assert any(p.get("anchor_mode") == 3 for k, _ in theirs for p in k.values())
    assert any("PIN" in ex for _, ex in theirs)
    assert any(("GOOG" in k) != ("GOOGL" in k) and {"GOOG", "GOOGL"} & set(k) for k, _ in theirs)
    assert len({t for k, _ in theirs for t in k}) < len(C.columns)   # some names never list


@pytest.mark.parametrize("end", ENDS)
def test_picks_match_scan_session_by_session(panel, end):
    C, V, spy, mine = panel
    theirs, _ = scan_picks(C, V, spy, end)
    day = mine[mine.date == C.index[end]].set_index("ticker")
    assert sorted(day.index) == sorted(theirs)
    for t, p in theirs.items():
        m = day.loc[t]
        assert m.score == pytest.approx(p["base_score"], abs=0.11)
        assert m.base_weeks == p["base_weeks"]
        assert m.pivot == pytest.approx(p["pivot_price"], abs=0.006)
        assert m.to_pivot == pytest.approx(p["to_pivot_pct"], abs=0.006)
        assert m.width == pytest.approx(p["width_pct"], abs=0.051)


def test_episodes_split_on_gaps():
    dates = pd.bdate_range("2026-01-05", periods=8)
    picks = pd.DataFrame(dict(
        date=dates[[0, 1, 2, 4, 5, 1]], ticker=["A", "A", "A", "A", "A", "B"],
        pivot=[10, 10.5, 11, 12, 12, 50], score=[50, 55, 60, 45, 45, 70], base_weeks=[20] * 6,
        to_pivot=[-2] * 6, width=[8] * 6, bb=[10] * 6, vol_dryup=[0.9] * 6, rs_slope=[0.5] * 6,
        rs=[80] * 6, tight=[False] * 6))
    E = episodes(picks, dates).sort_values(["ticker", "start"])
    assert E[["ticker", "start", "end"]].values.tolist() == [["A", 0, 2], ["A", 4, 5], ["B", 1, 1]]
    assert list(E.iloc[0].pivots) == [10, 10.5, 11]


def _one(highs, lows, opens, closes):
    T = len(closes)
    a = lambda x: np.array(x, dtype=float)[:, None]
    spy = np.linspace(100, 101, T)
    return a(opens), a(highs), a(lows), a(closes), spy, spy


def test_trade_fills_at_the_pivot_and_holds_to_the_horizon():
    T = HORIZON + 6
    closes = [100.0] * T
    highs = [100.0, 100.0, 103.0] + [104.0] * (T - 3)     # day 2 touches 102
    O, H, L, C, so, sc = _one(highs, [99.0] * T, [100.0, 100.0, 101.0] + [103.0] * (T - 3),
                             [100.0, 100.0, 103.0] + [104.0] * (T - 3))
    ep = pd.DataFrame(dict(ticker=["A"], start=[0], end=[3], pivots=[np.array([102.0] * 4)],
                           score=[50.0], base_weeks=[20.0], to_pivot=[-2.0], width=[8.0], bb=[10.0],
                           vol_dryup=[0.9], rs_slope=[0.5], rs=[80.0], tight=[False]))
    r = resolve(ep, O, H, L, C, {"A": 0}, so, sc, T).iloc[0]
    assert r.trig == 2 and r.fill == 102.0 and not r.stop_hit
    assert r.trade == pytest.approx((104 / 102 - 1) * 100)
    assert r.exit == HORIZON


def test_trade_stop_is_gap_aware_and_day0_reads_the_close():
    T = HORIZON + 6
    O, H, L, C, so, sc = _one([100, 106] + [100] * (T - 2), [99, 90] + [80] * (T - 2),
                             [100, 105] + [85] * (T - 2), [100, 101] + [85] * (T - 2))
    ep = pd.DataFrame(dict(ticker=["A"], start=[0], end=[0], pivots=[np.array([102.0])],
                           score=[50.0], base_weeks=[8.0], to_pivot=[-2.0], width=[8.0], bb=[10.0],
                           vol_dryup=[0.9], rs_slope=[0.5], rs=[80.0], tight=[False]))
    r = resolve(ep, O, H, L, C, {"A": 0}, so, sc, T).iloc[0]
    # Gapped open 105 fills above the 102 pivot; the day-1 low of 90 is
    # ignored (the fill may have come after it), its close of 101 isn't a stop.
    assert r.trig == 1 and r.fill == 105.0 and r.gap == pytest.approx((105 / 102 - 1) * 100)
    assert r.stop_hit and r.exit == 1
    assert r.trade == -STOP_PCT
    assert r.trade_gap == pytest.approx((85 / 105 - 1) * 100)   # opened under the 96.6 stop


def test_untriggered_episode_and_open_window():
    T = 10
    O, H, L, C, so, sc = _one([100] * T, [99] * T, [100] * T, [100] * T)
    ep = pd.DataFrame(dict(ticker=["A", "A"], start=[0, 7], end=[2, 9],
                           pivots=[np.array([105.0] * 3), np.array([105.0] * 3)],
                           score=[50.0] * 2, base_weeks=[8.0] * 2, to_pivot=[-5.0] * 2, width=[8.0] * 2,
                           bb=[10.0] * 2, vol_dryup=[0.9] * 2, rs_slope=[0.5] * 2, rs=[80.0] * 2,
                           tight=[False] * 2))
    r = resolve(ep, O, H, L, C, {"A": 0}, so, sc, T)
    assert len(r) == 1 and not r.iloc[0].triggered          # the second is still listed


def test_slots_one_position_per_ticker_and_daily_path():
    C = np.array([[100.0], [102.0], [99.0], [101.0]])
    tr = pd.DataFrame(dict(ticker=["A"], trig=[1], exit=[2], fill=[100.0], exit_px=[103.0], score=[50.0]))
    (k0, r), = trade_daily_returns(tr, C, {"A": 0}, cost_bps=10)
    assert k0 == 1
    assert r.tolist() == pytest.approx([.02, 99 / 102 - 1, 103 / 99 - 1 - .001])
    daily = [(1, np.array([.01, .01])), (2, np.array([.03])), (2, np.array([-.02]))]
    tr = pd.DataFrame(dict(trig=[1, 2, 2], score=[50.0, 60.0, 45.0], ticker=["A", "A", "C"]))
    rr, n = slotted(tr, daily, 4, 3, np.zeros(4))
    assert n.tolist() == [0, 1, 2, 0]
    assert rr[2] == pytest.approx((.01 - .02) / 3)
