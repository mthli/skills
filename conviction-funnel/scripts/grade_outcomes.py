#!/usr/bin/env python3
"""Outcome grading for conviction-funnel run ledgers.

The funnel's final table used to evaporate the moment it was printed —
the one layer of the pipeline that actually says "buy here, bail there"
had no feedback loop. Every funnel run now transcribes its output into
state/runs/<YYYY-MM-DD>.json (schema below, enforced by --validate), and
this script replays the accumulated ledger quarterly, like the sister
scans' backtest_outcomes.py. It grades two separable claims:

  - SELECTION — did the funnel rank names correctly? Raw forward returns
    (spot → close +5/10/20 sessions, no entry logic) compared across
    roles: finalists should beat runner-ups, and both should beat the
    names the deep-dive rejected. Rejections are predictions too.
  - EXECUTION — were the entry/stop plans any good? Finalist plans are
    replayed mechanically: market entries fill at the next session's
    open; pullback-limit fills the first day Low touches the level (at
    min(level, open)); pivot-stop fills the first day High touches it
    (at max(level, open)), skipped as gap_skip if the open is already
    >3% past the level (the funnel's own no-chase rule). A fill that
    lands at or below the stop itself (a crash gap through the plan) is
    skipped as gap_through_stop — untradable per plan, counted, never
    graded. After a fill, the stop exits at min(stop, open) on the first
    day Low touches it — including the fill day itself: a same-day
    fill→stop double-touch resolves pessimistically as stopped and is
    counted separately as ambiguous. Otherwise the trade is marked at
    +20 sessions. Realized R = P&L over planned risk (fill − stop).

Ledger schema (one file per run):

  {
    "run_date": "2026-07-31",             # ET date of the run
    "regime": {"state": "RISK-ON", "score": 6, "flags": "..."},
    "note": "optional free text",
    "picks": [
      {
        "ticker": "ELV",
        "role": "finalist",               # finalist | runner-up | rejected
        "tags": ["momentum", "mr-pocket"],# subset of: momentum, base-pocket, mr-pocket
        "spot": 412.5,                    # reference close at run time
        # finalist-only (required there, ignored elsewhere):
        "verdict": "✅",                  # ✅ | ⚠️
        "entry": {"type": "pullback-limit",  # market | pullback-limit | pivot-stop
                   "level": 405.0,           # omit for market
                   "valid_sessions": 10},    # optional, default 10
        "stop": 389.0,
        "size": "normal",                 # normal | half | minimal
        # optional, any role:
        "sector": "Healthcare",
        "upside_ref": {"level": 460.0, "basis": "analyst high"},
        "earnings_within_4wk": false,
        "invalidation": "loses 200DMA on volume",
        "reason": "why rejected / why runner-up"
      }
    ]
  }

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python grade_outcomes.py [--refresh-prices]
  # schema-check one ledger (exit 1 on problems; run after writing it):
  ... python grade_outcomes.py --validate ../state/runs/2026-07-31.json
"""

from __future__ import annotations

import argparse
import json
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
RUNS_DIR = SKILL_DIR / "state" / "runs"

FWD_WINDOWS = (5, 10, 20)   # sessions past the run day (selection view)
HORIZON = 20                # sessions past the fill (execution view)
GAP_SKIP_PCT = 3.0          # no-chase rule: skip pivot fills gapping past this
DEFAULT_VALID_SESSIONS = 10

ROLES = ("finalist", "runner-up", "rejected")
TAGS = ("momentum", "base-pocket", "mr-pocket")
ENTRY_TYPES = ("market", "pullback-limit", "pivot-stop")
VERDICTS = ("✅", "⚠️")
SIZES = ("normal", "half", "minimal")


# ---------------------------------------------------------------- ledger

@dataclass
class Pick:
    run_date: str
    regime_state: str
    ticker: str
    role: str
    tags: list
    spot: float
    verdict: str | None = None
    entry_type: str | None = None
    entry_level: float | None = None
    valid_sessions: int = DEFAULT_VALID_SESSIONS
    stop: float | None = None
    size: str | None = None
    # filled by resolve():
    status: str = "pending"        # graded / unfilled / gap_skip /
    #                                gap_through_stop / pending / no_bars /
    #                                unit_mismatch
    fwd: dict = field(default_factory=dict)      # w -> % spot → close+w
    spy_fwd: dict = field(default_factory=dict)  # w -> % SPY same window
    filled: bool | None = None     # finalists: did the entry trigger?
    fill_px: float | None = None
    stopped: bool | None = None
    ambiguous: bool = False        # stop hit on the fill bar itself
    realized_r: float | None = None   # P&L / planned risk, stop or +20d mark
    fill_fwd20: float | None = None   # % fill → close +20 sessions, no stop


def validate_ledger(doc: dict, path: str = "<ledger>") -> list[str]:
    errs: list[str] = []

    def err(msg):
        errs.append(f"{path}: {msg}")

    if not isinstance(doc, dict):
        return [f"{path}: top level must be an object"]
    rd = doc.get("run_date", "")
    if not (isinstance(rd, str) and len(rd) == 10 and rd[4] == rd[7] == "-"):
        err(f"run_date must be YYYY-MM-DD, got {rd!r}")
    regime = doc.get("regime")
    if not (isinstance(regime, dict) and regime.get("state")):
        err("regime.state is required")
    picks = doc.get("picks")
    if not (isinstance(picks, list) and picks):
        return errs + [f"{path}: picks must be a non-empty list"]
    for i, p in enumerate(picks):
        where = f"picks[{i}] ({p.get('ticker', '?')})"
        if not p.get("ticker"):
            err(f"{where}: ticker is required")
        if p.get("role") not in ROLES:
            err(f"{where}: role must be one of {ROLES}")
        tags = p.get("tags")
        if not (isinstance(tags, list) and tags
                and all(t in TAGS for t in tags)):
            err(f"{where}: tags must be a non-empty subset of {TAGS}")
        if not isinstance(p.get("spot"), (int, float)):
            err(f"{where}: spot (reference close) is required")
        if p.get("role") == "finalist":
            if p.get("verdict") not in VERDICTS:
                err(f"{where}: finalist verdict must be ✅ or ⚠️")
            entry = p.get("entry")
            if not (isinstance(entry, dict)
                    and entry.get("type") in ENTRY_TYPES):
                err(f"{where}: finalist entry.type must be one of "
                    f"{ENTRY_TYPES}")
            else:
                lvl = entry.get("level")
                if entry["type"] == "market":
                    if lvl is not None:
                        err(f"{where}: market entry must omit level")
                elif not isinstance(lvl, (int, float)):
                    err(f"{where}: {entry['type']} entry requires a "
                        f"numeric level")
            stop = p.get("stop")
            if not isinstance(stop, (int, float)):
                err(f"{where}: finalist stop is required")
            else:
                ref = (p.get("entry") or {}).get("level") or p.get("spot")
                if isinstance(ref, (int, float)) and stop >= ref:
                    err(f"{where}: stop {stop} must sit below the "
                        f"entry reference {ref}")
            if p.get("size") not in SIZES:
                err(f"{where}: finalist size must be one of {SIZES}")
    return errs


def load_ledgers(runs_dir: Path) -> tuple[list[Pick], list[str], list[str]]:
    picks: list[Pick] = []
    run_dates: list[str] = []
    problems: list[str] = []
    for f in sorted(runs_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{f.name}: unreadable ({e})")
            continue
        errs = validate_ledger(doc, f.name)
        if errs:
            problems.extend(errs)
            continue
        run_dates.append(doc["run_date"])
        for p in doc["picks"]:
            entry = p.get("entry") or {}
            picks.append(Pick(
                run_date=doc["run_date"],
                regime_state=doc["regime"]["state"],
                ticker=p["ticker"],
                role=p["role"],
                tags=list(p["tags"]),
                spot=float(p["spot"]),
                verdict=p.get("verdict"),
                entry_type=entry.get("type"),
                entry_level=(float(entry["level"])
                             if entry.get("level") is not None else None),
                valid_sessions=int(entry.get("valid_sessions",
                                             DEFAULT_VALID_SESSIONS)),
                stop=(float(p["stop"])
                      if p.get("stop") is not None else None),
                size=p.get("size"),
            ))
    return picks, sorted(set(run_dates)), problems


# ---------------------------------------------------------------- prices

NO_DATA_KEY = "_NO_DATA"  # cache slot: tickers Yahoo confirmed empty


def fetch_prices(tickers: list[str], start: str, cache: Path,
                 refresh: bool) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            data = pickle.load(f)
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


def resolve_fill(pick: Pick, bars: pd.DataFrame, run_pos: int) -> None:
    """Replay a finalist's entry plan. Sets filled / fill_px / stopped /
    realized_r / fill_fwd20 in place; leaves them None while pending."""
    window = 1 if pick.entry_type == "market" else pick.valid_sessions
    avail = bars.iloc[run_pos + 1:run_pos + 1 + window]
    fill_pos = None
    for off, (_, bar) in enumerate(avail.iterrows()):
        o = float(bar["Open"])
        if pick.entry_type == "market":
            pick.fill_px = o
        elif pick.entry_type == "pullback-limit":
            if float(bar["Low"]) > pick.entry_level:
                continue
            pick.fill_px = min(pick.entry_level, o)
        else:  # pivot-stop
            if float(bar["High"]) < pick.entry_level:
                continue
            if o > pick.entry_level * (1 + GAP_SKIP_PCT / 100):
                pick.filled = False
                pick.status = "gap_skip"
                return
            pick.fill_px = max(pick.entry_level, o)
        fill_pos = run_pos + 1 + off
        break
    if fill_pos is None:
        if len(avail) < window:
            pick.status = "pending"   # validity window not fully elapsed
        else:
            pick.filled = False
            pick.status = "unfilled"
        return

    risk = pick.fill_px - pick.stop
    if risk <= 0:
        # Crash gap: the fill itself lands at/below the stop. Untradable
        # per plan — you'd never open a position already through its stop.
        pick.fill_px = None
        pick.filled = False
        pick.status = "gap_through_stop"
        return
    pick.filled = True
    post = bars.iloc[fill_pos:fill_pos + 1 + HORIZON]
    for off, (_, bar) in enumerate(post.iterrows()):
        if float(bar["Low"]) <= pick.stop:
            exit_px = min(pick.stop, float(bar["Open"]))
            pick.stopped = True
            pick.ambiguous = off == 0   # fill and stop on the same bar
            pick.realized_r = (exit_px - pick.fill_px) / risk
            pick.status = "graded"
            break
    else:
        if len(post) < 1 + HORIZON:
            pick.status = "pending"   # horizon not elapsed, stop not hit
            return
        pick.stopped = False
        end = float(post["Close"].iloc[HORIZON])
        pick.realized_r = (end - pick.fill_px) / risk
        pick.status = "graded"
    if len(bars) > fill_pos + HORIZON:
        pick.fill_fwd20 = (float(bars["Close"].iloc[fill_pos + HORIZON])
                           / pick.fill_px - 1) * 100


def resolve(pick: Pick, bars: pd.DataFrame, spy: pd.DataFrame) -> None:
    date = pd.Timestamp(pick.run_date)
    run_pos = pos_of(bars, date)
    if run_pos is None:
        pick.status = "no_bars"
        return
    series_close = float(bars["Close"].iloc[run_pos])
    if abs(series_close / pick.spot - 1) > 0.05:
        pick.status = "unit_mismatch"
        return

    closes = bars["Close"]
    for w in FWD_WINDOWS:
        if run_pos + w < len(closes):
            pick.fwd[w] = (float(closes.iloc[run_pos + w])
                           / pick.spot - 1) * 100
    sp = pos_of(spy, date)
    if sp is not None:
        sc = spy["Close"]
        for w in FWD_WINDOWS:
            if sp + w < len(sc):
                pick.spy_fwd[w] = (float(sc.iloc[sp + w])
                                   / float(sc.iloc[sp]) - 1) * 100

    if pick.role == "finalist":
        resolve_fill(pick, bars, run_pos)
    else:
        pick.status = "graded" if pick.fwd else "pending"


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:.{nd}f}{suffix}"


def mean_of(vals) -> float | None:
    xs = [v for v in vals if v is not None]
    return float(np.mean(xs)) if xs else None


def sel_row(name: str, picks: list[Pick]) -> str:
    f20 = [p.fwd.get(20) for p in picks if p.fwd.get(20) is not None]
    x20 = [p.fwd[20] - p.spy_fwd[20] for p in picks
           if p.fwd.get(20) is not None and p.spy_fwd.get(20) is not None]
    win = 100 * sum(v > 0 for v in f20) / len(f20) if f20 else None
    return (f"| {name} | {len(picks)} | "
            f"{fmt(mean_of([p.fwd.get(5) for p in picks]), '%', 2)} | "
            f"{fmt(mean_of([p.fwd.get(10) for p in picks]), '%', 2)} | "
            f"{fmt(mean_of(f20), '%', 2)} | {fmt(win, '%', 0)} | "
            f"{fmt(mean_of(x20), 'pp', 2)} |")


def selection_table(title: str, groups: list[tuple[str, list[Pick]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Cohort | n | Fwd+5d | Fwd+10d | Fwd+20d | Win+20 | xSPY+20 |",
             "|---|---|---|---|---|---|---|"]
    lines += [sel_row(name, ps) for name, ps in groups if ps]
    return "\n".join(lines)


def tag_family(p: Pick) -> str:
    key = frozenset(p.tags)
    if len(key) == 3:
        return "all three (crowding)"
    if len(key) == 2:
        return " + ".join(sorted(key))
    return next(iter(key)) + " only"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    ap.add_argument("--validate", type=Path, metavar="LEDGER_JSON",
                    help="schema-check one ledger file and exit (0 ok, 1 not)")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir())
                    / "funnel_grade_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-07-01",
                    help="price download start date")
    args = ap.parse_args()

    if args.validate:
        try:
            doc = json.loads(args.validate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"unreadable: {e}")
        errs = validate_ledger(doc, args.validate.name)
        if errs:
            print("\n".join(errs))
            sys.exit(1)
        n = len(doc["picks"])
        roles = {r: sum(1 for p in doc["picks"] if p["role"] == r)
                 for r in ROLES}
        print(f"OK — {n} picks ({roles['finalist']} finalists, "
              f"{roles['runner-up']} runner-ups, {roles['rejected']} "
              f"rejected)")
        return

    picks, run_dates, problems = load_ledgers(args.runs_dir)
    if problems:
        print("Ledger problems (files/picks skipped):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
    if not picks:
        print(f"No usable run ledgers in {args.runs_dir} — the funnel "
              f"writes one per run (see SKILL.md Step 6). Nothing to grade "
              f"yet.")
        return

    tickers = sorted({p.ticker for p in picks})
    prices = fetch_prices(tickers + ["SPY"], args.start, args.cache,
                          args.refresh_prices)
    spy = prices["SPY"]

    for p in picks:
        if p.ticker in prices:
            resolve(p, prices[p.ticker], spy)
        else:
            p.status = "no_bars"

    graded = [p for p in picks if p.fwd]
    n_pending = sum(p.status == "pending" and not p.fwd for p in picks)
    n_bad = sum(p.status in ("no_bars", "unit_mismatch") for p in picks)
    fin = [p for p in graded if p.role == "finalist"]
    run_up = [p for p in graded if p.role == "runner-up"]
    rej = [p for p in graded if p.role == "rejected"]

    print(f"# conviction-funnel outcome grade — {len(run_dates)} runs "
          f"({run_dates[0]} → {run_dates[-1]})\n")
    print(f"**Picks**: {len(picks)} logged — {len(fin)} finalists, "
          f"{len(run_up)} runner-ups, {len(rej)} rejected with forward "
          f"data; {n_pending} too recent to grade, {n_bad} unresolvable "
          f"(no price data / unit mismatch)")

    print(selection_table(
        "Selection quality (raw forward returns, no entry logic)", [
            ("Finalists ✅", [p for p in fin if p.verdict == "✅"]),
            ("Finalists ⚠️", [p for p in fin if p.verdict == "⚠️"]),
            ("Runner-ups", run_up),
            ("Rejected by deep-dive", rej),
        ]))
    print("\n_The funnel's selection claim is the ordering of those rows: "
          "finalists > runner-ups > rejected. If rejected names outperform "
          "finalists, the deep-dive is subtracting value._")

    filled = [p for p in fin if p.filled]
    done = [p for p in filled if p.status == "graded"]
    stopped = [p for p in done if p.stopped]
    print("\n### Execution (finalist entry/stop plans, replayed "
          "mechanically)\n")
    print(f"- Entry triggered: {len(filled)} of {len(fin)} "
          f"({sum(1 for p in fin if p.status == 'unfilled')} never filled, "
          f"{sum(1 for p in fin if p.status == 'gap_skip')} gap-skipped "
          f"per the >3% no-chase rule, "
          f"{sum(1 for p in fin if p.status == 'gap_through_stop')} opened "
          f"through the stop (crash gap, untradable per plan), "
          f"{sum(1 for p in fin if p.status == 'pending')} pending)")
    if done:
        print(f"- Stop hit within {HORIZON} sessions: {len(stopped)} of "
              f"{len(done)} ({100 * len(stopped) / len(done):.0f}%); "
              f"same-bar fill→stop double-touches (resolved "
              f"pessimistically as stopped): "
              f"{sum(1 for p in stopped if p.ambiguous)}")
        print(f"- Realized R (P&L / planned risk; stopped trades ≈ −1, "
              f"open trades marked at +{HORIZON} sessions): mean "
              f"{fmt(mean_of([p.realized_r for p in done]), '', 2)}R, "
              f"median {fmt(float(np.median([p.realized_r for p in done])), '', 2)}R")
        print(f"- Fill → +{HORIZON} sessions, stop ignored: "
              f"{fmt(mean_of([p.fill_fwd20 for p in done]), '%', 2)} — "
              f"vs realized: the gap is what the stops cost/saved")

    print(selection_table("By signal source", [
        (fam, [p for p in graded if tag_family(p) == fam])
        for fam in sorted({tag_family(p) for p in graded})
    ]))
    print(selection_table("By regime state at run time", [
        (st, [p for p in graded if p.regime_state == st])
        for st in sorted({p.regime_state for p in graded})
    ]))
    if any(p.size for p in fin):
        print(selection_table("Finalists by size prescription", [
            (s, [p for p in fin if p.size == s]) for s in SIZES
        ]))

    print("\n### Per-run recap\n")
    print("| Run | Regime | Finalists (fwd+20) | Rejected (fwd+20) |")
    print("|---|---|---|---|")
    regime_of = {p.run_date: p.regime_state for p in picks}
    for rd in run_dates:
        rf = [p for p in fin if p.run_date == rd]
        rr = [p for p in rej if p.run_date == rd]
        def cell(ps):
            if not ps:
                return "—"
            return ", ".join(
                f"{p.ticker} {fmt(p.fwd.get(20), '%', 1)}" for p in ps)
        print(f"| {rd} | {regime_of.get(rd, '?')} | {cell(rf)} | "
              f"{cell(rr)} |")

    print(f"\n_Selection rows use raw spot→close returns with no entry "
          f"logic, so every role is measured on the same footing; xSPY = "
          f"excess over SPY's identical window. n is the cohort size — "
          f"each forward column averages only the picks whose window "
          f"exists, so effective samples shrink toward +20 near the data "
          f"edge (Win+20 and xSPY+20 use the +20 subset). Execution "
          f"replays the recorded plans: fills as documented in the module "
          f"docstring, stops gap-aware (exit at min(stop, open)), "
          f"same-bar fill→stop double-touches resolved pessimistically "
          f"and counted above. Same-run picks share one tape and runs "
          f"arrive slowly — treat every table as descriptive until the "
          f"ledger holds ~30+ finalists across regimes; the per-run recap "
          f"is the honest unit of review until then._")


if __name__ == "__main__":
    main()
