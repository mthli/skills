#!/usr/bin/env python3
"""Cross-scan aggregator.

Reads the latest snapshots from the four sister scans
(momentum-scan, base-breakout-scan, mean-reversion-scan, unusual-options-scan)
and finds tickers appearing in two or more of them on the same day.

Pure stdlib — no pandas, no yfinance. The sister scans already paid the
yfinance cost; this just joins their state files.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

DEFAULT_SCANS_DIR = Path.home() / ".claude" / "skills"
SCAN_NAMES = ["momentum", "base-breakout", "mean-reversion", "unusual-options"]
SCAN_DIR_MAP = {
    "momentum": "momentum-scan",
    "base-breakout": "base-breakout-scan",
    "mean-reversion": "mean-reversion-scan",
    "unusual-options": "unusual-options-scan",
}
# Each scan has its own flag to override the "don't save on non-trading days"
# guard. The CSV-based scans share `--save-stale`; UOA uses `--allow-same-day`.
# When --refresh is invoked, we always pass the appropriate one — otherwise a
# weekend/holiday refresh would run the scan but silently fail to save.
SAVE_STALE_FLAG = {
    "momentum": "--save-stale",
    "base-breakout": "--save-stale",
    "mean-reversion": "--save-stale",
    "unusual-options": "--allow-same-day",
}
STALE_DAYS = 3  # snapshots older than this get a ⚠️ STALE flag

# All four sister scans share these dependencies — they're all yfinance + pandas
# + numpy. Keeping them in one list means --refresh works without per-scan
# customization. If a scan ever grows a new dep, update here.
SCAN_DEPS = ["yfinance>=1.3,<2", "pandas>=2", "numpy>=1.24,<3"]
# 10 minutes per scan is more than enough — UOA's full 1000-ticker run is ~1m,
# the others are ~1-3m. Caps runaway hangs without surprising the user.
REFRESH_TIMEOUT_SEC = 600


@dataclass
class ScanRow:
    """One ticker's appearance in one scan's latest snapshot."""
    ticker: str
    rank: int
    score: float | None = None
    sector: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ScanSnapshot:
    """A scan's most-recent snapshot."""
    scan_name: str
    snapshot_date: date | None  # None if scan has no data
    rows: list[ScanRow]
    error: str | None = None  # set if the scan couldn't be read


# -----------------------------------------------------------------------
# On-demand refresh of sister scans.
# -----------------------------------------------------------------------
#
# When --refresh is set, we re-run any scan whose latest snapshot is older
# than STALE_DAYS by shelling out to its scripts/scan.py via `uv run`.
# Failures don't abort cross-scan — they log a warning and we fall through
# to whatever's already on disk. The freshness header in the final report
# will surface what's stale, so the human always knows what they got.

_FRESHNESS_CACHE: dict[tuple[str, str], date | None] = {}


def _read_latest_snapshot_date(scan_name: str, scan_dir: Path) -> date | None:
    """Cheap freshness probe — read just enough of each scan's state to
    pull the most-recent snapshot date. None if no prior data exists.

    Memoized per (scan_name, scan_dir) to avoid re-reading the same CSV
    when --refresh both probes before and verifies after refresh. The
    cache is explicitly invalidated by `_invalidate_freshness_cache`
    after a refresh writes new data."""
    key = (scan_name, str(scan_dir))
    if key in _FRESHNESS_CACHE:
        return _FRESHNESS_CACHE[key]
    result = _compute_latest_snapshot_date(scan_name, scan_dir)
    _FRESHNESS_CACHE[key] = result
    return result


def _invalidate_freshness_cache(scan_name: str, scan_dir: Path) -> None:
    _FRESHNESS_CACHE.pop((scan_name, str(scan_dir)), None)


def _compute_latest_snapshot_date(scan_name: str,
                                  scan_dir: Path) -> date | None:
    if scan_name == "unusual-options":
        history_dir = scan_dir / "state" / "history"
        if not history_dir.exists():
            return None
        snapshots = sorted(history_dir.glob("*.md"))
        if not snapshots:
            return None
        try:
            return date.fromisoformat(snapshots[-1].stem)
        except ValueError:
            return None
    # CSV scans: pull the max run_id without fully parsing the file.
    csv_path = scan_dir / "state" / "history.csv"
    if not csv_path.exists():
        return None
    max_rid = ""
    try:
        with csv_path.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                rid = (row.get("run_id") or "").strip()
                if rid > max_rid:
                    max_rid = rid
    except OSError:
        return None
    return _parse_run_id_to_date(max_rid) if max_rid else None


def _refresh_one_scan(scan_name: str, scan_dir: Path) -> str | None:
    """Re-run a sister scan via `uv run`. Returns None on success, or a
    short error string on failure (timeout, missing script, non-zero exit).

    Note: success here means "exit 0", not "snapshot actually advanced".
    The caller verifies the snapshot date moved forward before reporting
    success — sister scans can exit 0 without saving (e.g., when the
    trading-day guard fires and we forgot to pass --save-stale)."""
    script_path = scan_dir / "scripts" / "scan.py"
    if not script_path.exists():
        return f"missing script at {script_path}"

    cmd = ["uv", "run"]
    for dep in SCAN_DEPS:
        cmd += ["--with", dep]
    cmd += ["python", str(script_path)]

    # Always pass the per-scan "save even on non-trading days" override.
    # Without this, a weekend/holiday refresh would run the scan, exit 0,
    # and leave us looking at the same stale snapshot we started with.
    cmd.append(SAVE_STALE_FLAG[scan_name])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=REFRESH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"timed out after {REFRESH_TIMEOUT_SEC}s"
    except FileNotFoundError:
        return "`uv` not found in PATH"

    if result.returncode != 0:
        # Errors usually go to stderr; fall back to stdout only if stderr
        # is empty. Concatenating both can chop a long stderr message in the
        # middle and run it into stdout content, producing confusing output.
        err_output = (result.stderr or result.stdout or "").strip()
        tail = err_output[-400:].strip()
        return f"exit {result.returncode}: {tail or '(no output)'}"
    return None


def maybe_refresh_scans(requested: list[str], scans_dir: Path,
                        threshold_days: int, today: date) -> None:
    """For each scan in `requested`, check freshness; if older than
    `threshold_days` (or missing entirely), re-run it. Output goes to stderr
    so the markdown/JSON on stdout stays clean."""
    if shutil.which("uv") is None:
        print("⚠️  --refresh requested but `uv` is not in PATH. "
              "Skipping refresh; using whatever is on disk.",
              file=sys.stderr)
        return

    print(f"--refresh: checking freshness "
          f"(threshold = {threshold_days} days)...", file=sys.stderr)
    print(f"  (refreshes run silently for ~1-3 min each; no progress shown)",
          file=sys.stderr)
    for name in requested:
        scan_dir = scans_dir / SCAN_DIR_MAP[name]
        pre_date = _read_latest_snapshot_date(name, scan_dir)
        age = (today - pre_date).days if pre_date else None

        if age is None:
            reason = "no prior snapshot"
        elif age > threshold_days:
            reason = f"last snapshot {age}d old"
        else:
            print(f"  {name}-scan: {age}d old — fresh, skipping.",
                  file=sys.stderr)
            continue

        print(f"  Refreshing {name}-scan ({reason})...", file=sys.stderr)
        err = _refresh_one_scan(name, scan_dir)
        if err is not None:
            print(f"  ✗ {name}-scan refresh failed: {err}", file=sys.stderr)
            print(f"    Continuing with existing snapshot (if any).",
                  file=sys.stderr)
            continue

        # Refresh wrote new state to disk; the memoized freshness date is
        # now stale. Drop the cache entry so the verify-step re-reads.
        _invalidate_freshness_cache(name, scan_dir)

        # Sister scans can exit 0 without writing — verify the snapshot
        # date actually moved forward before claiming success.
        post_date = _read_latest_snapshot_date(name, scan_dir)
        if post_date is None:
            print(f"  ⚠️  {name}-scan: subprocess exited 0 but no snapshot "
                  f"was written. Check the scan's save guards.",
                  file=sys.stderr)
        elif pre_date is not None and post_date <= pre_date:
            print(f"  ⚠️  {name}-scan: subprocess exited 0 but snapshot "
                  f"date didn't advance (still {post_date}). "
                  f"The scan may have refused to save.",
                  file=sys.stderr)
        else:
            print(f"  ✓ {name}-scan refreshed → {post_date}.",
                  file=sys.stderr)


# -----------------------------------------------------------------------
# CSV-format scans (momentum, base-breakout, mean-reversion).
# -----------------------------------------------------------------------

def _parse_run_id_to_date(run_id: str) -> date | None:
    """The CSV scans use YYYYMMDD integer-like strings as run_id."""
    try:
        return datetime.strptime(run_id, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _load_sectors(scan_dir: Path) -> dict[str, str]:
    """Each sister scan caches sectors at state/sectors.json."""
    p = scan_dir / "state" / "sectors.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, str] = {}
    for tk, info in raw.items():
        if isinstance(info, dict):
            sector = info.get("sector")
            if sector:
                out[tk] = sector
    return out


def load_csv_scan(scan_name: str, scan_dir: Path,
                  target_date: date | None) -> ScanSnapshot:
    csv_path = scan_dir / "state" / "history.csv"
    if not csv_path.exists():
        return ScanSnapshot(scan_name, None, [],
                            error=f"no history.csv at {csv_path}")

    sectors = _load_sectors(scan_dir)

    # Group rows by run_id → list, picking the right one after.
    by_run: dict[str, list[dict]] = defaultdict(list)
    try:
        with csv_path.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                rid = row.get("run_id", "").strip()
                if not rid:
                    continue
                by_run[rid].append(row)
    except OSError as e:
        return ScanSnapshot(scan_name, None, [], error=f"read error: {e}")

    if not by_run:
        return ScanSnapshot(scan_name, None, [], error="history.csv is empty")

    if target_date is not None:
        wanted = target_date.strftime("%Y%m%d")
        if wanted not in by_run:
            return ScanSnapshot(scan_name, None, [],
                                error=f"no snapshot for {target_date}")
        chosen_rid = wanted
    else:
        chosen_rid = max(by_run.keys())

    snapshot_date = _parse_run_id_to_date(chosen_rid)

    # Score column varies by scan; rank is always 'rank'.
    score_field = {
        "momentum": "score",
        "base-breakout": "base_score",
        "mean-reversion": "score",
    }.get(scan_name)

    rows: list[ScanRow] = []
    for raw in sorted(by_run[chosen_rid],
                      key=lambda r: _safe_int(r.get("rank"))):
        ticker = (raw.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        rank = _safe_int(raw.get("rank"))
        score = _safe_float(raw.get(score_field)) if score_field else None
        # Keep the full raw row as `extra` for the composite-read logic to peek at.
        rows.append(ScanRow(
            ticker=ticker,
            rank=rank,
            score=score,
            sector=sectors.get(ticker),
            extra={k: v for k, v in raw.items() if k not in {
                "ticker", "rank"}},
        ))

    return ScanSnapshot(scan_name, snapshot_date, rows)


# -----------------------------------------------------------------------
# UOA snapshot (per-contract markdown table, one file per day).
# -----------------------------------------------------------------------
#
# Unlike the three CSV scans (one ranked-row per ticker), the UOA snapshot
# is a raw per-contract dump — multiple rows per ticker. We re-aggregate to
# per-ticker here using the same scoring formula UOA uses for its display
# ranking, so cross-scan's ranks line up with what the user sees when they
# run /unusual-options-scan directly.

_UOA_HEADER_RE = re.compile(r"^\|\s*ticker\s*\|", re.I)


def load_uoa_scan(scan_dir: Path, target_date: date | None) -> ScanSnapshot:
    history_dir = scan_dir / "state" / "history"
    if not history_dir.exists():
        return ScanSnapshot("unusual-options", None, [],
                            error=f"no history dir at {history_dir}")

    snapshots = sorted(history_dir.glob("*.md"))
    if not snapshots:
        return ScanSnapshot("unusual-options", None, [],
                            error="history dir is empty")

    if target_date is not None:
        wanted = history_dir / f"{target_date.isoformat()}.md"
        if not wanted.exists():
            return ScanSnapshot("unusual-options", None, [],
                                error=f"no snapshot for {target_date}")
        chosen = wanted
    else:
        chosen = snapshots[-1]  # lexicographic = chronological for ISO dates

    try:
        snapshot_date = date.fromisoformat(chosen.stem)
    except ValueError:
        snapshot_date = None

    try:
        text = chosen.read_text()
    except OSError as e:
        return ScanSnapshot("unusual-options", None, [], error=f"read: {e}")

    contracts = _parse_uoa_contracts(text)
    rows = _aggregate_uoa_per_ticker(contracts)

    sectors = _load_sectors(scan_dir)
    for r in rows:
        if not r.sector and r.ticker in sectors:
            r.sector = sectors[r.ticker]

    return ScanSnapshot("unusual-options", snapshot_date, rows)


def _parse_uoa_contracts(text: str) -> list[dict]:
    """Parse the per-contract markdown table to a list of dicts. Keys match
    the UOA scan's SNAPSHOT_COLS so the aggregation step can compute the
    same ticker-level score the scan uses."""
    lines = text.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if _UOA_HEADER_RE.match(ln):
            header_idx = i
            break
    if header_idx is None:
        return []

    header_cols = [c.strip() for c in _split_md_row(lines[header_idx])]
    rows: list[dict] = []
    # Skip header + separator
    for ln in lines[header_idx + 2:]:
        s = ln.strip()
        if not s.startswith("|"):
            break
        cells = _split_md_row(ln)
        if len(cells) < len(header_cols):
            continue
        rows.append(dict(zip(header_cols, cells)))
    return rows


def _aggregate_uoa_per_ticker(contracts: list[dict]) -> list[ScanRow]:
    """Group contracts by ticker, compute ticker score (same formula UOA's
    own scan uses), assign ranks. Returns ScanRow per ticker with the
    highest-Vol/OI contract's details surfaced in extras."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for c in contracts:
        tk = (c.get("ticker") or "").strip().upper()
        if not tk:
            continue
        by_ticker[tk].append(c)

    scored: list[tuple[float, str, dict, list[dict]]] = []
    for ticker, ticker_rows in by_ticker.items():
        # Sort the ticker's contracts by vol_oi desc — first is "top contract".
        ticker_rows.sort(key=lambda r: _safe_float(r.get("vol_oi")) or 0,
                         reverse=True)
        top = ticker_rows[0]
        score = _uoa_ticker_score(ticker_rows)
        scored.append((score, ticker, top, ticker_rows))

    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[ScanRow] = []
    for rank, (score, ticker, top, ticker_rows) in enumerate(scored, 1):
        # Combine flags from all contracts for the ticker-level view.
        combined_flags = "".join(sorted({
            ch for r in ticker_rows
            for ch in (r.get("flags") or "")
            if ch in "⚡🎯🔥📊💰"
        }))
        extra = {
            "vol_oi": top.get("vol_oi", ""),
            "top_notional": top.get("notional", ""),
            "top_flags": top.get("flags", ""),
            "ticker_flags": combined_flags,
            "ticker_cp_ratio": top.get("ticker_cp_ratio", ""),
            "ticker_notional_adv_mult": top.get("ticker_notional_adv_mult", ""),
            "top_strike": top.get("strike", ""),
            "top_type": top.get("type", ""),
            "top_expiry": top.get("expiry", ""),
            "top_dte": top.get("dte", ""),
            "num_contracts": len(ticker_rows),
        }
        out.append(ScanRow(
            ticker=ticker,
            rank=rank,
            score=score,
            sector=None,  # Filled from sectors.json after return.
            extra=extra,
        ))
    return out


def _uoa_ticker_score(ticker_rows: list[dict]) -> float:
    """Same formula as the UOA scan's `ticker_score` function — kept in sync
    so cross-scan ranks line up with what the user sees in /unusual-options-scan."""
    flagged_notional = sum(_safe_float(r.get("notional")) or 0
                           for r in ticker_rows)
    has_catalyst = any("🎯" in (r.get("flags") or "") for r in ticker_rows)
    has_squeeze = any("🔥" in (r.get("flags") or "") for r in ticker_rows)
    # ticker_cp_ratio and ticker_notional_adv_mult are ticker-level aggregates
    # that UOA repeats identically on every contract row for the ticker.
    # Reading from ticker_rows[0] is therefore safe; if UOA ever stops doing
    # that, this score will quietly drift and the comment is the canary.
    notnl_adv = _safe_float(
        ticker_rows[0].get("ticker_notional_adv_mult")) or 0
    cp = _safe_float(ticker_rows[0].get("ticker_cp_ratio")) or 1
    skew = max(cp, 1 / cp if cp > 0 else 1)
    return (
        math.log1p(flagged_notional / 1e5) * 10
        + (15 if has_catalyst else 0)
        + (20 if has_squeeze else 0)
        + min(notnl_adv, 5) * 5
        + min(math.log1p(skew - 1), 2) * 5
    )


def _split_md_row(line: str) -> list[str]:
    parts = line.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


# -----------------------------------------------------------------------
# Utilities.
# -----------------------------------------------------------------------

def _safe_int(v, default: int = 0) -> int:
    # Default is 0 because rank fields in the source CSVs are always 1-indexed;
    # the rare parse failure won't collide with a real rank.
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _staleness_days(snapshot: date | None, today: date) -> int | None:
    if snapshot is None:
        return None
    return (today - snapshot).days


def _fmt_notional(x: float | None) -> str:
    if x is None:
        return ""
    if x >= 1e6:
        return f"${x/1e6:.1f}M"
    if x >= 1e3:
        return f"${x/1e3:.0f}k"
    return f"${x:.0f}"


# -----------------------------------------------------------------------
# Cross-scan join.
# -----------------------------------------------------------------------

@dataclass
class OverlapRow:
    ticker: str
    sector: str | None
    scans: list[str]
    per_scan: dict[str, ScanRow]

    @property
    def overlap_count(self) -> int:
        return len(self.scans)


def join(snapshots: list[ScanSnapshot], min_overlap: int) -> list[OverlapRow]:
    """Build {ticker → list[(scan_name, ScanRow)]} then filter by min_overlap."""
    by_ticker: dict[str, dict[str, ScanRow]] = defaultdict(dict)
    sector_by_ticker: dict[str, str] = {}

    for snap in snapshots:
        if snap.error or not snap.rows:
            continue
        for row in snap.rows:
            by_ticker[row.ticker][snap.scan_name] = row
            if row.sector and row.ticker not in sector_by_ticker:
                sector_by_ticker[row.ticker] = row.sector

    overlaps: list[OverlapRow] = []
    for ticker, per_scan in by_ticker.items():
        if len(per_scan) < min_overlap:
            continue
        # Preserve a stable scan ordering for display.
        scans = [n for n in SCAN_NAMES if n in per_scan]
        overlaps.append(OverlapRow(
            ticker=ticker,
            sector=sector_by_ticker.get(ticker),
            scans=scans,
            per_scan=per_scan,
        ))

    # Sort: more overlap first; within same overlap count, by avg rank ascending.
    def sort_key(o: OverlapRow):
        avg_rank = sum(o.per_scan[s].rank for s in o.scans) / len(o.scans)
        return (-o.overlap_count, avg_rank)

    overlaps.sort(key=sort_key)
    return overlaps


# -----------------------------------------------------------------------
# Composite read — heuristic, not a score.
# -----------------------------------------------------------------------

def composite_read(row: OverlapRow) -> str:
    s = set(row.scans)
    notes = []

    def uoa_direction() -> str | None:
        """Returns 'call-heavy', 'put-heavy', or None based on the ticker's
        call/put ratio from UOA."""
        if "unusual-options" not in row.per_scan:
            return None
        cp = row.per_scan["unusual-options"].extra.get("ticker_cp_ratio")
        v = _safe_float(cp)
        if v is None:
            return None
        if v >= 3.0:
            return "call-heavy"
        if v <= 0.33:
            return "put-heavy"
        return None

    has_mom = "momentum" in s
    has_base = "base-breakout" in s
    has_mr = "mean-reversion" in s
    has_uoa = "unusual-options" in s
    uoa_dir = uoa_direction()
    added_pullback = False

    if has_base and has_uoa and uoa_dir == "call-heavy":
        notes.append("⭐ pre-breakout + call flow — best pattern")
    elif has_base and has_uoa and uoa_dir == "put-heavy":
        notes.append("base setup but put-flow disagrees — caution")
    elif has_base and has_uoa:
        notes.append("base setup + options activity")

    if has_mom and has_mr:
        notes.append("pullback in a leader")
        added_pullback = True
    if has_mom and has_uoa and uoa_dir == "call-heavy" and not added_pullback:
        notes.append("trend + call-flow confirmation")
    if has_mom and has_uoa and uoa_dir == "put-heavy":
        notes.append("⚠️ leader with bearish positioning")
    if has_mom and has_base and not (has_mr or has_uoa):
        notes.append("leader still consolidating")
    if has_mr and has_base and not has_mom:
        notes.append("oversold base candidate")

    if has_mom and has_base and has_mr:
        notes.append("leader, in base, pulled back hard — rare")

    if not notes:
        notes.append(f"in {row.overlap_count} scans")
    return " · ".join(notes)


# -----------------------------------------------------------------------
# Formatting.
# -----------------------------------------------------------------------

def _fmt_per_scan_cell(scan_name: str, row: ScanRow | None) -> str:
    if row is None:
        return "—"
    if scan_name == "momentum":
        score = f" ({row.score:.1f})" if row.score is not None else ""
        return f"#{row.rank}{score}"
    if scan_name == "base-breakout":
        score = f" (base {row.score:.0f})" if row.score is not None else ""
        return f"#{row.rank}{score}"
    if scan_name == "mean-reversion":
        rsi = row.extra.get("rsi2")
        rsi_part = f" (RSI2 {rsi})" if rsi else ""
        return f"#{row.rank}{rsi_part}"
    if scan_name == "unusual-options":
        voi_f = _safe_float(row.extra.get("vol_oi"))
        voi = f"V/O {voi_f:.0f}" if voi_f is not None else ""
        notnl = _fmt_notional(_safe_float(row.extra.get("top_notional")))
        flags = row.extra.get("ticker_flags") or ""
        bits = [b for b in [voi, notnl, flags] if b]
        return f"#{row.rank} ({', '.join(bits)})" if bits else f"#{row.rank}"
    return f"#{row.rank}"


def render_markdown(snapshots: list[ScanSnapshot],
                    overlaps: list[OverlapRow],
                    *, today: date, target_date: date | None,
                    top_n: int, min_overlap: int) -> str:
    out: list[str] = []
    if target_date is not None:
        out.append(f"# Cross-scan overlap — {target_date} (strict date)")
    else:
        out.append(f"# Cross-scan overlap — {today} (latest each)")
    out.append("")

    # Freshness header — in strict-date mode, measure staleness against the
    # requested date, not today's clock, so historical queries don't generate
    # spurious "STALE" warnings against snapshots that match the asked-for date.
    reference_date = target_date or today
    out.append("**Scan freshness**:")
    for snap in snapshots:
        if snap.error:
            out.append(f"- {snap.scan_name}-scan: ❌ {snap.error}")
            continue
        age = _staleness_days(snap.snapshot_date, reference_date)
        stale_tag = (f" ⚠️ STALE (>{STALE_DAYS} days old)"
                     if age is not None and age > STALE_DAYS else "")
        out.append(
            f"- {snap.scan_name}-scan: {snap.snapshot_date} "
            f"({len(snap.rows)} tickers){stale_tag}"
        )
    out.append("")

    # Summary line
    by_count = defaultdict(int)
    for o in overlaps:
        by_count[o.overlap_count] += 1
    parts = [f"{by_count[c]} in ≥{c} scans"
             for c in sorted(by_count.keys(), reverse=True)
             if c >= min_overlap]
    out.append("**Overlap summary**: " +
               " · ".join(parts or ["(no overlaps)"]))
    out.append("")

    if not overlaps:
        out.append("_No tickers appeared in the required number of scans today._")
        out.append("")
        out.append("This is itself a signal — often the market is rotating or "
                   "the scans simply disagree. Try lowering `--min-overlap`, "
                   "or check the freshness header above for stale data.")
        return "\n".join(out)

    # Split into tiers (3+ then 2). top_n is a hard cap on total rows shown,
    # with tier_3plus getting priority — if 3+ overlaps ever exceed top_n,
    # we truncate that tier too rather than silently overflow the cap.
    tier_3plus = [o for o in overlaps if o.overlap_count >= 3]
    tier_2 = [o for o in overlaps if o.overlap_count == 2]

    if tier_3plus:
        shown_3plus = tier_3plus[:top_n]
        header_3plus = "## Tickers in ≥3 scans (highest conviction)"
        if len(shown_3plus) < len(tier_3plus):
            header_3plus += f" — {len(shown_3plus)} of {len(tier_3plus)} shown"
        out.append(header_3plus)
        out.append("")
        out.extend(_render_overlap_table(shown_3plus))
        out.append("")

    if tier_2 and min_overlap <= 2:
        remaining = max(0, top_n - len(tier_3plus))
        if remaining > 0:
            shown = tier_2[:remaining]
            out.append(
                f"## Tickers in 2 scans (worth a look) — {len(shown)} of {len(tier_2)} shown")
            out.append("")
            out.extend(_render_overlap_table(shown))
            out.append("")

    return "\n".join(out)


def _render_overlap_table(overlaps: list[OverlapRow]) -> list[str]:
    rows = ["| Ticker | Sector | # | mom | base | mr | uoa | Composite read |",
            "|---|---|---|---|---|---|---|---|"]
    for o in overlaps:
        cells = [
            f"**{o.ticker}**",
            (o.sector or "")[:12],
            str(o.overlap_count),
            _fmt_per_scan_cell("momentum", o.per_scan.get("momentum")),
            _fmt_per_scan_cell(
                "base-breakout", o.per_scan.get("base-breakout")),
            _fmt_per_scan_cell(
                "mean-reversion", o.per_scan.get("mean-reversion")),
            _fmt_per_scan_cell("unusual-options",
                               o.per_scan.get("unusual-options")),
            composite_read(o),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def render_json(snapshots: list[ScanSnapshot],
                overlaps: list[OverlapRow], *,
                today: date, target_date: date | None,
                min_overlap: int) -> str:
    payload = {
        "generated_at": datetime.now().isoformat(),
        "today": today.isoformat(),
        "target_date": target_date.isoformat() if target_date else None,
        "min_overlap": min_overlap,
        "scans": [
            {
                "name": snap.scan_name,
                "snapshot_date": (snap.snapshot_date.isoformat()
                                  if snap.snapshot_date else None),
                "row_count": len(snap.rows),
                "stale_days": _staleness_days(snap.snapshot_date, today),
                "error": snap.error,
            }
            for snap in snapshots
        ],
        "overlaps": [
            {
                "ticker": o.ticker,
                "sector": o.sector,
                "overlap_count": o.overlap_count,
                "scans": o.scans,
                "per_scan": {
                    name: {
                        "rank": r.rank,
                        "score": r.score,
                        "extra": r.extra,
                    }
                    for name, r in o.per_scan.items()
                },
                "composite_read": composite_read(o),
            }
            for o in overlaps
        ],
    }
    return json.dumps(payload, indent=2, default=str)


# -----------------------------------------------------------------------
# Main.
# -----------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=str, default=None,
                    help="Target date YYYY-MM-DD (strict alignment). "
                         "If unset, each scan uses its own latest snapshot.")
    ap.add_argument("--min-overlap", type=int, default=2,
                    help="Min number of scans a ticker must appear in.")
    ap.add_argument("--top-n", type=int, default=30,
                    help="Max rows to display.")
    ap.add_argument("--scans", type=str,
                    default=",".join(SCAN_NAMES),
                    help="Comma-separated subset of scans to include.")
    ap.add_argument(
        "--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--scans-dir",
                    type=lambda s: Path(s).expanduser(),
                    default=DEFAULT_SCANS_DIR,
                    help="Parent directory holding the sister scan folders. "
                         "`~` is expanded.")
    ap.add_argument("--refresh", action="store_true",
                    help=f"Auto-refresh any requested scan whose latest "
                    f"snapshot is older than {STALE_DAYS} days (or "
                    f"missing entirely). Re-runs via `uv run`; "
                    f"requires `uv` in PATH. Ignored when --date is "
                    f"set (strict-date queries don't benefit from "
                    f"running new scans).")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    today = date.today()
    target_date = None
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"--date must be YYYY-MM-DD, got {args.date!r}",
                  file=sys.stderr)
            return 2

    requested = [s.strip() for s in args.scans.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SCAN_NAMES]
    if unknown:
        print(f"Unknown scan name(s): {unknown}. Valid: {SCAN_NAMES}",
              file=sys.stderr)
        return 2

    if args.refresh:
        if target_date is not None:
            print("⚠️  --refresh ignored when --date is set (strict-date mode "
                  "queries a specific historical date; refreshing wouldn't "
                  "help).", file=sys.stderr)
        else:
            maybe_refresh_scans(requested, args.scans_dir, STALE_DAYS, today)

    snapshots: list[ScanSnapshot] = []
    for name in requested:
        scan_dir = args.scans_dir / SCAN_DIR_MAP[name]
        if name == "unusual-options":
            snap = load_uoa_scan(scan_dir, target_date)
        else:
            snap = load_csv_scan(name, scan_dir, target_date)
        snapshots.append(snap)

    overlaps = join(snapshots, args.min_overlap)

    if args.format == "json":
        print(render_json(snapshots, overlaps,
                          today=today, target_date=target_date,
                          min_overlap=args.min_overlap))
    else:
        print(render_markdown(snapshots, overlaps,
                              today=today, target_date=target_date,
                              top_n=args.top_n,
                              min_overlap=args.min_overlap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
