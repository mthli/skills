#!/usr/bin/env python3
"""Compute the board-vs-index benchmark curves for the HTML dashboard.

Answers the one question the dashboard can't ask of itself: would the
same money have done better sitting in SPY / QQQ? Builds three equity
curves over the recorded run-days and writes them to
state/benchmark.json, which render_history_html.py draws as-is (that
script stays stdlib-only and never touches the network).

  - board: equal-weight the displayed top-N, rebalanced every run-day —
    the daily-portfolio form of the skill's canonical convention (enter
    at the listing close, exit at the dropout-observation close, i.e.
    backtest_outcomes.py's default --fills close). Day t's return
    applies day t-1's board to close(t-1) → close(t), so the board being
    held is always one that was already published: no lookahead.
  - spy / qqq: the same close-to-close dates, fully invested throughout.

Gaps between run-days (weekends, or a scan that didn't run) are HELD,
not skipped — the return over the gap is the return you'd actually have
taken, since the last published board is all you had.

All three lines are idealized close fills with no costs or slippage —
but NOT equally idealized, and the difference runs one way. The board
rebalances every run-day (~4 of 30 names swap daily in the 2026-05→07
sample, ~13% one-way turnover, plus the equal-weight reset on the rest);
SPY / QQQ are bought once and held. Free trading is therefore a subsidy
the board collects and the indices don't — at 10bp round-trip it is
worth roughly 0.6pp over 51 run-days, against a measured gap of 2.4pp.
Read the gap rather than any line's level, then discount the gap itself.

Per-day price coverage is recorded alongside the curves, so a name whose
series went missing shows up as a hole instead of quietly flattering the
board.

The same bars also yield a per-(member, run-day) price block — close, ATR
and the interval high — which the dashboard's tooltips turn into entry
price, stop and trailing stop. See build_prices for why those live here
instead of in history.csv.

Prices come from backtest_outcomes.fetch_prices (auto_adjust=True) and
share its on-disk cache, so running this right after the backtest costs
only the two index series.

Run (before re-rendering the dashboard; ~150 tickers, batched fetch):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python compute_benchmark.py [--top-n 30] [--refresh-prices]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest_outcomes import fetch_prices, pos_of

SKILL_DIR = Path(__file__).resolve().parent.parent
HISTORY_CSV = SKILL_DIR / "state" / "history.csv"
OUT_JSON = SKILL_DIR / "state" / "benchmark.json"
INDICES = ("SPY", "QQQ")
# MUST equal scan.py's ATR_PERIOD_DAYS. The dashboard draws its stop from
# the ATR computed here, and a stop that disagreed with the one the CLI
# printed the same morning is the worst kind of wrong — it looks right.
# test_render_benchmark.py holds the drift-guard.
ATR_PERIOD_DAYS = 14


def load_boards(history_path: Path, top_n: int) -> tuple[list[str], dict[str, list[str]]]:
    """Run-days plus the displayed top-N membership of each."""
    with open(history_path, newline="") as f:
        rows = list(csv.DictReader(f))
    # Last row wins per (run_id, ticker): scan.py upserts a re-run of the
    # same ET day in place, but a hand-edited history could still double up.
    dedup: dict[tuple[str, str], dict] = {}
    for r in rows:
        dedup[(r["run_id"], r["ticker"])] = r
    boards: dict[str, list[str]] = {}
    for (day, t), r in sorted(dedup.items()):
        if int(r["rank"]) <= top_n:
            boards.setdefault(day, []).append(t)
    days = sorted(boards)
    return days, boards


def pair_return(bars: pd.DataFrame, d0: pd.Timestamp,
                d1: pd.Timestamp) -> float | None:
    """Close-to-close return between two run-days, or None when the series
    lacks a fresh bar for each (halt, delisting, hole in the data)."""
    p0, p1 = pos_of(bars, d0), pos_of(bars, d1)
    if p0 is None or p1 is None or p1 <= p0:
        return None
    c0, c1 = float(bars["Close"].iloc[p0]), float(bars["Close"].iloc[p1])
    if not (c0 > 0 and c1 > 0):
        return None
    return c1 / c0 - 1.0


def ts_of(day: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:8]}")


def default_start(first_day: str) -> str:
    """Price-download start: a run-up before the first run-day.

    Derived rather than fixed, so a history that reaches further back
    can't silently resolve to "no bars before the start" — which reads
    as a flat board with a warning per day, not as an error.

    The curves only need one prior bar, but the price block's ATR needs
    ATR_PERIOD_DAYS + 1 sessions before it reports anything, so the
    run-up is sized for that instead: a two-week lead-in left the first
    three run-days of the whole history with no stop to draw. 32 calendar
    days clears ~22 sessions, holidays included.

    That is wider than fetch_prices' 7-day staleness tolerance around
    backtest_outcomes.py's own constant start, so the two no longer agree
    on range and the first run after this widening refetches once. The
    cache they share is wider for both afterwards."""
    return (ts_of(first_day) - pd.Timedelta(days=32)).strftime("%Y-%m-%d")


def priced_pairs(tickers: list[str], d0: pd.Timestamp, d1: pd.Timestamp,
                 prices: dict[str, pd.DataFrame]) -> int:
    return sum(1 for t in tickers
               if t in prices and pair_return(prices[t], d0, d1) is not None)


def covers_last_day(days: list[str], boards: dict[str, list[str]],
                    prices: dict[str, pd.DataFrame]) -> bool:
    """Does every leg of the final day exist in the price data?

    The cache is shared with backtest_outcomes.py, whose freshness guard
    only dates the snapshot by SPY — and SPY rides along on every chunk
    request, so it can be days newer than the member bars around it. That
    mix is the dangerous state: the indices would advance on the last day
    while the board is carried flat, which reads as underperformance the
    board never had."""
    d0, d1 = ts_of(days[-2]), ts_of(days[-1])
    return (priced_pairs(boards[days[-2]], d0, d1, prices) > 0
            and priced_pairs(list(INDICES), d0, d1, prices) == len(INDICES))


def build_curves(days: list[str], boards: dict[str, list[str]],
                 prices: dict[str, pd.DataFrame]) -> dict:
    """Three equity curves indexed to 100 on the first run-day."""
    board = [100.0]
    idx_curves = {k: [100.0] for k in INDICES}
    coverage: list[list[int] | None] = [None]
    for i in range(1, len(days)):
        d0, d1 = ts_of(days[i - 1]), ts_of(days[i])
        held = boards[days[i - 1]]  # yesterday's published board, held into today
        rets = [r for t in held
                if (r := (pair_return(prices[t], d0, d1)
                          if t in prices else None)) is not None]
        coverage.append([len(rets), len(held)])
        if not rets:
            print(f"WARNING: no priced holding on {days[i]}; carrying the "
                  f"board flat for that day", file=sys.stderr)
        board.append(board[-1] * (1 + (sum(rets) / len(rets) if rets else 0.0)))
        for k in INDICES:
            r = pair_return(prices[k], d0, d1) if k in prices else None
            if r is None:
                print(f"WARNING: no {k} bar pair for {days[i - 1]} → "
                      f"{days[i]}; carrying it flat", file=sys.stderr)
            idx_curves[k].append(idx_curves[k][-1] * (1 + (r or 0.0)))
    return {
        "days": days,
        "board": [round(v, 3) for v in board],
        **{k.lower(): [round(v, 3) for v in idx_curves[k]] for k in INDICES},
        "coverage": coverage,
    }


def build_prices(days: list[str], members: list[str],
                 prices: dict[str, pd.DataFrame]) -> dict:
    """Per-(member, run-day) close, ATR and interval high.

    The tooltips want three price facts history.csv cannot supply: what a
    name closed at on a day it was listed, where an ATR stop would have
    sat, and how high it got between two run-days (what a trailing stop
    rides). All three need daily bars, which this script already holds for
    every board member — so computing them here costs no fetch and covers
    the *whole* recorded history at once, rather than only the days since
    a new column started being written.

    Everything comes off one auto-adjusted series, which is the real
    reason not to store these per run: a split between the entry day and
    today rescales both ends here, so "up 11.7% since it listed" stays
    arithmetic. Mixing an as-seen entry close with a later adjusted close
    would not. The cost is that an old day's level is today's adjusted
    view of it, not the price that printed — history.csv's `close` stays
    the as-seen record for anything that needs it.

      c: close on the run-day itself
      a: ATR over the trailing ATR_PERIOD_DAYS sessions as of that bar
      h: highest close from the session after the previous run-day through
         this one — the piece a running peak-since-entry sums from, which
         keeps episode knowledge out of this function

    A day with no bar of its own is null in all three, and holds the
    interval open so the next day's `h` still spans the gap. The curves
    resolve such a day to the nearest prior bar within 5 days, which is
    right for them (the return over a halt is zero) and wrong here: a
    tooltip would print a week-old price under today's date. Requiring the
    exact session costs nothing on the current history (0 of 6987 cells
    resolve to an earlier bar) and keeps a halted name blank instead of
    stale.
    """
    stamps = [ts_of(d) for d in days]
    out: dict[str, dict] = {}
    for t in members:
        bars = prices.get(t)
        if bars is None or bars.empty:
            continue
        close = bars["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            bars["High"] - bars["Low"],
            (bars["High"] - prev_close).abs(),
            (bars["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        # Simple mean of the last N true ranges, matching scan.py's
        # compute_atrs (Wilder's smoothing differs by <5% in steady state
        # and this one has to agree with the CLI, not with the textbook).
        atr = tr.rolling(ATR_PERIOD_DAYS).mean()
        cs, as_, hs = [], [], []
        seen = None  # bar position of the last run-day that resolved
        for stamp in stamps:
            p = pos_of(bars, stamp)
            # A non-positive close would divide into the page's percentages
            # (scan.compute_atrs rejects the same thing before sizing a
            # stop off it), so it is missing data rather than a price.
            c = float(close.iloc[p]) if p is not None else 0.0
            if p is None or bars.index[p].normalize() != stamp or c <= 0:
                cs.append(None)
                as_.append(None)
                hs.append(None)
                continue
            cs.append(round(c, 4))
            a = atr.iloc[p]
            as_.append(None if pd.isna(a) or a <= 0 else round(float(a), 4))
            # First resolved day has no prior run-day to span from, so its
            # interval is itself — a peak-since-entry must start at the
            # entry close, not at some high the name made before listing.
            seg = close.iloc[(p if seen is None else seen + 1):p + 1]
            hs.append(round(float(seg.max()) if len(seg) else c, 4))
            seen = p
        if any(v is not None for v in cs):
            out[t] = {"c": cs, "a": as_, "h": hs}
    return out


DEFAULT_CACHE = Path(tempfile.gettempdir()) / "momentum_backtest_prices.pkl"


def refresh(history: Path = HISTORY_CSV, out: Path = OUT_JSON,
            top_n: int = 30, cache: Path = DEFAULT_CACHE,
            refresh_prices: bool = False,
            start: str | None = None) -> dict | None:
    """Rebuild the curves from `history` and write them to `out`.

    Returns the payload, or None when history is too short to draw one
    (the first scan against a fresh history.csv). scan.py calls this
    right after its own history save, so the file is never a day behind
    the board the dashboard would draw."""
    days, boards = load_boards(history, top_n)
    if len(days) < 2:
        return None
    start = start or default_start(days[0])
    members = sorted({t for m in boards.values() for t in m})
    print(f"history: {len(days)} run-days, {len(members)} board members",
          file=sys.stderr)

    prices = fetch_prices(members + list(INDICES), start, cache,
                          refresh_prices)
    if not refresh_prices and not covers_last_day(days, boards, prices):
        print(f"cached bars don't reach {days[-1]}; refetching everything",
              file=sys.stderr)
        prices = fetch_prices(members + list(INDICES), start, cache,
                              refresh=True)
    for k in INDICES:
        if k not in prices:
            # RuntimeError, not SystemExit: scan.py calls this mid-run and
            # a SystemExit would walk straight through its except-Exception
            # guard and kill the scan before it prints anything.
            raise RuntimeError(f"no price series for {k}; cannot draw the "
                               f"benchmark (retry with --refresh-prices)")
    missing = [t for t in members if t not in prices]
    if missing:
        print(f"no price series for {len(missing)} member(s) — they drop out "
              f"of the equal-weight mean on their days: "
              f"{', '.join(missing)}", file=sys.stderr)

    payload = {
        "generated": datetime.now(timezone.utc).astimezone()
                             .strftime("%Y-%m-%d %H:%M %Z"),
        "top_n": top_n,
        "fills": "close",
        "atr_period": ATR_PERIOD_DAYS,
        **build_curves(days, boards, prices),
        "px": build_prices(days, members, prices),
    }
    write_curves(payload, out)
    return payload


def write_curves(payload: dict, out: Path) -> None:
    """Write the curves one value per line.

    The file is tracked, and the daily scan rewrites it whole, so the
    format decides whether a year of commits reads as 250 appends or 250
    unreviewable one-line rewrites. Values are cumulative from a fixed
    base and a dividend rescales every bar before its ex-date by one
    factor, which leaves the ratios between older bars alone — so past
    days hold still and each run really does append.

    The price block dominates the byte count (three arrays per member vs
    four curves), and it appends the same way — one new value per array
    per run-day — so the format holds up at that size."""
    # tmp + rename, like append_history and save_sectors: the file is
    # tracked and the nightly job commits whatever it finds, so a write
    # interrupted halfway would be committed as truncated JSON.
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n")
    tmp.replace(out)


def summarize(payload: dict, out: Path) -> str:
    curves = ("board",) + tuple(k.lower() for k in INDICES)
    last = {k: payload[k][-1] - 100 for k in curves}
    return (f"wrote {out} ({len(payload['days'])} run-days: "
            + ", ".join(f"{k} {v:+.1f}%" for k, v in last.items())
            # .get: a summary line is never worth crashing a scan over,
            # and scan.py calls this on whatever payload it was handed.
            + f"; prices for {len(payload.get('px') or {})} members)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, default=HISTORY_CSV)
    ap.add_argument("--top-n", type=int, default=30,
                    help="board membership cutoff, matching scan.py's default")
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help="shared with backtest_outcomes.py")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default=None,
                    help="price download start date "
                         "(default: two weeks before the first run-day)")
    args = ap.parse_args()

    try:
        payload = refresh(args.history, args.out, args.top_n, args.cache,
                          args.refresh_prices, args.start)
    except RuntimeError as e:
        raise SystemExit(str(e))
    if payload is None:
        raise SystemExit("need at least 2 run-days to draw a curve")
    print(summarize(payload, args.out))


if __name__ == "__main__":
    main()
