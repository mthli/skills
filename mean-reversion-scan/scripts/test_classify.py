"""Pure-logic tests for mean-reversion scan.py — no network.

Covers the signal-breadth dial, the Sig classifier, the Reversion Score
components, Wilder RSI, the lite trend filter, trigger-frequency counting,
vol-collapse halves + the exclusion filter, the NYSE trading-day guard,
persistence/streak enrichment, and outcome resolution (WON / LOST /
EXPIRED / OPEN) with the win-rate aggregator.

Run (from this directory):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    --with 'numpy>=1.24,<3' --with pytest pytest test_classify.py
"""
from datetime import date

import numpy as np
import pandas as pd

import scan


def _series(vals):
    idx = pd.bdate_range("2026-01-05", periods=len(vals))
    return pd.Series(list(vals), index=idx, dtype=float)


# ---------------------------------------------------------------- breadth

def test_breadth_tiers_and_boundaries():
    assert scan.classify_signal_breadth(0)["tier"] == "thin"
    assert scan.classify_signal_breadth(29)["tier"] == "thin"
    assert scan.classify_signal_breadth(30)["tier"] == "normal"
    assert scan.classify_signal_breadth(60)["tier"] == "normal"
    assert scan.classify_signal_breadth(61)["tier"] == "washout"


def test_breadth_carries_cutoffs_for_json_consumers():
    d = scan.classify_signal_breadth(45)
    assert d == {"n_signals": 45, "tier": "normal",
                 "thin_max": scan.BREADTH_THIN_MAX,
                 "washout_min": scan.BREADTH_WASHOUT_MIN}


# ---------------------------------------------------------------- Sig tiers

def test_sig_tiers():
    th = 5.0
    assert scan.classify_signal(1.0, th) == "🔵"
    assert scan.classify_signal(2.5, th) == "🟢"   # exactly half-threshold is not deep
    assert scan.classify_signal(4.9, th) == "🟢"
    assert scan.classify_signal(5.0, th) == "🟡"   # at threshold → forming
    assert scan.classify_signal(9.9, th) == "🟡"
    assert scan.classify_signal(30.0, th) == "🟡"  # between 2×th and 50 → neutral watch
    assert scan.classify_signal(50.0, th) == "🟡"  # 50 exactly is not "too late"
    assert scan.classify_signal(50.1, th) == "🔴"
    assert scan.classify_signal(None, th) == "—"


# ---------------------------------------------------------------- score

def test_score_maxes_at_100():
    # RSI 0, -15% below 5DMA, +30% above 200DMA, never fired before
    assert scan.score_mr(0.0, -15.0, 30.0, 0, 5.0) == 100.0


def test_score_zero_floor():
    # RSI at threshold, no pullback, sitting on the 200DMA, noisy name
    assert scan.score_mr(5.0, 0.0, 0.0, 8, 5.0) == 0.0


def test_score_components_clip_not_overflow():
    # over-deep pullback / huge buffer clip at their caps; freq floors at 0
    assert scan.score_mr(0.0, -50.0, 90.0, 99, 5.0) == 85.0  # 40+30+15+0
    # a positive 5DMA gap (already bounced) and negative buffer contribute 0
    assert scan.score_mr(0.0, 3.0, -10.0, 0, 5.0) == 55.0    # 40+0+0+15


# ---------------------------------------------------------------- RSI

def test_rsi_all_gains_is_100_and_warmup_nan():
    rsi = scan.compute_rsi_wilder(_series(np.linspace(100, 120, 30)), 2)
    assert rsi.iloc[-1] == 100.0
    assert pd.isna(rsi.iloc[0])


def test_rsi_crash_goes_deep():
    rsi = scan.compute_rsi_wilder(
        _series(list(np.linspace(100, 110, 30)) + [99.0, 89.0, 80.0]), 2)
    assert rsi.iloc[-1] < 5.0


def test_rsi_bounded_0_100():
    rng = np.random.default_rng(7)
    s = _series(100 * np.cumprod(1 + rng.normal(0, 0.01, 120)))
    rsi = scan.compute_rsi_wilder(s, 2).dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()


# ---------------------------------------------------------------- trend gate

def test_trend_insufficient_history():
    ok, det = scan.passes_trend_filter(_series(np.linspace(100, 120, 100)))
    assert ok is False and det["reason"] == "insufficient_history"


def test_trend_uptrend_passes():
    ok, det = scan.passes_trend_filter(_series(np.linspace(100, 200, 300)))
    assert ok is True
    assert det["above_200dma"] and det["slope_positive"] and det["ma50_above_ma200"]


def test_trend_downtrend_fails():
    ok, det = scan.passes_trend_filter(_series(np.linspace(200, 100, 300)))
    assert ok is False
    assert det["above_200dma"] is False


def test_trend_flat_fails_on_strict_above():
    ok, _ = scan.passes_trend_filter(_series([100.0] * 300))
    assert ok is False  # last == ma200 is not "above"


# ---------------------------------------------------------------- frequency

def test_freq_short_series_zero():
    assert scan.count_past_triggers(_series(np.linspace(100, 110, 30)), 5.0) == 0


def test_freq_monotonic_rise_zero():
    assert scan.count_past_triggers(_series(np.linspace(100, 160, 120)), 5.0) == 0


def test_freq_stuck_oversold_counts_one_crossing():
    # rise, a 3-day slide (RSI(2) pins low — ONE crossing, not three),
    # recovery; today is a quiet up-day.
    vals = (list(np.linspace(100, 130, 90)) + [124.0, 118.0, 112.0]
            + list(np.linspace(113, 126, 27)))
    assert scan.count_past_triggers(_series(vals), 10.0) == 1


def test_freq_todays_trigger_not_counted():
    # the only crossing is the final bar — the current setup itself
    vals = list(np.linspace(100, 130, 119)) + [112.0]
    assert scan.count_past_triggers(_series(vals), 10.0) == 0


# ---------------------------------------------------------------- vol halves

def test_vol_halves_too_short():
    assert scan.compute_vol_halves(_series(np.linspace(100, 101, 10))) is None


def test_vol_halves_detect_collapse():
    rng = np.random.default_rng(3)
    noisy = 100 * np.cumprod(1 + rng.normal(0, 0.03, 30))
    flat = noisy[-1] * (1 + rng.normal(0, 0.001, 30))
    v1, v2 = scan.compute_vol_halves(_series(np.concatenate([noisy, flat])))
    assert v1 > 0 and v2 >= 0
    assert v2 < v1 * 0.2  # the price-pin signature the filter keys on


# ---------------------------------------------------------------- vol filter

def _collapse_series():
    """~47% annualized first half → sub-1% second half (price-pin)."""
    rng = np.random.default_rng(5)
    noisy = 100 * np.cumprod(1 + rng.normal(0, 0.03, 32))
    flat = noisy[-1] * (1 + rng.normal(0, 0.0005, 31))
    return _series(np.concatenate([noisy, flat]))


def _quiet_series():
    """Both halves quiet: ratio would trip, but v1 < the 5% floor."""
    rng = np.random.default_rng(6)
    a = 100 * (1 + rng.normal(0, 0.001, 32))
    b = 100 * (1 + rng.normal(0, 0.0001, 31))
    return _series(np.concatenate([a, b]))


def test_vol_filter_disabled_passthrough():
    picks = [{"ticker": "AAA", "rank": 1}]
    kept, excluded = scan.filter_vol_collapse(picks, {}, 0)
    assert kept == picks and excluded == []


def test_vol_filter_excludes_and_reranks():
    picks = [{"ticker": "PIN", "rank": 1}, {"ticker": "OK", "rank": 2}]
    closes = {"PIN": _collapse_series(),
              "OK": _series(np.linspace(100, 130, 63))}
    kept, excluded = scan.filter_vol_collapse(picks, closes, 0.2)
    assert [p["ticker"] for p in excluded] == ["PIN"]
    ex = excluded[0]
    assert ex["rank"] is None and ex["pre_filter_rank"] == 1
    assert ex["vol_ratio"] < 0.2 and ex["vol_first_pct"] > ex["vol_second_pct"]
    assert [p["ticker"] for p in kept] == ["OK"]
    assert kept[0]["rank"] == 1  # survivor re-ranked to fill the gap


def test_vol_filter_low_vol_floor_keeps_quiet_names():
    picks = [{"ticker": "QUIET", "rank": 1}]
    kept, excluded = scan.filter_vol_collapse(picks, {"QUIET": _quiet_series()}, 0.2)
    assert excluded == [] and kept[0]["ticker"] == "QUIET"


def test_vol_filter_missing_or_short_series_kept():
    picks = [{"ticker": "NODATA", "rank": 1}, {"ticker": "SHORT", "rank": 2}]
    closes = {"SHORT": _series(np.linspace(100, 110, 8))}  # halves → None
    kept, excluded = scan.filter_vol_collapse(picks, closes, 0.2)
    assert excluded == [] and len(kept) == 2


# ---------------------------------------------------------------- calendar

def test_trading_day_weekend_and_weekday():
    assert scan.is_nyse_trading_day(date(2026, 3, 11)) is True    # plain Wed
    assert scan.is_nyse_trading_day(date(2026, 8, 1)) is False    # Saturday
    assert scan.is_nyse_trading_day(date(2026, 8, 2)) is False    # Sunday


def test_trading_day_nyse_holidays():
    assert scan.is_nyse_trading_day(date(2026, 4, 3)) is False    # Good Friday
    assert scan.is_nyse_trading_day(date(2026, 6, 19)) is False   # Juneteenth
    assert scan.is_nyse_trading_day(date(2026, 7, 3)) is False    # Jul 4 observed (Sat→Fri)
    assert scan.is_nyse_trading_day(date(2026, 7, 6)) is True     # the Monday after is open
    assert scan.is_nyse_trading_day(date(2026, 12, 25)) is False  # Christmas


# ---------------------------------------------------------------- persistence

def _hist(rows):
    df = pd.DataFrame(rows, columns=["run_id", "run_date", "ticker",
                                     "rank", "score_rank"])
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True)
    return df


def test_persistence_empty_history():
    picks = [{"ticker": "AAA", "rank": 1, "score_rank": 1}]
    scan.enrich_with_persistence(picks, pd.DataFrame(), "20260729")
    assert picks[0]["streak"] == 1 and picks[0]["first_seen"] == "—"


def test_persistence_streak_gap_and_delta():
    hist = _hist([
        ("20260727", "2026-07-27", "AAA", 3, 3),
        ("20260728", "2026-07-28", "AAA", 2, 2),
        ("20260727", "2026-07-27", "BBB", 5, 5),
        # BBB absent on 0728 → its spell is broken
    ])
    picks = [{"ticker": "AAA", "rank": 1, "score_rank": 1},
             {"ticker": "BBB", "rank": 4, "score_rank": 4},
             {"ticker": "CCC", "rank": 9, "score_rank": 9}]
    scan.enrich_with_persistence(picks, hist, current_run_id="20260729")
    aaa, bbb, ccc = picks
    assert aaa["streak"] == 3                       # 2 consecutive priors + today
    assert aaa["first_seen"] == "2026-07-27"
    assert aaa["rank_delta"] == 1                   # prev score_rank 2 → now 1
    assert bbb["streak"] == 1                       # gap resets ("stuck" needs consecutive)
    assert bbb["prev_rank"] == 5
    assert ccc["first_seen"] == "🆕" and ccc["streak"] == 1


# ---------------------------------------------------------------- outcomes

def _sig_row(ticker, days_ago, entry=100.0, target=103.0, stop=95.0):
    d = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_ago)).normalize()
    return {"run_id": d.strftime("%Y%m%d"), "run_date": d, "ticker": ticker,
            "target_price": target, "stop_price": stop, "last_close": entry,
            "signal": "🟢"}


def _bars(frames, start):
    parts = {}
    for t, (highs, lows, closes) in frames.items():
        idx = pd.bdate_range(start, periods=len(highs))
        parts[(t, "High")] = pd.Series(highs, index=idx, dtype=float)
        parts[(t, "Low")] = pd.Series(lows, index=idx, dtype=float)
        parts[(t, "Close")] = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(parts)


def _post_start(row):
    return row["run_date"].tz_convert(None) + pd.Timedelta(days=1)


def test_resolve_won():
    row = _sig_row("AAA", days_ago=10)
    bars = _bars({"AAA": ([101, 104, 105, 105, 105],
                          [99, 100, 101, 101, 101],
                          [100, 103.5, 104, 104, 104])}, _post_start(row))
    outs = scan.resolve_outcomes(pd.DataFrame([row]), bars, 5)
    assert len(outs) == 1
    o = outs[0]
    assert o["outcome"] == "WON" and o["days_to_resolve"] == 2
    assert o["result_pct"] == 3.0  # resolves at the target, not the high


def test_resolve_lost_before_target():
    row = _sig_row("AAA", days_ago=10)
    bars = _bars({"AAA": ([101, 102, 104, 104, 104],
                          [99, 94, 100, 100, 100],   # stop 95 hit day 2
                          [100, 96, 103, 103, 103])}, _post_start(row))
    o = scan.resolve_outcomes(pd.DataFrame([row]), bars, 5)[0]
    assert o["outcome"] == "LOST" and o["days_to_resolve"] == 2
    assert o["result_pct"] == -5.0


def test_resolve_same_day_both_is_won():
    row = _sig_row("AAA", days_ago=10)
    bars = _bars({"AAA": ([104, 105, 105, 105, 105],
                          [94, 101, 101, 101, 101],   # day 1 touches both
                          [100, 104, 104, 104, 104])}, _post_start(row))
    o = scan.resolve_outcomes(pd.DataFrame([row]), bars, 5)[0]
    assert o["outcome"] == "WON" and o["days_to_resolve"] == 1


def test_resolve_expired_with_drift():
    row = _sig_row("AAA", days_ago=10)
    bars = _bars({"AAA": ([101, 101, 101, 101, 101],
                          [99, 99, 99, 99, 99],
                          [100, 100, 100, 100, 99.0])}, _post_start(row))
    o = scan.resolve_outcomes(pd.DataFrame([row]), bars, 5)[0]
    assert o["outcome"] == "EXPIRED" and o["days_to_resolve"] == 5
    assert o["result_pct"] == -1.0


def test_resolve_open_is_excluded():
    row = _sig_row("AAA", days_ago=4)
    bars = _bars({"AAA": ([101, 101], [99, 99], [100, 100])}, _post_start(row))
    assert scan.resolve_outcomes(pd.DataFrame([row]), bars, 5) == []


def test_resolve_missing_target_column():
    hist = pd.DataFrame([{"run_date": pd.Timestamp.now(tz="UTC"),
                          "ticker": "AAA"}])
    assert scan.resolve_outcomes(hist, pd.DataFrame(), 5) == []


def test_win_rate_stats():
    assert scan.compute_win_rate_stats([]) is None
    outs = [{"outcome": "WON", "days_to_resolve": 1},
            {"outcome": "WON", "days_to_resolve": 3},
            {"outcome": "LOST", "days_to_resolve": 2},
            {"outcome": "EXPIRED", "days_to_resolve": 5}]
    s = scan.compute_win_rate_stats(outs)
    assert s["win_rate_pct"] == 66.7        # EXPIRED excluded from the rate
    assert s["n_resolved"] == 4 and s["n_expired"] == 1
    assert s["avg_days_to_target"] == 2.0


def test_win_rate_all_expired_has_no_rate():
    s = scan.compute_win_rate_stats([{"outcome": "EXPIRED",
                                      "days_to_resolve": 5}])
    assert s["win_rate_pct"] is None and s["n_expired"] == 1
