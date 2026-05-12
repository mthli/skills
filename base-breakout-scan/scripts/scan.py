"""
Base-breakout scan: find US equities in valid bases (consolidations after
prior advance) and classify proximity to breakout — the "what's about to
run" sibling of momentum-scan's "what's already running".

Pipeline:
  1. Minervini Trend Template prefilter (7 criteria) — strict uptrend test
  2. Base detection — anchor on 252d max (ex-last-3d), require 30-200d span
     and ≤ max-base-width
  3. Quality metrics — BB squeeze percentile vs 6mo, vol dry-up ratio, RS
     slope vs SPY during base, three-weeks-tight check
  4. Composite Base Score (0-100) + Signal classification
     (🚀 breakout / 🔥 imminent / ⏳ coiled / 📊 setup)

Persistence: one snapshot per US market day to state/history.csv. Re-running
the same ET date overwrites that day's rows. Streak counts scan-days a
ticker has held the base setup; high streak = base is durable.

Self-contained — uses yfinance directly, no cross-skill dependencies.

Usage:
  python scan.py                              # standard run
  python scan.py --min-base-weeks 8           # require longer bases
  python scan.py --min-rs-rating 80           # only top-quintile RS
  python scan.py --show-history               # dump history summary
"""
import argparse
import json
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
    USThanksgivingDay, nearest_workday,
)
from yfinance import EquityQuery

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
UNIVERSE_FILE = STATE_DIR / "universe.txt"
HISTORY_FILE = STATE_DIR / "history.csv"
SECTORS_FILE = STATE_DIR / "sectors.json"
UNIVERSE_TTL_DAYS = 7
SECTORS_TTL_DAYS = 30  # sectors change slowly; long TTL keeps repeat runs fast
MARKET_TZ = ZoneInfo("America/New_York")  # one snapshot per US market day

HISTORY_COLS = [
    "run_id", "run_date", "ticker", "rank", "base_score",
    "base_weeks", "width_pct", "bb_pctile", "vol_dryup_ratio",
    "rs_slope_pct_per_wk", "to_pivot_pct", "pivot_price", "signal",
]


# ─── NYSE calendar ───────────────────────────────────────────────────────
class _NYSECalendar(AbstractHolidayCalendar):
    """NYSE-observed holidays. Pandas's USFederalHolidayCalendar isn't quite
    right (NYSE observes Good Friday but not Columbus / Veterans Day). Rare
    one-off closures (e.g. presidential funerals) aren't included — saving
    a stale snapshot on those ≤1/year days is preferred to maintenance."""
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday,
                start_date="2022-06-19"),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def is_nyse_trading_day(d: date) -> bool:
    """True iff `d` is a weekday and not an NYSE-observed holiday."""
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty


# ─── Universe ────────────────────────────────────────────────────────────
YF_SCREEN_PAGE_SIZE = 250  # yf.screen hard cap per call; raises ValueError above


def refresh_universe(min_market_cap: float, min_volume: int, count: int) -> list[str]:
    """Pull current US large caps via yfinance's screener. Caches to file.

    Paginates with `offset` when `count` > 250 (yf.screen's per-call cap).
    Up to 4 pages = 1000 names is supported by Yahoo before results dry up;
    in practice the per-page cap is the hard ceiling and the screener
    returns fewer than `size` for the deepest page when results are
    exhausted, which we accept as the natural stop."""
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_market_cap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    tickers: list[str] = []
    seen: set[str] = set()
    offset = 0
    while len(tickers) < count:
        page_size = min(YF_SCREEN_PAGE_SIZE, count - len(tickers))
        try:
            raw = yf.screen(query, sortField="intradaymarketcap",
                            sortAsc=False, size=page_size, offset=offset)
        except TypeError:
            # Older yfinance versions don't accept `offset` — fall back to
            # single-page mode and accept the 250-cap behavior silently.
            if offset == 0:
                raw = yf.screen(query, sortField="intradaymarketcap",
                                sortAsc=False, size=page_size)
            else:
                break
        quotes = raw.get("quotes") or []
        page_tickers = [q.get("symbol") for q in quotes if q.get("symbol")]
        # Dedupe across pages (Yahoo can return overlaps near pagination
        # boundaries when the underlying ranking has ties).
        new = [t for t in page_tickers if t not in seen]
        if not new:
            # Yahoo returned no new names — end of available data, stop.
            break
        tickers.extend(new)
        seen.update(new)
        offset += page_size
    if not tickers:
        raise RuntimeError(
            "Yahoo screener returned no results — possibly rate-limited or "
            "API drift. Try again in a few minutes."
        )
    UNIVERSE_FILE.write_text("\n".join(tickers))
    return tickers


def load_universe(min_market_cap: float, min_volume: int, count: int,
                  refresh_mode) -> list[str]:
    """refresh_mode: None = TTL-based (default), True = force, False = use cache as-is."""
    if not UNIVERSE_FILE.exists():
        return refresh_universe(min_market_cap, min_volume, count)
    if refresh_mode is True:
        return refresh_universe(min_market_cap, min_volume, count)
    cached = [t for t in UNIVERSE_FILE.read_text().splitlines() if t.strip()]
    if refresh_mode is False:
        return cached
    age_days = (datetime.now().timestamp() - UNIVERSE_FILE.stat().st_mtime) / 86400
    if age_days > UNIVERSE_TTL_DAYS:
        print(f"Universe cache stale ({age_days:.1f}d), refreshing...", file=sys.stderr)
        return refresh_universe(min_market_cap, min_volume, count)
    return cached


# ─── Bar fetch ───────────────────────────────────────────────────────────
# Need ≥ 252 trading days for 200DMA + 52w high + RS lookback. 14mo = ~290 td
# with comfortable buffer for missing-bar edge cases on newly-listed names.
DEFAULT_HISTORY_MONTHS = 14
MIN_BARS_REQUIRED = 220  # ~10.5 months; below this we can't compute 200DMA


def fetch_bars(tickers: list[str], history_months: int = DEFAULT_HISTORY_MONTHS) -> pd.DataFrame:
    """Pull the full OHLCV MultiIndex frame from yfinance. All downstream
    indicators (ATR, BB, vol, RS) reuse this single download — a second
    round-trip for the same 250 tickers would cost another ~10 sec."""
    period = f"{history_months}mo"
    print(f"Fetching {len(tickers)} tickers, period={period}...", file=sys.stderr)
    return yf.download(
        tickers, period=period, interval="1d", auto_adjust=True,
        progress=False, threads=True, group_by="ticker",
    )


def extract_field(bars: pd.DataFrame, tickers: list[str],
                  field: str, min_len: int = MIN_BARS_REQUIRED) -> dict[str, pd.Series]:
    """Pull a single OHLCV column per ticker, returning a dict {ticker: series}
    keyed by symbol. Tickers without enough history for 200DMA-based metrics
    are dropped silently — they'd fail the trend template anyway."""
    out = {}
    for t in tickers:
        try:
            if (t, field) not in bars.columns:
                continue
            s = bars[(t, field)].dropna()
            if len(s) >= min_len:
                out[t] = s
        except Exception:
            continue
    return out


# ─── RS Rating (universe-relative) ───────────────────────────────────────
# O'Neil-style weighted multi-period return, percentile-ranked across the
# universe to 1-99. Minervini's Trend Template requires RS ≥ 70 (top 30%).
RS_PERIODS_TRADING_DAYS = [63, 126, 189, 252]  # 3, 6, 9, 12 months
RS_WEIGHTS = [0.40, 0.20, 0.20, 0.20]  # most-recent quarter weighted heaviest


def compute_rs_ratings(closes: dict[str, pd.Series]) -> dict[str, float]:
    """For each ticker, compute weighted multi-period return then percentile-
    rank across the universe. Returns ticker → 1-99 RS rating.

    Tickers without enough history for the longest period (252 td) are
    excluded — they couldn't pass the trend template anyway."""
    raw_scores: dict[str, float] = {}
    max_period = max(RS_PERIODS_TRADING_DAYS)
    for t, s in closes.items():
        if len(s) < max_period + 1:
            continue
        weighted = 0.0
        valid = True
        for period, weight in zip(RS_PERIODS_TRADING_DAYS, RS_WEIGHTS):
            try:
                ret = float(s.iloc[-1] / s.iloc[-(period + 1)] - 1)
                weighted += ret * weight
            except (IndexError, ZeroDivisionError):
                valid = False
                break
        if valid and not np.isnan(weighted):
            raw_scores[t] = weighted

    if not raw_scores:
        return {}

    # Convert to 1-99 percentile rank
    sorted_items = sorted(raw_scores.items(), key=lambda kv: kv[1])
    n = len(sorted_items)
    ratings = {}
    for i, (t, _) in enumerate(sorted_items):
        # +1 so the lowest gets 1, not 0; scale to (1, 99) inclusive
        ratings[t] = (i + 1) / n * 99
    return ratings


# RS-vs-SPY proxy used by --ticker mode. Maps the ticker's weighted excess
# return vs SPY to an approximate 1-99 rating. Calibrated against observed
# universe RS Ratings across recent runs:
#   - Top large-cap names in a bull tape have weighted excess vs SPY of
#     +30% or more (e.g. INTC, AMD, MU at +100% return when SPY did +20%).
#   - Bottom decile sits around -30% weighted excess.
#   - Median large cap is roughly flat vs SPY (excess ~0%).
# Mapping:
#   excess = +30% → rating ≈ 99
#   excess = +10% → rating ≈ 65   (close to the 70 TT gate)
#   excess =   0% → rating ≈ 50
#   excess = -10% → rating ≈ 35
#   excess = -30% → rating ≈ 1
# Clips at ±30%.
#
# Earlier calibration used ±15% which dramatically overstated mid-tier
# names — HSBC (true RS=73 against the universe) showed as ~99 in the
# proxy. The wider band matches the actual distribution of weighted
# excess returns across the large-cap universe in trending markets.
RS_PROXY_RETURN_AT_99 = 0.30
RS_PROXY_RETURN_AT_1 = -0.30


def _compute_rs_proxy(close: pd.Series, spy_close: pd.Series) -> float | None:
    """Compute approximate 1-99 RS rating using only the ticker + SPY.

    Used by `--ticker` mode (no universe fetch needed). Matches the full
    `compute_rs_ratings` output within ~10-15 percentile points in
    practice — close enough to be the gate for the trend template, not
    close enough to publish as a ranked figure."""
    common = close.index.intersection(spy_close.index)
    if len(common) < max(RS_PERIODS_TRADING_DAYS) + 1:
        return None
    c = close.loc[common]
    s = spy_close.loc[common]
    weighted_excess = 0.0
    for period, weight in zip(RS_PERIODS_TRADING_DAYS, RS_WEIGHTS):
        try:
            ret_t = float(c.iloc[-1] / c.iloc[-(period + 1)] - 1)
            ret_s = float(s.iloc[-1] / s.iloc[-(period + 1)] - 1)
            weighted_excess += (ret_t - ret_s) * weight
        except (IndexError, ZeroDivisionError):
            return None
    # Linear map from excess return to 1-99 rating; clip outside the
    # calibration range.
    excess_clipped = max(RS_PROXY_RETURN_AT_1,
                          min(RS_PROXY_RETURN_AT_99, weighted_excess))
    span = RS_PROXY_RETURN_AT_99 - RS_PROXY_RETURN_AT_1
    rating = 1 + (excess_clipped - RS_PROXY_RETURN_AT_1) / span * 98
    return float(rating)


# ─── Trend Template (Minervini) ──────────────────────────────────────────
# All seven criteria must pass. These are the canonical Minervini gates;
# tickers failing them aren't valid Stage 2 candidates regardless of base
# quality. Tuned for liquid US large-caps — small-caps may need looser
# distance-from-52w-low (5b/6b) since they're more volatile structurally.

TT_MA50_PERIOD = 50
TT_MA150_PERIOD = 150
TT_MA200_PERIOD = 200
TT_MA200_SLOPE_LOOKBACK_DAYS = 21  # ~1 trading month
TT_MIN_DIST_FROM_52W_LOW_PCT = 30.0  # close ≥ 1.30 × 52w_low
TT_MIN_RS_RATING = 70.0


def passes_trend_template(close: pd.Series, rs_rating: float | None,
                          max_dist_from_52w_high_pct: float = 25.0
                          ) -> tuple[bool, dict]:
    """Check the 7 Minervini criteria against a single ticker's close series.

    Returns (passes, details). `details` contains the computed values so
    failing tickers can be diagnosed (mostly for testing/debugging — they
    don't appear in the output)."""
    if len(close) < TT_MA200_PERIOD + TT_MA200_SLOPE_LOOKBACK_DAYS:
        return False, {"reason": "insufficient_history"}

    ma50 = close.rolling(TT_MA50_PERIOD).mean()
    ma150 = close.rolling(TT_MA150_PERIOD).mean()
    ma200 = close.rolling(TT_MA200_PERIOD).mean()
    last = float(close.iloc[-1])
    ma50_last = float(ma50.iloc[-1])
    ma150_last = float(ma150.iloc[-1])
    ma200_last = float(ma200.iloc[-1])
    ma200_lookback = float(ma200.iloc[-(TT_MA200_SLOPE_LOOKBACK_DAYS + 1)])
    ma200_slope_pct = (ma200_last / ma200_lookback - 1) * 100

    high_52w = float(close.tail(252).max())
    low_52w = float(close.tail(252).min())
    dist_from_52w_low_pct = (last / low_52w - 1) * 100
    dist_from_52w_high_pct = (last / high_52w - 1) * 100  # negative or 0

    details = {
        "ma50": ma50_last, "ma150": ma150_last, "ma200": ma200_last,
        "ma200_slope_pct": ma200_slope_pct,
        "high_52w": high_52w, "low_52w": low_52w,
        "dist_from_52w_low_pct": dist_from_52w_low_pct,
        "dist_from_52w_high_pct": dist_from_52w_high_pct,
        "rs_rating": rs_rating,
    }

    # The 7 criteria. Each `if`/`return` evaluates the next gate; first fail
    # short-circuits and reports which one. The order matches Minervini's
    # canonical numbering so the failure reasons read cleanly.
    if not (last > ma150_last > ma200_last):
        details["reason"] = "fail_close_gt_ma150_gt_ma200"
        return False, details
    if ma200_slope_pct <= 0:
        details["reason"] = "fail_ma200_not_rising"
        return False, details
    if not (ma50_last > ma150_last > ma200_last):
        details["reason"] = "fail_ma50_gt_ma150_gt_ma200"
        return False, details
    if last <= ma50_last:
        details["reason"] = "fail_close_le_ma50"
        return False, details
    if dist_from_52w_low_pct < TT_MIN_DIST_FROM_52W_LOW_PCT:
        details["reason"] = "fail_too_close_to_52w_low"
        return False, details
    if dist_from_52w_high_pct < -max_dist_from_52w_high_pct:
        details["reason"] = "fail_too_far_from_52w_high"
        return False, details
    if rs_rating is None or rs_rating < TT_MIN_RS_RATING:
        details["reason"] = "fail_rs_rating"
        return False, details

    details["reason"] = "pass"
    return True, details


# ─── Base detection ──────────────────────────────────────────────────────
# Operational definition of "the current base":
#   - Anchor = max of close excluding last 3 trading days (so today-as-
#     breakout is detectable: anchor is "what we had to beat")
#   - Base period = anchor_date → today
#   - Valid if span ∈ [min_base_days, max_base_days] and width% ≤ max_width
#
# This is necessarily a simplification — true VCP / cup-and-handle pattern
# recognition takes pages of rules. But for a screener (not a chart
# annotator), the anchor-and-span heuristic captures the same population
# without false-positiving on every wiggle.

DEFAULT_MIN_BASE_WEEKS = 6
DEFAULT_MAX_BASE_WEEKS = 40
DEFAULT_MAX_BASE_WIDTH_PCT = 25.0
DEFAULT_MAX_TO_52W_HIGH_PCT = 15.0
BASE_BREAKOUT_DETECT_BUFFER_DAYS = 3  # exclude last N days when finding anchor
LOOKBACK_FOR_BASE_SEARCH = 252  # 1 year window for finding the anchor

# Smoothness metric: % of bars within ±SMOOTH_BAND_PCT of base mean. Higher =
# cleaner horizontal consolidation; lower = V-shape / jagged action that
# happens to fit the width envelope. Default band is ±2% which captures
# typical-day noise in liquid large-caps without flagging healthy small swings.
SMOOTH_BAND_PCT = 2.0


def detect_base(close: pd.Series, volume: pd.Series,
                min_base_weeks: float = DEFAULT_MIN_BASE_WEEKS,
                max_base_weeks: float = DEFAULT_MAX_BASE_WEEKS,
                max_base_width_pct: float = DEFAULT_MAX_BASE_WIDTH_PCT,
                max_to_52w_high_pct: float = DEFAULT_MAX_TO_52W_HIGH_PCT,
                smoothness_band_pct: float = SMOOTH_BAND_PCT
                ) -> dict | None:
    """Identify the current base by enumerating all valid trailing windows
    (across two anchor modes) and picking the one with the best **quality
    score** = days_in_base / max(width_pct, 1). This means longer + tighter
    wins, so a 30-day base at 5% width beats a 40-day base at 15% width.

    Anchor modes:
      - mode=0: window ends today (pre-breakout watch)
      - mode=3: window ends 3 trading days ago (today is the breakout)

    Returns None if no valid base exists by the given parameters.

    Why quality-scored over 'longest valid window': the longest-valid
    approach pegged width% at the max_base_width ceiling for nearly every
    pick — algorithmically guaranteed since 'longest under width X' tends
    to width≈X. Scoring by days/width naturally surfaces tighter bases at
    cost of length, which matches how chartists actually grade setups."""
    if len(close) < LOOKBACK_FOR_BASE_SEARCH:
        return None

    last = float(close.iloc[-1])
    recent = close.tail(LOOKBACK_FOR_BASE_SEARCH).dropna()
    if len(recent) < int(min_base_weeks * 5):
        return None
    high_52w = float(recent.max())

    # Distance to 52w high — names too far below the high are in correction
    # territory, not a base near resistance.
    to_52w_high_pct = (last / high_52w - 1) * 100
    if to_52w_high_pct < -max_to_52w_high_pct:
        return None

    min_days = int(min_base_weeks * 5)
    max_days = int(max_base_weeks * 5)

    best: dict | None = None
    best_quality = -1.0

    # Walk both anchor modes: 0 = base extends to today, 3 = base ended 3
    # days ago (gives today as the fresh breakout above the prior range).
    for end_offset in (0, BASE_BREAKOUT_DETECT_BUFFER_DAYS):
        end_idx = len(recent) - end_offset
        if end_idx <= min_days:
            continue
        upper = min(max_days, end_idx - 1)
        if upper < min_days:
            continue
        # Stride through window lengths in 1-week jumps. Step of 1 would
        # check every possible length (30, 31, 32, ...) but the quality
        # function `days / max(width, 1)` changes smoothly with `days`, so
        # the optimal length is within ±2 days of one of our samples and
        # the gain from finer enumeration is < 1% on the picked score.
        # Step of 5 (≈ one trading week) is 5× faster — a meaningful
        # speedup when this loop runs across all trend-template passers.
        for days in range(min_days, upper + 1, 5):
            start_idx = end_idx - days
            if start_idx < 0:
                continue
            window = recent.iloc[start_idx:end_idx]
            if len(window) < 2:
                continue
            win_high = float(window.max())
            win_low = float(window.min())
            if win_high <= 0:
                continue
            width = (win_high - win_low) / win_high * 100
            if width > max_base_width_pct:
                continue

            pivot = win_high
            to_pivot = (last / pivot - 1) * 100

            if end_offset == 0:
                # Mode 0: base contains today.
                if to_pivot > 0.5:
                    # Today set a new high — handled by mode=3.
                    continue
                if to_pivot < -max_base_width_pct:
                    # Price at the wrong end of the range, not near pivot.
                    continue
                is_breakout_day = to_pivot >= -0.2
            else:
                # Mode 3: base ended 3 days ago; today must clear window high.
                if last < win_high * 1.002:
                    continue
                is_breakout_day = True

            # Quality score: longer base with tighter width wins.
            # Clamp width floor at 1.0% so a freakishly tight 6-week base
            # doesn't dominate via a near-zero denominator.
            quality = days / max(width, 1.0)
            if quality <= best_quality:
                continue

            anchor_idx = recent.index[start_idx]
            best_quality = quality
            best = {
                "anchor_date": anchor_idx.strftime("%Y-%m-%d"),
                "days_in_base": days,
                "base_weeks": round(days / 5.0, 1),
                "base_high": win_high,
                "base_low": win_low,
                "width_pct": round(width, 1),
                "pivot_price": round(pivot, 2),
                "to_pivot_pct": round(to_pivot, 2),
                "to_52w_high_pct": round(to_52w_high_pct, 2),
                "is_breakout_day": is_breakout_day,
                "anchor_mode": end_offset,
                "quality_score": round(quality, 2),
            }

    if best is None:
        return None

    # Smoothness: fraction of base bars within ±SMOOTH_BAND_PCT of the base
    # mean. A V-shape that just happens to fit the width envelope will score
    # low here (most bars are at the extremes, not near the mean), while a
    # genuine horizontal consolidation scores high. The score adds a small
    # bonus to compute_base_score so smooth bases out-rank V-shapes of the
    # same width.
    base_start_pos = recent.index.get_loc(pd.Timestamp(best["anchor_date"]))
    end_pos = len(recent) - best["anchor_mode"]
    base_bars = recent.iloc[base_start_pos:end_pos]
    if len(base_bars) > 0 and float(base_bars.mean()) > 0:
        base_mean = float(base_bars.mean())
        band_lo = base_mean * (1 - smoothness_band_pct / 100)
        band_hi = base_mean * (1 + smoothness_band_pct / 100)
        within = ((base_bars >= band_lo) & (base_bars <= band_hi)).sum()
        smoothness_pct = float(within / len(base_bars) * 100)
    else:
        smoothness_pct = None

    # Volume metrics (mode-independent).
    if len(volume) >= 80:
        vol_recent = float(volume.tail(20).mean())
        vol_trailing = float(volume.iloc[-80:-20].mean())
        vol_dryup_ratio = (vol_recent / vol_trailing) if vol_trailing > 0 else None
    else:
        vol_dryup_ratio = None

    if len(volume) >= 21:
        today_vol = float(volume.iloc[-1])
        avg_20d_vol = float(volume.iloc[-21:-1].mean())
        today_vol_ratio = (today_vol / avg_20d_vol) if avg_20d_vol > 0 else None
    else:
        today_vol_ratio = None

    best["vol_dryup_ratio"] = round(vol_dryup_ratio, 2) if vol_dryup_ratio else None
    best["today_vol_ratio"] = round(today_vol_ratio, 2) if today_vol_ratio else None
    best["smoothness_pct"] = (round(smoothness_pct, 1)
                              if smoothness_pct is not None else None)
    best["last_close"] = last
    return best


# Recent breakouts: names that triggered the pivot in the last N trading days
# *but no longer pass the main base filter* (because the breakout altered the
# price structure). Surfaced in a separate output section so users can see
# "follow-through stage" candidates without polluting the watchlist.
RECENT_BREAKOUT_LOOKBACK_DAYS = 10  # window in which to detect a fresh breakout
RECENT_BREAKOUT_MIN_BASE_BARS = 50  # require ≥10wk of pre-breakout structure
# (Bumped from 30 = 6wk. 6 weeks of "pre-breakout range" wasn't enough to
# qualify as a real base — a single momentum thrust through a 30-day high
# isn't the same as breaking a multi-month consolidation.)
RECENT_BREAKOUT_VOL_RATIO = 1.7  # day-of breakout volume multiple of trailing avg
# (Bumped from 1.5. The lower bar let in too many false positives — a 1.5×
# day on a name with naturally cyclical volume isn't a strong signal.)


def detect_recent_breakout(close: pd.Series, volume: pd.Series,
                           lookback_days: int = RECENT_BREAKOUT_LOOKBACK_DAYS
                           ) -> dict | None:
    """Scan the last `lookback_days` for a single day where:
      - close > prior N-bar high (proper breakout)
      - day volume ≥ RECENT_BREAKOUT_VOL_RATIO × trailing 20d avg
    Returns the most-recent qualifying day if any, else None.

    The follow-through fields (current_close, follow_through_pct, days_since)
    let the caller report 'broke out N days ago, since up/down M%' — the
    relevant info for deciding whether to chase or wait for a pullback."""
    if len(close) < RECENT_BREAKOUT_MIN_BASE_BARS + lookback_days + 1:
        return None
    if volume is None or len(volume) < RECENT_BREAKOUT_MIN_BASE_BARS + lookback_days + 1:
        return None
    last_close = float(close.iloc[-1])
    # Walk back from most-recent → oldest to return the most recent breakout
    # if multiple exist in the window.
    for days_ago in range(1, lookback_days + 1):
        day_idx = len(close) - days_ago
        if day_idx < RECENT_BREAKOUT_MIN_BASE_BARS:
            continue
        prior_window = close.iloc[:day_idx]
        prior_high = float(prior_window.max())
        day_close = float(close.iloc[day_idx])
        if day_close < prior_high * 1.002:
            continue
        # Volume confirmation against the 20 bars *before* the breakout.
        vol_lookback_start = max(0, day_idx - 20)
        avg_vol_prior = float(volume.iloc[vol_lookback_start:day_idx].mean())
        if avg_vol_prior <= 0:
            continue
        day_vol = float(volume.iloc[day_idx])
        vol_ratio = day_vol / avg_vol_prior
        if vol_ratio < RECENT_BREAKOUT_VOL_RATIO:
            continue
        return {
            "days_since_breakout": days_ago,
            "breakout_date": close.index[day_idx].strftime("%Y-%m-%d"),
            "breakout_price": round(day_close, 2),
            "prior_pivot": round(prior_high, 2),
            "breakout_vol_ratio": round(vol_ratio, 2),
            "current_close": round(last_close, 2),
            # Follow-through is measured relative to the *pivot*, not the
            # breakout-day close, so "current/pivot - 1" answers the
            # actionable question: 'is price still above the breakout
            # level the user was watching?'. Positive = working breakout,
            # negative = failed (price fell back below pivot).
            "follow_through_pct": round((last_close / prior_high - 1) * 100, 2),
        }
    return None


# ─── Bollinger Band squeeze percentile ───────────────────────────────────
BB_PERIOD = 20
BB_PCTILE_LOOKBACK = 126  # ~6 months — captures one regime, not multi-year


def compute_bb_pctile(close: pd.Series, period: int = BB_PERIOD,
                      lookback: int = BB_PCTILE_LOOKBACK) -> float | None:
    """Where does the current Bollinger Band width sit within the last
    `lookback` trading days, as a percentile? Lower = tighter squeeze.

    BB width = (upper - lower) / middle = 4σ / mean. We compare today's
    width to the empirical distribution over the lookback window: 0 means
    today is the tightest in 6 months, 100 means today is the widest."""
    if len(close) < period + lookback:
        return None
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    # Normalized width = (upper - lower) / middle. Using 4σ here so the
    # number reads as a fraction of price (2σ would also work — only the
    # relative ranking matters).
    width = 4 * std / sma
    widths = width.dropna()
    if len(widths) < lookback:
        return None
    recent = widths.tail(lookback)
    current = float(recent.iloc[-1])
    pctile = float((recent < current).sum() / len(recent) * 100)
    return round(pctile, 1)


# ─── Three Weeks Tight ───────────────────────────────────────────────────
THREE_WEEKS_TIGHT_THRESHOLD_PCT = 1.5  # Minervini's canonical threshold


def three_weeks_tight(close: pd.Series,
                      threshold_pct: float = THREE_WEEKS_TIGHT_THRESHOLD_PCT
                      ) -> bool:
    """Minervini's '3 weeks tight' signal: the last 3 weekly closes (Fri-
    to-Fri) lie within `threshold_pct`% of each other. Indicates supply
    has been fully absorbed — institutional selling pressure has stopped."""
    if len(close) < 15:  # need ≥ 3 weeks of daily bars
        return False
    weekly = close.resample("W-FRI").last().dropna().tail(3)
    if len(weekly) < 3:
        return False
    mean = float(weekly.mean())
    if mean <= 0:
        return False
    spread_pct = (float(weekly.max()) - float(weekly.min())) / mean * 100
    return spread_pct <= threshold_pct


# ─── RS slope during base ────────────────────────────────────────────────
def compute_rs_slope_pct_per_wk(close: pd.Series, spy_close: pd.Series,
                                 anchor_idx) -> float | None:
    """OLS slope of (close / spy_close) over the base period, expressed as
    % per trading week. Positive = stock outperforming SPY while ranging
    (the single most important pre-breakout signal). Negative = stock
    losing relative strength — base may be the leading edge of a downturn."""
    try:
        base_close = close.loc[anchor_idx:].dropna()
        base_spy = spy_close.loc[anchor_idx:].dropna()
    except KeyError:
        return None
    common = base_close.index.intersection(base_spy.index)
    if len(common) < 10:
        return None
    base_close = base_close.loc[common]
    base_spy = base_spy.loc[common]
    rs = base_close / base_spy
    if float(rs.iloc[0]) <= 0:
        return None
    # Rebase to 1.0 at base start so slope reads as fractional change per day,
    # then scale to % per week (5 trading days).
    rs_normalized = rs.values / float(rs.iloc[0])
    x = np.arange(len(rs_normalized))
    slope_per_day, _ = np.polyfit(x, rs_normalized, 1)
    return float(slope_per_day * 5 * 100)


# ─── Composite Base Score ────────────────────────────────────────────────
# 0-100 composite, calibrated so a textbook-perfect setup (very tight width,
# max BB squeeze, deep vol dry-up, strong positive RS slope, smooth base, at
# the pivot, plus the 3-week-tight bonus) lands in the high-80s/low-90s.
# A 50-point pick is a solid setup; 70+ is high-conviction.
SCORE_WEIGHT_TIGHTNESS = 25       # width% — lower is better
SCORE_WEIGHT_BB_SQUEEZE = 20      # bb_pctile — lower is better
SCORE_WEIGHT_VOL_DRYUP = 15       # vol_dryup_ratio — lower is better
SCORE_WEIGHT_RS_SLOPE = 20        # rs_slope — positive is better
SCORE_WEIGHT_PIVOT_PROXIMITY = 15 # close to pivot is better
SCORE_WEIGHT_SMOOTHNESS = 10      # smoothness_pct — higher is better
SCORE_BONUS_3WK_TIGHT = 5
# Theoretical max: 25+20+15+20+15+10+5 = 110, so the score is capped at 100.
# Realistic top picks land 75-95; the calibration is meant to differentiate
# clearly between "good" and "great" without saturating in the middle of the
# range.


def _bell_score(value: float, ideal: float, falloff: float, max_pts: float
                ) -> float:
    """Gaussian-style score: max_pts at value==ideal, declining as |value -
    ideal| grows. `falloff` controls width (≈ stddev of the bell)."""
    if value is None:
        return 0.0
    z = (value - ideal) / falloff
    return float(max_pts * np.exp(-0.5 * z * z))


def _linear_score(value: float, best: float, worst: float,
                  max_pts: float) -> float:
    """Linear score mapping (worst → 0, best → max_pts), clamped to range."""
    if value is None:
        return 0.0
    if best == worst:
        return max_pts / 2
    if best < worst:
        # Lower is better
        if value <= best:
            return float(max_pts)
        if value >= worst:
            return 0.0
        return float(max_pts * (worst - value) / (worst - best))
    else:
        # Higher is better
        if value >= best:
            return float(max_pts)
        if value <= worst:
            return 0.0
        return float(max_pts * (value - worst) / (best - worst))


def compute_base_score(base: dict, bb_pctile: float | None,
                       rs_slope: float | None,
                       three_wk_tight: bool) -> float:
    """Aggregate 0-100 base-quality score.

    Calibration notes (rationale for each component's bounds):
      - Tightness: 5% width = full marks, 25% = 0. Below 5% is freakishly
        tight; the linear extrapolation is fine since few stocks get there.
      - BB squeeze: 0th pctile = full marks, ≥ 40th = 0. We don't reward
        non-squeezes (a base with median BB width isn't bad — just neutral).
      - Vol dry-up: 0.55 ratio = full marks, ≥ 1.10 = 0. Below 0.55 is
        rare and often indicates a data gap, so capping there avoids
        rewarding artifacts.
      - RS slope: +2.5%/wk = full marks, ≤ -0.5%/wk = 0. The previous +1.0
        ceiling saturated for genuinely strong names (ADI at +3.0 looked
        the same as +1.0). +2.5 is the real top of the distribution.
      - Pivot proximity: bell curve peaked at -2% (close enough to act on
        without same-day chase). Falloff 4 gives -8% / +4% partial credit.
      - Smoothness: 70% of bars within ±2% of mean = full marks, ≤ 20% = 0.
        The first calibration (90%/50%) was too strict — real bases have
        natural directional drift that pushes most bars outside the ±2%
        band even when they're cleanly horizontal in 'chart eye' terms.
        Empirically the smoothness distribution across passing bases sits
        in 20-60%, so this scaling rewards the genuinely tightest while
        still giving partial credit to typical-shape bases.
    """
    pts = 0.0

    pts += _linear_score(base["width_pct"], best=5.0, worst=25.0,
                          max_pts=SCORE_WEIGHT_TIGHTNESS)

    pts += _linear_score(bb_pctile, best=0.0, worst=40.0,
                          max_pts=SCORE_WEIGHT_BB_SQUEEZE)

    pts += _linear_score(base.get("vol_dryup_ratio"),
                          best=0.55, worst=1.10,
                          max_pts=SCORE_WEIGHT_VOL_DRYUP)

    pts += _linear_score(rs_slope, best=2.5, worst=-0.5,
                          max_pts=SCORE_WEIGHT_RS_SLOPE)

    pts += _bell_score(base["to_pivot_pct"], ideal=-2.0, falloff=4.0,
                       max_pts=SCORE_WEIGHT_PIVOT_PROXIMITY)

    pts += _linear_score(base.get("smoothness_pct"),
                          best=70.0, worst=20.0,
                          max_pts=SCORE_WEIGHT_SMOOTHNESS)

    if three_wk_tight:
        pts += SCORE_BONUS_3WK_TIGHT

    # Theoretical max 110 — cap at 100 so the 0-100 scale stays intact.
    return round(min(pts, 100.0), 1)


# ─── Signal classification ───────────────────────────────────────────────
# 🚀 BREAKOUT: today's close at-or-above pivot AND volume confirmation
# 🔥 IMMINENT: very close to pivot, with squeeze + dry-up
# ⏳ COILED:   meaningful distance to pivot, but squeeze is on
# 📊 SETUP:    valid base, not yet near pivot or coiled
BREAKOUT_VOL_CONFIRM_RATIO = 1.5  # today's volume > 1.5× 20d avg
IMMINENT_TO_PIVOT_PCT = -3.0  # within 3% below pivot
IMMINENT_BB_PCTILE = 25
IMMINENT_VOL_DRYUP_RATIO = 0.95
COILED_TO_PIVOT_PCT = -10.0
COILED_BB_PCTILE = 30


def classify_signal(base: dict, bb_pctile: float | None) -> str:
    """Map base + squeeze metrics to 🚀/🔥/⏳/📊.

    Evaluation order: 🚀 → 🔥 → ⏳ → 📊 (first match wins). The 🚀
    branch requires both price ≥ pivot AND volume confirmation; weakly
    higher closes without volume are still 🔥 (the breakout day is
    *suspect* without volume). The 🔥/⏳/📊 ordering reflects increasing
    distance from action: 🔥 is "today/tomorrow", ⏳ is "this week",
    📊 is "watch for the geometry to tighten"."""
    to_pivot = base.get("to_pivot_pct")
    vol_ratio_today = base.get("today_vol_ratio")
    vol_dryup = base.get("vol_dryup_ratio")
    is_breakout = base.get("is_breakout_day", False)

    if (is_breakout and vol_ratio_today is not None
            and vol_ratio_today >= BREAKOUT_VOL_CONFIRM_RATIO):
        return "🚀"

    if (to_pivot is not None and to_pivot >= IMMINENT_TO_PIVOT_PCT
            and to_pivot <= 0
            and bb_pctile is not None and bb_pctile < IMMINENT_BB_PCTILE
            and vol_dryup is not None and vol_dryup < IMMINENT_VOL_DRYUP_RATIO):
        return "🔥"

    if (to_pivot is not None and to_pivot >= COILED_TO_PIVOT_PCT
            and to_pivot < IMMINENT_TO_PIVOT_PCT
            and bb_pctile is not None and bb_pctile < COILED_BB_PCTILE):
        return "⏳"

    return "📊"


# ─── Regime (SPY 200DMA + breadth) ───────────────────────────────────────
# RISK-ON = SPY > 200DMA AND 200DMA itself rising. Same definition as
# momentum-scan — base breakouts also work best in healthy markets.
MA200_SLOPE_LOOKBACK_DAYS = 20
MA200_SLOPE_RISK_ON_THRESHOLD_PCT = -0.05


def compute_regime(spy_close: pd.Series,
                   universe_closes: dict[str, pd.Series]) -> dict | None:
    if len(spy_close) < 200 + MA200_SLOPE_LOOKBACK_DAYS:
        return None
    ma200_series = spy_close.rolling(200).mean()
    spy_last = float(spy_close.iloc[-1])
    spy_ma50 = float(spy_close.rolling(50).mean().iloc[-1])
    spy_ma200 = float(ma200_series.iloc[-1])
    spy_ma200_prev = float(ma200_series.iloc[-(MA200_SLOPE_LOOKBACK_DAYS + 1)])
    spy_ma200_slope_pct = (spy_ma200 / spy_ma200_prev - 1) * 100

    # Breadth: % of universe above own 200DMA
    breadth_pct_200 = None
    above, total = 0, 0
    for t, s in universe_closes.items():
        if len(s) >= 200:
            ma200 = float(s.rolling(200).mean().iloc[-1])
            if not np.isnan(ma200) and ma200 > 0:
                total += 1
                if float(s.iloc[-1]) > ma200:
                    above += 1
    if total > 0:
        breadth_pct_200 = above / total * 100

    risk_on = (spy_last > spy_ma200) and (
        spy_ma200_slope_pct > MA200_SLOPE_RISK_ON_THRESHOLD_PCT
    )
    return {
        "spy_last": spy_last,
        "spy_ma50": spy_ma50,
        "spy_ma200": spy_ma200,
        "spy_ma200_slope_pct": spy_ma200_slope_pct,
        "spy_above_200dma": spy_last > spy_ma200,
        "spy_50_above_200": spy_ma50 > spy_ma200,
        "breadth_pct_above_200dma": breadth_pct_200,
        "risk_on": risk_on,
    }


def render_regime_banner(regime: dict | None) -> str:
    if regime is None:
        return "**Regime**: unavailable (SPY data fetch failed or insufficient history)"
    verdict = "RISK-ON" if regime["risk_on"] else "RISK-OFF"
    spy_vs_200 = (regime["spy_last"] / regime["spy_ma200"] - 1) * 100
    cross = ">" if regime["spy_50_above_200"] else "<"
    parts = [
        f"SPY {regime['spy_last']:.1f} vs 200DMA {regime['spy_ma200']:.1f} "
        f"({spy_vs_200:+.1f}%)",
        f"50DMA {cross} 200DMA",
        f"200DMA slope (20d): {regime['spy_ma200_slope_pct']:+.2f}%",
    ]
    if regime.get("breadth_pct_above_200dma") is not None:
        parts.append(
            f"Breadth: {regime['breadth_pct_above_200dma']:.0f}% > 200DMA"
        )
    return f"**Regime**: {' · '.join(parts)} → **{verdict}**"


# ─── ATR for stops ───────────────────────────────────────────────────────
ATR_PERIOD_DAYS = 14


def compute_atrs(bars: pd.DataFrame, tickers: list[str],
                 period: int = ATR_PERIOD_DAYS) -> dict[str, dict]:
    """14-day ATR (simple-mean true-range variant) for each ticker, reused
    by stop-loss calculation. Reads from the same raw bars frame the
    universe was downloaded into — no extra network."""
    out = {}
    for t in tickers:
        try:
            if (t, "High") not in bars.columns:
                continue
            high = bars[(t, "High")].dropna()
            low = bars[(t, "Low")].dropna()
            close = bars[(t, "Close")].dropna()
            if min(len(high), len(low), len(close)) < period + 1:
                continue
            common = high.index.intersection(low.index).intersection(close.index)
            if len(common) < period + 1:
                continue
            high, low, close = high.loc[common], low.loc[common], close.loc[common]
            prev_close = close.shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1).dropna()
            if len(tr) < period:
                continue
            atr_val = float(tr.tail(period).mean())
            last_close = float(close.iloc[-1])
            if atr_val <= 0 or last_close <= 0:
                continue
            out[t] = {
                "atr": atr_val,
                "last_close": last_close,
                "atr_pct": atr_val / last_close * 100,
            }
        except Exception:
            continue
    return out


def attach_atr_stops(picks: list[dict], top_n: int,
                     atrs: dict[str, dict], atr_mult: float) -> list[dict]:
    """Add two stop levels per pick:
      - stop_now / stop_now_pct: ATR-from-current-price. Risk if entered at
        spot today (relevant if buying before the breakout triggers).
      - stop_trigger / stop_trigger_pct: ATR-from-pivot. Risk if entered at
        the breakout via a buy-stop at the pivot (the textbook entry for
        these setups). The % is relative to the *pivot*, not current price.

    The two numbers diverge by `to_pivot_pct`. For 🔥 names (close to pivot)
    they're similar; for ⏳ names a few % below pivot they differ — and the
    trigger stop is the one that actually applies if you wait for the
    proper breakout entry.

    No TrailStop here: trailing makes sense for already-running positions;
    base setups haven't triggered, so spot/pivot stops are what matters."""
    for p in picks[:top_n]:
        info = atrs.get(p["ticker"])
        if info is None:
            continue
        p["atr"] = round(info["atr"], 2)
        p["atr_pct"] = round(info["atr_pct"], 2)
        # Stop-from-spot
        stop_now = info["last_close"] - atr_mult * info["atr"]
        p["stop_now"] = round(stop_now, 2)
        p["stop_now_pct"] = round((stop_now / info["last_close"] - 1) * 100, 2)
        # Stop-from-pivot (canonical for base-and-breakout entries)
        pivot = p.get("pivot_price")
        if pivot is not None and pivot > 0:
            stop_trigger = pivot - atr_mult * info["atr"]
            p["stop_trigger"] = round(stop_trigger, 2)
            p["stop_trigger_pct"] = round(
                (stop_trigger / pivot - 1) * 100, 2
            )
        else:
            p["stop_trigger"] = None
            p["stop_trigger_pct"] = None
    return picks


# ─── Sectors ─────────────────────────────────────────────────────────────
SECTOR_ABBREV = {
    "Technology": "Tech",
    "Information Technology": "Tech",
    "Communication Services": "Comm Svc",
    "Consumer Cyclical": "Cons Cyc",
    "Consumer Discretionary": "Cons Disc",
    "Consumer Defensive": "Cons Def",
    "Consumer Staples": "Cons Stp",
    "Healthcare": "Health",
    "Health Care": "Health",
    "Financial Services": "Financ",
    "Financials": "Financ",
    "Industrials": "Indust",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Materials": "Materials",
    "Utilities": "Utils",
    "Real Estate": "REIT",
}


def load_sectors() -> dict[str, dict]:
    if not SECTORS_FILE.exists():
        return {}
    try:
        data = json.loads(SECTORS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_sectors(sectors: dict[str, dict]):
    tmp = SECTORS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sectors, indent=2, sort_keys=True))
    tmp.replace(SECTORS_FILE)


def _fetch_sector_one(ticker: str) -> tuple[str, dict | None]:
    try:
        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector") or ""
        industry = info.get("industry") or ""
        if not sector and not industry:
            return (ticker, None)
        return (ticker, {
            "sector": sector,
            "industry": industry,
            "ts": int(datetime.now().timestamp()),
        })
    except Exception:
        return (ticker, None)


def refresh_sectors(tickers: list[str], existing: dict | None = None,
                    max_workers: int = 10) -> dict[str, dict]:
    """Lazily refresh sectors for tickers missing or past TTL. Failed
    lookups (Yahoo throttles aggressively) render as `—` and retry next run."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache = dict(existing or {})
    now_ts = datetime.now().timestamp()
    stale_threshold = now_ts - SECTORS_TTL_DAYS * 86400
    to_fetch = [
        t for t in tickers
        if t not in cache or cache.get(t, {}).get("ts", 0) < stale_threshold
    ]
    if not to_fetch:
        return cache
    print(f"Fetching sectors for {len(to_fetch)} ticker(s)...", file=sys.stderr)
    fetched, failed = 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_sector_one, t) for t in to_fetch]
        for fut in as_completed(futures):
            t, info = fut.result()
            if info is not None:
                cache[t] = info
                fetched += 1
            else:
                failed += 1
    if failed:
        print(f"Sectors: {fetched} fetched, {failed} failed (will retry next run).",
              file=sys.stderr)
    save_sectors(cache)
    return cache


def attach_sectors(picks: list[dict], top_n: int,
                   sectors: dict[str, dict]) -> list[dict]:
    for p in picks[:top_n]:
        info = sectors.get(p["ticker"]) or {}
        p["sector"] = info.get("sector") or ""
        p["industry"] = info.get("industry") or ""
    return picks


def abbreviate_sector(sector: str) -> str:
    if not sector:
        return "—"
    return SECTOR_ABBREV.get(sector, sector[:10])


def compute_sector_breakdown(picks: list[dict], top_n: int) -> dict | None:
    n_total = len(picks[:top_n])
    tagged = [p.get("sector", "") for p in picks[:top_n] if p.get("sector")]
    if not tagged:
        return None
    counts: dict[str, int] = {}
    for s in tagged:
        counts[s] = counts.get(s, 0) + 1
    return {"counts": counts, "n_tagged": len(tagged), "n_total": n_total}


def render_sector_breakdown(picks: list[dict], top_n: int,
                            max_show: int = 5) -> str | None:
    bd = compute_sector_breakdown(picks, top_n)
    if bd is None:
        return None
    sorted_sectors = sorted(bd["counts"].items(), key=lambda x: -x[1])
    shown = sorted_sectors[:max_show]
    remainder = sum(c for _, c in sorted_sectors[max_show:])
    parts = [f"{s} {c}" for s, c in shown]
    if remainder:
        parts.append(f"Other {remainder}")
    n_tagged, n_total = bd["n_tagged"], bd["n_total"]
    suffix = f" ({n_tagged}/{n_total} tagged)" if n_tagged < n_total else ""
    return f"**Sectors**: {' · '.join(parts)}{suffix}"


# ─── History I/O ─────────────────────────────────────────────────────────
def make_run_id(now: datetime, allow_same_day: bool = False) -> str:
    """Default: date-only ET-anchored id (one scan per scan-day). With
    --allow-same-day, falls back to second precision to keep run_id unique."""
    if allow_same_day:
        return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return now.astimezone(MARKET_TZ).strftime("%Y%m%d")


def clear_history():
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    if tmp_path.exists():
        tmp_path.unlink()


def prune_non_trading_days() -> tuple[int, int]:
    """Drop history rows whose run_date ET-date is not an NYSE trading day."""
    history = load_history()
    if history.empty:
        return (0, 0)
    et_dates = history["run_date"].dt.tz_convert(MARKET_TZ).dt.date
    min_d, max_d = et_dates.min(), et_dates.max()
    cal = _NYSECalendar()
    holidays = set(cal.holidays(start=pd.Timestamp(min_d),
                                end=pd.Timestamp(max_d)).date)
    trading_mask = et_dates.map(
        lambda d: d.weekday() < 5 and d not in holidays
    )
    rows_removed = int((~trading_mask).sum())
    if rows_removed == 0:
        return (0, 0)
    run_ids_removed = int(history.loc[~trading_mask, "run_id"].nunique())
    kept = history[trading_mask].copy()
    kept["run_date"] = kept["run_date"].apply(lambda dt: dt.isoformat())
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    kept.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)
    return (rows_removed, run_ids_removed)


def load_history() -> pd.DataFrame:
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=HISTORY_COLS)
    df = pd.read_csv(HISTORY_FILE)
    if df.empty:
        return df
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True, format="ISO8601")
    return df


def append_history(picks: list[dict], run_id: str, run_date: datetime,
                   allow_same_day: bool = False):
    """At most one snapshot per ET calendar day. Same-day re-runs overwrite.
    Atomic write via .tmp + rename so a crash mid-write can't truncate."""
    if not picks:
        return

    new_rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "run_date": run_date.isoformat(),
                "ticker": p["ticker"],
                "rank": p["rank"],
                "base_score": p.get("base_score"),
                "base_weeks": p.get("base_weeks"),
                "width_pct": p.get("width_pct"),
                "bb_pctile": p.get("bb_pctile"),
                "vol_dryup_ratio": p.get("vol_dryup_ratio"),
                "rs_slope_pct_per_wk": p.get("rs_slope_pct_per_wk"),
                "to_pivot_pct": p.get("to_pivot_pct"),
                "pivot_price": p.get("pivot_price"),
                "signal": p.get("signal"),
            }
            for p in picks
        ],
        columns=HISTORY_COLS,
    )

    has_existing = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    existing = pd.read_csv(HISTORY_FILE) if has_existing else None

    if existing is not None and not allow_same_day and not existing.empty:
        today_et = run_date.astimezone(MARKET_TZ).date()
        existing_dates = (pd.to_datetime(existing["run_date"],
                                         utc=True, format="ISO8601")
                          .dt.tz_convert(MARKET_TZ).dt.date)
        mask = existing_dates == today_et
        replaced = int(mask.sum())
        if replaced:
            print(
                f"Replacing {replaced} existing rows for {today_et} "
                f"(same America/New_York date).",
                file=sys.stderr,
            )
        existing = existing.loc[~mask]

    if existing is None or existing.empty:
        combined = new_rows
    else:
        combined = pd.concat([existing, new_rows], ignore_index=True)
    combined = (combined
                .sort_values(["run_date", "rank"], kind="stable")
                .reset_index(drop=True))
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)


def enrich_with_persistence(picks: list[dict], history: pd.DataFrame,
                            current_run_id: str) -> list[dict]:
    """Add streak / first_seen / rank_delta from prior runs."""
    if history.empty:
        for p in picks:
            p.update({"streak": 1, "first_seen": "—", "prev_rank": None,
                      "rank_delta": None})
        return picks

    prior = history[history["run_id"] != current_run_id].copy()
    if prior.empty:
        for p in picks:
            p.update({"streak": 1, "first_seen": "—", "prev_rank": None,
                      "rank_delta": None})
        return picks

    run_ids_ordered = (prior.sort_values("run_date")["run_id"]
                       .drop_duplicates().tolist())

    for p in picks:
        t = p["ticker"]
        appearances = prior[prior["ticker"] == t].sort_values("run_date")
        if appearances.empty:
            p.update({"streak": 1, "first_seen": "🆕", "prev_rank": None,
                      "rank_delta": None})
            continue
        p["first_seen"] = appearances["run_date"].iloc[0].strftime("%Y-%m-%d")
        prev = appearances.iloc[-1]
        p["prev_rank"] = int(prev["rank"])
        p["rank_delta"] = int(prev["rank"]) - p["rank"]
        ticker_runs = set(appearances["run_id"].tolist())
        streak = 1
        for rid in reversed(run_ids_ordered):
            if rid in ticker_runs:
                streak += 1
            else:
                break
        p["streak"] = streak
    return picks


# Dropout-reason classification thresholds (relative to the prior-run pivot).
# A name that fell out of the watchlist between runs is classified as:
#   "broke_out"    — current close ≥ pivot × (1 + BROKE_OUT_PCT/100)
#   "broke_down"   — current close < pivot × (1 + BROKE_DOWN_PCT/100)
#   "faded"        — neither (e.g., score drifted below threshold)
# Defaults: broke_out at +0.5% (slightly above pivot = confirmed cross),
# broke_down at -8% (deep enough to confirm the base failed, not just a wobble).
BROKE_OUT_THRESHOLD_PCT = 0.5
BROKE_DOWN_THRESHOLD_PCT = -8.0


def dropouts_with_reason(history: pd.DataFrame, current_picks: set[str],
                          current_run_id: str, top_n: int,
                          current_prices: dict[str, pd.Series],
                          broke_out_pct: float = BROKE_OUT_THRESHOLD_PCT,
                          broke_down_pct: float = BROKE_DOWN_THRESHOLD_PCT,
                          ) -> list[dict]:
    """Names in prior run's top N but missing now. Where possible, infer
    *why*: broke out above pivot (good), broke down below base (bad),
    deduped against a same-issuer pair (neither good nor bad — just hidden
    by the rule), or no longer qualifies for some other reason (faded).

    Thresholds are tunable for users who want stricter "confirmed" criteria
    (e.g. broke_out_pct=2.0 only counts breakouts ≥ 2% above pivot).

    Note on dedup: a ticker that fell out *because of* same-issuer dedup
    (PBR dropped because PBR-A scored higher) is labeled `deduped` so the
    user doesn't read it as a real signal. The check is structural — if
    the dropped ticker has a same-issuer partner that IS in current_picks,
    we assume the dedup rule is the cause regardless of price action."""
    prior = history[history["run_id"] != current_run_id]
    if prior.empty:
        return []
    last_run_id = prior.sort_values("run_date")["run_id"].iloc[-1]
    last_run = prior[prior["run_id"] == last_run_id]
    last_run = last_run[last_run["rank"] <= top_n]
    dropped = last_run[~last_run["ticker"].isin(current_picks)]

    # Pre-build "if the dropped sibling is X, the kept sibling in
    # current_picks is Y" lookup — so dedup classification is O(1) per
    # dropped row instead of scanning SAME_ISSUER_PAIRS every iteration.
    deduped_against: dict[str, str] = {}
    for a, b in SAME_ISSUER_PAIRS:
        if a in current_picks:
            deduped_against[b] = a
        if b in current_picks:
            deduped_against[a] = b

    results = []
    for _, r in dropped.iterrows():
        ticker = r["ticker"]
        prev_pivot = r.get("pivot_price")
        prev_signal = r.get("signal", "")
        current_close = None
        if ticker in current_prices and not current_prices[ticker].empty:
            current_close = float(current_prices[ticker].iloc[-1])

        # Dedup takes precedence over price-based reasons — a deduped
        # ticker isn't really "broke_out" or "faded" in any meaningful
        # sense, just hidden by the same-issuer rule.
        if ticker in deduped_against:
            reason = "deduped"
        else:
            reason = "faded"
            if (current_close is not None and prev_pivot is not None
                    and prev_pivot > 0):
                change_from_pivot_pct = (current_close / float(prev_pivot) - 1) * 100
                if change_from_pivot_pct >= broke_out_pct:
                    reason = "broke_out"
                elif change_from_pivot_pct < broke_down_pct:
                    reason = "broke_down"

        results.append({
            "ticker": ticker,
            "prev_rank": int(r["rank"]),
            "prev_signal": prev_signal,
            "prev_pivot": float(prev_pivot) if prev_pivot is not None else None,
            "current_close": current_close,
            "reason": reason,
            "deduped_against": deduped_against.get(ticker),
        })
    return results


# ─── Scoring pipeline ────────────────────────────────────────────────────
def score_tickers(closes: dict[str, pd.Series],
                  volumes: dict[str, pd.Series],
                  spy_close: pd.Series,
                  rs_ratings: dict[str, float],
                  min_base_weeks: float,
                  max_base_weeks: float,
                  max_base_width_pct: float,
                  max_to_52w_high_pct: float,
                  min_rs_rating: float,
                  min_base_score: float,
                  smoothness_band_pct: float = SMOOTH_BAND_PCT,
                  verbose: bool = False) -> list[dict]:
    """Run each ticker through the full pipeline. Returns sorted picks
    (highest base_score first), each with rank assigned."""
    results = []
    n_eligible = 0
    n_passes_rs = 0
    n_passes_tt = 0
    n_has_volume = 0
    n_has_base = 0
    n_passes_score = 0
    tt_failure_reasons: dict[str, int] = {}
    for ticker, close in closes.items():
        n_eligible += 1
        rs_rating = rs_ratings.get(ticker)
        if rs_rating is None or rs_rating < min_rs_rating:
            continue
        n_passes_rs += 1

        passes, tt_details = passes_trend_template(
            close, rs_rating,
            max_dist_from_52w_high_pct=max_to_52w_high_pct,
        )
        if not passes:
            reason = tt_details.get("reason", "unknown")
            tt_failure_reasons[reason] = tt_failure_reasons.get(reason, 0) + 1
            continue
        n_passes_tt += 1

        volume = volumes.get(ticker)
        if volume is None or volume.empty:
            continue
        n_has_volume += 1

        base = detect_base(
            close, volume,
            min_base_weeks=min_base_weeks,
            max_base_weeks=max_base_weeks,
            max_base_width_pct=max_base_width_pct,
            max_to_52w_high_pct=max_to_52w_high_pct,
            smoothness_band_pct=smoothness_band_pct,
        )
        if base is None:
            continue
        n_has_base += 1

        bb_pctile = compute_bb_pctile(close)
        three_wk_tight = three_weeks_tight(close)
        try:
            anchor_idx = pd.Timestamp(base["anchor_date"])
        except Exception:
            anchor_idx = None
        rs_slope = (compute_rs_slope_pct_per_wk(close, spy_close, anchor_idx)
                    if anchor_idx is not None else None)

        base_score = compute_base_score(base, bb_pctile, rs_slope, three_wk_tight)
        if base_score < min_base_score:
            continue
        n_passes_score += 1

        signal = classify_signal(base, bb_pctile)

        results.append({
            "ticker": ticker,
            "rs_rating": round(rs_rating, 0),
            "base_score": base_score,
            "base_weeks": base["base_weeks"],
            "anchor_date": base["anchor_date"],
            "anchor_mode": base.get("anchor_mode"),
            "width_pct": base["width_pct"],
            "smoothness_pct": base.get("smoothness_pct"),
            # Note: base["quality_score"] is intentionally NOT carried through
            # — it's an internal window-selection metric inside detect_base,
            # not a user-facing measure. Surfacing it would invite confusion
            # with base_score.
            "bb_pctile": bb_pctile,
            "vol_dryup_ratio": base.get("vol_dryup_ratio"),
            "today_vol_ratio": base.get("today_vol_ratio"),
            "rs_slope_pct_per_wk": round(rs_slope, 2) if rs_slope is not None else None,
            "to_pivot_pct": base["to_pivot_pct"],
            "to_52w_high_pct": base["to_52w_high_pct"],
            "pivot_price": base["pivot_price"],
            "base_high": base["base_high"],
            "base_low": base["base_low"],
            "is_breakout_day": base.get("is_breakout_day", False),
            "three_wk_tight": three_wk_tight,
            "signal": signal,
            "last_close": base["last_close"],
        })

    results.sort(key=lambda r: -r["base_score"])
    pre_dedup_count = len(results)
    results, _ = _dedup_same_issuer(results)  # dropped list isn't returned;
    # the kept pick's `dedup_sibling` field carries the runner-up forward.
    post_dedup_count = len(results)
    n_deduped = pre_dedup_count - post_dedup_count

    # Funnel summary on stderr — single most useful context when the scan
    # returns few/no picks. Now also reports the dedup count so the
    # "score≥40 = 15" → "Passed filter: 14" gap is explained.
    # Use :g for the gate values — drops trailing zeros for integer-like
    # inputs (RS≥70, score≥40) but keeps decimals when the user passes
    # a non-integer (--min-rs-rating 75.5 displays as "RS≥75.5", not "76").
    funnel_parts = [
        f"{n_eligible}",
        f"{n_passes_rs} (RS≥{min_rs_rating:g})",
        f"{n_passes_tt} (TT)",
        f"{n_has_base} (valid base)",
        f"{n_passes_score} (score≥{min_base_score:g})",
    ]
    if n_deduped:
        funnel_parts.append(f"{post_dedup_count} (after dedup, -{n_deduped})")
    print("Funnel: " + " → ".join(funnel_parts), file=sys.stderr)

    if verbose and tt_failure_reasons:
        top_reasons = sorted(tt_failure_reasons.items(),
                              key=lambda kv: -kv[1])[:5]
        print("Trend template failures: " +
              ", ".join(f"{r}={n}" for r, n in top_reasons),
              file=sys.stderr)

    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


# Known same-issuer ticker pairs in the US large-cap universe. When both
# sides of a pair pass the filter, we keep only the higher-scoring one —
# they're the same company's economic exposure and shouldn't double-count
# in a watchlist. Naming convention: most-common ticker first. The list
# is intentionally short — only the pairs that *actually appear* regularly
# in the screened universe. Names that left the universe (Shell unified
# RDS-A/B into SHEL, etc.) or never appeared (BIO-B, UBSI-P) are removed.
SAME_ISSUER_PAIRS = [
    ("PBR", "PBR-A"),       # Petrobras common / preferred
    ("GOOG", "GOOGL"),      # Alphabet class C / class A
    # BRK-A/B intentionally omitted — BRK-A's $700k+ share price prevents
    # it from ever appearing in the volume/market-cap-filtered universe,
    # so the dedup rule would never fire even if both classes existed
    # there in principle.
    ("FOXA", "FOX"),        # Fox class A / class B (voting / non-voting)
    ("LBRDA", "LBRDK"),     # Liberty Broadband (also LBRDB exists but is rare)
    ("BF-B", "BF-A"),       # Brown-Forman B (non-voting, more liquid) / A
    ("HEI", "HEI-A"),       # HEICO common / class A
]


def _dedup_same_issuer(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop the lower-scoring half of any same-issuer ticker pair. Two
    Petrobras share classes (PBR + PBR-A) showing up in the top 5 are
    one economic signal, not two — keep the higher-scoring side.

    Returns (kept, dropped) so the caller can surface the dropped pair-
    mate as a small parenthetical hint on the kept ticker (e.g. "PBR-A
    (also PBR, score 46)") instead of silently disappearing it."""
    if not results:
        return results, []
    by_ticker = {r["ticker"]: r for r in results}
    dropped_records: list[dict] = []
    drop_tickers: set[str] = set()
    for a, b in SAME_ISSUER_PAIRS:
        if a in by_ticker and b in by_ticker:
            # Keep the higher base_score; drop the other. If tied, keep `a`
            # (the "main" ticker).
            if by_ticker[a]["base_score"] >= by_ticker[b]["base_score"]:
                kept, dropped = a, b
            else:
                kept, dropped = b, a
            drop_tickers.add(dropped)
            # Attach a sibling reference on the kept ticker so the renderer
            # can surface "(also: $dropped, score X)" instead of hiding it.
            by_ticker[kept].setdefault("dedup_sibling", {
                "ticker": dropped,
                "base_score": by_ticker[dropped]["base_score"],
            })
            dropped_records.append(by_ticker[dropped])
    if not drop_tickers:
        return results, []
    kept_results = [r for r in results if r["ticker"] not in drop_tickers]
    return kept_results, dropped_records


# ─── Rendering ───────────────────────────────────────────────────────────
def render_table(picks: list[dict], top_n: int) -> str:
    rows = picks[:top_n]
    if not rows:
        return "(no picks passed the filter)"
    show_sector = any(p.get("sector") for p in rows)
    show_stop = any(p.get("stop_trigger") is not None for p in rows)
    show_smooth = any(p.get("smoothness_pct") is not None for p in rows)
    headers = ["#", "Ticker"]
    if show_sector:
        headers.append("Sector")
    headers += ["Score", "RS", "BaseWks", "Width%"]
    if show_smooth:
        headers.append("Smooth%")
    headers += ["BB%ile", "Vol↓", "RSslope%/wk", "ToPivot%", "Pivot", "Sig"]
    if show_stop:
        # Stop @ trigger (pivot-anchored) is the canonical stop for these
        # setups — that's where the user actually enters via a buy-stop.
        headers.append("Stop@trigger")
    headers += ["Streak", "RankΔ", "FirstSeen"]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for p in rows:
        delta = p.get("rank_delta")
        if delta is None:
            delta_str = "🆕"
        elif delta > 0:
            delta_str = f"+{delta} ↗"
        elif delta < 0:
            delta_str = f"{delta} ↘"
        else:
            delta_str = "—"

        # Three-weeks-tight gets a 🔒 prefix on the ticker — quick visual
        # for "supply absorbed" without an extra column.
        ticker_disp = f"**{p['ticker']}**"
        if p.get("three_wk_tight"):
            ticker_disp = "🔒 " + ticker_disp
        # If this ticker absorbed a same-issuer dedup, append the runner-
        # up as a parenthetical. Users still learn "PBR also passed" even
        # though we only list PBR-A as the canonical row.
        sib = p.get("dedup_sibling")
        if sib:
            ticker_disp += f" _(also {sib['ticker']}, score {sib['base_score']:.0f})_"

        row = [str(p["rank"]), ticker_disp]
        if show_sector:
            row.append(abbreviate_sector(p.get("sector", "")))
        rs = p.get("rs_rating")
        bb = p.get("bb_pctile")
        vol_dryup = p.get("vol_dryup_ratio")
        rs_slope = p.get("rs_slope_pct_per_wk")
        smooth = p.get("smoothness_pct")
        row += [
            f"{p['base_score']:.0f}",
            f"{rs:.0f}" if rs is not None else "—",
            f"{p['base_weeks']:.1f}",
            f"{p['width_pct']:.1f}",
        ]
        if show_smooth:
            row.append(f"{smooth:.0f}" if smooth is not None else "—")
        row += [
            f"{bb:.0f}" if bb is not None else "—",
            f"{vol_dryup:.2f}" if vol_dryup is not None else "—",
            f"{rs_slope:+.2f}" if rs_slope is not None else "—",
            f"{p['to_pivot_pct']:+.1f}",
            f"${p['pivot_price']:.2f}",
        ]
        # Signal display: append a small mode marker. anchor_mode=3 means
        # the base ended a few days ago and today is a fresh cross above
        # the prior high — actionable now. mode=0 means today is still
        # inside the consolidation. Same Sig glyph in both cases, but the
        # mode marker tells the user which situation they're in.
        sig = p.get("signal", "—")
        if p.get("anchor_mode") == BASE_BREAKOUT_DETECT_BUFFER_DAYS:
            sig = sig + "*"  # asterisk = "breakout mode" (mode 3)
        row.append(sig)
        if show_stop:
            st = p.get("stop_trigger")
            stpct = p.get("stop_trigger_pct")
            if st is not None and stpct is not None:
                row.append(f"${st:.2f} ({stpct:+.1f}%)")
            else:
                row.append("—")
        row += [
            str(p.get("streak", 1)),
            delta_str,
            p.get("first_seen", "—"),
        ]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


# ─── History summary ─────────────────────────────────────────────────────
def _longest_consecutive_streak(run_ids_for_ticker: list[str],
                                 ordered_run_ids: list[str]) -> int:
    present = set(run_ids_for_ticker)
    best = cur = 0
    for rid in ordered_run_ids:
        if rid in present:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def show_history_summary(history: pd.DataFrame):
    if history.empty:
        print("History is empty.")
        return
    runs = history.sort_values("run_date")["run_id"].drop_duplicates().tolist()
    print(f"Total runs: {len(runs)}")
    print(f"Date range: {history['run_date'].min().date()} → "
          f"{history['run_date'].max().date()}")
    counts = history.groupby("ticker").size().sort_values(ascending=False)

    latest_run_id = runs[-1]
    longest_active: list[tuple[str, int]] = []
    longest_historical: list[tuple[str, int]] = []
    by_ticker = history.groupby("ticker")["run_id"].apply(list).to_dict()
    for t, ticker_runs in by_ticker.items():
        longest = _longest_consecutive_streak(ticker_runs, runs)
        longest_historical.append((t, longest))
        present = set(ticker_runs)
        active = 0
        for rid in reversed(runs):
            if rid in present:
                active += 1
            else:
                break
        if active > 0:
            longest_active.append((t, active))

    longest_active.sort(key=lambda x: -x[1])
    longest_historical.sort(key=lambda x: -x[1])

    if longest_active:
        print(f"\nLongest active base streaks (still in setup through {latest_run_id}):")
        for t, n in longest_active[:10]:
            print(f"  {t:<8} {n} runs")

    print(f"\nLongest historical base streaks (any time in history):")
    for t, n in longest_historical[:10]:
        print(f"  {t:<8} {n} runs")

    print(f"\nTop 20 most-frequent tickers across all runs:")
    for t, c in counts.head(20).items():
        print(f"  {t:<8} {c} appearances")


# ─── CLI ─────────────────────────────────────────────────────────────────
def _run_single_ticker_pipeline(args) -> dict:
    """Run the full pipeline on a single ticker and return a structured
    result dict. Used by both markdown and JSON render paths."""
    ticker = args.ticker.upper().strip()
    result: dict = {"ticker": ticker, "stages": {}}

    # Sector/industry tag (cheap — uses the same yf.Ticker.info call as
    # the main scan, hits the disk cache where possible). Useful especially
    # for foreign / less-known tickers where the user might not know the
    # business at a glance. Skipped only when --no-sectors is set.
    if not args.no_sectors:
        sectors_cache = load_sectors()
        sectors_cache = refresh_sectors([ticker], sectors_cache, max_workers=1)
        sec_info = sectors_cache.get(ticker, {})
        result["sector"] = sec_info.get("sector") or ""
        result["industry"] = sec_info.get("industry") or ""

    bars = fetch_bars([ticker], history_months=DEFAULT_HISTORY_MONTHS)
    closes = extract_field(bars, [ticker], "Close")
    volumes = extract_field(bars, [ticker], "Volume")
    if ticker not in closes:
        # Structured error so JSON consumers can branch on type rather
        # than regex-match the human-readable message.
        result["error_type"] = "no_data"
        result["error"] = (
            f"No price data. Either: (a) typo, "
            f"(b) too few bars (need ≥ {MIN_BARS_REQUIRED} trading days), "
            f"or (c) yfinance fetch failed."
        )
        return result
    close = closes[ticker]
    volume = volumes.get(ticker)
    result["last_close"] = float(close.iloc[-1])

    # SPY for RS slope (during base) and RS Rating proxy.
    try:
        spy_bars = yf.download("SPY", period=f"{DEFAULT_HISTORY_MONTHS}mo",
                                interval="1d", auto_adjust=True, progress=False)
        spy_close = spy_bars["Close"]
        if isinstance(spy_close, pd.DataFrame):
            spy_close = spy_close.iloc[:, 0]
        spy_close = spy_close.dropna()
    except Exception:
        spy_close = pd.Series(dtype=float)

    if not spy_close.empty and len(close) >= max(RS_PERIODS_TRADING_DAYS) + 1:
        rs_rating = _compute_rs_proxy(close, spy_close)
    else:
        rs_rating = None
    result["rs_proxy"] = rs_rating

    # Stage 1: Trend Template
    passes, tt_details = passes_trend_template(
        close, rs_rating,
        max_dist_from_52w_high_pct=args.max_to_52w_high,
    )
    result["stages"]["trend_template"] = {
        "passes": passes,
        "details": tt_details,
    }
    if not passes:
        return result

    # Stage 2: Base detection
    base = detect_base(close, volume,
                        min_base_weeks=args.min_base_weeks,
                        max_base_weeks=args.max_base_weeks,
                        max_base_width_pct=args.max_base_width,
                        max_to_52w_high_pct=args.max_to_52w_high,
                        smoothness_band_pct=args.smoothness_band_pct)
    result["stages"]["base"] = base
    if base is None:
        # Recent-breakout fallback for "broke out, no longer a base" names.
        if args.recent_breakout_days > 0:
            rb = detect_recent_breakout(close, volume,
                                        lookback_days=args.recent_breakout_days)
            result["stages"]["recent_breakout"] = rb
        return result

    # Stage 3: Quality metrics
    bb_pctile = compute_bb_pctile(close)
    three_wk = three_weeks_tight(close)
    try:
        anchor_idx = pd.Timestamp(base["anchor_date"])
        rs_slope = (compute_rs_slope_pct_per_wk(close, spy_close, anchor_idx)
                    if anchor_idx is not None else None)
    except Exception:
        rs_slope = None
    result["stages"]["quality"] = {
        "bb_pctile": bb_pctile,
        "rs_slope_pct_per_wk": rs_slope,
        "three_weeks_tight": three_wk,
    }

    # Stage 4: Score + signal + ATR stop
    base_score = compute_base_score(base, bb_pctile, rs_slope, three_wk)
    signal = classify_signal(base, bb_pctile)
    result["stages"]["score"] = {
        "base_score": base_score,
        "signal": signal,
    }

    # ATR-based stops (computed only when atr_stop_mult is enabled).
    # Reuses compute_atrs which takes the raw bars frame from fetch_bars.
    if args.atr_stop_mult and args.atr_stop_mult > 0:
        atrs = compute_atrs(bars, [ticker])
        info = atrs.get(ticker)
        if info is not None:
            stop_now = info["last_close"] - args.atr_stop_mult * info["atr"]
            stop_trigger = base["pivot_price"] - args.atr_stop_mult * info["atr"]
            result["atr"] = {
                "atr_14d": round(info["atr"], 2),
                "atr_pct": round(info["atr_pct"], 2),
                "stop_now": round(stop_now, 2),
                "stop_now_pct": round((stop_now / info["last_close"] - 1) * 100, 2),
                "stop_trigger": round(stop_trigger, 2),
                "stop_trigger_pct": round(
                    (stop_trigger / base["pivot_price"] - 1) * 100, 2
                ),
            }
    return result


def _render_single_ticker_markdown(args, result: dict) -> None:
    """Render the human-friendly markdown report from a pipeline result."""
    ticker = result["ticker"]
    sector = result.get("sector") or ""
    industry = result.get("industry") or ""
    if sector or industry:
        # Tucked into the header so the user immediately knows what
        # business they're looking at — especially useful for ADRs and
        # less-familiar tickers.
        tag_parts = [t for t in (sector, industry) if t]
        print(f"# Single-ticker check: {ticker} _({' / '.join(tag_parts)})_")
    else:
        print(f"# Single-ticker check: {ticker}")
    print(f"_(using fast RS-vs-SPY proxy in place of universe-relative RS "
          f"Rating; run the full scan if you need the exact percentile)_")

    if "error" in result:
        print(f"\n❌ {result['error']}")
        return

    rs_rating = result.get("rs_proxy")
    if rs_rating is not None:
        print(f"\n**RS proxy (vs SPY)**: ~{rs_rating:.0f}/99 (approximate)")
    else:
        print(f"\n**RS proxy (vs SPY)**: insufficient history "
              f"(need ≥ {max(RS_PERIODS_TRADING_DAYS) + 1} trading days)")

    # Stage 1
    tt = result["stages"]["trend_template"]
    tt_details = tt["details"]
    print(f"\n## Stage 1: Minervini Trend Template")
    if tt["passes"]:
        print(f"✅ **PASS** — all 7 criteria met")
    else:
        print(f"❌ **FAIL** — reason: `{tt_details.get('reason')}`")
    print(f"- Close: ${result['last_close']:.2f}")
    print(f"- 50DMA: ${tt_details.get('ma50', 0):.2f}, "
          f"150DMA: ${tt_details.get('ma150', 0):.2f}, "
          f"200DMA: ${tt_details.get('ma200', 0):.2f}")
    print(f"- 200DMA slope (21d): {tt_details.get('ma200_slope_pct', 0):+.2f}%")
    print(f"- 52w high: ${tt_details.get('high_52w', 0):.2f} "
          f"({tt_details.get('dist_from_52w_high_pct', 0):+.1f}% from current)")
    print(f"- 52w low: ${tt_details.get('low_52w', 0):.2f} "
          f"({tt_details.get('dist_from_52w_low_pct', 0):+.1f}% from current)")
    if rs_rating is not None:
        print(f"- RS proxy: ~{rs_rating:.0f} (gate is ≥ {args.min_rs_rating:.0f})")
    else:
        print(f"- RS proxy: —")

    if not tt["passes"]:
        print(f"\n→ {ticker} fails the trend template, so no base detection "
              f"runs. Not in tradeable-base state.")
        return

    # Stage 2
    base = result["stages"]["base"]
    print(f"\n## Stage 2: Base detection")
    if base is None:
        print(f"❌ **No valid base** — either too short (< "
              f"{args.min_base_weeks}wk), too wide (> {args.max_base_width}%), "
              f"or price too far from 52w high.")
        rb = result["stages"].get("recent_breakout")
        if rb is not None:
            # Action-oriented language: tell the user what to do, not just
            # "worth tracking". The "throwback" entry is the textbook
            # O'Neil play for retesting the prior pivot from above.
            print(f"\n→ But: **broke out {rb['days_since_breakout']}d ago** on "
                  f"{rb['breakout_date']} at ${rb['breakout_price']:.2f} "
                  f"({rb['breakout_vol_ratio']:.1f}× vol). Now: "
                  f"${rb['current_close']:.2f} "
                  f"({rb['follow_through_pct']:+.1f}% vs pivot).")
            print(f"\n  **Action**: set a price alert at "
                  f"${rb['prior_pivot']:.2f} (the breakout level). If price "
                  f"retests from above, that's the textbook O'Neil "
                  f"'throwback' entry. If it closes below ${rb['prior_pivot']:.2f} "
                  f"for 2 consecutive sessions, the breakout failed — drop it.")
        return

    print(f"✅ **Valid base** detected")
    print(f"- Length: {base['base_weeks']:.1f} weeks ({base['days_in_base']} trading days)")
    mode_label = "base-to-today" if base['anchor_mode'] == 0 else "breakout-today"
    print(f"- Anchor date: {base['anchor_date']} (mode={base['anchor_mode']}: "
          f"{mode_label})")
    print(f"- Width: {base['width_pct']:.1f}% (${base['base_low']:.2f} – ${base['base_high']:.2f})")
    if base.get('smoothness_pct') is not None:
        print(f"- Smoothness: {base['smoothness_pct']:.0f}% of bars within "
              f"±{args.smoothness_band_pct:.1f}% of mean")
    else:
        print(f"- Smoothness: —")
    print(f"- Pivot price: ${base['pivot_price']:.2f}")
    print(f"- Distance to pivot: {base['to_pivot_pct']:+.2f}%")
    print(f"- Volume dry-up ratio (20d/60d): "
          f"{base.get('vol_dryup_ratio', '—')}")
    print(f"- Today's volume vs 20d avg: "
          f"{base.get('today_vol_ratio', '—')}×")

    # Stage 3
    q = result["stages"]["quality"]
    print(f"\n## Stage 3: Quality metrics")
    if q['bb_pctile'] is not None:
        print(f"- BB(20) squeeze percentile (6mo): {q['bb_pctile']:.0f}")
    else:
        print(f"- BB squeeze pctile: —")
    if q['rs_slope_pct_per_wk'] is not None:
        print(f"- RS slope vs SPY during base: {q['rs_slope_pct_per_wk']:+.2f}%/wk")
    else:
        print(f"- RS slope: —")
    print(f"- Three-weeks-tight (🔒): {'yes' if q['three_weeks_tight'] else 'no'}")

    # Stage 4
    s = result["stages"]["score"]
    print(f"\n## Stage 4: Composite Base Score & Signal")
    print(f"- **Base Score: {s['base_score']:.0f}/100**")
    print(f"- **Signal: {s['signal']}**")

    # ATR stops — the position-sizing info that was missing before.
    atr = result.get("atr")
    if atr is not None:
        print(f"\n## Stage 5: Risk levels (ATR-based stops, "
              f"{args.atr_stop_mult}× ATR)")
        print(f"- 14-day ATR: ${atr['atr_14d']:.2f} "
              f"({atr['atr_pct']:.1f}% of price)")
        print(f"- **Stop @ trigger** (if entering at pivot): "
              f"${atr['stop_trigger']:.2f} "
              f"({atr['stop_trigger_pct']:+.1f}% from pivot)")
        print(f"- Stop @ now (if entering at current price): "
              f"${atr['stop_now']:.2f} "
              f"({atr['stop_now_pct']:+.1f}% from spot)")
        # Worked example for sizing — concrete number is more useful than
        # an abstract formula here.
        pivot = base['pivot_price']
        risk_per_share = pivot - atr['stop_trigger']
        if risk_per_share > 0:
            shares_per_500 = int(500 / risk_per_share)
            print(f"- For \\$500 max risk per trade: ~{shares_per_500} shares "
                  f"(risk-per-share = pivot − stop = ${risk_per_share:.2f})")

    if s['base_score'] < args.min_base_score:
        print(f"\n→ Below `--min-base-score {args.min_base_score}`, so this name "
              f"wouldn't appear in the standard watchlist. Geometry is real but "
              f"setup quality isn't loaded yet.")
    else:
        print(f"\n→ **{ticker} would appear in the standard watchlist.**")


def run_single_ticker_check(args) -> None:
    """Run the full pipeline on a single ticker and report which stage it
    passed or failed at. Output answers 'is this name in a tradeable base
    right now?' with the specific reason if not. No history is written —
    this is an ad-hoc lookup, not part of the persistence stream.

    Works for **any US ticker** that yfinance can fetch — large-cap,
    small-cap, ADR, even foreign-listed. The universe is not consulted.

    Speed (vs full scan): fetches only the target ticker + SPY, replacing
    the universe-relative RS Rating with an RS-vs-SPY proxy. ~2-5s vs
    ~30-60s for a full scan.

    Output format follows --format: markdown (default, 4-stage diagnostic
    report) or json (structured dict for downstream consumers).

    Run the full scan (without --ticker) if you need the exact universe-
    relative RS Rating."""
    result = _run_single_ticker_pipeline(args)
    if args.format == "json":
        # Replace pd.Series / numpy types with plain JSON-compatible types.
        # The pipeline result is already mostly clean; default=str catches
        # any pd.Timestamp that slipped through (e.g. inside tt_details).
        print(json.dumps(result, indent=2, default=str))
    else:
        _render_single_ticker_markdown(args, result)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-base-weeks", type=float, default=DEFAULT_MIN_BASE_WEEKS,
                    help=("Minimum base length in weeks (default 6). "
                          "Lower = catch fresh consolidations earlier, more noise."))
    ap.add_argument("--max-base-weeks", type=float, default=DEFAULT_MAX_BASE_WEEKS,
                    help=("Maximum base length in weeks (default 40). Very long "
                          "bases (year+) often resolve down."))
    ap.add_argument("--max-base-width", type=float, default=DEFAULT_MAX_BASE_WIDTH_PCT,
                    help=("Max width %% of base (high-low / high). Default 25. "
                          "Tighter bases (under 15%%) are higher quality."))
    ap.add_argument("--smoothness-band-pct", type=float, default=SMOOTH_BAND_PCT,
                    help=("Smoothness band (in %%). A base bar counts as "
                          "'smooth' if it's within ±this%% of the base mean. "
                          "Default 2.0 (calibrated for liquid US large-caps). "
                          "Lower (1.0) rewards only very tight bases; higher "
                          "(3-5) is more permissive — useful in high-vol "
                          "regimes where natural noise pushes bars outside "
                          "the default band even on clean horizontal bases."))
    ap.add_argument("--max-to-52w-high", type=float, default=DEFAULT_MAX_TO_52W_HIGH_PCT,
                    help=("Max distance from 52w high in %% (default 15). "
                          "Closer to high = nearer to resistance breakout."))
    ap.add_argument("--min-rs-rating", type=float, default=TT_MIN_RS_RATING,
                    help=("Minervini RS Rating threshold (default 70 = top 30%% "
                          "of universe). Lower = more candidates."))
    ap.add_argument("--min-base-score", type=float, default=40.0,
                    help=("Composite Base Score floor for display (0-100, "
                          "default 40). Bump to 60 for high-conviction only."))
    ap.add_argument("--top-n", type=int, default=30,
                    help="How many candidates to display + log.")
    ap.add_argument("--min-market-cap", type=float, default=5e9)
    ap.add_argument("--min-volume", type=int, default=1_000_000)
    ap.add_argument("--universe-count", type=int, default=250,
                    help=("Universe size pulled from Yahoo's screener. "
                          "Paginated in 250-name pages; up to ~1000 is "
                          "practically supported before Yahoo's screener "
                          "returns fewer results per page."))
    ap.add_argument("--ticker", type=str, default=None,
                    help=("Single-ticker check mode. Bypass the universe "
                          "scan; show this ticker's funnel stage (which "
                          "filter passes/fails) and all base metrics. "
                          "Useful for 'is AAPL in a tradeable base right "
                          "now' questions. No history is written."))
    ap.add_argument("--broke-out-pct", type=float, default=BROKE_OUT_THRESHOLD_PCT,
                    help=("Dropout-reason threshold: a dropped name with "
                          "current price ≥ pivot × (1 + this/100) is "
                          "labeled 'broke_out'. Default 0.5%%."))
    ap.add_argument("--broke-down-pct", type=float, default=BROKE_DOWN_THRESHOLD_PCT,
                    help=("Dropout-reason threshold: a dropped name with "
                          "current price < pivot × (1 + this/100) is "
                          "labeled 'broke_down'. Default -8%%."))
    ap.add_argument("--recent-breakout-days", type=int,
                    default=RECENT_BREAKOUT_LOOKBACK_DAYS,
                    help=("Window for the 'Recent breakouts' section: how "
                          "many trading days back to scan for fresh "
                          "breakouts on volume. Default 10. Set to 0 to "
                          "disable the section."))
    ap.add_argument("--refresh-universe", dest="refresh_universe",
                    action="store_const", const=True, default=None,
                    help="Force-refresh universe (ignore cache).")
    ap.add_argument("--no-refresh-universe", dest="refresh_universe",
                    action="store_const", const=False,
                    help="Use cached universe even if past TTL.")
    ap.add_argument("--show-history", action="store_true")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--prune-non-trading-days", action="store_true",
                    help=("Drop history rows whose run_date ET-date is not an "
                          "NYSE trading day."))
    ap.add_argument("--no-save", action="store_true",
                    help="Don't append this run to history.")
    ap.add_argument("--save-stale", action="store_true",
                    help=("Save to history even when today's ET date is a "
                          "weekend / NYSE holiday. Default skips so streak "
                          "counts don't inflate from duplicate-data days."))
    ap.add_argument("--allow-same-day", action="store_true",
                    help=("[advanced/debug] Append even if a row exists for "
                          "today's ET date. Default overwrites today's "
                          "snapshot — that's the right behavior for normal "
                          "use since intra-day re-runs should refresh, not "
                          "duplicate. Only enable if you specifically want "
                          "multiple snapshots per ET date in the history."))
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--regime-gate", choices=["off", "warn", "strict"],
                    default="warn",
                    help=("Market trend filter using SPY 200DMA + slope and "
                          "universe breadth. RISK-ON requires SPY > 200DMA "
                          "AND 200DMA slope > -0.05%% dead band. "
                          "off=no banner, warn=show banner + still print "
                          "top-N (default), strict=suppress top-N if RISK-OFF."))
    ap.add_argument("--atr-stop-mult", type=float, default=2.5,
                    help=("ATR-based stop multiplier (default 2.5). Computes "
                          "14-day ATR per pick and adds a Stop column. "
                          "Pass 0 to disable."))
    ap.add_argument("--no-sectors", action="store_true",
                    help="Skip sector tagging (no Sector column).")
    ap.add_argument("--persistent-min-streak", type=int, default=4,
                    help=("Streak threshold for 'Maturing bases' section. "
                          "Default 4 ≈ 'survived one trading week as a setup' "
                          "for daily users, or 4 weeks for weekly runners."))
    ap.add_argument("--verbose", action="store_true",
                    help=("Print pipeline funnel diagnostics (how many "
                          "names passed each filter stage) to stderr. "
                          "Useful when the screen returns few/no picks "
                          "and you want to understand why."))
    return ap


def main():
    args = build_argparser().parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Mode-mutex: --clear-history, --prune-non-trading-days, --show-history,
    # and --ticker each replace the normal scan flow entirely. Combining
    # them previously meant the earliest-handled one wins and the rest are
    # silently ignored — confusing UX. Error out explicitly with a clear
    # message so the user knows to pick one.
    mode_flags = {
        "--clear-history": args.clear_history,
        "--prune-non-trading-days": args.prune_non_trading_days,
        "--show-history": args.show_history,
        "--ticker": bool(args.ticker),
    }
    active_modes = [name for name, set_ in mode_flags.items() if set_]
    if len(active_modes) > 1:
        print(
            f"❌ Conflicting modes: {', '.join(active_modes)}. These flags "
            f"replace the normal scan and are mutually exclusive. Re-run "
            f"with just one of them.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.clear_history:
        clear_history()
        print("history.csv cleared.")
        return

    if args.prune_non_trading_days:
        rows, run_ids = prune_non_trading_days()
        print(f"Pruned {rows} row(s) across {run_ids} run_id(s) "
              f"with non-trading-day ET dates.")
        return

    if args.show_history:
        show_history_summary(load_history())
        return

    if args.ticker:
        run_single_ticker_check(args)
        return

    universe = load_universe(args.min_market_cap, args.min_volume,
                             args.universe_count, args.refresh_universe)
    bars = fetch_bars(universe, history_months=DEFAULT_HISTORY_MONTHS)
    closes = extract_field(bars, universe, "Close")
    volumes = extract_field(bars, universe, "Volume")

    # SPY: separate fetch (it's not in the screener universe). We need it
    # for both regime detection and per-name RS slope during base.
    spy_bars = yf.download("SPY", period=f"{DEFAULT_HISTORY_MONTHS}mo",
                            interval="1d", auto_adjust=True, progress=False)
    if spy_bars is None or spy_bars.empty:
        print("SPY fetch failed — RS slope and regime banner will be unavailable.",
              file=sys.stderr)
        spy_close = pd.Series(dtype=float)
    else:
        spy_close = spy_bars["Close"]
        if isinstance(spy_close, pd.DataFrame):
            spy_close = spy_close.iloc[:, 0]
        spy_close = spy_close.dropna()

    regime = (compute_regime(spy_close, closes)
              if args.regime_gate != "off" and not spy_close.empty
              else None)
    rs_ratings = compute_rs_ratings(closes)

    picks = score_tickers(
        closes, volumes, spy_close, rs_ratings,
        min_base_weeks=args.min_base_weeks,
        max_base_weeks=args.max_base_weeks,
        max_base_width_pct=args.max_base_width,
        max_to_52w_high_pct=args.max_to_52w_high,
        min_rs_rating=args.min_rs_rating,
        min_base_score=args.min_base_score,
        smoothness_band_pct=args.smoothness_band_pct,
        verbose=args.verbose,
    )

    history = load_history()
    now = datetime.now(timezone.utc)
    run_id = make_run_id(now, allow_same_day=args.allow_same_day)
    picks = enrich_with_persistence(picks, history, run_id)

    if args.atr_stop_mult and args.atr_stop_mult > 0:
        top_tickers = [p["ticker"] for p in picks[: args.top_n]]
        atrs = compute_atrs(bars, top_tickers)
        picks = attach_atr_stops(picks, args.top_n, atrs, args.atr_stop_mult)
    if not args.no_sectors:
        top_tickers = [p["ticker"] for p in picks[: args.top_n]]
        sectors = refresh_sectors(top_tickers, load_sectors())
        picks = attach_sectors(picks, args.top_n, sectors)

    current_set = {p["ticker"] for p in picks[: args.top_n]}
    drops = dropouts_with_reason(
        history, current_set, run_id, args.top_n, closes,
        broke_out_pct=args.broke_out_pct,
        broke_down_pct=args.broke_down_pct,
    )

    # Recent breakouts: names with fresh breakouts in the last N days that
    # are NOT in the current top-N (they passed through the screen, broke
    # out, then aged out of "valid base" status). Gated on Trend Template
    # — which internally checks RS ≥ min_rs_rating as criterion #7 — so
    # we don't surface downtrending stocks that briefly poked a local
    # high (dead-cat bounces inside a downtrend that happen to clear a
    # 30-day range — those aren't "broke out of a base").
    recent_breakouts: list[dict] = []
    if args.recent_breakout_days and args.recent_breakout_days > 0:
        for ticker in universe:
            if ticker in current_set:
                continue  # already in main watchlist
            close_s = closes.get(ticker)
            vol_s = volumes.get(ticker)
            if close_s is None or vol_s is None:
                continue
            # TT also handles the RS gate internally — short-circuits on
            # missing/low RS via its `fail_rs_rating` branch.
            rs = rs_ratings.get(ticker)
            passes, _ = passes_trend_template(
                close_s, rs,
                max_dist_from_52w_high_pct=args.max_to_52w_high,
            )
            if not passes:
                continue
            rb = detect_recent_breakout(close_s, vol_s,
                                        lookback_days=args.recent_breakout_days)
            if rb is not None:
                rb["ticker"] = ticker
                recent_breakouts.append(rb)
        # Sort: working breakouts (positive follow-through) first by
        # follow-through, then failing breakouts (negative) — also by
        # follow-through. Reasoning: users care most about which recent
        # breakouts are *still working* and which already failed.
        recent_breakouts.sort(
            key=lambda r: -r.get("follow_through_pct", 0)
        )
        # Apply same-issuer dedup so GOOG + GOOGL don't both appear as
        # "two" recent breakouts when they're really one. Use the
        # follow_through_pct as the score for which sibling to keep (the
        # better-performing breakout is the more informative signal).
        # `base_score` is what _dedup_same_issuer reads, so we temporarily
        # alias it from follow_through_pct here, then strip after.
        for rb in recent_breakouts:
            rb["base_score"] = rb.get("follow_through_pct", 0.0)
        recent_breakouts, _ = _dedup_same_issuer(recent_breakouts)
        for rb in recent_breakouts:
            rb.pop("base_score", None)
            rb.pop("dedup_sibling", None)  # not surfaced in this section

    today_et = now.astimezone(MARKET_TZ).date()
    today_is_trading = is_nyse_trading_day(today_et)

    if not args.no_save:
        if not today_is_trading and not args.save_stale:
            print(f"Skipping history save: {today_et} is not an NYSE trading "
                  f"day (weekend or market holiday). Pass --save-stale to "
                  f"override.", file=sys.stderr)
        else:
            append_history(picks, run_id, now,
                           allow_same_day=args.allow_same_day)

    suppress_picks = (args.regime_gate == "strict"
                      and regime is not None
                      and not regime["risk_on"])

    if args.format == "json":
        sector_breakdown = (None if args.no_sectors
                            else compute_sector_breakdown(picks, args.top_n))
        # Pre-compute signal_display so JSON consumers see the same
        # asterisk-decorated glyph that markdown shows in the Sig column.
        # (Otherwise consumers have to combine `signal` + `anchor_mode`
        # themselves to reproduce the display string.)
        for p in picks[: args.top_n]:
            sd = p.get("signal", "—")
            if p.get("anchor_mode") == BASE_BREAKOUT_DETECT_BUFFER_DAYS:
                sd = sd + "*"
            p["signal_display"] = sd
        print(json.dumps({
            "run_id": run_id,
            "run_date": now.isoformat(),
            "params": vars(args),
            "universe_size": len(universe),
            "n_with_rs": len(rs_ratings),
            "n_passed": len(picks),
            "top_n": args.top_n,
            "regime": regime,
            "sector_breakdown": sector_breakdown,
            "picks_suppressed_by_gate": suppress_picks,
            "picks": [] if suppress_picks else picks[: args.top_n],
            "dropouts_since_last_run": [] if suppress_picks else drops,
            "recent_breakouts": [] if suppress_picks else recent_breakouts,
        }, indent=2, default=str))
        return

    n_prior = len(history["run_id"].drop_duplicates()) if not history.empty else 0
    print(f"# Base-breakout scan — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n**Params**: base={args.min_base_weeks:.0f}-{args.max_base_weeks:.0f}wks, "
          f"max_width={args.max_base_width:.0f}%, "
          f"max_to_52w_high={args.max_to_52w_high:.0f}%, "
          f"min_rs={args.min_rs_rating:.0f}, "
          f"min_score={args.min_base_score:.0f}")
    print(f"**Universe**: {len(universe)} tickers · "
          f"**Passed filter**: {len(picks)} · "
          f"**Prior runs**: {n_prior}")
    if args.regime_gate != "off":
        print(render_regime_banner(regime))
        if regime is not None and not regime["risk_on"]:
            if args.regime_gate == "strict":
                print("\n> ⚠️ **RISK-OFF + strict gate**: top-N suppressed. "
                      "History still saved so persistence data survives "
                      "the regime. Re-run with `--regime-gate warn` to see names.")
            else:
                print("\n> ⚠️ **RISK-OFF regime**: base setups in weak markets "
                      "have higher failure rates. Most bases break *down*, "
                      "not up. Treat below as 'who's holding up structurally', "
                      "not 'what to enter'.")

    if not suppress_picks and not args.no_sectors:
        sector_line = render_sector_breakdown(picks, args.top_n)
        if sector_line is not None:
            print(sector_line)

    if suppress_picks:
        return

    # Highlight today's breakouts in a separate section before the main
    # table — they're the most actionable / time-sensitive subset. Most
    # days this section is empty (a fresh breakout-on-volume across the
    # whole universe is unusual on any given day); the section is skipped
    # entirely in that case rather than printing a "(0)" header.
    breakouts = [p for p in picks[: args.top_n] if p.get("signal") == "🚀"]
    if breakouts:
        print(f"\n## 🚀 Breakouts today ({len(breakouts)})")
        for p in breakouts:
            vol_ratio = p.get("today_vol_ratio")
            vol_str = (f"{vol_ratio:.1f}× avg" if vol_ratio is not None
                       else "vol unknown")
            print(f"- **{p['ticker']}** — broke above ${p['pivot_price']:.2f} pivot "
                  f"(base {p['base_weeks']:.0f}wks, width {p['width_pct']:.1f}%, "
                  f"today vol {vol_str})")

    print(f"\n## Top {args.top_n}\n")
    print(render_table(picks, args.top_n))
    # Legend for the Sig `*` marker — printed only when at least one pick
    # is in breakout mode (so we don't add noise on quiet days). Pulls
    # the "N days" wording from the constant so the legend stays in sync
    # if BASE_BREAKOUT_DETECT_BUFFER_DAYS is ever changed.
    if any(p.get("anchor_mode") == BASE_BREAKOUT_DETECT_BUFFER_DAYS
           for p in picks[: args.top_n]):
        print(f"\n_`*` after Sig = base ended within the last "
              f"{BASE_BREAKOUT_DETECT_BUFFER_DAYS} days and today is a fresh "
              f"cross above prior range._")

    if drops:
        broke_out = [d for d in drops if d["reason"] == "broke_out"]
        broke_down = [d for d in drops if d["reason"] == "broke_down"]
        deduped = [d for d in drops if d["reason"] == "deduped"]
        faded = [d for d in drops if d["reason"] == "faded"]
        print(f"\n## Dropouts since last run ({len(drops)})")
        if broke_out:
            print(f"**Broke out** ({len(broke_out)}):")
            for d in broke_out:
                pct = ((d["current_close"] / d["prev_pivot"] - 1) * 100
                       if d.get("current_close") and d.get("prev_pivot") else None)
                pct_str = f" (+{pct:.1f}% above pivot)" if pct is not None else ""
                print(f"- **{d['ticker']}** (was #{d['prev_rank']}, "
                      f"pivot ${d['prev_pivot']:.2f}){pct_str}")
        if broke_down:
            print(f"**Broke down** ({len(broke_down)}):")
            for d in broke_down:
                print(f"- **{d['ticker']}** (was #{d['prev_rank']}, "
                      f"now ${d['current_close']:.2f} vs pivot ${d['prev_pivot']:.2f})")
        if deduped:
            # Surface deduped names explicitly so they don't get read as
            # real "faded" signals (the prior false signal).
            print(f"**Deduped** ({len(deduped)}, same-issuer rule):")
            for d in deduped:
                print(f"- **{d['ticker']}** (was #{d['prev_rank']}; "
                      f"kept {d['deduped_against']} instead)")
        if faded:
            print(f"**Faded out** ({len(faded)}):")
            for d in faded:
                print(f"- **{d['ticker']}** (was #{d['prev_rank']})")

    # Recent breakouts: triggered in the last N days, not in current
    # watchlist. Split into "working" (positive follow-through, still above
    # pivot) and "failed" (negative, gave back the breakout). Working list
    # is shown in full up to 15; failed truncated to the worst 5 (more is
    # noise — the user just needs the signal that some are breaking down).
    if recent_breakouts:
        days_w = args.recent_breakout_days
        working = [r for r in recent_breakouts if r.get("follow_through_pct", 0) >= 0]
        failed = [r for r in recent_breakouts if r.get("follow_through_pct", 0) < 0]
        print(f"\n## Recent breakouts (last {days_w} trading days, "
              f"{len(working)} working / {len(failed)} failed)")
        if working:
            print(f"**Working** (still above pivot):")
            for rb in working[:15]:
                # Wording: "+pct above pivot ↗" reads as one phrase with
                # the arrow at the end. The earlier "vs pivot ↗" had the
                # arrow mid-phrase which broke the reading.
                print(f"- **{rb['ticker']}** — broke ${rb['prior_pivot']:.2f} pivot "
                      f"on {rb['breakout_date']} ({rb['days_since_breakout']}d ago, "
                      f"{rb['breakout_vol_ratio']:.1f}× vol). Now: ${rb['current_close']:.2f} "
                      f"(+{rb['follow_through_pct']:.1f}% above pivot ↗)")
        if failed:
            # Show the worst 5 — these are the cleanest "broken breakout"
            # signals (price gave back the most after triggering). Sort
            # ascending by follow_through means most negative first.
            worst = sorted(failed, key=lambda r: r.get("follow_through_pct", 0))[:5]
            print(f"**Failed** (back below pivot, top {len(worst)} worst):")
            for rb in worst:
                print(f"- **{rb['ticker']}** — broke ${rb['prior_pivot']:.2f} "
                      f"on {rb['breakout_date']} ({rb['days_since_breakout']}d ago), "
                      f"now ${rb['current_close']:.2f} "
                      f"({rb['follow_through_pct']:+.1f}% below pivot ↘)")

    if not history.empty:
        new_entries = [p for p in picks[: args.top_n] if p.get("prev_rank") is None]
        if new_entries:
            print(f"\n## New setups ({len(new_entries)})")
            for p in new_entries:
                print(f"- **{p['ticker']}** at #{p['rank']} "
                      f"(score {p['base_score']:.0f}, base {p['base_weeks']:.0f}wks, "
                      f"{p['signal']})")
        min_streak = args.persistent_min_streak
        mature = [p for p in picks[: args.top_n] if p.get("streak", 1) >= min_streak]
        if mature:
            print(f"\n## Maturing bases (streak ≥ {min_streak} runs)")
            for p in sorted(mature, key=lambda x: -x["streak"]):
                print(f"- **{p['ticker']}** — streak {p['streak']}, "
                      f"first seen {p.get('first_seen', '—')}, "
                      f"now #{p['rank']}, score {p['base_score']:.0f}, "
                      f"{p['signal']}")


if __name__ == "__main__":
    main()
