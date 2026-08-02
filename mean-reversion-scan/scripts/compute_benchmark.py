#!/usr/bin/env python3
"""Matched-horizon index returns for the ⭐ pocket panel's benchmark line.

Answers what the panel alone can't: is the expectancy an edge, or was the
market simply up over the days these signals were held? For every resolved
signal in state/outcomes.csv it buys SPY (and QQQ) at the same signal-day
close and sells after the same number of sessions the signal actually took
to resolve, then writes the per-day averages to state/benchmark.json for
render_history_html.py (which stays stdlib-only and never fetches).

Matched horizon, not a buy-and-hold curve. The panel's axis is %/signal,
so the benchmark has to be %/signal too — an index equity curve on that
axis would be two different measures sharing one scale. A signal that
resolved in 2 sessions is compared against 2 sessions of SPY; one that
expired after 5 gets 5.

Two known biases, both favoring the signals, so the gap is a ceiling
rather than a measurement:

  - Exit price. A signal books its result at the price that resolved it —
    the target or the stop it touched INTRADAY — while the index is sold
    at that day's close. Nobody gets to touch-trade SPY at the same
    moment, so the strategy side is priced at its best moment of the day
    and the benchmark at an arbitrary one.
  - Entry timing. The signals choose their days (oversold prints); the
    index only inherits them. That one is the point of the comparison,
    not a flaw — timing skill is supposed to show up as the gap — but it
    is why the gap is not a "SPY vs a fund" number.

Costs are ignored on both sides, and there the asymmetry runs the other
way from momentum-scan's board: this side turns over once per signal
either way, so free trading doesn't single out one line.

Per-day means rather than per-signal rows: the renderer already knows
which signals it counted, and a per-signal file would grow ~10 tracked
rows a day to say the same thing.

Run (fetches two tickers, so it is quick; scan.py does this automatically
after each ledger update):
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python compute_benchmark.py [--refresh-prices]
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

from backtest_outcomes import fetch_prices

SKILL_DIR = Path(__file__).resolve().parent.parent
OUTCOMES_CSV = SKILL_DIR / "state" / "outcomes.csv"
OUT_JSON = SKILL_DIR / "state" / "benchmark.json"
INDICES = ("SPY", "QQQ")
DEFAULT_CACHE = Path(tempfile.gettempdir()) / "mr_benchmark_prices.pkl"


def ts_of(day: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:8]}")


def default_start(first_day: str) -> str:
    """Two weeks of run-up before the earliest signal day."""
    return (ts_of(first_day) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")


def load_signals(path: Path) -> list[tuple[str, int]]:
    """(signal day, sessions held) for every resolved signal in the ledger."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        held = r.get("days_to_resolve")
        if not held:
            continue
        out.append((r["run_id"], int(float(held))))
    return sorted(out)


def matched_return(closes: pd.Series, day: str, held: int) -> float | None:
    """The index's return over the same window: signal-day close to the
    close `held` sessions later. None when the series doesn't reach."""
    p = closes.index.searchsorted(ts_of(day), side="right") - 1
    if p < 0 or (ts_of(day) - closes.index[p]).days > 5:
        return None
    q = p + held
    if q >= len(closes):
        return None
    c0, c1 = float(closes.iloc[p]), float(closes.iloc[q])
    if not (c0 > 0 and c1 > 0):
        return None
    return (c1 / c0 - 1.0) * 100


def covers_newest(signals: list[tuple[str, int]],
                  prices: dict[str, pd.DataFrame]) -> bool:
    """Can the newest resolved signal's window be priced?

    fetch_prices calls a cache fresh while its last bar is within 5 days,
    so a daily run would keep reusing bars that stop before the windows
    that just closed. Those signals would quietly sit out the average for
    days and then reappear once the cache aged past the tolerance."""
    day, held = max(signals)
    return all(matched_return(prices[k]["Close"].dropna(), day, held)
               is not None for k in INDICES if k in prices)


def build_curves(signals: list[tuple[str, int]],
                 prices: dict[str, pd.DataFrame]) -> dict:
    """Per-signal-day mean matched return, one bucket per day."""
    days = sorted({d for d, _ in signals})
    at = {d: i for i, d in enumerate(days)}
    sums = {k: [0.0] * len(days) for k in INDICES}
    counts = [0] * len(days)
    unmatched = 0
    for day, held in signals:
        vals = {k: matched_return(prices[k]["Close"].dropna(), day, held)
                for k in INDICES}
        if any(v is None for v in vals.values()):
            unmatched += 1
            continue
        counts[at[day]] += 1
        for k in INDICES:
            sums[k][at[day]] += vals[k]
    if unmatched:
        print(f"{unmatched} signal(s) had no matching index window "
              f"(too recent, or a hole in the index series); they sit out "
              f"the benchmark", file=sys.stderr)
    return {
        "days": days,
        "n": counts,
        **{k.lower(): [round(s / n, 4) if n else None
                       for s, n in zip(sums[k], counts)] for k in INDICES},
    }


def write_curves(payload: dict, out: Path) -> None:
    """One value per line, written atomically.

    The file is tracked and rewritten whole on every run, so a compact
    dump would make each commit an unreadable one-line rewrite; past days
    don't move, so this reads as an append. tmp + rename because the
    nightly job commits whatever it finds."""
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n")
    tmp.replace(out)


def refresh(outcomes: Path = OUTCOMES_CSV, out: Path = OUT_JSON,
            cache: Path = DEFAULT_CACHE, refresh_prices: bool = False,
            start: str | None = None) -> dict | None:
    """Rebuild the matched-horizon averages. None when nothing resolved yet."""
    signals = load_signals(outcomes)
    if not signals:
        return None
    start = start or default_start(signals[0][0])
    print(f"ledger: {len(signals)} resolved signal(s) from {signals[0][0]}",
          file=sys.stderr)
    prices = fetch_prices(list(INDICES), start, cache, refresh_prices)
    if not refresh_prices and not covers_newest(signals, prices):
        print(f"cached bars don't reach the newest resolved window; "
              f"refetching", file=sys.stderr)
        prices = fetch_prices(list(INDICES), start, cache, refresh=True)
    for k in INDICES:
        if k not in prices:
            # RuntimeError, not SystemExit: scan.py calls this mid-run and
            # a SystemExit would walk through its except-Exception guard.
            raise RuntimeError(f"no price series for {k}; cannot build the "
                               f"benchmark (retry with --refresh-prices)")
    payload = {
        "generated": datetime.now(timezone.utc).astimezone()
                             .strftime("%Y-%m-%d %H:%M %Z"),
        "fills": "close",
        "basis": "matched-horizon",
        **build_curves(signals, prices),
    }
    write_curves(payload, out)
    return payload


def summarize(payload: dict, out: Path) -> str:
    total = sum(payload["n"])
    means = {k.lower(): sum(v * n for v, n in zip(payload[k.lower()],
                                                  payload["n"]) if v is not None)
             / total for k in INDICES} if total else {}
    return (f"wrote {out} ({total} matched signal(s): "
            + ", ".join(f"{k} {v:+.2f}%/signal" for k, v in means.items()) + ")")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outcomes", type=Path, default=OUTCOMES_CSV)
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default=None,
                    help="price download start date "
                         "(default: two weeks before the first signal)")
    args = ap.parse_args()

    try:
        payload = refresh(args.outcomes, args.out, args.cache,
                          args.refresh_prices, args.start)
    except RuntimeError as e:
        raise SystemExit(str(e))
    if payload is None:
        raise SystemExit("no resolved signals in the ledger yet")
    print(summarize(payload, args.out))


if __name__ == "__main__":
    main()
