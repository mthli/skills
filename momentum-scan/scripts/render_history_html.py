#!/usr/bin/env python3
"""Render state/history.csv into a self-contained static HTML dashboard.

Reads the momentum-scan run history plus the sector cache and writes a single
HTML file (no external assets, no network) with:

  - a KPI row (span, current #1, longest live streak, new entrants, names tracked)
  - a rank bump chart over every recorded run (current top-8 emphasized)
  - a sector-composition stacked column per run day
  - a date x ticker rank heatmap with a min-appearances filter
  - a per-ticker summary table (the no-hover fallback for every value)
  - an English / 简体中文 / 繁體中文 / 日本語 / 한국어 language menu (top-right;
    choice kept in localStorage, first visit follows the browser language)
  - SEO meta tags (description + Open Graph + Twitter card) filled from the
    actual data span at render time

Usage:
    python scripts/render_history_html.py [--top-n 30] [--days 60] [--out state/history.html]
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TOP_SECTORS = 6  # sectors beyond the top 6 by cell-days fold into "Others"


def load_history(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# Entry-quality tier thresholds — MUST equal scan.py's constants of the
# same names (that's the source of truth; test_volume_fields.py has a
# drift-guard test asserting equality). Duplicated numerically because
# this script deliberately stays stdlib-only while scan.py imports
# yfinance at module level.
ENTRY_VOL_SURGE_MIN = 1.5
ENTRY_VOL_QUIET_MAX = 0.8
ENTRY_CLEAN_DIST_MAX = 1


def load_sectors(path: Path) -> dict:
    if not path.exists():
        # A missing cache is almost always a checkout/path problem, and the
        # silent-{} fallback renders EVERY ticker as "Unknown" sector — a
        # page that looks fine at a glance but is wrong everywhere. Warn
        # loudly; keep rendering so a truly cache-less setup still works.
        print(f"WARNING: sector cache not found at {path} — every ticker "
              f"will render with 'Unknown' sector. sectors.json is tracked "
              f"in the repo; pass --sectors if yours lives elsewhere.",
              file=sys.stderr)
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
        # Entry quality of the LATEST episode: walk back from the most
        # recent on-board day while days stay consecutive, then tier the
        # spell's first day by its entry-day volume character.
        top_ds = sorted(p["d"] for p in top_days)
        ep_start = top_ds[-1]
        for d in reversed(top_ds[:-1]):
            if d != ep_start - 1:
                break
            ep_start = d
        erow = runs[run_ids[ep_start]]
        eq = eqv = eqd = None
        vr_s = erow.get("vol_ratio_20d") or ""
        dd_s = erow.get("dist_days_25d") or ""
        if vr_s and dd_s:
            eqv, eqd = float(vr_s), int(float(dd_s))
            if eqv >= ENTRY_VOL_SURGE_MIN:
                eq = 3 if eqd <= ENTRY_CLEAN_DIST_MAX else 2
            elif eqv < ENTRY_VOL_QUIET_MAX:
                eq = 0
            else:
                eq = 1
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
            "eq": eq, "eqv": eqv, "eqd": eqd,
            "eqDay": day_labels[ep_start],
            # Filled dot only when the spell began on the latest run (the
            # scan CLI's "entrant" semantics); older entries render as a
            # hollow ring so a weeks-old surge tier can't be mistaken for
            # a live buy signal.
            "eqNew": ep_start == len(run_ids) - 1,
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
<meta name="description" content="__META_DESC__">
<meta property="og:type" content="website">
<meta property="og:site_name" content="momentum-scan">
<meta property="og:title" content="momentum-scan history">
<meta property="og:description" content="__META_DESC__">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="momentum-scan history">
<meta name="twitter:description" content="__META_DESC__">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
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
  /* Entry-quality ordinal ramp — 4 steps off the heat ramp, spaced for
     adjacent distinguishability (validated ΔE ≥ 15 on both surfaces). */
  --eq0: var(--h0); --eq1: var(--h3); --eq2: var(--h6); --eq3: var(--h9);
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
    /* Dark ramp compresses at the deep end — widen the middle steps so
       adjacent tiers keep ΔE ≥ 15 against the dark surface. */
    --eq1: var(--h4); --eq2: var(--h7);
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
#tip .v { color: var(--ink); font-weight: 600; font-size: 13.5px; }
#tip .k { display: inline-block; width: 14px; height: 2px; border-radius: 1px; vertical-align: middle; margin-right: 6px; }
/* Inherit .eqdot's baseline -1px alignment (the roster-cell look);
   vertical-align: middle sat visibly below the title's optical center. */
#tip .eqdot { margin-right: 6px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; cursor: pointer; user-select: none; }
th:hover { color: var(--ink-2); }
th.on { color: var(--ink); }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
td { font-variant-numeric: tabular-nums; color: var(--ink-2); }
/* Last row keeps its divider: the entry-quality legend sits right below
   the table, and the rule visually separates data from legend. */
td.tk { color: var(--ink); font-weight: 600; }
.eqdot { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
         border: 1px solid var(--border); vertical-align: -1px; box-sizing: border-box; }
.eq0 { background: var(--eq0); } .eq1 { background: var(--eq1); }
.eq2 { background: var(--eq2); } .eq3 { background: var(--eq3); }
/* Ring variant: the latest spell did NOT start on the latest run — the
   tier is a frozen entry-day annotation, not a live signal. Hollow keeps
   the color readable while visually demoting it. */
.eqdot.ring { background: transparent; border-width: 3px; }
.ring.eq0 { border-color: var(--eq0); } .ring.eq1 { border-color: var(--eq1); }
.ring.eq2 { border-color: var(--eq2); } .ring.eq3 { border-color: var(--eq3); }
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
      <h1><a href="https://github.com/mthli/skills/tree/master/momentum-scan" target="_blank" rel="noopener">momentum-scan</a> <span id="h1-suffix">history</span></h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <select id="lang-menu" aria-label="Language"></select>
  </div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2 id="bump-title">Rank trajectories</h2>
    <p class="note" id="bump-note"></p>
    <div class="scroll" id="bump"></div>
    <div class="legend" id="bump-legend"></div>
    <div class="detail" id="bump-detail"></div>
  </div>

  <div class="card">
    <div class="head">
      <div>
        <h2 id="heat-title">Board heatmap</h2>
        <p class="note" id="heat-note"></p>
      </div>
      <select id="heat-filter"></select>
    </div>
    <div class="scroll" id="heat"></div>
    <div class="legend" id="heat-legend"></div>
    <div class="detail" id="heat-detail"></div>
  </div>

  <div class="card">
    <h2 id="sec-title">Sector mix</h2>
    <p class="note" id="sec-note"></p>
    <div class="scroll" id="secchart"></div>
    <div class="legend" id="sec-legend"></div>
  </div>

  <div class="card">
    <h2 id="roster-title">Roster</h2>
    <p class="note" id="roster-note"></p>
    <div class="scroll"><table id="tbl"></table></div>
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
    title: "momentum-scan history",
    h1Suffix: "history",
    subtitle: (a, b, runs, n) => `${a} → ${b} · ${runs} trading days · persistence view of the daily top-${n} board`,
    winTag: (s, t) => ` Charts show the last ${s} of ${t} trading days.`,
    kSpan: "History span", kDays: n => `${n} days`,
    kNo1: "Current #1", kScore: s => `Score ${s}`,
    kStreak: "Longest streak",
    kNew: "New entrants", kDrop: "Dropouts",
    kTracked: "Names tracked", kTrackedSub: "Tickers with ≥1 day on the board",
    bumpTitle: "Rank trajectories",
    bumpNote: (m, min, n) => `${m} trajectories (≥${min} days on board + today's top ${n}; #1 at the top; ranks below #${n} clamp to the dashed floor). Top 8 in color; the right edge labels today's full board.\nHover to inspect; click a line to pin it and open its Score / Return / Drawdown strip.`,
    heatTitle: "Board heatmap",
    heatNote: "Rows sorted by days on board — persistent leaders on top, one-day wonders at the bottom. The more vivid the blue, the better the rank.\nClick a row to open its Score / Return / Drawdown strip.",
    heatFilterLabel: "Filter by days on board",
    geDays: n => `≥${n} days`, all: "All",
    heatLegendBelow: "Passed filter, below display cutoff",
    secTitle: "Sector mix",
    secNote: n => `Daily top-${n} counted by sector — watch which sector is taking over the board.`,
    names: c => `${c} names`,
    others: "Others",
    rosterTitle: "Roster",
    rosterNote: "One row per name that ever made the board. Click a header to sort; click again to reverse. Every hover value from the charts is readable here.\nEntry = entry-day volume character of the latest board spell (darker blue = stronger entry; hover or tap the dot for details).",
    cols: ["Ticker", "Sector", "Days on board", "Best rank", "Current rank", "Streak", "Entry", "First seen", "Last seen", "Latest score"],
    eqLabels: ["Quiet drift-in", "Neutral", "Volume surge", "Surge + clean"],
    eqTip: (v, d, day) => `Vol ${v}× · ${d} dist days · Entered ${day}`,
    eqFrozen: "Tier frozen at entry day, never updated",
    eqFreshNote: "Filled = entered this run; ring = earlier entry — tier frozen at entry day, never updated.",
    score: "Score", ret: "Return", dd: "Drawdown",
    formula: "Score = Return ÷ |Drawdown|",
    formulaNote: " — return per 1% of drawdown endured (sub-1% drawdowns count as 1%).",
    rankAt: r => `Rank #${r}`, rankBelow: r => `Rank #${r} (below cutoff)`,
    gapTag: g => ` · vs ${g} days earlier`,
    genBy: "Generated by ", genAt: t => ` at ${t} · Source: `,
    sectorNames: {},
  },
  zh: {
    htmlLang: "zh-CN",
    title: "momentum-scan 历史",
    h1Suffix: "历史",
    subtitle: (a, b, runs, n) => `${a} → ${b} · 共 ${runs} 个交易日 · 每日 top-${n} 榜单的持续性视图`,
    winTag: (s, t) => `图表仅显示最近 ${s} / ${t} 个交易日。`,
    kSpan: "历史跨度", kDays: n => `${n} 天`,
    kNo1: "当前第 1 名", kScore: s => `评分 ${s}`,
    kStreak: "最长连续在榜",
    kNew: "新进榜", kDrop: "掉出榜",
    kTracked: "追踪标的数", kTrackedSub: "至少上榜 1 天的标的",
    bumpTitle: "排名轨迹",
    bumpNote: (m, min, n) => `共 ${m} 条轨迹（在榜 ≥${min} 天 + 今日 top ${n}；第 1 名在顶部；低于第 ${n} 名的排名压到虚线底线）。前 8 名着色；右缘标注今日完整榜单。\n悬停查看数值；点击一条线可固定并展开其评分 / 收益 / 回撤走势。`,
    heatTitle: "榜单热力图",
    heatNote: "行按在榜天数排序 —— 常青领跑者在上，一日游在下。蓝色越醒目排名越好。\n点击任意行展开其评分 / 收益 / 回撤走势。",
    heatFilterLabel: "按在榜天数筛选",
    geDays: n => `≥${n} 天`, all: "全部",
    heatLegendBelow: "通过筛选，但低于显示截断线",
    secTitle: "行业构成",
    secNote: n => `每日 top-${n} 按行业计数 —— 观察哪个行业正在占领榜单。`,
    names: c => `${c} 只`,
    others: "其他",
    rosterTitle: "上榜名录",
    rosterNote: "每个曾经上榜的标的一行。点击表头排序；再次点击反向。图表中所有悬停数值在此均可查阅。\n入场 = 最近一段在榜区间入场日的量能特征（蓝色越深入场越强；悬停或点按圆点看详情）。",
    cols: ["代码", "行业", "在榜天数", "最佳排名", "当前排名", "连续在榜", "入场", "首次上榜", "最近上榜", "最新评分"],
    eqLabels: ["缩量飘入", "中性", "放量入场", "放量且干净"],
    eqTip: (v, d, day) => `量比 ${v}× · ${d} 个派发日 · ${day} 入场`,
    eqFrozen: "层级定格于入场日、不随行情更新",
    eqFreshNote: "实心 = 本期新入榜；空心 = 历史入场 —— 层级定格于入场日、不随行情更新。",
    score: "评分", ret: "收益", dd: "回撤",
    formula: "评分 = 收益 ÷ |回撤|",
    formulaNote: " —— 每承受 1% 回撤换来的收益（回撤不足 1% 按 1% 计）。",
    rankAt: r => `排名 #${r}`, rankBelow: r => `排名 #${r}（低于截断线）`,
    gapTag: g => ` · 与 ${g} 天前相比`,
    genBy: "由 ", genAt: t => ` 于 ${t} 生成 · 数据源：`,
    sectorNames: {
      "Technology": "科技", "Financial Services": "金融服务", "Healthcare": "医疗保健",
      "Consumer Cyclical": "可选消费", "Consumer Defensive": "必需消费", "Industrials": "工业",
      "Communication Services": "通信服务", "Energy": "能源", "Basic Materials": "基础材料",
      "Real Estate": "房地产", "Utilities": "公用事业", "Unknown": "未知", "Others": "其他",
    },
  },
  zht: {
    htmlLang: "zh-Hant",
    title: "momentum-scan 歷史",
    h1Suffix: "歷史",
    subtitle: (a, b, runs, n) => `${a} → ${b} · 共 ${runs} 個交易日 · 每日 top-${n} 榜單的持續性視圖`,
    winTag: (s, t) => `圖表僅顯示最近 ${s} / ${t} 個交易日。`,
    kSpan: "歷史跨度", kDays: n => `${n} 天`,
    kNo1: "目前第 1 名", kScore: s => `評分 ${s}`,
    kStreak: "最長連續在榜",
    kNew: "新進榜", kDrop: "掉出榜",
    kTracked: "追蹤標的數", kTrackedSub: "至少上榜 1 天的標的",
    bumpTitle: "排名軌跡",
    bumpNote: (m, min, n) => `共 ${m} 條軌跡（在榜 ≥${min} 天 + 今日 top ${n}；第 1 名在頂部；低於第 ${n} 名的排名壓到虛線底線）。前 8 名著色；右緣標註今日完整榜單。\n懸停查看數值；點擊一條線可固定並展開其評分 / 報酬 / 回撤走勢。`,
    heatTitle: "榜單熱力圖",
    heatNote: "行按在榜天數排序 —— 常青領跑者在上，一日遊在下。藍色越醒目排名越好。\n點擊任意行展開其評分 / 報酬 / 回撤走勢。",
    heatFilterLabel: "按在榜天數篩選",
    geDays: n => `≥${n} 天`, all: "全部",
    heatLegendBelow: "通過篩選，但低於顯示截斷線",
    secTitle: "產業構成",
    secNote: n => `每日 top-${n} 按產業計數 —— 觀察哪個產業正在佔領榜單。`,
    names: c => `${c} 檔`,
    others: "其他",
    rosterTitle: "上榜名錄",
    rosterNote: "每個曾經上榜的標的一行。點擊表頭排序；再次點擊反向。圖表中所有懸停數值在此均可查閱。\n進場 = 最近一段在榜區間進場日的量能特徵（藍色越深進場越強；懸停或點按圓點看詳情）。",
    cols: ["代號", "產業", "在榜天數", "最佳排名", "目前排名", "連續在榜", "進場", "首次上榜", "最近上榜", "最新評分"],
    eqLabels: ["縮量飄入", "中性", "放量進場", "放量且乾淨"],
    eqTip: (v, d, day) => `量比 ${v}× · ${d} 個派發日 · ${day} 進場`,
    eqFrozen: "層級定格於進場日、不隨行情更新",
    eqFreshNote: "實心 = 本期新進榜；空心 = 歷史進場 —— 層級定格於進場日、不隨行情更新。",
    score: "評分", ret: "報酬", dd: "回撤",
    formula: "評分 = 報酬 ÷ |回撤|",
    formulaNote: " —— 每承受 1% 回撤換來的報酬（回撤不足 1% 按 1% 計）。",
    rankAt: r => `排名 #${r}`, rankBelow: r => `排名 #${r}（低於截斷線）`,
    gapTag: g => ` · 與 ${g} 天前相比`,
    genBy: "由 ", genAt: t => ` 於 ${t} 生成 · 資料來源：`,
    sectorNames: {
      "Technology": "科技", "Financial Services": "金融服務", "Healthcare": "醫療保健",
      "Consumer Cyclical": "週期性消費", "Consumer Defensive": "防禦性消費", "Industrials": "工業",
      "Communication Services": "通訊服務", "Energy": "能源", "Basic Materials": "原物料",
      "Real Estate": "房地產", "Utilities": "公用事業", "Unknown": "未知", "Others": "其他",
    },
  },
  ja: {
    htmlLang: "ja",
    title: "momentum-scan 履歴",
    h1Suffix: "履歴",
    subtitle: (a, b, runs, n) => `${a} → ${b} · 全 ${runs} 営業日 · 日次 top-${n} ランキングの持続性ビュー`,
    winTag: (s, t) => `チャートは直近 ${s} / ${t} 営業日のみ表示。`,
    kSpan: "履歴期間", kDays: n => `${n} 日`,
    kNo1: "現在の第 1 位", kScore: s => `スコア ${s}`,
    kStreak: "最長連続ランクイン",
    kNew: "新規ランクイン", kDrop: "圏外へ",
    kTracked: "追跡銘柄数", kTrackedSub: "1 日以上ランクインした銘柄",
    bumpTitle: "順位推移",
    bumpNote: (m, min, n) => `全 ${m} 本の推移線（ランクイン ${min} 日以上 + 本日の top ${n}；第 1 位が最上部；第 ${n} 位より下は破線の底辺に固定）。上位 8 銘柄を着色；右端は本日の全ランキング。\nホバーで数値を確認；線をクリックすると固定され、スコア / リターン / ドローダウンの推移が開きます。`,
    heatTitle: "ランキングヒートマップ",
    heatNote: "行はランクイン日数順 —— 定着したリーダーが上、一日だけの銘柄が下。青が鮮やかなほど順位が高い。\n行をクリックするとスコア / リターン / ドローダウンの推移が開きます。",
    heatFilterLabel: "ランクイン日数で絞り込み",
    geDays: n => `${n} 日以上`, all: "すべて",
    heatLegendBelow: "絞り込みは通過、表示カットオフ未満",
    secTitle: "セクター構成",
    secNote: n => `日次 top-${n} をセクター別に集計 —— どのセクターがランキングを席巻しつつあるかを観察。`,
    names: c => `${c} 銘柄`,
    others: "その他",
    rosterTitle: "ランクイン銘柄一覧",
    rosterNote: "ランクインしたことのある銘柄を 1 行ずつ表示。ヘッダーをクリックでソート、もう一度クリックで逆順。チャートのホバー数値はすべてこの表で確認できます。\nエントリー = 直近ランクイン期間の初日の出来高特性（青が濃いほど強い。ドットにホバーまたはタップで詳細）。",
    cols: ["ティッカー", "セクター", "ランクイン日数", "最高順位", "現在順位", "連続日数", "エントリー", "初登場", "直近登場", "最新スコア"],
    eqLabels: ["薄商い流入", "中立", "出来高急増", "急増＋クリーン"],
    eqTip: (v, d, day) => `出来高比 ${v}× · 分配日 ${d} · ${day} エントリー`,
    eqFrozen: "階層はエントリー日で確定し、以後更新されません",
    eqFreshNote: "塗りつぶし = 今回新規ランクイン、リング = 過去のエントリー —— 階層はエントリー日で確定し、以後更新されません。",
    score: "スコア", ret: "リターン", dd: "ドローダウン",
    formula: "スコア = リターン ÷ |ドローダウン|",
    formulaNote: " —— ドローダウン 1% あたりのリターン（1% 未満のドローダウンは 1% として計算）。",
    rankAt: r => `順位 #${r}`, rankBelow: r => `順位 #${r}（カットオフ未満）`,
    gapTag: g => ` · ${g} 日前との比較`,
    genBy: "", genAt: t => ` により ${t} に生成 · データソース：`,
    sectorNames: {
      "Technology": "テクノロジー", "Financial Services": "金融サービス", "Healthcare": "ヘルスケア",
      "Consumer Cyclical": "一般消費財", "Consumer Defensive": "生活必需品", "Industrials": "資本財",
      "Communication Services": "通信サービス", "Energy": "エネルギー", "Basic Materials": "素材",
      "Real Estate": "不動産", "Utilities": "公益事業", "Unknown": "不明", "Others": "その他",
    },
  },
  ko: {
    htmlLang: "ko",
    title: "momentum-scan 히스토리",
    h1Suffix: "히스토리",
    subtitle: (a, b, runs, n) => `${a} → ${b} · 총 ${runs}거래일 · 일일 top-${n} 보드의 지속성 뷰`,
    winTag: (s, t) => `차트는 최근 ${s} / ${t}거래일만 표시합니다.`,
    kSpan: "기록 기간", kDays: n => `${n}일`,
    kNo1: "현재 1위", kScore: s => `점수 ${s}`,
    kStreak: "최장 연속 진입",
    kNew: "신규 진입", kDrop: "이탈",
    kTracked: "추적 종목 수", kTrackedSub: "1일 이상 순위에 오른 종목",
    bumpTitle: "순위 궤적",
    bumpNote: (m, min, n) => `총 ${m}개 궤적(순위 진입 ${min}일 이상 + 오늘의 top ${n}; 1위가 맨 위; ${n}위 아래 순위는 점선 바닥에 고정). 상위 8개 종목은 색상 표시; 오른쪽 끝은 오늘의 전체 보드.\n마우스를 올려 값 확인; 선을 클릭하면 고정되고 점수 / 수익률 / 낙폭 추이가 열립니다.`,
    heatTitle: "보드 히트맵",
    heatNote: "행은 순위 진입 일수순 —— 꾸준한 리더가 위, 하루짜리 종목이 아래. 파란색이 선명할수록 순위가 높습니다.\n행을 클릭하면 점수 / 수익률 / 낙폭 추이가 열립니다.",
    heatFilterLabel: "순위 진입 일수로 필터",
    geDays: n => `${n}일 이상`, all: "전체",
    heatLegendBelow: "필터 통과, 표시 컷오프 미만",
    secTitle: "섹터 구성",
    secNote: n => `일일 top-${n}을 섹터별로 집계 —— 어느 섹터가 보드를 장악해 가는지 관찰하세요.`,
    names: c => `${c}개 종목`,
    others: "기타",
    rosterTitle: "진입 종목 목록",
    rosterNote: "순위에 오른 적 있는 종목을 한 행씩 표시. 헤더를 클릭해 정렬, 다시 클릭하면 역순. 차트의 모든 호버 값을 이 표에서 확인할 수 있습니다.\n진입 = 최근 순위권 구간 첫날의 거래량 특성 (파란색이 진할수록 강한 진입; 점에 호버하거나 탭하면 상세).",
    cols: ["티커", "섹터", "진입 일수", "최고 순위", "현재 순위", "연속 일수", "진입", "첫 진입", "최근 진입", "최신 점수"],
    eqLabels: ["거래량 미달 진입", "중립", "거래량 급증", "급증+클린"],
    eqTip: (v, d, day) => `거래량비 ${v}× · 분배일 ${d} · ${day} 진입`,
    eqFrozen: "등급은 진입일에 확정되며 이후 갱신되지 않습니다",
    eqFreshNote: "채움 = 이번 런 신규 진입; 링 = 과거 진입 — 등급은 진입일에 확정되며 이후 갱신되지 않습니다.",
    score: "점수", ret: "수익률", dd: "낙폭",
    formula: "점수 = 수익률 ÷ |낙폭|",
    formulaNote: " —— 낙폭 1%당 얻은 수익률(1% 미만 낙폭은 1%로 계산).",
    rankAt: r => `순위 #${r}`, rankBelow: r => `순위 #${r} (컷오프 미만)`,
    gapTag: g => ` · ${g}일 전 대비`,
    genBy: "", genAt: t => `로 ${t}에 생성 · 데이터 출처: `,
    sectorNames: {
      "Technology": "기술", "Financial Services": "금융 서비스", "Healthcare": "헬스케어",
      "Consumer Cyclical": "임의소비재", "Consumer Defensive": "필수소비재", "Industrials": "산업재",
      "Communication Services": "커뮤니케이션 서비스", "Energy": "에너지", "Basic Materials": "소재",
      "Real Estate": "부동산", "Utilities": "유틸리티", "Unknown": "미상", "Others": "기타",
    },
  },
};
const LANG = (() => {
  try {
    const s = localStorage.getItem("momentumScanLang");
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
    try { localStorage.setItem("momentumScanLang", sel.value); } catch (e) {}
    location.reload();
  });
  document.getElementById("h1-suffix").textContent = T.h1Suffix;
  document.getElementById("bump-title").textContent = T.bumpTitle;
  document.getElementById("heat-title").textContent = T.heatTitle;
  document.getElementById("sec-title").textContent = T.secTitle;
  document.getElementById("roster-title").textContent = T.rosterTitle;
  document.getElementById("roster-note").textContent = T.rosterNote;
  document.getElementById("heat-filter").setAttribute("aria-label", T.heatFilterLabel);
}

const N = DATA.topN, DAYS = DATA.days.length;
const WIN_TAG = DATA.window.shown < DATA.window.total ? T.winTag(DATA.window.shown, DATA.window.total) : "";
document.getElementById("heat-note").textContent = T.heatNote + WIN_TAG;
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
const gapTag = g => g > 1 ? T.gapTag(g) : "";
const rankTxt = r => r <= N ? T.rankAt(r) : T.rankBelow(r);
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
  [[T.score, p => p.s, v => v.toFixed(2)],
   [T.ret, p => p.ret, v => v.toFixed(1) + "%"],
   [T.dd, p => p.dd, v => v.toFixed(1) + "%"]].forEach(([lbl, get, fmt]) => {
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
  fc.textContent = T.formula;
  fn.appendChild(fc);
  fn.appendChild(document.createTextNode(T.formulaNote));
};
const tip = document.getElementById("tip");
function showTip(x, y, build) {
  tip.textContent = "";
  // Any caller repainting the tip owns it — clear the eq-dot marker so a
  // chart hover doesn't leave a stale "this dot's tip is open" flag.
  delete tip.dataset.eq;
  build(tip);
  tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = (y - r.height - 12 < 4 ? y + 16 : y - r.height - 12) + "px";
}
const hideTip = () => { tip.style.display = "none"; delete tip.dataset.eq; };
function tipRows(t, rows, keyColor, keyClass) {
  const head = div(null, t);
  // keyClass wins over keyColor: the eq-dot tip mirrors the exact dot
  // being hovered (filled or ring) instead of the generic line-segment key.
  if (keyClass) { const k = document.createElement("span"); k.className = keyClass; head.appendChild(k); }
  else if (keyColor) { const k = document.createElement("span"); k.className = "k"; k.style.background = keyColor; head.appendChild(k); }
  const v = document.createElement("span"); v.className = "v"; v.textContent = rows[0]; head.appendChild(v);
  rows.slice(1).forEach(r => div(null, t, r));
}

// ---- subtitle + KPI row ----
{
  const k = DATA.kpi;
  document.getElementById("subtitle").textContent =
    T.subtitle(k.span[0], k.span[1], k.runs, N);
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
  tile(T.kSpan, T.kDays(k.runs), `${k.span[0]} → ${k.span[1]}`);
  if (k.no1) tile(T.kNo1, tickerLink(k.no1.t), T.kScore(k.no1.s.toFixed(2)));
  if (k.streak) tile(T.kStreak, T.kDays(k.streak.n), tickerLink(k.streak.t));
  tile(T.kNew, `${k.newEntrants.length}`, tickerList(k.newEntrants));
  tile(T.kDrop, `${k.dropouts.length}`, tickerList(k.dropouts));
  tile(T.kTracked, `${k.tracked}`, T.kTrackedSub);
}

// ---- bump chart ----
const BUMP_MIN_APPS = 5;
{
  const onBoard = new Set(BOARD.map(s => s.t));
  const plotted = DATA.series.filter(s => s.apps >= BUMP_MIN_APPS || onBoard.has(s.t));
  document.getElementById("bump-note").textContent =
    T.bumpNote(plotted.length, BUMP_MIN_APPS, N) + WIN_TAG;
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
  k.appendChild(document.createTextNode(T.others));

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
        [best.s.t, `${DATA.days[d]} · ${rankTxt(best.p.r)}`,
         `${T.score} ${best.p.s.toFixed(2)}${pp ? dlt(best.p.s - pp.s, 2) : ""} · ${T.ret} ${best.p.ret.toFixed(1)}%${pp ? dlt(best.p.ret - pp.ret, 1) : ""}`,
         `${T.dd} ${best.p.dd.toFixed(1)}%${pp ? dlt(best.p.dd - pp.dd, 1) + gapTag(best.p.d - pp.d) : ""}`],
        slotColorOf(best.s.t) || "var(--ctx-line)"));
    } else { lit = null; restyle(pinned); hideTip(); }
  });
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility","hidden"); restyle(pinned); hideTip(); });
  svg.addEventListener("click", () => { pinned = (pinned === lit ? null : lit); restyle(pinned); renderDetail(det, pinned); });
}

// ---- sector stacked columns ----
{
  const names = DATA.sectors.names;
  document.getElementById("sec-note").textContent = T.secNote(N) + WIN_TAG;
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
    k.appendChild(document.createTextNode(secName(n)));
  });
  svg.addEventListener("pointermove", ev => {
    const t = ev.target;
    if (t.tagName === "rect" && t.dataset.i !== undefined) {
      const i = +t.dataset.i;
      showTip(ev.clientX, ev.clientY, tt => tipRows(tt,
        [T.names(t.dataset.c), `${secName(names[i])} · ${DATA.days[+t.dataset.d]}`],
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
         `${DATA.days[+t.dataset.d]} · ${rankTxt(+t.dataset.r)}`,
         `${T.score} ${(+t.dataset.s).toFixed(2)}${t.dataset.ds ? dlt(+t.dataset.ds, 2) : ""} · ${T.ret} ${(+t.dataset.ret).toFixed(1)}%${t.dataset.dret ? dlt(+t.dataset.dret, 1) : ""}`,
         `${T.dd} ${(+t.dataset.dd).toFixed(1)}%${t.dataset.ddd ? dlt(+t.dataset.ddd, 1) + gapTag(+t.dataset.gap) : ""}`]));
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
  new Map([[c5, T.geDays(c5)], [c20, T.geDays(c20)], [0, T.all]]).forEach((lbl, min) => {
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
  k.appendChild(document.createTextNode(T.heatLegendBelow));
}

// ---- table ----
{
  const tbl = document.getElementById("tbl");
  // dir is each column's first-click direction: rank-like asc (best first), days/dates/score desc (largest/newest first)
  const COLS = [
    {h: T.cols[0], v: s => s.t,              dir: 1},
    {h: T.cols[1], v: s => s.sec,            dir: 1},
    {h: T.cols[2], v: s => s.apps,           dir: -1},
    {h: T.cols[3], v: s => s.best,           dir: 1},
    {h: T.cols[4], v: s => s.latest,         dir: 1},
    {h: T.cols[5], v: s => s.streak || null, dir: -1},
    {h: T.cols[6], v: s => s.eq,             dir: -1},
    {h: T.cols[7], v: s => s.firstD,         dir: -1},
    {h: T.cols[8], v: s => s.lastD,          dir: -1},
    {h: T.cols[9], v: s => s.score,          dir: -1},
  ];
  const EQ_COL = 6;
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
      [s.t, secName(s.sec), s.apps, "#"+s.best, s.latest ? "#"+s.latest : "—",
       s.streak || "—", s.eq, s.first, s.last,
       s.score !== null ? s.score.toFixed(2) : "—"]
      .forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 0) { td.className = "tk"; td.appendChild(tickerLink(c)); }
        else if (i === EQ_COL) {
          if (c === null || c === undefined) td.textContent = "—";
          else {
            const dot = document.createElement("span");
            dot.className = "eqdot eq" + c + (s.eqNew ? "" : " ring");
            dot.setAttribute("aria-label",
              `${T.eqLabels[c]} · ${T.eqTip(s.eqv, s.eqd, s.eqDay)} · ${T.eqFrozen}`);
            dot.setAttribute("role", "img");
            dot.tabIndex = 0;
            // Shared #tip layer instead of a native title: works for
            // hover, tap (mobile), and keyboard focus alike.
            const show = () => {
              const r = dot.getBoundingClientRect();
              showTip(r.left + r.width / 2, r.top, t => tipRows(
                t, [T.eqLabels[c], T.eqTip(s.eqv, s.eqd, s.eqDay), T.eqFrozen],
                null, "eqdot eq" + c + (s.eqNew ? "" : " ring")));
              tip.dataset.eq = s.t;
            };
            dot.addEventListener("pointerenter", ev => {
              if (ev.pointerType === "mouse") show();
            });
            dot.addEventListener("pointerleave", ev => {
              if (ev.pointerType === "mouse") hideTip();
            });
            dot.addEventListener("click", ev => {
              ev.stopPropagation();
              if (tip.style.display === "block" && tip.dataset.eq === s.t) hideTip();
              else show();
            });
            dot.addEventListener("focus", show);
            dot.addEventListener("blur", hideTip);
            td.appendChild(dot);
          }
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

  // Entry-quality legend under the table — same pattern as the heatmap's,
  // strongest tier first to match the ramp's reading direction.
  const leg = document.getElementById("roster-legend");
  [3, 2, 1, 0].forEach(q => {
    const k = div("key", leg);
    const d = document.createElement("span");
    d.className = "eqdot eq" + q;
    k.appendChild(d);
    k.appendChild(document.createTextNode(T.eqLabels[q]));
  });
  // Freshness is explained in words, not sample dots: filled = spell
  // began on the latest run; ring = older entry, tier frozen.
  leg.appendChild(document.createTextNode(T.eqFreshNote));
}

{
  const foot = document.getElementById("foot");
  foot.append(T.genBy);
  const fa = document.createElement("a");
  fa.href = "https://github.com/mthli/skills/blob/master/momentum-scan/scripts/render_history_html.py";
  fa.target = "_blank"; fa.rel = "noopener";
  fa.textContent = "render_history_html.py";
  foot.append(fa, T.genAt(GENERATED));
  const sa = document.createElement("a");
  sa.href = "https://github.com/mthli/skills/blob/master/momentum-scan/state/history.csv";
  sa.target = "_blank"; sa.rel = "noopener";
  sa.textContent = "history.csv";
  foot.append(sa);
}
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
    k = payload["kpi"]
    meta_desc = (
        f"Daily top-{args.top_n} US large-cap momentum board, "
        f"{k['span'][0]} to {k['span'][1]} ({k['runs']} trading days, "
        f"{k['tracked']} tickers tracked): rank trajectories, board heatmap, "
        "sector mix and a sortable roster of every name that made the board."
    )
    data_json = json.dumps(payload, separators=(
        ",", ":")).replace("</", r"<\/")
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    html = html.replace("__META_DESC__", meta_desc)
    html = html.replace("__GENERATED__", generated)
    out = Path(args.out)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, "
          f"{payload['kpi']['runs']} days x {payload['kpi']['tracked']} tickers)")


if __name__ == "__main__":
    main()
