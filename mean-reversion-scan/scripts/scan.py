"""
Mean-reversion scan: find Connors-style RSI(2) oversold setups inside confirmed
long-term uptrends, track persistence + outcome (win-rate) across runs.

Cadence-agnostic. One snapshot per US market day (America/New_York). Streak
counts consecutive scan-days a ticker has remained oversold (in MR context,
high streak is a warning, not a confirmation — see SKILL.md interpretation).

Self-contained — uses yfinance directly. Borrows the infrastructure (regime
gate, vol-collapse, sector cache, ATR stops, atomic-write history) from the
sister skills momentum-scan and base-breakout-scan; the MR-specific logic
(RSI(2) trigger, 5DMA target, frequency uniqueness, outcome resolution) is
new.

Usage:
  python scan.py                              # full run with default params
  python scan.py --rsi2-threshold 2           # only deep oversold
  python scan.py --ticker AAPL                # single-ticker diagnostic
  python scan.py --show-history               # win-rate stats, no new scan
  python scan.py --clear-history              # wipe history.csv (no prompt)
"""
import argparse
import json
import sys
import time
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
SCREENER_PAGE_SIZE = 250
SCREENER_PAGE_SLEEP_SEC = 0.2
SCREENER_MAX_PAGES = 20
SECTORS_TTL_DAYS = 30
MARKET_TZ = ZoneInfo("America/New_York")

# History schema. The two load-bearing additions vs sister skills are
# `target_price` and `stop_price` — outcome resolution depends on having
# the exact target/stop levels at signal time, not recomputing them later.
# `signal` is also persisted so a later analysis can split win-rate by tier.
HISTORY_COLS = [
    "run_id", "run_date", "ticker", "rank", "score_rank", "score",
    "rsi2", "dist_5dma_pct", "dist_50dma_pct", "dist_200dma_pct",
    "last_close", "target_price", "stop_price", "signal", "freq_60d",
]

# Data window: enough for 200DMA computation + 60-day frequency lookback +
# a comfortable buffer for outcome resolution on prior picks.
DATA_HISTORY_MONTHS = 13


class _NYSECalendar(AbstractHolidayCalendar):
    """NYSE-observed holidays. Same set as the sister skills — Pandas's
    USFederalHolidayCalendar isn't quite right (NYSE observes Good Friday but
    not Columbus / Veterans Day). Rare one-off closures aren't included."""
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
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty


# -----------------------------------------------------------------------
# Universe management — same approach as sister skills.
# -----------------------------------------------------------------------

def _screen_with_offset(query, page_size, offset):
    """Wrap yf.screen with a fallback for older yfinance without `offset`."""
    try:
        return yf.screen(query, sortField="intradaymarketcap",
                         sortAsc=False, size=page_size, offset=offset), True
    except TypeError as e:
        if "offset" not in str(e):
            raise
        return yf.screen(query, sortField="intradaymarketcap",
                         sortAsc=False, size=page_size), False


def refresh_universe(min_market_cap: float, min_volume: int,
                     count: int | None) -> list[str]:
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_market_cap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    tickers: list[str] = []
    seen: set[str] = set()
    offset = 0
    target = count
    started = time.monotonic()
    pages = 0
    truncated = False
    while target is None or len(tickers) < target:
        if pages >= SCREENER_MAX_PAGES:
            print(f"refresh_universe: hit SCREENER_MAX_PAGES={SCREENER_MAX_PAGES} "
                  f"backstop at {len(tickers)} tickers. Returning what we have.",
                  file=sys.stderr)
            truncated = True
            break
        if pages > 0:
            time.sleep(SCREENER_PAGE_SLEEP_SEC)
        page_size = (SCREENER_PAGE_SIZE if target is None
                     else min(SCREENER_PAGE_SIZE, target - len(tickers)))
        raw, supports_offset = _screen_with_offset(query, page_size, offset)
        pages += 1
        if target is None:
            reported_total = raw.get("total")
            if isinstance(reported_total, int) and reported_total > 0:
                target = reported_total
        quotes = raw.get("quotes") or []
        if not quotes:
            break
        added = 0
        for q in quotes:
            sym = q.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                tickers.append(sym)
                added += 1
        if not supports_offset or len(quotes) < page_size or added == 0:
            break
        offset += page_size
    if not tickers:
        raise RuntimeError(
            "Yahoo screener returned no results — possibly rate-limited."
        )
    tmp_path = UNIVERSE_FILE.with_suffix(".txt.tmp")
    tmp_path.write_text("\n".join(tickers))
    tmp_path.replace(UNIVERSE_FILE)
    status = "Refreshed universe (TRUNCATED)" if truncated else "Refreshed universe"
    print(f"{status}: {len(tickers)} tickers in {pages} request(s), "
          f"{time.monotonic() - started:.1f}s.", file=sys.stderr)
    return tickers


def load_universe(min_market_cap: float, min_volume: int, count: int | None,
                  refresh_mode) -> list[str]:
    if not UNIVERSE_FILE.exists() or refresh_mode is True:
        return refresh_universe(min_market_cap, min_volume, count)
    cached = [t for t in UNIVERSE_FILE.read_text().splitlines() if t.strip()]
    if refresh_mode is False:
        return cached
    if count is not None and len(cached) < count:
        print(f"Universe cache has {len(cached)} tickers but {count} requested, "
              f"refreshing...", file=sys.stderr)
        return refresh_universe(min_market_cap, min_volume, count)
    age_days = (datetime.now().timestamp() - UNIVERSE_FILE.stat().st_mtime) / 86400
    if age_days > UNIVERSE_TTL_DAYS:
        print(f"Universe cache stale ({age_days:.1f}d), refreshing...",
              file=sys.stderr)
        return refresh_universe(min_market_cap, min_volume, count)
    return cached


# -----------------------------------------------------------------------
# Data fetch.
# -----------------------------------------------------------------------

def fetch_bars(tickers: list[str], history_months: int = DATA_HISTORY_MONTHS):
    """Pull the full OHLCV MultiIndex frame from yfinance. We need ≥ 200
    trading days for the 200DMA gate, plus 60 for the frequency lookback,
    plus headroom for outcome resolution — ~13 months covers everything."""
    period = f"{history_months}mo"
    print(f"Fetching {len(tickers)} tickers, period={period}...", file=sys.stderr)
    return yf.download(
        tickers, period=period, interval="1d", auto_adjust=True,
        progress=False, threads=True, group_by="ticker",
    )


def extract_field(bars: pd.DataFrame, tickers: list[str],
                  field: str = "Close") -> dict[str, pd.Series]:
    """Pull one OHLCV column per ticker as a dict of Series. Drops tickers
    with insufficient history for the 200DMA + 60d frequency window."""
    out = {}
    min_len = 220  # 200DMA + ~20 buffer
    for t in tickers:
        try:
            if (t, field) not in bars.columns:
                continue
            s = bars[(t, field)].dropna()
            if len(s) >= min_len:
                out[t] = s
        except Exception:
            pass
    return out


# -----------------------------------------------------------------------
# Regime gauge — SPY 200DMA + slope, breadth.
# -----------------------------------------------------------------------

SPY_HISTORY_MONTHS = 13
MA200_SLOPE_LOOKBACK_DAYS = 20
MA200_SLOPE_RISK_ON_THRESHOLD_PCT = -0.05


def compute_regime(closes: dict[str, pd.Series]) -> dict | None:
    """SPY 200DMA + slope + cohort breadth. Same logic as sister skills."""
    try:
        spy_df = yf.download("SPY", period=f"{SPY_HISTORY_MONTHS}mo",
                             interval="1d", auto_adjust=True, progress=False)
    except Exception as e:
        print(f"SPY fetch failed: {e}", file=sys.stderr)
        return None
    if spy_df is None or spy_df.empty:
        return None
    spy_close = spy_df["Close"]
    if isinstance(spy_close, pd.DataFrame):
        spy_close = spy_close.iloc[:, 0]
    spy_close = spy_close.dropna()
    if len(spy_close) < 200 + MA200_SLOPE_LOOKBACK_DAYS:
        return None

    ma200_series = spy_close.rolling(200).mean()
    spy_last = float(spy_close.iloc[-1])
    spy_ma50 = float(spy_close.rolling(50).mean().iloc[-1])
    spy_ma200 = float(ma200_series.iloc[-1])
    spy_ma200_prev = float(ma200_series.iloc[-(MA200_SLOPE_LOOKBACK_DAYS + 1)])
    spy_ma200_slope_pct = (spy_ma200 / spy_ma200_prev - 1) * 100

    breadth_pct_200 = None
    if closes:
        # Build a frame from the closes dict for the breadth calc.
        prices = pd.DataFrame(closes).sort_index()
        if len(prices) >= 200:
            last = prices.iloc[-1]
            ma200_uni = prices.rolling(200).mean().iloc[-1]
            valid_200 = ma200_uni.notna() & last.notna()
            if valid_200.any():
                breadth_pct_200 = float(
                    (last[valid_200] > ma200_uni[valid_200]).mean() * 100
                )

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
        return "**Regime**: unavailable (SPY data fetch failed)"
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


# -----------------------------------------------------------------------
# Core MR logic: RSI(2), 5DMA distance, trend filter, score, Sig.
# -----------------------------------------------------------------------

RSI_PERIOD = 2  # the Connors signature; not exposed as a flag (changing it
# breaks the published win-rate calibration, which the persistence section
# relies on for meaningfulness)
SMA_TARGET_PERIOD = 5  # the canonical Connors exit target
SMA_MID_PERIOD = 50    # used for trend health component
SMA_LONG_PERIOD = 200  # the per-name uptrend gate
TREND_SLOPE_LOOKBACK = 20  # 200DMA slope window, matches regime gauge
FREQ_LOOKBACK_DAYS = 60  # how far back to count past triggers
ATR_PERIOD = 14


def compute_rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (EWMA with α = 1/period). Returns a series of RSI values
    aligned to `close`. The first `period` rows are NaN by construction."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    # Avoid div-by-zero: when avg_loss == 0 the all-gain case is RSI=100.
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def passes_trend_filter(close: pd.Series) -> tuple[bool, dict]:
    """Lite trend template: price > 200DMA, 200DMA slope > -0.05% over 20d,
    50DMA > 200DMA. Returns (passes, details_dict)."""
    if len(close) < SMA_LONG_PERIOD + TREND_SLOPE_LOOKBACK + 1:
        return False, {"reason": "insufficient_history"}
    last = float(close.iloc[-1])
    ma50 = float(close.rolling(SMA_MID_PERIOD).mean().iloc[-1])
    ma200_series = close.rolling(SMA_LONG_PERIOD).mean()
    ma200 = float(ma200_series.iloc[-1])
    ma200_prev = float(ma200_series.iloc[-(TREND_SLOPE_LOOKBACK + 1)])
    if pd.isna(ma200) or pd.isna(ma200_prev) or ma200_prev <= 0:
        return False, {"reason": "ma200_undefined"}
    slope_pct = (ma200 / ma200_prev - 1) * 100

    above_200 = last > ma200
    slope_ok = slope_pct > MA200_SLOPE_RISK_ON_THRESHOLD_PCT
    fifty_above = ma50 > ma200

    details = {
        "last": last,
        "ma50": ma50,
        "ma200": ma200,
        "ma200_slope_pct": slope_pct,
        "above_200dma": above_200,
        "slope_positive": slope_ok,
        "ma50_above_ma200": fifty_above,
    }
    return (above_200 and slope_ok and fifty_above), details


def count_past_triggers(close: pd.Series, threshold: float,
                       lookback: int = FREQ_LOOKBACK_DAYS) -> int:
    """Count how many days in the last `lookback` trading days had RSI(2)
    cross from above-threshold to below-threshold (a 'fresh trigger' event,
    not just 'sat below threshold for 5 days in a row'). The crossing
    definition prevents a stuck-oversold name from inflating its frequency
    count and being penalized for what is really one extended event."""
    if len(close) < lookback + RSI_PERIOD + 1:
        return 0
    rsi = compute_rsi_wilder(close, RSI_PERIOD)
    recent = rsi.tail(lookback + 1).dropna()
    if len(recent) < 2:
        return 0
    # Crossing: yesterday >= threshold AND today < threshold.
    triggers = ((recent.shift(1) >= threshold) & (recent < threshold))
    # Drop the first row (no prior comparison) and don't count today's
    # trigger if any (we'd be counting the current setup itself, which
    # would always make freq ≥ 1).
    return int(triggers.iloc[1:-1].sum()) if len(triggers) > 2 else 0


def score_mr(rsi2: float, dist_5dma_pct: float, dist_200dma_pct: float,
             freq_60d: int, rsi2_threshold: float) -> float:
    """Composite Reversion Score (0-100). All components are *variable* —
    none give constant points, since the trend filter is a hard gate before
    score_mr is called and any ma50/slope flags would be the same for every
    pick (constant points masquerade as differentiation).

    Components:
      - RSI(2) depth (40pts): linear, rsi2=0 → 40, rsi2=threshold → 0
        — the primary signal. Deeper oversold = higher score.
      - Pullback magnitude (30pts): linear, dist_5dma=-15% → 30,
        dist_5dma=0% → 0 — rewards real price dislocation, penalizes names
        already partially bounced.
      - Trend quality (15pts): linear, dist_200dma=0% → 0,
        dist_200dma=+30% → 15 — rewards bigger uptrend buffer ("MR inside a
        REAL uptrend, not a borderline one"). Capped at +30% above 200DMA.
      - Frequency uniqueness (15pts): linear, freq_60d=0 → 15,
        freq_60d=8 → 0 — penalizes noisy names where this signal fires
        often and means little.

    Realistic calibration after recalibration: a "textbook 🟢" pick lands
    around 50-65, "🔵 + low-freq + comfortable buffer" lands 70-85, and 90+
    is rare (RSI near 0, 5DMA gap > 10%, well above 200DMA, never-fired-
    before)."""
    depth = max(0.0, min(40.0, 40.0 * (1 - rsi2 / max(rsi2_threshold, 0.1))))
    pullback = max(0.0, min(30.0, 30.0 * (-dist_5dma_pct / 15.0)))
    trend_quality = max(0.0, min(15.0, 15.0 * (dist_200dma_pct / 30.0)))
    freq = max(0.0, min(15.0, 15.0 * (1 - freq_60d / 8.0)))
    return round(depth + pullback + trend_quality + freq, 1)


def classify_signal(rsi2: float, rsi2_threshold: float) -> str:
    """🟢 fresh trigger / 🔵 deep oversold / 🟡 setup forming / 🔴 too late."""
    if rsi2 is None:
        return "—"
    deep = rsi2_threshold / 2.0
    if rsi2 < deep:
        return "🔵"
    if rsi2 < rsi2_threshold:
        return "🟢"
    if rsi2 < rsi2_threshold * 2:
        return "🟡"
    if rsi2 > 50:
        return "🔴"
    return "🟡"  # in between threshold*2 and 50 — neutral watch


def score_tickers(closes: dict[str, pd.Series],
                  rsi2_threshold: float) -> list[dict]:
    """Run the full MR pipeline on every ticker in `closes`. Returns picks
    that pass the trend filter AND have RSI(2) < threshold * 2 (we include
    🟡 setup-forming names in the candidate list; final filter happens on
    Sig at render time). Stops are attached separately via
    attach_atr_stops_all so the methodology is uniform across all picks."""
    results = []
    for t, close in closes.items():
        passes, trend_details = passes_trend_filter(close)
        if not passes:
            continue
        rsi_series = compute_rsi_wilder(close, RSI_PERIOD)
        rsi2 = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None
        if rsi2 is None:
            continue
        # Only include candidates with RSI < threshold (the trigger zone).
        # 🟡 forming names (between threshold and 2×threshold) are included
        # in the "near-trigger" tier so the user sees what's about to fire.
        # 🔴 names (RSI > 50) are dropped from the candidate list — they're
        # already bouncing and not worth the user's attention.
        if rsi2 >= rsi2_threshold * 2:
            continue
        last = float(close.iloc[-1])
        sma5 = float(close.rolling(SMA_TARGET_PERIOD).mean().iloc[-1])
        if sma5 <= 0:
            continue
        dist_5dma_pct = (last / sma5 - 1) * 100
        dist_50dma_pct = (last / trend_details["ma50"] - 1) * 100
        dist_200dma_pct = (last / trend_details["ma200"] - 1) * 100
        freq = count_past_triggers(close, rsi2_threshold)
        score = score_mr(rsi2, dist_5dma_pct, dist_200dma_pct,
                         freq, rsi2_threshold)
        sig = classify_signal(rsi2, rsi2_threshold)

        # Target = 5DMA at signal time (fixed; outcome resolution checks
        # the current price against this static target). Stop is computed
        # later via attach_atr_stops_all using proper OHLC True Range —
        # we don't approximate it here so the persisted stop_price column
        # uses one consistent methodology across all ranks.
        target = sma5

        results.append({
            "ticker": t,
            "rsi2": round(rsi2, 2),
            "dist_5dma_pct": round(dist_5dma_pct, 2),
            "dist_50dma_pct": round(dist_50dma_pct, 2),
            "dist_200dma_pct": round(dist_200dma_pct, 2),
            "last_close": round(last, 2),
            "target_price": round(target, 2),
            "stop_price": None,  # filled in by attach_atr_stops_all
            "score": score,
            "signal": sig,
            "freq_60d": freq,
        })
    # Higher score first, then lower RSI as tiebreaker (more oversold wins).
    results.sort(key=lambda r: (-r["score"], r["rsi2"]))
    for i, r in enumerate(results, 1):
        r["rank"] = i
        r["score_rank"] = i
    return results


# -----------------------------------------------------------------------
# ATR — proper OHLC True Range, computed for ALL picks (not just top-N) so
# the stop_price column persisted to history.csv uses one consistent
# methodology regardless of where a pick ranks. This matters because
# outcome resolution reads stop_price back from history; methodology drift
# across ranks would produce subtly different win/loss boundaries.
# -----------------------------------------------------------------------

def compute_atrs_from_bars(bars: pd.DataFrame, tickers: list[str],
                           period: int = ATR_PERIOD) -> dict[str, dict]:
    """Proper N-day ATR using OHLC. Used to refine stop levels on the top-N
    after score_tickers has done its initial estimate. Same shape as the
    sister skills' compute_atrs."""
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


def attach_atr_stops_all(picks: list[dict], bars: pd.DataFrame,
                         atr_mult: float):
    """Compute proper OHLC ATR for every pick and attach stop_price.
    Operates on the full picks list (not just top-N) so history.csv's
    stop_price column has uniform methodology across all ranks. Mutates
    in place. No-op if atr_mult ≤ 0."""
    if atr_mult is None or atr_mult <= 0:
        return
    all_tickers = [p["ticker"] for p in picks]
    atrs = compute_atrs_from_bars(bars, all_tickers)
    for p in picks:
        info = atrs.get(p["ticker"])
        if info is None:
            # Leave stop_price as None — outcome resolver gracefully skips
            # picks without a stop (still resolves WON / EXPIRED, just no LOST).
            continue
        p["atr"] = round(info["atr"], 4)
        p["atr_pct"] = round(info["atr_pct"], 2)
        new_stop = info["last_close"] - atr_mult * info["atr"]
        p["stop_price"] = round(new_stop, 2)


# -----------------------------------------------------------------------
# Vol-collapse filter — same logic as sister skills.
# -----------------------------------------------------------------------

VOL_COLLAPSE_MIN_FIRST_VOL_PCT = 5.0
VOL_COLLAPSE_MIN_RETURNS_PER_HALF = 10
VOL_COLLAPSE_WINDOW_MONTHS = 3  # fixed for MR (we don't have a scoring window)


def compute_vol_halves(price_series: pd.Series) -> tuple[float, float] | None:
    daily = price_series.pct_change().dropna()
    n = len(daily)
    if n < 2 * VOL_COLLAPSE_MIN_RETURNS_PER_HALF:
        return None
    mid = n // 2
    v1 = float(daily.iloc[:mid].std() * np.sqrt(252) * 100)
    v2 = float(daily.iloc[mid:].std() * np.sqrt(252) * 100)
    return v1, v2


def filter_vol_collapse(picks: list[dict], closes: dict[str, pd.Series],
                        ratio_threshold: float) -> tuple[list[dict], list[dict]]:
    if ratio_threshold is None or ratio_threshold <= 0:
        return picks, []
    trading_days = VOL_COLLAPSE_WINDOW_MONTHS * 21
    kept, excluded = [], []
    for p in picks:
        t = p["ticker"]
        s = closes.get(t)
        if s is None:
            kept.append(p)
            continue
        window = s.tail(trading_days)
        halves = compute_vol_halves(window)
        if halves is None:
            kept.append(p)
            continue
        v1, v2 = halves
        if v1 < VOL_COLLAPSE_MIN_FIRST_VOL_PCT:
            kept.append(p)
            continue
        ratio = v2 / v1 if v1 > 0 else 1.0
        if ratio < ratio_threshold:
            p["vol_first_pct"] = round(v1, 1)
            p["vol_second_pct"] = round(v2, 1)
            p["vol_ratio"] = round(ratio, 3)
            p["pre_filter_rank"] = p.get("rank")
            p["rank"] = None
            excluded.append(p)
        else:
            kept.append(p)
    for i, p in enumerate(kept, 1):
        p["rank"] = i
    return kept, excluded


# -----------------------------------------------------------------------
# Sector tagging — same as sister skills.
# -----------------------------------------------------------------------

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
    fetched = failed = 0
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
        print(f"Sectors: {fetched} fetched, {failed} failed.", file=sys.stderr)
    save_sectors(cache)
    return cache


def attach_sectors(picks: list[dict], top_n: int, sectors: dict[str, dict]):
    for p in picks[:top_n]:
        info = sectors.get(p["ticker"]) or {}
        p["sector"] = info.get("sector") or ""
        p["industry"] = info.get("industry") or ""


def abbreviate_sector(sector: str) -> str:
    if not sector:
        return "—"
    return SECTOR_ABBREV.get(sector, sector[:10])


def render_sector_breakdown(picks: list[dict], top_n: int,
                            max_show: int = 5) -> str | None:
    rows = picks[:top_n]
    tagged = [p.get("sector", "") for p in rows if p.get("sector")]
    if not tagged:
        return None
    counts: dict[str, int] = {}
    for s in tagged:
        counts[s] = counts.get(s, 0) + 1
    sorted_sectors = sorted(counts.items(), key=lambda x: -x[1])
    shown = sorted_sectors[:max_show]
    remainder = sum(c for _, c in sorted_sectors[max_show:])
    parts = [f"{abbreviate_sector(s)} {c}" for s, c in shown]
    if remainder:
        parts.append(f"Other {remainder}")
    suffix = f" ({len(tagged)}/{len(rows)} tagged)" if len(tagged) < len(rows) else ""
    return f"**Sectors**: {' · '.join(parts)}{suffix}"


# -----------------------------------------------------------------------
# History I/O — atomic writes, ET-anchored dedup.
# -----------------------------------------------------------------------

def make_run_id(now: datetime, allow_same_day: bool = False) -> str:
    if allow_same_day:
        return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return now.astimezone(MARKET_TZ).strftime("%Y%m%d")


def clear_history():
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    if tmp.exists():
        tmp.unlink()


def prune_non_trading_days() -> tuple[int, int]:
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
    """At most one snapshot per ET calendar day. Empty picks = no-op (don't
    wipe a good prior snapshot for the same day). Atomic write."""
    if not picks:
        return
    new_rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "run_date": run_date.isoformat(),
                "ticker": p["ticker"],
                "rank": p["rank"],
                "score_rank": p.get("score_rank", p["rank"]),
                "score": p["score"],
                "rsi2": p["rsi2"],
                "dist_5dma_pct": p["dist_5dma_pct"],
                "dist_50dma_pct": p["dist_50dma_pct"],
                "dist_200dma_pct": p["dist_200dma_pct"],
                "last_close": p["last_close"],
                "target_price": p["target_price"],
                "stop_price": p["stop_price"],
                "signal": p["signal"],
                "freq_60d": p["freq_60d"],
            }
            for p in picks
        ],
        columns=HISTORY_COLS,
    )

    has_existing = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    existing = pd.read_csv(HISTORY_FILE) if has_existing else None

    if (existing is not None and not allow_same_day and not existing.empty):
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
    canonical = HISTORY_COLS + [c for c in combined.columns
                                if c not in HISTORY_COLS]
    combined = combined.reindex(columns=canonical)
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)


# -----------------------------------------------------------------------
# Persistence enrichment — streak / first_seen.
# -----------------------------------------------------------------------

def enrich_with_persistence(picks: list[dict], history: pd.DataFrame,
                            current_run_id: str):
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
    has_score_rank = "score_rank" in prior.columns
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
        prev_score_rank = prev["score_rank"] if has_score_rank else None
        if prev_score_rank is not None and not pd.isna(prev_score_rank):
            prev_for_delta = int(prev_score_rank)
        else:
            prev_for_delta = int(prev["rank"])
        cur_score_rank = p.get("score_rank", p["rank"])
        p["rank_delta"] = prev_for_delta - cur_score_rank
        ticker_runs = set(appearances["run_id"].tolist())
        streak = 1
        for rid in reversed(run_ids_ordered):
            if rid in ticker_runs:
                streak += 1
            else:
                break
        p["streak"] = streak
    return picks


# -----------------------------------------------------------------------
# Outcome resolution — the unique-to-MR piece.
# -----------------------------------------------------------------------

def resolve_outcomes(history: pd.DataFrame, bars: pd.DataFrame,
                     target_window_days: int) -> list[dict]:
    """For each prior signal in history, classify outcome by checking the
    price action in the days SINCE the signal:
      - WON if intraday high >= target_price within target_window_days
      - LOST if intraday low <= stop_price (when stop_price set) before target
      - EXPIRED if neither hit within window AND window has fully elapsed
      - OPEN if window hasn't fully elapsed yet (excluded from output)

    When both target and stop are hit on the same day, classify as WON
    optimistically — Connors's published methodology uses end-of-day prices,
    and a name that intraday-spiked through the target then sold off is still
    a real winning trade if a take-profit limit was set at signal time.
    Returns a list of resolved outcome dicts (excludes OPEN)."""
    if history.empty or "target_price" not in history.columns:
        return []
    # Don't try to resolve today's signals — they have 0 days of post-signal
    # data and would all show OPEN.
    today = pd.Timestamp.now(tz=timezone.utc).normalize()
    candidates = history[history["target_price"].notna()].copy()
    if candidates.empty:
        return []
    # Take only signals from the last (target_window_days × 3) calendar days
    # to keep the work bounded — older signals are already resolved historically
    # and don't need re-checking unless the user asks for the full --show-history.
    cutoff = today - pd.Timedelta(days=target_window_days * 3)
    candidates = candidates[candidates["run_date"] >= cutoff]

    outcomes = []
    for _, row in candidates.iterrows():
        ticker = row["ticker"]
        if (ticker, "Close") not in bars.columns:
            continue
        # Get the OHLC since the signal date (exclusive — the signal day's
        # close was the entry; we measure outcome on the days after).
        signal_date = row["run_date"]
        if pd.isna(signal_date):
            continue
        try:
            close_series = bars[(ticker, "Close")].dropna()
            high_series = bars[(ticker, "High")].dropna()
            low_series = bars[(ticker, "Low")].dropna()
        except Exception:
            continue
        # bars index is tz-naive; convert signal_date to a comparable form.
        sig_ts = pd.Timestamp(signal_date).tz_convert(None) if signal_date.tz is not None else pd.Timestamp(signal_date)
        # Align datetime unit to the index's unit — pandas 2.x rejects
        # searchsorted with a mismatched-precision Timestamp ("Cannot
        # losslessly convert units"). Daily bars at midnight have no sub-second
        # precision to lose, so as_unit with default round_ok=True is safe.
        try:
            idx_unit = high_series.index.unit
            sig_ts = sig_ts.as_unit(idx_unit)
        except (AttributeError, ValueError):
            sig_ts = pd.Timestamp(sig_ts.date())
        # Find the index of the signal date in the bars (closest trading day
        # at or before sig_ts; we use bars indexed AFTER the signal day).
        # Use searchsorted to find first bar strictly after the signal date.
        post_idx = high_series.index.searchsorted(sig_ts, side="right")
        post_high = high_series.iloc[post_idx:post_idx + target_window_days]
        post_low = low_series.iloc[post_idx:post_idx + target_window_days]
        if len(post_high) == 0:
            continue  # OPEN: no post-signal data yet
        target = float(row["target_price"])
        stop = float(row["stop_price"]) if pd.notna(row["stop_price"]) else None

        # Walk forward day-by-day — first hit wins, except WON > LOST on same day.
        outcome = None
        days_to_resolve = None
        resolve_price = None
        for d, (h, l) in enumerate(zip(post_high.values, post_low.values), 1):
            if h >= target:
                outcome = "WON"
                days_to_resolve = d
                resolve_price = target
                break
            if stop is not None and l <= stop:
                outcome = "LOST"
                days_to_resolve = d
                resolve_price = stop
                break

        if outcome is None:
            # Neither hit. EXPIRED only if the full window has elapsed.
            if len(post_high) >= target_window_days:
                last_close = float(close_series.iloc[post_idx + target_window_days - 1]) \
                    if len(close_series) > post_idx + target_window_days - 1 else None
                outcome = "EXPIRED"
                days_to_resolve = target_window_days
                resolve_price = last_close
            else:
                continue  # OPEN — skip from output

        entry = float(row["last_close"])
        result_pct = ((resolve_price / entry - 1) * 100) if resolve_price else None
        outcomes.append({
            "ticker": ticker,
            "signal_date": pd.Timestamp(signal_date).strftime("%Y-%m-%d"),
            "entry_price": entry,
            "target_price": target,
            "stop_price": stop,
            "outcome": outcome,
            "days_to_resolve": days_to_resolve,
            "resolve_price": round(resolve_price, 2) if resolve_price else None,
            "result_pct": round(result_pct, 2) if result_pct is not None else None,
            "signal": row.get("signal", "—"),
        })
    return outcomes


def compute_win_rate_stats(outcomes: list[dict]) -> dict | None:
    """Aggregate win-rate stats from resolved outcomes. Excludes OPEN
    (which doesn't appear in `outcomes` anyway) and EXPIRED from the rate
    numerator/denominator — only WON vs LOST are decisive trades. EXPIRED
    is shown separately as 'flat / no resolution'."""
    if not outcomes:
        return None
    won = [o for o in outcomes if o["outcome"] == "WON"]
    lost = [o for o in outcomes if o["outcome"] == "LOST"]
    expired = [o for o in outcomes if o["outcome"] == "EXPIRED"]
    n_decisive = len(won) + len(lost)
    if n_decisive == 0:
        return {
            "n_resolved": len(outcomes),
            "n_won": 0,
            "n_lost": 0,
            "n_expired": len(expired),
            "win_rate_pct": None,
            "avg_days_to_target": None,
        }
    win_rate_pct = (len(won) / n_decisive) * 100
    avg_days_won = (np.mean([o["days_to_resolve"] for o in won])
                    if won else None)
    return {
        "n_resolved": len(outcomes),
        "n_won": len(won),
        "n_lost": len(lost),
        "n_expired": len(expired),
        "win_rate_pct": round(win_rate_pct, 1),
        "avg_days_to_target": round(float(avg_days_won), 1) if avg_days_won is not None else None,
    }


# -----------------------------------------------------------------------
# Single-ticker mode.
# -----------------------------------------------------------------------

def run_single_ticker(args) -> dict:
    """Single-ticker diagnostic. Returns a result dict; renderers handle
    markdown vs JSON."""
    ticker = args.ticker.upper().strip()
    result: dict = {"ticker": ticker, "stages": {}}

    # Sector tag (cheap with cache).
    if not args.no_sectors:
        sectors_cache = load_sectors()
        sectors_cache = refresh_sectors([ticker], sectors_cache, max_workers=1)
        sec_info = sectors_cache.get(ticker, {})
        result["sector"] = sec_info.get("sector") or ""
        result["industry"] = sec_info.get("industry") or ""

    bars = fetch_bars([ticker])
    closes = extract_field(bars, [ticker], "Close")
    if ticker not in closes:
        result["error"] = (f"No data for {ticker}. Possibilities: "
                           f"(a) typo, (b) too few bars (need ≥ 220 trading days), "
                           f"(c) yfinance fetch failed.")
        return result

    close = closes[ticker]
    last = float(close.iloc[-1])
    result["last_close"] = round(last, 2)

    # Vol-collapse first — runs even if other stages fail, so the warning
    # always surfaces for buyout-target lookups.
    if args.vol_collapse_ratio > 0:
        window = close.tail(VOL_COLLAPSE_WINDOW_MONTHS * 21)
        halves = compute_vol_halves(window)
        if halves is not None:
            v1, v2 = halves
            if v1 >= VOL_COLLAPSE_MIN_FIRST_VOL_PCT:
                ratio = v2 / v1 if v1 > 0 else 1.0
                if ratio < args.vol_collapse_ratio:
                    result["vol_collapse_warning"] = {
                        "vol_first_pct": round(v1, 1),
                        "vol_second_pct": round(v2, 1),
                        "vol_ratio": round(ratio, 3),
                        "threshold": args.vol_collapse_ratio,
                    }

    # Stage 1: trend.
    passes, trend_details = passes_trend_filter(close)
    result["stages"]["trend"] = {"passes": passes, **trend_details}

    # Stage 2: short-term metrics (always computed — useful even on TT-fail).
    rsi_series = compute_rsi_wilder(close, RSI_PERIOD)
    rsi2 = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None
    sma5 = float(close.rolling(SMA_TARGET_PERIOD).mean().iloc[-1])
    dist_5dma_pct = (last / sma5 - 1) * 100 if sma5 > 0 else None
    result["stages"]["short_term"] = {
        "rsi2": round(rsi2, 2) if rsi2 is not None else None,
        "dist_5dma_pct": round(dist_5dma_pct, 2) if dist_5dma_pct is not None else None,
        "sma5": round(sma5, 2) if sma5 > 0 else None,
    }

    # Stage 3: score + signal — only meaningful if trend passes.
    if passes and rsi2 is not None and dist_5dma_pct is not None:
        dist_50 = (last / trend_details["ma50"] - 1) * 100
        dist_200 = (last / trend_details["ma200"] - 1) * 100
        freq = count_past_triggers(close, args.rsi2_threshold)
        score = score_mr(rsi2, dist_5dma_pct, dist_200, freq,
                         args.rsi2_threshold)
        sig = classify_signal(rsi2, args.rsi2_threshold)
        result["stages"]["score"] = {
            "score": score,
            "signal": sig,
            "freq_60d": freq,
            "dist_50dma_pct": round(dist_50, 2),
            "dist_200dma_pct": round(dist_200, 2),
        }

        # Stage 4: ATR stop + target.
        if args.atr_stop_mult and args.atr_stop_mult > 0:
            atrs = compute_atrs_from_bars(bars, [ticker])
            info = atrs.get(ticker)
            if info is not None:
                stop = last - args.atr_stop_mult * info["atr"]
                target = sma5
                rr = ((target - last) / (last - stop)) if (last - stop) > 0 else None
                result["stages"]["risk"] = {
                    "atr_14d": round(info["atr"], 4),
                    "atr_pct": round(info["atr_pct"], 2),
                    "stop_price": round(stop, 2),
                    "stop_pct_from_spot": round((stop / last - 1) * 100, 2),
                    "target_price": round(target, 2),
                    "target_pct_from_spot": round((target / last - 1) * 100, 2),
                    "risk_reward": round(rr, 2) if rr is not None else None,
                }

    # Stage 5: historical reliability — scan past 60 days for triggers and
    # resolve their outcomes. Pure-from-data, no history.csv required.
    historical = scan_ticker_historical_triggers(close, bars, ticker,
                                                  args.rsi2_threshold,
                                                  args.atr_stop_mult,
                                                  args.target_window_days)
    if historical:
        result["stages"]["historical"] = historical

    return result


def scan_ticker_historical_triggers(close: pd.Series, bars: pd.DataFrame,
                                    ticker: str, rsi2_threshold: float,
                                    atr_mult: float,
                                    target_window_days: int) -> dict | None:
    """Scan the last 60 trading days of `close` for past RSI(2) crossings
    below threshold and resolve each outcome from subsequent price action.
    Returns a stats dict or None if no triggers found."""
    if len(close) < FREQ_LOOKBACK_DAYS + RSI_PERIOD + 1:
        return None
    rsi = compute_rsi_wilder(close, RSI_PERIOD)
    sma5 = close.rolling(SMA_TARGET_PERIOD).mean()
    # Crossings in the lookback window, but exclude the most recent
    # `target_window_days` rows so all triggers have time to resolve.
    lookback_start = -FREQ_LOOKBACK_DAYS - target_window_days
    lookback_end = -target_window_days
    if lookback_end == 0:
        recent_rsi = rsi.iloc[lookback_start:]
    else:
        recent_rsi = rsi.iloc[lookback_start:lookback_end]
    if len(recent_rsi) < 2:
        return None
    triggers_mask = ((recent_rsi.shift(1) >= rsi2_threshold)
                     & (recent_rsi < rsi2_threshold))
    trigger_dates = recent_rsi.index[triggers_mask.fillna(False)]
    if len(trigger_dates) == 0:
        return {"n_triggers": 0, "n_resolved": 0, "win_rate_pct": None}

    # Get high/low — needed for outcome resolution against intraday hits.
    if (ticker, "High") not in bars.columns:
        return None
    high = bars[(ticker, "High")].dropna()
    low = bars[(ticker, "Low")].dropna()

    # ATR for stop sizing.
    atrs = compute_atrs_from_bars(bars, [ticker])
    atr_info = atrs.get(ticker)
    if atr_info is None and atr_mult and atr_mult > 0:
        return None  # can't compute stop, skip historical
    atr_val = atr_info["atr"] if atr_info else 0.0

    won = lost = expired = 0
    days_to_target = []
    last_trigger_date = None
    for sig_date in trigger_dates:
        last_trigger_date = sig_date
        try:
            entry_price = float(close.loc[sig_date])
            target = float(sma5.loc[sig_date])
            stop = entry_price - atr_mult * atr_val if atr_mult > 0 else None
        except (KeyError, ValueError):
            continue
        post_idx = high.index.searchsorted(sig_date, side="right")
        post_high = high.iloc[post_idx:post_idx + target_window_days]
        post_low = low.iloc[post_idx:post_idx + target_window_days]
        if len(post_high) < target_window_days:
            continue  # not enough bars to fully resolve
        outcome = None
        for d, (h, l) in enumerate(zip(post_high.values, post_low.values), 1):
            if h >= target:
                outcome = "WON"
                days_to_target.append(d)
                break
            if stop is not None and l <= stop:
                outcome = "LOST"
                break
        if outcome == "WON":
            won += 1
        elif outcome == "LOST":
            lost += 1
        else:
            expired += 1

    n_triggers = len(trigger_dates)
    n_resolved = won + lost + expired
    n_decisive = won + lost
    win_rate = (won / n_decisive * 100) if n_decisive > 0 else None
    return {
        "n_triggers": n_triggers,
        "n_resolved": n_resolved,
        "n_won": won,
        "n_lost": lost,
        "n_expired": expired,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "avg_days_to_target": (round(float(np.mean(days_to_target)), 1)
                               if days_to_target else None),
        "last_trigger": (pd.Timestamp(last_trigger_date).strftime("%Y-%m-%d")
                         if last_trigger_date is not None else None),
    }


# -----------------------------------------------------------------------
# Render: markdown table + sections.
# -----------------------------------------------------------------------

def render_table(picks: list[dict], top_n: int) -> str:
    rows = picks[:top_n]
    if not rows:
        return "(no picks passed the filter)"
    show_sector = any(p.get("sector") for p in rows)
    show_stop = any(p.get("stop_price") is not None for p in rows)

    headers = ["#", "Ticker"]
    if show_sector:
        headers.append("Sector")
    headers += ["RSI(2)", "5DMA%", "50DMA%", "200DMA%", "Score", "Sig",
                "Streak", "Freq60d"]
    if show_stop:
        headers += ["Stop", "Target"]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for p in rows:
        row = [str(p["rank"]), f"**{p['ticker']}**"]
        if show_sector:
            row.append(abbreviate_sector(p.get("sector", "")))
        row += [
            f"{p['rsi2']:.1f}",
            f"{p['dist_5dma_pct']:+.1f}",
            f"{p['dist_50dma_pct']:+.1f}",
            f"{p['dist_200dma_pct']:+.1f}",
            f"{p['score']:.0f}",
            p.get("signal", "—"),
            str(p.get("streak", 1)),
            str(p.get("freq_60d", 0)),
        ]
        if show_stop:
            sp = p.get("stop_price")
            tp = p.get("target_price")
            last = p.get("last_close")
            if sp is not None and last:
                spct = (sp / last - 1) * 100
                row.append(f"${sp:.2f} ({spct:+.1f}%)")
            else:
                row.append("—")
            if tp is not None and last:
                tpct = (tp / last - 1) * 100
                row.append(f"${tp:.2f} ({tpct:+.1f}%)")
            else:
                row.append("—")
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def render_resolved_section(outcomes: list[dict],
                            target_window_days: int) -> list[str]:
    """Render the recently-resolved-picks section. Returns lines (or empty
    list if nothing to show). Filters to outcomes whose signal_date is
    within the last `target_window_days × 2` calendar days — that's broad
    enough to catch any signal whose `target_window_days` resolution
    window is still in the recent past, while staying focused on 'recent'
    enough to be actionable. (resolve_outcomes itself looks back ×3 to
    avoid missing edge-of-window resolutions; we re-filter tighter here
    for display.)"""
    if not outcomes:
        return []
    today = pd.Timestamp.now(tz=timezone.utc).normalize().tz_convert(None)
    cutoff = today - pd.Timedelta(days=target_window_days * 2)
    recent = [o for o in outcomes
              if pd.Timestamp(o["signal_date"]) >= cutoff]
    if not recent:
        return []
    won = [o for o in recent if o["outcome"] == "WON"]
    lost = [o for o in recent if o["outcome"] == "LOST"]
    expired = [o for o in recent if o["outcome"] == "EXPIRED"]
    out = [f"\n## Recently resolved (last {target_window_days * 2} days, {len(recent)} picks)"]
    if won:
        out.append(f"**Won** ({len(won)}):")
        for o in sorted(won, key=lambda x: x["days_to_resolve"]):
            out.append(f"- **{o['ticker']}** — signaled {o['signal_date']} "
                       f"@ ${o['entry_price']:.2f}, hit target "
                       f"${o['target_price']:.2f} in "
                       f"{o['days_to_resolve']} day(s) "
                       f"({o['result_pct']:+.1f}%)")
    if lost:
        out.append(f"**Lost** ({len(lost)}):")
        for o in sorted(lost, key=lambda x: x["days_to_resolve"]):
            stop = o.get("stop_price")
            stop_str = f"${stop:.2f}" if stop is not None else "stop"
            out.append(f"- **{o['ticker']}** — signaled {o['signal_date']} "
                       f"@ ${o['entry_price']:.2f}, stopped at "
                       f"{stop_str} ({o['result_pct']:+.1f}%) "
                       f"in {o['days_to_resolve']} day(s)")
    if expired:
        out.append(f"**Expired** ({len(expired)}):")
        for o in expired:
            out.append(f"- **{o['ticker']}** — signaled {o['signal_date']}, "
                       f"drifted {o['result_pct']:+.1f}% over "
                       f"{o['days_to_resolve']} days, neither target nor "
                       f"stop hit")
    return out


def render_stuck_section(picks: list[dict], top_n: int,
                         min_streak: int, history: pd.DataFrame) -> list[str]:
    """Stuck-oversold names: high streak in MR is a yellow flag, not green.
    The bounce hasn't materialized after multiple runs — usually means
    something structural (broken trend, news, sector pressure) is at work."""
    sticky = [p for p in picks[:top_n] if p.get("streak", 1) >= min_streak]
    if not sticky:
        return []
    out = [f"\n## Stuck oversold (streak ≥ {min_streak} runs — REVIEW for structural break)"]
    for p in sorted(sticky, key=lambda x: -x["streak"]):
        # Pull the RSI history for this ticker so we can show the trajectory.
        history_for_t = (history[history["ticker"] == p["ticker"]]
                         .sort_values("run_date").tail(p["streak"]))
        rsi_traj = " → ".join(f"{float(r['rsi2']):.1f}"
                              for _, r in history_for_t.iterrows()
                              if pd.notna(r['rsi2']))
        out.append(f"- **{p['ticker']}** — streak {p['streak']}, "
                   f"first seen {p.get('first_seen', '—')}, "
                   f"RSI(2) trajectory: {rsi_traj}")
        out.append(f"  ⚠️ Bounce hasn't materialized in {p['streak']} session(s). "
                   f"Possible: real breakdown, missed news catalyst, sector-wide pressure.")
    return out


# -----------------------------------------------------------------------
# Show-history mode.
# -----------------------------------------------------------------------

def show_history_summary(history: pd.DataFrame, bars: pd.DataFrame | None,
                         target_window_days: int):
    if history.empty:
        print("History is empty.")
        return
    runs = history.sort_values("run_date")["run_id"].drop_duplicates().tolist()
    print(f"Total runs: {len(runs)}")
    print(f"Date range: {history['run_date'].min().date()} → "
          f"{history['run_date'].max().date()}")
    counts = history.groupby("ticker").size().sort_values(ascending=False)

    if bars is not None and not bars.empty:
        outcomes = resolve_outcomes(history, bars, target_window_days)
        stats = compute_win_rate_stats(outcomes)
        if stats:
            wr = stats["win_rate_pct"]
            rate_seg = f"{wr:.0f}%" if wr is not None else "n/a"
            print(f"\nWin rate (last ~{target_window_days * 3} days): "
                  f"{rate_seg} "
                  f"({stats['n_won']}W / {stats['n_lost']}L, "
                  f"{stats['n_expired']} expired)")
            if stats["avg_days_to_target"] is not None:
                print(f"Avg days to target (winners): "
                      f"{stats['avg_days_to_target']:.1f}")

    print(f"\nTop 20 most-frequent tickers across all runs:")
    for t, c in counts.head(20).items():
        print(f"  {t:<8} {c} appearances")


# -----------------------------------------------------------------------
# CLI.
# -----------------------------------------------------------------------

def _positive_int(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {s!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {v}")
    return v


def _vol_collapse_ratio(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {s!r}")
    if v > 1.0:
        raise argparse.ArgumentTypeError(
            f"must be ≤ 1.0; got {v}. Pass 0 or negative to disable.")
    return v


def _rsi_threshold(s: str) -> float:
    """argparse type for --rsi2-threshold. RSI is bounded [0, 100], so any
    threshold outside (0, 100) is meaningless. Reject 0 (would never fire)
    and ≥ 100 (would always fire); 50+ is silly but allowed since users may
    want to experiment with very loose thresholds."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {s!r}")
    if v <= 0 or v >= 100:
        raise argparse.ArgumentTypeError(
            f"must be in (0, 100); got {v}. RSI is bounded [0, 100], so "
            f"0 would never fire and ≥ 100 would always fire.")
    return v


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rsi2-threshold", type=_rsi_threshold, default=5.0,
                    help="RSI(2) ceiling for the 🟢 fresh-trigger signal "
                         "(must be in (0, 100); typical 2-10).")
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--min-market-cap", type=float, default=5e9)
    ap.add_argument("--min-volume", type=int, default=1_000_000)
    ap.add_argument("--universe-count", type=_positive_int, default=None)
    ap.add_argument("--refresh-universe", dest="refresh_universe",
                    action="store_const", const=True, default=None)
    ap.add_argument("--no-refresh-universe", dest="refresh_universe",
                    action="store_const", const=False)
    ap.add_argument("--ticker", type=str, default=None,
                    help="Single-ticker diagnostic mode.")
    ap.add_argument("--show-history", action="store_true")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--prune-non-trading-days", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--save-stale", action="store_true")
    ap.add_argument("--allow-same-day", action="store_true")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--regime-gate", choices=["off", "warn", "strict"],
                    default="warn")
    ap.add_argument("--atr-stop-mult", type=float, default=2.5)
    ap.add_argument("--no-sectors", action="store_true")
    ap.add_argument("--vol-collapse-ratio", type=_vol_collapse_ratio,
                    default=0.2)
    ap.add_argument("--persistent-min-streak", type=int, default=3)
    ap.add_argument("--target-window-days", type=_positive_int, default=5,
                    help="Days within which target must be hit for WON "
                         "(must be a positive integer; typical 3-10).")
    return ap


def main():
    args = build_argparser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear_history:
        clear_history()
        print("history.csv cleared.")
        return

    if args.prune_non_trading_days:
        rows, run_ids = prune_non_trading_days()
        print(f"Pruned {rows} row(s) across {run_ids} run_id(s).")
        return

    if args.show_history:
        # For show-history we still need price data to resolve outcomes, but
        # only for tickers that actually appear in history.
        history = load_history()
        bars = None
        if not history.empty:
            tickers_in_history = sorted(history["ticker"].unique().tolist())
            try:
                bars = fetch_bars(tickers_in_history)
            except Exception as e:
                print(f"Warning: couldn't fetch bars for outcome resolution: {e}",
                      file=sys.stderr)
        show_history_summary(history, bars, args.target_window_days)
        return

    if args.ticker:
        result = run_single_ticker(args)
        if args.format == "json":
            print(json.dumps(result, indent=2, default=str))
        else:
            render_single_ticker_markdown(result, args)
        return

    # Standard scan path.
    universe = load_universe(args.min_market_cap, args.min_volume,
                             args.universe_count, args.refresh_universe)
    bars = fetch_bars(universe)
    closes = extract_field(bars, universe, "Close")
    regime = compute_regime(closes) if args.regime_gate != "off" else None

    picks = score_tickers(closes, args.rsi2_threshold)
    picks, excluded_vol_collapse = filter_vol_collapse(
        picks, closes, args.vol_collapse_ratio,
    )

    # Attach ATR stops to ALL picks (not just top-N) before history save —
    # ensures stop_price column uses uniform OHLC methodology across ranks.
    attach_atr_stops_all(picks, bars, args.atr_stop_mult)

    history = load_history()
    now = datetime.now(timezone.utc)
    run_id = make_run_id(now, allow_same_day=args.allow_same_day)
    picks = enrich_with_persistence(picks, history, run_id)

    if not args.no_sectors:
        top_tickers = [p["ticker"] for p in picks[:args.top_n]]
        sectors = refresh_sectors(top_tickers, load_sectors())
        attach_sectors(picks, args.top_n, sectors)

    # Outcome resolution on prior signals (read-only against history + bars).
    outcomes = resolve_outcomes(history, bars, args.target_window_days)
    win_stats = compute_win_rate_stats(outcomes)

    # Non-trading-day guard for history save.
    today_et = now.astimezone(MARKET_TZ).date()
    today_is_trading = is_nyse_trading_day(today_et)
    if not args.no_save:
        if not today_is_trading and not args.save_stale:
            print(f"Skipping history save: {today_et} is not an NYSE trading "
                  f"day. Pass --save-stale to override.", file=sys.stderr)
        else:
            append_history(picks, run_id, now,
                           allow_same_day=args.allow_same_day)

    suppress_picks = (args.regime_gate == "strict"
                      and regime is not None
                      and not regime["risk_on"])

    if args.format == "json":
        print(json.dumps({
            "run_id": run_id,
            "run_date": now.isoformat(),
            "params": vars(args),
            "universe_size": len(universe),
            "passed_filter": len(picks),
            "top_n": args.top_n,
            "regime": regime,
            "win_stats": win_stats,
            "picks_suppressed_by_gate": suppress_picks,
            "picks": [] if suppress_picks else picks[:args.top_n],
            "outcomes": outcomes,
            "excluded_vol_collapse": excluded_vol_collapse,
        }, indent=2, default=str))
        return

    # Markdown render.
    n_prior = len(history["run_id"].drop_duplicates()) if not history.empty else 0
    print(f"# Mean-reversion scan — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n**Params**: rsi2_threshold={args.rsi2_threshold}, "
          f"target_window={args.target_window_days}d, "
          f"mcap>{args.min_market_cap:.0e}")
    if args.vol_collapse_ratio > 0:
        n_excl = len(excluded_vol_collapse)
        if n_excl == 0:
            passed = f"**Passed filter**: {len(picks)} (vol-collapse: 0 excluded)"
        else:
            passed = (f"**Passed filter**: {len(picks)} "
                      f"(vol-collapse: {n_excl} excluded of {len(picks) + n_excl})")
    else:
        passed = f"**Passed filter**: {len(picks)}"
    print(f"**Universe**: {len(universe)} tickers · {passed} · "
          f"**Prior runs**: {n_prior}")
    if args.regime_gate != "off":
        print(render_regime_banner(regime))
        if regime is not None and not regime["risk_on"]:
            if args.regime_gate == "strict":
                print("\n> ⚠️ **RISK-OFF + strict gate**: top-N suppressed. "
                      "Mean-reversion longs in confirmed downtrends are the "
                      "canonical disaster setup. History still saved.")
            else:
                print("\n> ⚠️ **RISK-OFF regime**: mean-reversion longs are "
                      "particularly dangerous here — every oversold bounce can "
                      "be followed by more selling (2008 H2, 2020 March, "
                      "2022 H1). Treat as paper-trade only.")

    if win_stats:
        n_exp = win_stats["n_expired"]
        expired_seg = f", {n_exp} expired" if n_exp else ""
        avg_days = win_stats.get("avg_days_to_target")
        avg_seg = (f" · avg days to target: {avg_days:.1f}"
                   if avg_days else "")
        wr = win_stats["win_rate_pct"]
        # Show stats even when all resolved are EXPIRED (win_rate_pct=None) —
        # "0W/0L, 5 expired" is itself informative (the system is firing but
        # bounces aren't materializing in the target window). Hiding that
        # would be silently dropping signal.
        rate_seg = f"{wr:.0f}%" if wr is not None else "n/a"
        print(f"**Win rate** (last ~{args.target_window_days * 3} days, "
              f"{win_stats['n_resolved']} resolved): "
              f"{rate_seg} "
              f"({win_stats['n_won']}W / {win_stats['n_lost']}L"
              f"{expired_seg}){avg_seg}")

    if not suppress_picks:
        sector_line = render_sector_breakdown(picks, args.top_n)
        if sector_line:
            print(sector_line)

    if excluded_vol_collapse:
        print(f"\n## Excluded by vol-collapse filter ({len(excluded_vol_collapse)})")
        ratio_pct_str = f"{args.vol_collapse_ratio * 100:g}%"
        print(f"_2nd-half realized vol < {ratio_pct_str} of 1st-half — likely "
              f"acquisition / lock-in, not tradable mean reversion._")
        for p in sorted(excluded_vol_collapse, key=lambda x: x["vol_ratio"]):
            print(f"- **{p['ticker']}** (RSI(2)={p['rsi2']:.1f}, "
                  f"vol {p['vol_first_pct']:.1f}% → "
                  f"{p['vol_second_pct']:.1f}%, "
                  f"ratio {p['vol_ratio']:.2f})")

    if suppress_picks:
        return

    print(f"\n## Top {min(args.top_n, len(picks))}\n")
    print(render_table(picks, args.top_n))

    # Recently-resolved + stuck-oversold sections.
    for line in render_resolved_section(outcomes, args.target_window_days):
        print(line)
    for line in render_stuck_section(picks, args.top_n,
                                      args.persistent_min_streak, history):
        print(line)


def render_single_ticker_markdown(result: dict, args):
    ticker = result["ticker"]
    sector = result.get("sector") or ""
    industry = result.get("industry") or ""
    if sector or industry:
        tag_parts = [t for t in (sector, industry) if t]
        print(f"# Single-ticker check: {ticker} _({' / '.join(tag_parts)})_")
    else:
        print(f"# Single-ticker check: {ticker}")

    if "error" in result:
        print(f"\n❌ {result['error']}")
        return

    # Vol-collapse warning at the TOP if triggered.
    if "vol_collapse_warning" in result:
        w = result["vol_collapse_warning"]
        print(f"\n> ⚠️ **VOL-COLLAPSE WARNING**: 2nd-half annualized vol = "
              f"{w['vol_second_pct']:.1f}% (1st-half was "
              f"{w['vol_first_pct']:.1f}%); ratio = {w['vol_ratio']:.3f}, "
              f"below the {w['threshold']:.2f} threshold.")
        print(f">\n> This is the canonical signature of a stock locked at an "
              f"acquisition cash offer price. RSI(2) low here is meaningless "
              f"as a mean-reversion signal — the stock is pinned, not panicking.")
        print(f">\n> Verify via `yfinance` skill: `sec_filings --type "
              f"PREM14A,DEFM14A` is the smoking gun for a pending merger.")

    last = result.get("last_close")
    print(f"\n**Last close**: ${last:.2f}" if last else "")

    # Stage 1: trend.
    trend = result["stages"].get("trend", {})
    if trend.get("passes"):
        print(f"\n## Stage 1: Long-term trend filter")
        print(f"✅ **PASS** — Price > 200DMA, 200DMA rising, 50DMA > 200DMA")
        print(f"- Price ${trend['last']:.2f} vs 200DMA ${trend['ma200']:.2f} "
              f"({(trend['last']/trend['ma200']-1)*100:+.1f}%)")
        print(f"- 200DMA slope (20d): {trend['ma200_slope_pct']:+.2f}%")
        print(f"- 50DMA ${trend['ma50']:.2f} "
              f"{'>' if trend['ma50_above_ma200'] else '<'} 200DMA")
    else:
        print(f"\n## Stage 1: Long-term trend filter")
        print(f"❌ **FAIL** — name does not pass the per-ticker uptrend gate")
        if trend.get("reason"):
            print(f"- Reason: {trend['reason']}")
        else:
            print(f"- Above 200DMA: {trend.get('above_200dma')}")
            print(f"- Slope positive: {trend.get('slope_positive')}")
            print(f"- 50DMA > 200DMA: {trend.get('ma50_above_ma200')}")
        print(f"\n→ Skipping subsequent stages — the system only trades MR "
              f"longs inside confirmed uptrends.")
        return

    # Stage 2: short-term metrics.
    st = result["stages"].get("short_term", {})
    print(f"\n## Stage 2: Short-term oversold metrics")
    print(f"- RSI(2): {st.get('rsi2')}")
    print(f"- Distance from 5DMA: {st.get('dist_5dma_pct'):+.2f}%")
    print(f"- 5DMA: ${st.get('sma5'):.2f}")

    # Stage 3: score + signal.
    sc = result["stages"].get("score")
    if sc:
        print(f"\n## Stage 3: Reversion Score & Signal")
        print(f"- **Score: {sc['score']:.0f}/100**")
        print(f"- **Signal: {sc['signal']}**")
        print(f"- 60-day trigger frequency: {sc['freq_60d']}")
        print(f"- Distance from 50DMA: {sc['dist_50dma_pct']:+.2f}%")
        print(f"- Distance from 200DMA: {sc['dist_200dma_pct']:+.2f}%")

    # Stage 4: risk levels.
    risk = result["stages"].get("risk")
    if risk:
        print(f"\n## Stage 4: Risk levels (ATR-based, {args.atr_stop_mult}×)")
        print(f"- 14-day ATR: ${risk['atr_14d']:.4f} ({risk['atr_pct']:.2f}% of price)")
        print(f"- **Stop**: ${risk['stop_price']:.2f} "
              f"({risk['stop_pct_from_spot']:+.1f}% from spot)")
        print(f"- **Target (5DMA)**: ${risk['target_price']:.2f} "
              f"({risk['target_pct_from_spot']:+.1f}% from spot)")
        if risk["risk_reward"]:
            print(f"- Risk/reward at current price: {risk['risk_reward']:.2f}")

    # Stage 5: historical reliability.
    hist = result["stages"].get("historical")
    if hist:
        print(f"\n## Stage 5: Historical reliability (last 60 trading days)")
        print(f"- Triggers: {hist['n_triggers']}"
              + (f" (last on {hist['last_trigger']})"
                 if hist.get('last_trigger') else ""))
        if hist.get("n_resolved", 0) > 0:
            print(f"- Resolved: {hist['n_resolved']} — "
                  f"{hist['n_won']} won"
                  + (f" (avg {hist['avg_days_to_target']:.1f} days)"
                     if hist.get("avg_days_to_target") else "")
                  + f", {hist['n_lost']} lost, {hist['n_expired']} expired")
            n = hist['n_won'] + hist['n_lost']
            if hist["win_rate_pct"] is not None:
                caveat = (" — small sample, treat as directional only"
                          if n < 5 else "")
                print(f"- Win rate: {hist['win_rate_pct']:.0f}% "
                      f"(n={n}{caveat})")
            else:
                # All-EXPIRED case — informative even without W/L. Bounce
                # consistently failing to materialize within the window is
                # itself a signal about this ticker's behavior.
                print(f"- Win rate: n/a (no decisive W/L; all "
                      f"{hist['n_expired']} prior triggers expired without "
                      f"target or stop hit)")
        else:
            print(f"- No prior triggers in the lookback window.")


if __name__ == "__main__":
    main()
