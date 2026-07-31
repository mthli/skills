#!/usr/bin/env python3
"""Signal-quality report + outcome grading for regime-scan history.

The gate is the highest-leverage layer of the pipeline — conviction-funnel
sizes positions off its state and premarket-brief frames every morning
with it — yet nothing ever measured the signal itself. This script grades
two different things, honestly separated because they mature at different
speeds:

  - SIGNAL QUALITY (meaningful immediately, even on weeks of data) — is
    the state readable at all? State-spell durations and the share of
    1-day flips quantify threshold chatter (the score oscillating around
    a state boundary flips the label without the tape changing); an
    N-day confirmation rule is replayed to show how many transitions
    survive and at what detection lag. Flag base rates expose chronic
    flags: a divergence flag that is on most days carries no contrast —
    it can't be tested, and downstream readers habituate to it.

  - OUTCOMES (matures over quarters) — SPY forward returns 5/10/20
    sessions after each reading, stratified by state, score, flag count,
    and state-transition days. Daily readings overlap heavily inside a
    forward window, so every stratum prints indep≈n/w (the approximate
    count of independent windows) and is marked thin until that reaches
    8. Until several regime turns are on record, read this section as
    "the pipe is plumbed", not as conclusions.

Conventions: rows are keyed on run_id (the ET session whose close the
scan read; same-day re-runs overwrite, last row wins). "RISK-OFF
(internals)" is folded into RISK-OFF for strata (the variant is counted
in the headline). A transition day is the first run-day showing a new
state; under confirmation-N it's the run-day on which the new state has
persisted N run-days (detection lag N−1). Forward windows are counted in
SPY trading sessions from the reading's session close.

Run:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
    python backtest_outcomes.py [--confirm-days 2] [--refresh-prices]
"""

from __future__ import annotations

import argparse
import csv
import pickle
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SKILL_DIR = Path(__file__).resolve().parent.parent
HISTORY_CSV = SKILL_DIR / "state" / "history.csv"

FWD_WINDOWS = (5, 10, 20)
THIN_INDEP = 8          # below this many independent windows a stratum is thin
CHRONIC_SHARE = 0.5     # a flag on more than this share of days is chronic


def ts_of(run_day: str) -> pd.Timestamp:
    return pd.Timestamp(f"{run_day[:4]}-{run_day[4:6]}-{run_day[6:]}")


# ---------------------------------------------------------------- history

@dataclass
class Reading:
    run_day: str            # YYYYMMDD, the ET session whose close was read
    label: str              # raw state label, e.g. "RISK-OFF (internals)"
    state: str              # base state: RISK-ON / CAUTION / RISK-OFF
    score: int
    flags: list             # individual flag strings
    fwd: dict = None        # w -> SPY % over the next w sessions

    def __post_init__(self):
        if self.fwd is None:
            self.fwd = {}


def flag_type(flag: str) -> str:
    """'Defensive rotation: ... by +4.4pp' → 'Defensive rotation';
    'VIX 5-day spike +24%' → 'VIX 5-day spike'."""
    if ":" in flag:
        return flag.split(":", 1)[0].strip()
    return re.sub(r"\s+[+\-−]?\d[\d.,]*%?$", "", flag).strip()


def load_history(path: Path) -> list[Reading]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = {r["run_id"]: r for r in csv.DictReader(f) if r.get("state")}
    readings = []
    for run_id in sorted(rows):
        r = rows[run_id]
        label = r["state"].strip()
        raw_flags = (r.get("flags") or "").strip()
        readings.append(Reading(
            run_day=run_id,
            label=label,
            state=label.split(" (")[0],
            score=int(float(r["score"])),
            flags=[fl.strip() for fl in raw_flags.split(" ; ") if fl.strip()],
        ))
    return readings


# ---------------------------------------------------------------- signal

def spells(states: list[str]) -> list[tuple[str, int]]:
    """Consecutive-run-day state spells as (state, length)."""
    out: list[tuple[str, int]] = []
    for s in states:
        if out and out[-1][0] == s:
            out[-1] = (s, out[-1][1] + 1)
        else:
            out.append((s, 1))
    return out


def confirmed_states(states: list[str], n: int) -> list[str]:
    """Effective state under an N-day confirmation rule: the label flips
    only once a new state has persisted n consecutive run-days; until
    then the previous confirmed state stands. The first reading seeds the
    confirmed state (history starts somewhere)."""
    if n <= 1:
        return list(states)
    out = [states[0]]
    streak_state, streak = states[0], 1
    for s in states[1:]:
        if s == streak_state:
            streak += 1
        else:
            streak_state, streak = s, 1
        if streak_state != out[-1] and streak >= n:
            out.append(streak_state)
        else:
            out.append(out[-1])
    return out


def transitions(states: list[str]) -> int:
    return sum(a != b for a, b in zip(states, states[1:]))


# ---------------------------------------------------------------- prices

def fetch_spy(start: str, cache: Path, refresh: bool) -> pd.DataFrame:
    import yfinance as yf

    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            spy = pickle.load(f)
        fresh = (len(spy)
                 and (pd.Timestamp.today().normalize()
                      - spy.index.max()).days <= 5
                 and spy.index.min() <= pd.Timestamp(start)
                 + pd.Timedelta(days=7))
        if fresh:
            return spy
        print(f"cache {cache} is stale; refetching", file=sys.stderr)
    for attempt in range(3):
        spy = yf.download("SPY", start=start, interval="1d",
                          auto_adjust=True, progress=False)
        if isinstance(spy.columns, pd.MultiIndex):
            spy = spy.droplevel(axis=1, level=1)
        spy = spy.dropna(subset=["Close"])
        if len(spy):
            with open(cache, "wb") as f:
                pickle.dump(spy, f)
            return spy
        time.sleep(2.0 * (attempt + 1))
    sys.exit("could not download SPY")


def pos_of(bars: pd.DataFrame, date: pd.Timestamp) -> int | None:
    idx = bars.index
    p = idx.searchsorted(date, side="right") - 1
    if p < 0 or (date - idx[p]).days > 5:
        return None
    return int(p)


def attach_forward(readings: list[Reading], spy: pd.DataFrame) -> None:
    closes = spy["Close"]
    for r in readings:
        p = pos_of(spy, ts_of(r.run_day))
        if p is None:
            continue
        base = float(closes.iloc[p])
        for w in FWD_WINDOWS:
            if p + w < len(closes):
                r.fwd[w] = (float(closes.iloc[p + w]) / base - 1) * 100


# ---------------------------------------------------------------- report

def fmt(v, suffix="", nd=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else \
        f"{v:.{nd}f}{suffix}"


def mean_of(vals) -> float | None:
    xs = [v for v in vals if v is not None]
    return float(np.mean(xs)) if xs else None


def fwd_row(name: str, rs: list[Reading]) -> str:
    f20 = [r.fwd.get(20) for r in rs if r.fwd.get(20) is not None]
    win = 100 * sum(v > 0 for v in f20) / len(f20) if f20 else None
    indep = len(rs) / max(FWD_WINDOWS)
    thin = " ⚠︎thin" if indep < THIN_INDEP else ""
    return (f"| {name} | {len(rs)} | ~{indep:.0f}{thin} | "
            f"{fmt(mean_of([r.fwd.get(5) for r in rs]), '%', 2)} | "
            f"{fmt(mean_of([r.fwd.get(10) for r in rs]), '%', 2)} | "
            f"{fmt(mean_of(f20), '%', 2)} | {fmt(win, '%', 0)} |")


def fwd_table(title: str, groups: list[tuple[str, list[Reading]]]) -> str:
    lines = [f"\n### {title}\n",
             "| Stratum | n | indep≈ | SPY+5d | SPY+10d | SPY+20d | Win+20 |",
             "|---|---|---|---|---|---|---|"]
    lines += [fwd_row(name, rs) for name, rs in groups if rs]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, default=HISTORY_CSV)
    ap.add_argument("--confirm-days", type=int, default=2,
                    help="N for the confirmed-state transition strata "
                         "(the stability section always shows N=1..3). The "
                         "default must stay in lockstep with scan.py's "
                         "STATE_CONFIRM_DAYS and build_packet.py's confirm "
                         "loop in premarket-brief")
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir())
                    / "regime_grade_spy.pkl")
    ap.add_argument("--refresh-prices", action="store_true")
    args = ap.parse_args()

    readings = load_history(args.history)
    if not readings:
        sys.exit("history is empty — run the scan first")
    states = [r.state for r in readings]
    days = [r.run_day for r in readings]

    spy = fetch_spy(str(ts_of(days[0]).date() - pd.Timedelta(days=10)),
                    args.cache, args.refresh_prices)
    attach_forward(readings, spy)

    n = len(readings)
    mix = Counter(states)
    variants = Counter(r.label for r in readings if r.label != r.state)
    print(f"# regime signal report — {n} readings, {days[0]} → {days[-1]}\n")
    print(f"**State mix**: " + ", ".join(
        f"{s} {c} ({100 * c / n:.0f}%)" for s, c in mix.most_common())
        + (f"; variants: " + ", ".join(f"{v} ×{c}"
           for v, c in variants.items()) if variants else ""))
    print(f"**Score**: min {min(r.score for r in readings)}, max "
          f"{max(r.score for r in readings)}, mean "
          f"{np.mean([r.score for r in readings]):.1f}")

    # ---- signal stability
    sp = spells(states)
    one_day = sum(1 for _, ln in sp if ln == 1)
    print("\n### Signal stability (state chatter)\n")
    print(f"- {len(sp)} state spells over {n} run-days — mean length "
          f"{np.mean([ln for _, ln in sp]):.1f}, median "
          f"{np.median([ln for _, ln in sp]):.0f}; **{one_day} spells "
          f"({100 * one_day / len(sp):.0f}%) lasted a single run-day** "
          f"(threshold chatter: the score crossing a state boundary "
          f"flips the label without the tape changing)")
    print("\n| Rule | Transitions | Whipsaws removed | Detection lag |")
    print("|---|---|---|---|")
    raw_t = transitions(states)
    for nconf in (1, 2, 3):
        t = transitions(confirmed_states(states, nconf))
        print(f"| {'raw' if nconf == 1 else f'{nconf}-day confirm'} | {t} | "
              f"{'—' if nconf == 1 else raw_t - t} | "
              f"{nconf - 1} run-day{'s' if nconf > 2 else ''} |")
    print("\n_Downstream readers (conviction-funnel sizing, premarket "
          "framing) consume the daily label; every whipsaw above reached "
          "them as a real regime change._")

    # ---- flag base rates
    print("\n### Flag base rates\n")
    per_type_days: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(readings):
        for fl in r.flags:
            per_type_days[flag_type(fl)].append(i)
    no_flag = sum(1 for r in readings if not r.flags)
    print(f"Readings with no flag at all: **{no_flag} of {n}** — the "
          f"contrast group every flag test needs.\n")
    print("| Flag | Days on | Share | Longest streak | Verdict |")
    print("|---|---|---|---|---|")
    for ftype, idxs in sorted(per_type_days.items(),
                              key=lambda kv: -len(kv[1])):
        streaks = spells(["on" if i in set(idxs) else "off"
                          for i in range(n)])
        longest = max((ln for s, ln in streaks if s == "on"), default=0)
        share = len(idxs) / n
        verdict = ("⚠️ chronic — always-on ⇒ no contrast, untestable, "
                   "habituating" if share > CHRONIC_SHARE else "episodic")
        print(f"| {ftype} | {len(idxs)} | {share:.0%} | {longest} | "
              f"{verdict} |")

    # ---- forward returns
    print("\n## Forward returns (grading framework — sample still thin)\n")
    total_indep = n / max(FWD_WINDOWS)
    if total_indep < THIN_INDEP:
        print(f"⚠️ The whole section holds ≈{total_indep:.0f} independent "
              f"{max(FWD_WINDOWS)}-session windows (need ~{THIN_INDEP}). "
              f"The plumbing below is locked so conclusions accrue with "
              f"the log; nothing here is a conclusion yet.")

    print(fwd_table("By state", [
        (s, [r for r in readings if r.state == s])
        for s in ("RISK-ON", "CAUTION", "RISK-OFF")
    ]))
    print(fwd_table("By score", [
        ("≤4 weak", [r for r in readings if r.score <= 4]),
        ("5–6 middling", [r for r in readings if 5 <= r.score <= 6]),
        ("≥7 strong", [r for r in readings if r.score >= 7]),
    ]))
    print(fwd_table("By flag count", [
        ("0 flags", [r for r in readings if not r.flags]),
        ("1 flag", [r for r in readings if len(r.flags) == 1]),
        ("2+ flags", [r for r in readings if len(r.flags) >= 2]),
    ]))
    # Per-type strata only where both sides of the contrast exist.
    contrast = []
    for ftype, idxs in sorted(per_type_days.items()):
        on = set(idxs)
        if len(on) >= 5 and n - len(on) >= 5:
            contrast.append((f"{ftype}: on", [readings[i] for i in on]))
            contrast.append((f"{ftype}: off",
                             [r for i, r in enumerate(readings)
                              if i not in on]))
    if contrast:
        print(fwd_table("By flag type (only types with ≥5 days on AND "
                        "off — chronic flags can't be tested)", contrast))

    conf = confirmed_states(states, args.confirm_days)
    raw_td = [readings[i] for i in range(1, n)
              if states[i] != states[i - 1]]
    conf_td = [readings[i] for i in range(1, n) if conf[i] != conf[i - 1]]
    print(fwd_table(
        f"Transition days (raw vs {args.confirm_days}-day confirmed)", [
            ("raw transition day", raw_td),
            (f"confirmed transition day", conf_td),
        ]))

    print(f"\n_Forward returns are SPY close-to-close over the next w "
          f"sessions from each reading's session close. Consecutive "
          f"daily readings overlap inside a window, so indep≈ = n/"
          f"{max(FWD_WINDOWS)} approximates the independent sample; "
          f"strata below {THIN_INDEP} are marked thin. The by-state and "
          f"by-flag tables inherit whatever regime mix the log happens "
          f"to contain — judge them only once every state has lived "
          f"through at least one full spell and the thin markers clear. "
          f"Re-run quarterly with the sister scans' backtests._")


if __name__ == "__main__":
    main()
