"""Unit tests for replay_board.py's pure logic. The load-bearing one: the
vectorized board must reproduce scan.py's score_tickers + filter_vol_collapse
on the same closes, session by session; a replay of a different board would
answer a different question. Also the universe mask, hysteresis holdings,
the cost model, partial-session trimming and the split-vs-dilution share
classification. No network.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    --with 'pytest' pytest test_replay_board.py
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import scan
from replay_board import (SCAN, board_ranks, complete_sessions, dilution_suspects,
                          hysteresis, net_returns)

N_DAYS = 100


def synthetic_closes(seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-01-02", periods=N_DAYS)
    cols = {}
    # Smooth uptrends with different slope / noise mixes; most clear the
    # filter, at different scores.
    for k, (drift, vol) in enumerate([(.006, .010), (.008, .020), (.005, .004),
                                      (.010, .030), (.0045, .006), (.007, .015)]):
        r = rng.normal(drift, vol, N_DAYS)
        r[0] = 0
        cols[f"UP{k}"] = 50 * np.exp(np.cumsum(r))
    # Buyout pin: drifts, gaps +45% early in the scoring window, then sits.
    pin = 40 * np.exp(np.cumsum(np.r_[0, rng.normal(.002, .015, N_DAYS - 1)]))
    gap = N_DAYS - 52
    pin[gap:] = pin[gap - 1] * 1.45 * (1 + rng.normal(0, .0004, N_DAYS - gap))
    cols["PIN"] = pin
    # Steady climber that breaks down 30% at the end (fails max drawdown).
    deep = 30 * np.exp(np.cumsum(np.r_[0, np.full(N_DAYS - 1, .01)]))
    deep[-15:] *= .7
    cols["DEEP"] = deep
    cols["FLAT"] = 20 * np.exp(np.cumsum(rng.normal(0, .01, N_DAYS)))
    return pd.DataFrame(cols, index=idx)


def scan_board(closes: pd.DataFrame) -> tuple[list[str], list[str]]:
    picks = scan.score_tickers(closes, SCAN.window_months, SCAN.min_return_pct, SCAN.max_dd_pct)
    kept, excluded = scan.filter_vol_collapse(picks, closes, SCAN.window_months,
                                              SCAN.vol_collapse_ratio)
    return [p["ticker"] for p in kept][:SCAN.top_n], [p["ticker"] for p in excluded]


def everywhere(closes: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(True, index=closes.index, columns=closes.columns)


def test_synthetic_panel_exercises_every_branch():
    # Guard on the fixture itself: if a seed change stops the pin tripping
    # vol-collapse or the drawdown name tripping the filter, the parity test
    # below would quietly stop covering those paths.
    closes = synthetic_closes()
    kept, excluded = scan_board(closes)
    assert "PIN" in excluded
    assert "DEEP" not in kept and "FLAT" not in kept
    assert len(kept) >= 4


@pytest.mark.parametrize("end", [64, 75, 88, N_DAYS - 1])
def test_board_matches_scan_session_by_session(end):
    closes = synthetic_closes()
    rank, _ = board_ranks(closes, everywhere(closes))
    mine = rank.iloc[end].dropna().sort_values().index.tolist()
    theirs, _ = scan_board(closes.iloc[:end + 1])
    assert mine == theirs


def test_board_is_empty_before_a_full_window():
    closes = synthetic_closes()
    rank, _ = board_ranks(closes, everywhere(closes))
    assert rank.iloc[:scan.window_trading_days(SCAN.window_months) - 1].isna().all().all()


def test_universe_mask_removes_a_name_only_while_excluded():
    closes = synthetic_closes()
    uni = everywhere(closes)
    leader = board_ranks(closes, uni)[0].iloc[-1].idxmin()
    uni.loc[uni.index[-1], leader] = False
    rank, _ = board_ranks(closes, uni)
    assert np.isnan(rank.iloc[-1][leader])
    assert not np.isnan(rank.iloc[-2][leader])               # still listed the day before


def test_hysteresis_enters_at_k_and_holds_until_off_the_exit_depth():
    idx = pd.bdate_range("2026-01-05", periods=5)
    rank = pd.DataFrame({"A": [12, 8, 25, 31, np.nan],      # enters day 1, exits day 3
                         "B": [15, 20, 28, 11, 9],          # never reaches top 10 until day 4
                         "C": [3, np.nan, 2, 2, 2]},        # drops off on day 1, re-enters day 2
                        index=idx, dtype=float)
    held = hysteresis(rank, enter=10, exit_=30)
    assert held["A"].tolist() == [False, True, True, False, False]
    assert held["B"].tolist() == [False, False, False, False, True]
    assert held["C"].tolist() == [True, False, True, True, True]


def test_net_returns_charges_half_the_round_trip_per_unit_traded():
    idx = pd.bdate_range("2026-01-05", periods=3)
    W = pd.DataFrame({"A": [1.0, 0.0, 0.0], "B": [0.0, 1.0, 1.0]}, index=idx)
    R = pd.DataFrame({"A": [.01, .02, .03], "B": [.10, .20, np.nan]}, index=idx)
    r = net_returns(W, R, cost_bps=10)
    # day 0 buys 1.0 (5bp); day 1 swaps A->B, 2.0 traded (10bp); day 2 holds, NaN return = 0
    assert r.round(6).tolist() == [round(.01 - .0005, 6), round(.20 - .0010, 6), 0.0]


def test_complete_sessions_drops_the_session_still_trading_at_fetch_time():
    idx = pd.bdate_range("2026-10-01", periods=4)          # Thu..Tue
    close = pd.DataFrame({"A": [1.0] * 4, "B": [1.0] * 4, "C": [1.0, 1.0, 1.0, np.nan]}, index=idx)
    bars = {"Close": close, "Open": close}
    et = ZoneInfo("America/New_York")
    intraday = complete_sessions(bars, datetime(2026, 10, 6, 11, 15, tzinfo=et))
    assert intraday["Close"].index[-1] == pd.Timestamp("2026-10-05")
    after = complete_sessions(bars, datetime(2026, 10, 6, 20, 0, tzinfo=et))
    # Fetched after the close: the session stays unless Yahoo published under half of it.
    assert after["Close"].index[-1] == pd.Timestamp("2026-10-06")
    close.iloc[-1, :2] = np.nan                           # 1 of 3 published
    assert complete_sessions(bars, datetime(2026, 10, 6, 20, 0, tzinfo=et))["Close"].index[-1] \
        == pd.Timestamp("2026-10-05")


def test_dilution_suspects_skips_splits_and_buybacks():
    d = pd.to_datetime(["2023-01-31", "2023-04-30", "2024-01-31", "2025-01-31"])
    hist = {
        "SPLIT": pd.Series([60e6, 60e6, 61e6, 600e6], index=d),    # 10:1 split
        "ISSUER": pd.Series([100e6, 105e6, 130e6, 160e6], index=d),
        "BUYBACK": pd.Series([100e6, 98e6, 90e6, 85e6], index=d),
        "MILD": pd.Series([100e6, 101e6, 110e6, 120e6], index=d),
        "EMPTY": None,
    }
    now = pd.Series({"SPLIT": 605e6, "ISSUER": 160e6, "BUYBACK": 85e6, "MILD": 120e6, "EMPTY": 1e6})
    assert dilution_suspects(hist, now, pd.Timestamp("2023-07-01")) == {"ISSUER"}
