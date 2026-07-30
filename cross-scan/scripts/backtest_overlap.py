#!/usr/bin/env python3
"""Outcome backtest for cross-scan's overlap premise.

cross-scan's core claim — "a ticker in 2+ sister scans on the same day is
the highest-conviction subset" — has never been measured. This script
reconstructs the daily overlap sets from the four sister scans' own state
histories (momentum / base-breakout / mean-reversion CSVs, UOA markdown
snapshots), joins them per trading session, and measures the underlying
stock's forward EXCESS returns (vs the equal-weight universe mean on the
same session) at T+5 / T+10 / T+20.

What gets tested:
  - the overlap-count gradient itself (1 vs 2 vs 3+ scans);
  - the within-scan confirmation question — for each technical scan, does
    ALSO appearing in another scan (or in UOA) improve that scan's names?
    This is the fair test: UOA flags ~15-20% of the universe daily, so
    "overlaps with UOA" is cheap by construction;
  - every composite-read pattern the SKILL.md sells (⭐ base+call-flow,
    "pullback in a leader", "leader with bearish positioning", ...);
  - rank-awareness: does a top-10 rank in the contributing technical scan
    separate overlap outcomes (the "no per-scan weighting" limitation);
  - fresh vs persistent overlaps, and an H1/H2 stability split.

Sessions are included only when momentum, base-breakout AND UOA all have a
snapshot for that session (those three always emit rows); mean-reversion
contributes when present — an absent MR day usually means zero signals
passed its gates, which is real information, not missing data.

Prices are fetched with auto_adjust=True; returns are computed purely
inside the adjusted series, so no unit re-anchoring is needed.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_overlap.py [--refresh-prices]
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
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCANS_DIR = SKILL_DIR.parent  # repo layout: sister scans are siblings

# Reuse UOA's snapshot parser — single source of truth for its format.
sys.path.insert(0, str(DEFAULT_SCANS_DIR / "unusual-options-scan" / "scripts"))
from scan import list_history_dates, parse_snapshot  # noqa: E402

WINDOWS = (5, 10, 20)
TECH = ("momentum", "base-breakout", "mean-reversion")


# ---------------------------------------------------------------- loading

def load_csv_days(scan_dir: Path) -> dict[str, dict[str, int]]:
    """history.csv → {run_id: {ticker: rank}}."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    path = scan_dir / "state" / "history.csv"
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rid, t = (r.get("run_id") or "").strip(), \
                (r.get("ticker") or "").strip().upper()
            if not rid or not t or not r.get("rank"):
                continue
            try:
                out[rid][t] = int(float(r["rank"]))
            except ValueError:
                continue
    return out


def load_uoa_days() -> dict[date, dict[str, dict]]:
    """UOA snapshots → {date: {ticker: {cp, flags}}}."""
    out: dict[date, dict[str, dict]] = {}
    for d in list_history_dates():
        rows = parse_snapshot(d) or []
        per: dict[str, dict] = {}
        for r in rows:
            t = (r.get("ticker") or "").strip().upper()
            if not t:
                continue
            info = per.setdefault(t, {"cp": None, "flags": set()})
            info["cp"] = r.get("ticker_cp_ratio")
            info["flags"] |= set(r.get("flags") or "")
        if per:
            out[d] = per
    return out


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


# ---------------------------------------------------------------- outcomes

@dataclass
class TDay:
    session: pd.Timestamp
    sess_i: int
    ticker: str
    scans: set[str] = field(default_factory=set)
    ranks: dict[str, int] = field(default_factory=dict)
    uoa_cp: float | None = None
    uoa_flags: set[str] = field(default_factory=set)
    spell: int = 1            # consecutive sessions with overlap >= 2
    fwd: dict[int, float] = field(default_factory=dict)
    x: dict[int, float] = field(default_factory=dict)
    ok: bool = False

    @property
    def n_scans(self) -> int:
        return len(self.scans)

    @property
    def uoa_dir(self) -> str:
        """call / put / neutral — same thresholds as aggregate.py."""
        if self.uoa_cp is None or not np.isfinite(self.uoa_cp):
            return "call" if self.uoa_cp == float("inf") else "neutral"
        if self.uoa_cp >= 3.0:
            return "call"
        if self.uoa_cp <= 1 / 3:
            return "put"
        return "neutral"


def fwd_from(bars: pd.DataFrame, session: pd.Timestamp) -> dict[int, float]:
    idx = bars.index
    pos = idx.searchsorted(session, side="right") - 1
    if pos < 0 or idx[pos].normalize() != session:
        return {}
    entry = float(bars["Close"].iloc[pos])
    post = bars.iloc[pos + 1:]
    return {k: (float(post["Close"].iloc[k - 1]) / entry - 1) * 100
            for k in WINDOWS if len(post) >= k}


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=2):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:+.{nd}f}{suffix}" if suffix == "%" else f"{v:.{nd}f}{suffix}"


def _mean(vals):
    return float(np.mean(vals)) if vals else None


def _med(vals):
    return float(np.median(vals)) if vals else None


def table(title: str, groups: list[tuple[str, list[TDay]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Stratum | n | n20 | xT+5 | xT+10 | med10 | xT+20 | Beat10 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, tds in groups:
        tds = [td for td in tds if td.ok]
        vals = {k: [td.x[k] for td in tds if k in td.x] for k in WINDOWS}
        if not vals[5]:
            lines.append(f"| {name} | 0 | 0 | — | — | — | — | — |")
            continue
        beat = 100 * np.mean([v > 0 for v in vals[10]]) if vals[10] else None
        lines.append(
            f"| {name} | {len(vals[5])} | {len(vals[20])} | "
            f"{fmt(_mean(vals[5]), '%')} | {fmt(_mean(vals[10]), '%')} | "
            f"{fmt(_med(vals[10]), '%')} | {fmt(_mean(vals[20]), '%')} | "
            f"{fmt(beat, '', 0)}% |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scans-dir", type=Path, default=DEFAULT_SCANS_DIR)
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "xscan_backtest_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-05-01")
    args = ap.parse_args()

    csv_days = {name: load_csv_days(args.scans_dir / f"{name}-scan")
                for name in TECH}
    uoa_days = load_uoa_days()

    universe_file = (args.scans_dir / "unusual-options-scan" / "state"
                     / "universe.txt")
    universe = [t for t in universe_file.read_text().splitlines()
                if t.strip()] if universe_file.exists() else []

    all_tickers = set(universe) | {"SPY"}
    for days in csv_days.values():
        for members in days.values():
            all_tickers |= set(members)
    for members in uoa_days.values():
        all_tickers |= set(members)

    prices = fetch_prices(sorted(all_tickers), args.start, args.cache,
                          args.refresh_prices)
    if "SPY" not in prices:
        sys.exit("no SPY bars — cannot anchor sessions")
    spy_idx = prices["SPY"].index

    def to_session(d: date) -> pd.Timestamp | None:
        pos = spy_idx.searchsorted(pd.Timestamp(d), side="right") - 1
        return spy_idx[pos].normalize() if pos >= 0 else None

    # Per-scan membership keyed by session.
    by_session: dict[str, dict[pd.Timestamp, dict]] = {}
    for name in TECH:
        m: dict[pd.Timestamp, dict] = {}
        for rid, members in csv_days[name].items():
            s = to_session(datetime.strptime(rid, "%Y%m%d").date())
            if s is not None:
                m[s] = members
        by_session[name] = m
    uoa_by_session: dict[pd.Timestamp, dict] = {}
    for d, members in uoa_days.items():
        s = to_session(d)
        if s is not None:
            uoa_by_session[s] = members  # later date wins (same-close rerun)

    # Session roster: momentum, base-breakout and UOA must all be present
    # (they always emit rows); mean-reversion joins when present.
    sessions = sorted(set(by_session["momentum"])
                      & set(by_session["base-breakout"])
                      & set(uoa_by_session))
    n_mr = sum(1 for s in sessions if s in by_session["mean-reversion"])
    if len(sessions) < 10:
        sys.exit(f"only {len(sessions)} joinable sessions — not enough")

    # Build ticker-days.
    tdays: list[TDay] = []
    for i, s in enumerate(sessions):
        per_ticker: dict[str, TDay] = {}

        def touch(t: str) -> TDay:
            if t not in per_ticker:
                per_ticker[t] = TDay(session=s, sess_i=i, ticker=t)
            return per_ticker[t]

        for name in TECH:
            for t, rank in by_session[name].get(s, {}).items():
                td = touch(t)
                td.scans.add(name)
                td.ranks[name] = rank
        for t, info in uoa_by_session[s].items():
            td = touch(t)
            td.scans.add("unusual-options")
            td.uoa_cp = info["cp"]
            td.uoa_flags = info["flags"]
        tdays.extend(per_ticker.values())

    # Overlap spells (consecutive sessions a ticker held >= 2 scans).
    in2: dict[str, set[int]] = defaultdict(set)
    for td in tdays:
        if td.n_scans >= 2:
            in2[td.ticker].add(td.sess_i)
    for td in tdays:
        if td.n_scans >= 2:
            spell, j = 1, td.sess_i - 1
            while j in in2[td.ticker]:
                spell, j = spell + 1, j - 1
            td.spell = spell

    # Universe baseline per session, then excess per ticker-day.
    memo: dict[tuple[str, pd.Timestamp], dict[int, float]] = {}

    def fwd_for(t: str, s: pd.Timestamp) -> dict[int, float]:
        key = (t, s)
        if key not in memo:
            memo[key] = fwd_from(prices[t], s) if t in prices else {}
        return memo[key]

    base_pool = universe if universe else sorted(
        {td.ticker for td in tdays})
    umean: dict[pd.Timestamp, dict[int, float]] = {}
    for s in sessions:
        per_k: dict[int, list[float]] = {k: [] for k in WINDOWS}
        for t in base_pool:
            for k, v in fwd_for(t, s).items():
                per_k[k].append(v)
        umean[s] = {k: float(np.mean(v)) for k, v in per_k.items()
                    if len(v) >= 50}

    def has_session_bar(t: str, s: pd.Timestamp) -> bool:
        idx = prices[t].index
        pos = idx.searchsorted(s, side="right") - 1
        return pos >= 0 and idx[pos].normalize() == s

    n_no_prices = 0
    n_no_bar = 0
    n_open = 0
    for td in tdays:
        td.fwd = fwd_for(td.ticker, td.session)
        if not td.fwd:
            # Distinguish "no price data at all", "has prices but no bar on
            # that session" (halt / data gap), and "shortest window not yet
            # elapsed" (the last ~5 sessions) — very different things.
            if td.ticker not in prices:
                n_no_prices += 1
            elif has_session_bar(td.ticker, td.session):
                n_open += 1
            else:
                n_no_bar += 1
            continue
        td.x = {k: v - umean[td.session][k] for k, v in td.fwd.items()
                if k in umean[td.session]}
        td.ok = bool(td.x)

    ok = [td for td in tdays if td.ok]

    # ---------------------------------------------------------------- header
    print(f"# cross-scan overlap backtest — sessions "
          f"{sessions[0].date()} → {sessions[-1].date()} "
          f"({len(sessions)} joined; MR present on {n_mr})\n")
    print(f"**Ticker-days**: {len(tdays):,} ({len(ok):,} resolved, "
          f"{n_open} with T+5 window still open, {n_no_bar} without a bar "
          f"on the session, {n_no_prices} without price data) "
          f"· **unique tickers**: {len({td.ticker for td in tdays})}")
    print(f"**Universe baseline pool**: {len(base_pool)} tickers")
    counts = defaultdict(int)
    for td in ok:
        counts[td.n_scans] += 1
    print("**Overlap-count frequency**: "
          + " · ".join(f"{c} scans: {counts[c]:,}" for c in sorted(counts)))

    spy_fwd = {k: [] for k in WINDOWS}
    for s in sessions:
        for k, v in fwd_for("SPY", s).items():
            spy_fwd[k].append(v)
    uni_rows = {k: [umean[s][k] for s in sessions if k in umean[s]]
                for k in WINDOWS}
    print(f"\n**Tape context** (raw, mean across sessions): universe "
          f"T+5 {fmt(_mean(uni_rows[5]), '%')} / T+10 "
          f"{fmt(_mean(uni_rows[10]), '%')} / T+20 "
          f"{fmt(_mean(uni_rows[20]), '%')} · SPY "
          f"T+5 {fmt(_mean(spy_fwd[5]), '%')} / T+10 "
          f"{fmt(_mean(spy_fwd[10]), '%')} / T+20 "
          f"{fmt(_mean(spy_fwd[20]), '%')}")

    # ---------------------------------------------------------------- tables
    def has(td: TDay, *names: str) -> bool:
        return all(n in td.scans for n in names)

    print(table("By overlap count (the core premise)", [
        ("1 scan only", [td for td in ok if td.n_scans == 1]),
        ("2 scans", [td for td in ok if td.n_scans == 2]),
        ("3 scans", [td for td in ok if td.n_scans == 3]),
        ("4 scans", [td for td in ok if td.n_scans == 4]),
    ]))

    print(table("Per-scan pools (context — each scan's full daily set)", [
        ("momentum (all)", [td for td in ok if has(td, "momentum")]),
        ("base-breakout (all)", [td for td in ok if has(td, "base-breakout")]),
        ("mean-reversion (all)",
         [td for td in ok if has(td, "mean-reversion")]),
        ("unusual-options (all)",
         [td for td in ok if has(td, "unusual-options")]),
    ]))

    # The fair confirmation test: within each technical scan's pool, does
    # extra membership help? (UOA flags ~15-20% of the universe daily, so
    # UOA-overlap must beat the scan's OWN baseline to mean anything.)
    for name in TECH:
        others = [n for n in TECH if n != name]
        pool = [td for td in ok if has(td, name)]
        print(table(f"Within {name}: does confirmation add anything?", [
            (f"{name} alone", [td for td in pool if td.n_scans == 1]),
            (f"+ UOA only",
             [td for td in pool if has(td, "unusual-options")
              and not any(has(td, o) for o in others)]),
            (f"+ other technical (no UOA)",
             [td for td in pool if any(has(td, o) for o in others)
              and not has(td, "unusual-options")]),
            (f"+ technical + UOA",
             [td for td in pool if any(has(td, o) for o in others)
              and has(td, "unusual-options")]),
        ]))

    print(table("Composite-read patterns (cells overlap; inclusive defs "
                "matching aggregate.py labels)", [
        ("mom+mr — 'pullback in a leader'",
         [td for td in ok if has(td, "momentum", "mean-reversion")]),
        ("mom+base — 'leader still consolidating'",
         [td for td in ok if has(td, "momentum", "base-breakout")]),
        ("base+mr — 'oversold base candidate'",
         [td for td in ok if has(td, "base-breakout", "mean-reversion")]),
        ("mom+base+mr — 'rare'",
         [td for td in ok
          if has(td, "momentum", "base-breakout", "mean-reversion")]),
        ("base+UOA call-heavy — '⭐ best pattern'",
         [td for td in ok if has(td, "base-breakout", "unusual-options")
          and td.uoa_dir == "call"]),
        ("base+UOA neutral",
         [td for td in ok if has(td, "base-breakout", "unusual-options")
          and td.uoa_dir == "neutral"]),
        ("base+UOA put-heavy — 'caution'",
         [td for td in ok if has(td, "base-breakout", "unusual-options")
          and td.uoa_dir == "put"]),
        ("mom+UOA call-heavy, no mr — '⚠️ crowding tell'",
         [td for td in ok if has(td, "momentum", "unusual-options")
          and td.uoa_dir == "call"
          and not has(td, "mean-reversion")]),
        ("mom+UOA put-heavy — '⚠️ bearish positioning'",
         [td for td in ok if has(td, "momentum", "unusual-options")
          and td.uoa_dir == "put"]),
    ]))

    # Rank-awareness inside overlaps: does the contributing technical rank
    # separate outcomes? (the "no per-scan weighting" limitation)
    def min_tech_rank(td: TDay) -> int | None:
        ranks = [td.ranks[n] for n in TECH if n in td.ranks]
        return min(ranks) if ranks else None

    ov2 = [td for td in ok if td.n_scans >= 2 and min_tech_rank(td)]
    print(table("Overlap (≥2) by best technical rank", [
        ("top-10 in a tech scan",
         [td for td in ov2 if min_tech_rank(td) <= 10]),
        ("rank 11-30", [td for td in ov2 if 10 < min_tech_rank(td) <= 30]),
        ("rank 31+", [td for td in ov2 if min_tech_rank(td) > 30]),
    ]))

    print(table("Overlap (≥2) by spell (consecutive sessions overlapped)", [
        ("1st session (fresh)", [td for td in ok
                                 if td.n_scans >= 2 and td.spell == 1]),
        ("2nd-3rd", [td for td in ok
                     if td.n_scans >= 2 and 2 <= td.spell <= 3]),
        ("4th+", [td for td in ok if td.n_scans >= 2 and td.spell >= 4]),
    ]))

    mid = len(sessions) // 2
    print(table(f"Stability — first vs second half "
                f"(split at {sessions[mid].date()})", [
        ("H1 ≥2 scans", [td for td in ok
                         if td.n_scans >= 2 and td.sess_i < mid]),
        ("H1 1 scan", [td for td in ok
                       if td.n_scans == 1 and td.sess_i < mid]),
        ("H2 ≥2 scans", [td for td in ok
                         if td.n_scans >= 2 and td.sess_i >= mid]),
        ("H2 1 scan", [td for td in ok
                       if td.n_scans == 1 and td.sess_i >= mid]),
    ]))

    print("""
_Columns: n = ticker-days with T+5 excess resolved; n20 = with T+20
resolved; xT+k = mean excess forward return vs the equal-weight universe
mean on the same session; med10 = median of the xT+10 values; Beat10 =
share of xT+10 values > 0._

**Caveats** — read before quoting numbers:
- One ~2-month window on a mostly RISK-ON tape.
- Ticker-days cluster (same name persists across sessions; all names share
  each session's tape). Excess removes the day effect, not the episode
  effect. The spell table's "1st session" row is the per-episode view.
- Membership sizes are wildly asymmetric (momentum ~30/day, UOA ~180/day),
  so overlap WITH UOA is cheap by construction — judge it via the
  within-scan tables, never by comparing against the whole-market baseline.
- Mean-reversion days with zero emitted signals are indistinguishable from
  days it didn't run; both count as "MR absent".
- The universe baseline is the CURRENT UOA universe list (survivorship
  optimism) and contains the scanned names themselves.
- Scan pools skew to high-beta/high-vol names in different degrees; part of
  any gap vs universe is style, not signal. Within-scan comparisons are the
  cleanest reads.
- T+20 windows are censored for July sessions (n20 << n) — later sessions
  drop out, so T+20 rows skew to the June tape.""")


if __name__ == "__main__":
    main()
