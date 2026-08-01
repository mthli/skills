#!/usr/bin/env python3
"""Render state/history.csv + state/outcomes.csv into a self-contained
static HTML dashboard.

Reads the mean-reversion run history, the outcomes ledger and the sector
cache and writes a single HTML file (no external assets, no network) with:

  - a KPI row (span, resolved signals + win rate, expectancy/signal,
    ⭐ pocket expectancy vs its backtest reference, latest signal breadth,
    currently stuck-oversold names)
  - a signal-breadth stacked column chart, each day's emitted signals
    colored by eventual outcome, with the backtest's thin/washout cutoffs
  - a date × ticker outcome grid (the MR counterpart of momentum-scan's
    rank heatmap: cell color = that day's signal outcome, dot = ⭐ pocket
    day, long unbroken rows = stuck oversold) with a min-days filter
  - a ⭐-pocket-vs-rest running-expectancy chart against the backtest's
    in-sample reference values
  - a per-ticker summary table (the no-hover fallback for every value)
  - an English / 简体中文 / 繁體中文 / 日本語 / 한국어 language menu (top-right;
    choice kept in localStorage, first visit follows the browser language)
  - SEO meta tags (description + Open Graph + Twitter card) filled from the
    actual data span at render time

The chart semantics deliberately DIFFER from momentum-scan's renderer:
momentum tells a persistence story (long streak = durable winner), so it
draws rank trajectories; mean reversion tells an event + outcome story
(every signal resolves within days, long streak = failed thesis), so this
page draws outcomes and puts the validated ⭐ pocket's out-of-sample
performance on screen. No rank chart on purpose — MR daily ranks are noise.

Usage:
    python scripts/render_history_html.py [--days 60] [--out state/history.html]
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# Mirrored from scan.py's constants of the same names (source of truth;
# test_render_html.py has a drift-guard test asserting equality).
# Duplicated numerically because this script deliberately stays
# stdlib-only while scan.py imports yfinance at module level.
VALIDATED_MIN_SCORE = 40.0
VALIDATED_MAX_STREAK = 2
BREADTH_THIN_MAX = 30
BREADTH_WASHOUT_MIN = 60
# Mirrors scan.py's --persistent-min-streak argparse default (no module
# constant to drift-guard against; change both together).
STUCK_MIN_STREAK = 3
# Mirrors scan.py's TARGET_WINDOW_DAYS (drift-guard tested): signals from
# the last N run days without a ledger row are OPEN (in flight), older ones
# are UNRESOLVED (no price data reached them — delisted ticker or a ledger
# gap that needs --backfill-outcomes).
TARGET_WINDOW_DAYS = 5
# In-sample reference expectancies from the 2026-05→07 outcome backtest
# (references/backtest-findings.md #1 and #2) — drawn as dashed reference
# lines so the pocket chart answers "is the validated edge still paying
# out-of-sample". Re-validate quarterly alongside the backtest re-run.
BACKTEST_POCKET_EXPECT = 1.83
BACKTEST_BASELINE_EXPECT = 0.68


def load_history(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_outcomes(path: Path) -> dict[tuple[str, str], dict]:
    """(run_id, ticker) → {outcome, days_to_resolve, result_pct}. A missing
    file just means every signal renders OPEN/UNRESOLVED — warn, since the
    usual cause is a never-seeded ledger (--backfill-outcomes)."""
    if not path.exists():
        print(f"WARNING: outcomes ledger not found at {path}; every cell "
              f"will render as open/unresolved. Seed it with "
              f"scan.py --backfill-outcomes.", file=sys.stderr)
        return {}
    with open(path, newline="") as f:
        return {(r["run_id"], r["ticker"]): r for r in csv.DictReader(f)}


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


def build_payload(rows: list[dict], outcomes: dict, sectors: dict,
                  days_window: int = 0,
                  target_window_days: int = TARGET_WINDOW_DAYS) -> dict:
    run_ids = sorted({r["run_id"] for r in rows})
    day_idx = {rid: i for i, rid in enumerate(run_ids)}
    day_labels = [f"{rid[4:6]}-{rid[6:8]}" for rid in run_ids]
    n_days = len(run_ids)
    # Charts render only the trailing window; summary/KPI stay full-history.
    win_start = n_days - \
        days_window if 0 < days_window < n_days else 0

    by_ticker: dict[str, dict] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], {})[r["run_id"]] = r

    def sector_of(t: str) -> str:
        return sectors.get(t, {}).get("sector", "Unknown")

    def outcome_cat(rid: str, t: str) -> tuple[str, dict | None]:
        """W / L / E from the ledger; O (in flight) for recent misses,
        U (unresolved — no price data reached it) for old ones."""
        o = outcomes.get((rid, t))
        if o:
            return o["outcome"][0], o  # WON→W, LOST→L, EXPIRED→E
        if day_idx[rid] >= n_days - target_window_days:
            return "O", None
        return "U", None

    series = []
    summary = []
    # Full-history per-day outcome stacks + per-day pocket/base resolved
    # results (for the cumulative expectancy lines).
    breadth_full = [{"W": 0, "L": 0, "E": 0, "O": 0, "U": 0}
                    for _ in run_ids]
    pocket_by_day: list[list[float]] = [[] for _ in run_ids]
    base_by_day: list[list[float]] = [[] for _ in run_ids]

    today_pocket: list[str] = []
    for t, runs in by_ticker.items():
        ds = sorted(day_idx[rid] for rid in runs)
        dset = set(ds)
        spells = sum(1 for d in ds if d - 1 not in dset)
        pts = []
        w = l = e = 0
        results = []
        pocket_days = 0
        for rid, r in sorted(runs.items()):
            d = day_idx[rid]
            streak = 1
            while d - streak in dset:
                streak += 1
            score = _f(r.get("score"))
            pocket = bool(score is not None
                          and score >= VALIDATED_MIN_SCORE
                          and streak <= VALIDATED_MAX_STREAK)
            cat, o = outcome_cat(rid, t)
            pct = _f(o["result_pct"]) if o else None
            dtr = None
            if o and o.get("days_to_resolve"):
                dtr = int(float(o["days_to_resolve"]))
            breadth_full[d][cat] += 1
            if cat == "W":
                w += 1
            elif cat == "L":
                l += 1
            elif cat == "E":
                e += 1
            if pct is not None and cat in "WLE":
                results.append(pct)
                (pocket_by_day if pocket else base_by_day)[d].append(pct)
            if pocket:
                pocket_days += 1
                if d == n_days - 1:
                    today_pocket.append(t)
            pts.append({
                "d": d, "o": cat, "p": 1 if pocket else 0, "k": streak,
                "sc": score, "rsi": _f(r.get("rsi2")),
                "dtr": dtr, "pct": pct,
            })
        win_pts = [{**p, "d": p["d"] - win_start}
                   for p in pts if p["d"] >= win_start]
        if win_pts:
            series.append({"t": t, "sec": sector_of(t),
                           "pts": win_pts, "days": len(ds)})
        last_pt = pts[-1]
        n_dec = w + l
        summary.append({
            "t": t, "sec": sector_of(t),
            "days": len(ds), "spells": spells, "pd": pocket_days,
            "w": w, "l": l, "e": e,
            "win": round(w / n_dec * 100) if n_dec else None,
            "exp": round(sum(results) / len(results), 2) if results else None,
            "tot": round(sum(results), 1) if results else None,
            "first": day_labels[ds[0]], "firstD": ds[0],
            "last": day_labels[ds[-1]], "lastD": ds[-1],
            "st": last_pt["o"],
            # Live streak only when listed on the latest run — feeds the
            # stuck-oversold KPI (streak is a warning in MR, not a merit).
            "stk": last_pt["k"] if ds[-1] == n_days - 1 else 0,
        })

    summary.sort(key=lambda s: (-s["days"], -s["lastD"], s["t"]))
    # Grid row order matches the roster's default: expectancy desc, so the
    # two adjacent panels read in ONE direction (winners' timelines on top,
    # bleeders clustered at the bottom); names with nothing resolved sink
    # below, ordered by days listed.
    exp_of = {r["t"]: r["exp"] for r in summary}
    series.sort(key=lambda s: (
        exp_of[s["t"]] is None,
        -(exp_of[s["t"]] or 0),
        -s["days"],
        s["t"],
    ))

    # Cumulative expectancy lines (running mean %/signal by signal day).
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

    latest_total = sum(breadth_full[-1].values())
    if latest_total < BREADTH_THIN_MAX:
        latest_tier = "thin"
    elif latest_total > BREADTH_WASHOUT_MIN:
        latest_tier = "washout"
    else:
        latest_tier = "normal"

    stuck = sorted((s["t"] for s in summary if s["stk"] >= STUCK_MIN_STREAK))

    n_won = sum(d["W"] for d in breadth_full)
    n_lost = sum(d["L"] for d in breadth_full)
    n_exp = sum(d["E"] for d in breadth_full)
    n_resolved = n_won + n_lost + n_exp
    all_results = [p for day in (pocket_by_day + base_by_day)
                   for p in day]

    return {
        "days": day_labels[win_start:],
        "window": {"total": n_days, "shown": n_days - win_start},
        "series": series,
        "summary": summary,
        "breadth": {
            "perDay": [[d["W"], d["L"], d["E"], d["O"], d["U"]]
                       for d in breadth_full[win_start:]],
            "thin": BREADTH_THIN_MAX, "washout": BREADTH_WASHOUT_MIN,
        },
        "pocket": {
            "pkt": pkt_line[win_start:], "pktN": pkt_n[win_start:],
            "base": base_line[win_start:], "baseN": base_n[win_start:],
            "refPkt": BACKTEST_POCKET_EXPECT,
            "refBase": BACKTEST_BASELINE_EXPECT,
            "minScore": VALIDATED_MIN_SCORE,
            "maxStreak": VALIDATED_MAX_STREAK,
        },
        "kpi": {
            "runs": n_days,
            "span": [f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"
                     for rid in (run_ids[0], run_ids[-1])],
            "resolved": {
                "n": n_resolved, "w": n_won, "l": n_lost, "e": n_exp,
                "rate": round(n_won / (n_won + n_lost) * 100)
                if (n_won + n_lost) else None,
            },
            "exp": round(sum(all_results) / len(all_results), 2)
            if all_results else None,
            "pexp": {
                "v": pkt_line[-1] if pkt_line else None,
                "n": pkt_n[-1] if pkt_n else 0,
            },
            "latestBreadth": {"n": latest_total, "tier": latest_tier},
            "todayPocket": sorted(today_pocket),
            "stuck": stuck,
            "tracked": len(by_ticker),
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mean-reversion-scan history</title>
<meta name="description" content="__META_DESC__">
<meta property="og:type" content="website">
<meta property="og:site_name" content="mean-reversion-scan">
<meta property="og:title" content="mean-reversion-scan history">
<meta property="og:description" content="__META_DESC__">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="mean-reversion-scan history">
<meta name="twitter:description" content="__META_DESC__">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎯</text></svg>">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --ctx-line: #c9c8c1;
  /* Outcome palette — validated with dataviz's validate_palette.js under
     --pairs all on both surfaces (grid cells can neighbor any pair):
     W/L/O worst-pair deutan ΔE 14.9 light / 13.4 dark, all ≥ 3:1 except
     the deliberate neutrals. EXPIRED is intentionally gray ("nothing
     happened" — the diverging-midpoint role, not a series), and UNRES is
     the faint below-cutoff wash; both lean on the legend + roster table
     per the relief rule. Green=won/red=lost matches the skill's own
     🟢/🔴 glyph language (US convention), stated in every legend. */
  --oW: #0ca30c; --oL: #a02525; --oO: #2a78d6;
  --oE: #b5b4ad; --oU: #eceae4;
  --accent: #2a78d6;
  --tpos: #006300; --tneg: #a02525;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --ctx-line: #47463f;
    --oW: #0eb30e; --oL: #b83636; --oO: #3987e5;
    --oE: #55544d; --oU: #262624;
    --accent: #3987e5;
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
.vclip { max-height: 540px; overflow-y: auto; }
.gridhead { position: sticky; top: 0; z-index: 1; background: var(--surface); width: max-content; }
svg { display: block; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); }
svg text.tick { font-variant-numeric: tabular-nums; }
svg text.dlabel { font-size: 11.5px; font-weight: 600; fill: var(--ink-2); }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 0; font-size: 12.5px; color: var(--ink-2); align-items: center; }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .line { width: 14px; height: 2px; border-radius: 1px; }
.legend .rect { width: 11px; height: 11px; border-radius: 3px; }
/* Flexbox centers each key against the text's LINE box, which reserves a
   descender's worth of space the mark should not be centered against. Align
   to the letters instead: at 12.5px system-ui the cap band's center sits
   0.78px above the line box's, the CJK ideographic square's 0.58px above,
   so one pixel puts both within half a pixel. Measure against cap height,
   never against a word's own ink — a label without a descender and one with
   it would each want a different offset. */
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
  color: var(--ink-2); max-width: 340px;
}
#tip .h { display: flex; align-items: center; gap: 6px; }
#tip .v { color: var(--ink); font-weight: 600; font-size: 13.5px; }
#tip .k { width: 11px; height: 11px; border-radius: 3px; flex: none; }
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
      <h1><a href="https://github.com/mthli/skills/tree/master/mean-reversion-scan" target="_blank" rel="noopener">mean-reversion-scan</a> <span id="h1-suffix">history</span></h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <select id="lang-menu" aria-label="Language"></select>
  </div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2 id="br-title">Signal breadth × outcome</h2>
    <p class="note" id="br-note"></p>
    <div class="scroll" id="brchart"></div>
    <div class="legend" id="br-legend"></div>
  </div>

  <div class="card">
    <h2 id="pk-title">⭐ Pocket vs the rest</h2>
    <p class="note" id="pk-note"></p>
    <div class="scroll" id="pkchart"></div>
    <div class="legend" id="pk-legend"></div>
  </div>

  <div class="card">
    <div class="head">
      <div>
        <h2 id="grid-title">Outcome grid</h2>
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

// ---- i18n ----
const I18N = {
  en: {
    htmlLang: "en",
    title: "mean-reversion-scan history",
    h1Suffix: "history",
    subtitle: (a, b, runs) => `${a} → ${b} · ${runs} trading days · every oversold signal's outcome: won / lost / expired`,
    winTag: (s, t) => ` Charts show the last ${s} of ${t} trading days.`,
    kToday: "Today's ⭐ pocket",
    resFilterLabel: "Filter by resolved count",
    geRes: n => `≥${n} resolved`,
    kResolved: "Resolved signals",
    kResolvedSub: (w, l, e, r) => `${w}W / ${l}L / ${e} expired${r !== null ? ` · win rate ${r}%` : ""}`,
    kExp: "Expectancy / signal",
    kExpSub: b => `backtest baseline +${b}% (in-sample)`,
    kPocket: "⭐ Pocket expectancy",
    kPocketSub: (n, r) => `n=${n} · backtest +${r}%`,
    kBreadth: "Latest breadth",
    tierName: { thin: "THIN", normal: "NORMAL", washout: "WASHOUT" },
    tierSub: {
      thin: "isolated oversold: research-only tape",
      normal: "between the backtest's cutoffs",
      washout: "market-wide panic: best regime in-sample",
    },
    kStuck: "Stuck oversold now",
    none: "none",
    brTitle: "Signal breadth × outcome",
    brNote: (thin, wo) => `One column per day: all of that day's signals, colored by how they ended.\nTwo dashed lines: under ${thin} (thin) the selling is isolated and tends to keep falling; over ${wo} (washout) the panic is market-wide and tends to snap back.`,
    gridTitle: "Outcome grid",
    gridNote: () => `One row per name, one cell per listed day, color = that day's outcome; a center dot = ⭐ pocket day (Score ≥ ${DATA.pocket.minScore}, day ≤ ${DATA.pocket.maxStreak}).\nRows sorted by expectancy, best first (matching the roster); nothing-resolved names sink. Row-end = expectancy (%/signal).\nRows running ≥ __STUCK_MIN_STREAK__ days unbroken = stuck oversold, a warning, not a bargain.`,
    all: "All",
    pkTitle: "⭐ Pocket vs the rest",
    pkNote: "Solid lines: the running expectancy (avg %/signal to date) of ⭐ pocket signals vs the rest.\nDashed lines: the backtest references. Pocket holding above its dash = the validated edge still pays.",
    pkPocket: "⭐ Pocket", pkBase: "The rest",
    pkRef: v => `backtest +${v}%`,
    pkTipN: n => `${n} resolved`,
    rosterTitle: "Roster",
    rosterNote: "One row per name that ever signaled. Click a header to sort; click again to reverse. Every hover value from the charts is readable here.\nExpectancy = avg %/signal, the column to judge by; the win rate runs hot by construction, and 100% can still lose money. Status = the latest signal's state.",
    cols: ["Ticker", "Sector", "Expect %/sig", "Total %", "W / L / Exp", "Win rate", "⭐ days", "Days", "Last seen", "Status"],
    oc: { W: "Won", L: "Lost", E: "Expired", O: "Open", U: "No data" },
    ocTip: {
      W: (p, d) => `Won +${p}% in ${d} day(s)`,
      L: (p, d) => `Lost ${p}% in ${d} day(s)`,
      E: p => `Expired flat (${p >= 0 ? "+" : ""}${p}% drift)`,
      O: () => "Open: inside the target window",
      U: () => "Unresolved: no price data reached it",
    },
    spellDay: k => `day ${k} of the spell`,
    pocketDay: "⭐ pocket day",
    score: "Score",
    dayLine: n => `${n} signals total`,
    genBy: "Generated by ", genAt: t => ` at ${t} · Source: `,
    sectorNames: {},
  },
  zh: {
    htmlLang: "zh-CN",
    title: "mean-reversion-scan 历史",
    h1Suffix: "历史",
    subtitle: (a, b, runs) => `${a} → ${b} · 共 ${runs} 个交易日 · 每个超卖信号的最终结局（赢 / 输 / 过期）`,
    winTag: (s, t) => `图表仅显示最近 ${s} / ${t} 个交易日。`,
    kToday: "今日 ⭐ 口袋",
    resFilterLabel: "按已结算单数筛选",
    geRes: n => `已结算 ≥ ${n} 单`,
    kResolved: "已结算信号",
    kResolvedSub: (w, l, e, r) => `${w} 赢 / ${l} 输 / ${e} 过期${r !== null ? ` · 胜率 ${r}%` : ""}`,
    kExp: "每信号期望",
    kExpSub: b => `回测基线 +${b}%（样本内）`,
    kPocket: "⭐ 口袋期望",
    kPocketSub: (n, r) => `样本 ${n} · 回测 +${r}%`,
    kBreadth: "最新信号广度",
    tierName: { thin: "接刀", normal: "正常", washout: "洗盘" },
    tierSub: {
      thin: "孤立超卖：只研究、别动手",
      normal: "处于回测两条档位线之间",
      washout: "全市场恐慌：回测中的最佳环境",
    },
    kStuck: "当前卡死名单",
    none: "无",
    brTitle: "信号广度 × 结局",
    brNote: (thin, wo) => `每天一根柱：当天的全部信号，颜色 = 最终结局。\n两条虚线：矮过 ${thin}（接刀线）= 零星下跌，往往继续跌，别接；高过 ${wo}（洗盘线）= 全场恐慌，反而最容易弹回来。`,
    gridTitle: "结局网格",
    gridNote: () => `一行一只票，一格一个上榜日，颜色 = 那天的结局；带中心点 = ⭐ 口袋日（Score ≥ ${DATA.pocket.minScore} 且上榜 ≤ ${DATA.pocket.maxStreak} 天）。\n行序按期望从高到低（与名录一致），无结算的沉底；行尾 = 该票期望（%/单）。\n连续 ≥ __STUCK_MIN_STREAK__ 天的长行 = 卡死超卖，是警告不是便宜货。`,
    all: "全部",
    pkTitle: "⭐ 口袋 vs 其余",
    pkNote: "实线：⭐ 口袋与其余信号各自的滚动期望（截至当日平均每单盈亏 %）。\n虚线：回测参考值。口袋线稳在自己虚线上方 = 验证过的边际还在兑现。",
    pkPocket: "⭐ 口袋", pkBase: "其余信号",
    pkRef: v => `回测 +${v}%`,
    pkTipN: n => `已结算 ${n} 个`,
    rosterTitle: "信号名录",
    rosterNote: "每个发过信号的标的一行。点击表头排序；再次点击反向。图表中所有悬停数值在此均可查阅。\n期望 = 平均每单盈亏 %，挑票看这列；胜率天生虚高，100% 胜率也可能在亏钱。状态 = 最近一次信号的状态。",
    cols: ["代码", "行业", "期望 %/信号", "累计 %", "赢 / 输 / 过期", "胜率", "⭐ 天数", "上榜天数", "最近上榜", "状态"],
    oc: { W: "赢", L: "输", E: "过期", O: "在途", U: "无数据" },
    ocTip: {
      W: (p, d) => `赢：+${p}%，${d} 天到目标`,
      L: (p, d) => `输：${p}%，${d} 天打到止损`,
      E: p => `过期：目标和止损都没碰到（漂移 ${p >= 0 ? "+" : ""}${p}%）`,
      O: () => "在途：仍在目标窗口内",
      U: () => "未结算：行情数据没覆盖到",
    },
    spellDay: k => `上榜第 ${k} 天`,
    pocketDay: "⭐ 口袋日",
    score: "评分",
    dayLine: n => `共 ${n} 个信号`,
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
    title: "mean-reversion-scan 歷史",
    h1Suffix: "歷史",
    subtitle: (a, b, runs) => `${a} → ${b} · 共 ${runs} 個交易日 · 每個超賣訊號的最終結局（贏 / 輸 / 過期）`,
    winTag: (s, t) => `圖表僅顯示最近 ${s} / ${t} 個交易日。`,
    kToday: "今日 ⭐ 口袋",
    resFilterLabel: "按已結算單數篩選",
    geRes: n => `已結算 ≥ ${n} 單`,
    kResolved: "已結算訊號",
    kResolvedSub: (w, l, e, r) => `${w} 贏 / ${l} 輸 / ${e} 過期${r !== null ? ` · 勝率 ${r}%` : ""}`,
    kExp: "每訊號期望",
    kExpSub: b => `回測基線 +${b}%（樣本內）`,
    kPocket: "⭐ 口袋期望",
    kPocketSub: (n, r) => `樣本 ${n} · 回測 +${r}%`,
    kBreadth: "最新訊號廣度",
    tierName: { thin: "接刀", normal: "正常", washout: "洗盤" },
    tierSub: {
      thin: "孤立超賣：只研究、別動手",
      normal: "處於回測兩條檔位線之間",
      washout: "全市場恐慌：回測中的最佳環境",
    },
    kStuck: "目前卡死名單",
    none: "無",
    brTitle: "訊號廣度 × 結局",
    brNote: (thin, wo) => `每天一根柱：當天的全部訊號，顏色 = 最終結局。\n兩條虛線：矮過 ${thin}（接刀線）= 零星下跌，往往繼續跌，別接；高過 ${wo}（洗盤線）= 全場恐慌，反而最容易彈回來。`,
    gridTitle: "結局網格",
    gridNote: () => `一行一檔票，一格一個上榜日，顏色 = 那天的結局；帶中心點 = ⭐ 口袋日（Score ≥ ${DATA.pocket.minScore} 且上榜 ≤ ${DATA.pocket.maxStreak} 天）。\n行序按期望從高到低（與名錄一致），無結算的沉底；行尾 = 該檔期望（%/單）。\n連續 ≥ __STUCK_MIN_STREAK__ 天的長行 = 卡死超賣，是警告不是便宜貨。`,
    all: "全部",
    pkTitle: "⭐ 口袋 vs 其餘",
    pkNote: "實線：⭐ 口袋與其餘訊號各自的滾動期望（截至當日平均每單盈虧 %）。\n虛線：回測參考值。口袋線穩在自己虛線上方 = 驗證過的邊際還在兌現。",
    pkPocket: "⭐ 口袋", pkBase: "其餘訊號",
    pkRef: v => `回測 +${v}%`,
    pkTipN: n => `已結算 ${n} 個`,
    rosterTitle: "訊號名錄",
    rosterNote: "每個發過訊號的標的一行。點擊表頭排序；再次點擊反向。圖表中所有懸停數值在此均可查閱。\n期望 = 平均每單盈虧 %，挑票看這欄；勝率天生虛高，100% 勝率也可能在虧錢。狀態 = 最近一次訊號的狀態。",
    cols: ["代號", "產業", "期望 %/訊號", "累計 %", "贏 / 輸 / 過期", "勝率", "⭐ 天數", "上榜天數", "最近上榜", "狀態"],
    oc: { W: "贏", L: "輸", E: "過期", O: "在途", U: "無數據" },
    ocTip: {
      W: (p, d) => `贏：+${p}%，${d} 天到目標`,
      L: (p, d) => `輸：${p}%，${d} 天打到止損`,
      E: p => `過期：目標和止損都沒碰到（漂移 ${p >= 0 ? "+" : ""}${p}%）`,
      O: () => "在途：仍在目標窗口內",
      U: () => "未結算：行情數據沒覆蓋到",
    },
    spellDay: k => `上榜第 ${k} 天`,
    pocketDay: "⭐ 口袋日",
    score: "評分",
    dayLine: n => `共 ${n} 個訊號`,
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
    title: "mean-reversion-scan 履歴",
    h1Suffix: "履歴",
    subtitle: (a, b, runs) => `${a} → ${b} · 全 ${runs} 営業日 · 各売られすぎシグナルの最終結果（勝ち / 負け / 期限切れ）`,
    winTag: (s, t) => `チャートは直近 ${s} / ${t} 営業日のみ表示。`,
    kToday: "本日の ⭐ ポケット",
    resFilterLabel: "確定件数で絞り込み",
    geRes: n => `確定 ${n} 件以上`,
    kResolved: "確定シグナル数",
    kResolvedSub: (w, l, e, r) => `${w} 勝 / ${l} 敗 / ${e} 期限切れ${r !== null ? ` · 勝率 ${r}%` : ""}`,
    kExp: "シグナルあたり期待値",
    kExpSub: b => `バックテスト基準 +${b}%（イン・サンプル）`,
    kPocket: "⭐ ポケット期待値",
    kPocketSub: (n, r) => `n=${n} · バックテスト +${r}%`,
    kBreadth: "最新シグナル数",
    tierName: { thin: "THIN", normal: "NORMAL", washout: "WASHOUT" },
    tierSub: {
      thin: "孤立した売られすぎ：リサーチのみ",
      normal: "バックテストの 2 つのカットオフの間",
      washout: "市場全体のパニック：イン・サンプルで最良の環境",
    },
    kStuck: "現在の停滞銘柄",
    none: "なし",
    brTitle: "シグナル数 × 結果",
    brNote: (thin, wo) => `1 日 1 本の柱：その日の全シグナル、色 = 最終結果。\n破線は 2 本：${thin} 未満（thin）なら散発的な下げでまだ下がりやすく、${wo} 超（washout）なら市場全体のパニックでかえって反発しやすい。`,
    gridTitle: "結果グリッド",
    gridNote: () => `1 行 = 1 銘柄、1 セル = リスト入り 1 日、色 = その日の結果。中心の点 = ⭐ ポケット日（Score ≥ ${DATA.pocket.minScore} かつ ${DATA.pocket.maxStreak} 日目以内）。\n行は期待値の高い順（一覧表と同じ）。確定なしは下へ。行末 = 期待値（%/シグナル）。\n__STUCK_MIN_STREAK__ 日以上続く行は停滞した売られすぎ。警告であり掘り出し物ではない。`,
    all: "すべて",
    pkTitle: "⭐ ポケット vs その他",
    pkNote: "実線：⭐ ポケットとその他それぞれのローリング期待値（当日までの平均損益 %/シグナル）。\n破線：バックテストの参考値。ポケット線が自身の破線の上にあれば、検証済みのエッジは健在。",
    pkPocket: "⭐ ポケット", pkBase: "その他",
    pkRef: v => `バックテスト +${v}%`,
    pkTipN: n => `確定 ${n} 件`,
    rosterTitle: "銘柄一覧",
    rosterNote: "シグナルが出たことのある銘柄を 1 行ずつ表示。ヘッダーをクリックでソート、もう一度クリックで逆順。チャートのホバー数値はすべてこの表で確認できます。\n期待値 = 平均損益 %/シグナル。銘柄選びはこの列で。勝率は構造的に高く出るため、100% でも損をしていることがある。ステータス = 直近シグナルの状態。",
    cols: ["ティッカー", "セクター", "期待値 %/シグナル", "累計 %", "勝 / 敗 / 期限切れ", "勝率", "⭐ 日数", "日数", "直近登場", "ステータス"],
    oc: { W: "勝ち", L: "負け", E: "期限切れ", O: "進行中", U: "データなし" },
    ocTip: {
      W: (p, d) => `勝ち：+${p}%、${d} 日で目標到達`,
      L: (p, d) => `負け：${p}%、${d} 日でストップ到達`,
      E: p => `期限切れ：目標もストップも未到達（ドリフト ${p >= 0 ? "+" : ""}${p}%）`,
      O: () => "進行中：ターゲット期間内",
      U: () => "未確定：価格データが届いていない",
    },
    spellDay: k => `リスト入り ${k} 日目`,
    pocketDay: "⭐ ポケット日",
    score: "スコア",
    dayLine: n => `全 ${n} シグナル`,
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
    title: "mean-reversion-scan 히스토리",
    h1Suffix: "히스토리",
    subtitle: (a, b, runs) => `${a} → ${b} · 총 ${runs}거래일 · 각 과매도 신호의 최종 결과 (승 / 패 / 만료)`,
    winTag: (s, t) => `차트는 최근 ${s} / ${t}거래일만 표시합니다.`,
    kToday: "오늘의 ⭐ 포켓",
    resFilterLabel: "확정 건수로 필터",
    geRes: n => `확정 ${n}건 이상`,
    kResolved: "확정 신호 수",
    kResolvedSub: (w, l, e, r) => `${w}승 / ${l}패 / ${e} 만료${r !== null ? ` · 승률 ${r}%` : ""}`,
    kExp: "신호당 기대값",
    kExpSub: b => `백테스트 기준 +${b}% (인샘플)`,
    kPocket: "⭐ 포켓 기대값",
    kPocketSub: (n, r) => `n=${n} · 백테스트 +${r}%`,
    kBreadth: "최신 신호 수",
    tierName: { thin: "THIN", normal: "NORMAL", washout: "WASHOUT" },
    tierSub: {
      thin: "고립된 과매도: 리서치 전용",
      normal: "백테스트 컷오프 사이",
      washout: "시장 전체 패닉: 인샘플 최고 환경",
    },
    kStuck: "현재 정체 종목",
    none: "없음",
    brTitle: "신호 수 × 결과",
    brNote: (thin, wo) => `하루 1개 기둥: 그날의 모든 신호, 색 = 최종 결과.\n점선 2개: ${thin} 미만(thin)이면 산발적 하락이라 더 떨어지기 쉽고, ${wo} 초과(washout)면 시장 전체 패닉이라 오히려 반등하기 쉽습니다.`,
    gridTitle: "결과 그리드",
    gridNote: () => `1행 = 1종목, 1셀 = 등재 1일, 색 = 그날의 결과. 중심의 점 = ⭐ 포켓일 (Score ≥ ${DATA.pocket.minScore}, 등재 ${DATA.pocket.maxStreak}일 이내).\n행은 기대값 높은 순(목록과 동일), 확정 없는 종목은 아래로. 행 끝 = 기대값(%/신호).\n__STUCK_MIN_STREAK__일 이상 이어지는 행은 정체된 과매도, 경고이지 헐값이 아닙니다.`,
    all: "전체",
    pkTitle: "⭐ 포켓 vs 나머지",
    pkNote: "실선: ⭐ 포켓과 나머지 각각의 롤링 기대값(현재까지 평균 손익 %/신호).\n점선: 백테스트 참고값. 포켓 선이 자기 점선 위에 있으면 검증된 엣지가 유효합니다.",
    pkPocket: "⭐ 포켓", pkBase: "나머지",
    pkRef: v => `백테스트 +${v}%`,
    pkTipN: n => `확정 ${n}건`,
    rosterTitle: "종목 목록",
    rosterNote: "신호가 나온 적 있는 종목을 한 행씩 표시. 헤더를 클릭해 정렬, 다시 클릭하면 역순. 차트의 모든 호버 값을 이 표에서 확인할 수 있습니다.\n기대값 = 평균 손익 %/신호. 종목은 이 열로 판단하세요. 승률은 구조적으로 높게 나와 100%여도 손해일 수 있습니다. 상태 = 최근 신호의 상태.",
    cols: ["티커", "섹터", "기대값 %/신호", "누적 %", "승 / 패 / 만료", "승률", "⭐ 일수", "일수", "최근 등재", "상태"],
    oc: { W: "승", L: "패", E: "만료", O: "진행 중", U: "데이터 없음" },
    ocTip: {
      W: (p, d) => `승: +${p}%, ${d}일 만에 목표 도달`,
      L: (p, d) => `패: ${p}%, ${d}일 만에 손절 도달`,
      E: p => `만료: 목표도 손절도 미도달 (드리프트 ${p >= 0 ? "+" : ""}${p}%)`,
      O: () => "진행 중: 목표 기간 내",
      U: () => "미확정: 가격 데이터가 닿지 않음",
    },
    spellDay: k => `등재 ${k}일째`,
    pocketDay: "⭐ 포켓일",
    score: "점수",
    dayLine: n => `총 ${n}개 신호`,
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
    const s = localStorage.getItem("meanReversionScanLang");
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
    try { localStorage.setItem("meanReversionScanLang", sel.value); } catch (e) {}
    location.reload();
  });
  document.getElementById("h1-suffix").textContent = T.h1Suffix;
  document.getElementById("br-title").textContent = T.brTitle;
  document.getElementById("grid-title").textContent = T.gridTitle;
  document.getElementById("pk-title").textContent = T.pkTitle;
  document.getElementById("roster-title").textContent = T.rosterTitle;
  document.getElementById("roster-note").textContent = T.rosterNote;
}

const DAYS = DATA.days.length;
const WIN_TAG = DATA.window.shown < DATA.window.total ? T.winTag(DATA.window.shown, DATA.window.total) : "";
// Outcome slot order everywhere (stacks bottom→top, legends, tips):
// decisive results first, then the neutral, then the undecided.
const OCATS = ["W", "L", "E", "O", "U"];
const OC_VAR = { W: "--oW", L: "--oL", E: "--oE", O: "--oO", U: "--oU" };
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
const pctTxt = v => (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
function ocLine(p) {
  // One tooltip/table line for a point's outcome, localized.
  if (p.o === "W" || p.o === "L") return T.ocTip[p.o](p.pct, p.dtr);
  if (p.o === "E") return T.ocTip.E(p.pct == null ? 0 : p.pct);
  return T.ocTip[p.o]();
}
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
  rows.slice(1).forEach(r => div(null, t, r));
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
  const r = k.resolved;
  tile(T.kResolved, `${r.n}`, T.kResolvedSub(r.w, r.l, r.e, r.rate));
  if (k.exp !== null)
    tile(T.kExp, pctTxt(k.exp), T.kExpSub(DATA.pocket.refBase));
  if (k.pexp.v !== null)
    tile(T.kPocket, pctTxt(k.pexp.v),
         T.kPocketSub(k.pexp.n, DATA.pocket.refPkt));
  const b = k.latestBreadth;
  tile(T.kBreadth, `${b.n} · ${T.tierName[b.tier]}`, T.tierSub[b.tier]);
  tile(T.kToday, `${k.todayPocket.length}`, tickerList(k.todayPocket));
  tile(T.kStuck, `${k.stuck.length}`, tickerList(k.stuck));
}

// ---- breadth stacked columns ----
{
  document.getElementById("br-note").textContent =
    T.brNote(DATA.breadth.thin, DATA.breadth.washout) + WIN_TAG;
  const ML = 34, MT = 12, MB = 26, DX = 19, BW = 13, PH = 170;
  // Keep both cutoff lines in frame even on a quiet stretch.
  const ymax = Math.max(DATA.breadth.washout + 10,
                        ...DATA.breadth.perDay.map(d => d.reduce((a, c) => a + c, 0)));
  // Right margin fits the widest cutoff label ("60 WASHOUT" ≈ 62px at 11px).
  const W = ML + DAYS * DX + 88, H = MT + PH + MB;
  const yOf = v => MT + PH - v / ymax * PH;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, document.getElementById("brchart"));
  const step = ymax > 120 ? 50 : ymax > 60 ? 25 : 10;
  for (let v = 0; v <= ymax; v += step) {
    el("line", { x1: ML - 4, x2: ML + DAYS * DX, y1: yOf(v), y2: yOf(v), stroke: "var(--grid)" }, svg);
    el("text", { x: ML - 8, y: yOf(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent = v;
  }
  [[DATA.breadth.thin, T.tierName.thin], [DATA.breadth.washout, T.tierName.washout]].forEach(([v, lbl]) => {
    el("line", { x1: ML - 4, x2: ML + DAYS * DX, y1: yOf(v), y2: yOf(v),
      stroke: "var(--axis)", "stroke-dasharray": "4 3" }, svg);
    el("text", { x: ML + DAYS * DX + 6, y: yOf(v) + 4, class: "tick" }, svg).textContent = `${v} ${lbl}`;
  });
  DATA.breadth.perDay.forEach((counts, d) => {
    let acc = 0;
    const x = ML + d * DX;
    counts.forEach((c, i) => {
      if (!c) return;
      const y1 = yOf(acc + c), y0 = yOf(acc);
      const r = el("rect", { x, y: y1 + 1, width: BW,
        height: Math.max(y0 - y1 - 2, 0.5), rx: 2,
        fill: `var(${OC_VAR[OCATS[i]]})` }, svg);
      r.dataset.d = d; r.dataset.i = i; r.dataset.c = c;
      acc += c;
    });
    if (d === DAYS - 1 || (d % 5 === 0 && DAYS - 1 - d >= 3))
      el("text", { x: x + BW / 2, y: H - 8, "text-anchor": "middle", class: "tick" }, svg).textContent = DATA.days[d];
  });
  const leg = document.getElementById("br-legend");
  OCATS.forEach(c => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${OC_VAR[c]})`;
    k.appendChild(document.createTextNode(T.oc[c]));
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.i !== undefined) {
      const cat = OCATS[+t.dataset.i], d = +t.dataset.d;
      const total = DATA.breadth.perDay[d].reduce((a, c) => a + c, 0);
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [`${T.oc[cat]} ${+t.dataset.c}`,
         `${DATA.days[d]} · ${T.dayLine(total)}`],
        getComputedStyle(document.documentElement).getPropertyValue(OC_VAR[cat])));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
}

// ---- outcome grid ----
const gridBox = document.getElementById("grid");
const EXP = new Map(DATA.summary.map(s => [s.t, s.exp]));
const RES = new Map(DATA.summary.map(s => [s.t, s.w + s.l + s.e]));
function renderGrid(minRes) {
  gridBox.textContent = "";
  const rows = DATA.series.filter(s => RES.get(s.t) >= minRes);
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
    const lt = el("text", { x: GL - 8, y: y + CH - 3, "text-anchor": "end", class: "tick" }, ra);
    lt.textContent = s.t;
    const ex = EXP.get(s.t);
    if (ex != null) {
      // The corrective number, placed where the seduction happens: dense
      // green rows up top read as "always bounces" while mostly running
      // a negative average. Inline style so it beats the muted svg-text rule.
      const et = el("text", { x: GL + DAYS * CW + 6, y: y + CH - 3, class: "tick" }, svg);
      et.style.fill = ex >= 0 ? "var(--tpos)" : "var(--tneg)";
      et.textContent = (ex >= 0 ? "+" : "") + ex.toFixed(1) + "%";
    }
    s.pts.forEach((p, pi) => {
      const r = el("rect", { x: GL + p.d * CW, y: y + 1, width: CW - 2, height: CH - 2, rx: 2,
        fill: `var(${OC_VAR[p.o]})` }, svg);
      r.dataset.t = s.t; r.dataset.i = pi;
      // ⭐ pocket day → surface-colored center dot (color-independent, so
      // it reads on every outcome fill and survives CVD).
      if (p.p) el("circle", { cx: GL + p.d * CW + (CW - 2) / 2, cy: y + CH / 2,
        r: 2, fill: "var(--surface)", "pointer-events": "none" }, svg);
    });
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.t) {
      const s = DATA.series.find(x => x.t === t.dataset.t);
      const p = s.pts[+t.dataset.i];
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [s.t + (p.p ? " ⭐" : ""),
         `${DATA.days[p.d]} · ${T.spellDay(p.k)}${p.p ? ` · ${T.pocketDay}` : ""}`,
         `RSI(2) ${p.rsi == null ? "—" : p.rsi.toFixed(1)} · ${T.score} ${p.sc == null ? "—" : p.sc.toFixed(0)}`,
         ocLine(p)],
        getComputedStyle(document.documentElement).getPropertyValue(OC_VAR[p.o])));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
}
{
  document.getElementById("grid-note").textContent = T.gridNote() + WIN_TAG;
  // Same filter dimension AND default as the roster, so the two panels
  // show the same rows in the same order.
  const sel = document.getElementById("grid-filter");
  sel.setAttribute("aria-label", T.resFilterLabel);
  new Map([[0, T.all], [1, T.geRes(1)], [3, T.geRes(3)]]).forEach((lbl, min) => {
    const o = document.createElement("option");
    o.value = min;
    o.textContent = lbl;
    sel.appendChild(o);
  });
  sel.value = 3;
  sel.addEventListener("change", () => renderGrid(+sel.value));
  renderGrid(+sel.value);
  const leg = document.getElementById("grid-legend");
  OCATS.forEach(c => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${OC_VAR[c]})`;
    k.appendChild(document.createTextNode(T.oc[c]));
  });
  const k = div("key", leg);
  const d = document.createElement("span"); d.className = "ocdot";
  d.style.background = "var(--oE)"; d.style.position = "relative";
  const dot = document.createElement("span");
  dot.style.cssText = "position:absolute;left:3px;top:3px;width:3px;height:3px;border-radius:50%;background:var(--surface)";
  d.appendChild(dot); k.appendChild(d);
  k.appendChild(document.createTextNode(T.pocketDay));
}

// ---- pocket vs rest cumulative expectancy ----
{
  document.getElementById("pk-note").textContent = T.pkNote + WIN_TAG;
  const ML = 40, MT = 12, MB = 26, DX = 19, PH = 160;
  const P = DATA.pocket;
  const allVals = [...P.pkt, ...P.base, P.refPkt, P.refBase, 0]
    .filter(v => v !== null);
  let lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad; hi += pad;
  // Right margin fits the widest CJK direct label ("其余信号 +0.39%" ≈ 130px).
  const W = ML + (DAYS - 1) * DX + 150, H = MT + PH + MB;
  const xOf = d => ML + d * DX, yOf = v => MT + (hi - v) / (hi - lo) * PH;
  const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, document.getElementById("pkchart"));
  // Gridlines on half-percent steps; the zero line gets the axis color.
  const step = (hi - lo) > 3 ? 1 : 0.5;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const zero = Math.abs(v) < 1e-9;
    el("line", { x1: ML - 4, x2: ML + (DAYS - 1) * DX, y1: yOf(v), y2: yOf(v),
      stroke: zero ? "var(--axis)" : "var(--grid)" }, svg);
    el("text", { x: ML - 8, y: yOf(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent =
      (v > 0 ? "+" : "") + (step < 1 ? v.toFixed(1) : v.toFixed(0)) + "%";
  }
  DATA.days.forEach((d, i) => {
    if (i === DAYS - 1 || (i % 5 === 0 && DAYS - 1 - i >= 3))
      el("text", { x: xOf(i), y: H - 8, "text-anchor": "middle", class: "tick" }, svg).textContent = d;
  });
  // In-sample reference dashes, labeled at the right edge with the line color
  // as the key (labels themselves stay in ink tokens).
  [[P.refPkt, "var(--accent)"], [P.refBase, "var(--ctx-line)"]].forEach(([v, col]) => {
    el("line", { x1: ML, x2: ML + (DAYS - 1) * DX, y1: yOf(v), y2: yOf(v),
      stroke: col, "stroke-dasharray": "4 3", opacity: 0.7 }, svg);
  });
  const lines = [
    { vals: P.pkt, ns: P.pktN, col: "var(--accent)", w: 2, lbl: T.pkPocket },
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
  [[P.refPkt, T.pkPocket, "var(--accent)"],
   [P.refBase, T.pkBase, "var(--ctx-line)"]].forEach(([v, lbl, col]) => {
    const k = div("key", leg);
    const l = div("line", k);
    // Same color as that series' on-chart dash, dashed the same way.
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

// ---- roster table ----
{
  const tbl = document.getElementById("tbl");
  // Column order follows the judgment path: identity, verdict (expectancy
  // + total), the evidence behind it (record + win rate), exposure, then
  // recency/state. Defaults pair the verdict sort with the ≥3-resolved
  // filter — without the filter an expectancy sort tops out on
  // single-lucky-win samples (the grid keeps the days view; two panels,
  // two roles).
  const COLS = [
    { h: T.cols[0], v: s => s.t,      dir: 1 },
    { h: T.cols[1], v: s => s.sec,    dir: 1 },
    { h: T.cols[2], v: s => s.exp,    dir: -1 },
    { h: T.cols[3], v: s => s.tot,    dir: -1 },
    { h: T.cols[4], v: s => s.w + s.l + s.e, dir: -1 },
    { h: T.cols[5], v: s => s.win,    dir: -1 },
    { h: T.cols[6], v: s => s.pd || null, dir: -1 },
    { h: T.cols[7], v: s => s.days,   dir: -1 },
    { h: T.cols[8], v: s => s.lastD,  dir: -1 },
    // Sort by the outcome slot order (won, lost, expired, open, no-data),
    // not the alphabet of the category codes.
    { h: T.cols[9], v: s => OCATS.indexOf(s.st), dir: 1 },
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

  let sortCol = 2, sortDir = -1, minRes = 3;
  const renderHead = () => ths.forEach((th, i) => {
    th.textContent = COLS[i].h + (i === sortCol ? (sortDir === 1 ? " ▲" : " ▼") : "");
    th.className = i === sortCol ? "on" : "";
  });
  const renderRows = () => {
    tb.textContent = "";
    const v = COLS[sortCol].v;
    DATA.summary.filter(s => s.w + s.l + s.e >= minRes).sort((a, b) => {
      const x = v(a), y = v(b);
      const xn = x === null || x === undefined, yn = y === null || y === undefined;
      if (xn || yn) return xn && yn ? (b.days - a.days) : xn ? 1 : -1;  // nulls always sink
      const c = (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir;
      return c || (b.days - a.days) || (b.lastD - a.lastD);
    }).forEach(s => {
      const tr = document.createElement("tr");
      [s.t, secName(s.sec),
       s.exp !== null ? pctTxt(s.exp) : "—",
       s.tot !== null ? (s.tot >= 0 ? "+" : "") + s.tot.toFixed(1) + "%" : "—",
       `${s.w} / ${s.l} / ${s.e}`,
       s.win !== null ? s.win + "%" : "—",
       s.pd || "—", s.days, s.last, s.st]
      .forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 0) { td.className = "tk"; td.appendChild(tickerLink(c)); }
        else if (i === 9) {
          const d = document.createElement("span");
          d.className = "ocdot";
          d.style.background = `var(${OC_VAR[s.st]})`;
          d.title = T.oc[s.st];
          d.setAttribute("aria-label", T.oc[s.st]);
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

  // Resolved-count filter: opt-in guard so an expectancy sort isn't
  // topped by single-lucky-win small samples. Default shows the full
  // archive.
  const sel = document.getElementById("roster-filter");
  sel.setAttribute("aria-label", T.resFilterLabel);
  new Map([[0, T.all], [1, T.geRes(1)], [3, T.geRes(3)]]).forEach((lbl, min) => {
    const o = document.createElement("option");
    o.value = min;
    o.textContent = lbl;
    sel.appendChild(o);
  });
  sel.value = minRes;
  sel.addEventListener("change", () => { minRes = +sel.value; renderRows(); });

  const leg = document.getElementById("roster-legend");
  OCATS.forEach(c => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = `var(${OC_VAR[c]})`;
    k.appendChild(document.createTextNode(T.oc[c]));
  });
}

{
  const foot = document.getElementById("foot");
  foot.append(T.genBy);
  const fa = document.createElement("a");
  fa.href = "https://github.com/mthli/skills/blob/master/mean-reversion-scan/scripts/render_history_html.py";
  fa.target = "_blank"; fa.rel = "noopener";
  fa.textContent = "render_history_html.py";
  foot.append(fa, T.genAt(GENERATED));
  ["state/history.csv", "state/outcomes.csv"].forEach((p, i) => {
    if (i) foot.append(" + ");
    const sa = document.createElement("a");
    sa.href = "https://github.com/mthli/skills/blob/master/mean-reversion-scan/" + p;
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
    ap.add_argument("--target-window-days", type=int,
                    default=TARGET_WINDOW_DAYS,
                    help="signals from the last N run days without a ledger "
                         "row render as OPEN instead of UNRESOLVED")
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
    payload = build_payload(rows, load_outcomes(Path(args.outcomes)),
                            load_sectors(Path(args.sectors)),
                            args.days, args.target_window_days)
    generated = datetime.now(
        timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    k = payload["kpi"]
    meta_desc = (
        f"Every outcome of the daily US large-cap RSI(2) mean-reversion "
        f"list, {k['span'][0]} to {k['span'][1]} ({k['runs']} trading days, "
        f"{k['resolved']['n']} resolved signals): breadth-by-outcome "
        "columns, a date-by-ticker outcome grid, validated-pocket vs rest "
        "expectancy and a sortable roster."
    )
    data_json = json.dumps(payload, separators=(
        ",", ":")).replace("</", r"<\/")
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    html = html.replace("__META_DESC__", meta_desc)
    html = html.replace("__GENERATED__", generated)
    html = html.replace("__STUCK_MIN_STREAK__", str(STUCK_MIN_STREAK))
    out = Path(args.out)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, "
          f"{k['runs']} days x {k['tracked']} tickers, "
          f"{k['resolved']['n']} resolved)")


if __name__ == "__main__":
    main()
