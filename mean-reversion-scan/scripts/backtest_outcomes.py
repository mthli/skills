#!/usr/bin/env python3
"""Outcome backtest for mean-reversion-scan history.

Replays state/history.csv with the exact trade convention scan.py's rolling
win-rate stat uses: entry at the signal-day close, a take-profit limit at the
recorded 5DMA target, a stop at the recorded ATR stop, WON if the intraday
High touches the target within the window, LOST if the intraday Low touches
the stop first, EXPIRED at the window-end close, same-day double-touch
resolved optimistically as WON (Connors end-of-day convention). What scan.py
never does is slice that stat — this script stratifies outcomes by every
attribute recorded at signal time (signal tier, RSI(2) depth, score,
pullback size, trend buffer, trigger frequency, rank) plus the derived
day-of-spell (how many consecutive runs the name had already been listed —
the "stuck oversold is a yellow flag" claim), and adds three lenses the
rolling stat can't see:

  - expectancy, not just win rate — the stop sits ~2.5 ATR below entry while
    the target sits a couple % above, so a high win rate can still lose money;
  - gap-aware fills — the limit fills at max(target, open) and the stop at
    min(stop, open), so overnight gaps pay their real slippage;
  - a no-exit baseline — the raw close-to-close return over the same window
    if you used no target and no stop at all.

Beyond the single-attribute strata it reports the combined entry filter
(Score >= 40 on a 1st-2nd-day listing), a scan-breadth stratification
(signals emitted that run-day — quiet-day vs washout-day signals behave
very differently), a first-half/second-half stability split, and an
exit-horizon test — every maximum holding period replayed over ONE fixed
set of trades, with the delta paired per trade against the live rule, so
"cut it if it hasn't bounced by day K" is answered by what the change
would have earned rather than by bucketing the ledger on days-to-resolve
(which conditions on the future and gives the opposite answer). Every
number quoted in SKILL.md's "Backtested outcomes" section reproduces from
this one script.

--entry next-open replays the realistic execution instead: the scan output
only exists after the close, so entry is the NEXT session's open (signals
that gap past their target or stop before entry are skipped), and outcomes
are measured from that fill. Compare against the default --entry close to
see how much of the edge survives entry timing.

Prices are fetched with auto_adjust=True. Dividend re-adjustment between the
scan date and today shifts the recorded price levels out of the series'
units, so each signal is re-anchored: the recorded close is matched against
the series' close on the signal day and target/stop are rescaled by that
ratio; signals whose recorded close disagrees with the series by >5% even
after trying the prior bar (corporate action, unit break) are dropped and
reported rather than resolved on mismatched units.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--target-window 5] [--entry next-open] \
    [--refresh-prices]
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SKILL_DIR = Path(__file__).resolve().parent.parent
HISTORY_CSV = SKILL_DIR / "state" / "history.csv"
SECTORS_JSON = SKILL_DIR / "state" / "sectors.json"

ET = "America/New_York"


# ---------------------------------------------------------------- history

@dataclass
class Signal:
    run_day: str            # YYYYMMDD run_id
    et_date: pd.Timestamp   # ET calendar date whose close the scan read
    ticker: str
    rank: int
    score: float
    rsi2: float
    dist_5dma: float
    dist_50dma: float
    dist_200dma: float
    last_close: float
    target: float
    stop: float | None
    signal: str
    freq_60d: int
    day_of_spell: int = 1   # 1 = first consecutive run-day this name listed


def load_signals(history_path: Path) -> tuple[list[Signal], list[str]]:
    with open(history_path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("rank")]
    run_days = sorted({r["run_id"] for r in rows})
    day_idx = {d: i for i, d in enumerate(run_days)}

    signals: list[Signal] = []
    for r in rows:
        run_date = pd.Timestamp(r["run_date"])
        # The scan reads "the latest close" at run time. Automated runs fire
        # in the ET evening after the close (08:00 Beijing), so the ET date
        # of the run timestamp IS the trading day whose close was recorded.
        et_date = run_date.tz_convert(ET).normalize().tz_localize(None)
        stop_raw = r.get("stop_price", "")
        signals.append(Signal(
            run_day=r["run_id"],
            et_date=et_date,
            ticker=r["ticker"],
            rank=int(r["rank"]),
            score=float(r["score"]),
            rsi2=float(r["rsi2"]),
            dist_5dma=float(r["dist_5dma_pct"]),
            dist_50dma=float(r["dist_50dma_pct"]),
            dist_200dma=float(r["dist_200dma_pct"]),
            last_close=float(r["last_close"]),
            target=float(r["target_price"]),
            stop=float(stop_raw) if stop_raw not in ("", "None") else None,
            signal=r["signal"].strip(),
            freq_60d=int(float(r["freq_60d"])) if r.get("freq_60d") else 0,
        ))

    # Day-of-spell: consecutive run-days a ticker keeps appearing. A gap in
    # the run-day sequence starts a new spell.
    per_ticker: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        per_ticker[s.ticker].append(s)
    for t, sigs in per_ticker.items():
        sigs.sort(key=lambda s: s.run_day)
        prev_idx = None
        spell = 1
        for s in sigs:
            idx = day_idx[s.run_day]
            spell = spell + 1 if (prev_idx is not None
                                  and idx - prev_idx == 1) else 1
            s.day_of_spell = spell
            prev_idx = idx
    return signals, run_days


def load_sector_map() -> dict[str, str]:
    try:
        with open(SECTORS_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        return {t: (v.get("sector") or "?") for t, v in raw.items()
                if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError):
        return {}


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

@dataclass
class Outcome:
    sig: Signal
    outcome: str                   # WON / LOST / EXPIRED
    days: int                      # days to hit, or window length if EXPIRED
    result: float                  # % vs entry, canonical fills (scan.py units)
    gap_result: float              # % vs entry, gap-aware fills
    ambiguous: bool                # target AND stop both touched same day
    fwd_ret: float | None          # close-to-close % over full window, no exits


def resolve_signal(sig: Signal, bars: pd.DataFrame, window: int,
                   entry_mode: str = "close") -> tuple[Outcome | None, str]:
    """Returns (outcome, status) where status is one of
    resolved / open / no_bars / unit_mismatch / gap_skip.

    entry_mode "close" is scan.py's convention (entry at the signal-day
    close). "next-open" is realistic execution: the scan output exists only
    after the close, so entry is the next session's open; a signal whose
    next open is already past the target (bounce done overnight) or through
    the stop (setup busted) is skipped as untradable → gap_skip."""
    closes = bars["Close"]
    pos = closes.index.searchsorted(sig.et_date, side="right") - 1
    if pos < 0:
        return None, "no_bars"

    # Re-anchor recorded units onto the (possibly re-adjusted) series. The
    # scan may also have run mid-session off a partial bar; if the bar at the
    # ET date disagrees with the recorded close, the prior bar gets a try.
    ratio = float(closes.iloc[pos]) / sig.last_close
    if abs(ratio - 1) > 0.02 and pos >= 1:
        alt = float(closes.iloc[pos - 1]) / sig.last_close
        if abs(alt - 1) < abs(ratio - 1):
            pos, ratio = pos - 1, alt
    if abs(ratio - 1) > 0.05:
        return None, "unit_mismatch"

    target = sig.target * ratio
    stop = sig.stop * ratio if sig.stop is not None else None

    post = bars.iloc[pos + 1:pos + 1 + window]
    if len(post) == 0:
        return None, "open"

    if entry_mode == "next-open":
        entry = float(post["Open"].iloc[0])
        if not np.isfinite(entry):
            return None, "no_bars"
        if entry >= target or (stop is not None and entry <= stop):
            return None, "gap_skip"
    else:
        entry = float(closes.iloc[pos])

    fwd_ret = (float(post["Close"].iloc[window - 1]) / entry - 1) * 100 \
        if len(post) >= window else None

    for d, (_, row) in enumerate(post.iterrows(), 1):
        hit_t = row["High"] >= target
        hit_s = stop is not None and row["Low"] <= stop
        if hit_t:
            # Limit sell at target: a gap-up open fills at the open (better).
            gap_fill = max(target, float(row["Open"]))
            return Outcome(sig, "WON", d,
                           (target / entry - 1) * 100,
                           (gap_fill / entry - 1) * 100,
                           ambiguous=hit_s, fwd_ret=fwd_ret), "resolved"
        if hit_s:
            # Stop order: a gap-down open fills at the open (worse).
            gap_fill = min(stop, float(row["Open"]))
            return Outcome(sig, "LOST", d,
                           (stop / entry - 1) * 100,
                           (gap_fill / entry - 1) * 100,
                           ambiguous=False, fwd_ret=fwd_ret), "resolved"

    if len(post) < window:
        return None, "open"  # window not fully elapsed and nothing hit
    last_close = float(post["Close"].iloc[window - 1])
    r = (last_close / entry - 1) * 100
    return Outcome(sig, "EXPIRED", window, r, r,
                   ambiguous=False, fwd_ret=fwd_ret), "resolved"


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:.{nd}f}{suffix}"


def agg(outs: list[Outcome]) -> dict:
    won = [o for o in outs if o.outcome == "WON"]
    lost = [o for o in outs if o.outcome == "LOST"]
    dec = len(won) + len(lost)
    fwd = [o.fwd_ret for o in outs if o.fwd_ret is not None]
    return {
        "n": len(outs),
        "dec": dec,
        "win": 100 * len(won) / dec if dec else None,
        "expired": 100 * (len(outs) - dec) / len(outs) if outs else None,
        "days_to_t": float(np.mean([o.days for o in won])) if won else None,
        "exp": float(np.mean([o.result for o in outs])) if outs else None,
        "gap_exp": float(np.mean([o.gap_result for o in outs])) if outs else None,
        "fwd": float(np.mean(fwd)) if fwd else None,
    }


def strata_table(title: str, groups: list[tuple[str, list[Outcome]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Stratum | n | dec | Win% | Expired% | d→T | Exp% | GapExp% | "
             "NoExit% |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, outs in groups:
        if not outs:
            continue
        a = agg(outs)
        lines.append(
            f"| {name} | {a['n']} | {a['dec']} | {fmt(a['win'], '%', 0)} | "
            f"{fmt(a['expired'], '%', 0)} | {fmt(a['days_to_t'])} | "
            f"{fmt(a['exp'], '%', 2)} | {fmt(a['gap_exp'], '%', 2)} | "
            f"{fmt(a['fwd'], '%', 2)} |")
    return "\n".join(lines)


def bucket(outs: list[Outcome], key, bounds: list[tuple[str, float, float]]):
    return [(name, [o for o in outs if lo <= key(o.sig) < hi])
            for name, lo, hi in bounds]


def paired_delta(cur: list[Outcome], base: list[Outcome]) -> tuple[float, str]:
    """Mean per-trade delta of one exit rule against another, trade matched
    to trade, with its 95% interval. A one-trade sample has no spread to
    quote and gets an em-dash rather than a nan."""
    d = np.array([c.result for c in cur]) - np.array([b.result for b in base])
    if len(d) < 2:
        return float(d.mean()), "—"
    se = float(d.std(ddof=1)) / np.sqrt(len(d))
    return float(d.mean()), (f"{d.mean() - 1.96 * se:+.2f} ~ "
                             f"{d.mean() + 1.96 * se:+.2f}")


def hold_days(o: Outcome, horizon: int) -> int:
    """Sessions the trade was actually held. A decisive trade leaves the day
    its target or stop prints; an expired one is held to the horizon's close.
    Averaged, this is time-in-market — the denominator a shorter exit rule is
    really buying."""
    return o.days if o.outcome in ("WON", "LOST") else horizon


def horizon_table(title: str, blurb: str, signals: list[Signal], prices: dict,
                  entry: str, horizons: list[int], ref: int, groups,
                  min_n: int = 30) -> None:
    """Exit-rule comparison on ONE fixed set of trades.

    Every horizon is replayed over the same signals — those resolvable at all
    of them — so the rows differ only by the exit rule, not by which signals
    happened to have enough bars (the trap the old window-sensitivity table
    fell into: a shorter window resolves signals a longer one still calls
    open, so the two columns were scoring different trades).

    The delta is PAIRED per trade against `ref`, the live rule, because that
    difference is what changing the rule would actually have earned. Reading
    it out of the resolved ledger instead — bucketing trades by how long they
    took to resolve — conditions on the future and inverts the answer: those
    buckets say holding past day 2 averages −1.5%, but that loss is measured
    from ENTRY and had already happened by day 2. Cutting there locks it in
    and forfeits the quarter of winners that land on days 3-5.

    Each table is followed by the same deltas over day-1 listings only — one
    trade per oversold spell, this file's per-spell view. The intervals in
    the table itself treat every row as an independent trade, and the
    consecutive days of one spell are not; the per-spell line is the honest
    read of how much evidence there is."""
    if ref not in horizons:
        raise ValueError(f"ref horizon {ref} must be one of {horizons}")
    by_h: dict[int, dict[tuple[str, str], Outcome]] = {h: {} for h in horizons}
    for s in signals:
        bars = prices.get(s.ticker)
        if bars is None:
            continue
        for h in horizons:
            o, status = resolve_signal(s, bars, h, entry)
            if status == "resolved":
                by_h[h][(s.run_day, s.ticker)] = o
    common = [k for k in by_h[ref] if all(k in by_h[h] for h in horizons)]
    print(f"\n### {title}\n")
    print(blurb)
    for gname, pred in groups:
        keys = [k for k in common if pred(by_h[ref][k].sig)]
        # Never drop a group in silence: a short history reaching this floor
        # would otherwise just make the table vanish.
        if len(keys) < min_n:
            print(f"\n**{gname}** — skipped: only {len(keys)} trades cleared "
                  f"every horizon, under this table's {min_n}-trade floor.")
            continue
        base = [by_h[ref][k] for k in keys]
        print(f"\n**{gname}** — n={len(keys)}\n")
        print(f"| Exit by | Win% | Stop% | Flat% | Hold d | Exp% | GapExp% | "
              f"Exp/day | Δ vs {ref}d | 95% CI |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for h in horizons:
            outs = [by_h[h][k] for k in keys]
            r = np.array([o.result for o in outs])
            days = float(np.mean([hold_days(o, h) for o in outs]))
            won = sum(o.outcome == "WON" for o in outs)
            lost = sum(o.outcome == "LOST" for o in outs)
            n = len(outs)
            if h == ref:
                delta = ci = "—"
            else:
                m, ci = paired_delta(outs, base)
                delta = f"{m:+.3f}%"
            label = f"{h}d" + (" **(live)**" if h == ref else "")
            print(f"| {label} | {100 * won / n:.0f}% | {100 * lost / n:.0f}% | "
                  f"{100 * (n - won - lost) / n:.0f}% | {days:.2f} | "
                  f"{r.mean():+.2f}% | "
                  f"{np.mean([o.gap_result for o in outs]):+.2f}% | "
                  f"{r.mean() / days:+.3f}% | {delta} | {ci} |")

        # The correlation-honest read: one trade per oversold spell. The
        # table's intervals count each consecutive day as its own evidence,
        # which overstates how much there is.
        indep = [k for k in keys if by_h[ref][k].sig.day_of_spell == 1]
        if len(indep) < min_n:
            print(f"\n_Per-spell view omitted: only {len(indep)} of these "
                  f"trades are day-1 listings, under the {min_n}-trade "
                  f"floor._")
        else:
            parts = []
            for h in horizons:
                if h == ref:
                    continue
                m, ci = paired_delta([by_h[h][k] for k in indep],
                                     [by_h[ref][k] for k in indep])
                parts.append(f"{h}d {m:+.3f}% ({ci})")
            print(f"\n_Per-spell view — day-1 listings only, n={len(indep)}: "
                  + " · ".join(parts) + "._")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, default=HISTORY_CSV)
    ap.add_argument("--target-window", type=int, default=5,
                    help="trading days for the bounce to play out (default 5, "
                         "matching scan.py)")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "mr_backtest_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default="2026-05-01",
                    help="price download start date")
    ap.add_argument("--entry", choices=["close", "next-open"], default="close",
                    help="close = scan.py's convention (signal-day close); "
                         "next-open = realistic execution at the next "
                         "session's open, skipping signals that gap past "
                         "target or stop before entry")
    args = ap.parse_args()

    signals, run_days = load_signals(args.history)
    tickers = sorted({s.ticker for s in signals})
    print(f"history: {len(run_days)} run-days, {len(signals)} signals, "
          f"{len(tickers)} tickers", file=sys.stderr)

    prices = fetch_prices(tickers + ["SPY"], args.start, args.cache,
                          args.refresh_prices)
    missing = sorted(t for t in tickers if t not in prices)

    outcomes: list[Outcome] = []
    n_open = 0
    n_no_bars = 0
    n_gap_skip = 0
    unit_mismatch: list[str] = []
    for s in signals:
        if s.ticker not in prices:
            continue
        o, status = resolve_signal(s, prices[s.ticker], args.target_window,
                                   args.entry)
        if status == "resolved":
            outcomes.append(o)
        elif status == "open":
            n_open += 1
        elif status == "no_bars":
            n_no_bars += 1
        elif status == "gap_skip":
            n_gap_skip += 1
        elif status == "unit_mismatch":
            unit_mismatch.append(f"{s.ticker}@{s.run_day}")

    a = agg(outcomes)
    ambiguous = [o for o in outcomes if o.ambiguous]
    # Pessimistic reading: every same-day double-touch counted as LOST.
    pes_won = sum(o.outcome == "WON" and not o.ambiguous for o in outcomes)
    pes_win = 100 * pes_won / a["dec"] if a["dec"] else None

    n_skipped_missing = sum(s.ticker not in prices for s in signals)
    print(f"# mean-reversion outcome backtest — history "
          f"{run_days[0]} → {run_days[-1]} — window {args.target_window}d — "
          f"entry mode: **{args.entry}**\n")
    print(f"**Signals**: {len(outcomes)} resolved of {len(signals)} "
          f"({n_open} still open, {n_no_bars} with no usable bars, "
          f"{n_skipped_missing} on tickers with no price data: "
          f"{', '.join(missing) or '—'})")
    if args.entry == "next-open":
        print(f"**Skipped at entry** (next open already past target or "
              f"through stop — untradable overnight gaps): {n_gap_skip}")
    if unit_mismatch:
        print(f"**Dropped for unit mismatch** (n={len(unit_mismatch)}): "
              f"{', '.join(unit_mismatch)}")
    print(f"**Decisive** (target or stop hit): {a['dec']} — win rate "
          f"{fmt(a['win'], '%', 0)}, avg {fmt(a['days_to_t'])} days to "
          f"target; {fmt(a['expired'], '%', 0)} expired flat")
    won_r = [o.result for o in outcomes if o.outcome == "WON"]
    lost_r = [o.result for o in outcomes if o.outcome == "LOST"]
    exp_r = [o.result for o in outcomes if o.outcome == "EXPIRED"]
    print(f"**Payoff asymmetry**: avg win "
          f"{fmt(float(np.mean(won_r)) if won_r else None, '%', 2)} "
          f"(n={len(won_r)}) · avg loss "
          f"{fmt(float(np.mean(lost_r)) if lost_r else None, '%', 2)} "
          f"(n={len(lost_r)}) · avg expired drift "
          f"{fmt(float(np.mean(exp_r)) if exp_r else None, '%', 2)} "
          f"(n={len(exp_r)})")
    print(f"**Same-day double-touch** (counted WON per Connors convention): "
          f"n={len(ambiguous)}; pessimistic win rate if all counted LOST: "
          f"{fmt(pes_win, '%', 0)}")
    print(f"**Expectancy per signal** (target/stop/expiry fills): "
          f"{fmt(a['exp'], '%', 2)} · gap-aware fills: "
          f"{fmt(a['gap_exp'], '%', 2)} · no-exit close-to-close over the "
          f"same window: {fmt(a['fwd'], '%', 2)}")

    print(strata_table("By signal tier", [
        ("🔵 deep (RSI2 < 2.5)", [o for o in outcomes if o.sig.signal == "🔵"]),
        ("🟢 fresh (2.5–5)", [o for o in outcomes if o.sig.signal == "🟢"]),
        ("🟡 forming (5–10)", [o for o in outcomes if o.sig.signal == "🟡"]),
    ]))

    print(strata_table("By RSI(2) depth", bucket(
        outcomes, lambda s: s.rsi2,
        [("<0.5 capitulation", 0, 0.5), ("0.5–1", 0.5, 1), ("1–2.5", 1, 2.5),
         ("2.5–5", 2.5, 5), ("5–10", 5, 10)])))

    print(strata_table("By day-of-spell (consecutive runs already listed)",
                       bucket(outcomes, lambda s: s.day_of_spell,
                              [("1st (fresh)", 1, 2), ("2nd", 2, 3),
                               ("3rd+ (stuck)", 3, 999)])))

    print(strata_table("By Reversion Score", bucket(
        outcomes, lambda s: s.score,
        [("<40", 0, 40), ("40–55", 40, 55), ("55–70", 55, 70),
         ("≥70", 70, 999)])))

    print(strata_table("By pullback depth (5DMA gap)", bucket(
        outcomes, lambda s: s.dist_5dma,
        [("≤-7% washout", -99, -7), ("-7..-4%", -7, -4), ("-4..-2%", -4, -2),
         (">-2% shallow", -2, 99)])))

    print(strata_table("By trend buffer (vs 200DMA)", bucket(
        outcomes, lambda s: s.dist_200dma,
        [("0–5% thin", 0, 5), ("5–15%", 5, 15), ("15–30%", 15, 30),
         (">30% extended", 30, 999)])))

    print(strata_table("By 50DMA side", bucket(
        outcomes, lambda s: s.dist_50dma,
        [("below 50DMA", -99, 0), ("0–5% above", 0, 5), (">5% above", 5, 99)])))

    print(strata_table("By trigger frequency (60d)", bucket(
        outcomes, lambda s: s.freq_60d,
        [("0 first time", 0, 1), ("1–2", 1, 3), ("3–5", 3, 6),
         ("≥6 noisy", 6, 999)])))

    print(strata_table("By rank at signal", bucket(
        outcomes, lambda s: s.rank,
        [("1–5", 1, 6), ("6–15", 6, 16), ("16+", 16, 9999)])))

    # Combined entry filter — the headline cross-cell SKILL.md quotes.
    def sc40(o):
        return o.sig.score >= 40

    def fresh(o):
        return o.sig.day_of_spell <= 2

    print(strata_table("Combined filter: Score≥40 × day-of-spell≤2", [
        ("Score≥40 & spell≤2 (THE filter)",
         [o for o in outcomes if sc40(o) and fresh(o)]),
        ("Score≥40 & spell 3+", [o for o in outcomes if sc40(o) and not fresh(o)]),
        ("Score<40 & spell≤2", [o for o in outcomes if not sc40(o) and fresh(o)]),
        ("Score<40 & spell 3+",
         [o for o in outcomes if not sc40(o) and not fresh(o)]),
    ]))

    # Scan breadth — how many signals the scan emitted that run-day (from the
    # raw history, open ones included). Cutoffs 30/60 were chosen in-sample.
    breadth = defaultdict(int)
    for s in signals:
        breadth[s.run_day] += 1

    def bre(o):
        return breadth[o.sig.run_day]

    print(strata_table("By scan breadth (signals emitted that run-day; "
                       "30/60 cutoffs chosen in-sample)", [
        ("<30 quiet day", [o for o in outcomes if bre(o) < 30]),
        ("30–60", [o for o in outcomes if 30 <= bre(o) <= 60]),
        (">60 washout day", [o for o in outcomes if bre(o) > 60]),
        ("<30 & Score≥40", [o for o in outcomes if bre(o) < 30 and sc40(o)]),
        (">60 & Score≥40", [o for o in outcomes if bre(o) > 60 and sc40(o)]),
    ]))

    # Stability split — does the combined filter's edge hold out-of-half?
    mid = run_days[len(run_days) // 2]
    print(strata_table(f"Stability: first vs second half (split at {mid})", [
        ("H1, filter", [o for o in outcomes
                        if o.sig.run_day <= mid and sc40(o) and fresh(o)]),
        ("H1, rest", [o for o in outcomes
                      if o.sig.run_day <= mid and not (sc40(o) and fresh(o))]),
        ("H2, filter", [o for o in outcomes
                        if o.sig.run_day > mid and sc40(o) and fresh(o)]),
        ("H2, rest", [o for o in outcomes
                      if o.sig.run_day > mid and not (sc40(o) and fresh(o))]),
    ]))

    sector_of = load_sector_map()
    by_sector: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_sector[sector_of.get(o.sig.ticker, "?")].append(o)
    sector_groups = sorted(
        ((sec, outs) for sec, outs in by_sector.items() if len(outs) >= 60),
        key=lambda kv: -len(kv[1]))
    if sector_groups:
        print(strata_table("By sector (n ≥ 60 only)", sector_groups))

    # Exit-rule sensitivity, both directions, paired on a fixed trade set.
    W = args.target_window
    horizon_groups = [
        ("All signals", lambda s: True),
        ("⭐ pocket (Score≥40 & spell≤2)",
         lambda s: s.score >= 40 and s.day_of_spell <= 2),
    ]
    horizon_table(
        f"Exit horizon — time stops vs the live {W}-day rule",
        "Same trades, same target and same stop; only the maximum holding "
        "period moves. This is the \"cut it if it hasn't bounced by day K\" "
        "rule, and Δ is what adopting it would have earned per trade.",
        signals, prices, args.entry, sorted({1, 2, 3, 4, W}), W,
        horizon_groups)
    horizon_table(
        f"Exit horizon — longer windows vs the live {W}-day rule",
        "The other direction: more time for the bounce. Paired on the "
        "signals that have bars for the longest horizon, so n is smaller "
        "than the table above and the two are not comparable row-for-row.",
        signals, prices, args.entry, sorted({W, W + 2, W * 2}), W,
        horizon_groups)

    print("\n_Exit-horizon columns: Win%/Stop%/Flat% = share of trades "
          "leaving at the target, at the stop, and at the horizon's close; "
          "Hold d = average sessions in the trade; Exp/day = Exp% divided by "
          "it, i.e. the capital-efficiency reading — a shorter rule can earn "
          "more per day held while earning less per trade, which only pays "
          "if the freed capital has another signal to go to. Δ and its "
          "interval are paired per trade against the live rule; that "
          "interval assumes the trades are independent, so read the "
          "per-spell line under each table as the amount of evidence there "
          "actually is (even it is only de-clustered by spell, not by day: "
          "names listed on the same session still fall together)._")

    print("\n_Columns: n = resolved signals; dec = decisive (target or stop "
          "hit); Win% = WON/dec; d→T = avg days to target among wins; Exp% = "
          "mean return per signal with canonical fills (target / stop / "
          "expiry close); GapExp% = same with gap-aware fills (limit fills at "
          "max(target, open), stop at min(stop, open)); NoExit% = mean "
          "close-to-close return over the full window with no target and no "
          "stop. Consecutive-day rows of one oversold spell are correlated — "
          "the day-of-spell table's '1st' row is the per-spell independent "
          "view. NoExit% is computed only over signals whose full window of "
          "bars exists, so signals resolved decisively near the data edge "
          "count toward Win%/Exp% but not NoExit% — the two columns' samples "
          "differ by that handful._")


if __name__ == "__main__":
    main()
