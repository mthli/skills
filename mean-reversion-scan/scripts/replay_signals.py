#!/usr/bin/env python3
"""Survivorship-free replay of the mean-reversion signals across several regimes.

backtest_outcomes.py replays what the scan actually published
(state/history.csv): point-in-time, but only as long as the history, which
is one regime. This script answers what history.csv can't: how the signals
and the once-validated pocket would have done since --eval-start. It rebuilds
the scan's pick list for every session from raw prices, reusing scan.py's
own constants and argparse defaults (a parameter change there flows
through here), on a point-in-time universe:

  - download pool: every US equity Yahoo lists TODAY above --pool-min-mcap
    ($500M), so names that were large then and have shrunk since are in it;
  - daily universe: mcap_t above scan's --min-market-cap, where mcap_t =
    split-adjusted close x today's share count, and 63-session average
    volume above scan's --min-volume (the screener's avgdailyvol3m).

Residual bias, reported rather than fixed: names delisted, acquired, or now
below the pool floor are absent. For a dip-buying scan that flatters: the
missing names are mostly ones that kept falling after an oversold print.
--universe-file restricts the pool to a ticker list, e.g. state/universe.txt
(today's large caps), to measure how much survivorship flatters it.

Each signal trades the scan's canonical convention (backtest_outcomes.py's):
entry at the signal-day close (or, with next-open fills, the next session's
open, skipping a gap already past the target or through the stop), a
take-profit at the 5DMA target, a stop at close - 2.5 ATR, the window-end
close otherwise; WON on a same-day double touch. Streak / day-of-spell is
consecutive sessions on the pick list, as enrich_with_persistence counts
consecutive runs.

It reports
  - per-signal expectancy by year and by every stratum backtest_outcomes.py
    and SKILL.md quote (tier, spell, score, the pocket, breadth, pullback),
    plus the excess over SPY across the same holding span;
  - portfolio curves, net of --cost-bps per round trip: every open trade
    equal-weighted (daily rebalanced, cash when none is open), and
    --slots fixed slots of 1/K equity each (the day's new signals fill free
    slots by score), idle slots in cash or in SPY. The SPY-idle variant is
    "index core, MR satellite", the version a long-only account would run.

Run (reuses replay_board.py's cache format; the first run downloads ~3000
tickers x 4+ years, a few minutes):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \\
    python replay_signals.py [--eval-start 2023-01-03] [--refresh] \\
    [--cache /path/to/momentum_replay_bars.pkl] [--universe-file ../state/universe.txt]
"""

from __future__ import annotations

import argparse
import pickle
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import scan

SCAN = scan.build_argparser().parse_args([])           # scan.py's defaults
TH = SCAN.rsi2_threshold
W = SCAN.target_window_days
MIN_BARS = scan.SMA_LONG_PERIOD + scan.TREND_SLOPE_LOOKBACK + 1   # passes_trend_filter
VC_DAYS = scan.VOL_COLLAPSE_WINDOW_MONTHS * 21                    # filter_vol_collapse
ADV_DAYS = 63                                                     # screener's avgdailyvol3m
HIGH_SCORE = scan.PAPER_MIN_SCORE   # the 📝 paper-track floor
# The pocket the 2026-05→07 live sample validated and this replay retired:
# score ≥ 40 on a 1st-2nd consecutive listing. Kept to re-test it.
OLD_POCKET_MIN_SCORE = 40.0
OLD_POCKET_MAX_SPELL = 2
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


# --------------------------------------------------------------- signals

PANEL_KEYS = ("pick", "score", "rsi2", "dist5", "dist50", "dist200", "freq",
              "target", "stop")


def ticker_panel(close: pd.Series, high: pd.Series, low: pd.Series) -> dict[str, pd.Series]:
    """scan.score_tickers + filter_vol_collapse + attach_atr_stops_all for
    one ticker at every session at once, on the ticker's own dropna'd bars
    as the scan sees them. `pick` is the post-vol-collapse pick list
    membership (RSI(2) under 2x the threshold, 🟡 included, as history.csv
    records it)."""
    s = close.dropna()
    n = np.arange(1, len(s) + 1)
    ma50 = s.rolling(scan.SMA_MID_PERIOD).mean()
    ma200 = s.rolling(scan.SMA_LONG_PERIOD).mean()
    ma200_prev = ma200.shift(scan.TREND_SLOPE_LOOKBACK)
    slope = (ma200 / ma200_prev - 1) * 100
    trend = ((n >= MIN_BARS) & (ma200_prev > 0) & (s > ma200)
             & (slope > scan.MA200_SLOPE_RISK_ON_THRESHOLD_PCT) & (ma50 > ma200))

    rsi = scan.compute_rsi_wilder(s, scan.RSI_PERIOD)
    sma5 = s.rolling(scan.SMA_TARGET_PERIOD).mean()
    dist5 = (s / sma5 - 1) * 100
    dist200 = (s / ma200 - 1) * 100
    cross = ((rsi.shift(1) >= TH) & (rsi < TH)).astype(float)
    freq = cross.rolling(scan.FREQ_LOOKBACK_DAYS - 1).sum().shift(1).fillna(0)
    score = (np.clip(40 * (1 - rsi / max(TH, .1)), 0, 40)
             + np.clip(30 * (-dist5 / 15), 0, 30)
             + np.clip(15 * (dist200 / 30), 0, 15)
             + np.clip(15 * (1 - freq / 8), 0, 15)).round(1)

    # filter_vol_collapse: the last VC_DAYS closes -> VC_DAYS-1 returns,
    # split at the midpoint, ddof=1 annualized std of each half.
    r = s.pct_change()
    half = (VC_DAYS - 1) // 2
    v2 = r.rolling(VC_DAYS - 1 - half).std() * np.sqrt(252) * 100
    v1 = r.shift(VC_DAYS - 1 - half).rolling(half).std() * np.sqrt(252) * 100
    collapsed = (v1 >= scan.VOL_COLLAPSE_MIN_FIRST_VOL_PCT) & (v2 / v1 < SCAN.vol_collapse_ratio)

    idx = s.index.intersection(high.dropna().index).intersection(low.dropna().index)
    h, l, c = high.loc[idx], low.loc[idx], s.loc[idx]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(scan.ATR_PERIOD).mean().reindex(s.index)

    pick = trend & (rsi < TH * 2) & ~collapsed.fillna(False)
    return dict(pick=pick, score=score, rsi2=rsi, dist5=dist5,
                dist50=(s / ma50 - 1) * 100, dist200=dist200, freq=freq,
                target=sma5, stop=s - SCAN.atr_stop_mult * atr)


def signal_panels(C: pd.DataFrame, H: pd.DataFrame, L: pd.DataFrame,
                  universe: pd.DataFrame) -> dict[str, pd.DataFrame]:
    cols = {k: {} for k in PANEL_KEYS}
    for t in C.columns:
        if C[t].notna().sum() < MIN_BARS:
            continue
        p = ticker_panel(C[t], H[t], L[t])
        for k in PANEL_KEYS:
            cols[k][t] = p[k]
    out = {k: pd.DataFrame(v).reindex(index=C.index, columns=C.columns) for k, v in cols.items()}
    out["pick"] = (out["pick"].fillna(False).astype(bool)
                   & universe.reindex_like(out["pick"]).fillna(False).astype(bool))
    return out


def spells(pick: pd.DataFrame) -> pd.DataFrame:
    """Consecutive sessions on the pick list, 1 on a first day, 0 off it."""
    a = pick.to_numpy()
    out = np.zeros(a.shape, dtype=int)
    for i in range(len(a)):
        out[i] = np.where(a[i], (out[i - 1] if i else 0) + 1, 0)
    return pd.DataFrame(out, index=pick.index, columns=pick.columns)


def tier(rsi2: np.ndarray) -> np.ndarray:
    return np.where(rsi2 < TH / 2, "🔵", np.where(rsi2 < TH, "🟢", "🟡"))


# --------------------------------------------------------------- outcomes

def resolve(sig: pd.DataFrame, O: np.ndarray, H: np.ndarray, L: np.ndarray,
            C: np.ndarray, spyO: np.ndarray, spyC: np.ndarray, mode: str) -> pd.DataFrame:
    """Every signal's trade under the canonical convention, vectorized.
    Adds outcome / both (a WON that touched the stop the same session) /
    exit_day / result (canonical fills) / gap (gap-aware fills) / spy (SPY
    over the same span) / entry_px / exit_px columns;
    next-open gap skips come back with outcome SKIP."""
    i, j = sig.i.to_numpy(), sig.j.to_numpy()
    target, stop = sig.target.to_numpy(), sig.stop.to_numpy()
    rows = i[:, None] + np.arange(1, W + 1)
    h, l, o = H[rows, j[:, None]], L[rows, j[:, None]], O[rows, j[:, None]]
    if mode == "close":
        entry = C[i, j]
        spy0 = spyC[i]
    else:
        entry = O[i + 1, j]
        spy0 = spyO[i + 1]
    with np.errstate(invalid="ignore"):
        hit_t = h >= target[:, None]
        hit_s = l <= stop[:, None]
    big = W + 1
    ft = np.where(hit_t.any(1), hit_t.argmax(1), big)
    fs = np.where(hit_s.any(1), hit_s.argmax(1), big)
    won = (ft < big) & (ft <= fs)
    lost = ~won & (fs < big)
    d = np.where(won, ft, np.where(lost, fs, W - 1))           # 0-based day in window
    exit_canon = np.where(won, target, np.where(lost, stop, C[i + W, j]))
    o_exit = o[np.arange(len(i)), d]
    exit_gap = np.where(won, np.fmax(target, o_exit), np.where(lost, np.fmin(stop, o_exit), exit_canon))
    out = sig.copy()
    out["outcome"] = np.where(won, "WON", np.where(lost, "LOST", "EXPIRED"))
    out["both"] = won & (ft == fs)                             # same-day double touch
    if mode == "next-open":
        skip = ~np.isfinite(entry) | (entry >= target) | (entry <= stop)
        out.loc[skip, "outcome"] = "SKIP"
    out["exit_day"] = i + 1 + d
    out["entry_px"] = entry
    out["exit_px"] = exit_gap
    out["result"] = (exit_canon / entry - 1) * 100
    out["gap"] = (exit_gap / entry - 1) * 100
    out["spy"] = (spyC[i + 1 + d] / spy0 - 1) * 100
    out["excess"] = out.gap - out.spy
    out["noexit"] = (C[i + W, j] / entry - 1) * 100
    out = out[np.isfinite(out.result) & np.isfinite(out.gap)]
    return out


# -------------------------------------------------------------- portfolio

def trade_daily_returns(tr: pd.DataFrame, C: np.ndarray,
                        cost_bps: float) -> list[tuple[int, np.ndarray]]:
    """(first day, daily returns) per trade: day k's return is close k-1 (or
    the entry fill) to close k, or to the exit fill on the exit day, which
    also pays the round-trip cost."""
    out = []
    for i, j, x, e, px in zip(tr.i.to_numpy(), tr.j.to_numpy(), tr.exit_day.to_numpy(),
                              tr.entry_px.to_numpy(), tr.exit_px.to_numpy()):
        path = C[i + 1:x + 1, j].copy()
        path[-1] = px
        prev = np.r_[e, path[:-1]]
        r = path / prev - 1
        r[-1] -= cost_bps / 1e4
        out.append((i + 1, np.nan_to_num(r)))
    return out


def ew_open(daily: list[tuple[int, np.ndarray]], T: int) -> tuple[np.ndarray, np.ndarray]:
    """Every open trade equal-weighted, daily rebalanced; cash on empty days."""
    s, n = np.zeros(T), np.zeros(T)
    for k0, r in daily:
        s[k0:k0 + len(r)] += r
        n[k0:k0 + len(r)] += 1
    return np.where(n > 0, s / np.maximum(n, 1), 0.0), n


def slotted(tr: pd.DataFrame, daily: list[tuple[int, np.ndarray]], T: int, k: int,
            idle: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """K slots of 1/K equity each; a session's new signals fill free slots
    by score (highest first), one position per ticker (a name still held
    from yesterday's signal isn't bought twice); idle slots earn `idle`
    (zeros = cash)."""
    order = np.lexsort((-tr.score.to_numpy(), tr.i.to_numpy()))
    tick = tr.ticker.to_numpy()
    held: list[tuple[int, str]] = []                         # (day the slot frees up, ticker)
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

def pct(v: float, nd: int = 1) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.{nd}f}%"


def pp(v: float) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:+.2f}%"


def by_day(x: pd.DataFrame) -> tuple[float, float]:
    """Excess over SPY averaged within each signal session first, then across
    sessions, with its t. Signals from one session move together (a washout
    day lists 100+ names that all bounce with the index), so the per-signal
    mean overweights those days and its naive t is inflated."""
    g = x.groupby("date").excess.mean()
    if len(g) < 3:
        return (g.mean() if len(g) else np.nan), np.nan
    return g.mean(), g.mean() / (g.std(ddof=1) / np.sqrt(len(g)))


def strata(title: str, groups: list[tuple[str, pd.DataFrame, pd.DataFrame]], yrs: list[int]) -> list[str]:
    L = ["", f"### {title}", "",
         "| stratum | n | win% | exp | gap exp | no-exit | next-open n / gap exp | next-open vs SPY by day (t) | "
         + " | ".join(map(str, yrs)) + " |",
         "|---|---|---|---|---|---|---|---|" + "---|" * len(yrs)]
    for name, c, o in groups:
        if len(c) == 0:
            continue
        dec = c.outcome.isin(["WON", "LOST"])
        win = (c.outcome == "WON").sum() / dec.sum() if dec.sum() else np.nan
        m, t = by_day(o)
        L.append(f"| {name} | {len(c)} | {win:.0%} | {pp(c.result.mean())} | {pp(c.gap.mean())} | "
                 f"{pp(c.noexit.mean())} | {len(o)} / {pp(o.gap.mean())} | {pp(m)} ({t:+.1f}) | "
                 + " | ".join(pp(by_day(o[o.year == y])[0]) for y in yrs) + " |")
    return L


def pocket(d: pd.DataFrame) -> pd.DataFrame:
    return d[(d.score >= OLD_POCKET_MIN_SCORE) & (d.spell <= OLD_POCKET_MAX_SPELL)]


def high(d: pd.DataFrame) -> pd.DataFrame:
    return d[d.score >= HIGH_SCORE]


def report(sc: pd.DataFrame, so: pd.DataFrame, curves: dict, meta: dict) -> str:
    yrs = sorted(sc.year.unique())
    def g(name, f):
        return (name, f(sc), f(so))
    L = [f"# Mean-reversion signal replay, {meta['first']} → {meta['last']}", "",
         f"Point-in-time universe: ~{meta['uni']:.0f} names/day from a {meta['pool']}-ticker pool"
         f"{meta['note']}. Picks mirror scan.py's defaults (RSI(2) < {TH * 2:g} inside the trend "
         f"filter, vol-collapse {SCAN.vol_collapse_ratio}, target 5DMA, stop {SCAN.atr_stop_mult} ATR, "
         f"{W}-session window). ~{meta['per_day']:.0f} picks/day; {meta['skip']} next-open gap skips.", "",
         "Per-signal columns are gross (no costs): exp = canonical fills, gap exp = gap-aware fills, "
         f"no-exit = the {W}-session close-to-close with no target or stop, all close fills. The verdict "
         "column is next-open (executable after the evening scan) gap exp minus SPY over the same span, "
         "averaged per signal session, then across sessions; the year columns are that number per year."]
    L += strata("Headline", [
        g("all picks", lambda d: d),
        g("old pocket (score ≥ 40 & spell ≤ 2)", pocket),
        g("pocket, 🟢🔵 only (RSI2 < 5)", lambda d: pocket(d)[pocket(d).rsi2 < TH]),
        g("not pocket", lambda d: d.drop(pocket(d).index)),
        g(f"score ≥ {HIGH_SCORE:g}", high),
    ], yrs)
    L += strata("By tier", [g(t, lambda d, t=t: d[d.tier == t]) for t in ("🔵", "🟢", "🟡")], yrs)
    L += strata("By day-of-spell", [
        g("1st", lambda d: d[d.spell == 1]), g("2nd", lambda d: d[d.spell == 2]),
        g("3rd", lambda d: d[d.spell == 3]), g("4th+", lambda d: d[d.spell >= 4])], yrs)
    L += strata("By score", [g(n, lambda d, lo=lo, hi=hi: d[(d.score >= lo) & (d.score < hi)])
                             for n, lo, hi in (("<40", 0, 40), ("40–55", 40, 55), ("55–70", 55, 70),
                                               ("≥70", 70, 999))], yrs)
    L += strata("By trend buffer (vs 200DMA)", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.dist200 >= lo) & (d.dist200 < hi)])
        for n, lo, hi in (("0–10%", 0, 10), ("10–20%", 10, 20), ("20–30%", 20, 30), ("30–50%", 30, 50),
                          ("≥ 50%", 50, 1e9))], yrs)
    L += strata("By scan breadth (picks that session)", [
        g(f"<{scan.BREADTH_THIN_MAX} thin", lambda d: d[d.breadth < scan.BREADTH_THIN_MAX]),
        g("normal", lambda d: d[(d.breadth >= scan.BREADTH_THIN_MAX) & (d.breadth <= scan.BREADTH_WASHOUT_MIN)]),
        g(f">{scan.BREADTH_WASHOUT_MIN} washout", lambda d: d[d.breadth > scan.BREADTH_WASHOUT_MIN]),
        g("pocket & thin", lambda d: pocket(d)[pocket(d).breadth < scan.BREADTH_THIN_MAX]),
        g("pocket & washout", lambda d: pocket(d)[pocket(d).breadth > scan.BREADTH_WASHOUT_MIN]),
    ], yrs)
    L += strata("By pullback (5DMA gap)", [
        g(n, lambda d, lo=lo, hi=hi: d[(d.dist5 >= lo) & (d.dist5 < hi)])
        for n, lo, hi in (("≤ −7% washout", -99, -7), ("−7..−4%", -7, -4), ("−4..−2%", -4, -2),
                          ("> −2% shallow", -2, 99))], yrs)
    L += strata("By market regime (SPY above a rising 200DMA = scan's RISK-ON)", [
        g("RISK-ON", lambda d: d[d.risk_on]), g("not RISK-ON", lambda d: d[~d.risk_on]),
        g("pocket & RISK-ON", lambda d: pocket(d)[pocket(d).risk_on]),
        g("pocket & not RISK-ON", lambda d: pocket(d)[~pocket(d).risk_on])], yrs)

    L += ["", f"## Portfolios (net of {meta['cost']:g}bp round trip)", "",
          "| strategy | close: total / CAGR / vol / maxDD / Sharpe | beta | alpha/yr vs SPY (t) | "
          "avg open | days invested | next-open: total / CAGR / maxDD |",
          "|---|---|---|---|---|---|---|"]
    for k, v in curves.items():
        c, o = v["c"], v.get("o")
        t = f"{c['alpha_t']:+.1f}" if np.isfinite(c["alpha_t"]) else "—"
        L.append(f"| {k} | {pct(c['total'])} / {pct(c['cagr'])} / {c['vol'] * 100:.0f}% / "
                 f"{pct(c['maxdd'])} / {c['sharpe']:.2f} | {c['beta']:.2f} | {pct(c['alpha'])} ({t}) | "
                 f"{v['n']:.1f} | {v['inv']:.0%} | "
                 + (f"{pct(o['total'])} / {pct(o['cagr'])} / {pct(o['maxdd'])} |" if o else "— |"))
    cy = sorted(next(iter(curves.values()))["c"]["years"])
    L += ["", "### Calendar years (close fills / next-open fills)", "",
          "| strategy | " + " | ".join(map(str, cy)) + " |", "|---|" + "---|" * len(cy)]
    for k, v in curves.items():
        o = v.get("o")
        L.append(f"| {k} | " + " | ".join(
            pct(v["c"]["years"].get(y, np.nan)) + (f" / {pct(o['years'].get(y, np.nan))}" if o else "")
            for y in cy) + " |")
    return "\n".join(L)


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-start", default="2023-01-03", help="first signal session counted")
    ap.add_argument("--fetch-start", default=None,
                    help="price download start (default: one year before --eval-start, for the 200DMA)")
    ap.add_argument("--pool-min-mcap", type=float, default=5e8,
                    help="download pool floor on TODAY's market cap (keep well under scan's --min-market-cap)")
    ap.add_argument("--slots", type=int, default=10, help="K for the fixed-slot portfolios")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="round-trip cost per trade")
    ap.add_argument("--universe-file", type=Path, help="restrict the pool to these tickers (one per line)")
    ap.add_argument("--signals-out", type=Path, help="write every resolved close-fill signal to this CSV")
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "mr_replay_bars.pkl")
    ap.add_argument("--refresh", action="store_true", help="refetch pool and bars")
    args = ap.parse_args()

    eval_start = pd.Timestamp(args.eval_start)
    fetch_start = args.fetch_start or (eval_start - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    pool, bars = load_data(args.cache, fetch_start, args.pool_min_mcap, args.refresh)
    adj = bars["Adj Close"] / bars["Close"]
    shares = pool.set_index("symbol").shares
    tickers = [t for t in bars["Close"].columns if t in shares.index]
    note = ""
    if args.universe_file:
        keep = {l.strip() for l in args.universe_file.read_text().splitlines() if l.strip()}
        tickers = [t for t in tickers if t in keep]
        note += f", restricted to {args.universe_file.name} ({len(tickers)} tickers)"
    C = bars["Adj Close"][tickers]
    O, H, L = ((bars[f] * adj)[tickers] for f in ("Open", "High", "Low"))
    universe = point_in_time_universe(bars["Close"][tickers], bars["Volume"][tickers], shares)
    print(f"scoring {len(tickers)} tickers x {len(C)} sessions...", file=sys.stderr)
    P = signal_panels(C, H, L, universe)
    sp = spells(P["pick"])

    dates = C.index
    S = dates.searchsorted(eval_start)
    T = len(dates)
    last_sig = T - W - 2                                   # full window + next-open entry
    pick = P["pick"].to_numpy().copy()
    pick[:S] = False
    pick[last_sig + 1:] = False
    ii, jj = np.nonzero(pick)
    spyC = bars["Adj Close"]["SPY"]
    spyO = bars["Open"]["SPY"] * adj["SPY"]
    ma = spyC.rolling(200).mean()
    risk_on = ((spyC > ma) & ((ma / ma.shift(scan.MA200_SLOPE_LOOKBACK_DAYS) - 1) * 100
                              > scan.MA200_SLOPE_RISK_ON_THRESHOLD_PCT)).to_numpy()
    breadth = P["pick"].sum(axis=1).to_numpy()
    sig = pd.DataFrame(dict(i=ii, j=jj, date=dates[ii], ticker=np.array(tickers)[jj],
                            **{k: P[k].to_numpy()[ii, jj] for k in PANEL_KEYS if k != "pick"},
                            spell=sp.to_numpy()[ii, jj], breadth=breadth[ii], risk_on=risk_on[ii]))
    sig["tier"] = tier(sig.rsi2.to_numpy())
    sig["year"] = sig.date.dt.year
    sig = sig[np.isfinite(sig.stop) & np.isfinite(sig.target)].reset_index(drop=True)

    arr = lambda df: df.to_numpy(dtype=float)
    Oa, Ha, La, Ca = arr(O), arr(H), arr(L), arr(C)
    sC, sO = spyC.to_numpy(), spyO.to_numpy()
    sc = resolve(sig, Oa, Ha, La, Ca, sO, sC, "close")
    so_all = resolve(sig, Oa, Ha, La, Ca, sO, sC, "next-open")
    so = so_all[so_all.outcome != "SKIP"]
    if args.signals_out:
        sc.drop(columns=["i", "j"]).to_csv(args.signals_out, index=False)

    idx = dates[S:last_sig + W + 1]
    spy_c = pd.Series(sC[1:] / sC[:-1] - 1, index=dates[1:]).reindex(idx).fillna(0.0)
    spy_daily = np.r_[0.0, sC[1:] / sC[:-1] - 1]
    curves = {}
    for name, sel in (("all picks, every open trade EW", lambda d: d),
                      ("old pocket, every open trade EW", pocket)):
        v = {}
        for mode, trades in (("c", sel(sc)), ("o", sel(so))):
            r, n = ew_open(trade_daily_returns(trades, Ca, args.cost_bps), T)
            rs = pd.Series(r, index=dates).reindex(idx)
            v[mode] = perf(rs, spy_c)
            if mode == "c":
                v["n"], v["inv"] = n[S:].mean(), (n[S:last_sig + W + 1] > 0).mean()
        curves[name] = v
    K = args.slots
    for name, sel, idle in ((f"old pocket, {K} slots, idle cash", pocket, np.zeros(T)),
                            (f"old pocket, {K} slots, idle in SPY", pocket, spy_daily),
                            (f"all picks, {K} slots, idle in SPY", lambda d: d, spy_daily),
                            (f"score ≥ {HIGH_SCORE:g}, {K} slots, idle cash", high, np.zeros(T)),
                            (f"score ≥ {HIGH_SCORE:g}, {K} slots, idle in SPY", high, spy_daily)):
        v = {}
        for mode, trades in (("c", sel(sc)), ("o", sel(so))):
            trades = trades.reset_index(drop=True)
            daily = trade_daily_returns(trades, Ca, args.cost_bps)
            r, n = slotted(trades, daily, T, K, idle)
            v[mode] = perf(pd.Series(r, index=dates).reindex(idx), spy_c)
            if mode == "c":
                v["n"], v["inv"] = n[S:].mean(), (n[S:last_sig + W + 1] > 0).mean()
        curves[name] = v
    curves["SPY"] = dict(c=perf(spy_c, spy_c), n=1.0, inv=1.0)

    meta = dict(first=dates[S].date(), last=dates[last_sig].date(), pool=len(tickers), note=note,
                uni=universe.iloc[S:last_sig + 1].sum(axis=1).mean(), cost=args.cost_bps,
                per_day=breadth[S:last_sig + 1].mean(), skip=int((so_all.outcome == "SKIP").sum()))
    print(report(sc, so, curves, meta))


if __name__ == "__main__":
    main()
