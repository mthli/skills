#!/usr/bin/env python3
"""Outcome ledger for snapback-scan — replays state/runs/*.json.

The sister scans keep a flat history.csv and grade it quarterly. This skill
already writes one packet per run day, so the packets ARE the history: every
keg carries the attributes recorded at signal time (armed, spark types,
ignited, quiet, the seller-mechanism reads) plus the three prices the
protocol commits to. This script replays them and writes state/outcomes.csv.

The trade convention is SKILL.md's protocol, NOT the MR scan's:

    entry  = latest_close      "tranche 1: 1/4 size at ~<latest_close>"
    stop   = signal_day_low    "invalidation <signal_day_low> (hard exit,
                                no averaging down below it)"
    target = mr_target         "target 1: <mr_target>"

That distinction matters most for ignited kegs: their entry has already run
away from the signal-day low, so the invalidation sits far below and the
target sits close above — a wide-stop/narrow-target geometry the MR
convention (ATR stop) would hide. Quantifying that geometry is the point.

Three claims SKILL.md asserts but has never verified — the tables exist to
answer them, in this order:

  1. armed vs unarmed — is a DATED catalyst worth anything? The whole skill
     rests on yes.
  2. spark type — SKILL.md says a same-sector megacap verdict is a better
     structure than the keg's own earnings ("a coin flip, NOT a verdict").
     Does the ledger agree?
  3. ignited — the chase-guard rests on two observations (AMD 07-27,
     TSM 07-17). Two is an anecdote.

It also reports the protocol-compliant subset (armed & not ignited & not
quiet) separately from the full sample. The full sample includes entries the
skill would never take; only the compliant subset is the skill's actual
track record. Both are printed, because the gap between them IS the value
of the rules.

Sample honesty: with a handful of run days the tables are a plumbing check,
not evidence. The header says so in plain terms and estimates when the
sample will be readable at the observed arming rate. Never quote a stratum
row into SKILL.md before that banner clears.

Prices use auto_adjust=True, so a dividend between the packet and today
shifts recorded levels out of the series' units. Every signal is re-anchored
against the series close on its packet date. The anchor moves off that date
ONLY when the bar there is more than ANCHOR_TOL from the recorded entry (a
weekend, holiday, or pre-close run legitimately records a 1-3 session older
bar), and it then takes the first bar back inside tolerance — never the
globally closest one. Chasing the closest match silently anchors low-priced
kegs days early, and a window that opens before the signal grades the setup
on its own decline. Signals still off by >5% are dropped and reported,
never resolved on mismatched units.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
    python backtest_outcomes.py [--window 5] [--entry close|next-open]
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
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SKILL_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = SKILL_DIR / "state" / "runs"
OUTCOMES_CSV = SKILL_DIR / "state" / "outcomes.csv"

# Below this many resolved signals the strata are noise. Chosen to match the
# order of magnitude at which the sister scans' pockets became readable.
READABLE_N = 50

# How far the packet-date close may sit from the recorded entry before we go
# looking for an older bar. Wide enough to absorb a re-adjustment or a
# partial bar, tight enough that a coincidental match can't pull the anchor
# backwards past the signal.
ANCHOR_TOL = 0.02

INF = float("inf")


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    packet_date: str          # YYYY-MM-DD, the snapback run day
    ticker: str
    rank: int
    score: float
    rsi2: float | None
    dist_200dma: float | None
    listing_age: int
    freq_60d: int
    sector: str
    regime: str
    entry: float              # latest_close — the protocol's tranche-1 price
    stop: float | None        # signal_day_low — the invalidation PRICE
    target: float | None      # mr_target
    armed: bool
    spark_kinds: set[str] = field(default_factory=set)
    ignited: bool = False
    quiet: bool = False
    down_streak: int | None = None
    ret_5d: float | None = None
    down_gaps: int | None = None
    vol_ratio: float | None = None
    day_of_packet: int = 1    # consecutive snapback packets this keg has been in

    @property
    def spark_kind(self) -> str:
        """One dominant label per signal.

        Own earnings dominates a co-occurring sector verdict: when the keg
        reports inside the window, its own print sets the price regardless of
        what the sector bellwether said. The 'sector verdict is the better
        structure' claim is tested separately by the cross rows below, which
        do not collapse the overlap."""
        if "own_earnings" in self.spark_kinds:
            return "own_earnings"
        if "sector_verdict" in self.spark_kinds:
            return "sector_verdict"
        if "macro" in self.spark_kinds:
            return "macro"
        return "unarmed"


def load_signals(runs_dir: Path) -> tuple[list[Signal], list[str]]:
    packets = sorted(runs_dir.glob("*.json"))
    signals: list[Signal] = []
    dates: list[str] = []
    for p in packets:
        try:
            pk = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"skipping unreadable packet {p.name}: {e}", file=sys.stderr)
            continue
        pdate = pk.get("today") or p.stem
        dates.append(pdate)
        regime = ((pk.get("regime") or {}).get("state")) or "?"
        for k in pk.get("kegs", []):
            entry = k.get("latest_close") or k.get("signal_close")
            if not entry:
                continue
            signals.append(Signal(
                packet_date=pdate,
                ticker=k["ticker"],
                rank=int(k.get("rank") or 0),
                score=float(k.get("score") or 0),
                rsi2=k.get("rsi2"),
                dist_200dma=k.get("dist_200dma_pct"),
                listing_age=int(k.get("listing_age_runs") or 0),
                freq_60d=int(k.get("freq_60d") or 0),
                sector=k.get("sector") or "?",
                regime=regime,
                entry=float(entry),
                stop=k.get("signal_day_low"),
                target=k.get("mr_target"),
                armed=bool(k.get("armed")),
                spark_kinds={s.get("type") for s in k.get("sparks", [])},
                ignited=bool(k.get("ignited")),
                quiet=bool(k.get("quiet_warning")),
                down_streak=k.get("down_streak"),
                ret_5d=k.get("ret_5d_pct"),
                down_gaps=k.get("down_gaps_5d"),
                vol_ratio=k.get("vol_ratio_5d_20d"),
            ))

    # Day-of-packet: consecutive packets a keg keeps appearing in. A keg that
    # rides the list for days is the same episode re-counted, not new
    # evidence; the '1st packet' row is the independent view.
    order = {d: i for i, d in enumerate(sorted(set(dates)))}
    per_ticker: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        per_ticker[s.ticker].append(s)
    for sigs in per_ticker.values():
        sigs.sort(key=lambda s: s.packet_date)
        prev = None
        run = 1
        for s in sigs:
            i = order[s.packet_date]
            run = run + 1 if (prev is not None and i - prev == 1) else 1
            s.day_of_packet = run
            prev = i
    return signals, sorted(set(dates))


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
NO_DATA_KEY = "_NO_DATA"


def fetch_prices(tickers: list[str], start: str, cache: Path,
                 refresh: bool) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    data: dict = {}
    no_data: set = set()
    if cache.exists() and not refresh:
        try:
            with open(cache, "rb") as f:
                data = pickle.load(f)
        except (OSError, pickle.UnpicklingError):
            data = {}
        # SPY rides in every fetch, so its bars date the whole cache on both
        # ends. The recent end catches a stale cache that would censor new
        # signals' windows; the early end catches a cache built for a later
        # --start, which would turn every earlier packet into `no_bars` —
        # silent censorship that only shows up as a smaller n.
        spy = data.get("SPY")
        last = spy.index.max() if spy is not None else None
        first = spy.index.min() if spy is not None else None
        if last is None or (pd.Timestamp.today().normalize() - last).days > 5:
            print(f"cache {cache} stale (last bar {last}); refetching",
                  file=sys.stderr)
            data = {}
        elif first > pd.Timestamp(start) + pd.Timedelta(days=7):
            print(f"cache {cache} starts {first.date()}, after the requested "
                  f"{start}; refetching", file=sys.stderr)
            data = {}
        no_data = data.get(NO_DATA_KEY, set())
    missing = [t for t in tickers if t not in data and t not in no_data]
    if not missing:
        return data

    CHUNK = 50
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i:i + CHUNK]
        print(f"fetching {i + 1}-{i + len(chunk)} of {len(missing)}...",
              file=sys.stderr)
        # SPY as a liveness probe: if it returns, the pipeline worked and an
        # empty chunk member is genuinely dead rather than a network blip.
        req = list(dict.fromkeys(chunk + ["SPY"]))
        raw = yf.download(req, start=start, interval="1d", auto_adjust=True,
                          group_by="ticker", progress=False, threads=True)
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
    try:
        with open(cache, "wb") as f:
            pickle.dump(data, f)
    except OSError as e:
        print(f"cache write failed ({e}); continuing", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass
class Outcome:
    sig: Signal
    outcome: str              # WON / LOST / EXPIRED
    days: int
    result: float             # % vs entry, canonical fills
    gap_result: float         # % vs entry, gap-aware fills
    ambiguous: bool           # target AND stop both touched the same day
    fwd_ret: float | None     # close-to-close over the window, no exits
    mae: float | None         # worst intraday drawdown vs entry, in %


def resolve_signal(sig: Signal, bars: pd.DataFrame, window: int,
                   entry_mode: str = "close") -> tuple[Outcome | None, str]:
    """Returns (outcome, status): resolved / open / no_bars / unit_mismatch /
    gap_skip / no_levels.

    entry_mode "close" is the protocol as written (tranche 1 at the packet's
    latest_close). "next-open" is honest execution: the packet is built after
    the close, so the earliest real fill is the next session's open; a keg
    that gaps past its target or through its invalidation before you can buy
    is untradable → gap_skip."""
    if sig.stop is None or sig.target is None:
        return None, "no_levels"
    closes = bars["Close"]
    anchor = pd.Timestamp(sig.packet_date)
    pos = closes.index.searchsorted(anchor, side="right") - 1
    if pos < 0:
        return None, "no_bars"

    # Re-anchor onto the (re-adjusted) series. Walk back only when the
    # packet-date bar plainly isn't the one recorded (a weekend, holiday, or
    # pre-close run legitimately records a 1-3 session older bar), and take
    # the FIRST bar inside tolerance rather than the globally closest.
    # Taking the closest silently anchors a low-priced keg days early
    # whenever an older close happens to sit nearer the recorded entry, and
    # a window that opens before the signal grades the setup on its own
    # decline — contamination that reads as a suspiciously good result.
    if abs(float(closes.iloc[pos]) / sig.entry - 1) > ANCHOR_TOL:
        for back in (1, 2, 3):
            if pos - back < 0:
                break
            if abs(float(closes.iloc[pos - back]) / sig.entry - 1) <= ANCHOR_TOL:
                pos -= back
                break
    if abs(float(closes.iloc[pos]) / sig.entry - 1) > 0.05:
        return None, "unit_mismatch"
    ratio = float(closes.iloc[pos]) / sig.entry
    target = sig.target * ratio
    stop = sig.stop * ratio

    post = bars.iloc[pos + 1:pos + 1 + window]
    if len(post) == 0:
        return None, "open"

    if entry_mode == "next-open":
        entry = float(post["Open"].iloc[0])
        if not (entry > 0):
            return None, "no_bars"
        if entry >= target or entry <= stop:
            return None, "gap_skip"
    else:
        entry = float(closes.iloc[pos])

    fwd = (float(post["Close"].iloc[window - 1]) / entry - 1) * 100 \
        if len(post) >= window else None
    # Maximum ADVERSE excursion: a trade that never traded below entry has
    # zero adverse excursion, not a positive one. Leaving the raw ratio here
    # lets never-underwater trades average against genuinely painful ones and
    # understate the heat the protocol asks you to sit through.
    mae = min(0.0, (float(post["Low"].min()) / entry - 1) * 100)

    for d, (_, row) in enumerate(post.iterrows(), 1):
        hit_t = float(row["High"]) >= target
        hit_s = float(row["Low"]) <= stop
        if hit_t:
            # Limit sell: a gap-up open fills better than the limit.
            fill = max(target, float(row["Open"]))
            return Outcome(sig, "WON", d, (target / entry - 1) * 100,
                           (fill / entry - 1) * 100, hit_s, fwd, mae), "resolved"
        if hit_s:
            # Hard exit: a gap-down open fills worse than the invalidation.
            fill = min(stop, float(row["Open"]))
            return Outcome(sig, "LOST", d, (stop / entry - 1) * 100,
                           (fill / entry - 1) * 100, False, fwd, mae), "resolved"

    if len(post) < window:
        return None, "open"
    r = (float(post["Close"].iloc[window - 1]) / entry - 1) * 100
    return Outcome(sig, "EXPIRED", window, r, r, False, fwd, mae), "resolved"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def fmt(v, suffix="", nd=1):
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def agg(outs: list[Outcome]) -> dict:
    won = [o for o in outs if o.outcome == "WON"]
    lost = [o for o in outs if o.outcome == "LOST"]
    dec = len(won) + len(lost)
    return {
        "n": len(outs),
        "dec": dec,
        "win": 100 * len(won) / dec if dec else None,
        "days_to_t": mean([o.days for o in won]),
        "exp": mean([o.result for o in outs]),
        "gap_exp": mean([o.gap_result for o in outs]),
        "fwd": mean([o.fwd_ret for o in outs]),
        "mae": mean([o.mae for o in outs]),
        # Payoff geometry at signal time. Without these columns a win rate is
        # unreadable: an ignited keg's target sits ~1.5% above an entry that
        # already ran, while its invalidation sits ~14% below, so it wins
        # nearly every time and still loses money over a full cycle.
        "tgt_dist": mean([(o.sig.target / o.sig.entry - 1) * 100
                          for o in outs]),
        "stop_dist": mean([(o.sig.stop / o.sig.entry - 1) * 100
                           for o in outs]),
        # Median of the PER-SIGNAL ratios, not the ratio of the two means:
        # (T=1%, S=-10%) and (T=10%, S=-1%) average to a tidy 1.0 while the
        # signals themselves are 0.1 and 10. The typical trade's odds are the
        # question, so the typical trade's ratio is the answer.
        "rr": median([abs((o.sig.target / o.sig.entry - 1)
                          / (o.sig.stop / o.sig.entry - 1))
                      for o in outs if o.sig.stop != o.sig.entry]),
    }


def strata_table(title: str, groups: list[tuple[str, list[Outcome]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Stratum | n | dec | Win% | T% | S% | R:R | d→T | Exp% | "
             "GapExp% | NoExit% | MAE% |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    any_row = False
    for name, outs in groups:
        if not outs:
            continue
        any_row = True
        a = agg(outs)
        # A row thin enough to be a single lucky name gets marked, so it can
        # never be quoted as a finding by accident.
        thin = " ⚠︎" if a["n"] < 10 else ""
        lines.append(
            f"| {name}{thin} | {a['n']} | {a['dec']} | {fmt(a['win'], '%', 0)} | "
            f"{fmt(a['tgt_dist'], '%', 1)} | {fmt(a['stop_dist'], '%', 1)} | "
            f"{fmt(a['rr'], '', 2)} | "
            f"{fmt(a['days_to_t'])} | {fmt(a['exp'], '%', 2)} | "
            f"{fmt(a['gap_exp'], '%', 2)} | {fmt(a['fwd'], '%', 2)} | "
            f"{fmt(a['mae'], '%', 2)} |")
    if not any_row:
        lines.append("| _(no signals in any stratum)_ | | | | | | | | | | | |")
    return "\n".join(lines)


def bucket(outs, key, bounds):
    """A missing attribute is excluded from every stratum, never coerced.
    Mapping None onto a sentinel would silently file "we didn't measure the
    tape" under whichever bucket the sentinel lands in."""
    def pairs(lo, hi):
        return [o for o in outs
                if (v := key(o.sig)) is not None and lo <= v < hi]
    return [(name, pairs(lo, hi)) for name, lo, hi in bounds]


def write_outcomes_csv(outs: list[Outcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["packet_date", "ticker", "armed", "spark_kind", "ignited",
                    "quiet", "score", "listing_age_runs", "day_of_packet",
                    "sector", "regime", "entry", "stop", "target", "outcome",
                    "days", "result_pct", "gap_result_pct", "fwd_ret_pct",
                    "mae_pct"])
        for o in sorted(outs, key=lambda o: (o.sig.packet_date, o.sig.ticker)):
            s = o.sig
            w.writerow([s.packet_date, s.ticker, int(s.armed), s.spark_kind,
                        int(s.ignited), int(s.quiet), s.score, s.listing_age,
                        s.day_of_packet, s.sector, s.regime,
                        round(s.entry, 2), round(s.stop, 2),
                        round(s.target, 2), o.outcome, o.days,
                        round(o.result, 2), round(o.gap_result, 2),
                        None if o.fwd_ret is None else round(o.fwd_ret, 2),
                        None if o.mae is None else round(o.mae, 2)])


def sample_banner(n_resolved: int, signals: list[Signal],
                  run_days: list[str]) -> str:
    """Say plainly whether the tables can be read yet, and if not, when.

    The estimate counts the unit READABLE_N is expressed in: resolved
    signals. Signals already emitted but undecided (`pending`) close the gap
    on their own — every signal eventually hits its target, hits the
    invalidation, or expires — so they need no new run days. Only the
    remainder does, arriving at the observed signals-per-run-day rate.
    Estimating off the ARMED rate instead (this function's first version)
    mixed units: unarmed kegs land in the ledger too, so the gap it claimed
    to close was never the gap being measured, and it read optimistic."""
    if n_resolved >= READABLE_N:
        return ""
    pending = len(signals) - n_resolved
    still_short = READABLE_N - n_resolved - pending
    per_day = len(signals) / len(run_days) if run_days else 0
    if still_short <= 0:
        when = (f" The {pending} signals already in flight cover the gap — "
                f"re-run once their windows elapse; no new run days needed.")
    elif per_day > 0:
        days = still_short / per_day
        weeks = days / 5
        # Only translate to weeks once that's the more legible unit — a
        # two-day gap rendered as "~0 weeks" reads like a rounding bug.
        in_weeks = (f" (~{weeks:.0f} week{'' if weeks < 1.5 else 's'})"
                    if weeks >= 1 else "")
        when = (f" {pending} are already in flight; at the observed "
                f"{per_day:.1f} signals per run day the remaining "
                f"{still_short:.0f} need roughly {days:.0f} more run "
                f"days{in_weeks}.")
    else:
        when = ""
    return (f"\n> ⚠︎ **SAMPLE TOO SMALL TO READ ({n_resolved} resolved, want "
            f"{READABLE_N}+).** Everything below is a plumbing check, not "
            f"evidence — single strata here are one or two names deep and "
            f"will swing wildly with the next run.{when} Do not quote these "
            f"rows into SKILL.md until this banner clears.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, default=RUNS_DIR)
    ap.add_argument("--window", type=int, default=5,
                    help="trading days for the snapback to play out "
                         "(default 5; the spark window is 3, so 3/5/10 "
                         "sensitivity is printed too)")
    ap.add_argument("--entry", choices=["close", "next-open"], default="close",
                    help="close = the protocol as written (tranche 1 at the "
                         "packet's latest_close); next-open = honest "
                         "execution at the next session's open")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "snapback_prices.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--start", default=None,
                    help="price download start (default: 10 calendar days "
                         "before the earliest packet, so the ledger keeps "
                         "working as the packet history grows)")
    ap.add_argument("--csv", type=Path, default=OUTCOMES_CSV)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    signals, run_days = load_signals(args.runs)
    if not signals:
        print(f"no packets in {args.runs} — nothing to grade", file=sys.stderr)
        raise SystemExit(1)
    tickers = sorted({s.ticker for s in signals})
    print(f"packets: {len(run_days)} run days, {len(signals)} keg-signals, "
          f"{len(tickers)} tickers", file=sys.stderr)

    # A signal's window opens the session AFTER its packet, but re-anchoring
    # may walk back a few bars, so the download starts before the earliest
    # packet rather than on it.
    start = args.start or (pd.Timestamp(run_days[0])
                           - pd.Timedelta(days=10)).date().isoformat()

    # armed comes off the packet; spark_kind is recomputed from sparks[].
    # They can only disagree if the arming rules changed under an old packet
    # — worth a line on stderr rather than a contradictory CSV row.
    odd = [s for s in signals if s.armed != (s.spark_kind != "unarmed")]
    if odd:
        print(f"⚠︎ {len(odd)} signal(s) disagree between the packet's `armed` "
              f"flag and their sparks[]: "
              f"{', '.join(f'{s.ticker}@{s.packet_date}' for s in odd[:5])}"
              f"{' …' if len(odd) > 5 else ''}", file=sys.stderr)

    prices = fetch_prices(tickers + ["SPY"], start, args.cache,
                          args.refresh_prices)
    missing = sorted(t for t in tickers if t not in prices)

    outcomes: list[Outcome] = []
    counts: dict[str, int] = defaultdict(int)
    mismatch: list[str] = []
    for s in signals:
        if s.ticker not in prices:
            counts["no_price_data"] += 1
            continue
        o, status = resolve_signal(s, prices[s.ticker], args.window, args.entry)
        if status == "resolved":
            outcomes.append(o)
        else:
            counts[status] += 1
            if status == "unit_mismatch":
                mismatch.append(f"{s.ticker}@{s.packet_date}")

    a = agg(outcomes)
    print(f"# snapback outcome ledger — packets {run_days[0]} → "
          f"{run_days[-1]} — window {args.window}d — entry **{args.entry}**\n")
    print(sample_banner(len(outcomes), signals, run_days))
    print(f"**Signals**: {len(outcomes)} resolved of {len(signals)} "
          f"({counts['open']} still open, {counts['no_bars']} no usable bars, "
          f"{counts['no_levels']} missing stop/target, "
          f"{counts['no_price_data']} on tickers with no price data"
          f"{': ' + ', '.join(missing) if missing else ''})")
    if args.entry == "next-open":
        print(f"**Skipped at entry** (gapped past target or through "
              f"invalidation before a fill was possible): {counts['gap_skip']}")
    if mismatch:
        print(f"**Dropped for unit mismatch** (n={len(mismatch)}): "
              f"{', '.join(mismatch)}")
    won = [o.result for o in outcomes if o.outcome == "WON"]
    lost = [o.result for o in outcomes if o.outcome == "LOST"]
    exp = [o.result for o in outcomes if o.outcome == "EXPIRED"]
    print(f"**Decisive** (target or invalidation hit): {a['dec']} — win rate "
          f"{fmt(a['win'], '%', 0)}, avg {fmt(a['days_to_t'])} days to target")
    print(f"**Payoff geometry**: avg win {fmt(mean(won), '%', 2)} "
          f"(n={len(won)}) · avg loss {fmt(mean(lost), '%', 2)} "
          f"(n={len(lost)}) · avg expired drift {fmt(mean(exp), '%', 2)} "
          f"(n={len(exp)})")
    print(f"**Expectancy per signal**: {fmt(a['exp'], '%', 2)} · gap-aware "
          f"{fmt(a['gap_exp'], '%', 2)} · no-exit close-to-close "
          f"{fmt(a['fwd'], '%', 2)} · avg worst drawdown before exit "
          f"{fmt(a['mae'], '%', 2)}")

    # --- claim 1: does a dated catalyst pay? -------------------------------
    print(strata_table("Claim 1 — armed vs unarmed (the skill's whole premise)", [
        ("⭐️ armed (≥1 scoped spark)", [o for o in outcomes if o.sig.armed]),
        ("👀 unarmed (no spark in window)",
         [o for o in outcomes if not o.sig.armed]),
    ]))

    # --- claim 2: which spark structure? -----------------------------------
    def has(o, kind):
        return kind in o.sig.spark_kinds

    print(strata_table("Claim 2 — by spark type (SKILL.md: a sector verdict "
                       "beats the keg's own coin-flip print)", [
        ("own earnings (coin flip)",
         [o for o in outcomes if o.sig.spark_kind == "own_earnings"]),
        ("sector verdict (someone else re-prices it)",
         [o for o in outcomes if o.sig.spark_kind == "sector_verdict"]),
        ("macro (FOMC/CPI-class)",
         [o for o in outcomes if o.sig.spark_kind == "macro"]),
        ("— cross: sector verdict, no own print —",
         [o for o in outcomes if has(o, "sector_verdict")
          and not has(o, "own_earnings")]),
        ("— cross: own print, no sector verdict —",
         [o for o in outcomes if has(o, "own_earnings")
          and not has(o, "sector_verdict")]),
        ("— cross: both —", [o for o in outcomes if has(o, "own_earnings")
                             and has(o, "sector_verdict")]),
    ]))

    # --- claim 3: what does chasing cost? ----------------------------------
    print(strata_table("Claim 3 — ignited chase-guard (entry already ≥ +7% "
                       "since signal)", [
        ("🔥 ignited (the chase)", [o for o in outcomes if o.sig.ignited]),
        ("not ignited (still coiled)",
         [o for o in outcomes if not o.sig.ignited]),
        ("🔥 ignited & armed", [o for o in outcomes
                                if o.sig.ignited and o.sig.armed]),
    ]))
    print("\n_Read the T% and R:R columns before this table's Win%. An "
          "ignited keg is bought after it already ran, so `mr_target` sits "
          "just overhead while the signal-day low sits far below — a "
          "geometry that manufactures a high win rate out of a bad payoff. "
          "A win rate here that beats the coiled row is not evidence the "
          "chase-guard is wrong._")

    print(strata_table("Quiet-tape warning (backtest says a quiet oversold "
                       "is a knife catch)", [
        ("😴 quiet", [o for o in outcomes if o.sig.quiet]),
        ("panic tape", [o for o in outcomes if not o.sig.quiet]),
    ]))

    # --- the subset the skill would actually trade -------------------------
    def compliant(o):
        return o.sig.armed and not o.sig.ignited and not o.sig.quiet

    print(strata_table("Protocol-compliant subset — the skill's real track "
                       "record (armed & not ignited & not quiet)", [
        ("✅ compliant (would be a brief)",
         [o for o in outcomes if compliant(o)]),
        ("✗ excluded by the rules", [o for o in outcomes if not compliant(o)]),
    ]))

    print(strata_table("By Reversion Score at signal", bucket(
        outcomes, lambda s: s.score,
        [("40–55", 40, 55), ("55–70", 55, 70), ("≥70", 70, INF)])))

    print(strata_table("By day-of-packet (consecutive packets this keg rode)",
                       bucket(outcomes, lambda s: s.day_of_packet,
                              [("1st (new keg)", 1, 2), ("2nd", 2, 3),
                               ("3rd+ (stale keg)", 3, INF)])))

    print(strata_table("Seller mechanism — down gaps in the 5d into signal",
                       bucket(outcomes, lambda s: s.down_gaps,
                              [("0 gaps (drift)", 0, 1), ("1 gap", 1, 2),
                               ("2+ gaps (forced)", 2, INF)])))

    print(strata_table("Seller mechanism — 5d volume vs 20d", bucket(
        outcomes, lambda s: s.vol_ratio,
        [("<1.0 quiet", -INF, 1.0), ("1.0–1.5", 1.0, 1.5),
         ("≥1.5 heavy", 1.5, INF)])))

    print(strata_table("Seller mechanism — consecutive down closes", bucket(
        outcomes, lambda s: s.down_streak,
        [("0–1", 0, 2), ("2–3", 2, 4), ("4+", 4, INF)])))

    by_regime: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_regime[o.sig.regime].append(o)
    print(strata_table("By regime at signal (the sizing gate)",
                       sorted(by_regime.items(), key=lambda kv: -len(kv[1]))))

    # "?" is a sectors.json gap on the build side, not a sector. Labelled and
    # sunk to the bottom so it can't be read as one alongside Technology.
    by_sector: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_sector[o.sig.sector].append(o)
    unknown = by_sector.pop("?", [])
    sector_rows = sorted(by_sector.items(), key=lambda kv: -len(kv[1]))
    if unknown:
        sector_rows.append(("(sector missing — a build-side gap, not a "
                            "sector)", unknown))
    print(strata_table("By sector", sector_rows))

    print("\n### Window sensitivity (aggregate)\n")
    print("| Window | n | dec | Win% | d→T | Exp% | GapExp% | NoExit% | MAE% |")
    print("|---|---|---|---|---|---|---|---|---|")
    for w in (3, 5, 10):
        outs_w = []
        for s in signals:
            if s.ticker not in prices:
                continue
            o, status = resolve_signal(s, prices[s.ticker], w, args.entry)
            if status == "resolved":
                outs_w.append(o)
        aw = agg(outs_w)
        label = f"{w}d" + (" (spark window)" if w == 3 else "")
        print(f"| {label} | {aw['n']} | {aw['dec']} | {fmt(aw['win'], '%', 0)} | "
              f"{fmt(aw['days_to_t'])} | {fmt(aw['exp'], '%', 2)} | "
              f"{fmt(aw['gap_exp'], '%', 2)} | {fmt(aw['fwd'], '%', 2)} | "
              f"{fmt(aw['mae'], '%', 2)} |")

    # The protocol trails past target 1 rather than selling all of it, so
    # every Exp% here truncates the right tail this skill exists to catch.
    # NoExit% is the honest read of that tail — and it stays empty until a
    # signal's full window has elapsed, which is why it lags the other
    # columns on a young ledger.
    n_fwd = sum(1 for o in outcomes if o.fwd_ret is not None)
    print(f"\n_Right-tail caveat: target-1 fills close the position here, but "
          f"SKILL.md trails beyond it — so Exp% is a floor, not the strategy's "
          f"return. NoExit% is the untruncated read and needs a fully elapsed "
          f"window: {n_fwd} of {len(outcomes)} resolved signals have one so "
          f"far._")

    if not args.no_save:
        write_outcomes_csv(outcomes, args.csv)
        print(f"\n_Wrote {len(outcomes)} resolved signals to {args.csv}._")

    print("\n_Columns: n = resolved; dec = decisive (target or invalidation "
          "hit); Win% = WON/dec; T% = mean distance from entry to target; "
          "S% = mean distance from entry to the invalidation price; R:R = "
          "the MEDIAN per-signal |T%/S%| (not T%/S% of the two means), the "
          "payoff geometry a win rate must be read against; "
          "d→T = avg days to target among wins; Exp% = mean return per signal "
          "with canonical fills; GapExp% = same with gap-aware fills (limit "
          "at max(target, open), invalidation at min(stop, open)); NoExit% = "
          "close-to-close over the full window with no exits at all; MAE% = "
          "mean worst intraday drawdown vs entry before the trade resolved — "
          "how much heat the protocol asks you to sit through. Rows marked ⚠︎ "
          "have n < 10 and are anecdotes. Consecutive packets of one keg are "
          "correlated; the day-of-packet '1st' row is the independent view._")


if __name__ == "__main__":
    main()
