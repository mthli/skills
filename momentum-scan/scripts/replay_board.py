#!/usr/bin/env python3
"""Survivorship-free replay of the momentum board across several regimes.

backtest_outcomes.py replays what the scan actually published
(state/history.csv): point-in-time, but only as long as the history, which
is one regime. This script answers what history.csv can't: how the board
would have done since --eval-start. It rebuilds the board for every session
from raw prices, mirroring scan.py's scoring (scan.py's own argparse
defaults and constants are imported, so a parameter change there flows
through here), on a point-in-time universe:

  - download pool: every US equity Yahoo lists TODAY above --pool-min-mcap
    ($500M), so names that were large then and have shrunk since are in it;
  - daily universe: mcap_t above scan's --min-market-cap, where mcap_t =
    split-adjusted close x today's share count, and 63-session average
    volume above scan's --min-volume (the screener's avgdailyvol3m).

Residual bias, reported rather than fixed: names delisted, acquired, or now
below the pool floor are absent, and today's share count misstates past
mcap for heavy issuers and buyers. --dilution-check drops every name whose
share count grew >25% since the start of the sample by anything other than
a clean split ratio (yfinance's share history is not split-adjusted and too
spiky to rebuild mcap from directly, so it is used only as this filter).
--universe-file restricts the pool to a ticker list, e.g. state/universe.txt
(today's large caps) to measure how much survivorship flatters the board.

It reports
  - portfolio curves: equal-weight top-N rebalanced daily (--top-ns),
    enter-at-top-K / exit-below-top-30 hysteresis (--hysteresis), top-10
    gated on scan's RISK-ON banner, an equal-weight universe baseline, SPY
    and QQQ. Close fills hold the board published at close t from close t
    to close t+1 (compute_benchmark.py's convention); next-open fills buy at
    open t+1 and hold to open t+2 (what's executable after a 20:00 ET scan).
    Net of --cost-bps round trip on every unit of weight traded;
  - episodes (top-30 listing -> dropout, close fills): the SKILL.md
    "Backtested outcomes" claims re-tested on the longer sample.

Run (the first run downloads ~3000 tickers x 4+ years, a few minutes; the
bars are cached in the temp dir afterwards):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \\
    python replay_board.py [--eval-start 2023-01-03] [--refresh] \\
    [--dilution-check] [--universe-file ../state/universe.txt]
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

SCAN = scan.build_argparser().parse_args([])           # scan.py's defaults
WINDOW = scan.window_trading_days(SCAN.window_months)  # 63 sessions
MIN_OBS = scan.min_window_observations(SCAN.window_months)
HALF = (WINDOW - 1) // 2                               # vol-collapse split point
ADV_DAYS = 63                                          # screener's avgdailyvol3m
FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
SPLIT_RATIOS = (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50)
DILUTION_MAX_GROWTH = 1.25
COL_BLOCK = 300                                        # tickers per sliding-window block


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
    bars = fetch_bars(sorted(pool.symbol) + ["SPY", "QQQ"], fetch_start)
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


# ----------------------------------------------------------------- board

def score_panel(closes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """scan.score_tickers' window statistics for every session at once.

    Same definitions as scan.py: return from the window's first to last
    present close, max drawdown off the running peak, annualized vol of the
    window's first and second half of daily returns. One deliberate
    simplification: a gap inside the window breaks the two returns around
    it here, where scan.py's dropna() bridges it; it only matters for a
    name with 1-3 missing sessions in the window.
    """
    a = closes.to_numpy(dtype=float)
    T, N = a.shape
    out = {k: np.full((T, N), np.nan) for k in ("ret", "dd", "v1", "v2", "nobs")}
    rows = slice(WINDOW - 1, None)
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        for c in range(0, N if T >= WINDOW else 0, COL_BLOCK):
            cols = slice(c, c + COL_BLOCK)
            w = sliding_window_view(a[:, cols], WINDOW, axis=0)    # (T-W+1, n, W)
            first, last = w[..., 0].copy(), w[..., -1].copy()
            for k in range(1, WINDOW - MIN_OBS + 1):               # up to 3 missing ends
                first = np.where(np.isnan(first), w[..., k], first)
                last = np.where(np.isnan(last), w[..., -1 - k], last)
            out["ret"][rows, cols] = last / first - 1
            out["dd"][rows, cols] = np.nanmin(w / np.fmax.accumulate(w, axis=2) - 1, axis=2)
            r = w[..., 1:] / w[..., :-1] - 1
            out["v1"][rows, cols] = np.nanstd(r[..., :HALF], axis=2, ddof=1) * np.sqrt(252) * 100
            out["v2"][rows, cols] = np.nanstd(r[..., HALF:], axis=2, ddof=1) * np.sqrt(252) * 100
            out["nobs"][rows, cols] = np.isfinite(w).sum(axis=2)
    return {k: pd.DataFrame(v, index=closes.index, columns=closes.columns) for k, v in out.items()}


def board_ranks(closes: pd.DataFrame, universe: pd.DataFrame,
                top_n: int = SCAN.top_n) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(rank, score): rank is 1..top_n on the board, NaN off it; mirrors
    scan.score_tickers + filter_vol_collapse, including the 2-decimal score
    rounding the scan sorts on."""
    p = score_panel(closes)
    score = (p["ret"] * 100 / np.maximum(p["dd"].abs() * 100, 1.0)).round(2)
    ok = (universe.reindex_like(score).fillna(False).astype(bool)
          & (p["nobs"] >= MIN_OBS)
          & (p["ret"] * 100 > SCAN.min_return_pct)
          & (p["dd"] * 100 > -abs(SCAN.max_dd_pct)))
    if SCAN.vol_collapse_ratio and SCAN.vol_collapse_ratio > 0:
        ok &= ~((p["v1"] >= scan.VOL_COLLAPSE_MIN_FIRST_VOL_PCT)
                & (p["v2"] / p["v1"] < SCAN.vol_collapse_ratio))
    rank = score.where(ok).rank(axis=1, ascending=False, method="first")
    return rank.where(rank <= top_n), score


def point_in_time_universe(close_raw: pd.DataFrame, volume: pd.DataFrame,
                           shares: pd.Series) -> pd.DataFrame:
    mcap = close_raw * shares.reindex(close_raw.columns).to_numpy()
    return (mcap > SCAN.min_market_cap) & (volume.rolling(ADV_DAYS).mean() > SCAN.min_volume)


def dilution_suspects(share_hist: dict[str, pd.Series], shares_now: pd.Series,
                      early_until: pd.Timestamp) -> set[str]:
    """Tickers whose share count grew > DILUTION_MAX_GROWTH since the sample's
    start, a jump close to a split ratio excepted (the history isn't split-adjusted)."""
    out = set()
    for t, s in share_hist.items():
        if s is None or len(s) < 3 or t not in shares_now:
            continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        early = s.loc[:early_until]
        base = (early if len(early) else s.iloc[:3]).median()
        ratio = shares_now[t] / base if base > 0 else np.nan
        if not np.isfinite(ratio) or ratio <= DILUTION_MAX_GROWTH:
            continue
        if not any(abs(ratio / q - 1) < .12 for q in SPLIT_RATIOS):
            out.add(t)
    return out


def fetch_share_history(tickers: list[str], start: str) -> dict[str, pd.Series | None]:
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    def one(t):
        for attempt in range(3):
            try:
                s = yf.Ticker(t).get_shares_full(start=start)
                if s is None or len(s) == 0:
                    return t, None
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                return t, s.astype(float)
            except Exception:  # yfinance raises a zoo of transient errors
                time.sleep(2 + 3 * attempt)
        return t, None

    print(f"fetching share history for {len(tickers)} tickers...", file=sys.stderr)
    with ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, tickers))


# ------------------------------------------------------------- portfolio

def equal_weight(mask: pd.DataFrame) -> pd.DataFrame:
    m = mask.fillna(False).astype(float)
    return m.div(m.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)


def hysteresis(rank: pd.DataFrame, enter: int, exit_: int) -> pd.DataFrame:
    """Hold a name from its first close at rank <= enter until a close off the top `exit_`."""
    R = rank.to_numpy()
    held: set[int] = set()
    out = np.zeros(R.shape, dtype=bool)
    for i, r in enumerate(R):
        held = {j for j in held if r[j] <= exit_}          # NaN (off board) compares False
        held |= set(np.nonzero(r <= enter)[0])
        out[i, list(held)] = True
    return pd.DataFrame(out, index=rank.index, columns=rank.columns)


def net_returns(W: pd.DataFrame, R: pd.DataFrame, cost_bps: float) -> pd.Series:
    """Daily portfolio return: weights decided at t applied to R indexed t,
    minus half the round-trip cost on every unit of weight bought or sold."""
    turnover = W.diff().fillna(W).abs().sum(axis=1)
    return (W * R.fillna(0.0)).sum(axis=1) - turnover * cost_bps / 2 / 1e4


def perf(r: pd.Series, bench: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    b = bench.reindex(r.index).fillna(0.0)
    X = np.column_stack([np.ones(len(b)), b.to_numpy()])
    coef, *_ = np.linalg.lstsq(X, r.to_numpy(), rcond=None)
    resid = r.to_numpy() - X @ coef
    se = resid.std(ddof=2) / np.sqrt(len(resid))
    se = se if se > 1e-12 else np.nan                  # a benchmark regressed on itself
    vol = r.std() * np.sqrt(252)
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    return dict(total=eq.iloc[-1] - 1, cagr=cagr, vol=vol, sharpe=cagr / vol if vol else np.nan,
                maxdd=(eq / eq.cummax() - 1).min(), beta=coef[1], alpha=coef[0] * 252,
                alpha_t=coef[0] / se,
                years={y: (1 + g).prod() - 1 for y, g in r.groupby(r.index.year)})


def risk_on(spy_close: pd.Series) -> pd.Series:
    """scan.compute_regime's RISK-ON test, evaluated every session."""
    ma = spy_close.rolling(200).mean()
    slope_pct = (ma / ma.shift(scan.MA200_SLOPE_LOOKBACK_DAYS) - 1) * 100
    return (spy_close > ma) & (slope_pct > scan.MA200_SLOPE_RISK_ON_THRESHOLD_PCT)


# -------------------------------------------------------------- episodes

def episodes(rank: pd.DataFrame, score: pd.DataFrame, C: pd.DataFrame, V: pd.DataFrame,
             spy: pd.Series, start: int) -> pd.DataFrame:
    """Top-N listing -> first session off the board, close fills, excess vs SPY."""
    on = rank.notna().to_numpy()
    Ca, Ra, Sa = C.to_numpy(), rank.to_numpy(), score.to_numpy()
    sp = spy.to_numpy()
    ret1 = C.pct_change().to_numpy()
    Va = V.to_numpy()
    dist_day = (ret1 <= scan.DIST_DAY_MIN_DECLINE) & (Va > np.vstack([np.full((1, Va.shape[1]), np.nan), Va[:-1]]))
    dist = pd.DataFrame(dist_day.astype(float)).rolling(scan.DIST_DAYS_LOOKBACK_DAYS).sum().to_numpy()
    T = len(rank)
    rows = []
    for j in np.nonzero(on[start:].any(axis=0))[0]:
        col = on[:, j]
        i = start
        while i < T:
            if not (col[i] and not col[i - 1]):
                i += 1
                continue
            k = i + 1
            while k < T and col[k]:
                k += 1
            closed = k < T
            x_at = k if closed else T - 1
            e = dict(ticker=rank.columns[j], entry=rank.index[i], tenure=k - i, closed=closed,
                     dist=dist[i, j], score=Sa[i, j], top10=np.nanmin(Ra[i:k, j]) <= 10,
                     x=Ca[x_at, j] / Ca[i, j] - 1 - (sp[x_at] / sp[i] - 1))
            for h in (5, 10, 20):
                if closed and k + h < T:
                    e[f"post{h}"] = Ca[k + h, j] / Ca[k, j] - 1 - (sp[k + h] / sp[k] - 1)
            rows.append(e)
            i = k
    E = pd.DataFrame(rows)
    E["year"] = pd.DatetimeIndex(E.entry).year
    return E


# ---------------------------------------------------------------- report

def pct(v: float) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.1f}%"


def report(res: dict, E: pd.DataFrame, meta: dict, attrib: pd.Series, roll: pd.Series) -> str:
    L = [f"# Momentum board replay, {meta['first']} → {meta['last']}", "",
         f"Point-in-time universe: ~{meta['uni']:.0f} names/day from a {meta['pool']}-ticker pool"
         f"{meta['note']}. Board matches scan.py's defaults (window {WINDOW}, ret > "
         f"{SCAN.min_return_pct:g}%, dd > −{SCAN.max_dd_pct:g}%, top {SCAN.top_n}). "
         f"Net of {meta['cost']:g}bp round trip.", "",
         "| strategy | names | turnover/yr | close: total / CAGR / vol / maxDD / Sharpe | beta | alpha/yr vs SPY (t) | next-open: total / CAGR / maxDD |",
         "|---|---|---|---|---|---|---|"]
    for k, v in res.items():
        c, o = v["c"], v.get("o")
        t = f"{c['alpha_t']:+.1f}" if np.isfinite(c["alpha_t"]) else "—"
        L.append(f"| {k} | {v['n']:.1f} | {v['turn']:.0f}× | {pct(c['total'])} / {pct(c['cagr'])} / "
                 f"{c['vol'] * 100:.0f}% / {pct(c['maxdd'])} / {c['sharpe']:.2f} | {c['beta']:.2f} | "
                 f"{pct(c['alpha'])} ({t}) | "
                 + (f"{pct(o['total'])} / {pct(o['cagr'])} / {pct(o['maxdd'])} |" if o else "— |"))
    yrs = sorted(next(iter(res.values()))["c"]["years"])
    L += ["", "## Calendar years (close fills)", "",
          "| strategy | " + " | ".join(map(str, yrs)) + " |", "|---|" + "---|" * len(yrs)]
    for k, v in res.items():
        L.append(f"| {k} | " + " | ".join(pct(v["c"]["years"].get(y, np.nan)) for y in yrs) + " |")

    tot = attrib.sum()
    top = attrib.sort_values(ascending=False)
    L += ["", f"## top-10 attribution (sum of daily contributions {pct(tot)})", "",
          "- best: " + ", ".join(f"{t} {pct(x)}" for t, x in top.head(8).items()),
          "- worst: " + ", ".join(f"{t} {pct(x)}" for t, x in top.tail(5).items()),
          f"- the 10 best names are {top.head(10).sum() / tot:.0%} of the total" if tot > 0 else "",
          f"- rolling 6-month top-10 minus SPY: ahead in {(roll > 0).mean():.0%} of windows, "
          f"median {pct(roll.median())}, worst {pct(roll.min())} (ending {roll.idxmin().date()}), "
          f"best {pct(roll.max())}"]

    cl = E[E.closed]
    def line(s, name):
        return (f"| {name} | {len(s)} | {pct(s.x.mean())} / {pct(s.x.median())} / {(s.x > 0).mean():.0%} | "
                f"{s.tenure.mean():.1f} | {s.top10.mean():.0%} | "
                + " | ".join(f"{pct(s[s.year == y].x.mean())}" for y in yrs) + " |")
    L += ["", f"## Episodes: top-{SCAN.top_n} listing → dropout ({len(cl)} closed, {len(E) - len(cl)} open)", "",
          "| stratum | n | excess mean / med / win | tenure | reached top-10 | " + " | ".join(map(str, yrs)) + " |",
          "|---|---|---|---|---|" + "---|" * len(yrs), line(cl, "all closed"),
          line(E[~E.closed], "still listed (marked at last close)")]
    clean, loaded = scan.ENTRY_CLEAN_DIST_MAX, scan.ENTRY_LOADED_DIST_MIN
    for name, m in ((f"dist ≤{clean} (clean)", cl.dist <= clean),
                    (f"dist {clean + 1}-{loaded - 1}", (cl.dist > clean) & (cl.dist < loaded)),
                    (f"dist ≥{loaded} (loaded)", cl.dist >= loaded)):
        L.append(line(cl[m], name))
    terc = pd.qcut(cl.score, 3, labels=["low", "mid", "high"])
    for lab in ("low", "mid", "high"):
        L.append(line(cl[terc == lab], f"score tercile {lab}"))
    L += ["", "## After the dropout (excess vs SPY from the dropout close; selling beat holding on by minus these)", "",
          "| group | n | +5d | +10d | +20d | +10d median |", "|---|---|---|---|---|---|"]
    for name, s in [("all", cl), ("reached top-10", cl[cl.top10]), ("never top-10", cl[~cl.top10])] + \
            [(str(y), cl[cl.year == y]) for y in yrs]:
        L.append(f"| {name} | {len(s)} | {pct(s.post5.mean())} | {pct(s.post10.mean())} | "
                 f"{pct(s.post20.mean())} | {pct(s.post10.median())} |")
    return "\n".join(x for x in L if x is not None)


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-start", default="2023-01-03", help="first session the curves and episodes count")
    ap.add_argument("--fetch-start", default=None,
                    help="price download start (default: one year before --eval-start, for the 63-session window)")
    ap.add_argument("--pool-min-mcap", type=float, default=5e8,
                    help="download pool floor on TODAY's market cap (keep well under scan's --min-market-cap)")
    ap.add_argument("--top-ns", default="3,5,10,30", help="comma-separated equal-weight board depths")
    ap.add_argument("--hysteresis", default="10,30", help="ENTER,EXIT ranks for the hold-until-dropout variant")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="round-trip cost per unit of weight traded")
    ap.add_argument("--universe-file", type=Path, help="restrict the pool to these tickers (one per line)")
    ap.add_argument("--exclude-file", type=Path, help="drop these tickers from the universe")
    ap.add_argument("--dilution-check", action="store_true",
                    help="drop names whose share count grew >25%% other than by a split (fetches share history)")
    ap.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "momentum_replay_bars.pkl")
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
    O = (bars["Open"] * adj)[tickers]
    V = bars["Volume"][tickers]
    universe = point_in_time_universe(bars["Close"][tickers], V, shares)
    drop = set()
    if args.exclude_file:
        drop |= {l.strip() for l in args.exclude_file.read_text().splitlines() if l.strip()}
    rank, score = board_ranks(C, universe)
    if args.dilution_check:
        passed = score_panel(C)
        ever = sorted(C.columns[((passed["ret"] * 100 > SCAN.min_return_pct)
                                 & (passed["dd"] * 100 > -abs(SCAN.max_dd_pct))).any().to_numpy()])
        sus = dilution_suspects(fetch_share_history(ever, fetch_start), shares,
                                eval_start + pd.DateOffset(months=6))
        drop |= sus
        note += f", minus {len(sus)} dilution suspects"
    if drop:
        universe.loc[:, [t for t in tickers if t in drop]] = False
        rank, score = board_ranks(C, universe)
        if args.exclude_file:
            note += f", minus {args.exclude_file.name}"

    dates = C.index
    S = dates.searchsorted(eval_start)
    end = len(dates) - 2                                   # next-open fills need t+2
    spyC, qqqC = bars["Adj Close"]["SPY"], bars["Adj Close"]["QQQ"]
    spyO = bars["Open"]["SPY"] * adj["SPY"]
    Rc = C.shift(-1) / C - 1
    Ro = O.shift(-2) / O.shift(-1) - 1
    spy_c = (spyC.shift(-1) / spyC - 1).iloc[S:end]
    spy_o = (spyO.shift(-2) / spyO.shift(-1) - 1).iloc[S:end]
    qqq_c = (qqqC.shift(-1) / qqqC - 1).iloc[S:end]

    enter, exit_ = (int(x) for x in args.hysteresis.split(","))
    masks = {f"top-{n}": rank <= n for n in (int(x) for x in args.top_ns.split(","))}
    masks[f"enter ≤{enter} / exit >{exit_}"] = hysteresis(rank, enter, exit_)
    gate = risk_on(spyC).reindex(dates).fillna(False).astype(int)
    masks["top-10, RISK-ON only"] = (rank <= 10).mul(gate, axis=0).astype(bool)
    masks["equal-weight universe"] = universe
    res = {}
    for name, m in masks.items():
        W = equal_weight(m)
        rc = net_returns(W, Rc, args.cost_bps).iloc[S:end]
        ro = net_returns(W, Ro, args.cost_bps).iloc[S:end]
        turn = W.diff().abs().sum(axis=1).iloc[S:end].mean() * 252
        res[name] = dict(c=perf(rc, spy_c), o=perf(ro, spy_o), turn=turn,
                         n=m.sum(axis=1).iloc[S:end].mean(), rc=rc)
    res["SPY"] = dict(c=perf(spy_c, spy_c), o=perf(spy_o, spy_o), turn=0.0, n=1.0)
    res["QQQ"] = dict(c=perf(qqq_c, spy_c), turn=0.0, n=1.0)

    W10 = equal_weight(rank <= 10)
    attrib = (W10 * Rc.fillna(0.0)).iloc[S:end].sum()
    cum = lambda r: (1 + r).rolling(126).apply(np.prod, raw=True)
    roll = (cum(net_returns(W10, Rc, args.cost_bps).iloc[S:end]) - cum(spy_c)).dropna()
    E = episodes(rank, score, C, V, spyC, S)
    meta = dict(first=dates[S].date(), last=dates[end].date(), uni=universe.iloc[S:end].sum(axis=1).mean(),
                pool=len(tickers), cost=args.cost_bps, note=note)
    print(report(res, E, meta, attrib, roll))


if __name__ == "__main__":
    main()
