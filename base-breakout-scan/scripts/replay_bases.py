#!/usr/bin/env python3
"""Survivorship-free replay of the base-breakout watchlist across several regimes.

backtest_outcomes.py replays what the scan actually published
(state/history.csv): point-in-time, but only as long as the history, which
is one regime. This script answers what history.csv can't: how the
watchlist's canonical trade, and the BaseWks >= 20 pocket, would have done
since --eval-start. It rebuilds the pick list for every session from raw
prices, reusing scan.py's own constants, defaults and score function, on a
point-in-time universe:

  - download pool: every US equity Yahoo lists TODAY above --pool-min-mcap
    ($500M), so names that were large then and have shrunk since are in it;
  - daily universe: mcap_t above scan's --min-market-cap, where mcap_t =
    split-adjusted close x today's share count, and 63-session average
    volume above scan's --min-volume (the screener's avgdailyvol3m).

RS ratings are percentile ranks across that day's universe, as
compute_rs_ratings ranks across the screener's. Residual bias, reported
rather than fixed: names delisted, acquired, or now below the pool floor
are absent; --universe-file restricts the pool to a ticker list, e.g.
state/universe.txt (today's large caps), to measure survivorship.

The trade is backtest_outcomes.py's canonical one: while a name stays on
the list (an episode = consecutive sessions), a buy-stop sits at the
latest pivot; it fills the first session the High touches the pivot, at
max(pivot, Open); exit on an 8% stop (the fill day judged by its close)
or at the close 20 sessions later. Episodes still listed, or whose window
runs past the data, are left out.

It reports
  - per-episode strata (start base length, score, pivot distance, volume
    dry-up, RS slope, fill gap, regime) with the trade's excess over SPY
    from the fill session's open to the exit close, averaged within each
    trigger session and then across sessions;
  - portfolio curves, net of --cost-bps per round trip: K slots of 1/K
    equity filled by score, one position per ticker, idle slots in cash
    or in SPY.

Run (reuses replay_board.py's cache format; the first run downloads ~3000
tickers x the span, a few minutes):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \\
    python replay_bases.py [--eval-start 2021-01-04] [--refresh] \\
    [--cache /path/to/bars.pkl] [--universe-file ../state/universe.txt]
"""

from __future__ import annotations

import argparse
import pickle
import sys
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

import scan

SCAN = scan.build_argparser().parse_args([])             # scan.py's defaults
MIN_DAYS = int(SCAN.min_base_weeks * 5)
MAX_DAYS = int(SCAN.max_base_weeks * 5)
LENGTHS = np.arange(MIN_DAYS, MAX_DAYS + 1, 5)            # detect_base's stride
MODES = (0, scan.BASE_BREAKOUT_DETECT_BUFFER_DAYS)
LOOKBACK = scan.LOOKBACK_FOR_BASE_SEARCH                 # 252
RS_MIN_BARS = max(scan.RS_PERIODS_TRADING_DAYS) + 1
TT_MIN_BARS = scan.TT_MA200_PERIOD + scan.TT_MA200_SLOPE_LOOKBACK_DAYS
VC_DAYS = max(int(round(scan.VOL_COLLAPSE_LOOKBACK_MONTHS * 21)), 21)
HORIZON = 20                                             # backtest_outcomes' DEFAULT_HORIZON
STOP_PCT = 8.0                                           # and DEFAULT_STOP_PCT
POCKET_WEEKS = scan.LONG_BASE_WEEKS
ADV_DAYS = 63
FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


# ------------------------------------------------------------------ data

def fetch_pool(min_mcap: float, min_volume: float) -> pd.DataFrame:
    """Today's US equities above the pool floor, with share count = mcap / price."""
    from yfinance import EquityQuery

    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_mcap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    rows, offset = [], 0
    for page in range(scan.SCREENER_MAX_PAGES):
        if page:
            time.sleep(scan.SCREENER_PAGE_SLEEP_SEC)
        raw, paged = scan._screen_with_offset(query, scan.SCREENER_PAGE_SIZE, offset)
        quotes = raw.get("quotes") or []
        rows += [(q.get("symbol"), q.get("marketCap"), q.get("regularMarketPrice"),
                  q.get("quoteType")) for q in quotes]
        if not paged or len(quotes) < scan.SCREENER_PAGE_SIZE:
            break
        offset += scan.SCREENER_PAGE_SIZE
    df = pd.DataFrame(rows, columns=["symbol", "mcap", "price", "type"]).dropna()
    df = df[df.type == "EQUITY"].drop_duplicates("symbol")
    if df.empty:
        raise RuntimeError("Yahoo screener returned no pool; rate-limited? retry later")
    return pd.DataFrame({"symbol": df.symbol, "shares": df.mcap / df.price})


def fetch_bars(tickers: list[str], start: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    parts = []
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        print(f"fetching {i + 1}-{i + len(chunk)} of {len(tickers)}...", file=sys.stderr)
        parts.append(yf.download(chunk, start=start, interval="1d", auto_adjust=False,
                                 group_by="column", progress=False, threads=True))
        time.sleep(1.0)
    raw = pd.concat(parts, axis=1)
    raw = raw.loc[:, ~raw.columns.duplicated()]
    return {f: raw[f] for f in FIELDS}


def load_data(cache: Path, fetch_start: str, pool_min_mcap: float,
              refresh: bool) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            d = pickle.load(f)
        last = d["bars"]["Close"].index.max()
        fresh = ((pd.Timestamp.today().normalize() - last).days <= 5
                 and d["fetch_start"] <= fetch_start and d["pool_min_mcap"] <= pool_min_mcap)
        if fresh:
            return d["pool"], complete_sessions(d["bars"], d["fetched_at"])
        print(f"cache {cache} is stale (last bar {last.date()}); refetching", file=sys.stderr)
    pool = fetch_pool(pool_min_mcap, 2e5)
    print(f"pool: {len(pool)} tickers above ${pool_min_mcap / 1e9:g}B", file=sys.stderr)
    fetched_at = datetime.now(scan.MARKET_TZ)
    bars = fetch_bars(sorted(pool.symbol) + ["SPY"], fetch_start)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump(dict(pool=pool, bars=bars, fetch_start=fetch_start,
                         pool_min_mcap=pool_min_mcap, fetched_at=fetched_at), f)
    return pool, complete_sessions(bars, fetched_at)


def complete_sessions(bars: dict[str, pd.DataFrame],
                      fetched_at: datetime) -> dict[str, pd.DataFrame]:
    """Drop a session that was still trading when the bars were fetched (ET),
    and any session Yahoo had only half-published."""
    close = bars["Close"]
    n = close.notna().sum(axis=1)
    keep = n > 0.5 * n.max()
    if (fetched_at.hour, fetched_at.minute) < (16, 15):
        keep &= close.index < pd.Timestamp(fetched_at.date())
    return {f: df[keep] for f, df in bars.items()}


def point_in_time_universe(close_raw: pd.DataFrame, volume: pd.DataFrame,
                           shares: pd.Series) -> pd.DataFrame:
    mcap = close_raw * shares.reindex(close_raw.columns).to_numpy()
    return (mcap > SCAN.min_market_cap) & (volume.rolling(ADV_DAYS).mean() > SCAN.min_volume)


# ------------------------------------------------------------ the scan

def best_bases(a: np.ndarray) -> dict[str, np.ndarray]:
    """detect_base's window search for every session of one close array.

    Every window length in LENGTHS ending today (mode 0) or three sessions
    ago with today clearing its high (mode 3), width under the ceiling;
    the winner maximizes days / max(width, 1), first-listed winning a tie
    (mode 0 before mode 3, shorter before longer), as detect_base's strict
    '>' does. Sessions with fewer than LOOKBACK bars, or more than
    --max-to-52w-high below the 252-session high, get no base."""
    n = len(a)
    s = pd.Series(a)
    nm = len(MODES) * len(LENGTHS)
    Q = np.full((n, nm), -1.0)
    HI = np.full((n, nm), np.nan)
    LO = np.full((n, nm), np.nan)
    W = np.full((n, nm), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        for li, L in enumerate(LENGTHS):
            mx = s.rolling(L).max().to_numpy()
            mn = s.rolling(L).min().to_numpy()
            for mi, m in enumerate(MODES):
                hi = np.r_[np.full(m, np.nan), mx[:n - m]] if m else mx
                lo = np.r_[np.full(m, np.nan), mn[:n - m]] if m else mn
                width = (hi - lo) / hi * 100
                ok = (hi > 0) & (width <= SCAN.max_base_width)
                if m:
                    ok &= a >= hi * 1.002
                col = mi * len(LENGTHS) + li
                Q[:, col] = np.where(ok, L / np.maximum(width, 1.0), -1.0)
                HI[:, col], LO[:, col], W[:, col] = hi, lo, width
        hi52 = s.rolling(LOOKBACK).max().to_numpy()
        near_high = (a / hi52 - 1) * 100 >= -SCAN.max_to_52w_high
    best = Q.argmax(axis=1)
    rows = np.arange(n)
    valid = (Q[rows, best] > 0) & (np.arange(1, n + 1) >= LOOKBACK) & near_high
    mode = np.asarray(MODES)[best // len(LENGTHS)]
    days = LENGTHS[best % len(LENGTHS)]
    pivot = HI[rows, best]
    with np.errstate(invalid="ignore", divide="ignore"):
        to_pivot = (a / pivot - 1) * 100
    return dict(valid=valid, mode=mode, days=days, pivot=pivot, width=W[rows, best],
                to_pivot=to_pivot, anchor=rows - mode - days + 1,
                is_breakout=(mode > 0) | (to_pivot >= -0.2),
                to_52w_high=(a / hi52 - 1) * 100)


def trend_template(s: pd.Series) -> np.ndarray:
    """passes_trend_template's six price criteria (RS is cross-sectional)."""
    a = s.to_numpy(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ma50 = s.rolling(scan.TT_MA50_PERIOD).mean().to_numpy()
        ma150 = s.rolling(scan.TT_MA150_PERIOD).mean().to_numpy()
        ma200 = s.rolling(scan.TT_MA200_PERIOD).mean()
        slope = ((ma200 / ma200.shift(scan.TT_MA200_SLOPE_LOOKBACK_DAYS) - 1) * 100).to_numpy()
        ma200 = ma200.to_numpy()
        hi52 = s.rolling(252, min_periods=1).max().to_numpy()
        lo52 = s.rolling(252, min_periods=1).min().to_numpy()
        return ((np.arange(1, len(a) + 1) >= TT_MIN_BARS)
                & (a > ma150) & (ma150 > ma200) & (slope > 0)
                & (ma50 > ma150) & (a > ma50)
                & ((a / lo52 - 1) * 100 >= scan.TT_MIN_DIST_FROM_52W_LOW_PCT)
                & ((a / hi52 - 1) * 100 >= -SCAN.max_to_52w_high))


def rs_raw(a: np.ndarray) -> np.ndarray:
    n = len(a)
    out = np.zeros(n)
    with np.errstate(invalid="ignore", divide="ignore"):
        for p, w in zip(scan.RS_PERIODS_TRADING_DAYS, scan.RS_WEIGHTS):
            back = np.full(n, np.nan)
            back[p:] = a[:max(n - p, 0)]
            out += w * (a / back - 1)
    out[:RS_MIN_BARS - 1] = np.nan
    return out


def bb_pctile(s: pd.Series) -> np.ndarray:
    """compute_bb_pctile at every session."""
    w = (4 * s.rolling(scan.BB_PERIOD).std() / s.rolling(scan.BB_PERIOD).mean()).to_numpy()
    out = np.full(len(s), np.nan)
    first = scan.BB_PERIOD - 1
    lb = scan.BB_PCTILE_LOOKBACK
    if len(s) < scan.BB_PERIOD + lb:
        return out
    win = sliding_window_view(w[first:], lb)                 # (n - first - lb + 1, lb)
    pct = (win < win[:, -1:]).sum(axis=1) / lb * 100
    out[first + lb - 1:] = np.round(pct, 1)
    out[:scan.BB_PERIOD + lb - 1] = np.nan                  # len(close) < period + lookback
    return out


def three_weeks_tight(s: pd.Series) -> np.ndarray:
    """three_weeks_tight at every session: the session's own (partial) week
    plus the last close of each of the two weeks before it."""
    a = s.to_numpy(float)
    code = s.index.to_period("W-FRI").asi8
    n = len(a)
    first = np.zeros(n, dtype=int)
    for i in range(1, n):
        first[i] = i if code[i] != code[i - 1] else first[i - 1]
    p1 = first - 1
    p2 = np.where(p1 >= 0, first[np.maximum(p1, 0)] - 1, -1)
    ok = (p2 >= 0) & (np.arange(1, n + 1) >= 15)
    trio = np.stack([a, a[np.maximum(p1, 0)], a[np.maximum(p2, 0)]], axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        spread = (trio.max(1) - trio.min(1)) / trio.mean(1) * 100
    return ok & (spread <= scan.THREE_WEEKS_TIGHT_THRESHOLD_PCT)


def vol_dryup(v: pd.Series) -> np.ndarray:
    """detect_base's vol_dryup_ratio: last-20 mean over the 60 before, NaN-skipping."""
    recent = v.rolling(20, min_periods=1).mean()
    trailing = v.rolling(60, min_periods=1).mean().shift(20)
    r = (recent / trailing.where(trailing > 0)).to_numpy().copy()
    r[: min(80, len(r)) - 1] = np.nan
    return np.round(r, 2)


def vol_collapsed(s: pd.Series) -> np.ndarray:
    """_check_vol_collapse at every session (VC_DAYS closes, halves of the returns)."""
    r = s.pct_change()
    nret = VC_DAYS - 1
    mid = nret // 2
    v2 = r.rolling(nret - mid).std() * np.sqrt(252) * 100
    v1 = r.shift(nret - mid).rolling(mid).std() * np.sqrt(252) * 100
    return ((v1 >= scan.VOL_COLLAPSE_MIN_FIRST_VOL_PCT)
            & (v2 / v1 < SCAN.vol_collapse_ratio)).to_numpy()


def ticker_rows(close: pd.Series, volume: pd.Series, spy: pd.Series) -> tuple[np.ndarray, pd.DataFrame]:
    """(rs_raw on the panel's dates, candidate rows): every session where the
    price trend template passes and a base exists, with what score_tickers
    computes for it. RS gates later, across the day's universe."""
    s = close.dropna()
    a = s.to_numpy(float)
    raw = pd.Series(rs_raw(a), index=s.index).reindex(close.index).to_numpy()
    if len(a) < LOOKBACK:
        return raw, pd.DataFrame()
    tt = trend_template(s)
    b = best_bases(a)
    cand = np.nonzero(tt & b["valid"])[0]
    if not len(cand):
        return raw, pd.DataFrame()
    v = volume.reindex(s.index)
    bb = bb_pctile(s)[cand]
    vd = vol_dryup(v)[cand]
    tw = three_weeks_tight(s)[cand]
    vc = vol_collapsed(s)[cand]
    sp = spy.reindex(s.index).to_numpy(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = a / sp
    rs0 = np.where(np.isfinite(rs), rs, 0.0)
    k = np.arange(len(a), dtype=float)
    P0, P1 = np.r_[0.0, np.cumsum(rs0)], np.r_[0.0, np.cumsum(k * rs0)]
    out = []
    for j, t in enumerate(cand):
        m, d, an = int(b["mode"][t]), int(b["days"][t]), int(b["anchor"][t])
        bars = a[an:t - m + 1]
        mean = bars.mean()
        lo, hi = mean * (1 - SCAN.smoothness_band_pct / 100), mean * (1 + SCAN.smoothness_band_pct / 100)
        smooth = round(float(((bars >= lo) & (bars <= hi)).sum() / len(bars) * 100), 1)
        N = t - an + 1
        slope = None
        if N >= 10 and rs[an] > 0 and np.isfinite(rs[an:t + 1]).all():
            Sy, Sky = P0[t + 1] - P0[an], P1[t + 1] - P1[an]
            Sxy = Sky - an * Sy
            Sx, Sxx = N * (N - 1) / 2, (N - 1) * N * (2 * N - 1) / 6
            slope = float((N * Sxy - Sx * Sy) / (N * Sxx - Sx * Sx) / rs[an] * 5 * 100)
        base = dict(width_pct=round(float(b["width"][t]), 1),
                    to_pivot_pct=round(float(b["to_pivot"][t]), 2),
                    vol_dryup_ratio=None if not np.isfinite(vd[j]) or vd[j] == 0 else float(vd[j]),
                    smoothness_pct=smooth)
        score = scan.compute_base_score(base, None if np.isnan(bb[j]) else float(bb[j]),
                                        slope, bool(tw[j]))
        out.append((s.index[t], score, d / 5.0, base["width_pct"], base["to_pivot_pct"],
                    round(float(b["pivot"][t]), 2), bb[j], base["vol_dryup_ratio"],
                    slope, bool(tw[j]), bool(vc[j]), bool(b["is_breakout"][t]), smooth))
    df = pd.DataFrame(out, columns=["date", "score", "base_weeks", "width", "to_pivot", "pivot",
                                    "bb", "vol_dryup", "rs_slope", "tight", "collapsed",
                                    "is_breakout", "smooth"])
    return raw, df


def rs_ratings(raw: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """compute_rs_ratings for every session: percentile rank of the weighted
    return among that day's universe names with enough history, 1..99."""
    r = raw.where(universe.reindex_like(raw).fillna(False).astype(bool))
    rank = r.rank(axis=1, method="first")
    return rank.div(r.notna().sum(axis=1), axis=0) * 99


def pick_lists(cands: pd.DataFrame, rs: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """score_tickers' gates, its same-issuer dedup, then filter_vol_collapse."""
    c = cands.copy()
    di = rs.index.get_indexer(c.date)
    ci = rs.columns.get_indexer(c.ticker)
    uni = universe.reindex_like(rs).fillna(False).astype(bool).to_numpy()
    c["in_universe"] = uni[di, ci]
    c["rs"] = rs.to_numpy()[di, ci]
    c = c[c.in_universe & (c.rs >= SCAN.min_rs_rating) & (c.score >= SCAN.min_base_score)]
    drop = []
    for a_, b_ in scan.SAME_ISSUER_PAIRS:
        both = c[c.ticker.isin([a_, b_])]
        for _, g in both.groupby("date"):
            if len(g) == 2:
                ga, gb = g[g.ticker == a_].iloc[0], g[g.ticker == b_].iloc[0]
                drop.append(gb.name if ga.score >= gb.score else ga.name)
    c = c.drop(index=drop)
    c = c[~c.collapsed]
    return c.drop(columns=["in_universe", "collapsed"]).sort_values(["date", "score"],
                                                                      ascending=[True, False])


# ---------------------------------------------------------- episodes

def episodes(picks: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Runs of consecutive sessions on the list, per ticker, with the
    start-day attributes and the pivot of every listed day."""
    pos = pd.Series(np.arange(len(dates)), index=dates)
    p = picks.assign(i=pos.reindex(picks.date).to_numpy()).sort_values(["ticker", "i"])
    rows = []
    for t, g in p.groupby("ticker", sort=False):
        i = g.i.to_numpy()
        breaks = np.r_[0, np.nonzero(np.diff(i) > 1)[0] + 1, len(i)]
        for b0, b1 in zip(breaks[:-1], breaks[1:]):
            first = g.iloc[b0]
            rows.append(dict(ticker=t, start=int(i[b0]), end=int(i[b1 - 1]),
                             pivots=g["pivot"].to_numpy()[b0:b1], score=first.score,
                             base_weeks=first.base_weeks, to_pivot=first.to_pivot,
                             width=first.width, bb=first.bb, vol_dryup=first.vol_dryup,
                             rs_slope=first.rs_slope, rs=first.rs, tight=first.tight))
    return pd.DataFrame(rows)


def resolve(ep: pd.DataFrame, O, H, L, C, col: dict, spyO, spyC, T: int) -> pd.DataFrame:
    """backtest_outcomes.resolve_episode's touch-entry trade for every episode."""
    out = []
    for e in ep.itertuples(index=False):
        j = col[e.ticker]
        last_watch = e.end + 1
        if last_watch >= T:
            continue                                     # still listed at the data edge
        trig = None
        for d in range(e.start + 1, last_watch + 1):
            piv = e.pivots[min(d - 1, e.end) - e.start]
            if np.isfinite(H[d, j]) and H[d, j] >= piv:
                trig, fill = d, max(piv, O[d, j]) if np.isfinite(O[d, j]) else piv
                break
        r = dict(e._asdict())
        r.pop("pivots")
        r["triggered"] = trig is not None
        if trig is None:
            out.append(r)
            continue
        if trig + HORIZON >= T:
            continue                                     # trade still open
        piv = e.pivots[min(trig - 1, e.end) - e.start]
        stop = fill * (1 - STOP_PCT / 100)
        win_l = L[trig:trig + HORIZON + 1, j].copy()
        win_l[0] = C[trig, j]
        hit = np.nonzero(win_l <= stop)[0]
        r.update(trig=trig, fill=fill, gap=(fill / piv - 1) * 100,
                 ret_h=(C[trig + HORIZON, j] / fill - 1) * 100,
                 fellback5=bool((C[trig + 1:trig + 6, j] < piv).any()))
        if len(hit):
            k = int(hit[0])
            px = C[trig, j] if k == 0 else min(stop, O[trig + k, j])
            r.update(stop_hit=True, exit=k, trade=-STOP_PCT, trade_gap=(px / fill - 1) * 100,
                     exit_px=px)
        else:
            r.update(stop_hit=False, exit=HORIZON, trade=r["ret_h"], trade_gap=r["ret_h"],
                     exit_px=C[trig + HORIZON, j])
        r["spy"] = (spyC[trig + r["exit"]] / spyO[trig] - 1) * 100
        r["excess"] = r["trade_gap"] - r["spy"]
        out.append(r)
    return pd.DataFrame(out)


# -------------------------------------------------------------- portfolio

def trade_daily_returns(tr: pd.DataFrame, C: np.ndarray, col: dict,
                        cost_bps: float) -> list[tuple[int, np.ndarray]]:
    """(fill session, daily returns): the fill to that session's close, then
    close to close, the exit session to the exit price, net of the cost."""
    out = []
    for t, k0, x, fill, px in zip(tr.ticker, tr.trig, tr.exit, tr.fill, tr.exit_px):
        j = col[t]
        path = C[k0:k0 + x + 1, j].copy()
        path[-1] = px
        prev = np.r_[fill, path[:-1]]
        r = path / prev - 1
        r[-1] -= cost_bps / 1e4
        out.append((int(k0), np.nan_to_num(r)))
    return out


def slotted(tr: pd.DataFrame, daily: list[tuple[int, np.ndarray]], T: int, k: int,
            idle: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """K slots of 1/K equity; a session's fills take free slots by score,
    one position per ticker; idle slots earn `idle` (zeros = cash)."""
    order = np.lexsort((-tr.score.to_numpy(), tr.trig.to_numpy()))
    tick = tr.ticker.to_numpy()
    held: list[tuple[int, str]] = []
    s, n = np.zeros(T), np.zeros(T)
    for q in order:
        k0, r = daily[q]
        held = [(f, t) for f, t in held if f > k0]
        if len(held) >= k or any(t == tick[q] for _, t in held):
            continue
        held.append((k0 + len(r), tick[q]))
        s[k0:k0 + len(r)] += r / k
        n[k0:k0 + len(r)] += 1
    return s + (1 - n / k) * idle, n


def perf(r: pd.Series, bench: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    b = bench.reindex(r.index).fillna(0.0)
    X = np.column_stack([np.ones(len(b)), b.to_numpy()])
    coef, *_ = np.linalg.lstsq(X, r.to_numpy(), rcond=None)
    resid = r.to_numpy() - X @ coef
    se = resid.std(ddof=2) / np.sqrt(len(resid))
    se = se if se > 1e-12 else np.nan
    vol = r.std() * np.sqrt(252)
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    return dict(total=eq.iloc[-1] - 1, cagr=cagr, vol=vol, sharpe=cagr / vol if vol else np.nan,
                maxdd=(eq / eq.cummax() - 1).min(), beta=coef[1], alpha=coef[0] * 252,
                alpha_t=coef[0] / se,
                years={y: (1 + g).prod() - 1 for y, g in r.groupby(r.index.year)})


# ---------------------------------------------------------------- report

def pct(v: float) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.1f}%"


def pp(v: float) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:+.2f}%"


def by_day(x: pd.DataFrame) -> tuple[float, float]:
    """Excess over SPY averaged within each fill session, then across them:
    breakouts cluster on strong market days."""
    g = x.groupby("trig").excess.mean()
    if len(g) < 3:
        return (g.mean() if len(g) else np.nan), np.nan
    return g.mean(), g.mean() / (g.std(ddof=1) / np.sqrt(len(g)))


def strata(title: str, groups: list[tuple[str, pd.DataFrame]], yrs: list[int]) -> list[str]:
    L = ["", f"### {title}", "",
         "| stratum | episodes | trig% | fills | win% (+20d) | ret +20d | stop% | trade | trade gap-aware | "
         "vs SPY by day (t) | " + " | ".join(map(str, yrs)) + " |",
         "|---|---|---|---|---|---|---|---|---|---|" + "---|" * len(yrs)]
    for name, e in groups:
        if len(e) == 0:
            continue
        f = e[e.triggered]
        if len(f) == 0:
            continue
        m, t = by_day(f)
        L.append(f"| {name} | {len(e)} | {len(f) / len(e):.0%} | {len(f)} | {(f.ret_h > 0).mean():.0%} | "
                 f"{pp(f.ret_h.mean())} | {f.stop_hit.mean():.0%} | {pp(f.trade.mean())} | "
                 f"{pp(f.trade_gap.mean())} | {pp(m)} ({t:+.1f}) | "
                 + " | ".join(pp(by_day(f[f.year == y])[0]) for y in yrs) + " |")
    return L


def report(E: pd.DataFrame, curves: dict, meta: dict) -> str:
    yrs = sorted(E.year.unique())
    bw = POCKET_WEEKS
    def g(name, f):
        return (name, f(E))
    L = [f"# Base-breakout watchlist replay, {meta['first']} → {meta['last']}", "",
         f"Point-in-time universe: ~{meta['uni']:.0f} names/day from a {meta['pool']}-ticker pool"
         f"{meta['note']}. Picks mirror scan.py's defaults (RS ≥ {SCAN.min_rs_rating:g}, trend template, "
         f"bases {SCAN.min_base_weeks:g}-{SCAN.max_base_weeks:g} weeks ≤ {SCAN.max_base_width:g}% wide, "
         f"score ≥ {SCAN.min_base_score:g}, vol-collapse {SCAN.vol_collapse_ratio}); ~{meta['per_day']:.0f} "
         f"picks/day, {meta['n_ep']} closed episodes.", "",
         f"Trade: buy-stop at the latest pivot while listed (and one session after), fill at "
         f"max(pivot, open), exit on an {STOP_PCT:g}% stop (the fill day judged by its close) or the "
         f"close {HORIZON} sessions on. 'trade' books a stop at exactly −{STOP_PCT:g}% "
         "(backtest_outcomes' convention); 'gap-aware' fills it at min(stop, open). Strata read the "
         "episode's first listed day. The verdict column is gap-aware trade minus SPY from the fill "
         "session's open to the exit close, averaged per fill session, then across sessions; the year "
         "columns are that number per year, gross of costs."]
    L += strata("Headline", [
        g("all episodes", lambda d: d),
        g(f"BaseWks ≥ {bw:g} (the pocket)", lambda d: d[d.base_weeks >= bw]),
        g(f"BaseWks < {bw:g}", lambda d: d[d.base_weeks < bw]),
    ], yrs)
    L += strata("By base length at the first listing", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.base_weeks >= lo) & (d.base_weeks < hi)])
        for n, lo, hi in (("6–10 wk", 0, 10), ("10–20 wk", 10, 20), ("20–30 wk", 20, 30),
                          ("30–40 wk", 30, 99))], yrs)
    L += strata("By base score", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.score >= lo) & (d.score < hi)])
        for n, lo, hi in (("40–50", 40, 50), ("50–60", 50, 60), ("60–70", 60, 70), ("≥ 70", 70, 999))], yrs)
    L += strata("By distance to the pivot at the first listing", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.to_pivot >= lo) & (d.to_pivot < hi)])
        for n, lo, hi in (("≥ −3% (near)", -3, 99), ("−10..−3%", -10, -3), ("< −10%", -99, -10))], yrs)
    L += strata("By volume dry-up at the first listing", [
        g("< 0.70 (deep)", lambda d: d[d.vol_dryup < 0.7]),
        g("0.70–0.90", lambda d: d[(d.vol_dryup >= 0.7) & (d.vol_dryup < 0.9)]),
        g("≥ 0.90 (none)", lambda d: d[d.vol_dryup >= 0.9]),
        g("< 10 wk & < 0.90 (the toxic cell)", lambda d: d[(d.base_weeks < 10) & (d.vol_dryup < 0.9)]),
    ], yrs)
    L += strata("By RS slope during the base (%/wk)", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.rs_slope >= lo) & (d.rs_slope < hi)])
        for n, lo, hi in (("< 0", -99, 0), ("0–1 (sweet spot)", 0, 1), ("≥ 1", 1, 99))], yrs)
    L += strata("By fill gap over the pivot", [
        g("≤ 3%", lambda d: d[~d.triggered | (d.gap <= 3)]),
        g("> 3% (gapped through)", lambda d: d[~d.triggered | (d.gap > 3)]),
    ], yrs)
    L += strata("By market regime at the first listing (SPY above a rising 200DMA)", [
        g("RISK-ON", lambda d: d[d.risk_on]), g("not RISK-ON", lambda d: d[~d.risk_on]),
        g(f"pocket & RISK-ON", lambda d: d[(d.base_weeks >= bw) & d.risk_on]),
        g(f"pocket & not RISK-ON", lambda d: d[(d.base_weeks >= bw) & ~d.risk_on])], yrs)

    L += ["", f"## Portfolios (fills only, net of {meta['cost']:g}bp round trip)", "",
          "| strategy | total / CAGR / vol / maxDD / Sharpe | beta | alpha/yr vs SPY (t) | avg open | "
          "days invested |", "|---|---|---|---|---|---|"]
    for k, v in curves.items():
        c = v["c"]
        t = f"{c['alpha_t']:+.1f}" if np.isfinite(c["alpha_t"]) else "—"
        L.append(f"| {k} | {pct(c['total'])} / {pct(c['cagr'])} / {c['vol'] * 100:.0f}% / "
                 f"{pct(c['maxdd'])} / {c['sharpe']:.2f} | {c['beta']:.2f} | {pct(c['alpha'])} ({t}) | "
                 f"{v['n']:.1f} | {v['inv']:.0%} |")
    cy = sorted(next(iter(curves.values()))["c"]["years"])
    L += ["", "### Calendar years", "",
          "| strategy | " + " | ".join(map(str, cy)) + " |", "|---|" + "---|" * len(cy)]
    for k, v in curves.items():
        L.append(f"| {k} | " + " | ".join(pct(v["c"]["years"].get(y, np.nan)) for y in cy) + " |")
    return "\n".join(L)


# ------------------------------------------------------------------ main

def scan_panel(C: pd.DataFrame, V: pd.DataFrame, spy: pd.Series,
               universe: pd.DataFrame, progress: bool = False) -> pd.DataFrame:
    """Every session's pick list, as scan.py would have published it."""
    raws, parts = {}, []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, t in enumerate(C.columns):
            if progress and i and i % 500 == 0:
                print(f"  {i}/{len(C.columns)} tickers", file=sys.stderr)
            raw, rows = ticker_rows(C[t], V[t], spy)
            raws[t] = raw
            if len(rows):
                parts.append(rows.assign(ticker=t))
    raw = pd.DataFrame(raws, index=C.index)
    rs = rs_ratings(raw, universe)
    if not parts:
        return pd.DataFrame(columns=["date", "ticker", "score"])
    return pick_lists(pd.concat(parts, ignore_index=True), rs, universe)


def build(bars: dict, pool: pd.DataFrame, tickers: list[str]) -> dict:
    adj = bars["Adj Close"] / bars["Close"]
    shares = pool.set_index("symbol").shares
    C = bars["Adj Close"][tickers]
    O, H, Lo = ((bars[f] * adj)[tickers] for f in ("Open", "High", "Low"))
    V = bars["Volume"][tickers]
    universe = point_in_time_universe(bars["Close"][tickers], V, shares)
    spy = bars["Adj Close"]["SPY"]
    picks = scan_panel(C, V, spy, universe, progress=True)
    return dict(C=C, O=O, H=H, L=Lo, universe=universe, picks=picks, spy=spy,
                spyO=bars["Open"]["SPY"] * adj["SPY"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-start", default="2021-01-04", help="first session an episode may start")
    ap.add_argument("--fetch-start", default=None,
                    help="price download start (default: one year before --eval-start, for the 252-session rules)")
    ap.add_argument("--pool-min-mcap", type=float, default=5e8,
                    help="download pool floor on TODAY's market cap (keep well under scan's --min-market-cap)")
    ap.add_argument("--slots", type=int, default=10, help="K for the slot portfolios")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="round-trip cost per trade")
    ap.add_argument("--universe-file", type=Path, help="restrict the pool to these tickers (one per line)")
    ap.add_argument("--episodes-out", type=Path, help="write every closed episode to this CSV")
    ap.add_argument("--picks-out", type=Path, help="write every session's pick list to this CSV")
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "bb_replay_bars.pkl")
    ap.add_argument("--refresh", action="store_true", help="refetch pool and bars")
    args = ap.parse_args()

    eval_start = pd.Timestamp(args.eval_start)
    fetch_start = args.fetch_start or (eval_start - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    pool, bars = load_data(args.cache, fetch_start, args.pool_min_mcap, args.refresh)
    tickers = [t for t in bars["Close"].columns if t in set(pool.symbol)]
    note = ""
    if args.universe_file:
        keep = {l.strip() for l in args.universe_file.read_text().splitlines() if l.strip()}
        tickers = [t for t in tickers if t in keep]
        note += f", restricted to {args.universe_file.name} ({len(tickers)} tickers)"
    print(f"scanning {len(tickers)} tickers x {len(bars['Close'])} sessions...", file=sys.stderr)
    B = build(bars, pool, tickers)
    if args.picks_out:
        B["picks"].to_csv(args.picks_out, index=False)

    dates = B["C"].index
    T, S = len(dates), dates.searchsorted(eval_start)
    col = {t: j for j, t in enumerate(tickers)}
    arr = lambda df: df.to_numpy(dtype=float)
    Oa, Ha, La, Ca = arr(B["O"]), arr(B["H"]), arr(B["L"]), arr(B["C"])
    spyC, spyO = B["spy"].to_numpy(float), B["spyO"].to_numpy(float)
    ma = B["spy"].rolling(200).mean()
    risk_on = ((B["spy"] > ma) & ((ma / ma.shift(scan.MA200_SLOPE_LOOKBACK_DAYS) - 1) * 100
                                 > scan.MA200_SLOPE_RISK_ON_THRESHOLD_PCT)).to_numpy()
    ep = episodes(B["picks"], dates)
    ep = ep[ep.start >= S].reset_index(drop=True)
    E = resolve(ep, Oa, Ha, La, Ca, col, spyO, spyC, T)
    E["date"] = dates[E.start.to_numpy()]
    E["year"] = E.date.dt.year
    E["risk_on"] = risk_on[E.start.to_numpy()]
    if args.episodes_out:
        E.to_csv(args.episodes_out, index=False)

    F = E[E.triggered].reset_index(drop=True)
    F["trig"] = F.trig.astype(int)
    F["exit"] = F.exit.astype(int)
    end = int(F.trig.max() + F.exit.max()) + 1 if len(F) else T
    idx = dates[S:min(end, T)]
    spy_c = pd.Series(spyC[1:] / spyC[:-1] - 1, index=dates[1:]).reindex(idx).fillna(0.0)
    spy_daily = np.r_[0.0, spyC[1:] / spyC[:-1] - 1]
    K = args.slots
    curves = {}
    for name, sel, idle in ((f"all fills, {K} slots, idle cash", lambda d: d, np.zeros(T)),
                            (f"all fills, {K} slots, idle in SPY", lambda d: d, spy_daily),
                            (f"pocket fills, {K} slots, idle cash",
                             lambda d: d[d.base_weeks >= POCKET_WEEKS], np.zeros(T)),
                            (f"pocket fills, {K} slots, idle in SPY",
                             lambda d: d[d.base_weeks >= POCKET_WEEKS], spy_daily)):
        tr = sel(F).reset_index(drop=True)
        r, n = slotted(tr, trade_daily_returns(tr, Ca, col, args.cost_bps), T, K, idle)
        curves[name] = dict(c=perf(pd.Series(r, index=dates).reindex(idx), spy_c),
                            n=n[S:len(idx) + S].mean(), inv=(n[S:len(idx) + S] > 0).mean())
    curves["SPY"] = dict(c=perf(spy_c, spy_c), n=1.0, inv=1.0)

    pk = B["picks"]
    meta = dict(first=dates[S].date(), last=dates[min(end, T) - 1].date(), pool=len(tickers), note=note,
                uni=B["universe"].iloc[S:].sum(axis=1).mean(), cost=args.cost_bps,
                per_day=pk[pk.date >= eval_start].groupby("date").size().reindex(dates[S:]).fillna(0).mean(),
                n_ep=len(E))
    print(report(E, curves, meta))


if __name__ == "__main__":
    main()
