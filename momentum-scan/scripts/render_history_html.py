#!/usr/bin/env python3
"""Render state/history.csv into a self-contained static HTML dashboard.

Reads the momentum-scan run history plus the sector cache and writes a single
HTML file (no external assets, no network) with:

  - a KPI row (span, current #1, longest live streak, new entrants, names tracked)
  - a rank bump chart over every recorded run (current top-8 emphasized)
  - a sector-composition stacked column per run day
  - a date x ticker rank heatmap with a min-appearances filter
  - a per-ticker summary table (the no-hover fallback for every value)

Usage:
    python scripts/render_history_html.py [--top-n 30] [--days 60] [--out state/history.html]
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TOP_SECTORS = 6  # sectors beyond the top 6 by cell-days fold into "Others"


def load_history(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_sectors(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def build_payload(rows: list[dict], sectors: dict, top_n: int, days_window: int = 0) -> dict:
    run_ids = sorted({r["run_id"] for r in rows})
    day_idx = {rid: i for i, rid in enumerate(run_ids)}
    day_labels = [f"{rid[4:6]}-{rid[6:8]}" for rid in run_ids]
    # Charts render only the trailing window; summary/KPI stay full-history.
    win_start = len(run_ids) - \
        days_window if 0 < days_window < len(run_ids) else 0
    win_ids = run_ids[win_start:]

    by_ticker: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        by_ticker.setdefault(t, {})[r["run_id"]] = r

    def sector_of(t: str) -> str:
        return sectors.get(t, {}).get("sector", "Unknown")

    # Only tickers that ever hit the displayed top-N make it into the page;
    # their below-cutoff appearances are kept as context points.
    tracked = {
        t: runs
        for t, runs in by_ticker.items()
        if any(int(r["rank"]) <= top_n for r in runs.values())
    }

    latest, prev = run_ids[-1], (run_ids[-2] if len(run_ids) > 1 else None)

    series = []
    summary = []
    for t, runs in tracked.items():
        pts = []
        for rid, r in sorted(runs.items()):
            pts.append({
                "d": day_idx[rid],
                "r": int(r["rank"]),
                "s": float(r["score"]),
                "ret": float(r["return_pct"]),
                "dd": float(r["max_dd_pct"]),
            })
        top_days = [p for p in pts if p["r"] <= top_n]
        apps = len(top_days)
        streak = 0
        for rid in reversed(run_ids):
            r = runs.get(rid)
            if r is None or int(r["rank"]) > top_n:
                break
            streak += 1
        latest_row = runs.get(latest)
        latest_rank = int(latest_row["rank"]) if latest_row else None
        win_pts = [{**p, "d": p["d"] - win_start}
                   for p in pts if p["d"] >= win_start]
        win_apps = sum(1 for p in win_pts if p["r"] <= top_n)
        if win_pts:
            series.append({"t": t, "sec": sector_of(
                t), "pts": win_pts, "apps": win_apps})
        first_d = min(p["d"] for p in top_days)
        last_d = max(p["d"] for p in top_days)
        summary.append({
            "t": t,
            "sec": sector_of(t),
            "apps": apps,
            "best": min(p["r"] for p in top_days),
            "latest": latest_rank if latest_rank and latest_rank <= top_n else None,
            "streak": streak,
            "first": day_labels[first_d], "firstD": first_d,
            "last": day_labels[last_d], "lastD": last_d,
            "score": float(latest_row["score"]) if latest_row else None,
        })

    summary.sort(key=lambda s: (-s["apps"], s["best"]))
    # Heatmap row order: board days desc, then most recently on board —
    # departed names sink within their tier instead of sorting alphabetically.
    series.sort(key=lambda s: (
        -s["apps"],
        -max((p["d"] for p in s["pts"] if p["r"] <= top_n), default=-1),
        s["t"],
    ))

    # Sector composition of the displayed top-N per day.
    sec_days: dict[str, int] = {}
    per_day_sec: list[dict] = []
    for rid in win_ids:
        counts: dict[str, int] = {}
        for t, runs in tracked.items():
            r = runs.get(rid)
            if r and int(r["rank"]) <= top_n:
                s = sector_of(t)
                counts[s] = counts.get(s, 0) + 1
                sec_days[s] = sec_days.get(s, 0) + 1
        per_day_sec.append(counts)
    top_secs = [s for s, _ in sorted(
        sec_days.items(), key=lambda kv: -kv[1])[:TOP_SECTORS]]
    sector_series = []
    for rid, counts in zip(win_ids, per_day_sec):
        row = [counts.get(s, 0) for s in top_secs]
        row.append(sum(counts.values()) - sum(row))  # Others
        sector_series.append(row)

    # KPI facts.
    latest_top = sorted(
        (r for runs in tracked.values() if (
            r := runs.get(latest)) and int(r["rank"]) <= top_n),
        key=lambda r: int(r["rank"]),
    )
    prior_seen = {
        t for t, runs in tracked.items()
        if any(rid != latest and int(r["rank"]) <= top_n for rid, r in runs.items())
    }
    new_entrants = [r["ticker"]
                    for r in latest_top if r["ticker"] not in prior_seen]
    dropouts = []
    if prev:
        prev_top = {
            t for t, runs in tracked.items()
            if (r := runs.get(prev)) and int(r["rank"]) <= top_n
        }
        latest_set = {r["ticker"] for r in latest_top}
        dropouts = sorted(prev_top - latest_set)
    streak_leader = max(
        (s for s in summary if s["latest"]), key=lambda s: s["streak"], default=None
    )

    return {
        "topN": top_n,
        "days": day_labels[win_start:],
        "window": {"total": len(run_ids), "shown": len(win_ids)},
        "series": series,
        "summary": summary,
        "sectors": {"names": top_secs + ["Others"], "perDay": sector_series},
        "kpi": {
            "runs": len(run_ids),
            "span": [f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}" for rid in (run_ids[0], run_ids[-1])],
            "no1": {"t": latest_top[0]["ticker"], "s": float(latest_top[0]["score"])} if latest_top else None,
            "streak": {"t": streak_leader["t"], "n": streak_leader["streak"]} if streak_leader else None,
            "newEntrants": new_entrants,
            "dropouts": dropouts,
            "tracked": len(tracked),
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>momentum-scan history</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --ctx-line: #c9c8c1;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4;
  --s6: #008300; --s7: #4a3aa7; --s8: #e34948;
  --other: #b5b4ad;
  --h0: #cde2fb; --h1: #b7d3f6; --h2: #9ec5f4; --h3: #86b6ef; --h4: #6da7ec;
  --h5: #5598e7; --h6: #3987e5; --h7: #2a78d6; --h8: #1c5cab; --h9: #0d366b;
  --hx: #eceae4;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --ctx-line: #47463f;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181;
    --s6: #008300; --s7: #9085e9; --s8: #e66767;
    --other: #55544d;
    --h0: #184f95; --h1: #1c5cab; --h2: #256abf; --h3: #2a78d6; --h4: #3987e5;
    --h5: #5598e7; --h6: #6da7ec; --h7: #86b6ef; --h8: #9ec5f4; --h9: #cde2fb;
    --hx: #262624;
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
svg { display: block; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); }
svg text.tick { font-variant-numeric: tabular-nums; }
svg text.dlabel { font-size: 11.5px; font-weight: 600; fill: var(--ink-2); }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 0; font-size: 12.5px; color: var(--ink-2); align-items: center; }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .line { width: 14px; height: 2px; border-radius: 1px; }
.legend .rect { width: 11px; height: 11px; border-radius: 3px; }
.detail { display: none; grid-template-columns: auto 1fr; gap: 8px 20px; align-items: center; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--grid); }
.detail .minis { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; }
.detail .dt-head { font-size: 13px; font-weight: 600; grid-row: span 2; }
.detail .dt-note { grid-column: 2; font-size: 11.5px; color: var(--muted); }
.dt-note code { font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
.mini .mlbl { font-size: 11.5px; color: var(--muted); margin-bottom: 2px; }
.mini .mlbl b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; margin-left: 4px; }
.head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin: 0 0 12px; }
.head .note { margin-bottom: 0; }
.head select {
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
#tip .v { color: var(--ink); font-weight: 600; font-size: 13.5px; }
#tip .k { display: inline-block; width: 14px; height: 2px; border-radius: 1px; vertical-align: middle; margin-right: 6px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; cursor: pointer; user-select: none; }
th:hover { color: var(--ink-2); }
th.on { color: var(--ink); }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
td { font-variant-numeric: tabular-nums; color: var(--ink-2); }
tbody tr:last-child td { border-bottom: none; }
td.tk { color: var(--ink); font-weight: 600; }
.foot { color: var(--muted); font-size: 12px; margin-top: 24px; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
svg a:hover text { text-decoration: underline; }
</style>
</head>
<body>
<div class="wrap">
  <h1>momentum-scan history</h1>
  <p class="sub" id="subtitle"></p>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>Rank trajectories</h2>
    <p class="note" id="bump-note"></p>
    <div class="scroll" id="bump"></div>
    <div class="legend" id="bump-legend"></div>
    <div class="detail" id="bump-detail"></div>
  </div>

  <div class="card">
    <div class="head">
      <div>
        <h2>Board heatmap</h2>
        <p class="note" id="heat-note">Rows sorted by days on board — persistent leaders on top, one-day wonders at the bottom. Darker = better rank.
Click a row to open its Score / Return / Drawdown strip.</p>
      </div>
      <select id="heat-filter" aria-label="Filter by days on board"></select>
    </div>
    <div class="scroll" id="heat"></div>
    <div class="legend" id="heat-legend"></div>
    <div class="detail" id="heat-detail"></div>
  </div>

  <div class="card">
    <h2>Sector mix</h2>
    <p class="note" id="sec-note"></p>
    <div class="scroll" id="secchart"></div>
    <div class="legend" id="sec-legend"></div>
  </div>

  <div class="card">
    <h2>Roster</h2>
    <p class="note">One row per name that ever made the board. Click a header to sort; click again to reverse. Every hover value from the charts is readable here.</p>
    <div class="scroll"><table id="tbl"></table></div>
  </div>

  <p class="foot" id="foot"></p>
</div>
<div id="tip"></div>
<script>
const DATA = __DATA_JSON__;
const GENERATED = "__GENERATED__";

const N = DATA.topN, DAYS = DATA.days.length;
const WIN_TAG = DATA.window.shown < DATA.window.total ? ` Charts show the last ${DATA.window.shown} of ${DATA.window.total} trading days.` : "";
if (WIN_TAG) document.getElementById("heat-note").textContent += WIN_TAG;
const SLOTS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
const BOARD = DATA.summary.filter(s => s.latest).sort((a, b) => a.latest - b.latest);
const TOP8 = BOARD.slice(0, 8).map(s => s.t);
const slotColorOf = t => { const i = TOP8.indexOf(t); return i < 0 ? null : `var(${SLOTS[i]})`; };
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
  if (!arr.length) return "\u2014";
  const sp = document.createElement("span");
  arr.forEach((t, i) => {
    if (i) sp.appendChild(document.createTextNode(" "));
    sp.appendChild(tickerLink(t));
  });
  return sp;
}
const dlt = (v, n) => {
  let s = v.toFixed(n);
  if (+s === 0) s = (0).toFixed(n);
  return ` (${+s >= 0 ? "+" : ""}${s})`;
};
const gapTag = g => g > 1 ? ` · vs ${g} days earlier` : "";
function renderDetail(det, t) {
  det.textContent = "";
  det.style.display = t ? "grid" : "none";
  if (!t) return;
  const s = DATA.series.find(x => x.t === t);
  if (!s) { det.style.display = "none"; return; }
  const head = div("dt-head", det);
  head.appendChild(tickerLink(t));
  const minis = div("minis", det);
  const col = slotColorOf(t) || "var(--ink-2)";
  const last = s.pts[s.pts.length-1];
  [["Score", p => p.s, v => v.toFixed(2)],
   ["Return", p => p.ret, v => v.toFixed(1) + "%"],
   ["Drawdown", p => p.dd, v => v.toFixed(1) + "%"]].forEach(([lbl, get, fmt]) => {
    const m = div("mini", minis);
    const l = div("mlbl", m, lbl);
    const b = document.createElement("b"); b.textContent = fmt(get(last)); l.appendChild(b);
    const W2 = 220, H2 = 34, PAD = 3;
    const vals = s.pts.map(get);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const x2 = d => PAD + d / Math.max(DAYS - 1, 1) * (W2 - 2*PAD);
    const y2 = v => PAD + (hi - v) / (hi - lo) * (H2 - 2*PAD);
    const sv = el("svg", {width: W2, height: H2, viewBox: `0 0 ${W2} ${H2}`}, m);
    let dstr = "", prev = null;
    for (const p of s.pts) {
      dstr += (prev !== null && p.d === prev + 1 ? "L" : "M") + x2(p.d).toFixed(1) + " " + y2(get(p)).toFixed(1);
      prev = p.d;
    }
    el("path", {d: dstr, fill: "none", stroke: col, "stroke-width": 1.5,
      "stroke-linecap": "round", "stroke-linejoin": "round"}, sv);
    el("circle", {cx: x2(last.d).toFixed(1), cy: y2(get(last)).toFixed(1), r: 2.5, fill: col}, sv);
  });
  const fn = div("dt-note", det);
  const fc = document.createElement("code");
  fc.textContent = "Score = Return ÷ |Drawdown|";
  fn.appendChild(fc);
  fn.appendChild(document.createTextNode(" — return per 1% of drawdown endured (sub-1% drawdowns count as 1%)."));
};
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
  const head = div(null, t);
  if (keyColor) { const k = document.createElement("span"); k.className = "k"; k.style.background = keyColor; head.appendChild(k); }
  const v = document.createElement("span"); v.className = "v"; v.textContent = rows[0]; head.appendChild(v);
  rows.slice(1).forEach(r => div(null, t, r));
}

// ---- subtitle + KPI row ----
{
  const k = DATA.kpi;
  document.getElementById("subtitle").textContent =
    `${k.span[0]} → ${k.span[1]} · ${k.runs} trading days · persistence view of the daily top-${N} board`;
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
  tile("History span", `${k.runs} days`, `${k.span[0]} → ${k.span[1]}`);
  if (k.no1) tile("Current #1", tickerLink(k.no1.t), `Score ${k.no1.s.toFixed(2)}`);
  if (k.streak) tile("Longest streak", `${k.streak.n} days`, tickerLink(k.streak.t));
  tile("New entrants", `${k.newEntrants.length}`, tickerList(k.newEntrants));
  tile("Dropouts", `${k.dropouts.length}`, tickerList(k.dropouts));
  tile("Names tracked", `${k.tracked}`, `Tickers with ≥1 day on the board`);
}

// ---- bump chart ----
const BUMP_MIN_APPS = 5;
{
  const onBoard = new Set(BOARD.map(s => s.t));
  const plotted = DATA.series.filter(s => s.apps >= BUMP_MIN_APPS || onBoard.has(s.t));
  document.getElementById("bump-note").textContent =
    `${plotted.length} trajectories (≥${BUMP_MIN_APPS} days on board + today's top ${N}; #1 at the top; ranks below #${N} clamp to the dashed floor). Top 8 in color; the right edge labels today's full board.\nHover to inspect; click a line to pin it and open its Score / Return / Drawdown strip.` + WIN_TAG;
  const ML = 34, MR = 78, MT = 12, MB = 26, DX = 19, RY = 15;
  const W = ML + (DAYS-1)*DX + MR, H = MT + N*RY + RY + MB;
  const xOf = d => ML + d*DX, yOf = r => MT + (Math.min(r, N+1) - 1) * RY;
  const svg = el("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, document.getElementById("bump"));
  [1,5,10,15,20,25,30].filter(r=>r<=N).forEach(r => {
    el("line", {x1: ML-4, x2: W-MR+30, y1: yOf(r), y2: yOf(r), stroke: "var(--grid)"}, svg);
    el("text", {x: ML-8, y: yOf(r)+4, "text-anchor":"end", class:"tick"}, svg).textContent = r;
  });
  el("line", {x1: ML-4, x2: W-MR+30, y1: yOf(N+1), y2: yOf(N+1), stroke: "var(--axis)", "stroke-dasharray":"3 3"}, svg);
  el("text", {x: ML-8, y: yOf(N+1)+4, "text-anchor":"end", class:"tick"}, svg).textContent = ">"+N;
  DATA.days.forEach((d,i) => {
    if (i === DAYS-1 || (i % 5 === 0 && DAYS-1-i >= 3))
      el("text", {x: xOf(i), y: H-8, "text-anchor":"middle", class:"tick"}, svg).textContent = d;
  });
  const cross = el("line", {y1: MT-4, y2: yOf(N+1), stroke: "var(--axis)", "stroke-width":1, visibility:"hidden"}, svg);
  const paths = {};
  plotted.forEach(s => {
    let dstr = "", prev = null;
    for (const p of s.pts) {
      dstr += (prev !== null && p.d === prev + 1 ? "L" : "M") + xOf(p.d) + " " + yOf(p.r);
      prev = p.d;
    }
    const c = slotColorOf(s.t);
    paths[s.t] = el("path", {d: dstr, fill: "none",
      stroke: c || "var(--ctx-line)", "stroke-width": c ? 2 : 1.25,
      "stroke-linecap": "round", "stroke-linejoin": "round", opacity: c ? 1 : 0.55}, svg);
  });
  TOP8.forEach(t => {
    const s = plotted.find(p => p.t === t); if (!s) return;
    const last = s.pts[s.pts.length-1];
    el("circle", {cx: xOf(last.d), cy: yOf(last.r), r: 4.5, fill: slotColorOf(t),
      stroke: "var(--surface)", "stroke-width": 2}, svg);
    const la = el("a", {href: tickerUrl(t), target: "_blank", rel: "noopener"}, svg);
    la.addEventListener("click", ev => ev.stopPropagation());
    el("text", {x: xOf(last.d)+10, y: yOf(last.r)+4, class:"dlabel"}, la).textContent = t;
  });
  BOARD.slice(8).forEach(sm => {
    const s = plotted.find(p => p.t === sm.t); if (!s) return;
    const last = s.pts[s.pts.length-1];
    if (last.d !== DAYS-1 || last.r > N) return;
    el("circle", {cx: xOf(last.d), cy: yOf(last.r), r: 3, fill: "var(--ctx-line)",
      stroke: "var(--surface)", "stroke-width": 2}, svg);
    const la = el("a", {href: tickerUrl(sm.t), target: "_blank", rel: "noopener"}, svg);
    la.addEventListener("click", ev => ev.stopPropagation());
    el("text", {x: xOf(last.d)+10, y: yOf(last.r)+4, class:"tick"}, la).textContent = sm.t;
  });
  const leg = document.getElementById("bump-legend");
  TOP8.forEach(t => {
    const k = div("key", leg); const l = div("line", k); l.style.background = slotColorOf(t);
    k.appendChild(tickerLink(t));
  });
  const k = div("key", leg); const l = div("line", k); l.style.background = "var(--ctx-line)";
  k.appendChild(document.createTextNode("Others"));

  const det = document.getElementById("bump-detail");

  let pinned = null, lit = null;
  const restyle = t => {
    const c = slotColorOf(t);
    Object.entries(paths).forEach(([tk,p]) => {
      const base = slotColorOf(tk);
      if (t && tk === t) p.setAttribute("style", `stroke:${c || "var(--ink)"};stroke-width:2.5;opacity:1`);
      else p.setAttribute("style", t ? `opacity:${base?0.35:0.18}` : "");
    });
  };
  svg.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect();
    const mx = ev.clientX - box.left, my = ev.clientY - box.top;
    const d = Math.max(0, Math.min(DAYS-1, Math.round((mx - ML) / DX)));
    cross.setAttribute("x1", xOf(d)); cross.setAttribute("x2", xOf(d));
    cross.setAttribute("visibility", "visible");
    let best = null, bd = 28;
    for (const s of plotted) {
      const p = s.pts.find(p => p.d === d);
      if (p) { const dy = Math.abs(yOf(p.r) - my); if (dy < bd) { bd = dy; best = {s, p}; } }
    }
    if (best) {
      lit = best.s.t; restyle(pinned || lit);
      const idx = best.s.pts.indexOf(best.p);
      const pp = idx > 0 ? best.s.pts[idx-1] : null;
      showTip(ev.clientX, ev.clientY, t => tipRows(t,
        [best.s.t, `${DATA.days[d]} · Rank ${best.p.r <= N ? "#"+best.p.r : "#"+best.p.r+" (below cutoff)"}`,
         `Score ${best.p.s.toFixed(2)}${pp ? dlt(best.p.s - pp.s, 2) : ""} · Return ${best.p.ret.toFixed(1)}%${pp ? dlt(best.p.ret - pp.ret, 1) : ""}`,
         `Drawdown ${best.p.dd.toFixed(1)}%${pp ? dlt(best.p.dd - pp.dd, 1) + gapTag(best.p.d - pp.d) : ""}`],
        slotColorOf(best.s.t) || "var(--ctx-line)"));
    } else { lit = null; restyle(pinned); hideTip(); }
  });
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility","hidden"); restyle(pinned); hideTip(); });
  svg.addEventListener("click", () => { pinned = (pinned === lit ? null : lit); restyle(pinned); renderDetail(det, pinned); });
}

// ---- sector stacked columns ----
{
  const names = DATA.sectors.names;
  document.getElementById("sec-note").textContent =
    `Daily top-${N} counted by sector — watch which sector is taking over the board.` + WIN_TAG;
  const ML = 30, MT = 8, MB = 26, DX = 19, BW = 13, SH = 6;
  const W = ML + DAYS*DX + 10, H = MT + N*SH + MB;
  const svg = el("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, document.getElementById("secchart"));
  [0,10,20,30].filter(v=>v<=N).forEach(v => {
    el("line", {x1: ML-4, x2: W-6, y1: MT+(N-v)*SH, y2: MT+(N-v)*SH, stroke: "var(--grid)"}, svg);
    el("text", {x: ML-8, y: MT+(N-v)*SH+4, "text-anchor":"end", class:"tick"}, svg).textContent = v;
  });
  const TOPSEC = names.length - 1;
  DATA.sectors.perDay.forEach((counts, d) => {
    let acc = 0;
    const x = ML + d*DX;
    counts.forEach((c, i) => {
      if (!c) return;
      const y0 = MT + N*SH - (acc + c)*SH;
      const r = el("rect", {x, y: y0 + 1, width: BW, height: c*SH - 2, rx: 2,
        fill: i < TOPSEC ? `var(${SLOTS[i]})` : "var(--other)"}, svg);
      r.dataset.d = d; r.dataset.i = i; r.dataset.c = c;
      acc += c;
    });
    if (d === DAYS-1 || (d % 5 === 0 && DAYS-1-d >= 3))
      el("text", {x: x+BW/2, y: H-8, "text-anchor":"middle", class:"tick"}, svg).textContent = DATA.days[d];
  });
  const leg = document.getElementById("sec-legend");
  names.forEach((n,i) => {
    const k = div("key", leg); const r = div("rect", k);
    r.style.background = i < TOPSEC ? `var(${SLOTS[i]})` : "var(--other)";
    k.appendChild(document.createTextNode(n));
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.i !== undefined) {
      const i = +t.dataset.i;
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [`${t.dataset.c} names`, `${names[i]} · ${DATA.days[+t.dataset.d]}`],
        i < TOPSEC ? `var(${SLOTS[i]})` : "var(--other)"));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
}

// ---- heatmap ----
const heatBox = document.getElementById("heat");
const heatDet = document.getElementById("heat-detail");
let heatPinned = null;
const highlightHeatRow = () => {
  heatBox.querySelectorAll("text[data-t]").forEach(x =>
    x.style.fill = x.dataset.t === heatPinned ? "var(--ink)" : "");
};
function renderHeat(minApps) {
  heatBox.textContent = "";
  const rows = DATA.series.filter(s => s.apps >= minApps);
  const GL = 64, MT = 20, CW = 16, CH = 14;
  const W = GL + DAYS*CW + 8, H = MT + rows.length*CH + 6;
  const svg = el("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`}, heatBox);
  DATA.days.forEach((d,i) => {
    if (i === DAYS-1 || (i % 5 === 0 && DAYS-1-i >= 3))
      el("text", {x: GL + i*CW + CW/2, y: 12, "text-anchor":"middle", class:"tick"}, svg).textContent = d;
  });
  rows.forEach((s, ri) => {
    const y = MT + ri*CH;
    const ra = el("a", {href: tickerUrl(s.t), target: "_blank", rel: "noopener"}, svg);
    const lt = el("text", {x: GL-8, y: y+CH-3, "text-anchor":"end", class:"tick"}, ra);
    lt.textContent = s.t; lt.dataset.t = s.t;
    let hprev = null;
    for (const p of s.pts) {
      const bucket = p.r <= N ? Math.min(9, Math.floor((N - p.r) / N * 10)) : null;
      const r = el("rect", {x: GL + p.d*CW, y: y+1, width: CW-2, height: CH-2, rx: 2,
        fill: bucket === null ? "var(--hx)" : `var(--h${bucket})`}, svg);
      r.dataset.t = s.t; r.dataset.d = p.d; r.dataset.r = p.r;
      r.dataset.s = p.s; r.dataset.ret = p.ret; r.dataset.dd = p.dd;
      if (hprev) {
        r.dataset.gap = p.d - hprev.d;
        r.dataset.ds = (p.s - hprev.s).toFixed(2);
        r.dataset.dret = (p.ret - hprev.ret).toFixed(1);
        r.dataset.ddd = (p.dd - hprev.dd).toFixed(1);
      }
      hprev = p;
    }
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.t) {
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [t.dataset.t,
         `${DATA.days[+t.dataset.d]} · ${+t.dataset.r <= N ? "Rank #"+t.dataset.r : "#"+t.dataset.r+" (below cutoff)"}`,
         `Score ${(+t.dataset.s).toFixed(2)}${t.dataset.ds ? dlt(+t.dataset.ds, 2) : ""} · Return ${(+t.dataset.ret).toFixed(1)}%${t.dataset.dret ? dlt(+t.dataset.dret, 1) : ""}`,
         `Drawdown ${(+t.dataset.dd).toFixed(1)}%${t.dataset.ddd ? dlt(+t.dataset.ddd, 1) + gapTag(+t.dataset.gap) : ""}`]));
    } else hideTip();
  });
  svg.addEventListener("pointerleave", hideTip);
  svg.addEventListener("click", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.t) {
      heatPinned = (heatPinned === t.dataset.t ? null : t.dataset.t);
    } else if (t.tagName !== "text") {
      heatPinned = null;
    }
    renderDetail(heatDet, heatPinned);
    highlightHeatRow();
  });
  highlightHeatRow();
}
{
  const sel = document.getElementById("heat-filter");
  // Cut points scale with history length: >=5% attendance filters one-day noise, >=20% marks regulars (>=3 / >=10 at 48 days)
  const c5 = Math.ceil(DAYS * 0.05), c20 = Math.ceil(DAYS * 0.20);
  new Map([[c5, `≥${c5} days`], [c20, `≥${c20} days`], [0, "All"]]).forEach((lbl, min) => {
    const o = document.createElement("option");
    o.value = min;
    o.textContent = lbl;
    sel.appendChild(o);
  });
  sel.addEventListener("change", () => renderHeat(+sel.value));
  sel.value = c20;
  renderHeat(+sel.value);
  const leg = document.getElementById("heat-legend");
  leg.appendChild(document.createTextNode("#1"));
  [9,7,5,2,0].forEach(b => { const r = div("rect", leg); r.style.background = `var(--h${b})`; });
  leg.appendChild(document.createTextNode("#" + N));
  const k = div("key", leg); const r = div("rect", k); r.style.background = "var(--hx)";
  k.appendChild(document.createTextNode("Passed filter, below display cutoff"));
}

// ---- table ----
{
  const tbl = document.getElementById("tbl");
  // dir is each column's first-click direction: rank-like asc (best first), days/dates/score desc (largest/newest first)
  const COLS = [
    {h: "Ticker",     v: s => s.t,              dir: 1},
    {h: "Sector",       v: s => s.sec,            dir: 1},
    {h: "Days on board", v: s => s.apps,          dir: -1},
    {h: "Best rank",    v: s => s.best,           dir: 1},
    {h: "Current rank", v: s => s.latest,         dir: 1},
    {h: "Streak",       v: s => s.streak || null, dir: -1},
    {h: "First seen",   v: s => s.firstD,         dir: -1},
    {h: "Last seen",    v: s => s.lastD,          dir: -1},
    {h: "Latest score", v: s => s.score,          dir: -1},
  ];
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  const ths = COLS.map(c => {
    const th = document.createElement("th");
    hr.appendChild(th);
    return th;
  });
  thead.appendChild(hr); tbl.appendChild(thead);
  const tb = document.createElement("tbody");
  tbl.appendChild(tb);

  let sortCol = 4, sortDir = 1;
  const renderHead = () => ths.forEach((th, i) => {
    th.textContent = COLS[i].h + (i === sortCol ? (sortDir === 1 ? " ▲" : " ▼") : "");
    th.className = i === sortCol ? "on" : "";
  });
  const renderRows = () => {
    tb.textContent = "";
    const v = COLS[sortCol].v;
    [...DATA.summary].sort((a, b) => {
      const x = v(a), y = v(b);
      const xn = x === null || x === undefined, yn = y === null || y === undefined;
      if (xn || yn) return xn && yn ? (b.apps - a.apps) : xn ? 1 : -1;  // nulls always sink
      const c = (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir;
      return c || (b.apps - a.apps) || (a.best - b.best);
    }).forEach(s => {
      const tr = document.createElement("tr");
      [s.t, s.sec, s.apps, "#"+s.best, s.latest ? "#"+s.latest : "—",
       s.streak || "—", s.first, s.last, s.score !== null ? s.score.toFixed(2) : "—"]
      .forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 0) { td.className = "tk"; td.appendChild(tickerLink(c)); }
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
}

document.getElementById("foot").textContent =
  `Generated by render_history_html.py at ${GENERATED} · Source: state/history.csv`;
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--days", type=int, default=60,
                    help="trading days shown in charts (table/KPI stay full-history); 0 = all")
    ap.add_argument(
        "--history", default=str(SKILL_DIR / "state" / "history.csv"))
    ap.add_argument(
        "--sectors", default=str(SKILL_DIR / "state" / "sectors.json"))
    ap.add_argument("--out", default=str(SKILL_DIR / "state" / "history.html"))
    args = ap.parse_args()

    rows = load_history(Path(args.history))
    if not rows:
        raise SystemExit("history.csv is empty — run a scan first")
    payload = build_payload(rows, load_sectors(
        Path(args.sectors)), args.top_n, args.days)
    generated = datetime.now(
        timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    data_json = json.dumps(payload, separators=(
        ",", ":")).replace("</", r"<\/")
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    html = html.replace("__GENERATED__", generated)
    out = Path(args.out)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, "
          f"{payload['kpi']['runs']} days x {payload['kpi']['tracked']} tickers)")


if __name__ == "__main__":
    main()
