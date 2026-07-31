"""Pure-logic tests for snapback-scan — no network.

Run: uv run --with pandas --with 'yfinance>=1.3,<2' --with pytest pytest test_build_snapback.py
(yfinance is imported by the module; every network call site is stubbed here.)
"""
import json
from datetime import date

import pandas as pd

import build_snapback as bp

TODAY = date(2026, 7, 31)

MR_COLS = ("run_id,run_date,ticker,rank,score_rank,score,rsi2,dist_5dma_pct,"
           "dist_50dma_pct,dist_200dma_pct,last_close,target_price,stop_price,"
           "signal,freq_60d")


def _mr_row(run_id, ticker, rank, score, last_close=100.0):
    return (f"{run_id},2026-07-30T00:00:00+00:00,{ticker},{rank},{rank},{score},"
            f"2.0,-5.0,-8.0,10.0,{last_close},{last_close*1.05},{last_close*0.9},"
            f"🔵,1")


def _bars(closes_by_ticker: dict[str, list], dates: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    cols = pd.MultiIndex.from_product(
        [list(closes_by_ticker), ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for tk, closes in closes_by_ticker.items():
        for i, c in enumerate(closes):
            if c is None:
                continue
            for f, v in (("Open", c), ("High", c + 1), ("Low", c - 1),
                         ("Close", c), ("Volume", 1000.0)):
                df.loc[idx[i], (tk, f)] = v
    return df


# --------------------------------------------------------------------------- #
# load_kegs — the backtest profile: score >= 40 AND consecutive age <= 2 runs
# --------------------------------------------------------------------------- #
def test_load_kegs_age_and_score_filters(tmp_path, monkeypatch):
    rows = [MR_COLS]
    rows += [_mr_row("20260728", t, i + 1, 60) for i, t in enumerate(["AGE3", "GAPPY"])]
    rows += [_mr_row("20260729", t, i + 1, 60) for i, t in enumerate(["AGE2", "AGE3"])]
    rows += [_mr_row("20260730", "FRESH", 1, 70),
             _mr_row("20260730", "AGE2", 2, 68),
             _mr_row("20260730", "AGE3", 3, 66),
             _mr_row("20260730", "GAPPY", 4, 64),   # in 28 + 30, NOT 29 → age 1
             _mr_row("20260730", "LOW", 5, 30)]     # score < 40 → out
    f = tmp_path / "history.csv"
    f.write_text("\n".join(rows))
    monkeypatch.setattr(bp, "MR_HISTORY", f)

    kegs, meta = bp.load_kegs(40.0, 2, 20, [], TODAY)
    assert [k["ticker"] for k in kegs] == ["FRESH", "AGE2", "GAPPY"]
    ages = {k["ticker"]: k["listing_age_runs"] for k in kegs}
    assert ages == {"FRESH": 1, "AGE2": 2, "GAPPY": 1}   # a gap resets the streak
    assert meta["run_id"] == "20260730" and meta["stale_days"] == 1
    assert meta["prior_run"]["run_id"] == "20260729"


# --------------------------------------------------------------------------- #
# join_sparks — arming is scoped; marketwide prints are context, not sparks
# --------------------------------------------------------------------------- #
def _kegs():
    return [{"ticker": "AAA", "sector": "Technology"},
            {"ticker": "BBB", "sector": "Healthcare"},
            {"ticker": "CCC", "sector": None}]


VERDICTS = [
    {"symbol": "MSFT", "sector": "Technology", "mktcap": 3e12,
     "date": "2026-08-03", "slot": "AMC"},
    {"symbol": "BIGX", "sector": None, "mktcap": 9e11,
     "date": "2026-08-04", "slot": "BMO"},
]


def test_marketwide_never_arms():
    kegs = _kegs()
    bp.join_sparks(kegs, {}, VERDICTS, [])
    armed = {k["ticker"]: k["armed"] for k in kegs}
    assert armed == {"AAA": True, "BBB": False, "CCC": False}
    aaa = next(k for k in kegs if k["ticker"] == "AAA")
    assert {s["type"] for s in aaa["sparks"]} == {"sector_verdict", "marketwide_verdict"}
    bbb = next(k for k in kegs if k["ticker"] == "BBB")
    assert all(s["type"] == "marketwide_verdict" for s in bbb["sparks"])


def test_own_earnings_and_macro_arm():
    kegs = _kegs()
    own = {"CCC": {"date": "2026-08-01", "slot": "AMC", "eps_forecast": "1.00"}}
    macro = [{"date": "2026-08-01", "time_et": "08:30", "title": "CPI y/y"}]
    bp.join_sparks(kegs, own, [], macro)
    assert all(k["armed"] for k in kegs)          # macro is a verdict for everyone
    ccc = next(k for k in kegs if k["ticker"] == "CCC")
    assert any(s["type"] == "own_earnings" for s in ccc["sparks"])
    assert "coin flip" in next(s["detail"] for s in ccc["sparks"]
                               if s["type"] == "own_earnings")


# --------------------------------------------------------------------------- #
# tape_check — setup metrics freeze at the signal day; ignition reads the tail
# --------------------------------------------------------------------------- #
def test_setup_slices_at_signal_and_chase_guard(monkeypatch):
    dates = ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23",
             "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
             "2026-07-30"]                                   # last bar is post-signal
    closes = {"SIG": [100, 99, 98, 97, 96, 90, 84, 76, 83.6],   # +10% after signal
              "QUIET": [100, 100.5, 100, 99.8, 99.5, 99.6, 99.4, 99.2, 99.3]}
    monkeypatch.setattr(bp.yf, "download", lambda *a, **k: _bars(closes, dates))
    kegs = [{"ticker": "SIG", "signal_close": 76.0},
            {"ticker": "QUIET", "signal_close": 99.2}]
    bp.tape_check(kegs, signal_date=date(2026, 7, 29), errors=[])

    sig = kegs[0]
    assert sig["since_signal_pct"] == 10.0 and sig["ignited"]
    assert sig["signal_day_low"] == 75.0            # signal bar, not the bounce bar
    # 5-session return spans 6 closes (iloc[-6] → 98), on the setup slice only
    assert sig["ret_5d_pct"] == round((76 / 98 - 1) * 100, 2)
    # 8 setup closes → 7 returns, all negative
    assert sig["down_streak"] == 7 and not sig["quiet_warning"]

    quiet = kegs[1]
    assert quiet["quiet_warning"] and not quiet["ignited"]


# --------------------------------------------------------------------------- #
# prior_review — full sample, both tails, own packet preferred over MR
# --------------------------------------------------------------------------- #
def test_prior_review_full_sample_stats(tmp_path, monkeypatch):
    pk = {"today": "2026-07-30",
          "kegs": [{"ticker": "AAA", "latest_close": 100.0, "armed": True},
                   {"ticker": "BBB", "latest_close": 200.0, "armed": False}]}
    (tmp_path / "2026-07-30.json").write_text(json.dumps(pk))
    monkeypatch.setattr(bp, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(bp.yf, "download", lambda *a, **k: _bars(
        {"AAA": [110.0], "BBB": [180.0]}, ["2026-07-31"]))

    r = bp.prior_review({"run_id": "20260729"}, [], TODAY)
    assert r["source"] == "snapback:2026-07-30" and r["n"] == 2
    assert r["pct_positive"] == 50.0 and r["avg_pct"] == 0.0
    assert r["best"][0] == {"ticker": "AAA", "since_pct": 10.0}
    assert r["worst"][0] == {"ticker": "BBB", "since_pct": -10.0}
    assert r["armed_subset"] == {"n": 1, "pct_positive": 100.0,
                                 "avg_pct": 10.0, "median_pct": 10.0}


# --------------------------------------------------------------------------- #
# calendar — spark days are NYSE days, not weekdays
# --------------------------------------------------------------------------- #
def test_next_trading_days_skips_observed_holiday():
    # July 4 2026 is a Saturday → observed Friday 2026-07-03 (NYSE closed).
    assert not bp.is_trading_day(date(2026, 7, 3))
    assert bp.next_trading_days(date(2026, 7, 2), 2) == [date(2026, 7, 6),
                                                         date(2026, 7, 7)]
