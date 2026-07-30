"""Tests for attach_volume_fields (history exit-rule research fields).

Run:
    uv run --with 'pytest' pytest scripts/test_volume_fields.py
"""
from datetime import datetime

import numpy as np
import pandas as pd

import scan


def make_bars(idx, **tickers) -> pd.DataFrame:
    """Build a MultiIndex (ticker, field) bars frame like yf.download's
    group_by='ticker' output. tickers: AAA={"Close": [...], "Volume": [...]}"""
    cols = {(t, f): vals for t, fields in tickers.items()
            for f, vals in fields.items()}
    df = pd.DataFrame(cols, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


# All tests pass an explicit after-close `now` far from the fixture dates
# unless the partial-session behavior is the thing under test.
AFTER_CLOSE = datetime(2027, 1, 15, 20, 0, tzinfo=scan.MARKET_TZ)


def run(bars, ticker="AAA", now=AFTER_CLOSE):
    picks = [{"ticker": ticker}]
    scan.attach_volume_fields(picks, bars, now=now)
    return picks[0]


def test_steady_volume_ratio_is_one():
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    bars = make_bars(idx, AAA={"Close": [100.0] * 30, "Volume": [1e6] * 30})
    p = run(bars)
    assert p["close"] == 100.0
    assert p["vol_ratio_20d"] == 1.0
    assert p["dollar_vol_20d_m"] == 100.0  # 100 × 1e6 / 1e6
    assert p["dist_days_25d"] == 0


def test_climax_day_excluded_from_own_base():
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    vols = [1e6] * 29 + [3e6]
    bars = make_bars(idx, AAA={"Close": [100.0] * 30, "Volume": vols})
    p = run(bars)
    # Base is the prior 20 sessions (1e6 each) — the 3e6 day doesn't dilute it.
    assert p["vol_ratio_20d"] == 3.0


def test_distribution_day_needs_decline_and_rising_volume():
    n = 26
    factors, vols = [1.0] * n, [1e6] * n
    factors[20], vols[20] = 0.995, 2e6   # −0.5% on rising volume → counts
    factors[15], vols[15] = 0.995, 5e5   # −0.5% on falling volume → no
    factors[10], vols[10] = 0.999, 2e6   # −0.1% (below threshold) → no
    closes = list(100.0 * np.cumprod(factors))
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    bars = make_bars(idx, AAA={"Close": closes, "Volume": vols})
    p = run(bars)
    assert p["dist_days_25d"] == 1


def test_nan_volume_gap_is_not_bridged():
    # Two small declines (−0.15%, −0.30%) straddling a NaN-volume session.
    # Intersect-and-drop would splice them into one −0.45% move compared
    # against the pre-gap volume and count a distribution day; keeping the
    # NaN disqualifies the comparison instead.
    n = 26
    factors, vols = [1.0] * n, [float(1e6 + 1000 * i) for i in range(n)]
    factors[20], vols[20] = 0.9985, float("nan")
    factors[21] = 0.9970
    closes = list(100.0 * np.cumprod(factors))
    idx = pd.bdate_range(end="2026-07-24", periods=n)
    bars = make_bars(idx, AAA={"Close": closes, "Volume": vols})
    p = run(bars)
    assert p["dist_days_25d"] == 0


def test_missing_volume_column_still_records_close():
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    bars = make_bars(idx, AAA={"Close": [100.0] * 30})
    p = run(bars)
    assert p["close"] == 100.0
    assert "vol_ratio_20d" not in p
    assert "dollar_vol_20d_m" not in p
    assert "dist_days_25d" not in p


def test_zero_volume_base_leaves_ratio_absent():
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    bars = make_bars(idx, AAA={"Close": [100.0] * 30, "Volume": [0.0] * 30})
    p = run(bars)
    assert "vol_ratio_20d" not in p


def test_lookback_boundaries():
    # Exactly 21 sessions → ratio present; 20 → absent. dist needs > 25.
    for n, present in [(21, True), (20, False)]:
        idx = pd.bdate_range(end="2026-07-24", periods=n)
        bars = make_bars(idx, AAA={"Close": [100.0] * n, "Volume": [1e6] * n})
        p = run(bars)
        assert ("vol_ratio_20d" in p) is present
        assert "dist_days_25d" not in p  # 25 completed rets need 26 sessions


def test_partial_session_trimmed_from_volume_trio():
    idx = pd.bdate_range(end="2026-07-24", periods=30)
    closes = [100.0] * 29 + [111.0]
    vols = [1e6] * 29 + [3e6]  # would read as a 3× climax if not trimmed
    bars = make_bars(idx, AAA={"Close": closes, "Volume": vols})
    mid_session = datetime(2026, 7, 24, 14, 0, tzinfo=scan.MARKET_TZ)
    p = run(bars, now=mid_session)
    assert p["close"] == 111.0        # close keeps the partial bar
    assert p["vol_ratio_20d"] == 1.0  # trio computed on completed bars only
    after_close = datetime(2026, 7, 24, 20, 0, tzinfo=scan.MARKET_TZ)
    p2 = run(bars, now=after_close)
    assert p2["vol_ratio_20d"] == 3.0


def test_entry_quality_thresholds_match_render_script():
    # render_history_html.py duplicates the tier thresholds (it stays
    # stdlib-only, so it can't import scan). This guard fails the build
    # if a re-calibration touches one side and forgets the other.
    import render_history_html as rh
    assert rh.ENTRY_VOL_SURGE_MIN == scan.ENTRY_VOL_SURGE_MIN
    assert rh.ENTRY_VOL_QUIET_MAX == scan.ENTRY_VOL_QUIET_MAX
    assert rh.ENTRY_CLEAN_DIST_MAX == scan.ENTRY_CLEAN_DIST_MAX


def test_entry_quality_tiers():
    assert scan.entry_quality(1.5, 1) == ("🟢", "surge+clean")
    assert scan.entry_quality(1.5, 2) == ("🔵", "surge")
    assert scan.entry_quality(2.4, 0) == ("🟢", "surge+clean")
    assert scan.entry_quality(0.79, 0) == ("🟠", "quiet drift-in")
    assert scan.entry_quality(0.8, 5) == ("⚪", "neutral")
    assert scan.entry_quality(1.49, 0) == ("⚪", "neutral")
    assert scan.entry_quality(None, 3) is None
    assert scan.entry_quality(1.2, None) is None


def test_single_partial_bar_does_not_crash():
    idx = pd.bdate_range(end="2026-07-24", periods=1)
    bars = make_bars(idx, AAA={"Close": [100.0], "Volume": [1e6]})
    mid_session = datetime(2026, 7, 24, 14, 0, tzinfo=scan.MARKET_TZ)
    p = run(bars, now=mid_session)
    assert p["close"] == 100.0
    assert "vol_ratio_20d" not in p
