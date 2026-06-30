"""
Momentum scan: find US equities in smooth uptrends, track persistence across runs.

Cadence-agnostic: every invocation logs a timestamp. "Streak" is consecutive
runs (interpret the granularity from the actual dates).

Self-contained — uses yfinance directly, no cross-skill dependencies.

Usage:
  python scan.py                        # full run with default params
  python scan.py --window-months 6      # smoother, slower-moving leaders
  python scan.py --top-n 50             # show more names
  python scan.py --no-refresh-universe  # use cached universe regardless of TTL
  python scan.py --show-history         # dump history.csv summary
  python scan.py --clear-history        # wipe history.csv (no confirmation)
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
SCREENER_PAGE_SIZE = 250  # Yahoo's hard cap per request — yf.screen raises ValueError above this
SCREENER_PAGE_SLEEP_SEC = 0.2  # small gap between pages so a 5-page sweep doesn't trip Yahoo throttling
SCREENER_MAX_PAGES = 20  # absolute backstop (~5000 tickers) for the case where Yahoo's `total` field is missing and only the per-page guards stop the loop. 5000 is already 5× the realistic US large-cap match count, so hitting this is a strong signal something is wrong upstream.
SECTORS_TTL_DAYS = 30  # sectors rarely change — long TTL keeps repeat runs fast
MARKET_TZ = ZoneInfo("America/New_York")  # one snapshot per US market day

HISTORY_COLS = [
    "run_id", "run_date", "ticker", "rank", "score_rank", "score",
    "return_pct", "max_dd_pct", "ann_vol_pct", "from_high_pct",
]
# `rank` is the display position the user sees (post-vol-collapse, contiguous
# 1..N). `score_rank` is the canonical pre-filter score-based rank — survives
# vol-collapse re-numbering, so rank_delta computed against it doesn't get a
# +1 nudge every time the filter removes someone above. Old history files
# without score_rank fall back to rank in enrich_with_persistence.


class _NYSECalendar(AbstractHolidayCalendar):
    """NYSE-observed holidays. Pandas's USFederalHolidayCalendar isn't quite
    right (NYSE observes Good Friday but not Columbus / Veterans Day). Rare
    one-off closures (e.g. presidential funerals, 9/11) aren't included —
    we'd save a stale snapshot on those (≤1 per year) and accept the noise."""
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


def _screen_with_offset(query, page_size, offset):
    """Wrap yf.screen with a fallback for older yfinance versions that don't
    accept the `offset` kwarg.

    Returns (raw_response, supports_offset). When the first call raises a
    TypeError that mentions `offset`, retries without `offset` and reports
    `supports_offset=False` so the caller can stop paginating after this
    page. Any other TypeError (e.g. wrong query type, unrelated kwarg
    mismatch) propagates — we only smooth over the one specific
    incompatibility we can positively identify, otherwise we'd silently
    degrade real bugs into single-page scans. If the retry call itself
    TypeErrors (a different cause this time), that also propagates."""
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
    """Pull current US large caps via yfinance's screener and cache to disk.

    Paginates with `offset` in `SCREENER_PAGE_SIZE` (250) row pages — Yahoo's
    per-request cap. With `count=None` the loop targets the response's `total`
    field; with an explicit positive `count` it stops at that many tickers
    (or earlier if the screener exhausts).

    Stop conditions, in priority order:
      1. `len(tickers) >= target`  — reached the requested / total count.
      2. Empty page (no quotes)    — screener returned nothing more.
      3. Short page (< page_size)  — natural end of results.
      4. Zero new tickers added    — duplicate page, prevents infinite loop
                                      when `total` is missing.
      5. `pages >= SCREENER_MAX_PAGES` — hard backstop; emits a stderr warning
                                      and returns whatever was collected.
      6. Old yfinance lacks `offset` — silently stop after page 1 (single-
                                      page mode, capped at SCREENER_PAGE_SIZE).

    Writes go through a sibling `.tmp` file + atomic rename so a crash mid-write
    can't truncate `universe.txt`. Failure to fetch any tickers raises
    RuntimeError *before* the cache file is touched, so a transient Yahoo
    outage can't poison the cache.
    """
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_market_cap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    tickers: list[str] = []
    seen: set[str] = set()
    offset = 0
    target = count  # None = pull everything Yahoo reports
    started = time.monotonic()
    pages = 0
    truncated = False
    while target is None or len(tickers) < target:
        if pages >= SCREENER_MAX_PAGES:
            # Backstop for the case where Yahoo's `total` is missing/zero and
            # the per-page guards never trigger — without this the loop could
            # run hundreds of pages before exhausting.
            print(f"refresh_universe: hit SCREENER_MAX_PAGES={SCREENER_MAX_PAGES} "
                  f"backstop at {len(tickers)} tickers (Yahoo did not report a "
                  f"`total`). Returning what we have.", file=sys.stderr)
            truncated = True
            break
        if pages > 0:
            # Spacer before the 2nd+ request — a multi-page burst against
            # Yahoo's screener has been observed to trip rate limits
            # intermittently. Putting it at the loop top (rather than after
            # the last successful page) means we don't sleep when about to
            # exit via `break` or the while condition. The skill only
            # paginates on cache refresh (every 7d), so amortized latency
            # is ~0.2s × (pages-1), negligible.
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
            break  # screener exhausted
        added = 0
        for q in quotes:
            sym = q.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                tickers.append(sym)
                added += 1
        if not supports_offset:
            # Old yfinance — pagination is unavailable, stop after page 1.
            break
        # Defensive stops: short page = end of results; zero new = duplicate
        # page (would loop forever). Both are the natural end of the screener.
        if len(quotes) < page_size or added == 0:
            break
        offset += page_size
    if not tickers:
        raise RuntimeError(
            "Yahoo screener returned no results — possibly rate-limited or "
            "API drift. Try again in a few minutes."
        )
    # Atomic write: tmp + rename, mirroring history.csv / sectors.json. A
    # crash mid-write would otherwise leave universe.txt truncated and the
    # next run would see a partial cache (within TTL → no auto-refresh).
    tmp_path = UNIVERSE_FILE.with_suffix(".txt.tmp")
    tmp_path.write_text("\n".join(tickers))
    tmp_path.replace(UNIVERSE_FILE)
    status = "Refreshed universe (TRUNCATED)" if truncated else "Refreshed universe"
    print(f"{status}: {len(tickers)} tickers in {pages} request(s), "
          f"{time.monotonic() - started:.1f}s.", file=sys.stderr)
    return tickers


def load_universe(min_market_cap: float, min_volume: int, count: int | None,
                  refresh_mode) -> list[str]:
    """Return the universe ticker list, refreshing from Yahoo when needed.

    count: None = pull all matches Yahoo reports; positive int = cap at that
        many tickers. If the on-disk cache has fewer rows than the requested
        `count`, force a refresh — otherwise the user would silently get an
        undersized universe (e.g. asking for 500 but cached 250 from a prior
        smaller run still being inside its TTL).
    refresh_mode: None = TTL-based (default), True = force refresh,
        False = use cache as-is regardless of TTL (offline / testing).
    """
    if not UNIVERSE_FILE.exists():
        return refresh_universe(min_market_cap, min_volume, count)
    if refresh_mode is True:
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
        print(f"Universe cache stale ({age_days:.1f}d), refreshing...", file=sys.stderr)
        return refresh_universe(min_market_cap, min_volume, count)
    return cached


def fetch_bars(tickers: list[str], window_months: int,
               min_period_months: int = 0) -> pd.DataFrame:
    """Pull the full OHLCV MultiIndex frame from yfinance. ATR/sector
    consumers reuse this single download via `extract_closes` — a second
    round-trip for the same ~1000 tickers would cost another ~30 sec."""
    months_needed = max(window_months + 1, 4, min_period_months)
    period = f"{months_needed}mo"
    print(f"Fetching {len(tickers)} tickers, period={period}...", file=sys.stderr)
    return yf.download(
        tickers, period=period, interval="1d", auto_adjust=True,
        progress=False, threads=True, group_by="ticker",
    )


def extract_closes(bars: pd.DataFrame, tickers: list[str],
                   min_len: int = 60) -> pd.DataFrame:
    """Pull Close column per ticker from the raw bars frame; drop tickers
    that don't have enough history to score the short window."""
    closes = {}
    for t in tickers:
        try:
            s = bars[(t, "Close")].dropna() if (t, "Close") in bars.columns else None
            if s is not None and len(s) > min_len:
                closes[t] = s
        except Exception:
            pass
    return pd.DataFrame(closes).sort_index()


# Regime gauges: SPY trend filter + universe breadth. Computed once per run and
# attached to the output banner; --regime-gate controls whether RISK-OFF
# suppresses the top-N table.
SPY_HISTORY_MONTHS = 13  # need ≥ 200 trading days + buffer for slope lookback
MA200_SLOPE_LOOKBACK_DAYS = 20  # ~1 trading month — long enough to filter noise
# Dead band on the 200DMA slope. A literally-flat MA (slope == 0) used to flip
# RISK-OFF; -0.05% over 20 trading days is ~noise from a single-bar revision.
MA200_SLOPE_RISK_ON_THRESHOLD_PCT = -0.05


def compute_regime(prices: pd.DataFrame) -> dict | None:
    """Compute SPY trend + universe breadth. Returns None if SPY fetch fails
    or has insufficient history; caller renders an 'unavailable' banner and
    proceeds — regime is informational, not load-bearing for scoring."""
    try:
        spy_df = yf.download("SPY", period=f"{SPY_HISTORY_MONTHS}mo",
                             interval="1d", auto_adjust=True, progress=False)
    except Exception as e:
        print(f"SPY fetch failed: {e}", file=sys.stderr)
        return None
    if spy_df is None or spy_df.empty:
        return None
    spy_close = spy_df["Close"]
    # yf.download with a single ticker string usually returns flat columns, but
    # certain versions/edge cases still wrap in MultiIndex — flatten defensively.
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
    # 20-trading-day % change in the 200DMA. Positive = trend lifting; negative
    # = trend rolling over even if price is still above the line.
    spy_ma200_slope_pct = (spy_ma200 / spy_ma200_prev - 1) * 100

    breadth_pct_200 = None
    breadth_pct_50_over_200 = None
    if prices is not None and not prices.empty and len(prices) >= 200:
        last = prices.iloc[-1]
        ma200_uni = prices.rolling(200).mean().iloc[-1]
        ma50_uni = prices.rolling(50).mean().iloc[-1]
        valid_200 = ma200_uni.notna() & last.notna()
        if valid_200.any():
            breadth_pct_200 = float(
                (last[valid_200] > ma200_uni[valid_200]).mean() * 100
            )
        valid_cross = ma50_uni.notna() & ma200_uni.notna()
        if valid_cross.any():
            breadth_pct_50_over_200 = float(
                (ma50_uni[valid_cross] > ma200_uni[valid_cross]).mean() * 100
            )

    # RISK-ON requires SPY above 200DMA AND the 200DMA itself rising — single
    # close-above-MA crosses whipsawed multiple times in 2015 and 2022; the
    # slope check defuses those false starts. The slope threshold is a small
    # negative dead band (not strict > 0) so a near-flat MA200 doesn't flip
    # on single-bar noise.
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
        "breadth_pct_50_above_200": breadth_pct_50_over_200,
        "risk_on": risk_on,
    }


# Vol targeting: cohort realized vol → suggested portfolio leverage. The
# motivation is specifically the Daniel-Moskowitz (2016) finding that
# vol-scaling momentum reduces the post-bear-crash drawdown — the failure
# mode the trend filter doesn't catch. Off by default (set --target-vol-pct).
VOL_TARGET_LOOKBACK_DAYS = 60  # ~3 trading months; canonical short-vol window.
# Note: this is deliberately decoupled from --window-months. The scoring window
# is the user's "what's been working lately" question; the vol-target window is
# the "how volatile is the cohort *right now*" question, and the literature
# (Daniel-Moskowitz, Barroso-Santa-Clara) converges on ~60 trading days as the
# vol-regime estimator that best predicts the next-period momentum crash.
# Using --window-months here would make 6-month users see laggier vol estimates.
VOL_TARGET_LEVERAGE_CLIP = (0.25, 1.0)  # cap at 1.0 — we only deleverage, never
# leverage up. Leveraging up during quiet pre-crash regimes is the exact
# trap Daniel-Moskowitz documented; 1.0 cap is the defensive variant.


def compute_vol_target(prices: pd.DataFrame, picks: list[dict], top_n: int,
                       target_vol_pct: float | None) -> dict | None:
    """Compute the equal-weight cohort's 60-day realized vol and the leverage
    needed to hit target_vol_pct. Returns None when disabled or under-data'd."""
    if target_vol_pct is None or target_vol_pct <= 0:
        return None
    cohort = [p["ticker"] for p in picks[:top_n] if p["ticker"] in prices.columns]
    if len(cohort) < 2:
        return None
    # +1 because pct_change drops the first row; we want LOOKBACK valid returns.
    basket = prices[cohort].tail(VOL_TARGET_LOOKBACK_DAYS + 1)
    daily_returns = basket.pct_change().dropna(how="all")
    # Equal-weight portfolio daily return = mean of constituent daily returns.
    # `.mean(axis=1)` ignores NaN per row, so newly-listed names that don't
    # cover the full window contribute only on days they have data — fine for
    # a vol estimate, mildly biases toward the always-present names.
    port_returns = daily_returns.mean(axis=1).dropna()
    if len(port_returns) < 20:
        return None
    cohort_vol = float(port_returns.std() * np.sqrt(252) * 100)
    if cohort_vol <= 0:
        return None
    raw_leverage = float(target_vol_pct) / cohort_vol
    lo, hi = VOL_TARGET_LEVERAGE_CLIP
    suggested_leverage = max(lo, min(hi, raw_leverage))
    return {
        "target_vol_pct": float(target_vol_pct),
        "cohort_vol_pct": cohort_vol,
        "lookback_days": int(VOL_TARGET_LOOKBACK_DAYS),
        "n_tickers": len(cohort),
        "raw_leverage": raw_leverage,
        "suggested_leverage": suggested_leverage,
        "leverage_clip": list(VOL_TARGET_LEVERAGE_CLIP),
    }


def assign_weights(picks: list[dict], top_n: int,
                   vol_target: dict | None) -> list[dict]:
    """Attach a `weight_pct` to each top-N pick using equal-risk-contribution
    (1/vol normalized) scaled by suggested_leverage. In percentage points —
    they sum to suggested_leverage * 100. No-op when vol_target is None."""
    if vol_target is None:
        return picks
    lev = vol_target["suggested_leverage"]
    top_picks = picks[:top_n]
    inv_vols = [(p["ticker"], 1.0 / p["ann_vol_pct"]) for p in top_picks
                if p.get("ann_vol_pct") and p["ann_vol_pct"] > 0]
    total_inv = sum(v for _, v in inv_vols)
    if total_inv <= 0:
        # Defensive — would only hit if every pick rounded to 0% vol.
        for p in top_picks:
            p["weight_pct"] = None
        return picks
    weights = {t: (v / total_inv) * lev * 100 for t, v in inv_vols}
    for p in top_picks:
        w = weights.get(p["ticker"])
        p["weight_pct"] = round(w, 1) if w is not None else None
    return picks


def render_vol_target_banner(vol_target: dict | None) -> str | None:
    if vol_target is None:
        return None
    return (
        f"**Vol target**: cohort {vol_target['lookback_days']}d vol "
        f"{vol_target['cohort_vol_pct']:.1f}% → suggested leverage "
        f"**{vol_target['suggested_leverage']:.2f}x** "
        f"(target {vol_target['target_vol_pct']:.0f}%, "
        f"raw {vol_target['raw_leverage']:.2f}x, "
        f"clip {vol_target['leverage_clip'][0]:.2f}–{vol_target['leverage_clip'][1]:.2f}x)"
    )


# Pullback entry indicator: pairs with momentum-scan's "what's running" answer
# by adding a "is it buyable *right now*" filter. Momentum names selected by
# trailing return often arrive already extended — the leader sits 30-50%
# above MA20 with RSI > 80, a state where mean-reversion pullbacks often give
# back a meaningful slice of the gain before the trend resumes. The buy zone
# (close to MA20 + cool RSI) is the classic "Trend Pullback" entry: buy
# strength, but only on its weakness.
PULLBACK_MA_PERIOD = 20
PULLBACK_RSI_PERIOD = 14  # canonical RSI period (Wilder, 1978)

# Signal classification thresholds. These are the literature-conventional RSI
# bands (< 40 = oversold / deep pullback, 40-55 = neutral pullback zone,
# > 70 = hot, > 80 = extreme) plus a 3% MA20 proximity that matches "stock is
# testing its 20-day average". Tweak inline; not exposed as CLI flags to keep
# the surface small.
PULLBACK_BUY_MA20_MAX_PCT = 3.0
PULLBACK_BUY_RSI_LOW = 40.0
PULLBACK_BUY_RSI_HIGH = 55.0
# 🔵 deep pullback: stock has pulled back through MA20 (more than the 🟢 buy
# zone's lower edge) AND RSI has cooled below the 🟢 zone — i.e., a Connors-
# style "uptrend got oversold short-term" entry, often the best risk/reward
# *if* the long-term trend is still intact. The momentum-scan filter ensures
# the trend is intact by construction (stock wouldn't pass the return floor
# otherwise), so deep pullbacks here are signal, not broken trends.
# DEEP/WATCH/OVEREXT all use the no-suffix trigger-threshold style; BUY is the
# only group that uses range-boundary suffixes (_MAX_PCT/_LOW/_HIGH) since 🟢
# is defined by a bounded interval, not a single threshold.
PULLBACK_DEEP_MA20_PCT = -3.0  # 🔵 fires at or below this MA20% (matches 🟢's lower edge for gap-free boundary)
PULLBACK_DEEP_RSI = 40.0       # 🔵 fires strictly below this RSI (the 🟢 zone's RSI floor)
PULLBACK_WATCH_MA20_PCT = 15.0  # 🟠 fires when MA20% exceeds this
PULLBACK_WATCH_RSI = 70.0       # 🟠 fires when RSI exceeds this
PULLBACK_OVEREXT_MA20_PCT = 25.0
PULLBACK_OVEREXT_RSI = 80.0


def compute_pullback_indicators(bars: pd.DataFrame,
                                tickers: list[str]) -> dict[str, dict]:
    """Compute MA20 distance and RSI(14) for each ticker. Reads from the same
    raw bars frame the universe was downloaded into — no extra network."""
    out = {}
    for t in tickers:
        try:
            if (t, "Close") not in bars.columns:
                continue
            close = bars[(t, "Close")].dropna()
            # RSI needs `period + 1` to seed the first delta; MA20 needs 20.
            min_len = max(PULLBACK_MA_PERIOD, PULLBACK_RSI_PERIOD + 1)
            if len(close) < min_len:
                continue
            last = float(close.iloc[-1])
            ma20 = float(close.rolling(PULLBACK_MA_PERIOD).mean().iloc[-1])
            if ma20 <= 0 or pd.isna(ma20):
                continue
            ma20_dist_pct = (last / ma20 - 1) * 100

            # RSI(14) with Wilder's smoothing — the canonical form. EWMA with
            # α = 1/period matches the original Wilder recursive average; SMA
            # would be wrong here (RSI without Wilder smoothing reads ~10
            # points higher in trending markets and would skew the signal).
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.ewm(alpha=1 / PULLBACK_RSI_PERIOD, adjust=False,
                                min_periods=PULLBACK_RSI_PERIOD).mean()
            avg_loss = loss.ewm(alpha=1 / PULLBACK_RSI_PERIOD, adjust=False,
                                min_periods=PULLBACK_RSI_PERIOD).mean()
            ag_last, al_last = avg_gain.iloc[-1], avg_loss.iloc[-1]
            if pd.isna(ag_last) or pd.isna(al_last):
                rsi = None
            elif al_last == 0:
                # All-gain window → RSI by convention is 100 (no downside).
                rsi = 100.0
            else:
                rs = ag_last / al_last
                rsi = 100 - (100 / (1 + rs))

            out[t] = {
                "ma20": ma20,
                "ma20_dist_pct": ma20_dist_pct,
                "rsi14": float(rsi) if rsi is not None else None,
            }
        except Exception:
            continue
    return out


def classify_pullback_signal(ma20_dist_pct: float | None,
                              rsi: float | None) -> str:
    """Map (MA20 distance, RSI) → 🟢/🔵/🟡/🟠/🔴 entry signal.

    Evaluation order: 🟢 → 🔵 → 🔴 → 🟠 → 🟡 (first match wins). The bucket
    descriptions below describe the *resulting* range after earlier buckets
    have been excluded, not the raw rule predicates.

    🟢 buy zone — price within ±3% of MA20 and RSI in 40-55 (cooled off,
        textbook Trend Pullback setup).
    🔵 deep pullback — price at or below MA20 by 3% or more AND RSI below 40
        (Connors-style "uptrend got oversold short-term"; the momentum-scan
        filter guarantees the long-term trend is still intact, so a deep
        short-term pullback here is signal, not broken trend). Both conditions
        are required — a below-MA20 price with a normal RSI usually means a
        mild drift, not the oversold setup the 🔵 entry relies on, so those
        cases fall through to 🟡 by design.
    🔴 overextended — MA20 distance > 25% or RSI > 80 (chasing here tends to
        cost a meaningful slice of the gain on the first mean-reversion bar).
    🟠 stretched — MA20 distance 15-25% or RSI 70-80 (in trend but extended;
        wait for a better entry).
    🟡 watch — everything else (in trend, neither at buy point nor extreme,
        including below-MA20 names whose RSI hasn't cooled below 40).
    — — data missing.
    """
    if ma20_dist_pct is None or rsi is None:
        return "—"
    if (-PULLBACK_BUY_MA20_MAX_PCT <= ma20_dist_pct <= PULLBACK_BUY_MA20_MAX_PCT
            and PULLBACK_BUY_RSI_LOW <= rsi <= PULLBACK_BUY_RSI_HIGH):
        return "🟢"
    # 🔵 uses `<=` on MA20% so there's no gap with 🟢's `>=` lower edge at -3:
    # MA20%=-3 + RSI=39 falls into 🔵, MA20%=-3 + RSI=40 into 🟢.
    if (ma20_dist_pct <= PULLBACK_DEEP_MA20_PCT
            and rsi < PULLBACK_DEEP_RSI):
        return "🔵"
    if ma20_dist_pct > PULLBACK_OVEREXT_MA20_PCT or rsi > PULLBACK_OVEREXT_RSI:
        return "🔴"
    if ma20_dist_pct > PULLBACK_WATCH_MA20_PCT or rsi > PULLBACK_WATCH_RSI:
        return "🟠"
    return "🟡"


def attach_pullback(picks: list[dict], top_n: int,
                    pullbacks: dict[str, dict]) -> list[dict]:
    """Attach ma20_dist_pct / rsi14 / pullback_signal to top-N picks. Picks
    without computed indicators get None / "—" so downstream JSON consumers
    see a consistent schema (every key always present when the indicator is
    enabled, even if individual values are missing)."""
    for p in picks[:top_n]:
        info = pullbacks.get(p["ticker"])
        if info is None:
            p["ma20_dist_pct"] = None
            p["rsi14"] = None
            p["pullback_signal"] = "—"
            continue
        p["ma20_dist_pct"] = round(info["ma20_dist_pct"], 1)
        rsi = info.get("rsi14")
        p["rsi14"] = round(rsi, 1) if rsi is not None else None
        p["pullback_signal"] = classify_pullback_signal(
            info["ma20_dist_pct"], rsi
        )
    return picks


# ATR-based stop loss: per-name "where would I cut this if it turns" measure.
# The skill's only built-in exit otherwise is "dropped out of top-N" which
# fires after a -20% max-drawdown — too late for active risk management.
ATR_PERIOD_DAYS = 14  # canonical Wilder period


def compute_atrs(bars: pd.DataFrame, tickers: list[str],
                 period: int = ATR_PERIOD_DAYS) -> dict[str, dict]:
    """Compute the latest N-day ATR (simple-mean true-range variant) for each
    ticker. Returns {ticker: {atr, last_close, atr_pct}}. Reads from the same
    raw bars frame the universe was downloaded into — no extra network."""
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
            # Align on the common index — newly-listed names occasionally have
            # one of OHLC missing for a session due to corporate-action edges.
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
            # Simple mean of last `period` true ranges. Wilder's smoothing is
            # canonical (EWMA with α=1/n) but differs by <5% in steady state;
            # SMA is easier to test and reason about for stop-sizing.
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
                     atrs: dict[str, dict],
                     prices: pd.DataFrame,
                     atr_mult: float,
                     trail_min_streak: int = 4) -> list[dict]:
    """Add stop_price / stop_pct (current-price anchored, always) and
    trail_stop_price / trail_stop_pct (peak-since-first_seen, only for
    streak ≥ trail_min_streak names — younger names don't have a stable
    peak to trail)."""
    for p in picks[:top_n]:
        info = atrs.get(p["ticker"])
        if info is None:
            continue
        p["atr"] = round(info["atr"], 2)
        p["atr_pct"] = round(info["atr_pct"], 2)
        stop = info["last_close"] - atr_mult * info["atr"]
        p["stop_price"] = round(stop, 2)
        p["stop_pct"] = round(
            (stop / info["last_close"] - 1) * 100, 2
        )
        # Trail stop: only meaningful once we've seen a name persist long
        # enough to have a real "peak since entry" instead of a single bar.
        if (p.get("streak", 1) >= trail_min_streak
                and p.get("first_seen") not in (None, "—", "🆕")
                and p["ticker"] in prices.columns):
            try:
                first_seen_ts = pd.Timestamp(p["first_seen"])
                series = prices[p["ticker"]].loc[first_seen_ts:].dropna()
                if not series.empty:
                    peak = float(series.max())
                    trail = peak - atr_mult * info["atr"]
                    p["trail_stop_price"] = round(trail, 2)
                    p["trail_stop_pct"] = round(
                        (trail / info["last_close"] - 1) * 100, 2
                    )
                    p["peak_since_first_seen"] = round(peak, 2)
            except Exception:
                pass
    return picks


# Sector / industry tagging. yfinance's Ticker.info is a separate HTTP round
# trip per ticker, so we cache to state/sectors.json with a 30-day TTL and
# only fetch for the top-N picks (not the full universe) on each run — the
# cache grows organically as different names cycle through leadership.
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
    """Fetch sector/industry for tickers missing from cache or past their
    TTL. Persists the merged cache back to disk. Failures are silently
    skipped — a tagged-as-empty sector falls through the abbrev mapping
    cleanly in render_sector_breakdown."""
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
    fetched = 0
    failed = 0
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
        # Yahoo throttles Ticker.info aggressively — partial failures are common.
        # Failed lookups render as `—` in the Sector column and skip the breakdown.
        print(f"Sectors: {fetched} fetched, {failed} failed (will retry next run).",
              file=sys.stderr)
    save_sectors(cache)
    return cache


def attach_sectors(picks: list[dict], top_n: int,
                   sectors: dict[str, dict]) -> list[dict]:
    """Attach sector/industry strings to top-N picks. Missing tickers get
    empty strings so render code can detect untagged rows."""
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
    """Return {counts: {sector: n}, n_tagged: int, n_total: int} or None when
    no picks have sector tags. Pure data; renderers and JSON consumers share
    this single source of truth."""
    n_total = len(picks[:top_n])
    tagged = [p.get("sector", "") for p in picks[:top_n] if p.get("sector")]
    if not tagged:
        return None
    counts: dict[str, int] = {}
    for s in tagged:
        counts[s] = counts.get(s, 0) + 1
    return {
        "counts": counts,
        "n_tagged": len(tagged),
        "n_total": n_total,
    }


def render_sector_breakdown(picks: list[dict], top_n: int,
                            max_show: int = 5) -> str | None:
    """Markdown line with top-K sectors and 'Other' rollup. None when no picks
    have sector tags (all lookups failed or sectors disabled)."""
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


def score_tickers(prices: pd.DataFrame, window_months: int,
                  min_return_pct: float, max_dd_pct: float) -> list[dict]:
    # Trading-day slice (~21 sessions / month) is more honest than a calendar
    # cutoff: prices.index only contains trading days, so a calendar cutoff
    # near a holiday week could include or exclude a session depending on
    # weekday placement. tail(N) deterministically takes the last N sessions.
    trading_days = max(int(round(window_months * 21)), 21)
    window = prices.tail(trading_days)
    results = []
    for t in window.columns:
        s = window[t].dropna()
        # Minimum-observations guard. The historical hard-coded 60 was tuned for
        # the 3mo default (~63 sessions); it silently zeroed out every name for
        # any window < ~3mo (tail(trading_days) can never reach 60 rows). Scale
        # it to the window — allow up to 3 missing sessions below the cap — while
        # capping at 60 so the ≥3mo behavior is byte-for-byte unchanged
        # (min(60, 63-3)=60; longer windows stay pinned at the 60 floor).
        if len(s) < min(60, trading_days - 3):
            continue
        ret = (s.iloc[-1] / s.iloc[0] - 1) * 100
        max_dd = ((s / s.cummax() - 1).min()) * 100
        from_high = (s.iloc[-1] / s.max() - 1) * 100
        daily = s.pct_change().dropna()
        ann_vol = daily.std() * np.sqrt(252) * 100
        # Clamp denominator at 1.0 so a near-zero drawdown doesn't blow up the
        # score (or produce inf when max_dd == 0). Means "score = return per
        # 1% drawdown, treating sub-1% pullbacks as 1%".
        score = ret / max(abs(max_dd), 1.0)
        if ret > min_return_pct and max_dd > -abs(max_dd_pct):
            results.append({
                "ticker": t,
                "return_pct": round(ret, 1),
                "max_dd_pct": round(max_dd, 1),
                "from_high_pct": round(from_high, 1),
                "ann_vol_pct": round(ann_vol, 0),
                "score": round(score, 2),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
        # score_rank is the immutable score-based ordering — survives any
        # downstream filtering / re-numbering. Used by enrich_with_persistence
        # so rank_delta reflects real score movement, not filter-induced shift.
        r["score_rank"] = i
    return results


# Vol-collapse filter: detect names whose realized vol crashed in the second
# half of the scoring window — the canonical signature of an acquisition
# target trading at the announced cash offer price (single-day gap up, then
# daily range collapses to pennies as the stock is pinned at the deal price
# and only M&A-arb spreads trade). Without this filter, the gap day inflates
# the window return while the post-event flat tape gives a misleadingly tiny
# max drawdown — together yielding an outlier Score that isn't tradable as
# momentum. The same signature catches reverse-merger / SPAC lock-ins and
# (less cleanly) some halted-into-cash situations.
VOL_COLLAPSE_MIN_FIRST_VOL_PCT = 5.0  # require ≥ 5% annualized in the first
# half before the ratio is meaningful — names already at very low vol have an
# unstable ratio and probably aren't being locked anyway.
VOL_COLLAPSE_MIN_RETURNS_PER_HALF = 10  # need enough returns per half for std
# to be meaningful; below this we skip the check rather than risk false flags.


def compute_vol_halves(price_series: pd.Series) -> tuple[float, float] | None:
    """Annualized realized vol for the first and second half of `price_series`.
    Returns (vol_first_pct, vol_second_pct) or None if too short to split."""
    daily = price_series.pct_change().dropna()
    n = len(daily)
    if n < 2 * VOL_COLLAPSE_MIN_RETURNS_PER_HALF:
        return None
    mid = n // 2
    v1 = float(daily.iloc[:mid].std() * np.sqrt(252) * 100)
    v2 = float(daily.iloc[mid:].std() * np.sqrt(252) * 100)
    return v1, v2


def filter_vol_collapse(
    picks: list[dict], prices: pd.DataFrame, window_months: int,
    ratio_threshold: float,
) -> tuple[list[dict], list[dict]]:
    """Split picks into (kept, excluded) by the vol-collapse signature.

    Excluded when vol_first_half ≥ MIN_FIRST_VOL_PCT *and* vol_second_half /
    vol_first_half < ratio_threshold. Each excluded pick gets `vol_first_pct`,
    `vol_second_pct`, `vol_ratio` attached so callers can surface the reason.
    The excluded pick's `rank` is set to None (rank in the kept list is the
    only meaningful display position) and the pre-filter score-based rank is
    preserved as `pre_filter_rank` for diagnostic display.

    Kept picks are re-ranked so #1..#N stay contiguous after exclusion."""
    if ratio_threshold is None or ratio_threshold <= 0:
        return picks, []
    trading_days = max(int(round(window_months * 21)), 21)
    window = prices.tail(trading_days)
    kept, excluded = [], []
    for p in picks:
        t = p["ticker"]
        if t not in window.columns:
            kept.append(p)
            continue
        halves = compute_vol_halves(window[t].dropna())
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
            # Preserve the score-based pre-filter position for display, then
            # clear `rank` — a JSON consumer reading rank=1 on an excluded
            # entry would otherwise think this name is the top pick.
            p["pre_filter_rank"] = p.get("rank")
            p["rank"] = None
            excluded.append(p)
        else:
            kept.append(p)
    # Re-number display rank on kept picks; score_rank stays put (set in
    # score_tickers) so persistence delta math is immune to this renumbering.
    for i, p in enumerate(kept, 1):
        p["rank"] = i
    return kept, excluded


def make_run_id(now: datetime, allow_same_day: bool = False) -> str:
    """Default runs get a date-only ET-anchored id (one scan per scan-day).
    --allow-same-day intentionally records multiple snapshots per ET date, so
    fall back to second precision to keep run_id unique per row."""
    if allow_same_day:
        return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return now.astimezone(MARKET_TZ).strftime("%Y%m%d")


def clear_history():
    """Remove history.csv and any leftover .tmp from a crashed atomic write."""
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    if tmp_path.exists():
        tmp_path.unlink()


def prune_non_trading_days() -> tuple[int, int]:
    """Drop history rows whose run_date ET-date is not an NYSE trading day.
    Cleans up snapshots that were saved before the non-trading-day guard
    existed (or with --save-stale). Returns (rows_removed, run_ids_removed)."""
    history = load_history()
    if history.empty:
        return (0, 0)
    et_dates = history["run_date"].dt.tz_convert(MARKET_TZ).dt.date
    min_d, max_d = et_dates.min(), et_dates.max()
    # Precompute the trading-day set once over the whole span — cheaper than
    # calling is_nyse_trading_day per row for histories with many rows.
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
    # load_history parsed run_date to tz-aware Timestamp; re-serialize so the
    # CSV round-trips identically to the original isoformat strings.
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
    # run_id is an identifier (an ET date stamp like "20260603", or
    # "20260603T143000Z" under --allow-same-day), not a number — but read_csv
    # infers int64 for the all-numeric default-mode files. make_run_id and every
    # current_run_id comparison use str, and `int64_col != "20260603"` is
    # silently always-true, so an int64 run_id breaks the current-run exclusion
    # in enrich_with_persistence / dropouts. Normalize to str here, at the single
    # entry point, so the in-memory frame matches the str semantics the rest of
    # the module (and HISTORY_COLS' type hints) already assume.
    df["run_id"] = df["run_id"].astype(str)
    # format='ISO8601' tolerates rows with and without microseconds in the same
    # file (pre-upgrade rows used microsecond precision; new ones don't).
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True, format="ISO8601")
    return df


def append_history(picks: list[dict], run_id: str, run_date: datetime,
                   allow_same_day: bool = False):
    """At most one snapshot per America/New_York calendar day — aligning the
    dedup unit with US market sessions, since that's what the data measures.
    If history already has rows for today's ET date, they're replaced, so
    re-running same-day refreshes the snapshot without inflating streak (which
    then naturally counts scan-days, not scan invocations).

    Empty picks lists are treated as no-ops rather than wipes: a failed/filtered
    scan shouldn't erase a good prior snapshot for the same day. To bypass the
    dedup entirely (e.g. for debugging), pass allow_same_day=True.

    Writes go through a sibling .tmp file + atomic rename so a crash mid-write
    can't truncate history.csv."""
    if not picks:
        return

    new_rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "run_date": run_date.isoformat(),
                "ticker": p["ticker"],
                "rank": p["rank"],
                # Fall back to rank if score_rank is missing — shouldn't
                # happen for kept picks after score_tickers, but defends
                # against an in-flight refactor.
                "score_rank": p.get("score_rank", p["rank"]),
                "score": p["score"],
                "return_pct": p["return_pct"],
                "max_dd_pct": p["max_dd_pct"],
                "ann_vol_pct": p["ann_vol_pct"],
                "from_high_pct": p["from_high_pct"],
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

    # Skip the concat when existing is empty/None — pd.concat with an empty
    # all-NaN-columns DataFrame triggers a FutureWarning that will become an
    # error in pandas 3.0.
    if existing is None or existing.empty:
        combined = new_rows
    else:
        combined = pd.concat([existing, new_rows], ignore_index=True)
    # Sort so the on-disk file is chronological — upserts append new rows at
    # the end, which would otherwise leave the file out of order after a
    # back-fill or any non-monotonic write.
    combined = (combined
                .sort_values(["run_date", "rank"], kind="stable")
                .reset_index(drop=True))
    # Lock column order to HISTORY_COLS (any future extra columns trail at
    # the end). Without this, an old-schema file gaining `score_rank` via
    # concat ends up with `score_rank` appended after `from_high_pct` rather
    # than at its canonical position after `rank`.
    canonical = HISTORY_COLS + [c for c in combined.columns
                                if c not in HISTORY_COLS]
    combined = combined.reindex(columns=canonical)
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)


def enrich_with_persistence(picks: list[dict], history: pd.DataFrame,
                            current_run_id: str) -> list[dict]:
    """Add streak / first_seen / rank_delta columns based on prior runs.

    rank_delta compares against the *latest prior appearance*, not the
    *immediately previous run*. A ticker that fell out for a few runs and
    is now back will show the delta against its last-seen rank — `streak`
    and the dropouts/new-entrants sections cover the gap separately.

    The delta is computed on `score_rank` (the pre-vol-collapse score-based
    rank), so removing a top pick by vol-collapse doesn't make every name
    below it falsely show +1 ↗. For backward compatibility with history rows
    written before the `score_rank` column existed (or rows where the column
    is NaN), falls back to `rank`."""
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
    has_score_rank_col = "score_rank" in prior.columns

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
        # Prefer score_rank for delta when both sides have it. Falls back to
        # rank when (a) the column doesn't exist in the file (pre-upgrade),
        # or (b) the value is NaN for this specific row (mixed-schema file).
        prev_score_rank = (prev["score_rank"]
                           if has_score_rank_col else None)
        if prev_score_rank is not None and not pd.isna(prev_score_rank):
            prev_for_delta = int(prev_score_rank)
        else:
            prev_for_delta = int(prev["rank"])
        cur_score_rank = p.get("score_rank", p["rank"])
        p["rank_delta"] = prev_for_delta - cur_score_rank  # positive = rising
        ticker_runs = set(appearances["run_id"].tolist())
        streak = 1
        for rid in reversed(run_ids_ordered):
            if rid in ticker_runs:
                streak += 1
            else:
                break
        p["streak"] = streak
    return picks


def dropouts(history: pd.DataFrame, current_picks: set[str],
             current_run_id: str, top_n: int) -> list[dict]:
    """Names that were in prior run's top N but missing now."""
    prior = history[history["run_id"] != current_run_id]
    if prior.empty:
        return []
    last_run_id = prior.sort_values("run_date")["run_id"].iloc[-1]
    last_run = prior[prior["run_id"] == last_run_id]
    last_run = last_run[last_run["rank"] <= top_n]
    dropped = last_run[~last_run["ticker"].isin(current_picks)]
    return [
        {"ticker": r["ticker"], "prev_rank": int(r["rank"]),
         "prev_return_pct": r["return_pct"]}
        for _, r in dropped.iterrows()
    ]


def render_table(picks: list[dict], top_n: int, window_months: int) -> str:
    rows = picks[:top_n]
    if not rows:
        return "(no picks passed the filter)"
    # Conditional columns: show only when at least one row carries the field.
    show_sector = any(p.get("sector") for p in rows)
    show_weight = any(p.get("weight_pct") is not None for p in rows)
    show_stop = any(p.get("stop_price") is not None for p in rows)
    show_pullback = any(p.get("ma20_dist_pct") is not None for p in rows)
    headers = ["#", "Ticker"]
    if show_sector:
        headers.append("Sector")
    headers += [f"{window_months}m%", "MaxDD%", "AnnVol%", "Score",
                "Streak", "RankΔ", "FirstSeen", "FromHigh%"]
    if show_pullback:
        headers += ["MA20%", "RSI", "Sig"]
    if show_stop:
        headers.append("Stop")
    if show_weight:
        headers.append("Weight%")
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
        row = [
            str(p["rank"]),
            f"**{p['ticker']}**",
        ]
        if show_sector:
            row.append(abbreviate_sector(p.get("sector", "")))
        ann_vol = p.get("ann_vol_pct")
        row += [
            f"{p['return_pct']:+.1f}",
            f"{p['max_dd_pct']:.1f}",
            f"{ann_vol:.0f}" if ann_vol is not None else "—",
            f"{p['score']:.1f}",
            str(p.get("streak", 1)),
            delta_str,
            p.get("first_seen", "—"),
            f"{p['from_high_pct']:.1f}",
        ]
        if show_pullback:
            md = p.get("ma20_dist_pct")
            rsi = p.get("rsi14")
            sig = p.get("pullback_signal")
            row.append(f"{md:+.1f}" if md is not None else "—")
            row.append(f"{rsi:.0f}" if rsi is not None else "—")
            row.append(sig if sig else "—")
        if show_stop:
            sp = p.get("stop_price")
            spct = p.get("stop_pct")
            if sp is not None and spct is not None:
                row.append(f"${sp:.2f} ({spct:+.1f}%)")
            else:
                row.append("—")
        if show_weight:
            w = p.get("weight_pct")
            row.append(f"{w:.1f}" if w is not None else "—")
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _longest_consecutive_streak(run_ids_for_ticker: list[str],
                                 ordered_run_ids: list[str]) -> int:
    """Longest run of consecutive ordered_run_ids that are in the ticker's set."""
    present = set(run_ids_for_ticker)
    best = cur = 0
    for rid in ordered_run_ids:
        if rid in present:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


SPARK_TICKS = "▁▂▃▄▅▆▇█"


def rank_sparkline(history: pd.DataFrame, ticker: str,
                   current_rank: int | None, top_n: int,
                   current_run_id: str | None = None,
                   max_points: int = 10) -> str:
    """Unicode sparkline of a ticker's leaderboard position over recent runs.

    Pulls the ticker's past `score_rank` from history (falling back to display
    `rank` for old rows that predate the column), chronologically ordered, then
    appends the current run's rank. Rows whose run_id matches `current_run_id`
    are dropped first: on a same-ET-day re-run, load_history() (which runs
    before append_history() in main()) returns a frame that still holds this
    morning's now-stale snapshot, and without this guard the trajectory would
    show "today" twice. The run_id comparison casts both sides to str
    defensively — load_history() already normalizes run_id to str, but this
    standalone helper doesn't trust its caller's dtype (a raw read_csv frame
    infers int64 for all-numeric run_ids, and `int64_col != "20260603"` is
    silently always-true).

    Heights are normalized against a FIXED 1..top_n leaderboard scale (not the
    name's own min/max), so a block's height is comparable across names: #1 is
    always the tallest block, #top_n the shortest, and ranks worse than top_n
    clamp to the floor. A name hovering near #1 reads as a near-flat row of tall
    blocks; one that swings deep reads as a tall-to-short plunge. Orientation is
    inverted so a *better* rank maps to a *taller* block — a rising line means
    climbing. Returns '' with fewer than two points (nothing to trend)."""
    series: list[int] = []
    if history is not None and not history.empty:
        rows = history[history["ticker"] == ticker]
        if current_run_id is not None:
            rows = rows[rows["run_id"].astype(str) != str(current_run_id)]
        rows = rows.sort_values("run_date")
        col = "score_rank" if "score_rank" in rows.columns else "rank"
        for v in rows[col].tolist():
            if pd.notna(v):
                series.append(int(v))
    if current_rank is not None:
        series.append(int(current_rank))
    series = series[-max_points:]
    if len(series) < 2:
        return ""
    if top_n <= 1:
        # Degenerate 1-name leaderboard: the only possible rank is #1 (the best),
        # so every block is the tallest. Guards both the div-by-zero and the
        # frac=(1-1)/1=0 inversion that would otherwise floor #1 to ▁.
        return SPARK_TICKS[-1] * len(series)
    span = top_n - 1
    out = []
    for v in series:
        # #1 -> 1.0 (tallest), #top_n -> 0.0; clamp ranks outside [1, top_n]
        frac = min(1.0, max(0.0, (top_n - v) / span))
        out.append(SPARK_TICKS[round(frac * (len(SPARK_TICKS) - 1))])
    return "".join(out)


def show_history_summary(history: pd.DataFrame):
    if history.empty:
        print("History is empty.")
        return
    runs = history.sort_values("run_date")["run_id"].drop_duplicates().tolist()
    print(f"Total runs: {len(runs)}")
    print(f"Date range: {history['run_date'].min().date()} → "
          f"{history['run_date'].max().date()}")
    counts = history.groupby("ticker").size().sort_values(ascending=False)

    # Streak analysis: per ticker, find longest consecutive run-id streak and
    # check whether the streak is "active" (extends to the most recent run).
    latest_run_id = runs[-1]
    longest_active: list[tuple[str, int]] = []
    longest_historical: list[tuple[str, int]] = []
    by_ticker = history.groupby("ticker")["run_id"].apply(list).to_dict()
    for t, ticker_runs in by_ticker.items():
        longest = _longest_consecutive_streak(ticker_runs, runs)
        longest_historical.append((t, longest))
        # Walk backwards from latest to find current active streak.
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
        print(f"\nLongest active streaks (extending through {latest_run_id}):")
        for t, n in longest_active[:10]:
            print(f"  {t:<8} {n} runs")

    print(f"\nLongest historical streaks (any time in history):")
    for t, n in longest_historical[:10]:
        print(f"  {t:<8} {n} runs")

    # Rank movement over the most recent comparable pair of runs. Compute
    # delta on score_rank (when available) instead of display rank, so a
    # vol-collapse exclusion of a top pick doesn't produce N false "+1
    # climbers" in the output. Mirrors the same fix in enrich_with_persistence.
    if len(runs) >= 2:
        use_score_rank = "score_rank" in history.columns

        def _slice_for_delta(run_id: str) -> pd.DataFrame:
            """Pull a (ticker, rank, _delta_rank) frame for one run. Display
            `rank` is preserved as-is for the climber/dropper display lines;
            `_delta_rank` is what the delta arithmetic uses (score_rank when
            present, fall back to rank when missing or NaN). Building
            `_delta_rank` from a fresh column avoids the duplicate-column
            trap of selecting `[..., "rank", "rank"]` and then renaming."""
            mask = history["run_id"] == run_id
            df = history.loc[mask, ["ticker", "rank"]].reset_index(drop=True)
            if use_score_rank:
                sr = history.loc[mask, "score_rank"].reset_index(drop=True)
                df["_delta_rank"] = sr.fillna(df["rank"])
            else:
                df["_delta_rank"] = df["rank"]
            return df

        latest = _slice_for_delta(runs[-1])
        prev = _slice_for_delta(runs[-2])
        merged = latest.merge(prev, on="ticker", suffixes=("_now", "_prev"))
        merged["delta"] = (merged["_delta_rank_prev"]
                           - merged["_delta_rank_now"])
        climbers = [r for _, r in merged.sort_values("delta", ascending=False)
                    .head(5).iterrows() if r["delta"] > 0]
        droppers = [r for _, r in merged.sort_values("delta", ascending=True)
                    .head(5).iterrows() if r["delta"] < 0]
        new = latest[~latest["ticker"].isin(prev["ticker"])]
        dropped = prev[~prev["ticker"].isin(latest["ticker"])]
        print(f"\nMost recent run pair: {runs[-2]} → {runs[-1]}")
        if climbers:
            print(f"  Biggest climbers:")
            for r in climbers:
                print(f"    {r['ticker']:<8} #{int(r['rank_prev'])} → #{int(r['rank_now'])} (+{int(r['delta'])})")
        if droppers:
            print(f"  Biggest droppers:")
            for r in droppers:
                print(f"    {r['ticker']:<8} #{int(r['rank_prev'])} → #{int(r['rank_now'])} ({int(r['delta'])})")
        if len(new):
            print(f"  New entrants ({len(new)}): {', '.join(new['ticker'].tolist()[:10])}"
                  + (" ..." if len(new) > 10 else ""))
        if len(dropped):
            print(f"  Dropouts ({len(dropped)}): {', '.join(dropped['ticker'].tolist()[:10])}"
                  + (" ..." if len(dropped) > 10 else ""))

    print(f"\nTop 20 most-frequent tickers across all runs:")
    for t, c in counts.head(20).items():
        print(f"  {t:<8} {c} appearances")


def _positive_int(s: str) -> int:
    """argparse type for flags that must be a positive integer (no 0/negative)."""
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {s!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer, got {v}"
        )
    return v


def _maybe_short_window_warning(window_months: int,
                                vol_collapse_ratio: float) -> str | None:
    """Returns the stderr warning string when the scoring window is too
    short for reliable vol-collapse half-vol estimation, else None. Extracted
    as a helper so tests can verify the warning text and triggering predicate
    without re-implementing the logic inline."""
    if window_months < 2 and vol_collapse_ratio > 0:
        return (f"Warning: --window-months {window_months} means each "
                f"vol-collapse half has only ~{window_months * 21 // 2} "
                f"returns; std estimates are noisy at this size. Consider "
                f"--vol-collapse-ratio 0 to disable if you see false flags.")
    return None


def _vol_collapse_ratio(s: str) -> float:
    """argparse type for --vol-collapse-ratio. Hard-reject values > 1.0:
    the ratio is `vol_second / vol_first`, and for the realistic equity
    universe v2/v1 is essentially always ≤ ~3, so any threshold > 1 would
    exclude essentially every name. Values ≤ 0 mean 'disable filter' and
    are allowed (and explicitly documented as the off switch)."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {s!r}")
    if v > 1.0:
        raise argparse.ArgumentTypeError(
            f"must be ≤ 1.0 (a 2nd-half / 1st-half vol ratio); got {v}. "
            f"Values > 1.0 would exclude essentially every name. Pass 0 "
            f"or a negative value to disable the filter."
        )
    return v


def build_argparser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Split out from main() so tests can verify
    flag registration / parsing without invoking the full scan."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-months", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--min-return-pct", type=float, default=30.0)
    ap.add_argument("--max-dd-pct", type=float, default=20.0)
    ap.add_argument("--min-market-cap", type=float, default=5e9)
    ap.add_argument("--min-volume", type=int, default=1_000_000)
    ap.add_argument("--universe-count", type=_positive_int, default=None,
                    help=("Universe size pulled from Yahoo's screener. Default "
                          "(unset) pulls every match the screener reports "
                          "(currently ~1000 US large caps at the default mcap/"
                          "volume floors). The screener returns at most 250 "
                          "rows per request, so larger values are paginated "
                          "automatically with `offset`. Pass an explicit "
                          "integer to cap the universe (e.g. 250 for a faster "
                          "refresh, 500 for the previous default)."))
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
                          "NYSE trading day (cleanup for pre-guard or "
                          "--save-stale runs)."))
    ap.add_argument("--no-save", action="store_true",
                    help="Don't append this run to history.")
    ap.add_argument("--save-stale", action="store_true",
                    help=("Save to history even when today's ET date is a "
                          "weekend or NYSE-observed holiday. Default skips "
                          "the save so streak counts don't inflate from "
                          "duplicate-data days. Pre-market runs on a real "
                          "trading day are always saved."))
    ap.add_argument("--allow-same-day", action="store_true",
                    help=("Append even if a row already exists for today's "
                          "America/New_York date. Default behavior overwrites "
                          "today's snapshot."))
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--regime-gate", choices=["off", "warn", "strict"],
                    default="warn",
                    help=("Market trend filter using SPY 200DMA + slope and "
                          "universe breadth. RISK-ON requires SPY > 200DMA "
                          "AND the 200DMA slope over the last 20 trading days "
                          "to exceed a small -0.05%% dead band (a near-flat "
                          "MA doesn't flip on single-bar noise). off=no "
                          "banner (skips the longer data fetch). warn=show "
                          "banner, still print top-N (default). strict="
                          "suppress top-N when RISK-OFF."))
    ap.add_argument("--target-vol-pct", type=float, default=None,
                    help=("Enable portfolio vol targeting. Computes the "
                          "equal-weight cohort's 60-day realized vol and a "
                          "suggested leverage = target / cohort_vol "
                          "(deleverage-only, clipped to [0.25, 1.0]). Adds a "
                          "Weight%% column using equal-risk-contribution × "
                          "leverage. Typical institutional target is 10–15. "
                          "Off by default."))
    ap.add_argument("--vol-collapse-ratio", type=_vol_collapse_ratio,
                    default=0.2,
                    help=("Exclude names whose 2nd-half realized vol (over the "
                          "scoring window) is less than this fraction of their "
                          "1st-half vol — the canonical signature of an "
                          "acquisition target locked at the cash offer price "
                          "(single-day gap up, then daily range collapses). "
                          "Default 0.2 (2nd half < 20%% of 1st half). Raise to "
                          "0.3 to catch more lock-ins (also more false "
                          "positives); lower to 0.15 to require a more "
                          "dramatic collapse before excluding (fewer "
                          "exclusions). A first-half vol floor of 5%% "
                          "annualized prevents already-low-vol names from "
                          "being flagged. Hard cap is 1.0 (argparse rejects "
                          "above). Pass 0 or a negative value to disable."))
    ap.add_argument("--atr-stop-mult", type=float, default=2.5,
                    help=("ATR-based stop loss multiplier (default 2.5). "
                          "Computes 14-day ATR per top-N pick and adds a Stop "
                          "column showing the suggested stop price = "
                          "last_close - mult × ATR. For streak ≥ "
                          "persistent-min-streak names, also computes a "
                          "TrailStop anchored to the peak since first_seen. "
                          "Typical multipliers: 2.0 tight, 2.5 standard, 3.0 "
                          "loose. Pass 0 or a negative value to disable."))
    ap.add_argument("--no-pullback", action="store_true",
                    help=("Disable the pullback entry indicator (MA20%% / "
                          "RSI / Sig columns). Default behavior shows price "
                          "relative to MA20, RSI(14), and a 5-level buy-zone "
                          "classification. See SKILL.md for thresholds."))
    ap.add_argument("--persistent-min-streak", type=int, default=3,
                    help=("Streak threshold used by both the 'Persistent "
                          "leaders' section and the ATR TrailStop. Default 3 "
                          "matches the historical display threshold; bump to "
                          "4 if you only want streaks that have survived "
                          "multiple periods of noise (the interpretation "
                          "guide's 'real signal' cutoff)."))
    ap.add_argument("--no-sectors", action="store_true",
                    help=("Disable sector tagging. Default behavior fetches "
                          "sector/industry for the top-N picks (cached in "
                          "state/sectors.json with a 30-day TTL), shows a "
                          "Sector column, and prints a sector-breakdown line. "
                          "First-run cost is ~1-2 sec per missing ticker "
                          "(parallelized at 10 workers)."))
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
        print(f"Pruned {rows} row(s) across {run_ids} run_id(s) "
              f"with non-trading-day ET dates.")
        return

    if args.show_history:
        show_history_summary(load_history())
        return

    short_window_warning = _maybe_short_window_warning(
        args.window_months, args.vol_collapse_ratio)
    if short_window_warning:
        print(short_window_warning, file=sys.stderr)

    universe = load_universe(args.min_market_cap, args.min_volume,
                             args.universe_count, args.refresh_universe)
    # When the regime gate is active we need ≥ 200 trading days for the 200DMA
    # breadth calc. Pull once in a longer window; score_tickers slices to the
    # scoring lookback internally. Off mode keeps the original short fetch.
    min_period = SPY_HISTORY_MONTHS if args.regime_gate != "off" else 0
    bars = fetch_bars(universe, args.window_months,
                      min_period_months=min_period)
    prices = extract_closes(bars, universe)
    regime = compute_regime(prices) if args.regime_gate != "off" else None
    picks = score_tickers(prices, args.window_months,
                          args.min_return_pct, args.max_dd_pct)
    picks, excluded_vol_collapse = filter_vol_collapse(
        picks, prices, args.window_months, args.vol_collapse_ratio,
    )
    history = load_history()
    now = datetime.now(timezone.utc)
    run_id = make_run_id(now, allow_same_day=args.allow_same_day)
    picks = enrich_with_persistence(picks, history, run_id)
    vol_target = compute_vol_target(prices, picks, args.top_n,
                                    args.target_vol_pct)
    picks = assign_weights(picks, args.top_n, vol_target)
    if args.atr_stop_mult and args.atr_stop_mult > 0:
        # ATR only needed for the top-N display picks, not the whole universe.
        top_tickers = [p["ticker"] for p in picks[: args.top_n]]
        atrs = compute_atrs(bars, top_tickers)
        picks = attach_atr_stops(picks, args.top_n, atrs, prices,
                                 args.atr_stop_mult,
                                 trail_min_streak=args.persistent_min_streak)
    if not args.no_pullback:
        top_tickers = [p["ticker"] for p in picks[: args.top_n]]
        pullbacks = compute_pullback_indicators(bars, top_tickers)
        picks = attach_pullback(picks, args.top_n, pullbacks)
    if not args.no_sectors:
        top_tickers = [p["ticker"] for p in picks[: args.top_n]]
        sectors = refresh_sectors(top_tickers, load_sectors())
        picks = attach_sectors(picks, args.top_n, sectors)
    current_set = {p["ticker"] for p in picks[: args.top_n]}
    drops = dropouts(history, current_set, run_id, args.top_n)

    # Non-trading-day guard: yfinance happily returns the last available
    # session's close on weekends/holidays. Recording it under today's ET
    # run_id would let streak counts inflate from duplicate data.
    # Pre-market runs on a trading day still save: today is a real trading
    # day from the streak's perspective even if we ran before the open.
    today_et = now.astimezone(MARKET_TZ).date()
    today_is_trading = is_nyse_trading_day(today_et)

    if not args.no_save:
        if not today_is_trading and not args.save_stale:
            print(f"Skipping history save: {today_et} is not an NYSE trading "
                  f"day (weekend or market holiday). Pass --save-stale to "
                  f"override.", file=sys.stderr)
        else:
            append_history(picks, run_id, now, allow_same_day=args.allow_same_day)

    # In strict mode, suppress the discovery sections when RISK-OFF. History
    # save still happens — streak is a fact about the market, not a trading
    # signal, and breaking it on every bear-market day would erase real
    # persistence data we'd want when the regime turns.
    suppress_picks = (args.regime_gate == "strict"
                      and regime is not None
                      and not regime["risk_on"])

    if args.format == "json":
        sector_breakdown = (None if args.no_sectors
                            else compute_sector_breakdown(picks, args.top_n))
        print(json.dumps({
            "run_id": run_id,
            "run_date": now.isoformat(),
            "params": vars(args),
            "universe_size": len(universe),
            "passed_filter": len(picks),
            "top_n": args.top_n,
            "regime": regime,
            "vol_target": vol_target,
            "sector_breakdown": sector_breakdown,
            "picks_suppressed_by_gate": suppress_picks,
            "picks": [] if suppress_picks else picks[: args.top_n],
            "dropouts_since_last_run": [] if suppress_picks else drops,
            "excluded_vol_collapse": excluded_vol_collapse,
        }, indent=2, default=str))
        return

    n_prior = len(history["run_id"].drop_duplicates()) if not history.empty else 0
    print(f"# Momentum scan — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n**Params**: window={args.window_months}mo, "
          f"min_return={args.min_return_pct}%, "
          f"max_dd={args.max_dd_pct}%, mcap>{args.min_market_cap:.0e}")
    # Banner reflects filter state with one unified pattern when active:
    #   - filter disabled (ratio ≤ 0): single "Passed filter: N"
    #   - filter active (any N):       "Passed filter: M (vol-collapse: K
    #     excluded of N)"  where M = final, K = exclusions, N = pre-filter
    # Zero-exclusion case still includes the parenthetical so the user can
    # distinguish "filter ran and found nothing" from the disabled case.
    if args.vol_collapse_ratio > 0:
        n_excluded = len(excluded_vol_collapse)
        n_passed_score = len(picks) + n_excluded
        if n_excluded == 0:
            passed_segment = (f"**Passed filter**: {len(picks)} "
                              f"(vol-collapse: 0 excluded)")
        else:
            passed_segment = (f"**Passed filter**: {len(picks)} "
                              f"(vol-collapse: {n_excluded} excluded of "
                              f"{n_passed_score})")
    else:
        passed_segment = f"**Passed filter**: {len(picks)}"
    universe_line = (f"**Universe**: {len(universe)} tickers · "
                     f"{passed_segment} · "
                     f"**Prior runs**: {n_prior}")
    print(universe_line)
    if args.regime_gate != "off":
        print(render_regime_banner(regime))
        if regime is not None and not regime["risk_on"]:
            if args.regime_gate == "strict":
                print("\n> ⚠️ **RISK-OFF + strict gate**: top-N suppressed. "
                      "History still saved so streak data survives the regime "
                      "change. Re-run with `--regime-gate warn` to see names.")
            else:
                print("\n> ⚠️ **RISK-OFF regime**: treat the names below as "
                      "*relative strength* (what's holding up), not absolute "
                      "buy candidates. Momentum strategies have their worst "
                      "drawdowns in this regime.")

    # Vol-target banner sits below the regime banner — same "context" block.
    # Skip it in strict+RISK-OFF since per-name weights wouldn't be shown either.
    if not suppress_picks:
        vt_banner = render_vol_target_banner(vol_target)
        if vt_banner is not None:
            print(vt_banner)
        sector_line = render_sector_breakdown(picks, args.top_n)
        if sector_line is not None:
            print(sector_line)

    w = args.window_months
    # Excluded-by-vol-collapse section prints *before* the suppress_picks gate
    # so the count in the universe banner always has a matching detail block.
    # In strict+RISK-OFF mode this is the one thing still shown — exclusions
    # aren't buy signals, they're warnings about names that look like
    # momentum but aren't, which is useful regardless of regime.
    if excluded_vol_collapse:
        print(f"\n## Excluded by vol-collapse filter "
              f"({len(excluded_vol_collapse)})")
        # Use :g on (ratio*100) so 0.2 renders as "20%" (no trailing zero) but
        # 0.155 renders as "15.5%" (preserves the precision the user set).
        ratio_pct_str = f"{args.vol_collapse_ratio * 100:g}%"
        print(f"_2nd-half realized vol < "
              f"{ratio_pct_str} of 1st-half — likely "
              f"acquisition / lock-in, not tradable momentum._")
        for p in sorted(excluded_vol_collapse, key=lambda x: x["vol_ratio"]):
            print(f"- **{p['ticker']}** ({w}m {p['return_pct']:+.1f}%, "
                  f"MaxDD {p['max_dd_pct']:.1f}%, "
                  f"vol {p['vol_first_pct']:.1f}% → "
                  f"{p['vol_second_pct']:.1f}%, "
                  f"ratio {p['vol_ratio']:.2f})")

    if suppress_picks:
        return

    print(f"\n## Top {args.top_n}\n")
    print(render_table(picks, args.top_n, args.window_months))

    # Tickers excluded by vol-collapse this run that were in the prior run's
    # top-N will surface in dropouts; label them so the reason is visible
    # (otherwise a +34% name silently "dropping out" is confusing).
    excluded_tickers = {p["ticker"] for p in excluded_vol_collapse}
    if drops:
        print(f"\n## Dropouts since last run ({len(drops)})")
        for d in drops:
            suffix = (" · *filtered by vol-collapse this run*"
                      if d["ticker"] in excluded_tickers else "")
            print(f"- **{d['ticker']}** (was #{d['prev_rank']}, "
                  f"{w}m={d['prev_return_pct']:+.1f}%){suffix}")

    if not history.empty:
        new_entries = [p for p in picks[: args.top_n] if p.get("prev_rank") is None]
        if new_entries:
            print(f"\n## New entrants ({len(new_entries)})")
            for p in new_entries:
                print(f"- **{p['ticker']}** at #{p['rank']} "
                      f"({w}m {p['return_pct']:+.1f}%, MaxDD {p['max_dd_pct']:.1f}%)")
        min_streak = args.persistent_min_streak
        sticky = [p for p in picks[: args.top_n] if p.get("streak", 1) >= min_streak]
        if sticky:
            print(f"\n## Persistent leaders (streak ≥ {min_streak} runs)")
            print(f"_rank trajectory vs top {args.top_n}: █ = #1 · "
                  f"▁ = #{args.top_n} or worse; rising = climbing_")
            for p in sorted(sticky, key=lambda x: -x["streak"]):
                spark = rank_sparkline(
                    history, p["ticker"], p.get("score_rank", p.get("rank")),
                    args.top_n, current_run_id=run_id)
                spark_prefix = f"`{spark}` " if spark else ""
                line = (f"- {spark_prefix}**{p['ticker']}** — streak {p['streak']}, "
                        f"first seen {p.get('first_seen', '—')}, "
                        f"now #{p['rank']}")
                # TrailStop attaches at the same min_streak threshold, so
                # names below it skip the suffix cleanly.
                if p.get("trail_stop_price") is not None:
                    line += (f" · trail stop "
                             f"${p['trail_stop_price']:.2f} "
                             f"({p['trail_stop_pct']:+.1f}% from spot, "
                             f"peak ${p['peak_since_first_seen']:.2f})")
                print(line)


if __name__ == "__main__":
    main()
