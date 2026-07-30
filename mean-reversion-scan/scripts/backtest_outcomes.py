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

Prices are fetched with auto_adjust=True. Dividend re-adjustment between the
scan date and today shifts the recorded price levels out of the series'
units, so each signal is re-anchored: the recorded close is matched against
the series' close on the signal day and target/stop are rescaled by that
ratio; signals whose recorded close disagrees with the series by >5% even
after trying the prior bar (corporate action, unit break) are dropped and
reported rather than resolved on mismatched units.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--target-window 5] [--refresh-prices]
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


def resolve_signal(sig: Signal, bars: pd.DataFrame,
                   window: int) -> tuple[Outcome | None, str]:
    """Returns (outcome, status) where status is one of
    resolved / open / no_bars / unit_mismatch."""
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

    entry = float(closes.iloc[pos])
    target = sig.target * ratio
    stop = sig.stop * ratio if sig.stop is not None else None

    post = bars.iloc[pos + 1:pos + 1 + window]
    if len(post) == 0:
        return None, "open"

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
    unit_mismatch: list[str] = []
    for s in signals:
        if s.ticker not in prices:
            continue
        o, status = resolve_signal(s, prices[s.ticker], args.target_window)
        if status == "resolved":
            outcomes.append(o)
        elif status == "open":
            n_open += 1
        elif status == "unit_mismatch":
            unit_mismatch.append(f"{s.ticker}@{s.run_day}")

    a = agg(outcomes)
    ambiguous = [o for o in outcomes if o.ambiguous]
    # Pessimistic reading: every same-day double-touch counted as LOST.
    pes_won = sum(o.outcome == "WON" and not o.ambiguous for o in outcomes)
    pes_win = 100 * pes_won / a["dec"] if a["dec"] else None

    n_skipped_missing = sum(s.ticker not in prices for s in signals)
    print(f"# mean-reversion outcome backtest — history "
          f"{run_days[0]} → {run_days[-1]} — window {args.target_window}d\n")
    print(f"**Signals**: {len(outcomes)} resolved of {len(signals)} "
          f"({n_open} still open, {n_skipped_missing} on tickers with no "
          f"price data: {', '.join(missing) or '—'})")
    if unit_mismatch:
        print(f"**Dropped for unit mismatch** (n={len(unit_mismatch)}): "
              f"{', '.join(unit_mismatch)}")
    print(f"**Decisive** (target or stop hit): {a['dec']} — win rate "
          f"{fmt(a['win'], '%', 0)}, avg {fmt(a['days_to_t'])} days to "
          f"target; {fmt(a['expired'], '%', 0)} expired flat")
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

    sector_of = load_sector_map()
    by_sector: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_sector[sector_of.get(o.sig.ticker, "?")].append(o)
    sector_groups = sorted(
        ((sec, outs) for sec, outs in by_sector.items() if len(outs) >= 60),
        key=lambda kv: -len(kv[1]))
    if sector_groups:
        print(strata_table("By sector (n ≥ 60 only)", sector_groups))

    # Window sensitivity — re-resolve everything at alternative windows.
    print("\n### Window sensitivity (aggregate)\n")
    print("| Window | n | dec | Win% | Expired% | d→T | Exp% | GapExp% | "
          "NoExit% |")
    print("|---|---|---|---|---|---|---|---|---|")
    for w in (3, 5, 10):
        outs_w = []
        for s in signals:
            if s.ticker not in prices:
                continue
            o, status = resolve_signal(s, prices[s.ticker], w)
            if status == "resolved":
                outs_w.append(o)
        aw = agg(outs_w)
        print(f"| {w}d | {aw['n']} | {aw['dec']} | {fmt(aw['win'], '%', 0)} | "
              f"{fmt(aw['expired'], '%', 0)} | {fmt(aw['days_to_t'])} | "
              f"{fmt(aw['exp'], '%', 2)} | {fmt(aw['gap_exp'], '%', 2)} | "
              f"{fmt(aw['fwd'], '%', 2)} |")

    print("\n_Columns: n = resolved signals; dec = decisive (target or stop "
          "hit); Win% = WON/dec; d→T = avg days to target among wins; Exp% = "
          "mean return per signal with canonical fills (target / stop / "
          "expiry close); GapExp% = same with gap-aware fills (limit fills at "
          "max(target, open), stop at min(stop, open)); NoExit% = mean "
          "close-to-close return over the full window with no target and no "
          "stop. Consecutive-day rows of one oversold spell are correlated — "
          "the day-of-spell table's '1st' row is the per-spell independent "
          "view._")


if __name__ == "__main__":
    main()
