#!/usr/bin/env python3
"""Outcome backtest for unusual-options-scan history.

Replays state/history/*.md — every flagged contract of every daily snapshot —
and measures what the flags actually predicted in the UNDERLYING stock.
Unlike the sister backtests (mean-reversion, base-breakout) there is no trade
convention to replay: this scan is a lead-generation funnel, so the honest
outcome measure is information content, not fills. Three lenses:

  - forward returns of the underlying at T+1 / T+5 / T+10 sessions from the
    snapshot-day close (or the next open with --entry next-open);
  - EXCESS forward returns vs the equal-weight universe mean on the same
    session — removes market drift and day-clustering, so strata compare
    signal against signal, not signal against a rising tape;
  - DIRECTION-SIGNED excess — call-flagged flow is a bet up, put-flagged
    flow a bet down, so signed = +excess for calls, −excess for puts. A
    positive signed mean says the flow pointed the right way.

Everything SKILL.md claims but never measured gets a stratum: cross-day OI
confirmation tiers (✅/≈/❌), flagged-contract cluster size, the 🎯/🔥
far-OTM lottery pattern (including the strike-touch-before-expiry rate),
📊 skew, 💰 notional/ADV, repeat-offender streaks, chronic vs episodic
tickers, and day-of-spell. Caveats worth reading sit at the bottom of the
report — the OI-confirmation replay in particular can only join contracts
that were RE-flagged the next day (the live scan joins against full chains,
which snapshots don't store).

Prices are fetched with auto_adjust=True. Strike levels are nominal, so for
the strike-touch analysis each ticker-day is re-anchored: the snapshot's
dist_pct implies the scan-day spot, and strikes are rescaled by (series
close / implied spot); ticker-days whose implied spot disagrees with the
series by >5% are dropped and reported rather than resolved on mismatched
units. Return windows need no anchoring — they are computed purely inside
the adjusted series.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--entry next-open] [--refresh-prices]
"""

from __future__ import annotations

import argparse
import pickle
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Reuse the scan's own snapshot parser — single source of truth for the
# markdown table format and the contract join key.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan import contract_key, list_history_dates, parse_snapshot  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = SKILL_DIR / "state" / "universe.txt"

WINDOWS = (1, 5, 10)
FAR_OTM_PCT = 10.0   # scan default --far-otm-pct; snapshots were all run with it
DIR_DOMINANT = 0.70  # flagged-notional share for call-/put-dominant ticker-days


# ---------------------------------------------------------------- history

@dataclass
class TDay:
    """One (snapshot, ticker) — the ticker-day unit of analysis."""
    snap_i: int
    snap_date: date
    ticker: str
    contracts: list[dict]
    n_flagged: int = 0
    call_share: float | None = None  # flagged notional share on the call side
    direction: str = "mixed"         # call / put / mixed (70/30 by notional)
    cp_ratio: float | None = None
    nadv: float | None = None
    spell: int = 1                   # consecutive snapshots listed, this run
    chron: float = 0.0               # share of ALL snapshots this ticker is in
    session: pd.Timestamp | None = None
    ratio: float | None = None       # series units / nominal units
    pre5: float | None = None        # 5-session return INTO the signal
    fwd: dict[int, float] = field(default_factory=dict)
    x: dict[int, float] = field(default_factory=dict)   # excess vs universe
    status: str = "pending"          # ok / no_bars / unit_mismatch / no_prices

    def signed(self, k: int) -> float | None:
        if self.direction == "mixed" or k not in self.x:
            return None
        return self.x[k] if self.direction == "call" else -self.x[k]


def load_history() -> tuple[list[date], dict[date, list[dict]]]:
    dates = list_history_dates()
    snaps: dict[date, list[dict]] = {}
    for d in dates:
        rows = parse_snapshot(d) or []
        # Defensive: drop rows missing the fields every analysis needs.
        rows = [r for r in rows
                if r.get("ticker") and r.get("type") in ("call", "put")
                and r.get("strike") and r.get("vol") is not None]
        if rows:
            snaps[d] = rows
    return [d for d in dates if d in snaps], snaps


def implied_spot(r: dict) -> float | None:
    """Back out the scan-day spot from strike + dist_pct (both stored)."""
    dist = r.get("dist_pct")
    strike = r.get("strike")
    if dist is None or strike is None:
        return None
    if r["type"] == "call":
        denom = 1 + dist / 100.0
        return strike / denom if denom > 0 else None
    return strike * (1 + dist / 100.0)


def build_tdays(snap_dates: list[date],
                snaps: dict[date, list[dict]]) -> list[TDay]:
    tdays: list[TDay] = []
    for i, d in enumerate(snap_dates):
        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for r in snaps[d]:
            by_ticker[r["ticker"]].append(r)
        for t, rows in by_ticker.items():
            td = TDay(snap_i=i, snap_date=d, ticker=t, contracts=rows)
            td.n_flagged = len(rows)
            call_n = sum(r.get("notional") or 0 for r in rows
                         if r["type"] == "call")
            tot_n = sum(r.get("notional") or 0 for r in rows)
            td.call_share = call_n / tot_n if tot_n > 0 else None
            if td.call_share is not None:
                if td.call_share >= DIR_DOMINANT:
                    td.direction = "call"
                elif td.call_share <= 1 - DIR_DOMINANT:
                    td.direction = "put"
            td.cp_ratio = rows[0].get("ticker_cp_ratio")
            td.nadv = rows[0].get("ticker_notional_adv_mult")
            tdays.append(td)

    # Spell (consecutive snapshots listed) + chronicity (share of snapshots).
    appearances: dict[str, list[TDay]] = defaultdict(list)
    for td in tdays:
        appearances[td.ticker].append(td)
    n_snaps = len(snap_dates)
    for t, tds in appearances.items():
        tds.sort(key=lambda td: td.snap_i)
        prev_i = None
        spell = 1
        for td in tds:
            spell = spell + 1 if (prev_i is not None
                                  and td.snap_i - prev_i == 1) else 1
            td.spell = spell
            td.chron = len(tds) / n_snaps
            prev_i = td.snap_i
    return tdays


# ---------------------------------------------------------------- prices

NO_DATA_KEY = "_NO_DATA"  # cache slot: tickers Yahoo confirmed empty


def fetch_prices(tickers: list[str], start: str, cache: Path,
                 refresh: bool) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            data = pickle.load(f)
        # Freshness guard: a quarterly re-run against an old cache would
        # silently censor every new signal's post-window. SPY is always in
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


# ---------------------------------------------------------------- outcomes

def outcome_at(bars: pd.DataFrame, session: pd.Timestamp,
               entry_mode: str) -> dict | None:
    """Forward returns from `session`'s close (or the next open). Returns
    {"close0", "pos", "fwd": {k: pct}} — fwd holds only fully-elapsed
    windows. None if the ticker has no bar on that session."""
    idx = bars.index
    pos = idx.searchsorted(session, side="right") - 1
    if pos < 0 or idx[pos].normalize() != session:
        return None
    closes = bars["Close"]
    post = bars.iloc[pos + 1:]
    out = {"close0": float(closes.iloc[pos]), "pos": pos, "fwd": {},
           "pre5": (float(closes.iloc[pos]) / float(closes.iloc[pos - 5]) - 1)
           * 100 if pos >= 5 else None}
    if entry_mode == "next-open":
        if not len(post):
            return out
        entry = float(post["Open"].iloc[0])
        if not np.isfinite(entry) or entry <= 0:
            return out
    else:
        entry = out["close0"]
    for k in WINDOWS:
        if len(post) >= k:
            out["fwd"][k] = (float(post["Close"].iloc[k - 1]) / entry - 1) * 100
    return out


def resolve_sessions(snap_dates: list[date],
                     spy: pd.DataFrame) -> dict[date, pd.Timestamp]:
    """Snapshot date → the trading session whose close the scan read (the
    snapshot may carry a weekend date if run Saturday Beijing time)."""
    sessions: dict[date, pd.Timestamp] = {}
    idx = spy.index
    used: dict[pd.Timestamp, date] = {}
    for d in snap_dates:
        pos = idx.searchsorted(pd.Timestamp(d), side="right") - 1
        if pos < 0:
            continue
        s = idx[pos].normalize()
        if s in used:
            # Both snapshots read the same close; keep only the later one so
            # its ticker-days aren't double-counted.
            print(f"warning: snapshots {used[s]} and {d} map to the same "
                  f"session {s.date()}; keeping {d}", file=sys.stderr)
            sessions.pop(used[s], None)
        used[s] = d
        sessions[d] = s
    return sessions


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=2):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:+.{nd}f}{suffix}" if suffix == "%" else f"{v:.{nd}f}{suffix}"


def _mean(vals: list[float]) -> float | None:
    return float(np.mean(vals)) if vals else None


def _med(vals: list[float]) -> float | None:
    return float(np.median(vals)) if vals else None


def excess_table(title: str, groups: list[tuple[str, list[TDay]]],
                 signed: bool = False) -> str:
    """Ticker-day strata. Unsigned: mean excess per window + raw |T+5| +
    share beating the universe. Signed: sign = +1 call-dominant, −1
    put-dominant (mixed rows have no sign and are dropped)."""
    tag = "s×" if signed else "x"
    lines = [f"\n### {title}\n",
             f"| Stratum | n | n10 | {tag}T+1 | {tag}T+5 | med | {tag}T+10 | "
             f"\\|T+5\\| | Beat5 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, tds in groups:
        tds = [td for td in tds if td.status == "ok"]
        if signed:
            vals = {k: [td.signed(k) for td in tds
                        if td.signed(k) is not None] for k in WINDOWS}
        else:
            vals = {k: [td.x[k] for td in tds if k in td.x] for k in WINDOWS}
        if not vals[1]:
            # Print empty strata rather than dropping them — a reader must be
            # able to tell "n=0" from "not computed".
            lines.append(f"| {name} | 0 | 0 | — | — | — | — | — | — |")
            continue
        raw5 = [abs(td.fwd[5]) for td in tds if 5 in td.fwd]
        beat = 100 * np.mean([v > 0 for v in vals[5]]) if vals[5] else None
        lines.append(
            f"| {name} | {len(vals[1])} | {len(vals[10])} | "
            f"{fmt(_mean(vals[1]), '%')} | {fmt(_mean(vals[5]), '%')} | "
            f"{fmt(_med(vals[5]), '%')} | {fmt(_mean(vals[10]), '%')} | "
            f"{fmt(_mean(raw5))} | {fmt(beat, '', 0)}% |")
    return "\n".join(lines)


def contract_table(title: str,
                   groups: list[tuple[str, list[tuple[int, TDay]]]]) -> str:
    """Contract-level strata, direction-signed by each contract's own side.
    Items are (sign, tday) pairs."""
    lines = [f"\n### {title}\n",
             "| Stratum | n | n10 | s×T+1 | s×T+5 | med | s×T+10 | Beat5 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, items in groups:
        items = [(sg, td) for sg, td in items if td.status == "ok"]
        vals = {k: [sg * td.x[k] for sg, td in items if k in td.x]
                for k in WINDOWS}
        if not vals[1]:
            lines.append(f"| {name} | 0 | 0 | — | — | — | — | — |")
            continue
        beat = 100 * np.mean([v > 0 for v in vals[5]]) if vals[5] else None
        lines.append(
            f"| {name} | {len(vals[1])} | {len(vals[10])} | "
            f"{fmt(_mean(vals[1]), '%')} | {fmt(_mean(vals[5]), '%')} | "
            f"{fmt(_med(vals[5]), '%')} | {fmt(_mean(vals[10]), '%')} | "
            f"{fmt(beat, '', 0)}% |")
    return "\n".join(lines)


def bucket_td(tds: list[TDay], key, bounds) -> list[tuple[str, list[TDay]]]:
    return [(name, [td for td in tds
                    if key(td) is not None and lo <= key(td) < hi])
            for name, lo, hi in bounds]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entry", choices=["close", "next-open"], default="close",
                    help="close = information content from the snapshot-day "
                         "close; next-open = realistic execution at the next "
                         "session's open (the scan output exists only after "
                         "the close)")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "uoa_backtest_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-05-01",
                    help="price download start date")
    args = ap.parse_args()

    snap_dates, snaps = load_history()
    if len(snap_dates) < 10:
        sys.exit(f"only {len(snap_dates)} snapshots — not enough history")
    n_rows = sum(len(v) for v in snaps.values())
    tdays = build_tdays(snap_dates, snaps)
    flagged_tickers = sorted({td.ticker for td in tdays})

    universe = [t for t in UNIVERSE_FILE.read_text().splitlines() if t.strip()] \
        if UNIVERSE_FILE.exists() else []
    all_tickers = sorted(set(flagged_tickers) | set(universe) | {"SPY"})
    prices = fetch_prices(all_tickers, args.start, args.cache,
                          args.refresh_prices)
    if "SPY" not in prices:
        sys.exit("no SPY bars — cannot anchor sessions")
    sessions = resolve_sessions(snap_dates, prices["SPY"])

    # Per-(ticker, session) outcome memo — shared by ticker-days and the
    # universe baseline so both use the exact same convention.
    memo: dict[tuple[str, pd.Timestamp], dict | None] = {}

    def out_for(t: str, s: pd.Timestamp) -> dict | None:
        k = (t, s)
        if k not in memo:
            memo[k] = outcome_at(prices[t], s, args.entry) \
                if t in prices else None
        return memo[k]

    # Universe baseline: equal-weight mean forward return per session, plus
    # pooled |T+5| and pre-signal-5d stats for the calibration table.
    umean: dict[pd.Timestamp, dict[int, float]] = {}
    uni_abs5_pool: list[float] = []
    uni_pre5_pool: list[float] = []
    base_pool = universe if universe else flagged_tickers
    for d in snap_dates:
        s = sessions.get(d)
        if s is None or s in umean:
            continue
        per_k: dict[int, list[float]] = {k: [] for k in WINDOWS}
        for t in base_pool:
            o = out_for(t, s)
            if o:
                for k, v in o["fwd"].items():
                    per_k[k].append(v)
                if 5 in o["fwd"]:
                    uni_abs5_pool.append(abs(o["fwd"][5]))
                if o.get("pre5") is not None:
                    uni_pre5_pool.append(o["pre5"])
        umean[s] = {k: float(np.mean(v)) for k, v in per_k.items()
                    if len(v) >= 50}

    # Resolve every ticker-day.
    n_mismatch = 0
    for td in tdays:
        s = sessions.get(td.snap_date)
        if s is None or td.ticker not in prices:
            td.status = "no_prices"
            continue
        td.session = s
        o = out_for(td.ticker, s)
        if o is None:
            td.status = "no_bars"
            continue
        spots = [v for v in (implied_spot(r) for r in td.contracts) if v]
        if spots:
            ratio = o["close0"] / float(np.median(spots))
            if abs(ratio - 1) > 0.05:
                td.status = "unit_mismatch"
                n_mismatch += 1
                continue
            td.ratio = ratio
        td.fwd = dict(o["fwd"])
        td.pre5 = o.get("pre5")
        td.x = {k: v - umean[s][k] for k, v in td.fwd.items()
                if k in umean.get(s, {})}
        td.status = "ok"

    ok = [td for td in tdays if td.status == "ok"]
    n_no_prices = sum(td.status == "no_prices" for td in tdays)
    n_no_bars = sum(td.status == "no_bars" for td in tdays)

    # Contract-level views. Sign: call = +1, put = −1.
    cons: list[tuple[dict, int, TDay]] = []
    for td in ok:
        for r in td.contracts:
            cons.append((r, 1 if r["type"] == "call" else -1, td))

    # Flag frequency (contract level).
    n_c = len(cons)
    freq = {f: sum(f in (r.get("flags") or "") for r, _, _ in cons)
            for f in ("🎯", "🔥", "📊", "💰")}

    # ---------------------------------------------------------------- header
    print(f"# unusual-options-scan outcome backtest — history "
          f"{snap_dates[0]} → {snap_dates[-1]} — entry mode: **{args.entry}**\n")
    print(f"**Snapshots**: {len(snap_dates)} · **contract rows**: {n_rows:,} "
          f"· **ticker-days**: {len(tdays):,} ({len(ok):,} resolved) "
          f"· **unique tickers**: {len(flagged_tickers)}")
    print(f"**Dropped**: {n_no_prices} ticker-days without price data, "
          f"{n_no_bars} without a bar on the session, "
          f"{n_mismatch} unit-mismatch (>5% vs implied spot)")
    print(f"**Universe baseline pool**: {len(base_pool)} tickers "
          f"({sum(1 for t in base_pool if t in prices)} with prices)")
    print(f"**Flag frequency** (contract level, n={n_c:,}): "
          + " · ".join(f"{f} {100 * n / n_c:.0f}%" for f, n in freq.items()))

    # ------------------------------------------------------- calibration
    raw = {k: [td.fwd[k] for td in ok if k in td.fwd] for k in WINDOWS}
    uni = {k: [] for k in WINDOWS}
    spy = {k: [] for k in WINDOWS}
    for d in snap_dates:
        s = sessions.get(d)
        if s is None:
            continue
        for k in WINDOWS:
            if k in umean.get(s, {}):
                uni[k].append(umean[s][k])
        o = out_for("SPY", s)
        if o:
            for k, v in o["fwd"].items():
                spy[k].append(v)
    print("\n### Calibration — raw forward returns (not excess)\n")
    print("| Row | n | pre5 | T+1 | T+5 | T+10 | \\|T+5\\| | Pos5 |")
    print("|---|---|---|---|---|---|---|---|")
    abs5 = [abs(v) for v in raw[5]]
    pre5s = [td.pre5 for td in ok if td.pre5 is not None]
    pos5 = 100 * np.mean([v > 0 for v in raw[5]]) if raw[5] else None
    print(f"| All flagged ticker-days | {len(raw[1])} | "
          f"{fmt(_mean(pre5s), '%')} | "
          f"{fmt(_mean(raw[1]), '%')} | {fmt(_mean(raw[5]), '%')} | "
          f"{fmt(_mean(raw[10]), '%')} | {fmt(_mean(abs5))} | "
          f"{fmt(pos5, '', 0)}% |")
    print(f"| Universe (pooled ticker-days) | {len(uni[1])}d | "
          f"{fmt(_mean(uni_pre5_pool), '%')} | "
          f"{fmt(_mean(uni[1]), '%')} | {fmt(_mean(uni[5]), '%')} | "
          f"{fmt(_mean(uni[10]), '%')} | {fmt(_mean(uni_abs5_pool))} | — |")
    print(f"| SPY (same sessions) | {len(spy[1])}d | — | "
          f"{fmt(_mean(spy[1]), '%')} | {fmt(_mean(spy[5]), '%')} | "
          f"{fmt(_mean(spy[10]), '%')} | — | — |")

    # ------------------------------------------------------- pre-signal move
    # Does the underperformance come from the flow itself, or is the scan a
    # de-facto recent-mover detector whose names then mean-revert? If flags
    # on FLAT names still underperform, the flow itself carries the signal.
    print(excess_table(
        "By pre-signal 5-session move of the underlying (what the flag "
        "chased)", bucket_td(
            ok, lambda td: td.pre5,
            [("≤−5% (post-dump)", -1e9, -5), ("−5..−2%", -5, -2),
             ("−2..+2% flat", -2, 2), ("+2..+10%", 2, 10),
             (">+10% (post-rip)", 10, 1e9)])))

    # ------------------------------------------------------- direction
    print(excess_table(
        "By flagged-notional direction (unsigned excess — informative flow "
        "should show a call→put gradient)", [
            ("≥90% call", [td for td in ok if td.call_share is not None
                           and td.call_share >= 0.9]),
            ("70–90% call", [td for td in ok if td.call_share is not None
                             and 0.7 <= td.call_share < 0.9]),
            ("mixed 30–70%", [td for td in ok if td.call_share is not None
                              and 0.3 < td.call_share < 0.7]),
            ("70–90% put", [td for td in ok if td.call_share is not None
                            and 0.1 < td.call_share <= 0.3]),
            ("≥90% put", [td for td in ok if td.call_share is not None
                          and td.call_share <= 0.1]),
        ]))

    dom = [td for td in ok if td.direction != "mixed"]
    print(excess_table(
        "Direction-signed (call-dom = +excess, put-dom = −excess; "
        "mixed excluded)", [
            ("All direction-dominant", dom),
            ("Call-dominant", [td for td in dom if td.direction == "call"]),
            ("Put-dominant", [td for td in dom if td.direction == "put"]),
        ], signed=True))

    # ------------------------------------------------------- chronicity
    print(excess_table(
        "By chronicity (share of all snapshots the ticker appears in — "
        "full-sample attribute, see caveats)", bucket_td(
            ok, lambda td: td.chron,
            [("<10% episodic", 0, 0.10), ("10–33%", 0.10, 1 / 3),
             ("33–67%", 1 / 3, 2 / 3), (">67% chronic", 2 / 3, 1.01)])))

    print(excess_table(
        "Direction-signed × chronicity (dominant ticker-days only)", [
            ("episodic ≤33%", [td for td in dom if td.chron <= 1 / 3]),
            ("33–67%", [td for td in dom if 1 / 3 < td.chron <= 2 / 3]),
            (">67% chronic", [td for td in dom if td.chron > 2 / 3]),
        ], signed=True))

    # ------------------------------------------------------- spell
    print(excess_table(
        "Direction-signed × day-of-spell (consecutive snapshots listed)", [
            ("1st day", [td for td in dom if td.spell == 1]),
            ("2nd day", [td for td in dom if td.spell == 2]),
            ("3rd+ day", [td for td in dom if td.spell >= 3]),
        ], signed=True))

    # ------------------------------------------------------- cluster size
    print(excess_table(
        "By flagged-contract cluster size (SKILL.md: cluster ≫ single)",
        bucket_td(ok, lambda td: td.n_flagged,
                  [("1 contract", 1, 2), ("2–4", 2, 5), ("5–9", 5, 10),
                   ("10+", 10, 10_000)])))

    print(excess_table(
        "Direction-signed × cluster size (dominant only)", [
            ("1 contract", [td for td in dom if td.n_flagged == 1]),
            ("2–4", [td for td in dom if 2 <= td.n_flagged <= 4]),
            ("5+", [td for td in dom if td.n_flagged >= 5]),
        ], signed=True))

    # ------------------------------------------------------- notional/ADV
    print(excess_table(
        "By ticker notional/ADV (💰 fires at ≥0.5; SKILL.md calls ≥1.5 "
        "'the strongest single-day signal')", bucket_td(
            ok, lambda td: td.nadv,
            [("<0.1", 0, 0.1), ("0.1–0.5", 0.1, 0.5),
             ("0.5–1.5 (💰)", 0.5, 1.5), ("≥1.5 (💰+)", 1.5, 1e9)])))

    print(excess_table(
        "Direction-signed × notional/ADV (dominant only)", [
            ("<0.1", [td for td in dom if td.nadv is not None
                      and td.nadv < 0.1]),
            ("0.1–0.5", [td for td in dom if td.nadv is not None
                         and 0.1 <= td.nadv < 0.5]),
            ("≥0.5 (💰)", [td for td in dom if td.nadv is not None
                           and td.nadv >= 0.5]),
        ], signed=True))

    # ------------------------------------------------------- CP skew
    def cp_of(td: TDay) -> float | None:
        c = td.cp_ratio
        return None if c is None or not np.isfinite(c) else c

    print(excess_table(
        "By ticker C/P ratio (📊 fires outside 0.33–3; unsigned — extreme "
        "calls should print positive, extreme puts negative)", [
            ("C/P ≥ 3 (📊 call-skew)",
             [td for td in ok if (td.cp_ratio or 0) >= 3]),
            ("1 ≤ C/P < 3", [td for td in ok if cp_of(td) is not None
                             and 1 <= cp_of(td) < 3]),
            ("0.33 < C/P < 1", [td for td in ok if cp_of(td) is not None
                                and 1 / 3 < cp_of(td) < 1]),
            ("C/P ≤ 0.33 (📊 put-skew)",
             [td for td in ok if cp_of(td) is not None
              and cp_of(td) <= 1 / 3]),
        ]))

    # ------------------------------------------------------- flags (contract)
    def has(r: dict, f: str) -> bool:
        return f in (r.get("flags") or "")

    print(contract_table(
        "By contract flag (signed by the contract's own side; 🔥 ⊂ 🎯 by "
        "construction, so 🎯 is split at DTE 10)", [
            ("⚡ only (no 🎯/🔥/📊/💰)",
             [(sg, td) for r, sg, td in cons
              if not any(has(r, f) for f in "🎯🔥📊💰")]),
            ("🎯 far-OTM, DTE 11–30",
             [(sg, td) for r, sg, td in cons if has(r, "🎯")
              and not has(r, "🔥")]),
            ("🔥 far-OTM, DTE ≤ 10",
             [(sg, td) for r, sg, td in cons if has(r, "🔥")]),
            ("📊 on ticker", [(sg, td) for r, sg, td in cons if has(r, "📊")]),
            ("💰 on ticker", [(sg, td) for r, sg, td in cons if has(r, "💰")]),
            ("🔥 + 💰 (catalyst-imminent claim)",
             [(sg, td) for r, sg, td in cons
              if has(r, "🔥") and has(r, "💰")]),
        ]))

    # ------------------------------------------------------- streaks
    keys_by_snap: list[set] = []
    row_by_key: list[dict] = []
    for d in snap_dates:
        m = {}
        for r in snaps[d]:
            try:
                m[contract_key(r)] = r
            except (KeyError, TypeError, ValueError):
                continue
        row_by_key.append(m)
        keys_by_snap.append(set(m))
    streak_at: list[dict] = []
    prev: dict = {}
    for i in range(len(snap_dates)):
        cur = {k: prev.get(k, -1) + 1 if k in prev else 0
               for k in keys_by_snap[i]}
        streak_at.append(cur)
        prev = cur

    td_index = {(td.snap_i, td.ticker): td for td in ok}

    def streak_items(lo: int, hi: int) -> list[tuple[int, TDay]]:
        out = []
        for r, sg, td in cons:
            try:
                s = streak_at[td.snap_i].get(contract_key(r))
            except (KeyError, TypeError, ValueError):
                continue
            if s is not None and lo <= s < hi:
                out.append((sg, td))
        return out

    print(contract_table(
        "By contract streak (prior consecutive snapshots flagged; 2+ = the "
        "'repeat offenders' section)", [
            ("0 (first appearance)", streak_items(0, 1)),
            ("1 (2nd day)", streak_items(1, 2)),
            ("2+ (repeat offender)", streak_items(2, 10_000)),
        ]))

    # ------------------------------------------------------- OI confirmation
    tiers: dict[str, list[tuple[int, TDay]]] = {"✅": [], "≈": [], "❌": []}
    n_joined = 0
    for i in range(len(snap_dates) - 1):
        common = keys_by_snap[i] & keys_by_snap[i + 1]
        for k in common:
            prior_oi = row_by_key[i][k].get("oi")
            today_oi = row_by_key[i + 1][k].get("oi")
            if not prior_oi or today_oi is None:
                continue
            n_joined += 1
            delta = (today_oi - prior_oi) / prior_oi * 100
            status = "✅" if delta >= 20 else ("❌" if delta < 5 else "≈")
            r = row_by_key[i + 1][k]
            td = td_index.get((i + 1, r["ticker"]))
            if td is not None:
                tiers[status].append((1 if r["type"] == "call" else -1, td))
    print(contract_table(
        f"Cross-day OI confirmation (n={n_joined} re-flagged joins — biased "
        "subset, see caveats; outcome from the confirmation day)", [
            ("✅ OI ≥ +20% (built & held)", tiers["✅"]),
            ("≈ OI +5–20% (partial)", tiers["≈"]),
            ("❌ OI < +5% (closed out)", tiers["❌"]),
        ]))

    # ------------------------------------------------------- strike touch
    print("\n### 🎯 lottery contracts — did the underlying ever touch the "
          "strike before expiry?\n")
    print("| Stratum | resolved | touched | Touch% | med days | censored |")
    print("|---|---|---|---|---|---|")
    touch_rows = []
    for r, sg, td in cons:
        dist, dte = r.get("dist_pct"), r.get("dte")
        if dist is None or dte is None or dist < FAR_OTM_PCT or dte > 30:
            continue
        if td.ratio is None or td.ticker not in prices:
            continue
        bars = prices[td.ticker]
        pos = bars.index.searchsorted(td.session, side="right") - 1
        exp_ts = pd.Timestamp(r["expiry"])
        post = bars.iloc[pos + 1:]
        post = post[post.index.normalize() <= exp_ts]
        strike_adj = r["strike"] * td.ratio
        if r["type"] == "call":
            hits = post.index[post["High"] >= strike_adj]
        else:
            hits = post.index[post["Low"] <= strike_adj]
        touched = len(hits) > 0
        days = int(post.index.get_loc(hits[0])) + 1 if touched else None
        resolved = touched or (len(bars.index) and
                               bars.index[-1].normalize() >= exp_ts)
        touch_rows.append((r, touched, days, resolved))

    def touch_line(name: str, rows: list) -> None:
        res = [x for x in rows if x[3]]
        cen = len(rows) - len(res)
        if not res:
            print(f"| {name} | 0 | 0 | — | — | {cen} |")
            return
        hit = [x for x in res if x[1]]
        med_d = _med([x[2] for x in hit]) if hit else None
        print(f"| {name} | {len(res)} | {len(hit)} | "
              f"{100 * len(hit) / len(res):.0f}% | {fmt(med_d, '', 0)} | "
              f"{cen} |")

    calls_t = [x for x in touch_rows if x[0]["type"] == "call"]
    puts_t = [x for x in touch_rows if x[0]["type"] == "put"]
    touch_line("Calls, all 🎯", calls_t)
    touch_line("· 10–15% OTM", [x for x in calls_t
                                if 10 <= x[0]["dist_pct"] < 15])
    touch_line("· 15–25% OTM", [x for x in calls_t
                                if 15 <= x[0]["dist_pct"] < 25])
    touch_line("· ≥25% OTM", [x for x in calls_t if x[0]["dist_pct"] >= 25])
    touch_line("· DTE ≤ 10 (🔥)", [x for x in calls_t if x[0]["dte"] <= 10])
    touch_line("· DTE 11–30", [x for x in calls_t if x[0]["dte"] > 10])
    touch_line("Puts, all 🎯", puts_t)
    touch_line("· 10–15% OTM", [x for x in puts_t
                                if 10 <= x[0]["dist_pct"] < 15])
    touch_line("· ≥15% OTM", [x for x in puts_t if x[0]["dist_pct"] >= 15])

    # ------------------------------------------------------- stability
    mid = len(snap_dates) // 2
    print(excess_table(
        f"Stability — direction-signed, first vs second half "
        f"(split at {snap_dates[mid]})", [
            ("H1 call-dom", [td for td in dom if td.snap_i < mid
                             and td.direction == "call"]),
            ("H1 put-dom", [td for td in dom if td.snap_i < mid
                            and td.direction == "put"]),
            ("H2 call-dom", [td for td in dom if td.snap_i >= mid
                             and td.direction == "call"]),
            ("H2 put-dom", [td for td in dom if td.snap_i >= mid
                            and td.direction == "put"]),
        ], signed=True))

    # ------------------------------------------------------- caveats
    print("""
_Columns: n = ticker-days (or contracts) with T+1 resolved; n10 = with T+10
resolved; xT+k = mean excess forward return vs the equal-weight universe mean
on the same session; s× = direction-signed (call = +excess, put = −excess);
med = median of the T+5 column's values; |T+5| = mean absolute raw T+5 move
(does the flag predict MOVEMENT); Beat5 = share of the SAME table's T+5
column values > 0 — i.e. excess in unsigned tables, signed excess in s×
tables (the calibration table's Pos5 is the only raw-return share)._

**Caveats** — read before quoting numbers:
- One ~2.3-month window on a mostly RISK-ON tape; no regime variety.
- Samples are heavily clustered: the same ticker re-appears on consecutive
  days (one flow episode → many ticker-days) and all tickers share each
  session's tape. Excess-vs-universe removes the day effect; nothing removes
  the episode effect. Treat n as generous.
- EOD snapshots cannot see trade side: a "call-dominant" day may be opening
  buys (bullish), closing sells, or covered-call writing (neutral/bearish).
  Direction-signed results test the naive "follow the printed side" reading.
- The OI-confirmation replay joins only contracts RE-flagged the next
  snapshot (vol still elevated). The live scan joins full chains, including
  the higher-information "vol normalized, OI held" case that snapshots
  don't store. Tier comparisons here are within-subset only.
- Chronicity is computed over the full sample (mildly forward-looking as a
  filter attribute; in live use, trailing appearance counts approximate it).
- Universe baseline uses the CURRENT universe list (survivorship optimism)
  equal-weighted over ~1,000 names — and it CONTAINS the flagged names
  themselves (~10-20% of the pool on a typical day), which drags the
  baseline slightly toward the flagged pool; measured underperformance is
  therefore mildly conservative.
- The flagged pool structurally skews to high-volatility names (that's what
  generates options volume), so part of the underperformance may be a style
  effect of volatile names in this particular tape rather than information
  in the flow. The flat-pre-move bucket (−2..+2%) is the cleanest read of
  the flow-specific component.
- Bucket cutoffs (70/30 direction, chronicity thirds, cluster sizes) were
  chosen in-sample; many strata are tested, so isolated 1-in-20 outliers
  are expected. Weight consistent gradients over single cells.""")


if __name__ == "__main__":
    main()
