#!/usr/bin/env python3
"""Outcome backtest for base-breakout-scan history.

Replays state/history.csv as if traded the canonical way described in
SKILL.md: while a ticker stays on the watchlist (an "episode" = consecutive
run-days), a buy-stop sits at the most recent pivot; when the ticker drops
off the list the order is cancelled. A trigger fires the first trading day
the intraday High touches the pivot; the fill is max(pivot, that day's Open)
so gap-ups pay their real slippage.

Per triggered episode it measures forward returns (+5/+10/+20 sessions),
whether price fell back below the pivot within 5 sessions, whether an 8%
stop was hit, and the R-multiple of the stop-based trade. Episodes that
never trigger are classified as faded or broken down. Everything is then
stratified by the episode-start attributes recorded in history.csv
(base_score, signal, bb_pctile, vol_dryup_ratio, rs_slope, to_pivot_pct,
base_weeks, width_pct) to find which attributes actually predict outcomes.

Prices are fetched with auto_adjust=True — the same units scan.py used to
compute the recorded pivots — so splits and spinoffs stay continuous.
Post-scan dividend re-adjustment shifts the comparison by <1%; corporate
actions large enough to break the unit match (e.g. a spinoff between scan
date and today rescales history under the recorded pivot) are caught by a
consistency check: history's to_pivot_pct implies the scan-day close, and
any episode whose implied close disagrees with the price series by >15% is
dropped and reported rather than traded on mismatched units.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--horizon 20] [--stop-pct 8] [--refresh-prices]
"""

from __future__ import annotations

import argparse
import csv
import pickle
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

SKILL_DIR = Path(__file__).resolve().parent.parent
HISTORY_CSV = SKILL_DIR / "state" / "history.csv"
OUTCOMES_CSV = SKILL_DIR / "state" / "outcomes.csv"

# The resolution convention every published finding is quoted for. Named
# constants rather than inline argparse defaults so render_history_html.py
# can drift-guard its reference lines against them.
DEFAULT_HORIZON = 20
DEFAULT_STOP_PCT = 8.0
DEFAULT_ENTRY = "touch"


def ts_of(run_day: str) -> pd.Timestamp:
    """YYYYMMDD run-day string → Timestamp."""
    return pd.Timestamp(f"{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}")


# ---------------------------------------------------------------- history

@dataclass
class Appearance:
    run_day: str          # YYYYMMDD (ET date of the snapshot's close)
    pivot: float
    score: float
    signal: str
    bb_pctile: float
    vol_dryup: float
    rs_slope: float
    to_pivot: float
    base_weeks: float
    width_pct: float
    rank: int


@dataclass
class Episode:
    ticker: str
    appearances: list[Appearance] = field(default_factory=list)

    @property
    def start(self) -> Appearance:
        return self.appearances[0]

    @property
    def start_day(self) -> str:
        return self.appearances[0].run_day

    @property
    def end_day(self) -> str:
        return self.appearances[-1].run_day

    def best_signal(self) -> str:
        order = {"📊": 0, "⏳": 1, "🔥": 2, "🚀": 3}
        return max((a.signal for a in self.appearances),
                   key=lambda s: order.get(s.strip("*"), 0))


def load_episodes(history_path: Path) -> tuple[list[Episode], list[str]]:
    with open(history_path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("rank")]
    run_days = sorted({r["run_id"] for r in rows})
    day_idx = {d: i for i, d in enumerate(run_days)}

    per_ticker: dict[str, list[Appearance]] = defaultdict(list)
    for r in rows:
        per_ticker[r["ticker"]].append(Appearance(
            run_day=r["run_id"],
            pivot=float(r["pivot_price"]),
            score=float(r["base_score"]),
            signal=r["signal"].strip(),
            bb_pctile=float(r["bb_pctile"]),
            vol_dryup=float(r["vol_dryup_ratio"]),
            rs_slope=float(r["rs_slope_pct_per_wk"]),
            to_pivot=float(r["to_pivot_pct"]),
            base_weeks=float(r["base_weeks"]),
            width_pct=float(r["width_pct"]),
            rank=int(r["rank"]),
        ))

    episodes: list[Episode] = []
    for ticker, apps in per_ticker.items():
        apps.sort(key=lambda a: a.run_day)
        current: Episode | None = None
        prev_idx = None
        for a in apps:
            idx = day_idx[a.run_day]
            if current is None or idx - prev_idx > 1:
                current = Episode(ticker=ticker)
                episodes.append(current)
            current.appearances.append(a)
            prev_idx = idx
    return episodes, run_days


# ---------------------------------------------------------------- prices

NO_DATA_KEY = "_NO_DATA"  # cache slot: tickers Yahoo confirmed empty


def fetch_prices(tickers: list[str], start: str, cache: Path,
                 refresh: bool) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            data = pickle.load(f)
        # Freshness guard: a quarterly re-run against an old cache would
        # silently censor every new episode's post-window. SPY is always in
        # the cache, so its last bar dates the whole snapshot.
        last_bar = data["SPY"].index.max() if "SPY" in data else None
        stale = (last_bar is None
                 or (pd.Timestamp.today().normalize() - last_bar).days > 5
                 or (len(data["SPY"]) and data["SPY"].index.min()
                     > pd.Timestamp(start) + pd.Timedelta(days=7)))
        if stale:
            print(f"cache {cache} is stale (last bar {last_bar}); "
                  f"refetching everything", file=sys.stderr)
            data = {}
        no_data = data.get(NO_DATA_KEY, set())
        missing = [t for t in tickers if t not in data and t not in no_data]
        if not missing:
            return data
    else:
        data = {}
        no_data = set()
        missing = list(tickers)

    CHUNK = 50
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i:i + CHUNK]
        print(f"fetching {i + 1}-{i + len(chunk)} of {len(missing)}...",
              file=sys.stderr)
        # SPY rides along as a liveness probe: if it comes back, the request
        # pipeline worked, so empty chunk members are genuinely dead
        # (delisted) rather than a transient network failure.
        req = list(dict.fromkeys(chunk + ["SPY"]))
        raw = yf.download(req, start=start, interval="1d",
                          auto_adjust=True, group_by="ticker",
                          progress=False, threads=True)
        got_any = False
        for t in req:
            try:
                # yfinance returns MultiIndex columns for multi-ticker
                # requests and (version-dependent) flat columns for one.
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if len(df):
                data[t] = df
                got_any = True
        # Remember permanently-empty tickers so reruns don't refetch them.
        if got_any:
            no_data.update(t for t in chunk if t not in data)
        time.sleep(1.0)

    data[NO_DATA_KEY] = no_data
    with open(cache, "wb") as f:
        pickle.dump(data, f)
    return data


def units_consistent(ep: Episode, bars: pd.DataFrame,
                     tol_pct: float = 15.0) -> bool:
    """history's to_pivot_pct implies the scan-day close; if that disagrees
    with the price series the pivot is in different units (spinoff/split
    re-adjustment between scan date and today) and the episode is untestable."""
    a = ep.start
    start_ts = ts_of(a.run_day)
    implied_close = a.pivot * (1 + a.to_pivot / 100)
    closes = bars.loc[bars.index <= start_ts, "Close"]
    if len(closes) == 0:
        return False
    return abs(closes.iloc[-1] / implied_close - 1) * 100 <= tol_pct


# ---------------------------------------------------------------- outcomes

@dataclass
class Outcome:
    ep: Episode
    watch_days: int
    triggered: bool
    censored: bool                 # watch window ran past available data
    days_to_trigger: int | None = None
    trigger_pivot: float | None = None
    fill: float | None = None
    gap_pct: float | None = None   # fill premium over pivot
    ret5: float | None = None
    ret10: float | None = None
    ret_h: float | None = None     # horizon-session return (default 20)
    spy_h: float | None = None     # SPY over the same horizon window
    above_pivot10: bool | None = None
    fellback5: bool | None = None
    stop_hit: bool | None = None
    trade_ret: float | None = None  # stop-based trade: -stop% or +horizon close
    no_trigger_drift: float | None = None
    broke_down: bool | None = None


def resolve_episode(ep: Episode, bars: pd.DataFrame, spy: pd.DataFrame,
                    calendar: pd.DatetimeIndex, horizon: int,
                    stop_pct: float, entry: str = "touch") -> Outcome | None:
    start_ts = ts_of(ep.start_day)
    end_ts = ts_of(ep.end_day)

    # Watch window: trading days strictly after the first snapshot, through
    # one trading day past the last snapshot (the order placed after the
    # final listing is still live the next session, then cancelled).
    after_end = calendar[calendar > end_ts]
    if len(after_end) == 0:
        return None  # episode is still open as of the data edge
    window = calendar[(calendar > start_ts) & (calendar <= after_end[0])]
    if len(window) == 0:
        return None

    # Pivot in force on day T = pivot from the latest snapshot before T.
    pivots = {ts_of(a.run_day): a.pivot for a in ep.appearances}
    pivot_days = sorted(pivots)

    def pivot_for(day: pd.Timestamp) -> float:
        live = [d for d in pivot_days if d < day]
        return pivots[live[-1]] if live else pivots[pivot_days[0]]

    tbars = bars.loc[bars.index.isin(window)]
    censored = window[-1] > bars.index.max() if len(bars) else True
    out = Outcome(ep=ep, watch_days=len(window), triggered=False,
                  censored=bool(censored))

    vol20 = bars["Volume"].rolling(20).mean().shift(1) if "Volume" in bars else None

    trigger_ts = None
    for ts, row in tbars.iterrows():
        p = pivot_for(ts)
        if entry == "touch":
            fired = row["High"] >= p
        else:
            fired = row["Close"] >= p
            if fired and entry == "confirmed":
                v20 = vol20.get(ts) if vol20 is not None else None
                fired = bool(v20 and not np.isnan(v20)
                             and row["Volume"] >= 1.5 * v20)
        if fired:
            out.triggered = True
            out.trigger_pivot = p
            out.days_to_trigger = int((window < ts).sum()) + 1
            if entry == "touch":
                trigger_ts = ts
                out.fill = max(p, row["Open"])
            else:
                # Confirmation is visible after the close; entry is the next
                # session's open.
                later = bars.index[bars.index > ts]
                if len(later) == 0:
                    out.censored = True
                    return out
                trigger_ts = later[0]
                out.fill = float(bars.loc[trigger_ts, "Open"])
            out.gap_pct = (out.fill / p - 1) * 100
            break

    if trigger_ts is None:
        if not censored and len(tbars):
            start_close = bars.loc[bars.index <= start_ts, "Close"]
            if len(start_close):
                sc = start_close.iloc[-1]
                out.no_trigger_drift = (tbars["Close"].iloc[-1] / sc - 1) * 100
                out.broke_down = bool(
                    (tbars["Close"].min() / sc - 1) * 100 <= -stop_pct)
        return out

    post = bars.loc[bars.index >= trigger_ts]
    fill, pivot = out.fill, out.trigger_pivot

    def ret_at(k: int) -> float | None:
        if len(post) <= k:
            return None
        return (post["Close"].iloc[k] / fill - 1) * 100

    out.ret5, out.ret10, out.ret_h = ret_at(5), ret_at(10), ret_at(horizon)
    if len(post) > 10:
        out.above_pivot10 = bool(post["Close"].iloc[10] >= pivot)
    if len(post) > 5:
        out.fellback5 = bool((post["Close"].iloc[1:6] < pivot).any())
    if len(post) > horizon:
        win = post.iloc[:horizon + 1]
        stop_level = fill * (1 - stop_pct / 100)
        lows = win["Low"].copy()
        if entry == "touch":
            # The trigger day's Low can predate the intraday fill (morning
            # dip, then rally through the pivot), so judge day 0 by its
            # close; from day 1 on the position exists all session.
            lows.iloc[0] = win["Close"].iloc[0]
        out.stop_hit = bool((lows <= stop_level).any())
        if out.stop_hit:
            out.trade_ret = -stop_pct
        else:
            out.trade_ret = out.ret_h
        spy_win = spy.loc[spy.index >= trigger_ts, "Close"]
        if len(spy_win) > horizon:
            out.spy_h = (spy_win.iloc[horizon] / spy_win.iloc[0] - 1) * 100
    return out


# ---------------------------------------------------------------- ledger

# Outcomes ledger schema. One row per resolved-or-triggered EPISODE, not
# per run-day: the canonical trade is episode-scoped (a buy-stop that lives
# from first listing to dropout), so (start_run_id, ticker) is the natural
# key and the join back into history.csv, which already carries everything
# known at signal time (score, base_weeks, signal tier, pivot). The ledger
# adds only what resolution produces. The last three columns record the
# resolution *convention* so a consumer can tell whether the numbers are
# comparable to references/backtest-findings.md (horizon 20 / stop 8 /
# touch) without having to guess.
LEDGER_COLS = ["start_run_id", "ticker", "end_run_id", "outcome",
               "days_to_trigger", "gap_pct", "ret5", "ret10", "ret_h",
               "trade_ret_pct", "fellback5", "stop_hit", "censored",
               "horizon", "stop_pct", "entry"]


def ledger_rows(outcomes: list[Outcome], horizon: int, stop_pct: float,
                entry: str) -> list[dict]:
    """Episodes worth recording: everything that triggered, plus everything
    whose watch window fully elapsed without triggering. The excluded case
    is the still-undecided one (no trigger yet AND the window ran past the
    data edge) — writing those would freeze an in-flight episode as a
    failure. Consumers treat a missing row as "in flight", the same
    contract mean-reversion-scan's ledger uses for OPEN signals."""
    def r2(v, nd=2):
        return None if v is None or (isinstance(v, float) and np.isnan(v)) \
            else round(float(v), nd)

    def b01(v):
        return None if v is None else int(bool(v))

    rows = []
    for o in outcomes:
        if not (o.triggered or not o.censored):
            continue
        if o.triggered:
            outcome = "TRIGGERED"
        elif o.broke_down:
            outcome = "BROKE_DOWN"
        else:
            outcome = "FADED"
        rows.append({
            "start_run_id": o.ep.start_day,
            "ticker": o.ep.ticker,
            "end_run_id": o.ep.end_day,
            "outcome": outcome,
            "days_to_trigger": o.days_to_trigger,
            "gap_pct": r2(o.gap_pct),
            "ret5": r2(o.ret5),
            "ret10": r2(o.ret10),
            # A never-triggered episode has no fill, so its "result" is the
            # drift over the watch window — recorded in the same column so
            # the ledger has one outcome magnitude per row, with `outcome`
            # saying which kind it is.
            "ret_h": r2(o.ret_h if o.triggered else o.no_trigger_drift),
            "trade_ret_pct": r2(o.trade_ret),
            "fellback5": b01(o.fellback5),
            "stop_hit": b01(o.stop_hit),
            "censored": b01(o.censored),
            "horizon": horizon,
            "stop_pct": stop_pct,
            "entry": entry,
        })
    return rows


def write_ledger(rows: list[dict], path: Path) -> int:
    """Upsert rows into the ledger, keyed by (start_run_id, ticker).

    Re-resolution replaces the stored row — resolution is deterministic, so
    a re-run is normally a no-op refresh that also self-heals after upstream
    price corrections. Rows this run couldn't reach (a ticker Yahoo no
    longer serves, e.g. after a delisting) are preserved rather than
    dropped, so the ledger accumulates history the resolver alone can't
    reconstruct. Atomic write. Returns the ledger's total row count.

    Empty input is a no-op: nothing new resolved, so there is nothing to
    merge, and rewriting would only risk churning a file that is fine as it
    stands (pandas also warns on concat with an empty frame)."""
    if not rows:
        if not (path.exists() and path.stat().st_size > 0):
            return 0
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.reader(f)) - 1
    new_rows = pd.DataFrame(rows, columns=LEDGER_COLS)
    if path.exists() and path.stat().st_size > 0:
        existing = pd.read_csv(path, dtype={"start_run_id": str,
                                            "end_run_id": str,
                                            "ticker": str})
        new_keys = set(zip(new_rows["start_run_id"].astype(str),
                           new_rows["ticker"]))
        keep = [(str(s), t) not in new_keys
                for s, t in zip(existing["start_run_id"], existing["ticker"])]
        combined = pd.concat([existing.loc[keep], new_rows], ignore_index=True)
    else:
        combined = new_rows
    combined = (combined
                .sort_values(["start_run_id", "ticker"], kind="stable")
                .reset_index(drop=True)
                .reindex(columns=LEDGER_COLS))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    combined.to_csv(tmp, index=False)
    tmp.replace(path)
    return len(combined)


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:.{nd}f}{suffix}"


def agg(outs: list[Outcome], horizon: int) -> dict:
    trig_denom = [o for o in outs if o.triggered or not o.censored]
    trig = [o for o in outs if o.triggered]
    res = [o for o in trig if o.ret_h is not None]
    ap10 = [o for o in trig if o.above_pivot10 is not None]
    fb5 = [o for o in trig if o.fellback5 is not None]
    tr = [o for o in trig if o.trade_ret is not None]
    d = {
        "n": len(outs),
        "trig_rate": 100 * len(trig) / len(trig_denom) if trig_denom else None,
        "n_trig": len(trig),
        "med_days": float(np.median([o.days_to_trigger for o in trig])) if trig else None,
        "ret_h": float(np.mean([o.ret_h for o in res])) if res else None,
        "med_ret_h": float(np.median([o.ret_h for o in res])) if res else None,
        "win": 100 * sum(o.ret_h > 0 for o in res) / len(res) if res else None,
        "above_p10": 100 * sum(o.above_pivot10 for o in ap10) / len(ap10) if ap10 else None,
        "fellback5": 100 * sum(o.fellback5 for o in fb5) / len(fb5) if fb5 else None,
        "stop_hit": 100 * sum(o.stop_hit for o in tr) / len(tr) if tr else None,
        "trade_ret": float(np.mean([o.trade_ret for o in tr])) if tr else None,
        "n_resolved": len(res),
    }
    spy = [o.spy_h for o in res if o.spy_h is not None]
    d["alpha"] = (d["ret_h"] - float(np.mean(spy))) if (spy and d["ret_h"] is not None) else None
    return d


def strata_table(title: str, groups: list[tuple[str, list[Outcome]]],
                 horizon: int) -> str:
    lines = [f"\n### {title}\n",
             f"| Stratum | Eps | Trig% | MedDays | n(res) | Ret{horizon} avg | "
             f"Ret{horizon} med | Win% | AbovePivot@10 | Fellback@5 | StopHit | "
             f"Trade avg |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, outs in groups:
        if not outs:
            continue
        a = agg(outs, horizon)
        lines.append(
            f"| {name} | {a['n']} | {fmt(a['trig_rate'], '%', 0)} | "
            f"{fmt(a['med_days'], '', 0)} | {a['n_resolved']} | "
            f"{fmt(a['ret_h'], '%')} | {fmt(a['med_ret_h'], '%')} | "
            f"{fmt(a['win'], '%', 0)} | {fmt(a['above_p10'], '%', 0)} | "
            f"{fmt(a['fellback5'], '%', 0)} | {fmt(a['stop_hit'], '%', 0)} | "
            f"{fmt(a['trade_ret'], '%')} |")
    return "\n".join(lines)


def bucket(outs: list[Outcome], key, bounds: list[tuple[str, float, float]]):
    groups = []
    for name, lo, hi in bounds:
        groups.append((name, [o for o in outs if lo <= key(o.ep.start) < hi]))
    return groups


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, default=HISTORY_CSV)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                    help="post-trigger horizon in sessions (default 20)")
    ap.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT,
                    help="stop distance %% below fill for the trade sim")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "bb_backtest_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-04-01",
                    help="price download start date")
    ap.add_argument("--entry", choices=["touch", "close", "confirmed"],
                    default=DEFAULT_ENTRY,
                    help="trigger definition: touch = intraday High >= pivot "
                         "(buy-stop fill at max(pivot, open)); close = close "
                         ">= pivot, enter next open; confirmed = close >= "
                         "pivot on >=1.5x 20d volume, enter next open")
    ap.add_argument("--write-ledger", nargs="?", type=Path,
                    const=OUTCOMES_CSV, default=None, metavar="PATH",
                    help="also upsert per-episode outcomes into a tracked "
                         f"CSV (default {OUTCOMES_CSV.relative_to(SKILL_DIR)})."
                         " render_history_html.py joins this against "
                         "history.csv to draw realized outcomes; without it "
                         "the dashboard can only show the setup up to its "
                         "trigger. Re-run after each scan to keep it current.")
    args = ap.parse_args()

    episodes, run_days = load_episodes(args.history)
    tickers = sorted({e.ticker for e in episodes})
    print(f"history: {len(run_days)} run-days, {len(episodes)} episodes, "
          f"{len(tickers)} tickers", file=sys.stderr)

    prices = fetch_prices(tickers + ["SPY"], args.start, args.cache,
                          args.refresh_prices)
    missing = [t for t in tickers if t not in prices]
    spy = prices["SPY"]
    calendar = spy.index

    outcomes: list[Outcome] = []
    skipped = 0
    unit_mismatch: list[str] = []
    for ep in episodes:
        if ep.ticker not in prices:
            skipped += 1
            continue
        bars = prices[ep.ticker]
        if not units_consistent(ep, bars):
            unit_mismatch.append(f"{ep.ticker}@{ep.start_day}")
            continue
        o = resolve_episode(ep, bars, spy, calendar, args.horizon,
                            args.stop_pct, args.entry)
        if o is None:
            skipped += 1
            continue
        outcomes.append(o)

    if args.write_ledger:
        rows = ledger_rows(outcomes, args.horizon, args.stop_pct, args.entry)
        total = write_ledger(rows, args.write_ledger)
        print(f"ledger: upserted {len(rows)} episodes into "
              f"{args.write_ledger} ({total} rows total)", file=sys.stderr)

    trig = [o for o in outcomes if o.triggered]
    a = agg(outcomes, args.horizon)

    print(f"# base-breakout outcome backtest — history "
          f"{run_days[0]} → {run_days[-1]} — entry mode: **{args.entry}**\n")
    print(f"**Episodes**: {len(outcomes)} resolved-or-watching "
          f"({skipped} skipped: still-open/no-data; "
          f"{len(missing)} tickers missing prices: {', '.join(missing) or '—'})")
    if unit_mismatch:
        print(f"**Dropped for unit mismatch** (corporate-action re-adjustment, "
              f"n={len(unit_mismatch)}): {', '.join(unit_mismatch)}")
    print(f"**Trigger rate**: {fmt(a['trig_rate'], '%', 0)} of episodes with a "
          f"complete watch window touched their pivot "
          f"(n={a['n_trig']} triggered, median {fmt(a['med_days'], '', 0)} "
          f"sessions to trigger)")
    print(f"**Post-trigger (+{args.horizon} sessions, n={a['n_resolved']})**: "
          f"avg {fmt(a['ret_h'], '%')} / median {fmt(a['med_ret_h'], '%')} "
          f"from fill, win rate {fmt(a['win'], '%', 0)}, "
          f"alpha vs SPY {fmt(a['alpha'], 'pt')}")
    print(f"**Breakout durability**: above pivot at +10 sessions "
          f"{fmt(a['above_p10'], '%', 0)}; fell back below pivot within 5 "
          f"sessions {fmt(a['fellback5'], '%', 0)}")
    print(f"**Stop-based trade** (buy fill, {args.stop_pct:.0f}% stop, exit "
          f"+{args.horizon}d close): avg {fmt(a['trade_ret'], '%')} per trade, "
          f"stop hit {fmt(a['stop_hit'], '%', 0)} of the time")
    gaps = [o.gap_pct for o in trig if o.gap_pct is not None]
    if gaps:
        print(f"**Fill slippage**: avg gap over pivot "
              f"{float(np.mean(gaps)):.2f}% (median {float(np.median(gaps)):.2f}%)")

    nt = [o for o in outcomes if not o.triggered and not o.censored]
    bd = [o for o in nt if o.broke_down]
    drifts = [o.no_trigger_drift for o in nt if o.no_trigger_drift is not None]
    if nt:
        print(f"**Never triggered** (n={len(nt)}): avg drift "
              f"{fmt(float(np.mean(drifts)) if drifts else None, '%')} over the "
              f"watch window; {len(bd)} ({100 * len(bd) / len(nt):.0f}%) broke "
              f"down ≥{args.stop_pct:.0f}% without ever touching the pivot")

    print(strata_table("By Base Score at episode start", bucket(
        outcomes, lambda x: x.score,
        [("<50", 0, 50), ("50–59", 50, 60), ("60–69", 60, 70), ("≥70", 70, 999)]),
        args.horizon))

    sig_groups = [(sig, [o for o in outcomes
                         if o.ep.start.signal.strip("*") == sig])
                  for sig in ("📊", "⏳", "🔥", "🚀")]
    print(strata_table("By Signal at episode start", sig_groups, args.horizon))

    best_groups = [(f"best={sig}", [o for o in outcomes
                                    if o.ep.best_signal().strip("*") == sig])
                   for sig in ("📊", "⏳", "🔥", "🚀")]
    print(strata_table("By best Signal reached during episode", best_groups,
                       args.horizon))

    print(strata_table("By BB%ile at episode start", bucket(
        outcomes, lambda x: x.bb_pctile,
        [("≤10 (max squeeze)", 0, 10.001), ("10–25", 10.001, 25),
         (">25", 25, 999)]), args.horizon))

    print(strata_table("By Vol dry-up at episode start", bucket(
        outcomes, lambda x: x.vol_dryup,
        [("<0.70 deep", 0, 0.70), ("0.70–0.90", 0.70, 0.90),
         ("≥0.90 none", 0.90, 99)]), args.horizon))

    print(strata_table("By RS slope at episode start", bucket(
        outcomes, lambda x: x.rs_slope,
        [("≤0 (losing RS)", -99, 0), ("0–1", 0, 1), (">1 strong", 1, 99)]),
        args.horizon))

    print(strata_table("By distance to pivot at start", bucket(
        outcomes, lambda x: x.to_pivot,
        [("≥-3% near", -3, 99), ("-10..-3%", -10, -3), ("<-10% far", -99, -10)]),
        args.horizon))

    print(strata_table("By base length", bucket(
        outcomes, lambda x: x.base_weeks,
        [("<10wk", 0, 10), ("10–<20wk", 10, 20), ("≥20wk", 20, 99)]),
        args.horizon))

    print(strata_table("By base width", bucket(
        outcomes, lambda x: x.width_pct,
        [("<10%", 0, 10), ("10–15%", 10, 15), (">15%", 15, 99)]),
        args.horizon))

    print(strata_table("By days-to-trigger (triggered only)", [
        (name, [o for o in trig if o.days_to_trigger is not None
                and lo <= o.days_to_trigger < hi])
        for name, lo, hi in [("1–2d", 1, 3), ("3–5d", 3, 6), ("6–10d", 6, 11),
                             (">10d", 11, 999)]], args.horizon))

    print("\n_Columns: Eps = episodes; Trig% = pivot touched during watch "
          f"window; n(res) = triggered with full post-window; Ret{args.horizon}"
          f" = close-to-fill return at +{args.horizon} sessions; AbovePivot@10 "
          "= close ≥ trigger pivot at +10; Fellback@5 = any close back below "
          "pivot in sessions 1–5 after the trigger day (the trigger day's own "
          "close is not counted); Trade avg = mean return of the stop-based "
          "trade._")


if __name__ == "__main__":
    main()
