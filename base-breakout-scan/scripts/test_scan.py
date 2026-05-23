"""
Tests for base-breakout-scan core logic. Run with:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    --with 'pytest' pytest scripts/

These tests use synthetic price/volume series — no network, no Yahoo
calls — so they're fast and deterministic. They cover the parts of the
algorithm that are easy to reason about analytically: trend-template
gates, base detection on hand-crafted patterns, RS rating, smoothness,
the scoring function, and signal classification.

They do NOT cover: yfinance fetch, regime banner, history I/O — those
have integration-style risk that's better validated via live runs.
"""
import scan
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ─── Helpers ─────────────────────────────────────────────────────────────
def daily_index(n_days: int, end_date: str = "2026-05-12") -> pd.DatetimeIndex:
    """Generate `n_days` business-day index ending at `end_date`."""
    end = pd.Timestamp(end_date)
    return pd.bdate_range(end=end, periods=n_days)


def make_close(values: list[float], end_date: str = "2026-05-12") -> pd.Series:
    """Build a Close series with a business-day index ending at end_date."""
    return pd.Series(values, index=daily_index(len(values), end_date), dtype=float)


def make_volume(n_days: int, base_vol: float = 1_000_000,
                end_date: str = "2026-05-12") -> pd.Series:
    """Constant volume — simplest case. Tests that need dry-up patterns
    construct their own series."""
    return pd.Series([base_vol] * n_days,
                     index=daily_index(n_days, end_date), dtype=float)


# ─── Trend Template ──────────────────────────────────────────────────────
class TestPassesTrendTemplate:
    def test_perfect_uptrend_passes(self):
        # 252 days climbing smoothly from 100 to 200 — every MA aligned, RS=90.
        prices = np.linspace(100, 200, 252)
        close = make_close(list(prices))
        passes, details = scan.passes_trend_template(close, rs_rating=90.0)
        assert passes, f"Expected pass; failure reason: {details.get('reason')}"
        assert details["dist_from_52w_high_pct"] >= -1  # at the high
        assert details["dist_from_52w_low_pct"] >= 90   # ~100% above low

    def test_low_rs_rating_fails(self):
        prices = np.linspace(100, 200, 252)
        close = make_close(list(prices))
        passes, details = scan.passes_trend_template(close, rs_rating=50.0)
        assert not passes
        assert details["reason"] == "fail_rs_rating"

    def test_close_below_50dma_fails(self):
        # Climb to 200, then a sharp 15% drop in the last 10 days.
        prices = list(np.linspace(100, 200, 242)) + [170] * 10
        close = make_close(prices)
        passes, details = scan.passes_trend_template(close, rs_rating=90.0)
        assert not passes
        # Could fail at close-vs-MA50 OR close-vs-MA150 — both are legit.
        assert details["reason"] in {
            "fail_close_le_ma50", "fail_close_gt_ma150_gt_ma200",
            "fail_ma50_gt_ma150_gt_ma200",
        }

    def test_downtrend_fails(self):
        # Monotone decline — 200DMA slope negative, close below all MAs.
        prices = np.linspace(200, 100, 252)
        close = make_close(list(prices))
        passes, _ = scan.passes_trend_template(close, rs_rating=90.0)
        assert not passes

    def test_insufficient_history_fails(self):
        prices = np.linspace(100, 200, 100)
        close = make_close(list(prices))
        passes, details = scan.passes_trend_template(close, rs_rating=90.0)
        assert not passes
        assert details["reason"] == "insufficient_history"


# ─── Base detection ──────────────────────────────────────────────────────
class TestDetectBase:
    def test_tight_flat_base_after_uptrend_detected(self):
        # 200 days of uptrend, then 60 days perfectly flat at $200.
        uptrend = list(np.linspace(100, 200, 200))
        flat = [200.0] * 60
        close = make_close(uptrend + flat)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol, min_base_weeks=6,
                                max_base_weeks=40, max_base_width_pct=25,
                                max_to_52w_high_pct=15)
        assert base is not None
        # Trailing window can pick up the last uptrend day at 199.x, giving
        # width up to ~1%. The test intent is "essentially flat" — allow 2%.
        assert base["width_pct"] < 2.0
        assert base["to_pivot_pct"] == pytest.approx(0.0, abs=0.5)
        assert base["pivot_price"] == pytest.approx(200.0, abs=0.5)
        assert base["base_weeks"] >= 6
        # Smoothness should be very high — every bar is at the mean.
        assert base["smoothness_pct"] >= 95

    def test_no_base_when_far_from_52w_high(self):
        # Stock made 52w high at 200, now at 150 (-25% off — too far).
        uptrend = list(np.linspace(100, 200, 200))
        decline = list(np.linspace(200, 150, 60))
        close = make_close(uptrend + decline)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol, max_to_52w_high_pct=15)
        assert base is None

    def test_v_shape_low_smoothness(self):
        # Two-phase: 30 days falling from 200 to 180, then 30 days back to 200.
        # Width is 10% (fits envelope) but smoothness is low (V shape).
        uptrend = list(np.linspace(100, 200, 200))
        v_left = list(np.linspace(200, 180, 30))
        v_right = list(np.linspace(180, 200, 30))
        close = make_close(uptrend + v_left + v_right)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol, min_base_weeks=6,
                                max_base_weeks=40, max_base_width_pct=25,
                                max_to_52w_high_pct=15)
        # Should still detect a base by width but smoothness should be modest.
        # V-shape distributes bars across the full range, so % within ±2% of
        # the mean is roughly the fraction near the middle — empirically ~37%
        # for a clean V; allow up to 45% to cover algorithm-level rounding.
        assert base is not None
        assert base["smoothness_pct"] < 45

    def test_breakout_today_mode_detected(self):
        # 60 days flat at 100, then today closes at 105 (breakout).
        # Expect mode 3: the prior 60-day window's high is 100, today
        # broke above it; the base period is the flat region pre-breakout,
        # not the breakout days themselves.
        uptrend = list(np.linspace(80, 100, 200))
        flat = [100.0] * 58
        breakout = [102.0, 103.0, 105.5]  # last 3 days breaking out
        close = make_close(uptrend + flat + breakout)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol)
        assert base is not None
        # The breakout detector should land on mode 3 (today is the fresh
        # cross above prior range), with the pivot equal to the prior 100
        # high — not 105 (today's close).
        assert base["anchor_mode"] == 3, (
            f"Expected mode=3 (breakout-today), got mode={base['anchor_mode']}"
        )
        assert base["is_breakout_day"] is True
        assert base["pivot_price"] == pytest.approx(100.0, abs=1.0)

    def test_wide_base_rejected(self):
        # 60 days swinging between 150 and 200 — 25% width, at the ceiling.
        # With max_width=20%, should be rejected.
        uptrend = list(np.linspace(100, 200, 200))
        swings = list(np.linspace(200, 150, 30)) + \
            list(np.linspace(150, 200, 30))
        close = make_close(uptrend + swings)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol, max_base_width_pct=20)
        assert base is None or base["width_pct"] <= 20

    def test_quality_score_prefers_tighter(self):
        # Build a series with two candidate windows:
        #   - 60-day wide-ish window covering both the wide and tight
        #     halves: width ~5% (max 200, min 190).
        #   - 30-day window covering just the tight half: width ~1%.
        # The algorithm should pick the 30-day window because:
        #   60-day quality = 60 / max(5, 1) = 12
        #   30-day quality = 30 / max(1, 1) = 30  (tighter wins)
        # We assert BOTH the width (tight = ~1%) and the base length
        # (~30 trading days = 6 weeks), confirming the right window was
        # selected, not just any valid one.
        uptrend = list(np.linspace(100, 200, 200))
        wide = [190.0 if i % 2 == 0 else 200.0 for i in range(30)]
        tight = [198.0 if i % 2 == 0 else 200.0 for i in range(30)]
        close = make_close(uptrend + wide + tight)
        vol = make_volume(len(close))
        base = scan.detect_base(close, vol, min_base_weeks=6)
        assert base is not None
        # Tight window should win — width near 1%, length near 30 days.
        # (Stride=5 in detect_base means days lands on 30 exactly.)
        assert base["width_pct"] < 3, (
            f"Expected tight window (width ~1%); got width {base['width_pct']}%"
        )
        assert base["days_in_base"] <= 35, (
            f"Expected ~30-day window; got {base['days_in_base']} days "
            f"(algorithm picked the wider/longer window by mistake)"
        )


# ─── Recent breakout ─────────────────────────────────────────────────────
class TestDetectRecentBreakout:
    def test_breakout_5_days_ago_detected(self):
        # 60 days flat at 100, then 5 days ago broke to 105 on 2x vol, holding.
        uptrend = list(np.linspace(80, 100, 200))
        flat = [100.0] * 55
        followthrough = [105.0, 104.0, 106.0,
                         105.5, 105.8]  # last 5 days, all > 100
        close = make_close(uptrend + flat + followthrough)
        # Vol: constant 1M for the whole series, plus a 2M spike on the day
        # the breakout occurred (5 days ago = index len-5).
        vol_arr = [1_000_000] * len(close)
        breakout_idx = len(close) - 5
        vol_arr[breakout_idx] = 2_000_000
        vol = pd.Series(vol_arr, index=close.index, dtype=float)
        rb = scan.detect_recent_breakout(close, vol, lookback_days=10)
        assert rb is not None
        assert rb["days_since_breakout"] >= 1  # should fire within window
        # follow_through is relative to prior_pivot (~100), current ~$105.8
        assert rb["follow_through_pct"] > 0

    def test_breakout_without_volume_confirm_rejected(self):
        # Same as above but volume stays at 1M (no confirmation).
        uptrend = list(np.linspace(80, 100, 200))
        flat = [100.0] * 55
        followthrough = [105.0, 104.0, 106.0, 105.5, 105.8]
        close = make_close(uptrend + flat + followthrough)
        vol = make_volume(len(close))  # constant 1M, no spike
        rb = scan.detect_recent_breakout(close, vol, lookback_days=10)
        assert rb is None

    def test_no_breakout_in_window(self):
        # Pure flat — never broke out.
        flat = [100.0] * 260
        close = make_close(flat)
        vol = make_volume(len(close))
        rb = scan.detect_recent_breakout(close, vol, lookback_days=10)
        assert rb is None


# ─── RS Rating ───────────────────────────────────────────────────────────
class TestComputeRsRatings:
    def test_ranks_by_weighted_return(self):
        # Two stocks: A goes 100 → 200 (100% return). B goes 100 → 110 (10% return).
        # A should rank above B.
        closes = {
            "A": make_close(list(np.linspace(100, 200, 260))),
            "B": make_close(list(np.linspace(100, 110, 260))),
        }
        ratings = scan.compute_rs_ratings(closes)
        assert "A" in ratings and "B" in ratings
        assert ratings["A"] > ratings["B"]

    def test_skips_short_history(self):
        # Need ≥ 252 + 1 days. A series with 100 days should be skipped.
        closes = {
            "SHORT": make_close(list(np.linspace(100, 200, 100))),
            "LONG": make_close(list(np.linspace(100, 200, 260))),
        }
        ratings = scan.compute_rs_ratings(closes)
        assert "SHORT" not in ratings
        assert "LONG" in ratings


# ─── Scoring ─────────────────────────────────────────────────────────────
class TestComputeBaseScore:
    def _base(self, **overrides):
        """Build a minimal base dict with all required keys filled with
        'middle of the road' values, then apply overrides."""
        d = {
            "width_pct": 15.0,
            "vol_dryup_ratio": 0.85,
            "to_pivot_pct": -5.0,
            "smoothness_pct": 60.0,
        }
        d.update(overrides)
        return d

    def test_perfect_setup_near_100(self):
        # Best possible: 5% width, BB pctile 0, vol 0.55, RS slope +2.5,
        # at pivot, 70% smoothness (the calibrated "best"), three-wk-tight
        # bonus. Sum: 25 + 20 + 15 + 20 + 15 + 10 + 5 = 110 → capped at 100.
        # (Previous test used smoothness=90 — also max-points, but 70 is
        # the actual calibrated boundary so the test now reflects intent.)
        base = self._base(width_pct=5.0, vol_dryup_ratio=0.55,
                          to_pivot_pct=-2.0, smoothness_pct=70.0)
        score = scan.compute_base_score(base, bb_pctile=0.0, rs_slope=2.5,
                                        three_wk_tight=True)
        assert score == 100.0

    def test_worst_setup_zero(self):
        # Smoothness=20 is the new "worst" (was 50 in the first calibration —
        # adjusted after observing that real bases rarely hit 50% smoothness).
        base = self._base(width_pct=25.0, vol_dryup_ratio=1.10,
                          to_pivot_pct=-20.0, smoothness_pct=20.0)
        score = scan.compute_base_score(base, bb_pctile=50.0, rs_slope=-1.0,
                                        three_wk_tight=False)
        # Most components at 0, pivot proximity bell at -20 with ideal=-2,
        # falloff=4 is also near 0.
        assert score < 5

    def test_score_caps_at_100(self):
        # Even with all-perfect + bonus, score should not exceed 100.
        base = self._base(width_pct=0.0, vol_dryup_ratio=0.0,
                          to_pivot_pct=-2.0, smoothness_pct=100.0)
        score = scan.compute_base_score(base, bb_pctile=0.0, rs_slope=5.0,
                                        three_wk_tight=True)
        assert score <= 100.0

    def test_rs_slope_no_longer_saturates(self):
        # Two bases identical except RS slope: +1.0 vs +3.0. Old logic
        # gave both the same 20pt cap; new logic should differentiate.
        b = self._base()
        score_modest = scan.compute_base_score(b, bb_pctile=10.0,
                                               rs_slope=1.0,
                                               three_wk_tight=False)
        score_strong = scan.compute_base_score(b, bb_pctile=10.0,
                                               rs_slope=3.0,
                                               three_wk_tight=False)
        assert score_strong > score_modest


# ─── Signal classification ───────────────────────────────────────────────
class TestClassifySignal:
    def _base(self, **overrides):
        d = {
            "to_pivot_pct": -5.0,
            "today_vol_ratio": 1.0,
            "vol_dryup_ratio": 0.85,
            "is_breakout_day": False,
        }
        d.update(overrides)
        return d

    def test_breakout_day_with_volume_is_rocket(self):
        base = self._base(is_breakout_day=True, today_vol_ratio=2.0,
                          to_pivot_pct=0.0)
        sig = scan.classify_signal(base, bb_pctile=10.0)
        assert sig == "🚀"

    def test_breakout_without_volume_is_not_rocket(self):
        base = self._base(is_breakout_day=True, today_vol_ratio=1.0,
                          to_pivot_pct=0.0)
        sig = scan.classify_signal(base, bb_pctile=10.0)
        # Without volume confirm, falls through to 🔥/⏳/📊 instead.
        assert sig != "🚀"

    def test_imminent_when_close_to_pivot_and_squeezed(self):
        base = self._base(to_pivot_pct=-1.5, vol_dryup_ratio=0.7)
        sig = scan.classify_signal(base, bb_pctile=10.0)
        assert sig == "🔥"

    def test_coiled_when_farther_and_squeezed(self):
        base = self._base(to_pivot_pct=-7.0, vol_dryup_ratio=0.7)
        sig = scan.classify_signal(base, bb_pctile=20.0)
        assert sig == "⏳"

    def test_setup_when_not_squeezed_or_far(self):
        base = self._base(to_pivot_pct=-12.0, vol_dryup_ratio=1.0)
        sig = scan.classify_signal(base, bb_pctile=50.0)
        assert sig == "📊"


# ─── Three weeks tight ───────────────────────────────────────────────────
class TestThreeWeeksTight:
    def test_tight_weekly_closes_pass(self):
        # 30 days; last 3 Fridays are within 1% of each other.
        prices = [100.0] * 30  # all 100 → perfectly tight
        close = make_close(prices)
        assert scan.three_weeks_tight(close) is True

    def test_loose_weekly_closes_fail(self):
        # Spread weekly closes by 5% — should fail the 1.5% threshold.
        prices = ([100.0] * 5 + [102.0] * 5 + [105.0] * 5 + [108.0] * 5
                  + [110.0] * 5 + [113.0] * 5)
        close = make_close(prices)
        assert scan.three_weeks_tight(close) is False


# ─── Same-issuer dedup ──────────────────────────────────────────────────
class TestDedupSameIssuer:
    def test_keeps_higher_scoring_of_pair(self):
        results = [
            {"ticker": "PBR-A", "base_score": 60},
            {"ticker": "PBR", "base_score": 65},
            {"ticker": "AAPL", "base_score": 70},
        ]
        kept, dropped = scan._dedup_same_issuer(results)
        kept_tickers = [r["ticker"] for r in kept]
        dropped_tickers = [r["ticker"] for r in dropped]
        assert "PBR" in kept_tickers
        assert "PBR-A" not in kept_tickers
        assert "AAPL" in kept_tickers
        assert "PBR-A" in dropped_tickers

    def test_no_op_when_no_pairs(self):
        results = [
            {"ticker": "AAPL", "base_score": 70},
            {"ticker": "MSFT", "base_score": 65},
        ]
        kept, dropped = scan._dedup_same_issuer(results)
        assert len(kept) == 2
        assert dropped == []

    def test_tie_keeps_first_listed(self):
        # On a tie, the function should keep the "main" ticker (first in
        # the SAME_ISSUER_PAIRS tuple). For PBR/PBR-A, that's PBR.
        results = [
            {"ticker": "PBR-A", "base_score": 60},
            {"ticker": "PBR", "base_score": 60},
        ]
        kept, _ = scan._dedup_same_issuer(results)
        kept_tickers = [r["ticker"] for r in kept]
        assert "PBR" in kept_tickers
        assert "PBR-A" not in kept_tickers

    def test_dedup_sibling_field_attached(self):
        # The kept pick should carry a `dedup_sibling` field with the
        # dropped twin's ticker + score so the renderer can show
        # "PBR (also PBR-A, score 60)" as a parenthetical hint.
        results = [
            {"ticker": "PBR-A", "base_score": 60},
            {"ticker": "PBR", "base_score": 65},
        ]
        kept, _ = scan._dedup_same_issuer(results)
        pbr = next(r for r in kept if r["ticker"] == "PBR")
        assert pbr.get("dedup_sibling") == {
            "ticker": "PBR-A", "base_score": 60}


# ─── BB squeeze percentile ───────────────────────────────────────────────
class TestComputeBbPctile:
    def test_returns_none_for_short_history(self):
        close = make_close(list(np.linspace(100, 200, 50)))
        assert scan.compute_bb_pctile(close) is None

    def test_tight_present_low_pctile(self):
        # 200 days normal vol, then 20 days extremely flat → current BB
        # width should be at the very low end of the 6mo distribution.
        normal = list(np.linspace(100, 200, 200) +
                      np.random.RandomState(0).randn(200) * 2)
        flat = [200.0] * 20
        close = make_close(normal + flat)
        pct = scan.compute_bb_pctile(close)
        assert pct is not None
        assert pct < 20  # current BB width near the low end of the distribution


# ─── RS-vs-SPY proxy ─────────────────────────────────────────────────────
class TestComputeRsProxy:
    def _series_with_total_return(self, total_return: float, n_days: int = 260
                                  ) -> pd.Series:
        """Build a smoothly-trending series with the given total return
        over n_days. Used to construct ticker / SPY pairs with controlled
        excess return for proxy testing."""
        end_price = 100.0 * (1 + total_return)
        return make_close(list(np.linspace(100.0, end_price, n_days)))

    def test_returns_high_rating_for_strong_outperformer(self):
        # Ticker up 100%, SPY up 20% over 12mo. With smooth-linear series,
        # the 3/6/9/12-month returns are roughly 1/4, 1/2, 3/4, full of the
        # total — so weighted excess is well above +30% → clipped → rating
        # close to 99. (A 50%/10% pair gives weighted excess only ~+18%,
        # which the proxy correctly maps to ~80, not 99.)
        ticker = self._series_with_total_return(1.00)
        spy = self._series_with_total_return(0.20)
        rating = scan._compute_rs_proxy(ticker, spy)
        assert rating is not None
        assert rating > 90

    def test_returns_low_rating_for_strong_underperformer(self):
        # Ticker down 30%, SPY up 30% — weighted excess about -60% →
        # clipped at -30% → rating near 1.
        ticker = self._series_with_total_return(-0.30)
        spy = self._series_with_total_return(0.30)
        rating = scan._compute_rs_proxy(ticker, spy)
        assert rating is not None
        assert rating < 10

    def test_returns_mid_rating_when_matching_spy(self):
        # Ticker and SPY both up 20% — excess = 0 → rating near 50.
        ticker = self._series_with_total_return(0.20)
        spy = self._series_with_total_return(0.20)
        rating = scan._compute_rs_proxy(ticker, spy)
        assert rating is not None
        # The proxy maps excess=0 to ~50 (midpoint of the 1-99 scale).
        # Smooth-trending synthetic series give exactly 0 excess at every
        # window, so we expect almost exactly 50.
        assert 45 <= rating <= 55

    def test_returns_none_for_insufficient_history(self):
        # Only 100 days — not enough for the 252-day window.
        ticker = self._series_with_total_return(0.20, n_days=100)
        spy = self._series_with_total_return(0.10, n_days=100)
        rating = scan._compute_rs_proxy(ticker, spy)
        assert rating is None

    def test_rating_clamps_at_extremes(self):
        # +300% return vs flat SPY — should clip cleanly at 99, not crash.
        ticker = self._series_with_total_return(3.00)
        spy = self._series_with_total_return(0.0)
        rating = scan._compute_rs_proxy(ticker, spy)
        assert rating is not None
        assert 95 <= rating <= 99
