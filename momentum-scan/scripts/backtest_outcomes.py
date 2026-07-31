#!/usr/bin/env python3
"""Outcome backtest for momentum-scan history.

Replays state/history.csv with the skill's canonical trade convention: a
name is "held" while it stays in the displayed top-N (an episode =
consecutive run-days at rank <= top-N), entered at the close of its first
listed day and sold at the close of the day its dropout is observed (the
first run-day it no longer appears — the scan's only built-in exit). The
running scan already quotes calibration numbers from a one-off 2026-07
analysis; this script makes that analysis re-runnable quarterly so edge
decay is detectable. It measures three things the live scan can't:

  - the dropout rule's value — held-to-dropout return vs holding through
    the dropout for +5/+10/+20 more sessions and to the data edge, split
    by whether the episode ever reached the top 10 (the claim: dropout is
    an excellent stop for former leaders, noise for marginal names);
  - post-dropout drift — forward returns measured from the exit itself
    (the claim: dropped names are weakest in the first two weeks);
  - entry-quality tiers — episodes stratified by the entry-day volume
    character the scan tags entrants with (surge >= 1.5x, quiet < 0.8x,
    clean <= 1 distribution day), plus rank / score / re-entry strata.

--fills next-open replays realistic execution instead: the scan output
only exists after the close, so entry and exit both fill at the NEXT
session's open. Compare with the default --fills close to see how much of
the measured difference survives execution timing.

--exit last-listed reproduces the pre-script one-off convention for
reconciliation ONLY: every episode (open ones included) is marked at its
last LISTED day's close instead of the dropout-observation day's. That
convention is look-ahead (you can't know a day was the last listed day
until the next scan) and marks still-open winners at unresolved paper
gains — it's how the original +9.0%/+3.1% tier calibration was produced,
and running it should reproduce those episode counts. Never use it to
judge the strategy.

Prices are fetched with auto_adjust=True. No recorded price level is
replayed (entry and exit both come from the same adjusted series), so
units stay internally consistent through splits; corporate actions that
break the series (spinoffs, delistings) are caught where possible by
checking the recorded scan-day close (rows since 2026-07-30) against the
series — >5% disagreement drops the episode as unit_mismatch rather than
resolving it on broken units.

Two censoring caveats, both reported rather than hidden: episodes that
start on the very first history run-day are LEFT-CENSORED (the name may
have been leading long before tracking began — their "entry" is an
artifact), and episodes still listed on the last run-day are OPEN. The
day-1 cohort is kept in the aggregate tables (the in-repo calibration
numbers included it) but broken out in its own stratum so the cohort
effect is visible.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--top-n 30] [--fills next-open] \
    [--refresh-prices]
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

POST_WINDOWS = (5, 10, 20)  # sessions past the dropout fill

# Entry-quality thresholds — keep in lockstep with scan.py's
# ENTRY_VOL_SURGE_MIN / ENTRY_VOL_QUIET_MAX / ENTRY_CLEAN_DIST_MAX.
VOL_SURGE_MIN = 1.5
VOL_QUIET_MAX = 0.8
CLEAN_DIST_MAX = 1


def ts_of(run_day: str) -> pd.Timestamp:
    """YYYYMMDD run-day string → Timestamp."""
    return pd.Timestamp(f"{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}")


def ffloat(v) -> float | None:
    try:
        return float(v) if v not in ("", None, "None") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- history

@dataclass
class Episode:
    ticker: str
    start_day: str              # first run-day listed (YYYYMMDD)
    last_day: str               # last run-day listed
    dropout_day: str | None     # run-day absence was observed; None = open
    tenure: int                 # run-days listed
    peak_rank: int              # best (lowest) display rank reached
    entry_rank: int
    entry_score: float | None
    entry_vol_ratio: float | None
    entry_dist_days: float | None
    entry_close_rec: float | None   # scan-recorded close, when available
    left_censored: bool         # started on history's first run-day
    reentry: bool               # ticker had an earlier episode
    # filled by resolve():
    htd: float | None = None            # held-to-dropout %, entry→exit fill
    post: dict = field(default_factory=dict)   # w → % from exit fill
    hold: dict = field(default_factory=dict)   # w → % entry→dropout+w close
    hold_end: float | None = None       # % entry→last available bar
    spy_htd: float | None = None        # SPY over the same entry→exit dates
    spy_hold10: float | None = None     # SPY entry→dropout+10 sessions
    unrealized: float | None = None     # open episodes: entry→last bar


def entry_tier(vol_ratio: float | None, dist_days: float | None) -> str:
    """Mirror scan.py's entry_quality() labels."""
    if vol_ratio is None or dist_days is None:
        return "n/a (fields missing)"
    if vol_ratio >= VOL_SURGE_MIN:
        return "🟢 surge+clean" if dist_days <= CLEAN_DIST_MAX else "🔵 surge"
    if vol_ratio < VOL_QUIET_MAX:
        return "🟠 quiet drift-in"
    return "⚪ neutral"


def load_episodes(history_path: Path,
                  top_n: int) -> tuple[list[Episode], list[str]]:
    with open(history_path, newline="", encoding="utf-8") as f:
        raw = [r for r in csv.DictReader(f) if r.get("rank")]
    # Same-day re-runs overwrite in scan.py; keep last row per (day, ticker)
    # in case an old file predates that.
    dedup: dict[tuple[str, str], dict] = {}
    for r in raw:
        dedup[(r["run_id"], r["ticker"])] = r
    run_days = sorted({day for day, _ in dedup})
    day_idx = {d: i for i, d in enumerate(run_days)}

    # History rows cover every kept pick, not just the displayed top-N —
    # membership (and therefore dropout) is defined on rank <= top_n, the
    # same read-time filter scan.py's persistence stats apply.
    listed: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for (day, t), r in dedup.items():
        if int(r["rank"]) <= top_n:
            listed[t].append((day_idx[day], r))

    episodes: list[Episode] = []
    for t, apps in listed.items():
        apps.sort(key=lambda a: a[0])
        groups: list[list[tuple[int, dict]]] = [[apps[0]]]
        for a in apps[1:]:
            if a[0] == groups[-1][-1][0] + 1:
                groups[-1].append(a)
            else:
                groups.append([a])
        for gi, g in enumerate(groups):
            first, last = g[0], g[-1]
            entry = first[1]
            episodes.append(Episode(
                ticker=t,
                start_day=run_days[first[0]],
                last_day=run_days[last[0]],
                dropout_day=(run_days[last[0] + 1]
                             if last[0] + 1 < len(run_days) else None),
                tenure=len(g),
                peak_rank=min(int(r["rank"]) for _, r in g),
                entry_rank=int(entry["rank"]),
                entry_score=ffloat(entry.get("score")),
                entry_vol_ratio=ffloat(entry.get("vol_ratio_20d")),
                entry_dist_days=ffloat(entry.get("dist_days_25d")),
                entry_close_rec=ffloat(entry.get("close")),
                left_censored=first[0] == 0,
                reentry=gi > 0,
            ))
    episodes.sort(key=lambda e: (e.start_day, e.ticker))
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
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if len(df):
                data[t] = df
                got_any = True
        if got_any:
            no_data.update(t for t in chunk if t not in data)
        time.sleep(1.0)

    data[NO_DATA_KEY] = no_data
    with open(cache, "wb") as f:
        pickle.dump(data, f)
    return data


# ---------------------------------------------------------------- resolve

def pos_of(bars: pd.DataFrame, date: pd.Timestamp) -> int | None:
    """Index of the bar for `date` (or the nearest prior bar within 5
    calendar days — a small data hole). None = series doesn't cover it."""
    idx = bars.index
    p = idx.searchsorted(date, side="right") - 1
    if p < 0 or (date - idx[p]).days > 5:
        return None
    return int(p)


def resolve(ep: Episode, bars: pd.DataFrame, spy: pd.DataFrame,
            fills: str, exit_mode: str = "dropout") -> str:
    """Fill the episode's outcome fields in place. Returns a status:
    resolved / open / pending / no_bars / unit_mismatch.

    fills "close" is the canonical convention (enter at the entry-day
    close, sell at the dropout-observation-day close). "next-open" is
    realistic execution: both scan outputs exist only after the close, so
    both fills move to the following session's open. "pending" = the
    dropout is too recent for its next-open fill or the entry fill bar to
    exist yet. exit_mode "last-listed" is the look-ahead reconciliation
    convention (see module docstring): exit at the last listed day's
    close, open episodes resolved there too."""
    closes = bars["Close"]
    p_in = pos_of(bars, ts_of(ep.start_day))
    if p_in is None:
        return "no_bars"

    # Unit check where the scan recorded the close it saw (post-2026-07-30
    # rows). Mid-session runs may have recorded a partial bar that differs
    # from the completed one; the prior bar gets a try, like the sister
    # scripts.
    if ep.entry_close_rec:
        ratio = float(closes.iloc[p_in]) / ep.entry_close_rec
        if abs(ratio - 1) > 0.02 and p_in >= 1:
            alt = float(closes.iloc[p_in - 1]) / ep.entry_close_rec
            if abs(alt - 1) < abs(ratio - 1):
                ratio = alt
        if abs(ratio - 1) > 0.05:
            return "unit_mismatch"

    if fills == "next-open":
        if p_in + 1 >= len(bars):
            return "pending"
        entry_px = float(bars["Open"].iloc[p_in + 1])
    else:
        entry_px = float(closes.iloc[p_in])
    if not np.isfinite(entry_px) or entry_px <= 0:
        return "no_bars"

    if exit_mode == "last-listed":
        exit_day = ep.last_day
    else:
        if ep.dropout_day is None:
            ep.unrealized = (float(closes.iloc[-1]) / entry_px - 1) * 100
            return "open"
        exit_day = ep.dropout_day

    p_out = pos_of(bars, ts_of(exit_day))
    if p_out is None:
        return "no_bars"  # series ended before the dropout (delisting)
    if fills == "next-open":
        if p_out + 1 >= len(bars):
            return "pending"
        base = p_out + 1
        exit_px = float(bars["Open"].iloc[base])
    else:
        base = p_out
        exit_px = float(closes.iloc[base])
    if not np.isfinite(exit_px) or exit_px <= 0:
        return "no_bars"

    ep.htd = (exit_px / entry_px - 1) * 100
    for w in POST_WINDOWS:
        if base + w < len(closes):
            c = float(closes.iloc[base + w])
            ep.post[w] = (c / exit_px - 1) * 100
            ep.hold[w] = (c / entry_px - 1) * 100
    ep.hold_end = (float(closes.iloc[-1]) / entry_px - 1) * 100

    sp_in = pos_of(spy, ts_of(ep.start_day))
    sp_out = pos_of(spy, ts_of(exit_day))
    if sp_in is not None and sp_out is not None:
        sc = spy["Close"]
        ep.spy_htd = (float(sc.iloc[sp_out]) / float(sc.iloc[sp_in]) - 1) * 100
        if sp_out + 10 < len(sc):
            ep.spy_hold10 = (float(sc.iloc[sp_out + 10])
                             / float(sc.iloc[sp_in]) - 1) * 100
    return "resolved"


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:.{nd}f}{suffix}"


def mean_of(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return float(np.mean(xs)) if xs else None


def agg(eps: list[Episode]) -> dict:
    htds = [e.htd for e in eps if e.htd is not None]
    return {
        "n": len(eps),
        "tenure": float(np.mean([e.tenure for e in eps])) if eps else None,
        "top10": (100 * sum(e.peak_rank <= 10 for e in eps) / len(eps)
                  if eps else None),
        "htd": float(np.mean(htds)) if htds else None,
        "med": float(np.median(htds)) if htds else None,
        "win": 100 * sum(h > 0 for h in htds) / len(htds) if htds else None,
        "post10": mean_of([e.post.get(10) for e in eps]),
    }


def strata_table(title: str, groups: list[tuple[str, list[Episode]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Stratum | n | Tenure | Top-10% | HTD% | Med% | Win% | "
             "Post+10d% |",
             "|---|---|---|---|---|---|---|---|"]
    for name, eps in groups:
        if not eps:
            continue
        a = agg(eps)
        lines.append(
            f"| {name} | {a['n']} | {fmt(a['tenure'])} | "
            f"{fmt(a['top10'], '%', 0)} | {fmt(a['htd'], '%', 2)} | "
            f"{fmt(a['med'], '%', 2)} | {fmt(a['win'], '%', 0)} | "
            f"{fmt(a['post10'], '%', 2)} |")
    return "\n".join(lines)


def hold_row(name: str, eps: list[Episode]) -> str:
    # The sell-vs-hold delta must be computed on the PAIRED subset (episodes
    # whose +10d bar exists), or censoring near the data edge biases it.
    paired = [e for e in eps if e.hold.get(10) is not None]
    delta = (float(np.mean([e.htd for e in paired]))
             - float(np.mean([e.hold[10] for e in paired]))) if paired else None
    return (f"| {name} | {len(eps)} | {fmt(mean_of([e.htd for e in eps]), '%', 2)} | "
            f"{fmt(mean_of([e.hold.get(5) for e in eps]), '%', 2)} | "
            f"{fmt(mean_of([e.hold.get(10) for e in eps]), '%', 2)} | "
            f"{fmt(mean_of([e.hold.get(20) for e in eps]), '%', 2)} | "
            f"{fmt(mean_of([e.hold_end for e in eps]), '%', 2)} | "
            f"{fmt(delta, 'pt', 2)} |")


def post_row(name: str, eps: list[Episode]) -> str:
    cells = []
    for w in POST_WINDOWS:
        vals = [e.post[w] for e in eps if e.post.get(w) is not None]
        cells.append(f"{len(vals)} | {fmt(mean_of(vals), '%', 2)}")
    return f"| {name} | " + " | ".join(cells) + " |"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, default=HISTORY_CSV)
    ap.add_argument("--top-n", type=int, default=30,
                    help="list membership cutoff (display rank), matching "
                         "scan.py's default top-N")
    ap.add_argument("--fills", choices=["close", "next-open"], default="close",
                    help="close = canonical (entry-day close in, "
                         "dropout-day close out); next-open = realistic "
                         "execution at the following session's open for "
                         "both fills")
    ap.add_argument("--exit", choices=["dropout", "last-listed"],
                    default="dropout", dest="exit_mode",
                    help="dropout = honest convention (sell when the "
                         "dropout is observed); last-listed = look-ahead "
                         "reconciliation mode reproducing the pre-script "
                         "calibration (exit at the last listed day's "
                         "close, open episodes marked there too) — never "
                         "use it to judge the strategy")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir())
                    / "momentum_backtest_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-05-01",
                    help="price download start date")
    args = ap.parse_args()

    episodes, run_days = load_episodes(args.history, args.top_n)
    tickers = sorted({e.ticker for e in episodes})
    print(f"history: {len(run_days)} run-days, {len(episodes)} episodes, "
          f"{len(tickers)} tickers", file=sys.stderr)

    prices = fetch_prices(tickers + ["SPY"], args.start, args.cache,
                          args.refresh_prices)
    spy = prices["SPY"]
    missing = sorted(t for t in tickers if t not in prices)

    closed: list[Episode] = []
    open_eps: list[Episode] = []
    n_pending = n_no_bars = 0
    unit_mismatch: list[str] = []
    for e in episodes:
        if e.ticker not in prices:
            n_no_bars += 1
            continue
        status = resolve(e, prices[e.ticker], spy, args.fills, args.exit_mode)
        if status == "resolved":
            closed.append(e)
        elif status == "open":
            open_eps.append(e)
        elif status == "pending":
            n_pending += 1
        elif status == "no_bars":
            n_no_bars += 1
        elif status == "unit_mismatch":
            unit_mismatch.append(f"{e.ticker}@{e.start_day}")

    day1 = [e for e in closed if e.left_censored]
    entrants = [e for e in closed if not e.left_censored]
    a = agg(closed)

    print(f"# momentum outcome backtest — history {run_days[0]} → "
          f"{run_days[-1]} — top-{args.top_n} membership — fills: "
          f"**{args.fills}** — exit: **{args.exit_mode}**\n")
    if args.exit_mode == "last-listed":
        print("⚠️ RECONCILIATION MODE — look-ahead exits, open episodes "
              "marked at paper gains. For comparing against the pre-script "
              "calibration numbers only; not a strategy measurement. Every "
              "held-to-dropout (HTD) figure below is really "
              "held-to-last-listed-day in this mode.\n")
    print(f"**Episodes**: {len(episodes)} total — {len(closed)} closed, "
          f"{len(open_eps)} still listed (open), {n_pending} pending fills, "
          f"{n_no_bars} unresolvable (no/ended price data"
          f"{': ' + ', '.join(missing) if missing else ''}), "
          f"{len(unit_mismatch)} unit-mismatch dropped"
          f"{' (' + ', '.join(unit_mismatch) + ')' if unit_mismatch else ''}")
    htd_label = ("held-to-last-listed" if args.exit_mode == "last-listed"
                 else "held-to-dropout")
    print(f"**Closed episodes**: avg tenure {fmt(a['tenure'])} run-days, "
          f"{htd_label} {fmt(a['htd'], '%', 2)} avg / "
          f"{fmt(a['med'], '%', 2)} median, win rate {fmt(a['win'], '%', 0)}, "
          f"top-10 reach {fmt(a['top10'], '%', 0)}; day-1 left-censored "
          f"cohort {len(day1)} of {len(closed)}")
    print(f"**Open now**: {len(open_eps)} episodes, avg unrealized "
          f"{fmt(mean_of([e.unrealized for e in open_eps]), '%', 2)}, avg "
          f"tenure so far "
          f"{fmt(float(np.mean([e.tenure for e in open_eps])) if open_eps else None)} "
          f"run-days")

    top10 = [e for e in closed if e.peak_rank <= 10]
    marginal = [e for e in closed if e.peak_rank > 10]

    print("\n### Sell-on-dropout vs holding through it\n")
    print("| Cohort | n | Sell@drop% | Hold+5d% | Hold+10d% | Hold+20d% | "
          "Hold→end% | Sell−Hold10 |")
    print("|---|---|---|---|---|---|---|---|")
    print(hold_row("All closed", closed))
    print(hold_row("Reached top-10", top10))
    print(hold_row("Marginal (peak 11+)", marginal))
    print(hold_row("True entrants only", entrants))
    spy_pairs = [e for e in closed if e.spy_htd is not None]
    print(f"\nSPY over the same windows: entry→dropout "
          f"{fmt(mean_of([e.spy_htd for e in spy_pairs]), '%', 2)}, "
          f"entry→dropout+10d "
          f"{fmt(mean_of([e.spy_hold10 for e in spy_pairs]), '%', 2)} "
          f"(n={len(spy_pairs)}) — the hold columns' drag is only meaningful "
          f"net of this beta.")

    print("\n### Post-dropout drift (measured from the exit fill)\n")
    print("| Cohort | n+5d | +5d% | n+10d | +10d% | n+20d | +20d% |")
    print("|---|---|---|---|---|---|---|")
    print(post_row("All closed", closed))
    print(post_row("Reached top-10", top10))
    print(post_row("Marginal (peak 11+)", marginal))

    print(strata_table("By entry-quality tier (scan.py thresholds)", [
        (t, [e for e in closed
             if entry_tier(e.entry_vol_ratio, e.entry_dist_days) == t])
        for t in ("🟢 surge+clean", "🔵 surge", "⚪ neutral",
                  "🟠 quiet drift-in", "n/a (fields missing)")
    ]))

    print(strata_table("By entry-day distribution days (25d)", [
        ("0–1 clean", [e for e in closed if e.entry_dist_days is not None
                       and e.entry_dist_days <= 1]),
        ("2–3", [e for e in closed if e.entry_dist_days is not None
                 and 2 <= e.entry_dist_days <= 3]),
        ("4+ loaded", [e for e in closed if e.entry_dist_days is not None
                       and e.entry_dist_days >= 4]),
    ]))

    print(strata_table("By entry rank", [
        ("1–10", [e for e in closed if e.entry_rank <= 10]),
        ("11–20", [e for e in closed if 11 <= e.entry_rank <= 20]),
        ("21+", [e for e in closed if e.entry_rank >= 21]),
    ]))

    print(strata_table("Debut vs re-entry (true entrants)", [
        ("First-ever debut", [e for e in entrants if not e.reentry]),
        ("Re-entry", [e for e in entrants if e.reentry]),
    ]))

    scores = sorted(e.entry_score for e in closed if e.entry_score is not None)
    if len(scores) >= 9:
        t1, t2 = scores[len(scores) // 3], scores[2 * len(scores) // 3]
        print(strata_table(
            f"By entry Score (terciles: {t1:.1f} / {t2:.1f})", [
                (f"< {t1:.1f}", [e for e in closed
                                 if e.entry_score is not None
                                 and e.entry_score < t1]),
                (f"{t1:.1f}–{t2:.1f}", [e for e in closed
                                        if e.entry_score is not None
                                        and t1 <= e.entry_score < t2]),
                (f"≥ {t2:.1f}", [e for e in closed
                                 if e.entry_score is not None
                                 and e.entry_score >= t2]),
            ]))

    print(strata_table("Censoring cohorts", [
        ("Day-1 cohort (left-censored)", day1),
        ("True entrants", entrants),
    ]))

    mid = run_days[len(run_days) // 2]
    print(strata_table(
        f"Stability: entries first vs second half (split at {mid})", [
            ("H1 entrants", [e for e in entrants if e.start_day <= mid]),
            ("H2 entrants", [e for e in entrants if e.start_day > mid]),
        ]))

    print("\n_Columns: Tenure = run-days listed (open episodes excluded "
          "everywhere except the headline); Top-10% = share of episodes "
          "whose best rank reached the top 10; HTD% = held-to-dropout "
          "return, entry fill → dropout fill; Med% / Win% = median / share "
          "positive of HTD; Post+10d% = return over the 10 sessions after "
          "the exit fill (censored near the data edge — its n is smaller "
          "than the stratum's). Hold+Nd% = entry fill → close N sessions "
          "past the dropout; Hold→end% mixes horizons (older episodes have "
          "had longer to drift) — read it as 'what buy-and-hold did with "
          "the same entries', not as a per-trade expectancy. Sell−Hold10 "
          "is computed on the paired subset only. The day-1 cohort's entry "
          "attributes describe an arbitrary tracking-start day, not a real "
          "entry — the tier and rank strata inherit that noise; the "
          "censoring table sizes it. Consecutive episodes of one ticker "
          "and overlapping episodes across tickers share market beta — "
          "strata are descriptive, not independent samples._")


if __name__ == "__main__":
    main()
