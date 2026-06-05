"""
Regime scan: a daily market-level read on TREND and SENTIMENT TURN.

Where momentum-scan answers "which names are working", this answers the level
above it — "is the tape healthy, is it narrowing, is sentiment about to turn".
It pulls a small macro basket (indices, VIX term structure, credit, defensive
rotation) plus a bundled cross-sector large-cap list for true breadth, scores
four layers, and folds them into a single 🟢/🟡/🔴 state plus a list of
divergence flags. The turn is almost always "internals deteriorate while the
index still prints highs" — that's exactly what the divergence flags catch.

Each US market day (America/New_York) is logged once to state/history.csv, so
the *slope* of breadth / VIX / credit over days — the real turn signal — is
visible across runs, not just today's snapshot.

Self-contained — uses yfinance directly, no cross-skill dependencies.

Usage:
  python scan.py                 # full run
  python scan.py --format json   # machine-readable
  python scan.py --show-history  # dump the daily state log (no new scan)
  python scan.py --lookback N    # change the slope/RS lookback (default 20d)
  python scan.py --no-save       # don't append today to history
  python scan.py --clear-history # wipe history.csv (no confirmation)
"""
import argparse
import json
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd
import yfinance as yf
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
    USThanksgivingDay, nearest_workday,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
BREADTH_UNIVERSE_FILE = STATE_DIR / "breadth_universe.txt"
HISTORY_FILE = STATE_DIR / "history.csv"
MARKET_TZ = ZoneInfo("America/New_York")

# --- Macro basket -----------------------------------------------------------
# Liquid, always-available instruments. Each is one HTTP-free column out of the
# single batched download. ^VIX3M is CBOE's 3-month vol index (the VIX term-
# structure partner); ^VXV is its retired symbol, used as a fallback.
INDEX_TICKERS = ["SPY", "QQQ", "RSP"]   # RSP = equal-weight S&P, the breadth proxy
VOL_TICKERS = ["^VIX", "^VIX3M"]        # ^VXV (retired) handled as a fallback in compute_metrics
CREDIT_TICKERS = ["HYG", "LQD"]         # HY vs IG — credit confirms/leads equities
SECTOR_OFFENSIVE = ["XLK", "XLY", "XLC"]
SECTOR_DEFENSIVE = ["XLU", "XLP", "XLV"]
MACRO_TICKERS = (INDEX_TICKERS + VOL_TICKERS + CREDIT_TICKERS
                 + SECTOR_OFFENSIVE + SECTOR_DEFENSIVE)

# --- Windows ----------------------------------------------------------------
MA_FAST, MA_SLOW = 50, 200
SLOPE_LOOKBACK_DEFAULT = 20   # default --lookback for RELATIVE-STRENGTH signals
                              # (RSP/SPY, credit, rotation) — user-tunable
MA200_SLOPE_LOOKBACK = 20     # FIXED window for the 200DMA trend-gate slope —
# deliberately decoupled from --lookback so changing the RS window can't silently
# re-tune the trend gate (and the deadband below, which is calibrated for 20d).
NHNL_WINDOW = 252             # 52-week new-high / new-low window
MA200_SLOPE_DEADBAND_PCT = -0.05  # near-flat MA200 (over the fixed 20d) shouldn't flip the gate
NEAR_HIGH_PCT = 3.0           # within 3% of the 252d high counts as "near highs"
FETCH_MONTHS = 14             # ≥ 200 + 252-day lookbacks + buffer

# --- Classification thresholds ---------------------------------------------
BREADTH_BULL, BREADTH_BEAR = 60.0, 40.0   # % of names above an MA
RSPSPY_DEADBAND = 0.5         # % move of RSP/SPY over the lookback to count
CREDIT_DEADBAND = 0.25        # % move of HYG/LQD over the lookback to count
ROTATION_DEADBAND = 0.5       # pp of (defensive − offensive) return to count
VIX_CALM, VIX_STRESS = 17.0, 25.0
VIX_SPIKE_5D_PCT = 20.0       # VIX up >20% in 5 sessions = a sentiment jolt

HISTORY_COLS = [
    "run_id", "run_date", "state", "score", "n_bull", "n_bear", "n_flags",
    "spy_vs_200_pct", "ma200_slope_pct", "breadth_50_pct", "breadth_200_pct",
    "rsp_spy_pct", "nhnl_pct", "vix", "vix_term", "credit_pct", "def_off_pct",
    "flags",
]


# --------------------------------------------------------------------------- #
# NYSE trading-day calendar (mirrors momentum-scan so the two stay in lockstep)
# --------------------------------------------------------------------------- #
class _NYSECalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday,
                start_date="2022-06-19"),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def is_nyse_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty


# --------------------------------------------------------------------------- #
# Data fetch
# --------------------------------------------------------------------------- #
def load_breadth_universe() -> list[str]:
    if not BREADTH_UNIVERSE_FILE.exists():
        return []
    # Skip blank lines and `#` comments so the file can carry a provenance
    # header (source / date / quarter) written by build_universe.py.
    return [t.strip() for t in BREADTH_UNIVERSE_FILE.read_text().splitlines()
            if t.strip() and not t.lstrip().startswith("#")]


def fetch_bars(tickers: list[str]) -> pd.DataFrame:
    print(f"Fetching {len(tickers)} tickers, period={FETCH_MONTHS}mo...",
          file=sys.stderr)
    return yf.download(tickers, period=f"{FETCH_MONTHS}mo", interval="1d",
                       auto_adjust=True, progress=False, threads=True,
                       group_by="ticker")


def get_close(bars: pd.DataFrame, ticker: str) -> pd.Series | None:
    """Pull one ticker's Close series from the batched MultiIndex frame.
    Returns None when the column is absent or all-NaN (e.g. a symbol Yahoo
    didn't recognize) so every downstream metric degrades to None cleanly."""
    try:
        if (ticker, "Close") not in bars.columns:
            return None
        s = bars[(ticker, "Close")].dropna()
        return s if not s.empty else None
    except Exception:
        return None


def extract_closes(bars: pd.DataFrame, tickers: list[str]) -> dict[str, pd.Series]:
    out = {}
    for t in tickers:
        s = get_close(bars, t)
        if s is not None:
            out[t] = s
    return out


# --------------------------------------------------------------------------- #
# Metric helpers
# --------------------------------------------------------------------------- #
def pct_over(series: pd.Series | None, lookback: int) -> float | None:
    """Percent change of `series` over `lookback` sessions."""
    if series is None:
        return None
    s = series.dropna()
    if len(s) < lookback + 1:
        return None
    return (s.iloc[-1] / s.iloc[-1 - lookback] - 1) * 100


def ratio_slope(a: pd.Series | None, b: pd.Series | None,
                lookback: int) -> float | None:
    """Percent change of the ratio a/b over `lookback` sessions (aligned)."""
    if a is None or b is None:
        return None
    idx = a.dropna().index.intersection(b.dropna().index)
    if len(idx) < lookback + 1:
        return None
    r = (a.loc[idx] / b.loc[idx])
    return (r.iloc[-1] / r.iloc[-1 - lookback] - 1) * 100


def pct_above_ma(closes: dict[str, pd.Series], period: int) -> float | None:
    """Share of the breadth universe trading above its `period`-day MA."""
    flags = []
    for s in closes.values():
        s = s.dropna()
        if len(s) >= period:
            ma = s.rolling(period).mean().iloc[-1]
            if pd.notna(ma) and ma > 0:
                flags.append(1.0 if s.iloc[-1] > ma else 0.0)
    if not flags:
        return None
    return sum(flags) / len(flags) * 100


def new_high_low(closes: dict[str, pd.Series],
                 window: int = NHNL_WINDOW) -> dict | None:
    """Count names at a 52-week high vs low. `last >= rolling max` (ties count)
    because a close equal to the window max IS the high."""
    nh = nl = n = 0
    for s in closes.values():
        s = s.dropna()
        if len(s) < window:
            continue
        w = s.tail(window)
        last = s.iloc[-1]
        if last >= w.max():
            nh += 1
        elif last <= w.min():
            nl += 1
        n += 1
    if n == 0:
        return None
    return {"nh": nh, "nl": nl, "n": n, "nhnl_pct": (nh - nl) / n * 100}


def trend_block(close: pd.Series | None) -> dict | None:
    """SPY/QQQ-style trend read: price vs 50/200 DMA, 200DMA slope (fixed 20d —
    the trend gate must not move with --lookback), distance from the 252-day
    high. None when there isn't enough history for a 200DMA."""
    if close is None:
        return None
    s = close.dropna()
    if len(s) < MA_SLOW + MA200_SLOPE_LOOKBACK:
        return None
    ma200 = s.rolling(MA_SLOW).mean()
    last = float(s.iloc[-1])
    ma50_last = float(s.rolling(MA_FAST).mean().iloc[-1])
    ma200_last = float(ma200.iloc[-1])
    slope = (ma200.iloc[-1] / ma200.iloc[-1 - MA200_SLOPE_LOOKBACK] - 1) * 100
    high_252 = float(s.tail(NHNL_WINDOW).max())
    return {
        "last": last,
        "above_50": last > ma50_last,
        "above_200": last > ma200_last,
        "vs_200_pct": (last / ma200_last - 1) * 100,
        "ma200_slope_pct": float(slope),
        "from_high_pct": (last / high_252 - 1) * 100,
    }


# --------------------------------------------------------------------------- #
# Compute all raw metrics
# --------------------------------------------------------------------------- #
def compute_metrics(macro: dict[str, pd.Series],
                    breadth: dict[str, pd.Series],
                    lookback: int) -> dict:
    spy = macro.get("SPY")
    qqq = macro.get("QQQ")
    rsp = macro.get("RSP")

    spy_trend = trend_block(spy)   # trend gate uses a fixed 20d slope, not `lookback`
    qqq_trend = trend_block(qqq)

    breadth_50 = pct_above_ma(breadth, MA_FAST)
    breadth_200 = pct_above_ma(breadth, MA_SLOW)
    nhnl = new_high_low(breadth)

    rsp_spy = ratio_slope(rsp, spy, lookback)   # >0 = broadening, <0 = narrowing

    # VIX + term structure. ^VIX3M preferred, ^VXV fallback (retired symbol).
    vix = macro.get("^VIX")
    # Explicit None-check, not `a or b` — `Series or Series` raises on ambiguous
    # truthiness. ^VXV is the retired symbol, kept only as a fallback.
    vix3m = macro.get("^VIX3M")
    if vix3m is None:
        vix3m = macro.get("^VXV")
    vix_last = float(vix.dropna().iloc[-1]) if vix is not None and not vix.dropna().empty else None
    vix_5d = pct_over(vix, 5)
    vix_term = None
    if (vix is not None and vix3m is not None
            and not vix.dropna().empty and not vix3m.dropna().empty):
        vt = float(vix.dropna().iloc[-1]) / float(vix3m.dropna().iloc[-1])
        vix_term = vt   # >1.0 = backwardation (acute stress); <1.0 = contango (calm)

    credit = ratio_slope(macro.get("HYG"), macro.get("LQD"), lookback)

    # Defensive vs offensive sector relative strength over the lookback.
    off_rets = [pct_over(macro.get(t), lookback) for t in SECTOR_OFFENSIVE]
    def_rets = [pct_over(macro.get(t), lookback) for t in SECTOR_DEFENSIVE]
    off_rets = [r for r in off_rets if r is not None]
    def_rets = [r for r in def_rets if r is not None]
    def_off = None
    if off_rets and def_rets:
        # Positive = defensives leading = risk-off tilt.
        def_off = sum(def_rets) / len(def_rets) - sum(off_rets) / len(off_rets)

    return {
        "spy_trend": spy_trend,
        "qqq_trend": qqq_trend,
        "breadth_50_pct": breadth_50,
        "breadth_200_pct": breadth_200,
        "nhnl": nhnl,
        "rsp_spy_pct": rsp_spy,
        "vix": vix_last,
        "vix_5d_pct": vix_5d,
        "vix_term": vix_term,
        "credit_pct": credit,
        "def_off_pct": def_off,
        "lookback": lookback,
        "n_breadth": len(breadth),
    }


# --------------------------------------------------------------------------- #
# Classify: votes per signal → score; divergence flags → turn warning; state
# --------------------------------------------------------------------------- #
def _vote(value, bull_thr, bear_thr, higher_is_bull=True):
    """+1 bullish / -1 bearish / 0 neutral. None value → 0 (signal abstains)."""
    if value is None:
        return 0
    if higher_is_bull:
        if value >= bull_thr:
            return 1
        if value <= bear_thr:
            return -1
    else:
        if value <= bull_thr:
            return 1
        if value >= bear_thr:
            return -1
    return 0


def _plural(n: int, noun: str) -> str:
    """'1 divergence' / '0 divergences' / '3 divergences' — no '1 divergences'."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def classify(m: dict) -> dict:
    """Fold raw metrics into per-signal votes, divergence flags, and a single
    🟢/🟡/🔴 state. The state machine mirrors the user's escalation ladder:
    healthy → caution (tighten trail stops / raise cash) → risk-off (cut)."""
    spy = m["spy_trend"]
    qqq = m["qqq_trend"]
    signals = []

    def add(layer, label, value, vote, reading):
        signals.append({"layer": layer, "label": label, "value": value,
                        "vote": vote, "reading": reading})

    # --- Layer 1: Trend ---
    uptrend_intact = bool(
        spy and spy["above_200"]
        and spy["ma200_slope_pct"] > MA200_SLOPE_DEADBAND_PCT
    )
    if spy:
        v = 1 if (spy["above_50"] and spy["above_200"]
                  and spy["ma200_slope_pct"] > 0) else (
            -1 if (not spy["above_200"]
                   or spy["ma200_slope_pct"] <= MA200_SLOPE_DEADBAND_PCT) else 0)
        add("Trend", "SPY vs 50/200DMA",
            spy["vs_200_pct"], v,
            f"{'>' if spy['above_50'] else '<'}50DMA, "
            f"{'>' if spy['above_200'] else '<'}200DMA "
            f"({spy['vs_200_pct']:+.1f}%), slope {spy['ma200_slope_pct']:+.2f}%")
    if qqq:
        v = 1 if (qqq["above_50"] and qqq["above_200"]) else (
            -1 if not qqq["above_200"] else 0)
        add("Trend", "QQQ vs 50/200DMA", qqq["vs_200_pct"], v,
            f"{'>' if qqq['above_50'] else '<'}50DMA, "
            f"{'>' if qqq['above_200'] else '<'}200DMA ({qqq['vs_200_pct']:+.1f}%)")

    # --- Layer 2: Breadth ---
    v = _vote(m["breadth_50_pct"], BREADTH_BULL, BREADTH_BEAR)
    add("Breadth", "% > 50DMA", m["breadth_50_pct"], v,
        "—" if m["breadth_50_pct"] is None else f"{m['breadth_50_pct']:.0f}% of universe")
    v = _vote(m["breadth_200_pct"], BREADTH_BULL, BREADTH_BEAR)
    add("Breadth", "% > 200DMA", m["breadth_200_pct"], v,
        "—" if m["breadth_200_pct"] is None else f"{m['breadth_200_pct']:.0f}% of universe")
    v = _vote(m["rsp_spy_pct"], RSPSPY_DEADBAND, -RSPSPY_DEADBAND)
    add("Breadth", "RSP/SPY (equal vs cap wt)", m["rsp_spy_pct"], v,
        "—" if m["rsp_spy_pct"] is None else
        f"{m['rsp_spy_pct']:+.1f}% / {m['lookback']}d "
        f"({'broadening' if m['rsp_spy_pct'] > 0 else 'narrowing'})")
    nhnl_pct = m["nhnl"]["nhnl_pct"] if m["nhnl"] else None
    v = _vote(nhnl_pct, 0.001, -0.001)
    add("Breadth", "New highs − new lows", nhnl_pct, v,
        "—" if m["nhnl"] is None else
        f"{m['nhnl']['nh']} NH − {m['nhnl']['nl']} NL ({nhnl_pct:+.0f}%)")

    # --- Layer 3: Vol / sentiment ---
    v = _vote(m["vix"], VIX_CALM, VIX_STRESS, higher_is_bull=False)
    add("Vol", "VIX level", m["vix"], v,
        "—" if m["vix"] is None else f"{m['vix']:.1f}")
    # term structure: <0.95 strong contango (+1), >1.0 backwardation (-1)
    v = 0
    if m["vix_term"] is not None:
        v = 1 if m["vix_term"] < 0.95 else (-1 if m["vix_term"] > 1.0 else 0)
    add("Vol", "VIX term (VIX/VIX3M)", m["vix_term"], v,
        "—" if m["vix_term"] is None else
        f"{m['vix_term']:.2f} ({'backwardation' if m['vix_term'] > 1.0 else 'contango'})")

    # --- Layer 4: Credit / rotation ---
    v = _vote(m["credit_pct"], CREDIT_DEADBAND, -CREDIT_DEADBAND)
    add("Credit", "HYG/LQD (HY vs IG)", m["credit_pct"], v,
        "—" if m["credit_pct"] is None else
        f"{m['credit_pct']:+.1f}% / {m['lookback']}d")
    # defensive − offensive: negative = offensives leading = risk-on (+1)
    v = _vote(m["def_off_pct"], -ROTATION_DEADBAND, ROTATION_DEADBAND,
              higher_is_bull=False)
    add("Credit", "Defensive − offensive RS", m["def_off_pct"], v,
        "—" if m["def_off_pct"] is None else
        f"{m['def_off_pct']:+.1f}pp / {m['lookback']}d "
        f"({'defensives leading' if m['def_off_pct'] > 0 else 'offensives leading'})")

    score = sum(s["vote"] for s in signals)
    n_bull = sum(1 for s in signals if s["vote"] == 1)
    n_bear = sum(1 for s in signals if s["vote"] == -1)

    # --- Divergence flags: the turn detector. Each fires only while the index
    # itself is still holding up (uptrend_intact) — a deteriorating internal
    # under an already-broken tape isn't a "divergence", it's just the bear. ---
    flags = []
    near_high = bool(spy and spy["from_high_pct"] >= -NEAR_HIGH_PCT)
    if uptrend_intact:
        if m["breadth_50_pct"] is not None and m["breadth_50_pct"] < 50 and near_high:
            flags.append(f"Breadth divergence: SPY {spy['from_high_pct']:+.1f}% from high but only "
                         f"{m['breadth_50_pct']:.0f}% of names above 50DMA")
        if m["rsp_spy_pct"] is not None and m["rsp_spy_pct"] < -RSPSPY_DEADBAND:
            flags.append(f"Narrowing rally: equal-weight lagging cap-weight (RSP/SPY {m['rsp_spy_pct']:+.1f}%/"
                         f"{m['lookback']}d) — only megacaps lifting the index")
        if m["credit_pct"] is not None and m["credit_pct"] < -CREDIT_DEADBAND:
            flags.append(f"Credit weakening: HYG/LQD {m['credit_pct']:+.1f}%/{m['lookback']}d "
                         f"— credit not confirming equities")
        if m["def_off_pct"] is not None and m["def_off_pct"] > ROTATION_DEADBAND:
            flags.append(f"Defensive rotation: defensives outran offensives over {m['lookback']}d by "
                         f"{m['def_off_pct']:+.1f}pp")
        if m["vix_term"] is not None and m["vix_term"] > 1.0:
            flags.append(f"Vol-curve inversion: VIX>VIX3M ({m['vix_term']:.2f}) — acute stress")
        if m["vix_5d_pct"] is not None and m["vix_5d_pct"] > VIX_SPIKE_5D_PCT:
            flags.append(f"VIX 5-day spike {m['vix_5d_pct']:+.0f}%")
    n_flags = len(flags)

    # --- State machine (priority order) ---
    b50 = m["breadth_50_pct"]
    if not uptrend_intact:
        state, label = "🔴", "RISK-OFF"
        reason = "SPY broke below / flattened its 200DMA — trend gate off"
        action = "Cut gross exposure, let trail stops take over; don't bottom-fish an unconfirmed bounce"
    elif n_flags >= 4 or score <= -3:
        state, label = "🔴", "RISK-OFF (internals)"
        reason = f"Price still above 200DMA but internals broke ({_plural(n_flags, 'divergence')} / score {score})"
        action = "Cut gross exposure; this is the 'price hasn't dropped yet but internals are already rotting' late-stage top"
    elif n_flags >= 2 or score <= -2 or (b50 is not None and b50 < 45):
        # CAUTION is driven by the turn detector (flags) + weak breadth, not by a
        # merely-neutral score: a trend-up, zero-divergence tape with lots of ⚪
        # neutral votes shouldn't cry wolf. score ≤ -2 is a net-bearish-votes
        # safety net that sits one notch above the 🔴 internals trigger (≤ -3).
        state, label = "🟡", "CAUTION"
        reason = f"Trend intact but {_plural(n_flags, 'divergence')} firing (score {score})"
        action = "Tighten trail stops, raise cash buffer; new money only on pullbacks, never chase 🔴"
    else:
        state, label = "🟢", "RISK-ON"
        reason = f"All layers confirm each other (score {score}, {_plural(n_flags, 'divergence')})"
        action = "Trend healthy, hold per rules; new money can scale in on pullbacks"

    return {
        "state": state, "state_label": label, "reason": reason, "action": action,
        "score": score, "n_bull": n_bull, "n_bear": n_bear,
        "uptrend_intact": uptrend_intact, "near_high": near_high,
        "signals": signals, "flags": flags, "n_flags": n_flags,
    }


# --------------------------------------------------------------------------- #
# History (one row per ET market day; atomic write; mirrors momentum-scan)
# --------------------------------------------------------------------------- #
def make_run_id(now: datetime) -> str:
    return now.astimezone(MARKET_TZ).strftime("%Y%m%d")


def load_history() -> pd.DataFrame:
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=HISTORY_COLS)
    df = pd.read_csv(HISTORY_FILE)
    if df.empty:
        return df
    df["run_id"] = df["run_id"].astype(str)
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True, format="ISO8601")
    return df


def clear_history():
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    if tmp.exists():
        tmp.unlink()


def _round(v, n=1):
    return None if v is None else round(v, n)


def history_row(run_id: str, run_date: datetime, m: dict, c: dict) -> dict:
    spy = m["spy_trend"] or {}
    return {
        "run_id": run_id,
        "run_date": run_date.isoformat(),
        "state": c["state_label"],
        "score": c["score"],
        "n_bull": c["n_bull"],
        "n_bear": c["n_bear"],
        "n_flags": c["n_flags"],
        "spy_vs_200_pct": _round(spy.get("vs_200_pct")),
        "ma200_slope_pct": _round(spy.get("ma200_slope_pct"), 2),
        "breadth_50_pct": _round(m["breadth_50_pct"], 0),
        "breadth_200_pct": _round(m["breadth_200_pct"], 0),
        "rsp_spy_pct": _round(m["rsp_spy_pct"]),
        "nhnl_pct": _round(m["nhnl"]["nhnl_pct"], 0) if m["nhnl"] else None,
        "vix": _round(m["vix"]),
        "vix_term": _round(m["vix_term"], 2),
        "credit_pct": _round(m["credit_pct"]),
        "def_off_pct": _round(m["def_off_pct"]),
        "flags": " ; ".join(c["flags"]),
    }


def append_history(row: dict, run_date: datetime):
    """Upsert one row keyed on the America/New_York date so re-running the same
    day refreshes rather than duplicates. Atomic tmp+rename like momentum-scan."""
    new = pd.DataFrame([row], columns=HISTORY_COLS)
    has_existing = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    existing = pd.read_csv(HISTORY_FILE) if has_existing else None
    if existing is not None and not existing.empty:
        today_et = run_date.astimezone(MARKET_TZ).date()
        ed = (pd.to_datetime(existing["run_date"], utc=True, format="ISO8601")
              .dt.tz_convert(MARKET_TZ).dt.date)
        existing = existing.loc[ed != today_et]
    combined = new if existing is None or existing.empty else pd.concat(
        [existing, new], ignore_index=True)
    combined = (combined.sort_values("run_date", kind="stable")
                .reset_index(drop=True))
    canonical = HISTORY_COLS + [c for c in combined.columns if c not in HISTORY_COLS]
    combined = combined.reindex(columns=canonical)
    tmp = HISTORY_FILE.with_suffix(".csv.tmp")
    combined.to_csv(tmp, index=False)
    tmp.replace(HISTORY_FILE)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
VOTE_EMOJI = {1: "🟢", 0: "⚪", -1: "🔴"}
LAYER_ORDER = ["Trend", "Breadth", "Vol", "Credit"]


def render_markdown(m: dict, c: dict, history: pd.DataFrame, run_id: str,
                    now: datetime) -> str:
    out = []
    out.append(f"# Regime scan — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    out.append("")
    out.append(f"## {c['state']} **{c['state_label']}** "
               f"(score {c['score']:+d} · {c['n_bull']}🟢/{c['n_bear']}🔴 · "
               f"{_plural(c['n_flags'], 'divergence')})")
    out.append(f"_{c['reason']}_")
    out.append(f"> **Action**: {c['action']}")
    out.append("")

    # Turn detector — lead with it, it's the whole point.
    if c["flags"]:
        out.append(f"### ⚠️ Turn warnings ({c['n_flags']})")
        for f in c["flags"]:
            out.append(f"- {f}")
    else:
        out.append("### ✅ No divergences — all layers agree with price")
    out.append("")

    # Layered signal table.
    out.append("### Layered signals")
    out.append("")
    out.append("| Layer | Signal | Reading | Vote |")
    out.append("|---|---|---|---|")
    layer_label = {"Trend": "Trend", "Breadth": "Breadth", "Vol": "Vol/Sentiment",
                   "Credit": "Credit/Rotation"}
    for layer in LAYER_ORDER:
        for s in c["signals"]:
            if s["layer"] == layer:
                out.append(f"| {layer_label[layer]} | {s['label']} | "
                           f"{s['reading']} | {VOTE_EMOJI[s['vote']]} |")
    out.append("")
    out.append(f"_Breadth pool: {m['n_breadth']} cross-sector large caps · slope/RS lookback "
               f"{m['lookback']}d_")

    # Trajectory — the real turn signal lives in the slope across days.
    traj = render_trajectory(history, run_id)
    if traj:
        out.append("")
        out.append(traj)
    return "\n".join(out)


def render_trajectory(history: pd.DataFrame, current_run_id: str) -> str | None:
    """Show how state / breadth / vix / credit moved over the last several runs
    — a single day's snapshot doesn't tell you if sentiment is *turning*; the
    slope does."""
    if history is None or history.empty:
        return None
    prior = history[history["run_id"] != current_run_id]
    if prior.empty:
        return None
    recent = prior.sort_values("run_date").tail(6)
    lines = ["### Recent trajectory (read the slope, not the single point)", "",
             "| Date | State | score | %>50DMA | %>200DMA | RSP/SPY | VIX | Credit | Flags |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in recent.iterrows():
        d = pd.to_datetime(r["run_date"]).astimezone(MARKET_TZ).strftime("%m-%d")
        def g(col, suf="", fmt="{:.0f}"):
            v = r.get(col)
            if pd.isna(v):
                return "—"
            return fmt.format(v) + suf
        lines.append(
            f"| {d} | {r['state']} | {int(r['score']):+d} | "
            f"{g('breadth_50_pct')} | {g('breadth_200_pct')} | "
            f"{g('rsp_spy_pct', '%', '{:+.1f}')} | {g('vix', '', '{:.1f}')} | "
            f"{g('credit_pct', '%', '{:+.1f}')} | {int(r['n_flags'])} |")
    return "\n".join(lines)


def show_history_summary(history: pd.DataFrame):
    if history.empty:
        print("No history yet.")
        return
    print(f"# Regime history — {history['run_id'].nunique()} run(s)\n")
    print("| Date | State | score | %>50 | %>200 | RSP/SPY | NH−NL | VIX | VIX term | Credit | Flags |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in history.sort_values("run_date").iterrows():
        d = pd.to_datetime(r["run_date"]).astimezone(MARKET_TZ).strftime("%Y-%m-%d")
        def g(col, suf="", fmt="{:.0f}"):
            v = r.get(col)
            return "—" if pd.isna(v) else fmt.format(v) + suf
        print(f"| {d} | {r['state']} | {int(r['score']):+d} | "
              f"{g('breadth_50_pct')} | {g('breadth_200_pct')} | "
              f"{g('rsp_spy_pct', '%', '{:+.1f}')} | {g('nhnl_pct', '%', '{:+.0f}')} | "
              f"{g('vix', '', '{:.1f}')} | {g('vix_term', '', '{:.2f}')} | "
              f"{g('credit_pct', '%', '{:+.1f}')} | {int(r['n_flags'])} |")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_argparser():
    ap = argparse.ArgumentParser(description="Daily market regime + sentiment-turn scan")
    ap.add_argument("--lookback", type=int, default=SLOPE_LOOKBACK_DEFAULT,
                    help=f"Sessions for slope / relative-strength windows "
                         f"(default {SLOPE_LOOKBACK_DEFAULT}, ~1 trading month).")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--show-history", action="store_true",
                    help="Print the daily state log and exit (no new scan).")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--no-save", action="store_true",
                    help="Don't append this run to history.")
    ap.add_argument("--save-stale", action="store_true",
                    help="Save even on a weekend / NYSE holiday (default skips "
                         "so duplicate-data days don't pollute the trajectory).")
    return ap


def main():
    args = build_argparser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear_history:
        clear_history()
        print("history.csv cleared.")
        return
    if args.show_history:
        show_history_summary(load_history())
        return

    breadth_universe = load_breadth_universe()
    if not breadth_universe:
        print("WARNING: breadth_universe.txt missing/empty — breadth signals "
              "will abstain.", file=sys.stderr)
    all_tickers = list(dict.fromkeys(MACRO_TICKERS + breadth_universe))
    bars = fetch_bars(all_tickers)
    macro = extract_closes(bars, MACRO_TICKERS)
    breadth = extract_closes(bars, breadth_universe)

    if macro.get("SPY") is None:
        print("ERROR: SPY data unavailable — cannot compute regime. Try again "
              "in a few minutes (Yahoo may be throttling).", file=sys.stderr)
        sys.exit(1)

    m = compute_metrics(macro, breadth, args.lookback)
    c = classify(m)

    history = load_history()
    now = datetime.now(timezone.utc)
    run_id = make_run_id(now)
    today_et = now.astimezone(MARKET_TZ).date()
    today_is_trading = is_nyse_trading_day(today_et)

    if not args.no_save:
        if not today_is_trading and not args.save_stale:
            print(f"Skipping history save: {today_et} is not an NYSE trading "
                  f"day. Pass --save-stale to override.", file=sys.stderr)
        else:
            append_history(history_row(run_id, now, m, c), now)

    if args.format == "json":
        print(json.dumps({
            "run_id": run_id, "run_date": now.isoformat(),
            "lookback": args.lookback, "metrics": m, "classification": c,
        }, indent=2, default=str))
        return

    print(render_markdown(m, c, history, run_id, now))


if __name__ == "__main__":
    main()
