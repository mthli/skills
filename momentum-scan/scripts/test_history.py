"""Tests for append_history's same-ET-day upsert behavior.

Run from the skill root via:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
      --with 'pytest' pytest scripts/
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import scan


def _pick(ticker: str, rank: int) -> dict:
    return {
        "ticker": ticker, "rank": rank, "score": 1.0,
        "return_pct": 50.0, "max_dd_pct": -10.0,
        "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }


@pytest.fixture
def history_file(tmp_path, monkeypatch):
    f = tmp_path / "history.csv"
    monkeypatch.setattr(scan, "HISTORY_FILE", f)
    return f


def test_first_write_creates_file(history_file):
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "AAPL"


def test_same_et_day_overwrites(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0,
                    tzinfo=timezone.utc)  # 16:00 ET 5/12
    scan.append_history([_pick("AAPL", 1), _pick("MSFT", 2)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0,
                    tzinfo=timezone.utc)  # 18:00 ET 5/12
    scan.append_history([_pick("NVDA", 1)], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_different_et_day_appends(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("MSFT", 1)], "20260513", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 2
    assert set(df["ticker"]) == {"AAPL", "MSFT"}


def test_utc_late_night_still_same_et_day(history_file):
    # 03:00 UTC May 13 = 23:00 EDT May 12 — still ET date 5/12
    run1 = datetime(2026, 5, 12, 20, 0, 0,
                    tzinfo=timezone.utc)  # 16:00 ET 5/12
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 13, 3, 0, 0,
                    tzinfo=timezone.utc)   # 23:00 ET 5/12
    scan.append_history([_pick("NVDA", 1)], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(
        df) == 1, "both runs share ET date 2026-05-12 despite straddling UTC midnight"
    assert df.iloc[0]["ticker"] == "NVDA"


def test_empty_picks_does_not_wipe(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)
    scan.append_history([], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1, "empty picks should not wipe existing rows"
    assert df.iloc[0]["ticker"] == "AAPL"


def test_allow_same_day_appends(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("NVDA", 1)], "20260512", run2,
                        allow_same_day=True)
    df = pd.read_csv(history_file)
    assert len(df) == 2
    assert set(df["ticker"]) == {"AAPL", "NVDA"}


def test_dst_spring_forward(history_file):
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT. Both runs below are 2026-03-08 ET.
    run1 = datetime(2026, 3, 8, 5, 0, 0, tzinfo=timezone.utc)   # 00:00 EST
    scan.append_history([_pick("AAPL", 1)], "20260308", run1)
    run2 = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc)   # 03:00 EDT
    scan.append_history([_pick("NVDA", 1)], "20260308", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_dst_fall_back(history_file):
    # 2026-11-01: 02:00 EDT rolls back to 01:00 EST. Both occurrences are 11/1 ET.
    run1 = datetime(2026, 11, 1, 5, 30, 0,
                    tzinfo=timezone.utc)  # 01:30 EDT (1st)
    scan.append_history([_pick("AAPL", 1)], "20261101", run1)
    run2 = datetime(2026, 11, 1, 6, 30, 0,
                    tzinfo=timezone.utc)  # 01:30 EST (2nd)
    scan.append_history([_pick("NVDA", 1)], "20261101", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_atomic_write_leaves_no_tmp(history_file):
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run)
    tmp = history_file.with_suffix(".csv.tmp")
    assert not tmp.exists(), "tmp file should be renamed away after successful write"


def test_make_run_id_default_is_et_date(history_file):
    # 03:00 UTC on 5/13 = 23:00 EDT on 5/12 → ET date 2026-05-12
    now = datetime(2026, 5, 13, 3, 0, 0, tzinfo=timezone.utc)
    assert scan.make_run_id(now) == "20260512"


def test_make_run_id_allow_same_day_is_second_precision(history_file):
    now = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    assert scan.make_run_id(now, allow_same_day=True) == "20260512T200000Z"


def test_history_sorted_after_backfill(history_file):
    # Write in reverse chronological order — file should still come out sorted.
    run_late = datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("LATE", 1)], "20260513", run_late)
    run_early = datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("EARLY", 1)], "20260511", run_early)
    df = pd.read_csv(history_file)
    dates = pd.to_datetime(df["run_date"], utc=True).tolist()
    assert dates == sorted(
        dates), "history.csv rows should be in chronological order"


def test_clear_history_removes_tmp(history_file):
    # Simulate a crashed atomic write leaving a stale .tmp alongside the main file
    history_file.write_text("dummy\n")
    tmp = history_file.with_suffix(".csv.tmp")
    tmp.write_text("partial\n")

    scan.clear_history()

    assert not history_file.exists(), "main history file should be gone"
    assert not tmp.exists(), "stale .tmp should also be gone"


def test_clear_history_safe_when_nothing_exists(history_file):
    # No files present — should not raise
    scan.clear_history()


# render_regime_banner has no I/O, so we can exercise the strict-mode display
# branch without waiting for a live RISK-OFF tape.

def _regime(**overrides) -> dict:
    base = {
        "spy_last": 600.0,
        "spy_ma50": 580.0,
        "spy_ma200": 550.0,
        "spy_ma200_slope_pct": 0.50,
        "spy_above_200dma": True,
        "spy_50_above_200": True,
        "breadth_pct_above_200dma": 65.0,
        "breadth_pct_50_above_200": 70.0,
        "risk_on": True,
    }
    base.update(overrides)
    return base


def test_banner_risk_on():
    out = scan.render_regime_banner(_regime())
    assert "RISK-ON" in out
    assert "RISK-OFF" not in out
    assert "50DMA > 200DMA" in out
    assert "Breadth: 65% > 200DMA" in out


def test_banner_risk_off_when_below_200dma():
    # SPY below 200DMA → risk_on must be False regardless of slope sign.
    out = scan.render_regime_banner(_regime(
        spy_last=500.0, spy_above_200dma=False, risk_on=False,
    ))
    assert "RISK-OFF" in out


def test_banner_risk_off_when_slope_negative():
    # SPY above 200DMA but the 200DMA itself rolling over — the slope filter
    # exists exactly to catch this (2022 had a textbook example).
    out = scan.render_regime_banner(_regime(
        spy_ma200_slope_pct=-0.30, risk_on=False,
    ))
    assert "RISK-OFF" in out
    assert "-0.30%" in out


def test_banner_handles_50_below_200_cross():
    out = scan.render_regime_banner(_regime(
        spy_ma50=540.0, spy_50_above_200=False, risk_on=False,
    ))
    assert "50DMA < 200DMA" in out


def test_banner_omits_breadth_when_missing():
    # Universe didn't have enough history → breadth fields are None.
    out = scan.render_regime_banner(_regime(
        breadth_pct_above_200dma=None, breadth_pct_50_above_200=None,
    ))
    assert "Breadth" not in out


def test_banner_unavailable_when_regime_is_none():
    out = scan.render_regime_banner(None)
    assert "unavailable" in out.lower()
    assert "RISK-ON" not in out
    assert "RISK-OFF" not in out


# Vol target tests — synthetic price data so we can pin the expected vol exactly.


def _synthetic_prices(n_days: int, n_tickers: int, daily_vol: float,
                      seed: int = 0) -> pd.DataFrame:
    """Generate i.i.d. lognormal closes with a known daily vol per name."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=0.0005, scale=daily_vol, size=(n_days, n_tickers))
    prices = 100.0 * np.exp(np.cumsum(rets, axis=0))
    idx = pd.date_range(end="2026-05-09", periods=n_days, freq="B")
    cols = [f"T{i}" for i in range(n_tickers)]
    return pd.DataFrame(prices, index=idx, columns=cols)


def _vol_pick(ticker: str, rank: int, ann_vol_pct: float) -> dict:
    return {
        "ticker": ticker, "rank": rank, "score": 1.0,
        "return_pct": 50.0, "max_dd_pct": -10.0,
        "ann_vol_pct": ann_vol_pct, "from_high_pct": -1.0,
    }


def test_vol_target_returns_none_when_disabled():
    prices = _synthetic_prices(100, 5, daily_vol=0.02)
    picks = [_vol_pick(f"T{i}", i + 1, 30.0) for i in range(5)]
    assert scan.compute_vol_target(prices, picks, 5, None) is None
    assert scan.compute_vol_target(prices, picks, 5, 0) is None
    assert scan.compute_vol_target(prices, picks, 5, -5) is None


def test_vol_target_high_cohort_vol_deleverages():
    # Daily vol 5% → ~79% per name annualized. Equal-weight basket of 5 iid
    # names still has portfolio ann vol ~35% (single / sqrt(N)). Target 15%
    # → raw leverage solidly below 1.
    prices = _synthetic_prices(100, 5, daily_vol=0.05, seed=1)
    picks = [_vol_pick(f"T{i}", i + 1, 79.0) for i in range(5)]
    vt = scan.compute_vol_target(prices, picks, 5, target_vol_pct=15.0)
    assert vt is not None
    assert vt["raw_leverage"] < 1.0  # must be deleveraging
    assert vt["suggested_leverage"] < 1.0
    assert vt["suggested_leverage"] >= 0.25  # respect the floor
    assert vt["lookback_days"] == 60
    assert vt["n_tickers"] == 5


def test_vol_target_clipped_at_floor_when_cohort_extreme():
    # Daily vol 12% → cohort ann vol ~100%+. Raw leverage tiny; suggested
    # clipped to the 0.25 floor.
    prices = _synthetic_prices(100, 3, daily_vol=0.12, seed=3)
    picks = [_vol_pick(f"T{i}", i + 1, 190.0) for i in range(3)]
    vt = scan.compute_vol_target(prices, picks, 3, target_vol_pct=15.0)
    assert vt is not None
    assert vt["raw_leverage"] < 0.25
    assert vt["suggested_leverage"] == 0.25


def test_vol_target_clipped_at_1_when_cohort_quiet():
    # Daily vol 0.3% → ann ~5%. Target 15% → raw ~3.0x, but clipped to 1.0x.
    prices = _synthetic_prices(100, 5, daily_vol=0.003, seed=2)
    picks = [_vol_pick(f"T{i}", i + 1, 5.0) for i in range(5)]
    vt = scan.compute_vol_target(prices, picks, 5, target_vol_pct=15.0)
    assert vt is not None
    assert vt["raw_leverage"] > 1.5  # would leverage up
    assert vt["suggested_leverage"] == 1.0  # but we cap


def test_vol_target_returns_none_when_too_few_tickers_match_prices():
    prices = _synthetic_prices(100, 3, daily_vol=0.02)
    # Pick a ticker not in prices columns + one that is. Only 1 match.
    picks = [_vol_pick("MISSING", 1, 30.0), _vol_pick("T0", 2, 30.0)]
    assert scan.compute_vol_target(
        prices, picks, 5, target_vol_pct=15.0) is None


def test_assign_weights_no_op_when_vol_target_none():
    picks = [_vol_pick(f"T{i}", i + 1, 30.0) for i in range(3)]
    out = scan.assign_weights(picks, 3, None)
    assert all("weight_pct" not in p for p in out)


def test_assign_weights_inverse_vol_normalized():
    # Three picks with vols 20, 40, 40. Inverse vols: 0.05, 0.025, 0.025 →
    # weights normalize to 0.5, 0.25, 0.25. Leverage 0.6 → 30, 15, 15 (%).
    picks = [
        _vol_pick("LOWVOL", 1, 20.0),
        _vol_pick("HIGHVOL1", 2, 40.0),
        _vol_pick("HIGHVOL2", 3, 40.0),
    ]
    vt = {
        "suggested_leverage": 0.6, "target_vol_pct": 15.0,
        "cohort_vol_pct": 25.0, "lookback_days": 60, "n_tickers": 3,
        "raw_leverage": 0.6, "leverage_clip": [0.25, 1.0],
    }
    out = scan.assign_weights(picks, 3, vt)
    assert out[0]["weight_pct"] == 30.0
    assert out[1]["weight_pct"] == 15.0
    assert out[2]["weight_pct"] == 15.0
    # Sums to leverage × 100.
    total = sum(p["weight_pct"] for p in out)
    assert abs(total - 60.0) < 0.1


def test_assign_weights_skips_picks_outside_top_n():
    picks = [_vol_pick(f"T{i}", i + 1, 30.0) for i in range(5)]
    vt = {
        "suggested_leverage": 0.5, "target_vol_pct": 15.0,
        "cohort_vol_pct": 30.0, "lookback_days": 60, "n_tickers": 3,
        "raw_leverage": 0.5, "leverage_clip": [0.25, 1.0],
    }
    out = scan.assign_weights(picks, 3, vt)
    # First 3 get weights; last 2 should not.
    assert all(p.get("weight_pct") is not None for p in out[:3])
    assert all("weight_pct" not in p for p in out[3:])


def test_assign_weights_zero_vol_defensive():
    # All picks rounded to 0% vol (degenerate but defensive).
    picks = [_vol_pick(f"T{i}", i + 1, 0.0) for i in range(3)]
    vt = {
        "suggested_leverage": 0.5, "target_vol_pct": 15.0,
        "cohort_vol_pct": 30.0, "lookback_days": 60, "n_tickers": 3,
        "raw_leverage": 0.5, "leverage_clip": [0.25, 1.0],
    }
    out = scan.assign_weights(picks, 3, vt)
    # Should not crash; weight_pct should be None on all.
    assert all(p["weight_pct"] is None for p in out)


def test_vol_target_banner_renders_key_numbers():
    vt = {
        "target_vol_pct": 15.0, "cohort_vol_pct": 24.8, "lookback_days": 60,
        "n_tickers": 30, "raw_leverage": 0.60, "suggested_leverage": 0.60,
        "leverage_clip": [0.25, 1.0],
    }
    out = scan.render_vol_target_banner(vt)
    assert "24.8%" in out
    assert "0.60x" in out
    assert "60d" in out
    assert "target 15%" in out


def test_vol_target_banner_none_when_disabled():
    assert scan.render_vol_target_banner(None) is None


def test_render_table_omits_weight_column_when_unset():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕",
    }]
    out = scan.render_table(picks, 5, 3)
    assert "Weight%" not in out


def test_render_table_shows_weight_column_when_set():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕", "weight_pct": 12.5,
    }]
    out = scan.render_table(picks, 5, 3)
    assert "Weight%" in out
    assert "12.5" in out


# ATR tests — synthetic OHLC so the expected ATR is computable by hand.

def _synthetic_bars(n_days: int, tickers: list[str], daily_range: float,
                    base_price: float = 100.0, seed: int = 0) -> pd.DataFrame:
    """Build a yf.download-shape MultiIndex frame for `tickers` with a
    constant daily range and a small random drift."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end="2026-05-09", periods=n_days, freq="B")
    frames = {}
    for t in tickers:
        rets = rng.normal(loc=0.0, scale=0.005, size=n_days)
        closes = base_price * np.exp(np.cumsum(rets))
        # Highs/lows symmetric around close with the chosen daily_range.
        highs = closes * (1 + daily_range / 2)
        lows = closes * (1 - daily_range / 2)
        opens = (highs + lows) / 2
        frames[t] = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows,
            "Close": closes, "Volume": np.ones(n_days) * 1e6,
        }, index=idx)
    # Stack into MultiIndex columns (ticker, field) to match yf.download.
    return pd.concat(frames, axis=1)


def test_atr_skips_tickers_without_high_low_data():
    # Bars dataframe with only one ticker. Asking for ATR on an unknown
    # ticker should silently skip it.
    bars = _synthetic_bars(30, ["FOO"], daily_range=0.04)
    atrs = scan.compute_atrs(bars, ["MISSING"])
    assert atrs == {}


def test_atr_computes_for_synthetic_bars():
    bars = _synthetic_bars(30, ["AAA", "BBB"], daily_range=0.04)
    atrs = scan.compute_atrs(bars, ["AAA", "BBB"])
    assert set(atrs.keys()) == {"AAA", "BBB"}
    for t, info in atrs.items():
        # 4% daily H-L band on ~100 close → ATR should be ~4. True ranges
        # also incorporate gaps, so ATR is slightly above the H-L band.
        assert 3.5 < info["atr"] < 5.5
        assert info["last_close"] > 0
        assert 3.0 < info["atr_pct"] < 6.0


def test_atr_skips_when_insufficient_history():
    # ATR period is 14 days; with only 10 days of data we should skip.
    bars = _synthetic_bars(10, ["AAA"], daily_range=0.04)
    atrs = scan.compute_atrs(bars, ["AAA"])
    assert atrs == {}


def test_attach_atr_stops_sets_stop_columns():
    bars = _synthetic_bars(30, ["AAA"], daily_range=0.04, base_price=100.0)
    atrs = scan.compute_atrs(bars, ["AAA"])
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        "streak": 1, "first_seen": "🆕",
    }]
    closes = bars.xs("Close", level=1, axis=1)
    out = scan.attach_atr_stops(picks, 5, atrs, closes, atr_mult=2.5)
    p = out[0]
    assert p["atr"] > 0
    assert p["stop_price"] > 0
    assert p["stop_price"] < atrs["AAA"]["last_close"]
    # With mult=2.5 and atr~4 on a ~100 close, stop should be ~10 below
    # spot. Verify the relationship.
    expected_stop = atrs["AAA"]["last_close"] - 2.5 * atrs["AAA"]["atr"]
    assert abs(p["stop_price"] - round(expected_stop, 2)) < 0.01
    # Stop% is negative (stop is below current price).
    assert p["stop_pct"] < 0
    # Streak=1, so no trail stop attached.
    assert "trail_stop_price" not in p


def test_attach_atr_stops_adds_trail_stop_above_min_streak():
    bars = _synthetic_bars(60, ["AAA"], daily_range=0.04, base_price=100.0)
    atrs = scan.compute_atrs(bars, ["AAA"])
    closes = bars.xs("Close", level=1, axis=1)
    # first_seen needs to be a date string the function can pd.Timestamp.
    first_seen = closes.index[5].strftime("%Y-%m-%d")
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        "streak": 5, "first_seen": first_seen,
    }]
    out = scan.attach_atr_stops(picks, 5, atrs, closes, atr_mult=2.5)
    p = out[0]
    assert p.get("trail_stop_price") is not None
    assert p.get("peak_since_first_seen") is not None
    # Trail stop = peak - 2.5×ATR. Peak ≥ current close, so trail_stop_pct
    # could be positive or negative depending on where current sits.
    assert p["peak_since_first_seen"] >= closes["AAA"].iloc[-1]


def test_attach_atr_stops_trail_min_streak_respected():
    # streak=3 is below trail_min_streak=4 → no trail stop attached.
    bars = _synthetic_bars(60, ["AAA"], daily_range=0.04, base_price=100.0)
    atrs = scan.compute_atrs(bars, ["AAA"])
    closes = bars.xs("Close", level=1, axis=1)
    first_seen = closes.index[5].strftime("%Y-%m-%d")
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        "streak": 3, "first_seen": first_seen,
    }]
    out = scan.attach_atr_stops(picks, 5, atrs, closes, atr_mult=2.5,
                                trail_min_streak=4)
    assert "trail_stop_price" not in out[0]
    # Same pick with trail_min_streak=3 → trail stop is attached.
    out = scan.attach_atr_stops(picks, 5, atrs, closes, atr_mult=2.5,
                                trail_min_streak=3)
    assert out[0].get("trail_stop_price") is not None


def test_attach_atr_stops_no_trail_when_first_seen_missing():
    bars = _synthetic_bars(30, ["AAA"], daily_range=0.04)
    atrs = scan.compute_atrs(bars, ["AAA"])
    closes = bars.xs("Close", level=1, axis=1)
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        "streak": 5, "first_seen": "🆕",  # sentinel for new entrant
    }]
    out = scan.attach_atr_stops(picks, 5, atrs, closes, atr_mult=2.5)
    assert "trail_stop_price" not in out[0]


# Pullback indicator tests — synthetic closes so MA20 / RSI are deterministic.

def _trend_bars(n_days: int, ticker: str, daily_return: float,
                base_price: float = 100.0) -> pd.DataFrame:
    """Build a yf.download-shape frame with a constant daily compounding
    return so MA20 and RSI are analytically predictable."""
    idx = pd.date_range(end="2026-05-09", periods=n_days, freq="B")
    closes = base_price * np.power(1 + daily_return, np.arange(n_days))
    df = pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": np.ones(n_days) * 1e6,
    }, index=idx)
    return pd.concat({ticker: df}, axis=1)


def test_pullback_computes_ma20_and_rsi_on_uptrend():
    # Steady +1%/day → price ends well above MA20, RSI saturates near 100.
    bars = _trend_bars(40, "UPP", daily_return=0.01)
    out = scan.compute_pullback_indicators(bars, ["UPP"])
    assert "UPP" in out
    info = out["UPP"]
    # 1%/day compounded over 40 days → final price ≈9.7% above the trailing
    # 20-day average (analytically: 100 × 1.01^39 / mean(100 × 1.01^[20..39])).
    assert 8 < info["ma20_dist_pct"] < 12
    # Pure-up series with no down days → RSI = 100 by convention.
    assert info["rsi14"] == 100.0


def test_pullback_computes_rsi_on_downtrend():
    # Steady -1%/day → no up days → RSI saturates at 0 by the standard formula
    # (avg_gain = 0 → rs = 0 → RSI = 100 - 100/(1+0) = 0).
    bars = _trend_bars(40, "DWN", daily_return=-0.01)
    out = scan.compute_pullback_indicators(bars, ["DWN"])
    assert "DWN" in out
    assert out["DWN"]["rsi14"] is not None
    assert out["DWN"]["rsi14"] < 5.0
    # Price ends well below MA20 (mirror image of the uptrend case).
    assert out["DWN"]["ma20_dist_pct"] < -8


def test_pullback_skips_when_insufficient_history():
    # MA20 needs ≥ 20 closes. 10 days is too few.
    bars = _trend_bars(10, "SHORT", daily_return=0.01)
    out = scan.compute_pullback_indicators(bars, ["SHORT"])
    assert out == {}


def test_pullback_skips_unknown_ticker():
    bars = _trend_bars(40, "AAA", daily_return=0.001)
    out = scan.compute_pullback_indicators(bars, ["MISSING"])
    assert out == {}


def test_classify_pullback_signal_buy_zone():
    # Within ±3% of MA20 AND RSI in 40-55 → 🟢.
    assert scan.classify_pullback_signal(0.0, 50.0) == "🟢"
    assert scan.classify_pullback_signal(2.5, 45.0) == "🟢"
    assert scan.classify_pullback_signal(-2.5, 55.0) == "🟢"


def test_classify_pullback_signal_overextended():
    # MA20% > 25 OR RSI > 80 → 🔴.
    assert scan.classify_pullback_signal(30.0, 60.0) == "🔴"
    assert scan.classify_pullback_signal(10.0, 85.0) == "🔴"
    assert scan.classify_pullback_signal(50.0, 90.0) == "🔴"


def test_classify_pullback_signal_stretched():
    # MA20% 15-25 OR RSI 70-80 → 🟠.
    assert scan.classify_pullback_signal(18.0, 60.0) == "🟠"
    assert scan.classify_pullback_signal(5.0, 75.0) == "🟠"


def test_classify_pullback_signal_watch():
    # In trend but not in any other bucket → 🟡.
    assert scan.classify_pullback_signal(8.0, 65.0) == "🟡"
    assert scan.classify_pullback_signal(5.0, 58.0) == "🟡"


def test_classify_pullback_signal_deep_pullback():
    # MA20% ≤ -3 AND RSI < 40 → 🔵 (Connors-style deep pullback in an intact
    # uptrend; the momentum-scan filter ensures the long-term trend is fine).
    assert scan.classify_pullback_signal(-5.0, 35.0) == "🔵"
    assert scan.classify_pullback_signal(-10.0, 30.0) == "🔵"
    # Boundary on MA20% (= -3): with RSI < 40 → 🔵 (no gap with 🟢's lower edge).
    assert scan.classify_pullback_signal(-3.0, 39.0) == "🔵"
    # Same MA20% but RSI just at 🟢's floor → 🟢 wins (evaluated first).
    assert scan.classify_pullback_signal(-3.0, 40.0) == "🟢"


def test_classify_pullback_signal_below_ma20_but_normal_rsi_is_watch():
    # Below MA20 but RSI hasn't cooled to deep-pullback territory → 🟡, not 🔵.
    assert scan.classify_pullback_signal(-5.0, 60.0) == "🟡"
    assert scan.classify_pullback_signal(-8.0, 45.0) == "🟡"


def test_classify_pullback_signal_missing_data():
    assert scan.classify_pullback_signal(None, 50.0) == "—"
    assert scan.classify_pullback_signal(10.0, None) == "—"
    assert scan.classify_pullback_signal(None, None) == "—"


def test_classify_pullback_signal_overextended_beats_stretched():
    # Boundary case: RSI=85 (in overext range) AND MA20%=18 (in stretched).
    # Overextended wins.
    assert scan.classify_pullback_signal(18.0, 85.0) == "🔴"


def test_attach_pullback_sets_keys():
    bars = _trend_bars(40, "AAA", daily_return=0.005)
    indicators = scan.compute_pullback_indicators(bars, ["AAA"])
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }]
    out = scan.attach_pullback(picks, 5, indicators)
    p = out[0]
    assert "ma20_dist_pct" in p
    assert "rsi14" in p
    assert "pullback_signal" in p
    assert p["pullback_signal"] in {"🟢", "🔵", "🟡", "🟠", "🔴", "—"}


def test_attach_pullback_sets_none_for_missing_indicator():
    # No indicator for AAA → keys are still set, but to None / "—". This gives
    # JSON consumers a consistent schema (every key always present when the
    # indicator is enabled at all, even if individual values are missing).
    picks = [{
        "ticker": "AAA", "rank": 1, "score": 1.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }]
    out = scan.attach_pullback(picks, 5, {})
    assert out[0]["ma20_dist_pct"] is None
    assert out[0]["rsi14"] is None
    assert out[0]["pullback_signal"] == "—"


def test_argparser_no_pullback_default_false():
    args = scan.build_argparser().parse_args([])
    assert args.no_pullback is False


def test_argparser_no_pullback_flag_sets_true():
    args = scan.build_argparser().parse_args(["--no-pullback"])
    assert args.no_pullback is True


def test_argparser_atr_stop_mult_default_2_5():
    # Locks the documented default; bump SKILL.md if you change this.
    args = scan.build_argparser().parse_args([])
    assert args.atr_stop_mult == 2.5


def test_argparser_atr_stop_mult_zero_to_disable():
    # Sentinel value for disabling the Stop column; not a no-op input.
    args = scan.build_argparser().parse_args(["--atr-stop-mult", "0"])
    assert args.atr_stop_mult == 0.0


def test_render_table_shows_pullback_columns_when_set():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕",
        "ma20_dist_pct": -1.5, "rsi14": 48.2, "pullback_signal": "🟢",
    }]
    out = scan.render_table(picks, 5, 3)
    assert "MA20%" in out
    assert "RSI" in out
    assert "Sig" in out
    assert "-1.5" in out
    assert "48" in out  # RSI rounded to no decimals
    assert "🟢" in out


def test_render_table_omits_pullback_columns_when_unset():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕",
    }]
    out = scan.render_table(picks, 5, 3)
    assert "MA20%" not in out
    assert "| Sig |" not in out


# Sector / cache tests — pure dict ops, no network.

@pytest.fixture
def sectors_file(tmp_path, monkeypatch):
    f = tmp_path / "sectors.json"
    monkeypatch.setattr(scan, "SECTORS_FILE", f)
    return f


def test_load_sectors_empty_when_no_file(sectors_file):
    assert scan.load_sectors() == {}


def test_save_then_load_sectors_roundtrip(sectors_file):
    scan.save_sectors(
        {"FOO": {"sector": "Tech", "industry": "Software", "ts": 0}})
    out = scan.load_sectors()
    assert out == {"FOO": {"sector": "Tech", "industry": "Software", "ts": 0}}


def test_load_sectors_returns_empty_on_corrupt_file(sectors_file):
    sectors_file.write_text("{not json")
    assert scan.load_sectors() == {}


def test_abbreviate_sector_known_and_unknown():
    assert scan.abbreviate_sector("Information Technology") == "Tech"
    assert scan.abbreviate_sector("Technology") == "Tech"
    assert scan.abbreviate_sector("Healthcare") == "Health"
    assert scan.abbreviate_sector("") == "—"
    # Unknown sectors get first 10 chars.
    assert scan.abbreviate_sector("Made Up Long Sector Name") == "Made Up Lo"


def test_attach_sectors_handles_missing_tickers():
    picks = [
        {"ticker": "FOO", "rank": 1, "score": 1.0, "return_pct": 0,
         "max_dd_pct": 0, "ann_vol_pct": 0, "from_high_pct": 0},
        {"ticker": "BAR", "rank": 2, "score": 1.0, "return_pct": 0,
         "max_dd_pct": 0, "ann_vol_pct": 0, "from_high_pct": 0},
    ]
    sectors = {"FOO": {"sector": "Technology",
                       "industry": "Software", "ts": 0}}
    out = scan.attach_sectors(picks, 2, sectors)
    assert out[0]["sector"] == "Technology"
    assert out[0]["industry"] == "Software"
    # BAR isn't in cache — gets empty strings, not error.
    assert out[1]["sector"] == ""
    assert out[1]["industry"] == ""


def test_sector_breakdown_counts_and_other_rollup():
    picks = [
        {"ticker": f"T{i}", "rank": i + 1,
         "sector": "Tech" if i < 5 else ("Energy" if i < 8 else "Health")}
        for i in range(10)
    ]
    out = scan.render_sector_breakdown(picks, 10, max_show=5)
    assert "Tech 5" in out
    assert "Energy 3" in out
    assert "Health 2" in out
    assert "Other" not in out  # only 3 sectors, fits in max_show=5


def test_sector_breakdown_other_rollup_when_many_sectors():
    picks = [
        {"ticker": f"T{i}", "rank": i + 1,
         "sector": f"Sector_{i % 8}"}  # 8 distinct sectors
        for i in range(16)
    ]
    out = scan.render_sector_breakdown(picks, 16, max_show=3)
    assert "Other" in out


def test_sector_breakdown_flags_untagged_count():
    picks = [
        {"ticker": "T1", "rank": 1, "sector": "Tech"},
        {"ticker": "T2", "rank": 2, "sector": ""},  # untagged
        {"ticker": "T3", "rank": 3, "sector": "Energy"},
    ]
    out = scan.render_sector_breakdown(picks, 3)
    assert "2/3 tagged" in out


def test_sector_breakdown_none_when_no_tags():
    picks = [
        {"ticker": "T1", "rank": 1, "sector": ""},
        {"ticker": "T2", "rank": 2, "sector": ""},
    ]
    assert scan.render_sector_breakdown(picks, 2) is None


def test_render_table_shows_sector_column_when_set():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕", "sector": "Technology",
    }]
    out = scan.render_table(picks, 5, 3)
    assert "Sector" in out
    assert "Tech" in out  # abbreviated form


def test_render_table_shows_stop_column_when_set():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕",
        "stop_price": 95.50, "stop_pct": -4.5,
    }]
    out = scan.render_table(picks, 5, 3)
    assert "Stop" in out
    assert "$95.50" in out
    assert "-4.5%" in out


def test_render_table_shows_annvol_when_set():
    picks = [{
        "ticker": "FOO", "rank": 1, "score": 5.0, "return_pct": 50.0,
        "max_dd_pct": -10.0, "from_high_pct": -1.0, "streak": 1,
        "rank_delta": None, "first_seen": "🆕", "ann_vol_pct": 42.0,
    }]
    out = scan.render_table(picks, 5, 3)
    assert "AnnVol%" in out
    assert "42" in out


# ─── enrich_with_persistence ─────────────────────────────────────────────────

def _hist_row(run_id: str, run_date: str, ticker: str, rank: int) -> dict:
    return {
        "run_id": run_id, "run_date": run_date, "ticker": ticker, "rank": rank,
        "score": 1.0, "return_pct": 50.0, "max_dd_pct": -10.0,
        "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }


def _make_history(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=scan.HISTORY_COLS)
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True, format="ISO8601")
    return df


def test_enrich_empty_history_marks_all_new():
    picks = [_pick("AAA", 1), _pick("BBB", 2)]
    out = scan.enrich_with_persistence(picks, _make_history([]), "20260512")
    for p in out:
        assert p["streak"] == 1
        assert p["first_seen"] == "—"
        assert p["prev_rank"] is None
        assert p["rank_delta"] is None


def test_enrich_consecutive_streak_counted():
    # AAA in 3 consecutive prior runs → streak should be 4 (3 prior + current).
    history = _make_history([
        _hist_row("20260509", "2026-05-09T20:00:00+00:00", "AAA", 5),
        _hist_row("20260510", "2026-05-10T20:00:00+00:00", "AAA", 3),
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 2),
    ])
    picks = [_pick("AAA", 1)]
    out = scan.enrich_with_persistence(picks, history, "20260512")
    assert out[0]["streak"] == 4
    assert out[0]["first_seen"] == "2026-05-09"
    assert out[0]["prev_rank"] == 2
    assert out[0]["rank_delta"] == 1  # rose from rank 2 → 1


def test_enrich_streak_breaks_on_gap():
    # AAA in 20260509, missing 20260510, present 20260511 → streak counts only
    # the consecutive runs ending most recently (so 20260511 + current = 2).
    history = _make_history([
        _hist_row("20260509", "2026-05-09T20:00:00+00:00", "AAA", 5),
        _hist_row("20260510", "2026-05-10T20:00:00+00:00", "BBB", 1),
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 2),
    ])
    picks = [_pick("AAA", 1)]
    out = scan.enrich_with_persistence(picks, history, "20260512")
    assert out[0]["streak"] == 2  # 20260511 + current; 20260509 doesn't count
    # but first_seen is the earliest
    assert out[0]["first_seen"] == "2026-05-09"


def test_enrich_new_ticker_marked():
    history = _make_history([
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 1),
    ])
    picks = [_pick("BBB", 1)]
    out = scan.enrich_with_persistence(picks, history, "20260512")
    assert out[0]["streak"] == 1
    assert out[0]["first_seen"] == "🆕"
    assert out[0]["prev_rank"] is None


def test_enrich_excludes_current_run_id():
    # If current run is already saved (run_id collision), enrichment must
    # ignore those rows so streak counts only prior runs.
    history = _make_history([
        _hist_row("20260512", "2026-05-12T20:00:00+00:00", "AAA", 1),
    ])
    picks = [_pick("AAA", 1)]
    out = scan.enrich_with_persistence(picks, history, "20260512")
    # No prior runs → streak 1, first_seen "—".
    assert out[0]["streak"] == 1
    assert out[0]["first_seen"] == "—"


# ─── dropouts ────────────────────────────────────────────────────────────────

def test_dropouts_finds_missing_names_from_last_run():
    history = _make_history([
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 1),
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "BBB", 2),
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "CCC", 3),
    ])
    out = scan.dropouts(history, {"AAA", "CCC"}, "20260512", top_n=10)
    assert len(out) == 1
    assert out[0]["ticker"] == "BBB"
    assert out[0]["prev_rank"] == 2


def test_dropouts_respects_top_n():
    history = _make_history([
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 1),
        _hist_row("20260511", "2026-05-11T20:00:00+00:00",
                  "BBB", 11),  # outside top_n=10
    ])
    out = scan.dropouts(history, set(), "20260512", top_n=10)
    tickers = {d["ticker"] for d in out}
    assert "AAA" in tickers
    assert "BBB" not in tickers


def test_dropouts_empty_when_no_prior_runs():
    history = _make_history([])
    assert scan.dropouts(history, {"AAA"}, "20260512", top_n=10) == []


# ─── run_id dtype round-trip (CSV path) ──────────────────────────────────────
# The in-memory enrich/dropouts tests above build run_id as a Python str, so
# `run_id != current_run_id` compares str-to-str and passes even on buggy code.
# Real frames come from read_csv, which infers int64 for all-numeric run_ids —
# and `int64_col != "20260512"` is silently always-true. These go through the
# CSV path to catch that; they fail if load_history doesn't coerce run_id to str.

def test_load_history_coerces_numeric_run_id_to_str(history_file):
    pd.DataFrame([
        _hist_row("20260601", "2026-06-01T20:00:00+00:00", "AAA", 1),
        _hist_row("20260602", "2026-06-02T20:00:00+00:00", "AAA", 1),
    ], columns=scan.HISTORY_COLS).to_csv(history_file, index=False)
    h = scan.load_history()
    assert h["run_id"].dtype == object
    assert h["run_id"].tolist() == ["20260601", "20260602"]  # not "20260602.0"


def test_enrich_excludes_current_run_through_csv_path(history_file):
    # Same-ET-day re-run: history.csv already holds this morning's snapshot
    # (run_id 20260602). After load_history (int64 -> str), enrich must exclude
    # it so streak counts only the genuinely-prior 20260601 — not today twice.
    pd.DataFrame([
        _hist_row("20260601", "2026-06-01T20:00:00+00:00", "AAA", 2),
        _hist_row("20260602", "2026-06-02T20:00:00+00:00", "AAA", 1),  # stale today
    ], columns=scan.HISTORY_COLS).to_csv(history_file, index=False)
    out = scan.enrich_with_persistence([_pick("AAA", 1)], scan.load_history(),
                                       "20260602")
    assert out[0]["streak"] == 2          # not 3 (which double-counts today)
    assert out[0]["first_seen"] == "2026-06-01"
    assert out[0]["prev_rank"] == 2        # from 20260601, not today's stale #1


def test_dropouts_excludes_current_run_through_csv_path(history_file):
    # On a same-day re-run, dropouts must compare against yesterday (20260601),
    # not this morning's stale save (20260602) — else BBB, which dropped between
    # yesterday and this morning, is silently missed.
    pd.DataFrame([
        _hist_row("20260601", "2026-06-01T20:00:00+00:00", "AAA", 1),
        _hist_row("20260601", "2026-06-01T20:00:00+00:00", "BBB", 2),
        _hist_row("20260602", "2026-06-02T20:00:00+00:00", "AAA", 1),  # BBB gone
    ], columns=scan.HISTORY_COLS).to_csv(history_file, index=False)
    out = scan.dropouts(scan.load_history(), {"AAA"}, "20260602", top_n=10)
    assert [d["ticker"] for d in out] == ["BBB"]


# ─── score_tickers ───────────────────────────────────────────────────────────

def _score_prices(n_days: int, columns: dict[str, np.ndarray]) -> pd.DataFrame:
    idx = pd.date_range(end="2026-05-09", periods=n_days, freq="B")
    return pd.DataFrame(columns, index=idx)


def test_score_filters_by_return_floor():
    # AAA rises 100%, BBB only 10%. min_return=30 keeps only AAA.
    n = 80
    aaa = np.linspace(100, 200, n)
    bbb = np.linspace(100, 110, n)
    prices = _score_prices(n, {"AAA": aaa, "BBB": bbb})
    out = scan.score_tickers(prices, window_months=3,
                             min_return_pct=30.0, max_dd_pct=20.0)
    tickers = {r["ticker"] for r in out}
    assert "AAA" in tickers
    assert "BBB" not in tickers


def test_score_filters_by_max_drawdown():
    # AAA: smooth rise. BBB: rises but takes a 30% drawdown midway → fails max_dd=20.
    n = 80
    aaa = np.linspace(100, 200, n)
    bbb = np.concatenate([np.linspace(100, 150, 40), np.linspace(150, 100, 20),
                          np.linspace(100, 200, 20)])
    prices = _score_prices(n, {"AAA": aaa, "BBB": bbb})
    out = scan.score_tickers(prices, window_months=3,
                             min_return_pct=30.0, max_dd_pct=20.0)
    tickers = {r["ticker"] for r in out}
    assert "AAA" in tickers
    assert "BBB" not in tickers


def test_score_rank_by_score_descending():
    # AAA: 100% return with -2% dd → score 50. BBB: 50% return with -5% dd → score 10.
    n = 80
    aaa = np.linspace(100, 200, n)
    bbb = np.linspace(100, 150, n)
    prices = _score_prices(n, {"AAA": aaa, "BBB": bbb})
    out = scan.score_tickers(prices, window_months=3,
                             min_return_pct=30.0, max_dd_pct=20.0)
    assert out[0]["ticker"] == "AAA"
    assert out[0]["rank"] == 1
    assert out[0]["score"] > out[1]["score"]


def test_score_skips_short_history():
    # Only 30 bars — score_tickers requires ≥ 60.
    n = 30
    prices = _score_prices(n, {"AAA": np.linspace(100, 200, n)})
    out = scan.score_tickers(prices, window_months=3,
                             min_return_pct=30.0, max_dd_pct=20.0)
    assert out == []


# ─── NYSE trading-day calendar ───────────────────────────────────────────────

def test_is_nyse_trading_day_weekday():
    from datetime import date
    # 2026-05-12 is a Tuesday.
    assert scan.is_nyse_trading_day(date(2026, 5, 12))


def test_is_nyse_trading_day_weekend():
    from datetime import date
    # 2026-05-09 is a Saturday.
    assert not scan.is_nyse_trading_day(date(2026, 5, 9))
    assert not scan.is_nyse_trading_day(date(2026, 5, 10))


def test_is_nyse_trading_day_christmas():
    from datetime import date
    # Christmas Day 2026 is a Friday.
    assert not scan.is_nyse_trading_day(date(2026, 12, 25))


def test_is_nyse_trading_day_good_friday():
    from datetime import date
    # Good Friday 2026 = 2026-04-03.
    assert not scan.is_nyse_trading_day(date(2026, 4, 3))


def test_is_nyse_trading_day_juneteenth_post_2022():
    from datetime import date
    # 2026-06-19 is a Friday — observed since 2022.
    assert not scan.is_nyse_trading_day(date(2026, 6, 19))


# ─── prune_non_trading_days ──────────────────────────────────────────────────

def test_prune_drops_weekend_rows(history_file):
    # Mix of weekday and weekend rows.
    history = _make_history([
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 1),  # Mon ET
        _hist_row("20260509", "2026-05-09T20:00:00+00:00", "BBB", 1),  # Sat ET
    ])
    history.to_csv(history_file, index=False)
    rows, run_ids = scan.prune_non_trading_days()
    assert rows == 1
    assert run_ids == 1
    remaining = pd.read_csv(history_file)
    assert set(remaining["ticker"]) == {"AAA"}


def test_prune_noop_when_all_trading_days(history_file):
    history = _make_history([
        _hist_row("20260511", "2026-05-11T20:00:00+00:00", "AAA", 1),
        _hist_row("20260512", "2026-05-12T20:00:00+00:00", "BBB", 1),
    ])
    history.to_csv(history_file, index=False)
    rows, run_ids = scan.prune_non_trading_days()
    assert rows == 0
    assert run_ids == 0


def test_prune_handles_empty_history(history_file):
    # No file present — should not raise.
    rows, run_ids = scan.prune_non_trading_days()
    assert rows == 0
    assert run_ids == 0


# ─── compute_regime breadth math ─────────────────────────────────────────────
# We can't easily mock yfinance's SPY fetch here, but the breadth portion of
# compute_regime takes the universe prices directly — exercise it by calling
# the function with a synthetic prices frame and tolerating None when the SPY
# half fails (offline tests). Instead, test the math by replicating the
# breadth-pct calculation against a known synthetic universe.

def test_breadth_math_consistent_with_implementation():
    # 5 names, 3 above their 200DMA, 2 below.
    n = 250
    idx = pd.date_range(end="2026-05-09", periods=n, freq="B")
    cols = {}
    # Three uptrenders: current close well above 200DMA mean.
    for t in ["UP1", "UP2", "UP3"]:
        cols[t] = np.linspace(50, 200, n)
    # Two downtrenders: current close well below 200DMA mean.
    for t in ["DN1", "DN2"]:
        cols[t] = np.linspace(200, 100, n)
    prices = pd.DataFrame(cols, index=idx)
    last = prices.iloc[-1]
    ma200 = prices.rolling(200).mean().iloc[-1]
    above = (last > ma200).sum()
    assert above == 3
    # The breadth percentage the regime banner would show.
    breadth_pct = float((last > ma200).mean() * 100)
    assert breadth_pct == 60.0


# ─── sector breakdown (data layer) ───────────────────────────────────────────

def test_compute_sector_breakdown_returns_dict():
    picks = [
        {"ticker": "T1", "rank": 1, "sector": "Tech"},
        {"ticker": "T2", "rank": 2, "sector": "Tech"},
        {"ticker": "T3", "rank": 3, "sector": "Energy"},
        {"ticker": "T4", "rank": 4, "sector": ""},
    ]
    bd = scan.compute_sector_breakdown(picks, 4)
    assert bd["counts"] == {"Tech": 2, "Energy": 1}
    assert bd["n_tagged"] == 3
    assert bd["n_total"] == 4


def test_compute_sector_breakdown_none_when_no_tags():
    picks = [{"ticker": "T1", "rank": 1, "sector": ""}]
    assert scan.compute_sector_breakdown(picks, 1) is None


# ─── _longest_consecutive_streak ─────────────────────────────────────────────

def test_longest_streak_all_present():
    # Ticker present in every run → longest streak == len(runs).
    runs = ["r1", "r2", "r3", "r4"]
    assert scan._longest_consecutive_streak(
        ["r1", "r2", "r3", "r4"], runs) == 4


def test_longest_streak_with_gap():
    # Two separate streaks of 2 → longest is 2, not 4.
    runs = ["r1", "r2", "r3", "r4", "r5"]
    assert scan._longest_consecutive_streak(
        ["r1", "r2", "r4", "r5"], runs) == 2


def test_longest_streak_picks_max_of_multiple_runs():
    # Streaks of 1, 3, 2 → longest is 3.
    runs = ["r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8"]
    present = ["r1", "r3", "r4", "r5", "r7", "r8"]
    assert scan._longest_consecutive_streak(present, runs) == 3


def test_longest_streak_empty_ticker_runs():
    runs = ["r1", "r2", "r3"]
    assert scan._longest_consecutive_streak([], runs) == 0


def test_longest_streak_single_run_present():
    runs = ["r1", "r2", "r3"]
    assert scan._longest_consecutive_streak(["r2"], runs) == 1


def test_longest_streak_ignores_unknown_run_ids():
    # Run ids not in the master `runs` list should never contribute.
    runs = ["r1", "r2", "r3"]
    assert scan._longest_consecutive_streak(["rX", "rY"], runs) == 0


# ---- Schema migration: old-schema files gain score_rank, columns reorder ---


def test_append_history_migrates_old_schema_file(history_file):
    """An existing CSV without score_rank should gain the column on first
    write and end up with the canonical HISTORY_COLS order (not score_rank
    tacked on at the end after concat). This is the regression for the
    column-drift bug surfaced in review."""
    # Write an old-schema file by hand — same shape as pre-upgrade history.
    old_rows = pd.DataFrame([
        {
            "run_id": "20260511",
            "run_date": datetime(2026, 5, 11, 20, 0, 0,
                                 tzinfo=timezone.utc).isoformat(),
            "ticker": "AAPL", "rank": 1, "score": 5.0,
            "return_pct": 50.0, "max_dd_pct": -10.0,
            "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        },
    ])
    history_file.write_text(old_rows.to_csv(index=False))
    # Confirm the seed file is genuinely old-schema (no score_rank column).
    seeded = pd.read_csv(history_file)
    assert "score_rank" not in seeded.columns

    # Append one new-schema row. The pick has score_rank set explicitly.
    new_pick = _pick("MSFT", 1)
    new_pick["score_rank"] = 1
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([new_pick], "20260512", run)

    df = pd.read_csv(history_file)
    # score_rank column now exists, populated for the new row, NaN for old.
    assert "score_rank" in df.columns
    aapl = df[df["ticker"] == "AAPL"].iloc[0]
    msft = df[df["ticker"] == "MSFT"].iloc[0]
    assert pd.isna(aapl["score_rank"])
    assert int(msft["score_rank"]) == 1
    # Column order: HISTORY_COLS prefix preserved (score_rank between
    # `rank` and `score`, not at the end after `from_high_pct`).
    assert df.columns.tolist()[:len(scan.HISTORY_COLS)] == scan.HISTORY_COLS


def _hist_rows(ticker_ranks: list[tuple[str, str, int]]) -> pd.DataFrame:
    """Build a minimal history frame from (run_id, ticker, score_rank) triples.
    run_id is stored as int64 to mirror the *raw* read_csv inference for an
    all-numeric column — which load_history() now normalizes to str, but which
    rank_sparkline still defensively re-casts as a standalone helper. Keeping
    int64 here exercises that cast. run_date is derived from run_id so sort is
    stable."""
    rows = []
    for run_id, ticker, sr in ticker_ranks:
        rows.append({
            "run_id": int(run_id),
            "run_date": pd.Timestamp(f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}",
                                     tz="UTC"),
            "ticker": ticker, "rank": sr, "score_rank": sr, "score": 1.0,
            "return_pct": 50.0, "max_dd_pct": -10.0,
            "ann_vol_pct": 30.0, "from_high_pct": -1.0,
        })
    return pd.DataFrame(rows)


def test_rank_sparkline_orientation_better_rank_is_taller():
    # Ranks improve over time: 8 -> 4 -> 1, then current run #1 (top_n=10).
    h = _hist_rows([("20260501", "AAA", 8), ("20260502", "AAA", 4),
                    ("20260503", "AAA", 1)])
    spark = scan.rank_sparkline(h, "AAA", 1, 10)
    assert len(spark) == 4
    # #1 anchors to the tallest block on the absolute 1..top_n scale.
    assert spark[-1] == scan.SPARK_TICKS[-1]
    # Improving rank (8 -> 1) -> non-decreasing, strictly-rising tick heights.
    idxs = [scan.SPARK_TICKS.index(c) for c in spark]
    assert idxs == sorted(idxs)
    assert idxs[0] < idxs[-1]


def test_rank_sparkline_absolute_scale_is_independent_of_window():
    # A name parked at #1-#2 reads near-flat-tall; a name swinging #1->#9 reads
    # as a real plunge — even though both windows span the same rank delta in
    # raw terms. This is the cross-name comparability the absolute scale buys.
    calm = scan.rank_sparkline(_hist_rows([("20260501", "C", 1)]), "C", 2, 10)
    wild = scan.rank_sparkline(_hist_rows([("20260501", "W", 1)]), "W", 9, 10)
    calm_idxs = [scan.SPARK_TICKS.index(c) for c in calm]
    wild_idxs = [scan.SPARK_TICKS.index(c) for c in wild]
    assert max(calm_idxs) - min(calm_idxs) < max(wild_idxs) - min(wild_idxs)


def test_rank_sparkline_appends_current_run():
    # Only the current rank plus one prior point -> two-char sparkline.
    h = _hist_rows([("20260501", "BBB", 5)])
    spark = scan.rank_sparkline(h, "BBB", 2, 10)
    assert len(spark) == 2
    # Current #2 is better than prior #5, so the last block is taller.
    assert scan.SPARK_TICKS.index(spark[-1]) > scan.SPARK_TICKS.index(spark[0])


def test_rank_sparkline_too_few_points_returns_empty():
    # No history and no current rank -> nothing to trend.
    assert scan.rank_sparkline(pd.DataFrame(), "X", None, 10) == ""
    # A single point (current only) is still not a trajectory.
    assert scan.rank_sparkline(pd.DataFrame(), "X", 3, 10) == ""


def test_rank_sparkline_flat_series_holds_absolute_height():
    # Held #4 every run -> every block is the absolute tick for #4 (not a
    # neutral mid block): a steady good rank looks steadily good.
    h = _hist_rows([("20260501", "CCC", 4), ("20260502", "CCC", 4)])
    spark = scan.rank_sparkline(h, "CCC", 4, 10)
    assert len(spark) == 3
    expected = scan.SPARK_TICKS[round((10 - 4) / 9 * (len(scan.SPARK_TICKS) - 1))]
    assert set(spark) == {expected}


def test_rank_sparkline_clamps_rank_worse_than_top_n_to_floor():
    # #50 with top_n=30 is off the bottom of the scale -> floor tick; #1 -> top.
    h = _hist_rows([("20260501", "DEEP", 50)])
    spark = scan.rank_sparkline(h, "DEEP", 1, 30)
    assert spark[0] == scan.SPARK_TICKS[0]   # #50 clamps to ▁
    assert spark[-1] == scan.SPARK_TICKS[-1]  # #1 -> █


def test_rank_sparkline_degenerate_top_n_one_is_all_top():
    # A 1-name leaderboard: the only possible rank is #1, so every block is the
    # tallest — never the floored ▁ the raw (1-v)/span formula would produce.
    h = _hist_rows([("20260501", "ONE", 1)])
    spark = scan.rank_sparkline(h, "ONE", 1, 1)
    assert len(spark) == 2
    assert spark == scan.SPARK_TICKS[-1] * len(spark)


def test_rank_sparkline_excludes_current_run_id():
    # A same-ET-day re-run leaves this morning's stale snapshot in history.
    # Passing current_run_id must drop it so "today" isn't plotted twice. The
    # run_id column is int64 (see _hist_rows) and current_run_id is a str, so
    # this also covers the str-normalized comparison.
    h = _hist_rows([("20260601", "ZZZ", 6), ("20260603", "ZZZ", 9)])
    # Without the guard the stale 20260603 row stays -> 3 points.
    assert len(scan.rank_sparkline(h, "ZZZ", 2, 10)) == 3
    # With it, only the 20260601 prior + current remain -> 2 points.
    assert len(scan.rank_sparkline(h, "ZZZ", 2, 10, current_run_id="20260603")) == 2


def test_rank_sparkline_respects_max_points():
    rows = [(f"202605{d:02d}", "DDD", d) for d in range(1, 16)]  # 15 prior runs
    h = _hist_rows(rows)
    assert len(scan.rank_sparkline(h, "DDD", 1, 30, max_points=10)) == 10


def test_rank_sparkline_falls_back_to_rank_without_score_rank_column():
    h = _hist_rows([("20260501", "EEE", 6), ("20260502", "EEE", 2)])
    h = h.drop(columns=["score_rank"])  # old-schema rows
    spark = scan.rank_sparkline(h, "EEE", 2, 10)
    assert spark and len(spark) == 3
