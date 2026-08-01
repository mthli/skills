#!/usr/bin/env python3
"""Render state/history.csv + state/outcomes.csv into a self-contained
static HTML dashboard.

Reads the base-breakout run history, the per-episode outcomes ledger and
the sector cache and writes a single HTML file (no external assets, no
network) with:

  - a KPI row (span, today's watchlist and how loaded it is, today's ⭐
    pocket, episode trigger rate, ⭐ pocket trade expectancy vs its
    backtest reference, the baseline it has to beat)
  - an approach-to-pivot chart: one line per episode, y = distance to the
    pivot, so the zero line IS the buy trigger and a line reaching it is a
    breakout (filterable: ⭐ pocket / triggered / all)
  - a date × ticker maturity grid, cell color = that day's Sig tier
    (forming → coiled → imminent → breakout), dot = ⭐ pocket day, row end =
    that name's realized trade expectancy, with a min-days filter
  - a cohort chart: each day's watchlist stacked by Sig tier with the ⭐
    pocket count overlaid, answering "is the list loaded, and does it hold
    anything validated"
  - a ⭐-pocket-vs-rest running trade-expectancy chart against the
    backtest's in-sample reference values
  - a per-sector realized-result panel: one bar per sector (avg %/completed
    trade) with its 95% interval drawn above it, read against a dashed
    all-trades average — a sector whose interval reaches that line is not
    distinguishable from the board and is drawn back to 42% opacity
  - a per-ticker summary table (the no-hover fallback for every value)
  - an English / 简体中文 / 繁體中文 / 日本語 / 한국어 language menu (top-right;
    choice kept in localStorage, first visit follows the browser language)
  - SEO meta tags (description + Open Graph + Twitter card) filled from the
    actual data span at render time

The chart semantics deliberately DIFFER from the sibling renderers.
momentum-scan tells a persistence story (long streak = durable winner), so
it draws rank trajectories; mean-reversion-scan tells an event + outcome
story (every signal resolves within days), so it draws outcomes. This scan
tells a MATURATION story — a base tightens for weeks, then either clears
its pivot or rots — so the spine here is distance-to-pivot over time, the
one axis in the family with an absolute meaning (0 = the trigger price).

The unit of account is the EPISODE (a consecutive run of listed days), not
the run-day: the canonical trade is a buy-stop that lives from first
listing to dropout, so that is what outcomes.csv keys on and what this page
scores.

Usage:
    python scripts/render_history_html.py [--days 60] [--out state/history.html]
"""

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# Mirrored from scan.py's constant of the same name (source of truth;
# test_render_html.py has a drift-guard test asserting equality).
# Duplicated numerically because this script deliberately stays
# stdlib-only while scan.py imports yfinance at module level.
VALIDATED_BASE_WEEKS = 20.0
# Sig tiers in ascending order of "how close to firing" — the ordinal ramp
# for the grid, the stack order for the cohort chart, and the sort order
# for the roster's status column. Mirrors scan.py's classifier. These glyphs
# are the CSV's vocabulary only; the page names the tiers in words and
# carries the ordinal in color, so it never prints them.
SIG_ORDER = ["📊", "⏳", "🔥", "🚀"]
# In-sample reference expectancies from the 2026-05→07 outcome backtest
# (references/backtest-findings.md #1 and #4), both on the stop-based trade
# (20-session horizon, 8% stop, buy-stop touch entry) — drawn as dashed
# reference lines so the pocket chart answers "is the validated edge still
# paying out-of-sample". Re-validate quarterly alongside the backtest
# re-run; they only mean anything against a ledger built with the same
# convention, which check_ledger_convention() enforces.
BACKTEST_POCKET_TRADE = 4.9
BACKTEST_BASELINE_TRADE = -0.8
LEDGER_CONVENTION = {"horizon": "20", "stop_pct": "8.0", "entry": "touch"}
# Roster/grid rows need a floor before "trade expectancy" means anything;
# one lucky episode is not a track record.
DEFAULT_MIN_DAYS = 3
# Sector panel: a sector needs this many completed trades to get its own
# bar; thinner ones fold into a counted "Other" rather than vanishing. Not a
# validated threshold — it is the point below which the 95% interval is
# wider than any difference the bar could show. Base breakouts resolve into
# far fewer trades than MR signals do, so this floor bites here.
MIN_SECTOR_N = 10


def load_history(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_outcomes(path: Path) -> dict[tuple[str, str], dict]:
    """(start_run_id, ticker) → ledger row. A missing file just means every
    episode renders as in-flight — warn, since the usual cause is a
    never-seeded ledger (backtest_outcomes.py --write-ledger)."""
    if not path.exists():
        print(f"WARNING: outcomes ledger not found at {path}; every episode "
              f"will render as in-flight and the pocket chart will be empty. "
              f"Seed it with backtest_outcomes.py --write-ledger.",
              file=sys.stderr)
        return {}
    with open(path, newline="") as f:
        return {(r["start_run_id"], r["ticker"]): r for r in csv.DictReader(f)}


def check_ledger_convention(outcomes: dict) -> None:
    """The dashed backtest references are quoted for one resolution
    convention. A ledger built with --horizon/--stop-pct/--entry overrides
    is internally fine but not comparable to those numbers, so say so
    rather than drawing a misleading comparison silently."""
    seen = {k: {r[k] for r in outcomes.values() if r.get(k)}
            for k in LEDGER_CONVENTION}
    off = {k: v for k, v in seen.items()
           if v and v != {LEDGER_CONVENTION[k]}}
    if off:
        print(f"WARNING: outcomes ledger was built with a different "
              f"resolution convention than the backtest references "
              f"({off}); the dashed reference lines are not comparable to "
              f"these results.", file=sys.stderr)


def load_sectors(path: Path) -> dict:
    if not path.exists():
        print(f"WARNING: sector cache not found at {path}; every ticker "
              f"will render with 'Unknown' sector. sectors.json is tracked "
              f"in the repo; pass --sectors if yours lives elsewhere.",
              file=sys.stderr)
        return {}
    return json.loads(path.read_text())


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _n(v, nd=1):
    """Round for the payload, dropping a trailing .0 — base weeks and scores
    are whole numbers, and `21` costs two bytes less than `21.0` on every
    one of the ~2k points."""
    if v is None:
        return None
    v = round(float(v), nd)
    return int(v) if v == int(v) else v


def ledger_horizon(outcomes: dict) -> int:
    """Sessions the ledger holds a trade for. Read from the rows rather than
    assumed, so a ledger built with --horizon still labels itself honestly
    (check_ledger_convention already warns that its numbers are then not
    comparable to the backtest references)."""
    seen = {r.get("horizon") for r in outcomes.values() if r.get("horizon")}
    if len(seen) == 1:
        h = _i(next(iter(seen)))
        if h:
            return h
    return int(LEDGER_CONVENTION["horizon"])


def _weekdays_after(a: str, b: str) -> int:
    """Weekdays strictly after run day a, through run day b."""
    da, db = (datetime.strptime(s, "%Y%m%d").date() for s in (a, b))
    n, cur = 0, da
    while cur < db:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def open_trade_day(led: dict, start_rid: str, latest_rid: str,
                   horizon: int) -> int | None:
    """How many sessions a triggered-but-unresolved trade has been running.

    A TRIGGERED episode carries no trade_ret_pct until its full horizon has
    printed, so the tooltip would otherwise show nothing and read the same
    as missing data. The scan's run days are not a market calendar (it skips
    one now and then), so the count starts as a weekday count and is then
    clamped to the band the ledger itself proves: ret5 and ret10 only exist
    once 5 and 10 bars have printed after the fill. Without that clamp a
    market holiday inside the window reads a day long.
    """
    if led.get("outcome") != "TRIGGERED" or led.get("trade_ret_pct"):
        return None
    dtt = _i(led.get("days_to_trigger"))
    if dtt is None:
        return None
    lo, hi = (10, horizon - 1) if led.get("ret10") \
        else (5, 9) if led.get("ret5") else (1, 4)
    return max(lo, min(max(lo, hi), _weekdays_after(start_rid, latest_rid) - dtt))


def _day_runs(pts: list[dict]):
    """Split points into runs of consecutive days. The approach chart stores
    a line as (first day, values) and rebuilds x from the index, which is
    only valid while the days are contiguous — an episode is contiguous by
    construction, but dropping points with no to_pivot_pct can punch a hole
    in one, and that would silently shift the rest of the line left."""
    seg: list[dict] = []
    for p in pts:
        if seg and p["d"] != seg[-1]["d"] + 1:
            yield seg
            seg = []
        seg.append(p)
    if seg:
        yield seg


def sector_edge(buckets: dict[str, list[float]],
                tallies: dict[str, list[int]], min_n: int) -> dict:
    """Per-sector realized trade result: mean, 95% interval, best first.

    The interval is the point of the panel, not decoration. Across ~45
    trading days most sectors resolve only a couple dozen trades, so the
    sampling noise alone is wider than the gaps between them — a bare bar
    would invite reading a 5-point spread as an edge. Sectors under min_n
    fold into a counted "Other" instead of showing a mean nobody should act
    on.
    """
    rows, folded_vals, folded_secs = [], [], []
    for s, vals in buckets.items():
        if len(vals) < min_n:
            folded_vals.extend(vals)
            folded_secs.append(s)
            continue
        m = statistics.mean(vals)
        # 1.96 SE, normal approximation. Stopped-out trades are fat-tailed
        # and same-week breakouts are correlated, so treat the width as a
        # "how much of this could be noise" cue, not a coverage guarantee.
        half = (1.96 * statistics.stdev(vals) / len(vals) ** 0.5
                if len(vals) > 1 else None)
        trig, fade, broke = tallies.get(s, [0, 0, 0])
        rows.append({
            "s": s, "n": len(vals), "exp": round(m, 2),
            "lo": round(m - half, 2) if half is not None else None,
            "hi": round(m + half, 2) if half is not None else None,
            "trig": trig, "fade": fade, "broke": broke,
        })
    rows.sort(key=lambda r: (-r["exp"], r["s"]))
    all_vals = [v for vals in buckets.values() for v in vals]
    return {
        "rows": rows,
        "folded": {"secs": len(folded_secs), "n": len(folded_vals),
                   "names": sorted(folded_secs)},
        "all": round(statistics.mean(all_vals), 2) if all_vals else None,
        "allN": len(all_vals),
        "minN": min_n,
    }


def build_payload(rows: list[dict], outcomes: dict, sectors: dict,
                  days_window: int = 0) -> dict:
    run_ids = sorted({r["run_id"] for r in rows})
    horizon = ledger_horizon(outcomes)
    day_idx = {rid: i for i, rid in enumerate(run_ids)}
    day_labels = [f"{rid[4:6]}-{rid[6:8]}" for rid in run_ids]
    n_days = len(run_ids)
    # Charts render only the trailing window; summary/KPI stay full-history.
    win_start = n_days - days_window if 0 < days_window < n_days else 0

    by_ticker: dict[str, dict] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], {})[r["run_id"]] = r

    def sector_of(t: str) -> str:
        return sectors.get(t, {}).get("sector", "Unknown")

    def sig_idx(s: str) -> int:
        # A trailing "*" marks a base that just resolved its geometry; the
        # tier itself is the glyph, so strip it before ranking.
        s = (s or "").strip().rstrip("*")
        return SIG_ORDER.index(s) if s in SIG_ORDER else 0

    series = []          # grid rows: one per ticker
    approach = []        # trajectory lines: one per episode
    summary = []         # roster rows: one per ticker
    cohort = [[0] * len(SIG_ORDER) for _ in run_ids]
    pocket_per_day = [0 for _ in run_ids]
    # Realized trade returns bucketed by the day the episode STARTED, so the
    # running expectancy advances when the bet was placed, not when it paid.
    pocket_by_day: list[list[float]] = [[] for _ in run_ids]
    base_by_day: list[list[float]] = [[] for _ in run_ids]
    # Realized trade returns bucketed by the name's sector, plus that
    # sector's triggered/faded/broke-down episode tally. Only TRIGGERED
    # episodes carry a trade return, so the bar is "what the ones that
    # fired paid" and the tally is how often they fired at all.
    sec_results: dict[str, list[float]] = {}
    sec_tally: dict[str, list[int]] = {}

    today_pocket: list[str] = []
    # (base weeks TODAY, ticker) for names on the latest run. Bases reset —
    # a name can carry a 40-week base in history and an 8-week one now — so
    # the "longest base right now" KPI must read the latest row, never the
    # per-ticker max. Computed here rather than in the page so it's covered
    # by tests.
    live_bases: list[tuple[float, str]] = []
    n_trig = n_faded = n_broke = 0

    for t, runs in sorted(by_ticker.items()):
        ds = sorted(day_idx[rid] for rid in runs)
        dset = set(ds)
        pts = []
        eps: list[dict] = []       # this ticker's episodes
        cur: dict | None = None
        max_bw = 0.0
        min_width = None
        best_tp = None             # closest approach to the pivot, ever
        pocket_days = 0

        for rid, r in sorted(runs.items()):
            d = day_idx[rid]
            streak = 1
            while d - streak in dset:
                streak += 1
            bw = _f(r.get("base_weeks"))
            tp = _f(r.get("to_pivot_pct"))
            si = sig_idx(r.get("signal"))
            pocket = bool(bw is not None and bw >= VALIDATED_BASE_WEEKS)

            cohort[d][si] += 1
            if pocket:
                pocket_per_day[d] += 1
                pocket_days += 1
                if d == n_days - 1:
                    today_pocket.append(t)
            if bw is not None:
                max_bw = max(max_bw, bw)
                if d == n_days - 1:
                    live_bases.append((bw, t))
            wd = _f(r.get("width_pct"))
            if wd is not None:
                min_width = wd if min_width is None else min(min_width, wd)
            if tp is not None:
                best_tp = tp if best_tp is None else max(best_tp, tp)

            # Episode boundary: a gap in listed days starts a new bet.
            if cur is None or d - 1 not in dset:
                led = outcomes.get((rid, t), {})
                tr = _f(led.get("trade_ret_pct"))
                cur = {
                    "start": rid, "sd": d, "oc": led.get("outcome"),
                    "tr": tr, "dtt": _i(led.get("days_to_trigger")),
                    "od": open_trade_day(led, rid, run_ids[-1], horizon),
                    "gap": _f(led.get("gap_pct")),
                    "res": _f(led.get("ret_h")),
                    "fb": _i(led.get("fellback5")),
                    "pk": pocket, "pts": [],
                }
                eps.append(cur)
                sec = sector_of(t)
                slot = {"TRIGGERED": 0, "FADED": 1, "BROKE_DOWN": 2}.get(
                    cur["oc"])
                if slot is not None:
                    sec_tally.setdefault(sec, [0, 0, 0])[slot] += 1
                if cur["oc"] == "TRIGGERED":
                    n_trig += 1
                elif cur["oc"] == "BROKE_DOWN":
                    n_broke += 1
                elif cur["oc"] == "FADED":
                    n_faded += 1
                if tr is not None:
                    (pocket_by_day if pocket else base_by_day)[d].append(tr)
                    sec_results.setdefault(sec, []).append(tr)

            # The pivot moves as the base rebases, so it is a per-DAY value:
            # the buy-stop price that was live that day, not the episode's.
            pv = _n(_f(r.get("pivot_price")), 2)
            cur["pts"].append({"d": d, "tp": tp, "pv": pv, "bw": _n(bw)})
            pts.append({
                "d": d, "s": si, "p": 1 if pocket else 0, "k": streak,
                "bw": _n(bw), "tp": _n(tp, 2), "pv": pv,
                "sc": _n(_f(r.get("base_score"))), "wd": _n(wd),
                "e": len(eps) - 1,
            })

        # Per-ticker realized expectancy across its episodes.
        trs = [e["tr"] for e in eps if e["tr"] is not None]
        exp = round(sum(trs) / len(trs), 2) if trs else None
        n_ep_trig = sum(1 for e in eps if e["oc"] == "TRIGGERED")
        n_ep_res = sum(1 for e in eps if e["oc"])

        win_pts = [{**p, "d": p["d"] - win_start}
                   for p in pts if p["d"] >= win_start]
        if win_pts:
            series.append({
                "t": t, "pts": win_pts, "days": len(ds),
                "exp": exp, "eps": [{"oc": e["oc"], "tr": e["tr"],
                                     "dtt": e["dtt"], "gap": e["gap"],
                                     "fb": e["fb"], "od": e["od"]}
                                    for e in eps],
            })
        for e in eps:
            # An episode is a run of CONSECUTIVE listed days, so the x
            # positions are implied by the first one — store the start index
            # plus the values, not a point object per day. Points with no
            # to_pivot_pct drop out, which can break that contiguity, so
            # each surviving run becomes its own line segment.
            vis = [p for p in e["pts"] if p["d"] >= win_start
                   and p["tp"] is not None]
            for seg in _day_runs(vis):
                approach.append({
                    "t": t, "sec": sector_of(t), "pk": 1 if e["pk"] else 0,
                    "oc": e["oc"], "tr": e["tr"], "dtt": e["dtt"],
                    "od": e["od"], "fb": e["fb"],
                    "gap": e["gap"], "d0": seg[0]["d"] - win_start,
                    "tps": [_n(p["tp"], 2) for p in seg],
                    # State on the segment's last day — the day the end dot
                    # sits on, and the day its to-pivot % is measured
                    # against. Base weeks rides along because it is the
                    # ranker the backtest validated; the tooltip would
                    # otherwise send the reader to the roster for it.
                    "pv": seg[-1]["pv"], "bw": seg[-1]["bw"],
                })

        last = pts[-1]
        summary.append({
            "t": t, "sec": sector_of(t), "days": len(ds), "eps": len(eps),
            "exp": exp,
            "trate": round(n_ep_trig / n_ep_res * 100) if n_ep_res else None,
            "mbw": _n(max_bw) if max_bw else None,
            "pd": pocket_days,
            "wd": round(min_width, 1) if min_width is not None else None,
            "tp": round(best_tp, 1) if best_tp is not None else None,
            "last": day_labels[ds[-1]], "lastD": ds[-1],
            "st": last["s"],
        })

    # Grid row order = roster default: longest base first, so the ⭐ pocket
    # (the one validated stratum) clusters at the top and the two adjacent
    # panels read in ONE direction.
    #
    # The key must match the roster's default comparator KEY FOR KEY —
    # max base weeks desc, days desc, last-seen desc, with names that have
    # no base weeks sunk — because the roster re-sorts this list in the page
    # and only falls back to the order below once every key ties (JS sort is
    # stable). A third key the page doesn't share silently scatters rows
    # between the two panels: ticker-name tie-breaking here put 48 of 198
    # rows at different heights than the roster showed them.
    order = {s["t"]: (s["mbw"] is None, -(s["mbw"] or 0), -s["days"],
                      -s["lastD"], s["t"]) for s in summary}
    series.sort(key=lambda s: order[s["t"]])
    summary.sort(key=lambda s: order[s["t"]])
    # Draw the ⭐ pocket last so its lines land on top of the gray mass.
    approach.sort(key=lambda a: (a["pk"], a["oc"] == "TRIGGERED"))

    def cum_mean(per_day: list[list[float]]) -> tuple[list, list]:
        vals, ns = [], []
        total, n = 0.0, 0
        for day_vals in per_day:
            total += sum(day_vals)
            n += len(day_vals)
            vals.append(round(total / n, 2) if n else None)
            ns.append(n)
        return vals, ns

    pkt_line, pkt_n = cum_mean(pocket_by_day)
    base_line, base_n = cum_mean(base_by_day)

    latest = cohort[-1]
    latest_total = sum(latest)
    # "Loaded" = the share of the list within striking distance (the top two
    # Sig tiers: imminent + breakout).
    latest_hot = latest[2] + latest[3]

    # Ties broken by ticker so the tile is deterministic across renders.
    longest_live = min(live_bases, key=lambda b: (-b[0], b[1])) \
        if live_bases else None

    n_resolved = n_trig + n_faded + n_broke
    all_trs = [v for day in (pocket_by_day + base_by_day) for v in day]

    # A sector whose bases all faded has episodes but no completed trades,
    # and would otherwise vanish from the panel entirely — not even counted
    # as folded. Seed it with an empty bucket so it lands in the folded
    # tally like any other sector too thin to draw.
    for s in sec_tally:
        sec_results.setdefault(s, [])
    # "Unknown" is a cache miss, not a sector — it can't be acted on, so it
    # leaves the panel and is reported as a coverage count instead.
    untagged = len(sec_results.pop("Unknown", []))
    sec_tally.pop("Unknown", None)
    sec_panel = sector_edge(sec_results, sec_tally, MIN_SECTOR_N)
    sec_panel["untagged"] = untagged

    return {
        "days": day_labels[win_start:],
        "horizon": horizon,
        "window": {"total": n_days, "shown": n_days - win_start},
        "series": series,
        "approach": approach,
        "summary": summary,
        "cohort": {
            "perDay": cohort[win_start:],
            "pocket": pocket_per_day[win_start:],
        },
        "pocket": {
            "pkt": pkt_line[win_start:], "pktN": pkt_n[win_start:],
            "base": base_line[win_start:], "baseN": base_n[win_start:],
            "refPkt": BACKTEST_POCKET_TRADE,
            "refBase": BACKTEST_BASELINE_TRADE,
            "minWeeks": VALIDATED_BASE_WEEKS,
        },
        "sectorEdge": sec_panel,
        "kpi": {
            "runs": n_days,
            "span": [f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"
                     for rid in (run_ids[0], run_ids[-1])],
            "latest": {"n": latest_total, "hot": latest_hot,
                       "sig": latest},
            "longestLive": ({"t": longest_live[1], "wk": _n(longest_live[0])}
                            if longest_live else None),
            "todayPocket": sorted(today_pocket),
            "episodes": {
                "n": n_resolved, "trig": n_trig, "faded": n_faded,
                "broke": n_broke,
                "rate": round(n_trig / n_resolved * 100) if n_resolved else None,
            },
            "exp": round(sum(all_trs) / len(all_trs), 2) if all_trs else None,
            "pexp": {
                "v": pkt_line[-1] if pkt_line else None,
                "n": pkt_n[-1] if pkt_n else 0,
            },
            "bexp": {
                "v": base_line[-1] if base_line else None,
                "n": base_n[-1] if base_n else 0,
            },
            "tracked": len(by_ticker),
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>base-breakout-scan history</title>
<meta name="description" content="__META_DESC__">
<meta property="og:type" content="website">
<meta property="og:site_name" content="base-breakout-scan">
<meta property="og:title" content="base-breakout-scan history">
<meta property="og:description" content="__META_DESC__">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="base-breakout-scan history">
<meta name="twitter:description" content="__META_DESC__">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🧗</text></svg>">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --ctx-line: #c9c8c1;
  /* Sig tiers are ORDINAL (how close to firing), so they get a sequential
     ramp rather than categorical hues: 4 steps off the same blue ramp the
     sibling dashboards use, spaced for adjacent distinguishability
     (CIE76 ΔE ≥ 30 adjacent on both surfaces). */
  --g0: #cde2fb; --g1: #86b6ef; --g2: #3987e5; --g3: #0d366b;
  --gx: #eceae4;
  /* Episode outcome — the trajectory lines and row-end verdicts. Triggered
     carries the emphasis (the thing the setup exists to do); broke-down is the
     alarm; faded is the deliberate neutral ("nothing happened"), leaning on
     the legend + roster per the relief rule.
     In-flight shares that same neutral rather than taking a fourth hue: the
     palette's remaining slots all collide on one surface (violet sits ΔE 10
     from the triggered blue in dark, 2 under simulated deuteranopia; magenta
     12 from the broke-down red; yellow reaches only 2.1:1 on white, too thin
     for a 1.2px line). Two gray STEPS were the first attempt and read as one
     color at 6.3 ΔE. So the state rides on line type instead — see the dash
     + hollow end dot in renderApproach. */
  --oT: #2a78d6; --oB: #a02525; --oF: #b5b4ad;
  /* ⭐ pocket, in BOTH panels that draw it (the count line over the Sig
     stack, the expectancy line and its dashed reference). One stratum, one
     hue. Aqua = dataviz slot 3: off the blue ramp it overlays (ΔE 20.9
     light / 19.2 dark, deutan 18.4 / 15.7) and out of the warm family,
     which on this page means loss (--oB, --tneg). It was slot-2 orange,
     which sat 10.4 from the broke-down red on the dark surface and 1.4
     under simulated deuteranopia. Light takes the darker step to clear
     3:1 on white (3.32:1), dark the lighter one (6.19:1). */
  --pkt: #199e70;
  --tpos: #006300; --tneg: #a02525;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --ctx-line: #47463f;
    --g0: #184f95; --g1: #2a78d6; --g2: #6da7ec; --g3: #cde2fb;
    --gx: #262624;
    --oT: #3987e5; --oB: #b83636; --oF: #55544d;
    --pkt: #1baf7a;
    --tpos: #0ca30c; --tneg: #e66767;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 21px; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 20px; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 20px; margin: 16px 0;
}
.card h2 { font-size: 15px; margin: 0 0 2px; }
.card .note { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; white-space: pre-line; }
.kpis { display: flex; flex-wrap: wrap; gap: 12px; }
.kpi {
  flex: 1 1 150px; min-width: 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 16px;
}
.kpi .lbl { color: var(--ink-2); font-size: 12.5px; }
.kpi .val { font-size: 22px; font-weight: 600; margin-top: 2px; }
.kpi .sub2 { color: var(--muted); font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.scroll { overflow-x: auto; }
/* Approach chart focus: the pointer picks the episode nearest to it and the
   rest recede, since landing on a 1.2px line among 500+ of them is not a
   thing a hand can do. Class on the root so one toggle restyles the lot —
   inline-styling ~1100 nodes per pointermove drops frames. */
#apchart svg.focus [data-a] { opacity: 0.12; }
#apchart svg.focus [data-a].lit { opacity: 1; }
#apchart svg.focus path[data-a].lit { stroke-width: 2.5; }
.vclip { max-height: 540px; overflow-y: auto; }
.gridhead { position: sticky; top: 0; z-index: 1; background: var(--surface); width: max-content; }
svg { display: block; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); }
svg text.tick { font-variant-numeric: tabular-nums; }
svg text.dlabel { font-size: 11.5px; font-weight: 600; fill: var(--ink-2); }
/* A tick riding INSIDE a label — the sample size that flows off the end of
   the sector panel's value. The rules above are `svg text.*`, which a tspan
   never matches, so without this it inherits the label's ink and weight and
   the sample size reads as loud as the number it qualifies. */
svg tspan.tick { font-size: 11px; font-weight: 400; fill: var(--muted);
                 font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 0; font-size: 12.5px; color: var(--ink-2); align-items: center; }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .line { width: 14px; height: 2px; border-radius: 1px; }
.legend .rect { width: 11px; height: 11px; border-radius: 3px; }
/* Flexbox centers each key against the text's LINE box, which reserves a
   descender's worth of space the mark should not be centered against. Align
   to the letters instead: at 12.5px system-ui the cap band's center sits
   0.78px above the line box's, the CJK ideographic square's 0.58px above,
   so one pixel puts both within half a pixel. Measure against cap height,
   never against a word's own ink — "Breakout" has no descender and
   "Triggered" does, and chasing that gives every label a different answer.
   Tooltip keys are exempt: their 13.5px/600 title already lands at 0.12px. */
.legend .line, .legend .rect { position: relative; top: -1px; }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin: 0 0 20px; }
.topbar .sub { margin-bottom: 0; }
.head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin: 0 0 12px; }
.head .note { margin-bottom: 0; }
select {
  font: 12.5px system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink);
  background: var(--surface); border: 1px solid var(--border); border-radius: 7px;
  padding: 4px 8px; flex: none; cursor: pointer;
}
#tip {
  position: fixed; pointer-events: none; z-index: 10; display: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.14); padding: 8px 11px; font-size: 12.5px;
  /* 360 fits the widest identity line — "Communication Services · To pivot
     +0.4% · Pivot $144.43" measures 336px, the Japanese equivalent 322 —
     so the sector, the distance and the buy-stop price stay on one line. */
  color: var(--ink-2); max-width: 360px;
}
#tip .h { display: flex; align-items: center; gap: 6px; }
#tip .v { color: var(--ink); font-weight: 600; font-size: 13.5px; }
#tip .k { width: 11px; height: 11px; border-radius: 3px; flex: none; }
/* Tooltip keys are a stroke, not a box: at this density a filled swatch is
   data-weight ink doing a label's job. Legends keep the box, since there it
   mirrors the mark. */
#tip .kline { width: 14px; height: 2px; border-radius: 1px; flex: none; }
#tip .aux { margin-left: auto; color: var(--muted); font-size: 12px; padding-left: 10px; }
/* Shorthand, not margin-top: the page-level .sub (the h1's subtitle) ships
   a 20px bottom margin, and this line would inherit it and float the rule
   away from the header. */
#tip .sub { margin: 2px 0 0; }
#tip .rule { border-top: 1px solid var(--border); margin: 7px 0; }
/* The setup's numbers, aligned. Value wears the ink and the label the muted
   step — in a tooltip the reader already knows which line they hovered and
   came for the number. */
#tip .kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 14px; }
#tip .kv .val { color: var(--ink); font-variant-numeric: tabular-nums; }
#tip .oc { color: var(--ink); margin-top: 7px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; cursor: pointer; user-select: none;
     position: sticky; top: 0; background: var(--surface); }
th:hover { color: var(--ink-2); }
th.on { color: var(--ink); }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
td { font-variant-numeric: tabular-nums; color: var(--ink-2); }
td.tk { color: var(--ink); font-weight: 600; }
.ocdot { display: inline-block; width: 11px; height: 11px; border-radius: 3px;
         border: 1px solid var(--border); vertical-align: -1px; margin-right: 6px; }
.foot { color: var(--muted); font-size: 12px; margin-top: 24px; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
svg a:hover text { text-decoration: underline; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1><a href="https://github.com/mthli/skills/tree/master/base-breakout-scan" target="_blank" rel="noopener">base-breakout-scan</a> <span id="h1-suffix">history</span></h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <select id="lang-menu" aria-label="Language"></select>
  </div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <div class="head">
      <div>
        <h2 id="ap-title">Approach to pivot</h2>
        <p class="note" id="ap-note"></p>
      </div>
      <select id="ap-filter"></select>
    </div>
    <div class="scroll" id="apchart"></div>
    <div class="legend" id="ap-legend"></div>
  </div>

  <div class="card">
    <h2 id="co-title">Watchlist tension</h2>
    <p class="note" id="co-note"></p>
    <div class="scroll" id="cochart"></div>
    <div class="legend" id="co-legend"></div>
  </div>

  <div class="card" id="pk-card">
    <h2 id="pk-title">⭐ Pocket vs the rest</h2>
    <p class="note" id="pk-note"></p>
    <div class="scroll" id="pkchart"></div>
    <div class="legend" id="pk-legend"></div>
  </div>

  <div class="card" id="se-card">
    <h2 id="se-title">Which sectors paid</h2>
    <p class="note" id="se-note"></p>
    <div class="scroll" id="sechart"></div>
    <div class="legend" id="se-legend"></div>
  </div>

  <div class="card">
    <div class="head">
      <div>
        <h2 id="grid-title">Maturity grid</h2>
        <p class="note" id="grid-note"></p>
      </div>
      <select id="grid-filter"></select>
    </div>
    <div class="scroll vclip" id="grid"></div>
    <div class="legend" id="grid-legend"></div>
  </div>

  <div class="card">
    <div class="head">
      <div>
        <h2 id="roster-title">Roster</h2>
        <p class="note" id="roster-note"></p>
      </div>
      <select id="roster-filter"></select>
    </div>
    <div class="scroll vclip"><table id="tbl"></table></div>
    <div class="legend" id="roster-legend"></div>
  </div>

  <p class="foot" id="foot"></p>
</div>
<div id="tip"></div>
<script>
const DATA = __DATA_JSON__;
const GENERATED = "__GENERATED__";
const MINWK = DATA.pocket.minWeeks;

// ---- i18n ----
const I18N = {
  en: {
    htmlLang: "en",
    title: "base-breakout-scan history",
    h1Suffix: "history",
    subtitle: (a, b, runs) => `${a} → ${b} · ${runs} trading days · every base's path to its pivot, and what happened after`,
    winTag: (s, t) => ` Charts show the last ${s} of ${t} trading days.`,
    kList: "Today's watchlist",
    kListSub: (hot, n) => `${hot} near or past trigger`,
    kToday: "Today's ⭐ pocket",
    kTrig: "Trigger rate",
    kTrigSub: (t, n) => `${t} / ${n} resolved`,
    kPocket: "⭐ Pocket per trade",
    kPocketSub: (n, r) => `n=${n} · backtest ${r >= 0 ? "+" : ""}${r}%`,
    kBase: "Everything else",
    kBaseSub: (n, r) => `n=${n} · backtest ${r >= 0 ? "+" : ""}${r}%`,
    kLongest: "Longest base now",
    none: "None",
    apTitle: "Approach to pivot",
    apNote: () => `One line per episode (a name's unbroken run on the list). Height = how far the price sits below its pivot.\nThe 0 line is the pivot, the price that makes the setup a buy. A line reaching it broke out; color = how the episode ended.`,
    apFilter: { pocket: `⭐ Pocket only (base ≥ ${MINWK}wk)`, trig: "Triggered only", all: "All episodes" },
    apFilterLabel: "Filter episodes",
    apEmpty: "No episodes match this filter in the charted window.",
    oc: { TRIGGERED: "Cleared the pivot", FADED: "Faded off the list", BROKE_DOWN: "Broke down", null: "In flight" },
    ocShort: { TRIGGERED: "Triggered", FADED: "Faded", BROKE_DOWN: "Broke down", null: "In flight" },
    pivotLine: "Pivot (trigger price)",
    coTitle: "Watchlist tension",
    coNote: () => `One column per day: the whole watchlist stacked by how close each name is to firing.\nA stack thick with imminent and breakout means the list is loaded and time-sensitive. All forming means nothing is near a trigger, so check back later.\nThe line counts the ⭐ pocket (bases ≥ ${MINWK} weeks). At zero, the list holds nothing the backtest validated.`,
    coPocketLine: "⭐ Pocket count",
    pkTitle: "⭐ Pocket vs the rest",
    pkNote: "Solid: the running average result per trade (pivot buy, 8% stop, 20 sessions), ⭐ pocket vs the rest. Base length counts from day one.\nDashed: the backtest's own numbers. A pocket line above its dash means the validated edge still pays.",
    pkPocket: "⭐ Pocket", pkBase: "The rest",
    pkRef: v => `backtest ${v >= 0 ? "+" : ""}${v}%`,
    pkTipN: n => `${n} trades`,
    seTitle: "Which sectors paid",
    seNote: minN => `Average result per completed trade, grouped by the name's sector — the ledger's answer to "does it matter what kind of stock the base is in".\nThe capped line above each bar is its 95% interval. Where that line reaches the dashed all-trades average, this sector is NOT distinguishable from the board as a whole. Sectors under ${minN} completed trades fold away.\nA sector's edge here can just as easily be the last two months of sector beta as a durable property of its bases — treat it as an observation to re-check each quarter, not a filter.`,
    sePos: "Sector made money", seNeg: "Sector lost money",
    seCI: "95% interval",
    seAll: v => `All trades ${v >= 0 ? "+" : ""}${v}%`,
    seTipN: n => `${n} completed trades`,
    seTipCI: (lo, hi) => `95% interval ${lo >= 0 ? "+" : ""}${lo}% to ${hi >= 0 ? "+" : ""}${hi}%`,
    seTipTrig: (rate, trig, tot) => `Trigger rate ${rate}% — ${trig} of ${tot} bases fired`,
    seTipSame: "Overlaps the all-trades average — no readable difference",
    seTipBetter: "Clears the all-trades average",
    seTipWorse: "Below the all-trades average",
    seFolded: (secs, n) => `\n${secs} sector(s) below the cutoff folded away (${n} trades).`,
    seUntagged: n => `\n${n} completed trades have no sector tag and sit outside this panel.`,
    gridTitle: "Maturity grid",
    gridNote: () => `One row per name, one cell per listed day, color = how close it was to firing that day; a center dot = ⭐ pocket day (base ≥ ${MINWK} weeks).\nRows run longest-base first, so the validated names lead. Row-end = that name's realized result per trade.\nA row whose color grows more vivid left to right is a base tightening toward its trigger; a break in the row is a dropout.`,
    all: "All",
    geDays: n => `≥${n} days listed`,
    daysFilterLabel: "Filter by days listed",
    rosterTitle: "Roster",
    rosterNote: "One row per name that ever made the list. Click a header to sort; click again to reverse. This table carries every value the charts show on hover.\nJudge by base weeks: the backtest validated that attribute and no other. Score is a display floor, not a ranker.",
    cols: ["Ticker", "Sector", "Max base wks", "Result %/trade", "Trigger rate", "Episodes", "⭐ days", "Tightest %", "Closest to pivot", "Days", "Last seen", "Sig"],
    sigName: { 0: "Forming", 1: "Coiled", 2: "Imminent", 3: "Breakout" },
    sigTip: {
      0: "Valid base, not near the trigger yet",
      1: "Squeezing, a few % of work left",
      2: "Loaded, within 3% of the trigger",
      3: "Broke out today on volume",
    },
    spellDay: k => `Day ${k} on the list`,
    pocketDay: "⭐ Pocket day",
    baseWks: "Base",
    wks: n => `${n} wks`,
    toPivot: "To pivot",
    pivotPx: "Pivot",
    listedOn: "Listed",
    score: "Score",
    width: "Width",
    trigIn: d => `Triggered after ${d} session(s)`,
    gapOver: g => `Filled ${g >= 0 ? "+" : ""}${g}% vs pivot`,
    tradeRet: v => `Trade result ${v >= 0 ? "+" : ""}${v}%`,
    tradeOpen: (k, h) => `Trade open (day ${k} of ${h})`,
    fellBack: "Fell back below the pivot within 5 sessions",
    heldPivot: "Held above the pivot for 5 sessions",
    dayLine: n => `${n} names listed`,
    genBy: "Generated by ", genAt: t => ` at ${t} · Source: `,
    sectorNames: {},
  },
  zh: {
    htmlLang: "zh-CN",
    title: "base-breakout-scan 历史",
    h1Suffix: "历史",
    subtitle: (a, b, runs) => `${a} → ${b} · 共 ${runs} 个交易日 · 每个平台基逼近触发线的全过程，以及之后发生了什么`,
    winTag: (s, t) => `图表仅显示最近 ${s} / ${t} 个交易日。`,
    kList: "今日名单",
    kListSub: (hot, n) => `${hot} 只临门一脚或已突破`,
    kToday: "今日 ⭐ 口袋",
    kTrig: "触发率",
    kTrigSub: (t, n) => `${n} 段里 ${t} 段冲过`,
    kPocket: "⭐ 口袋每单结果",
    kPocketSub: (n, r) => `样本 ${n} · 回测 ${r >= 0 ? "+" : ""}${r}%`,
    kBase: "其余全部",
    kBaseSub: (n, r) => `样本 ${n} · 回测 ${r >= 0 ? "+" : ""}${r}%`,
    kLongest: "当前最长的基",
    none: "无",
    apTitle: "逼近触发线",
    apNote: () => `一条线 = 一段上榜（某只票连续留在名单上的那一段）。线的高度 = 现价还差多少才够到触发价。\n0 线是触发线：线碰到它 = 突破了。线的颜色 = 这段上榜的结局。`,
    apFilter: { pocket: `只看 ⭐ 口袋（基龄 ≥ ${MINWK} 周）`, trig: "只看已触发", all: "全部上榜段" },
    apFilterLabel: "筛选上榜段",
    apEmpty: "当前窗口内没有符合此筛选的上榜段。",
    oc: { TRIGGERED: "冲过了触发线", FADED: "没冲上去，淡出名单", BROKE_DOWN: "跌穿了", null: "进行中" },
    ocShort: { TRIGGERED: "已触发", FADED: "淡出", BROKE_DOWN: "跌穿", null: "进行中" },
    pivotLine: "触发价",
    coTitle: "名单上膛程度",
    coNote: () => `每天一根柱：当天名单上的全部票，按「离触发还有多远」分层堆叠。\n柱子又高、临门一脚和今日突破又多 = 名单已上膛，时间敏感；全是成形中 = 没有一只逼近触发，过几天再看。\n折线 = ⭐ 口袋只数（基龄 ≥ ${MINWK} 周）。折线落到 0，当天名单里没有一只是回测验证过的类型。`,
    coPocketLine: "⭐ 口袋只数",
    pkTitle: "⭐ 口袋 vs 其余",
    pkNote: "实线：每单滚动平均盈亏（触发价买入、8% 止损、20 天卖出），⭐ 口袋 vs 其余。基龄按上榜第一天算。\n虚线：回测里的对应数。口袋线在虚线上方 = 那点验证过的边际还在兑现。",
    pkPocket: "⭐ 口袋", pkBase: "其余全部",
    pkRef: v => `回测 ${v >= 0 ? "+" : ""}${v}%`,
    pkTipN: n => `${n} 单`,
    seTitle: "哪类股票的底部真的兑现了",
    seNote: minN => `按股票所属板块，算它每笔了结交易的平均结果 —— 账本对"底部形态出现在什么类型的股票上要不要紧"的回答。\n每根条上方那条带端点的横线是 95% 误差范围。横线只要够到"全体平均"那条虚线，就说明这个板块跟整体比不出差别。已了结交易少于 ${minN} 笔的板块不单独画。\n这里的板块差距，同样可能只是最近两个月的板块行情，而不是这类股票的底部更靠谱 —— 当成每季度要复查的观察，别当成筛选条件。`,
    sePos: "该板块赚钱", seNeg: "该板块亏钱",
    seCI: "95% 误差范围",
    seAll: v => `全体平均 ${v >= 0 ? "+" : ""}${v}%`,
    seTipN: n => `已了结 ${n} 笔交易`,
    seTipCI: (lo, hi) => `95% 误差范围 ${lo >= 0 ? "+" : ""}${lo}% 到 ${hi >= 0 ? "+" : ""}${hi}%`,
    seTipTrig: (rate, trig, tot) => `触发率 ${rate}% —— ${tot} 个底部里 ${trig} 个真的突破了`,
    seTipSame: "与全体平均重叠 —— 看不出差别",
    seTipBetter: "确实高于全体平均",
    seTipWorse: "确实低于全体平均",
    seFolded: (secs, n) => `\n另有 ${secs} 个板块样本不足，已折叠（共 ${n} 笔交易）。`,
    seUntagged: n => `\n另有 ${n} 笔已了结交易没有板块标签，不计入本图。`,
    gridTitle: "成熟度网格",
    gridNote: () => `一行一只票，一格一个上榜日，颜色 = 那天离触发有多近；带中心点 = ⭐ 口袋日（基龄 ≥ ${MINWK} 周）。\n行序按最长基龄排，验证过的名字在最上面；行尾 = 该票实际每单盈亏。\n一行从左到右越来越醒目 = 这个基在收紧、逼近触发；行中间断开 = 那天掉出名单了。`,
    all: "全部",
    geDays: n => `上榜 ≥ ${n} 天`,
    daysFilterLabel: "按上榜天数筛选",
    rosterTitle: "上榜名录",
    rosterNote: "每只上过榜的票一行。点击表头排序；再次点击反向。图表里悬停能看到的数值，这张表都有。\n挑票看「最长基龄」这列，它是回测里唯一验证过的属性。Score 是显示门槛，不是排序依据。",
    cols: ["代码", "行业", "最长基龄(周)", "每单盈亏 %", "触发率", "上榜段数", "⭐ 天数", "最紧宽度 %", "最接近触发", "上榜天数", "最近上榜", "状态"],
    sigName: { 0: "成形中", 1: "收紧中", 2: "临门一脚", 3: "今日突破" },
    sigTip: {
      0: "基有效，但还没靠近触发价",
      1: "在收紧，离触发还差几个点",
      2: "已上膛，离触发价 3% 以内",
      3: "今天带量冲过了触发价",
    },
    spellDay: k => `上榜第 ${k} 天`,
    pocketDay: "⭐ 口袋日",
    baseWks: "基龄",
    wks: n => `${n} 周`,
    toPivot: "距触发",
    pivotPx: "触发价",
    listedOn: "上榜",
    score: "评分",
    width: "宽度",
    trigIn: d => `第 ${d} 个交易日触发`,
    gapOver: g => `成交价比触发价高 ${g >= 0 ? "+" : ""}${g}%`,
    tradeRet: v => `这单结果 ${v >= 0 ? "+" : ""}${v}%`,
    tradeOpen: (k, h) => `这单还在跑（第 ${k} / ${h} 天）`,
    fellBack: "触发后 5 天内又跌回触发价下方",
    heldPivot: "触发后 5 天都收在触发价上方",
    dayLine: n => `当天共 ${n} 只上榜`,
    genBy: "由 ", genAt: t => ` 于 ${t} 生成 · 数据源：`,
    sectorNames: {
      "Technology": "科技", "Financial Services": "金融服务", "Healthcare": "医疗保健",
      "Consumer Cyclical": "可选消费", "Consumer Defensive": "必需消费", "Industrials": "工业",
      "Communication Services": "通信服务", "Energy": "能源", "Basic Materials": "基础材料",
      "Real Estate": "房地产", "Utilities": "公用事业", "Unknown": "未知",
    },
  },
  zht: {
    htmlLang: "zh-Hant",
    title: "base-breakout-scan 歷史",
    h1Suffix: "歷史",
    subtitle: (a, b, runs) => `${a} → ${b} · 共 ${runs} 個交易日 · 每個平台基逼近觸發線的全過程，以及之後發生了什麼`,
    winTag: (s, t) => `圖表僅顯示最近 ${s} / ${t} 個交易日。`,
    kList: "今日名單",
    kListSub: (hot, n) => `${hot} 檔臨門一腳或已突破`,
    kToday: "今日 ⭐ 口袋",
    kTrig: "觸發率",
    kTrigSub: (t, n) => `${n} 段裡 ${t} 段衝過`,
    kPocket: "⭐ 口袋每筆結果",
    kPocketSub: (n, r) => `樣本 ${n} · 回測 ${r >= 0 ? "+" : ""}${r}%`,
    kBase: "其餘全部",
    kBaseSub: (n, r) => `樣本 ${n} · 回測 ${r >= 0 ? "+" : ""}${r}%`,
    kLongest: "目前最長的基",
    none: "無",
    apTitle: "逼近觸發線",
    apNote: () => `一條線 = 一段上榜（某檔票連續留在名單上的那一段）。線的高度 = 現價還差多少才夠到觸發價。\n0 線是觸發線：線碰到它 = 突破了。線的顏色 = 這段上榜的結局。`,
    apFilter: { pocket: `只看 ⭐ 口袋（基齡 ≥ ${MINWK} 週）`, trig: "只看已觸發", all: "全部上榜段" },
    apFilterLabel: "篩選上榜段",
    apEmpty: "目前窗口內沒有符合此篩選的上榜段。",
    oc: { TRIGGERED: "衝過了觸發線", FADED: "沒衝上去，淡出名單", BROKE_DOWN: "跌穿了", null: "進行中" },
    ocShort: { TRIGGERED: "已觸發", FADED: "淡出", BROKE_DOWN: "跌穿", null: "進行中" },
    pivotLine: "觸發價",
    coTitle: "名單上膛程度",
    coNote: () => `每天一根柱：當天名單上的全部票，按「離觸發還有多遠」分層堆疊。\n柱子又高、臨門一腳和今日突破又多 = 名單已上膛，時間敏感；全是成形中 = 沒有一檔逼近觸發，過幾天再看。\n折線 = ⭐ 口袋檔數（基齡 ≥ ${MINWK} 週）。折線落到 0，當天名單裡沒有一檔是回測驗證過的類型。`,
    coPocketLine: "⭐ 口袋檔數",
    pkTitle: "⭐ 口袋 vs 其餘",
    pkNote: "實線：每筆滾動平均盈虧（觸發價買入、8% 停損、20 天賣出），⭐ 口袋 vs 其餘。基齡按上榜第一天算。\n虛線：回測裡的對應數。口袋線在虛線上方 = 那點驗證過的邊際還在兌現。",
    pkPocket: "⭐ 口袋", pkBase: "其餘全部",
    pkRef: v => `回測 ${v >= 0 ? "+" : ""}${v}%`,
    pkTipN: n => `${n} 筆`,
    seTitle: "哪類股票的底部真的兌現了",
    seNote: minN => `按股票所屬板塊，算它每筆了結交易的平均結果 —— 帳本對「底部型態出現在什麼類型的股票上要不要緊」的回答。\n每根條上方那條帶端點的橫線是 95% 誤差範圍。橫線只要搆到「全體平均」那條虛線，就代表這個板塊跟整體比不出差別。已了結交易少於 ${minN} 筆的板塊不單獨畫。\n這裡的板塊差距，同樣可能只是最近兩個月的板塊行情，而不是這類股票的底部更可靠 —— 當成每季要複查的觀察，別當成篩選條件。`,
    sePos: "該板塊賺錢", seNeg: "該板塊虧錢",
    seCI: "95% 誤差範圍",
    seAll: v => `全體平均 ${v >= 0 ? "+" : ""}${v}%`,
    seTipN: n => `已了結 ${n} 筆交易`,
    seTipCI: (lo, hi) => `95% 誤差範圍 ${lo >= 0 ? "+" : ""}${lo}% 到 ${hi >= 0 ? "+" : ""}${hi}%`,
    seTipTrig: (rate, trig, tot) => `觸發率 ${rate}% —— ${tot} 個底部裡 ${trig} 個真的突破了`,
    seTipSame: "與全體平均重疊 —— 看不出差別",
    seTipBetter: "確實高於全體平均",
    seTipWorse: "確實低於全體平均",
    seFolded: (secs, n) => `\n另有 ${secs} 個板塊樣本不足，已摺疊（共 ${n} 筆交易）。`,
    seUntagged: n => `\n另有 ${n} 筆已了結交易沒有板塊標籤，不計入本圖。`,
    gridTitle: "成熟度網格",
    gridNote: () => `一行一檔票，一格一個上榜日，顏色 = 那天離觸發有多近；帶中心點 = ⭐ 口袋日（基齡 ≥ ${MINWK} 週）。\n行序按最長基齡排，驗證過的名字在最上面；行尾 = 該檔實際每筆盈虧。\n一行從左到右越來越醒目 = 這個基在收緊、逼近觸發；行中間斷開 = 那天掉出名單了。`,
    all: "全部",
    geDays: n => `上榜 ≥ ${n} 天`,
    daysFilterLabel: "按上榜天數篩選",
    rosterTitle: "上榜名錄",
    rosterNote: "每檔上過榜的票一行。點擊表頭排序；再次點擊反向。圖表裡懸停看得到的數值，這張表都有。\n挑票看「最長基齡」這欄，它是回測裡唯一驗證過的屬性。Score 是顯示門檻，不是排序依據。",
    cols: ["代號", "產業", "最長基齡(週)", "每筆盈虧 %", "觸發率", "上榜段數", "⭐ 天數", "最緊寬度 %", "最接近觸發", "上榜天數", "最近上榜", "狀態"],
    sigName: { 0: "成形中", 1: "收緊中", 2: "臨門一腳", 3: "今日突破" },
    sigTip: {
      0: "基有效，但還沒靠近觸發價",
      1: "在收緊，離觸發還差幾個點",
      2: "已上膛，離觸發價 3% 以內",
      3: "今天帶量衝過了觸發價",
    },
    spellDay: k => `上榜第 ${k} 天`,
    pocketDay: "⭐ 口袋日",
    baseWks: "基齡",
    wks: n => `${n} 週`,
    toPivot: "距觸發",
    pivotPx: "觸發價",
    listedOn: "上榜",
    score: "評分",
    width: "寬度",
    trigIn: d => `第 ${d} 個交易日觸發`,
    gapOver: g => `成交價比觸發價高 ${g >= 0 ? "+" : ""}${g}%`,
    tradeRet: v => `這筆結果 ${v >= 0 ? "+" : ""}${v}%`,
    tradeOpen: (k, h) => `這筆還在跑（第 ${k} / ${h} 天）`,
    fellBack: "觸發後 5 天內又跌回觸發價下方",
    heldPivot: "觸發後 5 天都收在觸發價上方",
    dayLine: n => `當天共 ${n} 檔上榜`,
    genBy: "由 ", genAt: t => ` 於 ${t} 生成 · 資料來源：`,
    sectorNames: {
      "Technology": "科技", "Financial Services": "金融服務", "Healthcare": "醫療保健",
      "Consumer Cyclical": "週期性消費", "Consumer Defensive": "防禦性消費", "Industrials": "工業",
      "Communication Services": "通訊服務", "Energy": "能源", "Basic Materials": "原物料",
      "Real Estate": "房地產", "Utilities": "公用事業", "Unknown": "未知",
    },
  },
  ja: {
    htmlLang: "ja",
    title: "base-breakout-scan 履歴",
    h1Suffix: "履歴",
    subtitle: (a, b, runs) => `${a} → ${b} · 全 ${runs} 営業日 · 各ベースがピボットに近づく過程と、その後の結果`,
    winTag: (s, t) => `チャートは直近 ${s} / ${t} 営業日のみ表示。`,
    kList: "本日のリスト",
    kListSub: (hot, n) => `${hot} 銘柄が目前か突破`,
    kToday: "本日の ⭐ ポケット",
    kTrig: "トリガー率",
    kTrigSub: (t, n) => `${n} 件中 ${t} 件が突破`,
    kPocket: "⭐ ポケット 1 回の損益",
    kPocketSub: (n, r) => `n=${n} · 検証値 ${r >= 0 ? "+" : ""}${r}%`,
    kBase: "それ以外すべて",
    kBaseSub: (n, r) => `n=${n} · 検証値 ${r >= 0 ? "+" : ""}${r}%`,
    kLongest: "現在の最長ベース",
    none: "なし",
    apTitle: "ピボットへの接近",
    apNote: () => `1 本の線 = 1 エピソード（銘柄がリストに連続して載っていた期間）。線の高さ = 現在値がピボットまであと何 % か。\n0 の線がトリガー：線がそこに届けばブレイクアウト成立。線の色はエピソードの結末を表します。`,
    apFilter: { pocket: `⭐ ポケットのみ（ベース ${MINWK} 週以上）`, trig: "トリガー済みのみ", all: "全エピソード" },
    apFilterLabel: "エピソードを絞り込み",
    apEmpty: "この期間に該当するエピソードはありません。",
    oc: { TRIGGERED: "ピボット突破", FADED: "届かずリスト落ち", BROKE_DOWN: "下方ブレイク", null: "進行中" },
    ocShort: { TRIGGERED: "トリガー済み", FADED: "フェード", BROKE_DOWN: "下方ブレイク", null: "進行中" },
    pivotLine: "ピボット（トリガー価格）",
    coTitle: "リストの張り詰め具合",
    coNote: () => `1 日 1 本の柱：その日のリスト全体を「発火までの近さ」で積み上げたもの。\n目前とブレイクが厚い柱 = リストは装填済みで時間との勝負。すべて形成中 = 発火寸前の銘柄なし、日を改めて確認を。\n折れ線は ⭐ ポケット数（ベース ${MINWK} 週以上）。ゼロの日は検証済みタイプが 1 つもありません。`,
    coPocketLine: "⭐ ポケット数",
    pkTitle: "⭐ ポケット vs その他",
    pkNote: "実線：1 トレード平均損益の推移（ピボット買い、8% ストップ、20 セッション）。⭐ ポケット vs その他。ベース週数は初日で判定。\n破線：バックテストの対応値。ポケット線が破線の上なら、検証済みのエッジは健在。",
    pkPocket: "⭐ ポケット", pkBase: "その他",
    pkRef: v => `バックテスト ${v >= 0 ? "+" : ""}${v}%`,
    pkTipN: n => `${n} トレード`,
    seTitle: "どのセクターのベースが実際に報われたか",
    seNote: minN => `銘柄のセクター別に、決済済みトレード1件あたりの平均結果。「ベースがどんな種類の株にできているかが効くのか」への台帳からの回答です。\n各バーの上にある端点付きの線は95%誤差範囲。この線が「全体平均」の破線に届くセクターは、全体との差が読み取れません。決済済みトレードが${minN}件未満のセクターは折り畳まれます。\nここでの差は、ベースの質ではなく直近2か月のセクター物色である可能性も同じくらいあります — 四半期ごとに見直す観察であって、絞り込み条件ではありません。`,
    sePos: "このセクターは利益", seNeg: "このセクターは損失",
    seCI: "95%誤差範囲",
    seAll: v => `全体平均 ${v >= 0 ? "+" : ""}${v}%`,
    seTipN: n => `決済済み ${n} トレード`,
    seTipCI: (lo, hi) => `95%誤差範囲 ${lo >= 0 ? "+" : ""}${lo}% 〜 ${hi >= 0 ? "+" : ""}${hi}%`,
    seTipTrig: (rate, trig, tot) => `トリガー率 ${rate}% — ${tot}件のベースのうち${trig}件が発動`,
    seTipSame: "全体平均と重なる — 差は読み取れない",
    seTipBetter: "全体平均を明確に上回る",
    seTipWorse: "全体平均を明確に下回る",
    seFolded: (secs, n) => `\nサンプル不足の${secs}セクター（計${n}トレード）は折り畳み。`,
    seUntagged: n => `\nセクター未設定の決済済み${n}トレードは本図の対象外。`,
    gridTitle: "成熟度グリッド",
    gridNote: () => `1 行 = 1 銘柄、1 セル = リスト入り 1 日、色 = その日の発火までの近さ。中心の点 = ⭐ ポケット日（ベース ${MINWK} 週以上）。\n行は最長ベース順、検証済みの銘柄が上に来ます。行末 = その銘柄の実際の 1 トレード損益。\n左から右へ色が鮮やかになる行はベースが締まりトリガーへ近づいた証。行の途切れはリスト落ちです。`,
    all: "すべて",
    geDays: n => `リスト入り ${n} 日以上`,
    daysFilterLabel: "リスト入り日数で絞り込み",
    rosterTitle: "銘柄一覧",
    rosterNote: "リストに載ったことのある銘柄を 1 行ずつ表示。ヘッダーをクリックでソート、もう一度クリックで逆順。チャートのホバー数値はすべてこの表で確認できます。\n判断はベース週数の列で。バックテストで検証された唯一の属性です。スコアは表示の足切りであり、ランキング指標ではありません。",
    cols: ["ティッカー", "セクター", "最長ベース(週)", "1 トレード損益 %", "トリガー率", "エピソード", "⭐ 日数", "最小幅 %", "ピボット最接近", "日数", "直近登場", "シグナル"],
    sigName: { 0: "形成中", 1: "収縮中", 2: "目前", 3: "ブレイク" },
    sigTip: {
      0: "有効なベース、まだトリガーには遠い",
      1: "収縮中、トリガーまであと数 %",
      2: "装填済み、トリガーまで 3% 以内",
      3: "本日、出来高を伴い突破",
    },
    spellDay: k => `リスト入り ${k} 日目`,
    pocketDay: "⭐ ポケット日",
    baseWks: "ベース",
    wks: n => `${n} 週`,
    toPivot: "ピボットまで",
    pivotPx: "ピボット",
    listedOn: "リスト入り",
    score: "スコア",
    width: "幅",
    trigIn: d => `${d} セッション目にトリガー`,
    gapOver: g => `約定はピボット比 ${g >= 0 ? "+" : ""}${g}%`,
    tradeRet: v => `トレード結果 ${v >= 0 ? "+" : ""}${v}%`,
    tradeOpen: (k, h) => `トレード継続中（${h} 日中 ${k} 日目）`,
    fellBack: "トリガー後 5 セッション以内にピボット下へ戻った",
    heldPivot: "トリガー後 5 セッション、ピボットの上を維持",
    dayLine: n => `その日 ${n} 銘柄がリスト入り`,
    genBy: "", genAt: t => ` により ${t} に生成 · データソース：`,
    sectorNames: {
      "Technology": "テクノロジー", "Financial Services": "金融サービス", "Healthcare": "ヘルスケア",
      "Consumer Cyclical": "一般消費財", "Consumer Defensive": "生活必需品", "Industrials": "資本財",
      "Communication Services": "通信サービス", "Energy": "エネルギー", "Basic Materials": "素材",
      "Real Estate": "不動産", "Utilities": "公益事業", "Unknown": "不明",
    },
  },
  ko: {
    htmlLang: "ko",
    title: "base-breakout-scan 히스토리",
    h1Suffix: "히스토리",
    subtitle: (a, b, runs) => `${a} → ${b} · 총 ${runs}거래일 · 각 베이스가 피봇에 다가가는 과정과 그 이후 결과`,
    winTag: (s, t) => `차트는 최근 ${s} / ${t}거래일만 표시합니다.`,
    kList: "오늘의 목록",
    kListSub: (hot, n) => `${hot}종목 임박 또는 돌파`,
    kToday: "오늘의 ⭐ 포켓",
    kTrig: "발동률",
    kTrigSub: (t, n) => `${n}건 중 ${t}건 돌파`,
    kPocket: "⭐ 포켓 거래당 손익",
    kPocketSub: (n, r) => `n=${n} · 백테스트 ${r >= 0 ? "+" : ""}${r}%`,
    kBase: "나머지 전체",
    kBaseSub: (n, r) => `n=${n} · 백테스트 ${r >= 0 ? "+" : ""}${r}%`,
    kLongest: "현재 가장 긴 베이스",
    none: "없음",
    apTitle: "피봇 접근",
    apNote: () => `선 1개 = 에피소드 1개(종목이 목록에 연속으로 남아 있던 구간). 선의 높이 = 현재가가 피봇까지 몇 % 남았는지.\n0 선이 발동선입니다. 선이 거기 닿으면 돌파 성공. 선 색깔은 에피소드의 결말을 뜻합니다.`,
    apFilter: { pocket: `⭐ 포켓만 (베이스 ${MINWK}주 이상)`, trig: "발동한 것만", all: "전체 에피소드" },
    apFilterLabel: "에피소드 필터",
    apEmpty: "이 기간에 해당하는 에피소드가 없습니다.",
    oc: { TRIGGERED: "피봇 돌파", FADED: "못 넘고 목록에서 이탈", BROKE_DOWN: "하방 이탈", null: "진행 중" },
    ocShort: { TRIGGERED: "발동", FADED: "소멸", BROKE_DOWN: "하방 이탈", null: "진행 중" },
    pivotLine: "피봇(발동 가격)",
    coTitle: "목록의 긴장도",
    coNote: () => `하루 1개 기둥: 그날 목록 전체를 '발동까지의 거리'로 쌓은 것.\n임박과 돌파가 두꺼운 기둥이면 목록이 장전된 상태이고 시간이 중요합니다. 전부 형성 중이면 발동에 가까운 종목이 없다는 뜻이니 나중에 다시 보세요.\n선은 ⭐ 포켓 개수(베이스 ${MINWK}주 이상)입니다. 0인 날은 백테스트로 검증된 유형이 하나도 없습니다.`,
    coPocketLine: "⭐ 포켓 개수",
    pkTitle: "⭐ 포켓 vs 나머지",
    pkNote: "실선: 거래당 롤링 평균 손익(피봇 매수, 8% 손절, 20세션 청산). ⭐ 포켓 vs 나머지. 베이스 주수는 첫날 기준.\n점선: 백테스트 수치. 포켓 선이 점선 위면 검증된 엣지가 유효합니다.",
    pkPocket: "⭐ 포켓", pkBase: "나머지",
    pkRef: v => `백테스트 ${v >= 0 ? "+" : ""}${v}%`,
    pkTipN: n => `${n}건`,
    seTitle: "어떤 섹터의 베이스가 실제로 결실을 맺었나",
    seNote: minN => `종목의 섹터별로 청산된 거래 1건당 평균 결과 — "베이스가 어떤 종류의 주식에 생겼는지가 중요한가"에 대한 장부의 답입니다.\n각 막대 위에 있는 끝점 달린 선은 95% 오차 범위입니다. 이 선이 "전체 평균" 점선에 닿으면 그 섹터는 전체와 구분되지 않습니다. 청산된 거래가 ${minN}건 미만인 섹터는 접힙니다.\n여기서 보이는 차이는 베이스의 질이 아니라 최근 두 달의 섹터 장세일 가능성도 그만큼 큽니다 — 분기마다 다시 확인할 관찰이지, 필터가 아닙니다.`,
    sePos: "이 섹터는 수익", seNeg: "이 섹터는 손실",
    seCI: "95% 오차 범위",
    seAll: v => `전체 평균 ${v >= 0 ? "+" : ""}${v}%`,
    seTipN: n => `청산된 거래 ${n}건`,
    seTipCI: (lo, hi) => `95% 오차 범위 ${lo >= 0 ? "+" : ""}${lo}% ~ ${hi >= 0 ? "+" : ""}${hi}%`,
    seTipTrig: (rate, trig, tot) => `발동률 ${rate}% — 베이스 ${tot}개 중 ${trig}개가 돌파`,
    seTipSame: "전체 평균과 겹침 — 차이를 읽을 수 없음",
    seTipBetter: "전체 평균을 확실히 상회",
    seTipWorse: "전체 평균을 확실히 하회",
    seFolded: (secs, n) => `\n표본이 부족한 ${secs}개 섹터(총 ${n}건)는 접었습니다.`,
    seUntagged: n => `\n섹터 태그가 없는 청산된 거래 ${n}건은 이 패널에서 제외됩니다.`,
    gridTitle: "성숙도 그리드",
    gridNote: () => `1행 = 1종목, 1셀 = 등재 1일, 색 = 그날 발동까지의 거리. 중심의 점 = ⭐ 포켓일(베이스 ${MINWK}주 이상).\n행은 최장 베이스 순이라 검증된 종목이 위에 옵니다. 행 끝 = 그 종목의 실제 거래당 손익.\n왼쪽에서 오른쪽으로 색이 선명해지는 행은 베이스가 조여지며 발동에 다가간 것이고, 행이 끊기면 목록에서 빠진 것입니다.`,
    all: "전체",
    geDays: n => `등재 ${n}일 이상`,
    daysFilterLabel: "등재 일수로 필터",
    rosterTitle: "종목 목록",
    rosterNote: "목록에 오른 적 있는 종목을 한 행씩 표시. 헤더를 클릭해 정렬, 다시 클릭하면 역순. 차트의 모든 호버 값을 이 표에서 확인할 수 있습니다.\n판단은 베이스 주수 열로 하세요. 백테스트가 검증한 유일한 속성입니다. 점수는 표시 기준일 뿐 순위 지표가 아닙니다.",
    cols: ["티커", "섹터", "최장 베이스(주)", "거래당 손익 %", "발동률", "에피소드", "⭐ 일수", "최소 폭 %", "피봇 최근접", "일수", "최근 등재", "신호"],
    sigName: { 0: "형성 중", 1: "수축 중", 2: "임박", 3: "돌파" },
    sigTip: {
      0: "유효한 베이스, 아직 발동선과 거리 있음",
      1: "수축 중, 발동까지 몇 % 남음",
      2: "장전됨, 발동선까지 3% 이내",
      3: "오늘 거래량과 함께 돌파",
    },
    spellDay: k => `등재 ${k}일째`,
    pocketDay: "⭐ 포켓일",
    baseWks: "베이스",
    wks: n => `${n}주`,
    toPivot: "피봇까지",
    pivotPx: "피봇",
    listedOn: "등재",
    score: "점수",
    width: "폭",
    trigIn: d => `${d}번째 세션에 발동`,
    gapOver: g => `체결가는 피봇 대비 ${g >= 0 ? "+" : ""}${g}%`,
    tradeRet: v => `거래 결과 ${v >= 0 ? "+" : ""}${v}%`,
    tradeOpen: (k, h) => `거래 진행 중 (${h}일 중 ${k}일째)`,
    fellBack: "발동 후 5세션 내에 피봇 아래로 되밀림",
    heldPivot: "발동 후 5세션 동안 피봇 위 유지",
    dayLine: n => `그날 ${n}종목 등재`,
    genBy: "", genAt: t => `로 ${t}에 생성 · 데이터 출처: `,
    sectorNames: {
      "Technology": "기술", "Financial Services": "금융 서비스", "Healthcare": "헬스케어",
      "Consumer Cyclical": "임의소비재", "Consumer Defensive": "필수소비재", "Industrials": "산업재",
      "Communication Services": "커뮤니케이션 서비스", "Energy": "에너지", "Basic Materials": "소재",
      "Real Estate": "부동산", "Utilities": "유틸리티", "Unknown": "미상",
    },
  },
};
const LANG = (() => {
  try {
    const s = localStorage.getItem("baseBreakoutScanLang");
    if (I18N[s]) return s;
  } catch (e) {}
  const l = (navigator.language || "").toLowerCase();
  if (l.startsWith("ja")) return "ja";
  if (l.startsWith("ko")) return "ko";
  if (!l.startsWith("zh")) return "en";
  return /hant|tw|hk|mo/.test(l) ? "zht" : "zh";
})();
const T = I18N[LANG];
const secName = s => T.sectorNames[s] || s;
document.documentElement.lang = T.htmlLang;
document.title = T.title;
{
  const sel = document.getElementById("lang-menu");
  [["en", "English"], ["zh", "简体中文"], ["zht", "繁體中文"], ["ja", "日本語"], ["ko", "한국어"]].forEach(([v, lbl]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = lbl;
    sel.appendChild(o);
  });
  sel.value = LANG;
  // The whole page renders once at load, so a language switch just re-runs it.
  sel.addEventListener("change", () => {
    try { localStorage.setItem("baseBreakoutScanLang", sel.value); } catch (e) {}
    location.reload();
  });
  document.getElementById("h1-suffix").textContent = T.h1Suffix;
  document.getElementById("ap-title").textContent = T.apTitle;
  document.getElementById("co-title").textContent = T.coTitle;
  document.getElementById("pk-title").textContent = T.pkTitle;
  document.getElementById("se-title").textContent = T.seTitle;
  document.getElementById("grid-title").textContent = T.gridTitle;
  document.getElementById("roster-title").textContent = T.rosterTitle;
  document.getElementById("roster-note").textContent = T.rosterNote;
}

const DAYS = DATA.days.length;
const WIN_TAG = DATA.window.shown < DATA.window.total ? T.winTag(DATA.window.shown, DATA.window.total) : "";
// Days-listed filter, shared by the grid and the roster so the two panels
// always show the same rows. The preset choices always include whatever
// --min-days rendered with: an unmatched <select> value silently falls back
// to "" (which reads as 0), which would leave the menu blank AND desync the
// two panels.
const MIN_DAYS = __MIN_DAYS__;
const DAY_FILTERS = [...new Set([0, 3, 5, MIN_DAYS])].sort((a, b) => a - b);
function buildDayFilter(sel, onChange) {
  sel.setAttribute("aria-label", T.daysFilterLabel);
  DAY_FILTERS.forEach(n => {
    const o = document.createElement("option");
    o.value = n;
    o.textContent = n === 0 ? T.all : T.geDays(n);
    sel.appendChild(o);
  });
  sel.value = MIN_DAYS;
  sel.addEventListener("change", () => onChange(+sel.value));
}
// Sig tiers ascend toward the trigger everywhere: stack bottom→top, ramp
// pale→dark, roster sort order.
const SIGS = [0, 1, 2, 3];
const SIG_VAR = { 0: "--g0", 1: "--g1", 2: "--g2", 3: "--g3" };
// Episode outcomes. `null` (no ledger row) means in flight, not unknown.
// In-flight borrows the faded gray; the dash + hollow dot carry the state.
const OC_VAR = { TRIGGERED: "--oT", FADED: "--oF", BROKE_DOWN: "--oB", null: "--oF" };
const OCS = ["TRIGGERED", "FADED", "BROKE_DOWN", null];
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function div(cls, parent, text) {
  const e = document.createElement("div");
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
const tickerUrl = t => `https://finance.yahoo.com/quote/${encodeURIComponent(t)}`;
function tickerLink(t) {
  const a = document.createElement("a");
  a.href = tickerUrl(t); a.target = "_blank"; a.rel = "noopener";
  a.textContent = t;
  return a;
}
function tickerList(arr) {
  if (!arr.length) return T.none;
  const sp = document.createElement("span");
  arr.forEach((t, i) => {
    if (i) sp.appendChild(document.createTextNode(" "));
    sp.appendChild(tickerLink(t));
  });
  return sp;
}
const pctTxt = (v, nd) => (v >= 0 ? "+" : "") + v.toFixed(nd === undefined ? 2 : nd) + "%";
// The pivot is the one number on this page you could act on — it is the
// buy-stop price. US large caps only, so the $ is not a currency guess.
const pxTxt = v => "$" + v.toFixed(2);
const cssVar = v => getComputedStyle(document.documentElement).getPropertyValue(v);
const tip = document.getElementById("tip");
function showTip(x, y, build) {
  tip.textContent = "";
  build(tip);
  tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = (y - r.height - 12 < 4 ? y + 16 : y - r.height - 12) + "px";
}
const hideTip = () => { tip.style.display = "none"; };
function tipRows(t, rows, keyColor) {
  const head = div("h", t);
  if (keyColor) { const k = document.createElement("span"); k.className = "k"; k.style.background = keyColor; head.appendChild(k); }
  const v = document.createElement("span"); v.className = "v"; v.textContent = rows[0]; head.appendChild(v);
  rows.slice(1).filter(Boolean).forEach(r => div(null, t, r));
}
// The two rich tooltips (a trajectory, a grid cell) carry three different
// kinds of fact, and as one flat list of sentences they read as a wall.
// Split them: who this is, what the setup measured, what the trade did.
function tipCard(t, { title, aux, color, line, sub, kv, notes }) {
  const head = div("h", t);
  if (color) {
    const k = document.createElement("span");
    k.className = line ? "kline" : "k";
    k.style.background = color;
    head.appendChild(k);
  }
  const v = document.createElement("span");
  v.className = "v"; v.textContent = title;
  head.appendChild(v);
  if (aux) div("aux", head, aux);
  if (sub) div("sub", t, sub);
  const pairs = (kv || []).filter(Boolean);
  if (pairs.length) {
    div("rule", t);
    const g = div("kv", t);
    pairs.forEach(([k, val]) => { div(null, g, k); div("val", g, val); });
  }
  (notes || []).filter(Boolean).forEach((n, i) => div(i ? null : "oc", t, n));
}
// Episode lines for a tooltip: what happened, and the numbers behind it.
function epLines(e) {
  const out = [T.oc[e.oc === null || e.oc === undefined ? null : e.oc]];
  if (e.dtt != null) out.push(T.trigIn(e.dtt));
  if (e.gap != null) out.push(T.gapOver(e.gap));
  // A triggered episode with no result is a trade still running, not a hole
  // in the data: the ledger only scores it once the full horizon prints.
  const open = e.tr == null && e.od != null;
  if (e.tr != null) out.push(T.tradeRet(e.tr));
  else if (open) out.push(T.tradeOpen(e.od, DATA.horizon));
  // How the first week went, but only while the trade is still running: it
  // is the one forward-looking fact there (+5.3%/trade and 13% stop-hit for
  // the ones that hold the pivot, -4.3% and 72% for the ones that don't).
  // On a finished trade the result line above already answers it, and
  // repeating it on all 154 of those pushes a signal the exit-rule backtest
  // says NOT to act on — cutting at that first close below the pivot costs
  // the ⭐ pocket 3.6pt per trade. A null fb means those 5 sessions have not
  // printed yet, so neither line is true.
  if (open && e.fb != null) out.push(e.fb ? T.fellBack : T.heldPivot);
  return out;
}

// ---- subtitle + KPI row ----
{
  const k = DATA.kpi;
  document.getElementById("subtitle").textContent =
    T.subtitle(k.span[0], k.span[1], k.runs);
  const box = document.getElementById("kpis");
  const tile = (lbl, val, ...subs) => {
    const t = div("kpi", box);
    div("lbl", t, lbl);
    const v = div("val", t);
    if (val instanceof Node) v.appendChild(val); else v.textContent = val;
    subs.filter(Boolean).forEach(s => {
      const d = div("sub2", t);
      if (s instanceof Node) d.appendChild(s); else d.textContent = s;
      d.title = d.textContent;
    });
  };
  tile(T.kList, `${k.latest.n}`, T.kListSub(k.latest.hot, k.latest.n));
  tile(T.kToday, `${k.todayPocket.length}`, tickerList(k.todayPocket));
  const e = k.episodes;
  if (e.rate !== null)
    tile(T.kTrig, `${e.rate}%`, T.kTrigSub(e.trig, e.n));
  if (k.pexp.v !== null)
    tile(T.kPocket, pctTxt(k.pexp.v), T.kPocketSub(k.pexp.n, DATA.pocket.refPkt));
  if (k.bexp.v !== null)
    tile(T.kBase, pctTxt(k.bexp.v), T.kBaseSub(k.bexp.n, DATA.pocket.refBase));
  // Longest base among the names listed TODAY, read from today's own row —
  // bases reset, so a per-ticker maximum would report a base that no longer
  // exists. Resolved in build_payload (tested); this only renders it.
  if (k.longestLive)
    tile(T.kLongest, T.wks(k.longestLive.wk), tickerLink(k.longestLive.t));
}

// ---- approach-to-pivot trajectories ----
const apBox = document.getElementById("apchart");
function renderApproach(mode) {
  apBox.textContent = "";
  const eps = DATA.approach.filter(a =>
    mode === "all" ? true : mode === "trig" ? a.oc === "TRIGGERED" : a.pk);
  // The default ⭐-pocket filter can legitimately match nothing, and bare
  // axes would claim "no edge" when the truth is "no data" — say so.
  if (!eps.length) {
    const d = div(null, apBox, T.apEmpty);
    d.style.cssText = "color:var(--muted);font-size:12.5px;padding:26px 0;text-align:center";
    return;
  }
  const ML = 40, MT = 12, MB = 26, DX = 19, PH = 210;
  const W = ML + (DAYS - 1) * DX + 60, H = MT + PH + MB;
  const vals = eps.flatMap(a => a.tps);
  // Always keep the trigger line and a little air above it in frame, even
  // when every selected episode sits far below.
  let hi = Math.max(1.5, ...(vals.length ? vals : [0]));
  let lo = Math.min(-1.5, ...(vals.length ? vals : [0]));
  const pad = (hi - lo) * 0.06;
  hi += pad; lo -= pad;
  const xOf = d => ML + d * DX, yOf = v => MT + (hi - v) / (hi - lo) * PH;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, apBox);
  const step = (hi - lo) > 18 ? 5 : (hi - lo) > 8 ? 2 : 1;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    if (Math.abs(v) < 1e-9) continue;  // zero gets its own emphatic line
    el("line", { x1: ML - 4, x2: ML + (DAYS - 1) * DX, y1: yOf(v), y2: yOf(v),
      stroke: "var(--grid)" }, svg);
    el("text", { x: ML - 8, y: yOf(v) + 4, "text-anchor": "end", class: "tick" }, svg)
      .textContent = (v > 0 ? "+" : "") + v.toFixed(0) + "%";
  }
  DATA.days.forEach((d, i) => {
    if (i === DAYS - 1 || (i % 5 === 0 && DAYS - 1 - i >= 3))
      el("text", { x: xOf(i), y: H - 8, "text-anchor": "middle", class: "tick" }, svg).textContent = d;
  });
  const marks = [];   // episode index → the elements that draw it
  eps.forEach((a, ai) => {
    const oc = a.oc === null || a.oc === undefined ? null : a.oc;
    const col = `var(${OC_VAR[oc]})`;
    // Unresolved episodes share the faded gray: both are the absence of a
    // verdict, and the three hues left in the palette all collide with the
    // triggered blue or the broke-down red on one surface or the other. The
    // distinction rides on line type instead — dashes plus a hollow end dot,
    // which survives CVD and black-and-white printing.
    const live = oc === null;
    const op = a.pk ? 0.95 : 0.45, lastD = a.d0 + a.tps.length - 1;
    if (a.tps.length > 1) {
      let dstr = "";
      a.tps.forEach((tp, i) => {
        dstr += (i ? "L" : "M") + xOf(a.d0 + i) + " " + yOf(tp).toFixed(1);
      });
      const attrs = { d: dstr, fill: "none", stroke: col,
        "stroke-width": a.pk ? 2 : 1.2, opacity: op,
        "stroke-linecap": "round", "stroke-linejoin": "round" };
      if (live) {
        attrs["stroke-dasharray"] = "5 3";
        attrs["stroke-linecap"] = "butt";   // round caps close the gaps up
      }
      marks[ai] = [el("path", attrs, svg)];
      marks[ai][0].dataset.a = ai;
    } else marks[ai] = [];
    // End dot: the episode's last known distance to the pivot. On a one-day
    // episode it is the whole mark, which is why in-flight has to read here
    // too — 40 of the 96 ⭐ pocket episodes are a single day.
    const cy = yOf(a.tps[a.tps.length - 1]).toFixed(1);
    const dot = live
      ? { r: a.pk ? 3.5 : 2.5, fill: "var(--surface)", stroke: col,
          "stroke-width": 1.2 }
      : { r: a.pk ? 3 : 2, fill: col };
    const e = el("circle", { cx: xOf(lastD), cy, opacity: op, ...dot }, svg);
    e.dataset.a = ai;
    marks[ai].push(e);
  });
  // The trigger line last so it sits above every trajectory. Hairline like
  // every other gridline; the ink step (grid → secondary) is what marks it
  // as the one line with a meaning.
  el("line", { x1: ML - 4, x2: ML + (DAYS - 1) * DX, y1: yOf(0), y2: yOf(0),
    stroke: "var(--ink-2)" }, svg);
  el("text", { x: ML - 8, y: yOf(0) + 4, "text-anchor": "end", class: "tick" }, svg)
    .textContent = "0%";
  // Focus follows the pointer; a click pins so the line survives a trip to
  // the tooltip or another window. Restyle only when the focused episode
  // CHANGES — the class toggle is cheap, doing it 60×/second is not.
  let lit = null, pinned = null;
  const focus = i => {
    if (i === lit) return;
    if (lit !== null && marks[lit]) marks[lit].forEach(m => m.classList.remove("lit"));
    lit = i;
    if (i === null) { svg.classList.remove("focus"); return; }
    marks[i].forEach(m => m.classList.add("lit"));
    svg.classList.add("focus");
  };
  // Nearest episode to the pointer, within 24px vertically, in one pass.
  // The comparison walks the drawn SEGMENT (y interpolated at the pointer's
  // fractional day) rather than the nearest day's value: on a line dropping
  // 4% in a session, snapping to the day column hands the focus to whatever
  // flat line happens to pass nearby, which is not the line under the hand.
  const nearest = (mx, my) => {
    const fd = (mx - ML) / DX;
    if (fd < -0.6 || fd > DAYS - 0.4) return null;
    let best = null, bd = 24;
    eps.forEach((a, i) => {
      const k = fd - a.d0, last = a.tps.length - 1;
      if (k < -0.6 || k > last + 0.6) return;
      const j = Math.max(0, Math.min(last - 1, Math.floor(k)));
      const y = last === 0 ? yOf(a.tps[0])
        : yOf(a.tps[j]) + (yOf(a.tps[j + 1]) - yOf(a.tps[j]))
          * Math.max(0, Math.min(1, k - j));
      const dy = Math.abs(y - my);
      if (dy < bd) { bd = dy; best = i; }
    });
    return best;
  };
  const at = ev => {
    const box = svg.getBoundingClientRect();
    return nearest(ev.clientX - box.left, ev.clientY - box.top);
  };
  svg.addEventListener("pointermove", ev => {
    const i = at(ev);
    focus(pinned !== null ? pinned : i);
    if (i === null) { hideTip(); return; }
    const a = eps[i];
    showTip(ev.clientX, ev.clientY, tt => tipCard(tt, {
      title: a.t + (a.pk ? " ⭐" : ""),
      aux: secName(a.sec),
      color: cssVar(OC_VAR[a.oc === null || a.oc === undefined ? null : a.oc]),
      line: true,
      kv: [
        // When, first: a card with no date leaves a June line and a July
        // line looking identical. The span is the episode's listed days,
        // and it also says which day the numbers below are read on — the
        // last one, since that is where the end dot sits.
        [T.listedOn, DATA.days[a.d0] + (a.tps.length > 1
          ? ` → ${DATA.days[a.d0 + a.tps.length - 1]}` : "")],
        a.bw == null ? null : [T.baseWks, T.wks(a.bw)],
        // Then the two price rows. Spans and base length are both
        // durations; the pivot and the distance to it are both prices, and
        // the pivot sits next to the outcome lines that measure against it.
        a.pv == null ? null : [T.pivotPx, pxTxt(a.pv)],
        [T.toPivot, pctTxt(a.tps[a.tps.length - 1], 1)],
      ],
      notes: epLines(a),
    }));
  });
  svg.addEventListener("pointerleave", () => { focus(pinned); hideTip(); });
  svg.addEventListener("click", ev => {
    const i = at(ev);
    pinned = pinned !== null && pinned === i ? null : i;
    focus(pinned !== null ? pinned : i);
  });
}
{
  document.getElementById("ap-note").textContent = T.apNote() + WIN_TAG;
  const sel = document.getElementById("ap-filter");
  sel.setAttribute("aria-label", T.apFilterLabel);
  // Default to the ⭐ pocket: with every episode drawn the panel is a gray
  // thicket, and the pocket is the only stratum the backtest validated.
  [["pocket", T.apFilter.pocket], ["trig", T.apFilter.trig], ["all", T.apFilter.all]]
    .forEach(([v, lbl]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = lbl;
      sel.appendChild(o);
    });
  sel.value = "pocket";
  sel.addEventListener("change", () => renderApproach(sel.value));
  renderApproach(sel.value);
  const leg = document.getElementById("ap-legend");
  OCS.forEach(o => {
    const k = div("key", leg); const l = div("line", k);
    const c = `var(${OC_VAR[o]})`;
    // In-flight wears its dash in the legend too, or the key would show two
    // identical gray bars.
    l.style.background = o === null
      ? `repeating-linear-gradient(90deg,${c} 0 4px,transparent 4px 7px)` : c;
    k.appendChild(document.createTextNode(T.ocShort[o]));
  });
  const k = div("key", leg); const l = div("line", k);
  l.style.background = "var(--ink-2)";
  k.appendChild(document.createTextNode(T.pivotLine));
}

// ---- watchlist tension: Sig stack + pocket line ----
{
  document.getElementById("co-note").textContent = T.coNote() + WIN_TAG;
  const ML = 34, MT = 12, MB = 26, DX = 19, BW = 13, PH = 170;
  const totals = DATA.cohort.perDay.map(d => d.reduce((a, c) => a + c, 0));
  const ymax = Math.max(10, ...totals);
  const W = ML + DAYS * DX + 76, H = MT + PH + MB;
  const yOf = v => MT + PH - v / ymax * PH;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, document.getElementById("cochart"));
  const step = ymax > 120 ? 50 : ymax > 60 ? 25 : 10;
  for (let v = 0; v <= ymax; v += step) {
    el("line", { x1: ML - 4, x2: ML + DAYS * DX, y1: yOf(v), y2: yOf(v), stroke: "var(--grid)" }, svg);
    el("text", { x: ML - 8, y: yOf(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent = v;
  }
  DATA.cohort.perDay.forEach((counts, d) => {
    let acc = 0;
    const x = ML + d * DX;
    counts.forEach((c, i) => {
      if (!c) return;
      const y1 = yOf(acc + c), y0 = yOf(acc);
      const r = el("rect", { x, y: y1 + 1, width: BW,
        height: Math.max(y0 - y1 - 2, 0.5), rx: 2,
        fill: `var(${SIG_VAR[i]})` }, svg);
      r.dataset.d = d; r.dataset.i = i; r.dataset.c = c;
      acc += c;
    });
    if (d === DAYS - 1 || (d % 5 === 0 && DAYS - 1 - d >= 3))
      el("text", { x: x + BW / 2, y: H - 8, "text-anchor": "middle", class: "tick" }, svg).textContent = DATA.days[d];
  });
  // ⭐ pocket count rides on the same count axis as the stack, so the two
  // read against each other directly (no second scale to reconcile).
  let dstr = "";
  DATA.cohort.pocket.forEach((v, d) => {
    dstr += (d ? "L" : "M") + (ML + d * DX + BW / 2) + " " + yOf(v).toFixed(1);
  });
  el("path", { d: dstr, fill: "none", stroke: "var(--pkt)", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round" }, svg);
  const lastP = DATA.cohort.pocket[DAYS - 1];
  el("circle", { cx: ML + (DAYS - 1) * DX + BW / 2, cy: yOf(lastP).toFixed(1), r: 4,
    fill: "var(--pkt)", stroke: "var(--surface)", "stroke-width": 2 }, svg);
  el("text", { x: ML + (DAYS - 1) * DX + BW / 2 + 10, y: yOf(lastP) + 4, class: "dlabel" }, svg)
    .textContent = `⭐ ${lastP}`;
  const leg = document.getElementById("co-legend");
  // Every Sig legend on this page descends (breakout first): here it
  // matches the stack's own top-down order, and the grid and the roster
  // follow so the reader learns the scale once.
  SIGS.slice().reverse().forEach(i => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${SIG_VAR[i]})`;
    k.appendChild(document.createTextNode(T.sigName[i]));
  });
  const k = div("key", leg); const l = div("line", k);
  l.style.background = "var(--pkt)";
  k.appendChild(document.createTextNode(T.coPocketLine));
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.i !== undefined) {
      const i = +t.dataset.i, d = +t.dataset.d;
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [`${T.sigName[i]} ${+t.dataset.c}`,
         T.sigTip[i],
         `${DATA.days[d]} · ${T.dayLine(totals[d])} · ⭐ ${DATA.cohort.pocket[d]}`],
        cssVar(SIG_VAR[i])));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
}

// ---- pocket vs rest cumulative trade expectancy ----
{
  const P = DATA.pocket;
  // Nothing resolved yet (a fresh history, or a ledger nobody has seeded):
  // an empty pair of axes claims "no edge" when the truth is "no data".
  // Drop the whole card and let the setup panels carry the page.
  if (![...P.pkt, ...P.base].some(v => v !== null))
    document.getElementById("pk-card").style.display = "none";
  else {
  document.getElementById("pk-note").textContent = T.pkNote + WIN_TAG;
  const ML = 40, MT = 12, MB = 26, DX = 19, PH = 160;
  const allVals = [...P.pkt, ...P.base, P.refPkt, P.refBase, 0]
    .filter(v => v !== null);
  let lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad; hi += pad;
  // Right margin fits the widest CJK direct label ("其余全部 -2.26%" ≈ 130px).
  const W = ML + (DAYS - 1) * DX + 150, H = MT + PH + MB;
  const xOf = d => ML + d * DX, yOf = v => MT + (hi - v) / (hi - lo) * PH;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, document.getElementById("pkchart"));
  const step = (hi - lo) > 12 ? 4 : (hi - lo) > 6 ? 2 : 1;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const zero = Math.abs(v) < 1e-9;
    el("line", { x1: ML - 4, x2: ML + (DAYS - 1) * DX, y1: yOf(v), y2: yOf(v),
      stroke: zero ? "var(--axis)" : "var(--grid)" }, svg);
    el("text", { x: ML - 8, y: yOf(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent =
      (v > 0 ? "+" : "") + v.toFixed(0) + "%";
  }
  DATA.days.forEach((d, i) => {
    if (i === DAYS - 1 || (i % 5 === 0 && DAYS - 1 - i >= 3))
      el("text", { x: xOf(i), y: H - 8, "text-anchor": "middle", class: "tick" }, svg).textContent = d;
  });
  [[P.refPkt, "var(--pkt)"], [P.refBase, "var(--ctx-line)"]].forEach(([v, col]) => {
    el("line", { x1: ML, x2: ML + (DAYS - 1) * DX, y1: yOf(v), y2: yOf(v),
      stroke: col, "stroke-dasharray": "4 3", opacity: 0.7 }, svg);
  });
  const lines = [
    { vals: P.pkt, ns: P.pktN, col: "var(--pkt)", w: 2, lbl: T.pkPocket },
    { vals: P.base, ns: P.baseN, col: "var(--ctx-line)", w: 2, lbl: T.pkBase },
  ];
  lines.forEach(L => {
    let dstr = "", started = false;
    L.vals.forEach((v, d) => {
      if (v === null) return;
      dstr += (started ? "L" : "M") + xOf(d) + " " + yOf(v).toFixed(1);
      started = true;
    });
    el("path", { d: dstr, fill: "none", stroke: L.col, "stroke-width": L.w,
      "stroke-linecap": "round", "stroke-linejoin": "round" }, svg);
    let lastD = -1;
    L.vals.forEach((v, d) => { if (v !== null) lastD = d; });
    if (lastD >= 0) {
      el("circle", { cx: xOf(lastD), cy: yOf(L.vals[lastD]).toFixed(1), r: 4,
        fill: L.col, stroke: "var(--surface)", "stroke-width": 2 }, svg);
      el("text", { x: xOf(lastD) + 10, y: yOf(L.vals[lastD]) + 4, class: "dlabel" }, svg)
        .textContent = `${L.lbl} ${pctTxt(L.vals[lastD])}`;
    }
  });
  const leg = document.getElementById("pk-legend");
  lines.forEach(L => {
    const k = div("key", leg); const l = div("line", k); l.style.background = L.col;
    k.appendChild(document.createTextNode(L.lbl));
  });
  [[P.refPkt, T.pkPocket, "var(--pkt)"],
   [P.refBase, T.pkBase, "var(--ctx-line)"]].forEach(([v, lbl, col]) => {
    const k = div("key", leg);
    const l = div("line", k);
    l.style.cssText = `background:repeating-linear-gradient(90deg,${col} 0 4px,transparent 4px 7px)`;
    k.appendChild(document.createTextNode(`${lbl} ${T.pkRef(v)}`));
  });
  const cross = el("line", { y1: MT - 4, y2: MT + PH, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
  svg.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect();
    const d = Math.max(0, Math.min(DAYS - 1, Math.round((ev.clientX - box.left - ML) / DX)));
    cross.setAttribute("x1", xOf(d)); cross.setAttribute("x2", xOf(d));
    cross.setAttribute("visibility", "visible");
    const rows = [DATA.days[d]];
    lines.forEach(L => {
      if (L.vals[d] !== null)
        rows.push(`${L.lbl} ${pctTxt(L.vals[d])} · ${T.pkTipN(L.ns[d])}`);
    });
    if (rows.length > 1)
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt, rows));
    else hideTip();
  });
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  }
}

// ---- maturity grid ----
const gridBox = document.getElementById("grid");
function renderGrid(minDays) {
  gridBox.textContent = "";
  const rows = DATA.series.filter(s => s.days >= minDays);
  const GL = 64, CW = 16, CH = 14;
  const W = GL + DAYS * CW + 56;
  // Date labels live in their own sticky layer (SVG can't sticky-position
  // internal elements); opaque surface background masks rows scrolling by.
  const head = document.createElement("div");
  head.className = "gridhead";
  gridBox.appendChild(head);
  const hsvg = el("svg", { width: W, height: 18, viewBox: `0 0 ${W} 18` }, head);
  DATA.days.forEach((d, i) => {
    if (i === DAYS - 1 || (i % 5 === 0 && DAYS - 1 - i >= 3))
      el("text", { x: GL + i * CW + CW / 2, y: 13, "text-anchor": "middle", class: "tick" }, hsvg).textContent = d;
  });
  const H = rows.length * CH + 4;
  // Clip exactly on a row boundary: header height plus a whole number of
  // rows, so no half-cut row peeks out at the bottom of the viewport.
  const HEAD_H = 18, CAP = 540;
  gridBox.style.maxHeight = (HEAD_H + Math.floor((CAP - HEAD_H) / CH) * CH) + "px";
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, gridBox);
  rows.forEach((s, ri) => {
    const y = ri * CH;
    const ra = el("a", { href: tickerUrl(s.t), target: "_blank", rel: "noopener" }, svg);
    el("text", { x: GL - 8, y: y + CH - 3, "text-anchor": "end", class: "tick" }, ra)
      .textContent = s.t;
    if (s.exp != null) {
      // The corrective number, placed where the seduction happens: a long
      // dark row reads as "this one was really cooking" while the trade it
      // produced may well have lost money.
      const et = el("text", { x: GL + DAYS * CW + 6, y: y + CH - 3, class: "tick" }, svg);
      et.style.fill = s.exp >= 0 ? "var(--tpos)" : "var(--tneg)";
      et.textContent = pctTxt(s.exp, 1);
    }
    s.pts.forEach((p, pi) => {
      const r = el("rect", { x: GL + p.d * CW, y: y + 1, width: CW - 2, height: CH - 2, rx: 2,
        fill: `var(${SIG_VAR[p.s]})` }, svg);
      r.dataset.t = s.t; r.dataset.i = pi;
      // ⭐ pocket day → surface-colored center dot (color-independent, so
      // it reads on every tier fill and survives CVD).
      if (p.p) el("circle", { cx: GL + p.d * CW + (CW - 2) / 2, cy: y + CH / 2,
        r: 2, fill: "var(--surface)", "pointer-events": "none" }, svg);
    });
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.t) {
      const s = DATA.series.find(x => x.t === t.dataset.t);
      const p = s.pts[+t.dataset.i];
      const e = s.eps[p.e] || {};
      showTip(ev.clientX, ev.clientY, tt => tipCard(tt, {
        title: s.t + (p.p ? " ⭐" : ""),
        aux: DATA.days[p.d],
        color: cssVar(SIG_VAR[p.s]),
        // The tier name belongs with the day it describes, not in a row of
        // its own: the cell's color already said it, this only names it.
        sub: `${T.sigName[p.s]} · ${T.spellDay(p.k)}`
          + (p.p ? ` · ${T.pocketDay}` : ""),
        // Same grouping as the trajectory card: what the base is, then the
        // two prices. Score last, since the backtest found it does not
        // discriminate outcomes (findings #3) — it is a display floor.
        kv: [
          [T.baseWks, p.bw == null ? "—" : T.wks(p.bw)],
          [T.width, p.wd == null ? "—" : p.wd.toFixed(1) + "%"],
          p.pv == null ? null : [T.pivotPx, pxTxt(p.pv)],
          [T.toPivot, p.tp == null ? "—" : pctTxt(p.tp, 1)],
          [T.score, p.sc == null ? "—" : p.sc.toFixed(0)],
        ],
        notes: epLines(e),
      }));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
}
{
  document.getElementById("grid-note").textContent = T.gridNote() + WIN_TAG;
  // Same filter dimension AND default as the roster, so the two panels
  // show the same rows in the same order.
  buildDayFilter(document.getElementById("grid-filter"), renderGrid);
  renderGrid(MIN_DAYS);
  const leg = document.getElementById("grid-legend");
  SIGS.slice().reverse().forEach(i => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${SIG_VAR[i]})`;
    k.appendChild(document.createTextNode(T.sigName[i]));
  });
  const k = div("key", leg);
  const d = document.createElement("span"); d.className = "ocdot";
  d.style.background = "var(--g1)"; d.style.position = "relative";
  const dot = document.createElement("span");
  dot.style.cssText = "position:absolute;left:3px;top:3px;width:3px;height:3px;border-radius:50%;background:var(--surface)";
  d.appendChild(dot); k.appendChild(d);
  k.appendChild(document.createTextNode(T.pocketDay));
}

// ---- sector edge: realized trade result per sector, with 95% intervals ----
if (!DATA.sectorEdge.rows.length) {
  // Nothing has cleared the cutoff yet — there is no panel to draw.
  document.getElementById("se-card").style.display = "none";
} else {
  const SE = DATA.sectorEdge;
  let note = T.seNote(SE.minN);
  if (SE.folded.secs) note += T.seFolded(SE.folded.secs, SE.folded.n);
  if (SE.untagged) note += T.seUntagged(SE.untagged);
  document.getElementById("se-note").textContent = note;

  const R = SE.rows;
  // ML holds the longest sector name, measured rather than guessed: English
  // "Communication Services" is the widest across all five locales at 141px
  // (11.5px system-ui), so 150 clipped it by a pixel. MR holds the value
  // column, parked at a fixed x rather than floating off each bar's end —
  // bars point both ways here, and alternating label sides makes the
  // numbers unscannable.
  //
  // The interval rides ABOVE its bar rather than through it: both are
  // horizontal marks on the same row, so drawn co-linearly the interval
  // reads as a slot cut through the bar — and the surface halo it would
  // need to stay legible off-bar is exactly what cuts it. BY is the bar's
  // offset from the row center, CY the interval's.
  const ML = 162, MR = 112, MT = 10, MB = 26, RH = 30, BH = 9, PW = 440;
  const BY = 2, CY = -7, CAP = 3.5;
  const W = ML + PW + MR, H = MT + R.length * RH + MB;
  const ends = [0, SE.all];
  R.forEach(r => {
    ends.push(r.exp);
    if (r.lo !== null) ends.push(r.lo, r.hi);
  });
  let lo = Math.min(...ends), hi = Math.max(...ends);
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad; hi += pad;
  const xOf = v => ML + (v - lo) / (hi - lo) * PW;
  const yOf = i => MT + i * RH + RH / 2;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` },
    document.getElementById("sechart"));

  // Gridlines on whole percents, solid hairlines; zero carries the axis tone
  // because it is where every bar starts, not just another gridline.
  const span = hi - lo;
  const step = span > 24 ? 10 : span > 12 ? 5 : span > 6 ? 2 : 1;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const zero = Math.abs(v) < 1e-9;
    el("line", { x1: xOf(v), x2: xOf(v), y1: MT, y2: MT + R.length * RH,
      stroke: zero ? "var(--axis)" : "var(--grid)" }, svg);
    el("text", { x: xOf(v), y: H - 8, "text-anchor": "middle", class: "tick" },
      svg).textContent = (v > 0 ? "+" : "") + v.toFixed(0) + "%";
  }
  // The all-trades average: the line an interval has to clear before a
  // sector is saying anything the board isn't already saying.
  const xAll = xOf(SE.all);
  el("line", { x1: xAll, x2: xAll, y1: MT, y2: MT + R.length * RH,
    stroke: "var(--ink-2)", "stroke-dasharray": "4 3", opacity: 0.75 }, svg);

  // Bar with 4px rounded ends on the DATA end only — the zero end stays
  // square against the axis it is anchored to.
  const barPath = (x0, x1, y, h) => {
    const r = Math.min(4, Math.abs(x1 - x0), h / 2), t = y - h / 2, b = y + h / 2;
    const s = x1 >= x0 ? 1 : 0, d = x1 >= x0 ? -r : r;
    return `M${x0} ${t}H${x1 + d}A${r} ${r} 0 0 ${s} ${x1} ${t + r}`
      + `V${b - r}A${r} ${r} 0 0 ${s} ${x1 + d} ${b}H${x0}Z`;
  };
  // A missing interval (a lone sample) is not evidence of a difference, so
  // it reads exactly like an overlapping one: not distinguishable. Without
  // the null arm, a one-trade sector would be drawn solid and told the
  // reader it "clears the average" on the strength of that single trade.
  const straddles = r =>
    r.lo === null || (r.lo <= SE.all && r.hi >= SE.all);

  R.forEach((r, i) => {
    const y = yOf(i), by = y + BY;
    // The roster's own +/− tones. This page's outcome hues mean triggered /
    // faded / broke-down, not profit — borrowing the triggered blue for
    // "made money" would collide with the meaning it already carries here.
    const col = r.exp >= 0 ? "var(--tpos)" : "var(--tneg)";
    el("text", { x: ML - 10, y: by + 4, "text-anchor": "end", class: "dlabel" },
      svg).textContent = secName(r.s);
    el("path", { d: barPath(xOf(0), xOf(r.exp), by, BH), fill: col,
      // A sector that overlaps the board average is drawn back, not hidden:
      // the bar still reports its sign, at the weight the evidence supports.
      opacity: straddles(r) ? 0.42 : 1 }, svg);
    if (r.lo !== null) {
      const cy = y + CY;
      el("line", { x1: xOf(r.lo), x2: xOf(r.hi), y1: cy, y2: cy,
        stroke: "var(--ink-2)", "stroke-width": 1.5 }, svg);
      [r.lo, r.hi].forEach(v => el("line", { x1: xOf(v), x2: xOf(v),
        y1: cy - CAP, y2: cy + CAP, stroke: "var(--ink-2)",
        "stroke-width": 1.5 }, svg));
    }
    // Value then sample size in ONE text element: the n rides on a tspan
    // whose dx flows it off the end of the percentage, so a two-digit
    // sector return pushes it right instead of colliding with it. A second
    // text at a fixed x would need the widest label measured first.
    const vt = el("text", { x: ML + PW + 10, y: by + 4, class: "dlabel" }, svg);
    vt.textContent = pctTxt(r.exp);
    el("tspan", { dx: 8, class: "tick" }, vt).textContent = "n=" + r.n;
    // Episodes that reached a verdict. Trades (r.n) can be FEWER than the
    // triggered count — a fired base whose 20 sessions are still running
    // has no realized return yet — so the two numbers are reported as what
    // they are rather than folded together.
    const decided = r.trig + r.fade + r.broke;
    const hit = el("rect", { x: 0, y: y - RH / 2, width: W, height: RH,
      fill: "transparent" }, svg);
    hit.addEventListener("pointermove", ev => showTip(ev.clientX, ev.clientY,
      tt => tipRows(tt, [
        `${secName(r.s)} ${pctTxt(r.exp)}`,
        T.seTipN(r.n),
        r.lo === null ? "" : T.seTipCI(r.lo, r.hi),
        straddles(r) ? T.seTipSame
          : (r.exp > SE.all ? T.seTipBetter : T.seTipWorse),
        decided ? T.seTipTrig(Math.round(r.trig / decided * 100), r.trig,
          decided) : "",
      ].filter(Boolean), col)));
    hit.addEventListener("pointerleave", hideTip);
  });

  const leg = document.getElementById("se-legend");
  [[T.sePos, "var(--tpos)"], [T.seNeg, "var(--tneg)"]].forEach(([lbl, c]) => {
    const k = div("key", leg);
    div("rect", k).style.background = c;
    k.appendChild(document.createTextNode(lbl));
  });
  const ci = div("key", leg);
  div("line", ci).style.background = "var(--ink-2)";
  ci.appendChild(document.createTextNode(T.seCI));
  const av = div("key", leg);
  div("line", av).style.cssText =
    "background:repeating-linear-gradient(90deg,var(--ink-2) 0 4px,transparent 4px 7px)";
  av.appendChild(document.createTextNode(T.seAll(SE.all)));
}

// ---- roster table ----
{
  const tbl = document.getElementById("tbl");
  // Column order follows the judgment path: identity, the validated ranker
  // (base weeks), the realized verdict, the evidence behind it, then
  // geometry, exposure and current state.
  const COLS = [
    { h: T.cols[0], v: s => s.t,     dir: 1 },
    { h: T.cols[1], v: s => s.sec,   dir: 1 },
    { h: T.cols[2], v: s => s.mbw,   dir: -1 },
    { h: T.cols[3], v: s => s.exp,   dir: -1 },
    { h: T.cols[4], v: s => s.trate, dir: -1 },
    { h: T.cols[5], v: s => s.eps,   dir: -1 },
    { h: T.cols[6], v: s => s.pd || null, dir: -1 },
    { h: T.cols[7], v: s => s.wd,    dir: 1 },
    { h: T.cols[8], v: s => s.tp,    dir: -1 },
    { h: T.cols[9], v: s => s.days,  dir: -1 },
    { h: T.cols[10], v: s => s.lastD, dir: -1 },
    { h: T.cols[11], v: s => s.st,   dir: -1 },
  ];
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  const ths = COLS.map(() => {
    const th = document.createElement("th");
    hr.appendChild(th);
    return th;
  });
  thead.appendChild(hr); tbl.appendChild(thead);
  const tb = document.createElement("tbody");
  tbl.appendChild(tb);

  let sortCol = 2, sortDir = -1, minDays = MIN_DAYS;
  const renderHead = () => ths.forEach((th, i) => {
    th.textContent = COLS[i].h + (i === sortCol ? (sortDir === 1 ? " ▲" : " ▼") : "");
    th.className = i === sortCol ? "on" : "";
  });
  const renderRows = () => {
    tb.textContent = "";
    const v = COLS[sortCol].v;
    DATA.summary.filter(s => s.days >= minDays).sort((a, b) => {
      const x = v(a), y = v(b);
      const xn = x === null || x === undefined, yn = y === null || y === undefined;
      if (xn || yn) return xn && yn ? (b.days - a.days) : xn ? 1 : -1;  // nulls always sink
      const c = (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir;
      return c || (b.days - a.days) || (b.lastD - a.lastD);
    }).forEach(s => {
      const tr = document.createElement("tr");
      [s.t, secName(s.sec),
       // Keep the decimal: rounding 19.6 up to "20" would claim the ⭐
       // threshold (≥ 20) for a base that never reached it.
       s.mbw !== null ? String(s.mbw) : "—",
       s.exp !== null ? pctTxt(s.exp) : "—",
       s.trate !== null ? s.trate + "%" : "—",
       s.eps,
       s.pd || "—",
       s.wd !== null ? s.wd.toFixed(1) : "—",
       s.tp !== null ? pctTxt(s.tp, 1) : "—",
       s.days, s.last, s.st]
      .forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 0) { td.className = "tk"; td.appendChild(tickerLink(c)); }
        else if (i === 3 && s.exp !== null) {
          td.textContent = c;
          td.style.color = s.exp >= 0 ? "var(--tpos)" : "var(--tneg)";
        }
        else if (i === 11) {
          const d = document.createElement("span");
          d.className = "ocdot";
          d.style.background = `var(${SIG_VAR[s.st]})`;
          d.title = T.sigName[s.st];
          d.setAttribute("aria-label", T.sigName[s.st]);
          d.setAttribute("role", "img");
          td.appendChild(d);
        }
        else td.textContent = c;
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
  };
  ths.forEach((th, i) => th.addEventListener("click", () => {
    if (sortCol === i) sortDir = -sortDir;
    else { sortCol = i; sortDir = COLS[i].dir; }
    renderHead(); renderRows();
  }));
  renderHead(); renderRows();

  // Grow the 540px viewport up to the boundary of the row it would cut,
  // so the default view ends on a whole row (row height is font-derived,
  // so measure the real layout instead of assuming a constant).
  for (const r of tb.rows) {
    const bottom = r.offsetTop + r.offsetHeight;
    if (bottom >= 540) {
      tbl.parentElement.style.maxHeight = bottom + "px";
      break;
    }
  }

  buildDayFilter(document.getElementById("roster-filter"), n => {
    minDays = n;
    renderRows();
  });

  const leg = document.getElementById("roster-legend");
  SIGS.slice().reverse().forEach(i => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${SIG_VAR[i]})`;
    k.appendChild(document.createTextNode(T.sigName[i]));
  });
}

{
  const foot = document.getElementById("foot");
  foot.append(T.genBy);
  const fa = document.createElement("a");
  fa.href = "https://github.com/mthli/skills/blob/master/base-breakout-scan/scripts/render_history_html.py";
  fa.target = "_blank"; fa.rel = "noopener";
  fa.textContent = "render_history_html.py";
  foot.append(fa, T.genAt(GENERATED));
  ["state/history.csv", "state/outcomes.csv"].forEach((p, i) => {
    if (i) foot.append(" + ");
    const sa = document.createElement("a");
    sa.href = "https://github.com/mthli/skills/blob/master/base-breakout-scan/" + p;
    sa.target = "_blank"; sa.rel = "noopener";
    sa.textContent = p.split("/")[1];
    foot.append(sa);
  });
}
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60,
                    help="trading days shown in charts (table/KPI stay full-history); 0 = all")
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                    help="default 'days listed' floor for the grid + roster")
    ap.add_argument(
        "--history", default=str(SKILL_DIR / "state" / "history.csv"))
    ap.add_argument(
        "--outcomes", default=str(SKILL_DIR / "state" / "outcomes.csv"))
    ap.add_argument(
        "--sectors", default=str(SKILL_DIR / "state" / "sectors.json"))
    ap.add_argument("--out", default=str(SKILL_DIR / "state" / "history.html"))
    args = ap.parse_args()

    rows = load_history(Path(args.history))
    if not rows:
        raise SystemExit("history.csv is empty; run a scan first")
    outcomes = load_outcomes(Path(args.outcomes))
    check_ledger_convention(outcomes)
    payload = build_payload(rows, outcomes,
                            load_sectors(Path(args.sectors)), args.days)
    generated = datetime.now(
        timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    k = payload["kpi"]
    meta_desc = (
        f"Every pre-breakout base on the daily US large-cap watchlist, "
        f"{k['span'][0]} to {k['span'][1]} ({k['runs']} trading days, "
        f"{k['episodes']['n']} finished episodes): approach-to-pivot "
        "trajectories, a date-by-ticker maturity grid, validated-pocket vs "
        "rest trade expectancy and a sortable roster."
    )
    data_json = json.dumps(payload, separators=(
        ",", ":")).replace("</", r"<\/")
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    html = html.replace("__META_DESC__", meta_desc)
    html = html.replace("__GENERATED__", generated)
    html = html.replace("__MIN_DAYS__", str(args.min_days))
    out = Path(args.out)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, "
          f"{k['runs']} days x {k['tracked']} tickers, "
          f"{k['episodes']['n']} finished episodes)")


if __name__ == "__main__":
    main()
