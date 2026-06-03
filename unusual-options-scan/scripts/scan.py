"""
Unusual-options-activity scan: find US large-cap equities with anomalous
options flow in today's EOD snapshot — single-contract Vol/OI spikes,
far-OTM short-DTE accumulation, extreme C/P ratios, and outsized total
options notional vs the equity's average dollar volume.

Cadence-agnostic but designed for one run per US trading day after the
close (when OI has refreshed). Cross-day signals (OI growth confirmation,
repeat-offender streak) populate once 2+ daily snapshots exist.

State lives in `state/history/YYYY-MM-DD.md` — one human-readable +
git-diffable markdown table per day. No SQLite/parquet; volumes are small.

Usage:
  python scan.py                          # full run with default params
  python scan.py --min-vol-oi 5           # high-conviction only
  python scan.py --num-expiries 1         # fastest (single expiry)
  python scan.py --show-history           # summary, no new scan
  python scan.py --clear-history          # wipe state/history/*.md (no prompt)
"""
from yfinance import EquityQuery
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
    USThanksgivingDay, nearest_workday,
)
import yfinance as yf
import pandas as pd
import numpy as np
import argparse
import json
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
HISTORY_DIR = STATE_DIR / "history"
UNIVERSE_FILE = STATE_DIR / "universe.txt"
SECTORS_FILE = STATE_DIR / "sectors.json"
UNIVERSE_TTL_DAYS = 7
SCREENER_PAGE_SIZE = 250
SCREENER_PAGE_SLEEP_SEC = 0.2
SCREENER_MAX_PAGES = 20
SECTORS_TTL_DAYS = 30
MARKET_TZ = ZoneInfo("America/New_York")

# Markdown snapshot columns (one row per anomaly contract). Order matters
# — tomorrow's run parses these positionally if header drifts.
SNAPSHOT_COLS = [
    "ticker", "expiry", "strike", "type",
    "vol", "oi", "vol_oi", "last_price", "notional",
    "iv", "dist_pct", "dte",
    "ticker_cp_ratio", "ticker_total_notional", "ticker_notional_adv_mult",
    "flags",
]

# Equity-bars window: 30 trading days is plenty for 20-day ADV.
EQUITY_HISTORY_DAYS = 45


# -----------------------------------------------------------------------
# NYSE calendar (shared convention with sister skills).
# -----------------------------------------------------------------------

class _NYSECalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday,
                start_date="2022-06-19"),
        Holiday("Independence Day", month=7,
                day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def is_nyse_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty


# -----------------------------------------------------------------------
# Universe management (lifted from sister skills, lightly trimmed).
# -----------------------------------------------------------------------

def _screen_with_offset(query, page_size, offset):
    try:
        return yf.screen(query, sortField="intradaymarketcap",
                         sortAsc=False, size=page_size, offset=offset), True
    except TypeError as e:
        if "offset" not in str(e):
            raise
        return yf.screen(query, sortField="intradaymarketcap",
                         sortAsc=False, size=page_size), False


def refresh_universe(min_market_cap: float, min_volume: int,
                     count: int | None) -> list[str]:
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_market_cap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    tickers: list[str] = []
    seen: set[str] = set()
    offset = 0
    target = count
    started = time.monotonic()
    pages = 0
    while target is None or len(tickers) < target:
        if pages >= SCREENER_MAX_PAGES:
            print(f"refresh_universe: hit SCREENER_MAX_PAGES={SCREENER_MAX_PAGES} "
                  f"backstop at {len(tickers)} tickers.", file=sys.stderr)
            break
        if pages > 0:
            time.sleep(SCREENER_PAGE_SLEEP_SEC)
        page_size = (SCREENER_PAGE_SIZE if target is None
                     else min(SCREENER_PAGE_SIZE, target - len(tickers)))
        raw, supports_offset = _screen_with_offset(query, page_size, offset)
        pages += 1
        if target is None:
            reported_total = raw.get("total")
            if isinstance(reported_total, int) and reported_total > 0:
                target = reported_total
        quotes = raw.get("quotes") or []
        if not quotes:
            break
        added = 0
        for q in quotes:
            sym = q.get("symbol")
            if sym and sym not in seen:
                seen.add(sym)
                tickers.append(sym)
                added += 1
        if not supports_offset or len(quotes) < page_size or added == 0:
            break
        offset += page_size
    if not tickers:
        raise RuntimeError(
            "Yahoo screener returned no results — possibly rate-limited.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UNIVERSE_FILE.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(tickers))
    tmp.replace(UNIVERSE_FILE)
    print(f"Refreshed universe: {len(tickers)} tickers in {pages} request(s), "
          f"{time.monotonic() - started:.1f}s.", file=sys.stderr)
    return tickers


def load_universe(min_market_cap: float, min_volume: int, count: int | None,
                  refresh_mode) -> list[str]:
    if not UNIVERSE_FILE.exists() or refresh_mode is True:
        return refresh_universe(min_market_cap, min_volume, count)
    cached = [t for t in UNIVERSE_FILE.read_text().splitlines() if t.strip()]
    if refresh_mode is False:
        return cached
    if count is not None and len(cached) < count:
        return refresh_universe(min_market_cap, min_volume, count)
    age_days = (datetime.now().timestamp() -
                UNIVERSE_FILE.stat().st_mtime) / 86400
    if age_days > UNIVERSE_TTL_DAYS:
        return refresh_universe(min_market_cap, min_volume, count)
    return cached


# -----------------------------------------------------------------------
# Equity bars — needed for ADV (denominator in notional/ADV signal).
# -----------------------------------------------------------------------

def fetch_equity_bars(tickers: list[str]) -> pd.DataFrame:
    print(
        f"Fetching equity bars for {len(tickers)} tickers...", file=sys.stderr)
    return yf.download(
        tickers, period=f"{EQUITY_HISTORY_DAYS}d", interval="1d",
        auto_adjust=True, progress=False, threads=True, group_by="ticker",
    )


def compute_advs(bars: pd.DataFrame, tickers: list[str],
                 lookback: int = 20) -> dict[str, float]:
    """Return ticker → 20-day average dollar volume (close × volume)."""
    advs: dict[str, float] = {}
    for t in tickers:
        try:
            if (t,) in bars.columns or t in bars.columns.levels[0]:
                close = bars[t]["Close"].dropna()
                vol = bars[t]["Volume"].dropna()
            else:
                continue
            joined = pd.concat([close, vol], axis=1,
                               join="inner").tail(lookback)
            if len(joined) < 5:
                continue
            advs[t] = float((joined["Close"] * joined["Volume"]).mean())
        except (KeyError, AttributeError):
            continue
    return advs


def compute_spot(bars: pd.DataFrame, tickers: list[str]) -> dict[str, float]:
    """Latest close per ticker."""
    spots: dict[str, float] = {}
    for t in tickers:
        try:
            close = bars[t]["Close"].dropna()
            if len(close):
                spots[t] = float(close.iloc[-1])
        except (KeyError, AttributeError):
            continue
    return spots


def compute_close_on_date(bars: pd.DataFrame, tickers: list[str],
                          target_date: date) -> dict[str, float]:
    """Close on `target_date` per ticker — used by the cross-day confirmation
    section to compute spot drift between the prior snapshot's session and
    today's. Falls back to the nearest available bar *on or before* the target
    date so a holiday-mismatch (prior_date was a half-day or NYSE adjustment)
    still resolves rather than silently dropping the ticker.

    Crucial: must look up by `target_date`, not by `iloc[-N]`, because
    `prior_date` may be 2+ days back (long weekend, user skipped a run) and a
    fixed iloc offset would show the *wrong* drift in those cases."""
    out: dict[str, float] = {}
    target_ts = pd.Timestamp(target_date).normalize()
    for t in tickers:
        try:
            close = bars[t]["Close"].dropna()
            if close.empty:
                continue
            on_or_before = close[close.index.normalize() <= target_ts]
            if not on_or_before.empty:
                out[t] = float(on_or_before.iloc[-1])
        except (KeyError, AttributeError):
            continue
    return out


# -----------------------------------------------------------------------
# Options chain fetch — per ticker, threaded.
# -----------------------------------------------------------------------

def fetch_options_for_ticker(ticker: str, num_expiries: int, max_dte: int,
                             today: date) -> list[dict] | None:
    """Return list of contract dicts (calls + puts across nearest N expiries
    within max_dte), or None if no options coverage / fetch error."""
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
    except Exception:
        return None
    if not expirations:
        return None

    contracts: list[dict] = []
    # Walk up to 2× num_expiries to skip past ones that exceed max_dte.
    for exp in expirations[:max(num_expiries * 2, num_expiries)]:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < 0 or dte > max_dte:
            continue
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue
        for df, side in ((chain.calls, "call"), (chain.puts, "put")):
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                vol = _safe_int(row.get("volume"))
                oi = _safe_int(row.get("openInterest"))
                last = _safe_float(row.get("lastPrice"))
                # Keep vol=0 contracts (they get filtered out of `flagged` later
                # via min_contract_vol, but stay in `chains` so OI confirmation
                # can join them — yesterday's flag whose vol normalized today
                # still has meaningful OI to compare).
                if vol is None or vol < 0 or last is None:
                    continue
                strike = _safe_float(row.get("strike"))
                if strike is None or strike <= 0:
                    continue
                contracts.append({
                    "ticker": ticker,
                    "expiry": exp,
                    "strike": strike,
                    "type": side,
                    "vol": vol,
                    "oi": oi if oi is not None else 0,
                    "last_price": last,
                    "iv": _safe_float(row.get("impliedVolatility")),
                    "dte": dte,
                })
        # If we have enough usable expirations, stop early.
        seen_expiries = {c["expiry"] for c in contracts}
        if len(seen_expiries) >= num_expiries:
            break
    return contracts if contracts else None


def _safe_float(x):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_int(x):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def fetch_all_chains(tickers: list[str], num_expiries: int, max_dte: int,
                     max_workers: int, today: date) -> dict[str, list[dict]]:
    """Threaded fan-out. yfinance per-ticker option_chain calls are I/O-bound
    so threading gets us a real ~10× speedup vs serial."""
    print(f"Fetching options for {len(tickers)} tickers "
          f"(num_expiries={num_expiries}, max_dte={max_dte}, "
          f"workers={max_workers})...", file=sys.stderr)
    started = time.monotonic()
    results: dict[str, list[dict]] = {}
    coverage_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_ticker = {
            pool.submit(fetch_options_for_ticker, t, num_expiries, max_dte, today): t
            for t in tickers
        }
        done = 0
        for fut in as_completed(future_to_ticker):
            t = future_to_ticker[fut]
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(tickers)} "
                      f"({coverage_count} with chains)", file=sys.stderr)
            try:
                contracts = fut.result()
            except Exception:
                contracts = None
            if contracts:
                results[t] = contracts
                coverage_count += 1
    print(f"Done in {time.monotonic() - started:.1f}s — "
          f"{coverage_count}/{len(tickers)} had options coverage.", file=sys.stderr)
    return results


# -----------------------------------------------------------------------
# Anomaly detection.
# -----------------------------------------------------------------------

def enrich_contract(c: dict, spot: float | None) -> dict:
    """Return a NEW dict with derived fields (vol_oi, notional, dist_pct) added.
    Doesn't mutate input — the original `c` is shared with `chains[ticker]` and
    we want raw chains to stay raw so downstream aggregates / OI joins can rely
    on a clean field set."""
    out = dict(c)
    vol = out["vol"]
    oi = out["oi"]
    last = out["last_price"]
    strike = out["strike"]
    out["vol_oi"] = (vol / oi) if oi and oi > 0 else None
    out["notional"] = vol * last * 100.0
    if spot and spot > 0:
        if out["type"] == "call":
            out["dist_pct"] = (strike / spot - 1.0) * 100.0
        else:
            out["dist_pct"] = (spot / strike - 1.0) * 100.0
    else:
        out["dist_pct"] = None
    return out


def filter_contracts(contracts: list[dict], spots: dict[str, float],
                     min_vol_oi: float, min_contract_vol: int,
                     min_notional: float) -> list[dict]:
    """Apply contract-level anomaly filters. Returns NEW enriched dicts (input
    contracts are not mutated)."""
    out: list[dict] = []
    for raw in contracts:
        spot = spots.get(raw["ticker"])
        c = enrich_contract(raw, spot)
        if c["vol"] < min_contract_vol:
            continue
        if c["notional"] < min_notional:
            continue
        # Vol/OI gate. OI=0 (brand-new strike) passes if vol >= 2× min_contract_vol
        # — informational floor since the ratio is undefined.
        if c["vol_oi"] is None:
            if c["vol"] < 2 * min_contract_vol:
                continue
        elif c["vol_oi"] < min_vol_oi:
            continue
        out.append(c)
    return out


def compute_ticker_aggregates(all_contracts: dict[str, list[dict]],
                              spots: dict[str, float],
                              advs: dict[str, float]) -> dict[str, dict]:
    """Per-ticker aggregates over ALL today's contracts (not just flagged
    ones) — these go into the C/P ratio, notional/ADV, and ATM IV signals."""
    aggs: dict[str, dict] = {}
    for ticker, contracts in all_contracts.items():
        call_vol = sum(c["vol"] for c in contracts if c["type"] == "call")
        put_vol = sum(c["vol"] for c in contracts if c["type"] == "put")
        total_notional = sum(c["vol"] * c["last_price"]
                             * 100.0 for c in contracts)
        cp = (call_vol / put_vol) if put_vol > 0 else (float("inf")
                                                       if call_vol > 0 else 0.0)
        adv = advs.get(ticker)
        notnl_adv = (total_notional / adv) if adv and adv > 0 else None
        # ATM IV: median IV across strikes within ±5% of spot. Useful as a
        # ticker-level "is vol elevated" hint and as a building block for the
        # eventual IV-percentile feature (needs accumulated history).
        spot = spots.get(ticker)
        atm_iv = None
        if spot and spot > 0:
            near = [c["iv"] for c in contracts
                    if c.get("iv") is not None
                    and c["iv"] > 0
                    and abs(c["strike"] / spot - 1.0) <= 0.05]
            if near:
                atm_iv = float(np.median(near))
        aggs[ticker] = {
            "cp_ratio": cp,
            "total_notional": total_notional,
            "notional_adv_mult": notnl_adv,
            "call_vol": call_vol,
            "put_vol": put_vol,
            "atm_iv": atm_iv,
        }
    return aggs


def assign_flags(contract: dict, ticker_agg: dict,
                 far_otm_pct: float, cp_ratio_extreme: float,
                 notional_adv_mult: float) -> list[str]:
    """Per-row flag list. The contract is already a Vol/OI survivor by the
    time we get here, so ⚡ is always present."""
    flags = ["⚡"]
    dist = contract["dist_pct"]
    dte = contract["dte"]
    if dist is not None and dist >= far_otm_pct and dte <= 30:
        flags.append("🎯")
    if dist is not None and dist >= far_otm_pct and dte <= 10:
        flags.append("🔥")
    cp = ticker_agg["cp_ratio"]
    if cp >= cp_ratio_extreme or (cp > 0 and cp <= 1.0 / cp_ratio_extreme):
        flags.append("📊")
    nadv = ticker_agg["notional_adv_mult"]
    if nadv is not None and nadv >= notional_adv_mult:
        flags.append("💰")
    return flags


# -----------------------------------------------------------------------
# Sector cache (optional cosmetic enrichment).
# -----------------------------------------------------------------------

def load_sectors() -> dict[str, dict]:
    if not SECTORS_FILE.exists():
        return {}
    try:
        return json.loads(SECTORS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_sectors(sectors: dict[str, dict]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECTORS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sectors, indent=2))
    tmp.replace(SECTORS_FILE)


def _fetch_sector_one(ticker: str) -> tuple[str, dict | None]:
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector") or "Other"
        industry = info.get("industry") or ""
        return ticker, {
            "sector": sector, "industry": industry,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return ticker, None


def refresh_sectors(tickers: list[str], existing: dict[str, dict],
                    max_workers: int = 8) -> dict[str, dict]:
    """Fetch sector info for tickers missing from cache or stale (> 30d)."""
    now = datetime.now(timezone.utc)
    needed = []
    for t in tickers:
        meta = existing.get(t)
        if not meta:
            needed.append(t)
            continue
        try:
            ts = datetime.fromisoformat(meta["ts"])
            if (now - ts).days > SECTORS_TTL_DAYS:
                needed.append(t)
        except (KeyError, ValueError):
            needed.append(t)
    if not needed:
        return existing
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_sector_one, t): t for t in needed}
        for fut in as_completed(futures):
            t, meta = fut.result()
            if meta:
                existing[t] = meta
    save_sectors(existing)
    return existing


def abbreviate_sector(sector: str) -> str:
    return {
        "Technology": "Tech",
        "Healthcare": "Health",
        "Financial Services": "Financ",
        "Consumer Cyclical": "Cons Cyc",
        "Consumer Defensive": "Cons Def",
        "Communication Services": "Comm",
        "Industrials": "Indust",
        "Energy": "Energy",
        "Basic Materials": "Materials",
        "Real Estate": "RE",
        "Utilities": "Util",
    }.get(sector, sector[:8])


# -----------------------------------------------------------------------
# Markdown history I/O.
# -----------------------------------------------------------------------

def history_path(d: date) -> Path:
    return HISTORY_DIR / f"{d.isoformat()}.md"


def write_snapshot(d: date, rows: list[dict], params: dict):
    """Write today's full anomaly-contract table to state/history/YYYY-MM-DD.md.
    All anomaly contracts go in (not just top-N tickers) so tomorrow has
    the full join basis for OI confirmation."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# UOA snapshot — {d.isoformat()}")
    lines.append("")
    lines.append(f"**Params**: {json.dumps(params, sort_keys=True)}")
    lines.append(f"**Rows**: {len(rows)}")
    lines.append("")
    lines.append("| " + " | ".join(SNAPSHOT_COLS) + " |")
    lines.append("|" + "|".join(["---"] * len(SNAPSHOT_COLS)) + "|")
    for r in rows:
        cells = []
        for col in SNAPSHOT_COLS:
            v = r.get(col)
            if v is None:
                cells.append("")
            elif isinstance(v, float):
                # `:g` drops trailing zeros (607.5000 → 607.5) and keeps the file
                # readable. Precision is fine — we never use snapshot floats for
                # more than join-keys (strike) and rough comparison (oi/vol_oi).
                cells.append(f"{v:g}")
            elif isinstance(v, list):
                cells.append("".join(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    tmp = history_path(d).with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(history_path(d))


_TABLE_HEADER_RE = re.compile(r"^\|\s*ticker\s*\|", re.IGNORECASE)


def parse_snapshot(d: date) -> list[dict] | None:
    """Read state/history/YYYY-MM-DD.md back to a list of contract dicts.
    Returns None if no file. Tolerant of header drift — uses positional
    mapping to SNAPSHOT_COLS."""
    p = history_path(d)
    if not p.exists():
        return None
    lines = p.read_text().splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if _TABLE_HEADER_RE.match(ln):
            header_idx = i
            break
    if header_idx is None:
        return None
    header_cells = [c.strip() for c in lines[header_idx].strip("|").split("|")]
    rows = []
    for ln in lines[header_idx + 2:]:  # skip separator
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != len(header_cells):
            continue
        row = {}
        for col, val in zip(header_cells, cells):
            if col in ("vol", "oi", "dte"):
                try:
                    row[col] = int(val) if val else None
                except ValueError:
                    row[col] = None
            elif col in ("strike", "vol_oi", "last_price", "notional", "iv",
                         "dist_pct", "ticker_cp_ratio", "ticker_total_notional",
                         "ticker_notional_adv_mult"):
                try:
                    row[col] = float(val) if val else None
                except ValueError:
                    row[col] = None
            else:
                row[col] = val
        rows.append(row)
    return rows


def contract_key(r: dict) -> tuple:
    """The join key used to link yesterday's anomaly row to today's contract."""
    return (r["ticker"], r["expiry"], float(r["strike"]), r["type"])


def list_history_dates() -> list[date]:
    if not HISTORY_DIR.exists():
        return []
    out = []
    for p in HISTORY_DIR.glob("*.md"):
        try:
            out.append(date.fromisoformat(p.stem))
        except ValueError:
            continue
    return sorted(out)


# -----------------------------------------------------------------------
# Cross-day enrichment.
# -----------------------------------------------------------------------

def compute_oi_deltas(today_by_key: dict[tuple, dict],
                      prior_snapshot: list[dict] | None) -> dict[tuple, dict]:
    """Map contract_key → {prior_oi, today_oi, delta_pct, status, prior_vol}
    for every contract that appeared in the prior snapshot AND is still
    fetchable today. `today_by_key` should be built from the FULL raw chains,
    not just today's flagged contracts — otherwise we'd lose the "vol spiked
    last run, today vol normalized but OI grew" case, which is precisely the
    highest-information confirmation pattern.

    Status tiers (drive the three rendered sections, sum to total joined):
      ✅ strong growth: OI ≥ +20% — clean "position built and held"
      ≈ partial growth: +5% to +20% — some positions kept, some closed
      ❌ flat/declined: OI < +5% — day-trade churn, position closed out
      🆕 new strike: prior OI was 0 (no ratio possible); ✅ iff today OI > 0
    """
    out: dict[tuple, dict] = {}
    if not prior_snapshot:
        return out
    for r in prior_snapshot:
        try:
            k = contract_key(r)
        except (KeyError, ValueError, TypeError):
            continue
        prior_oi = r.get("oi")
        prior_vol = r.get("vol")
        if prior_oi is None:
            continue
        today_c = today_by_key.get(k)
        today_oi = today_c["oi"] if today_c else None
        if today_oi is None:
            # Contract dropped from today's chain entirely (rare — usually means
            # the contract expired between snapshots, or yfinance briefly lost
            # coverage). Silently skip; we can't compute a delta.
            continue
        if prior_oi == 0:
            # New strike yesterday — no ratio possible, use absolute presence.
            out[k] = {
                "prior_oi": 0, "today_oi": today_oi,
                "delta_pct": None,
                "status": "✅" if today_oi > 0 else "❌",
                "prior_vol": prior_vol,
            }
            continue
        delta_pct = (today_oi - prior_oi) / prior_oi * 100.0
        if delta_pct >= 20:
            status = "✅"
        elif delta_pct < 5:
            status = "❌"
        else:
            status = "≈"
        out[k] = {
            "prior_oi": prior_oi, "today_oi": today_oi,
            "delta_pct": delta_pct, "status": status,
            "prior_vol": prior_vol,
        }
    return out


STREAK_MAX_DEPTH = 60


def compute_streaks(today_contracts: list[dict],
                    history_dates: list[date],
                    today: date) -> dict[tuple, int]:
    """Count consecutive prior snapshot dates this exact contract has been
    flagged. Walks backward through *snapshot history* (not calendar trading
    days), so a user who skips a run doesn't have all streaks reset to 0.
    Stops at the first prior snapshot where the contract is missing, OR at
    STREAK_MAX_DEPTH (beyond ~60 runs the marginal info is zero).

    Note this differs from "consecutive trading days" — e.g., if a user runs
    Mon/Wed/Fri (skipping Tue/Thu), a contract appearing all three runs gets
    streak=2 (two prior appearances), consistent with how prior_date for OI
    deltas also tolerates calendar gaps."""
    streaks: dict[tuple, int] = {contract_key(c): 0 for c in today_contracts}
    still_streaking = set(streaks.keys())
    prior_dates_desc = sorted([d for d in history_dates if d < today],
                              reverse=True)
    for depth, d in enumerate(prior_dates_desc):
        if not still_streaking or depth >= STREAK_MAX_DEPTH:
            break
        snap = parse_snapshot(d)
        if not snap:
            break
        snap_keys = set()
        for r in snap:
            try:
                snap_keys.add(contract_key(r))
            except (KeyError, ValueError, TypeError):
                continue
        appeared = still_streaking & snap_keys
        if not appeared:
            break
        for k in appeared:
            streaks[k] += 1
        still_streaking = appeared
    return streaks


# -----------------------------------------------------------------------
# Rendering.
# -----------------------------------------------------------------------

def select_top_per_ticker(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by ticker, sorted within each ticker by vol_oi desc
    (with None last)."""
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda r: (r.get("vol_oi") or 0), reverse=True)
    return by_ticker


def ticker_score(rows: list[dict], ticker_agg: dict) -> float:
    """Heuristic ticker-level priority score for top-N selection. Heavier
    weight on # flags fired, total flagged notional, and presence of the
    catalyst-imminent (🎯🔥) cluster. Tunable later; keeps top-N stable."""
    flagged_notional = sum(r.get("notional", 0) for r in rows)
    has_catalyst = any("🎯" in r.get("flags", []) for r in rows)
    has_squeeze = any("🔥" in r.get("flags", []) for r in rows)
    notnl_adv = ticker_agg.get("notional_adv_mult") or 0
    cp = ticker_agg.get("cp_ratio") or 1
    skew = max(cp, 1 / cp if cp > 0 else 1)
    score = (
        np.log1p(flagged_notional / 1e5) * 10
        + (15 if has_catalyst else 0)
        + (20 if has_squeeze else 0)
        + min(notnl_adv, 5) * 5
        + min(np.log1p(skew - 1), 2) * 5
    )
    return float(score)


def fmt_money(x: float) -> str:
    if x >= 1e6:
        return f"${x/1e6:.2f}M"
    if x >= 1e3:
        return f"${x/1e3:.0f}k"
    return f"${x:.0f}"


def fmt_oi_delta(d: dict | None) -> str:
    if not d:
        return "🆕"
    pct = d.get("delta_pct")
    status = d.get("status", "")
    if pct is None:
        return f"OI=new {status}"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}% {status}"


def render_table(top_tickers: list[tuple[str, list[dict], dict]],
                 sectors: dict[str, dict],
                 oi_deltas: dict[tuple, dict],
                 streaks: dict[tuple, int]) -> str:
    """Main top-N table — one row per ticker (its top contract), with
    `+N more` annotation when ticker has additional flagged contracts."""
    # TopContract already encodes side via the C/P letter (e.g. $40C, $11P),
    # so no separate Side column.
    header = ("| # | Ticker | Sector | TopContract | Vol | OI | Vol/OI "
              "| Notional | DTE | %OTM | Flags | CP% | Notnl/ADV "
              "| OIΔ | Streak |")
    sep = "|" + "|".join(["---"] * 15) + "|"
    out = [header, sep]
    for i, (ticker, rows, agg) in enumerate(top_tickers, 1):
        top = rows[0]
        extra = len(rows) - 1
        sector_meta = sectors.get(ticker, {})
        sector = abbreviate_sector(sector_meta.get("sector", "—"))
        strike_str = f"{int(top['strike'])}" if top["strike"] == int(
            top["strike"]) else f"{top['strike']:g}"
        type_letter = "C" if top["type"] == "call" else "P"
        expiry_short = top["expiry"][5:]  # "MM-DD"
        contract_str = f"${strike_str}{type_letter} {expiry_short}"
        vol_oi = top.get("vol_oi")
        vol_oi_str = f"{vol_oi:.1f}" if vol_oi is not None else "new"
        dist = top.get("dist_pct")
        dist_str = f"{dist:+.0f}%" if dist is not None else "—"
        cp = agg.get("cp_ratio", 0)
        cp_str = f"{cp:.1f}" if cp != float("inf") else "∞"
        nadv = agg.get("notional_adv_mult")
        nadv_str = f"{nadv:.2f}×" if nadv is not None else "—"
        flags_str = "".join(top.get("flags", []))
        oi_delta = oi_deltas.get(contract_key(top))
        oi_str = fmt_oi_delta(oi_delta)
        streak = streaks.get(contract_key(top), 0)
        streak_str = str(streak + 1) if streak > 0 else "1"
        ticker_disp = f"**{ticker}**"
        if extra:
            ticker_disp += f" _(+{extra} more)_"
        out.append(
            f"| {i} | {ticker_disp} | {sector} | {contract_str} "
            f"| {top['vol']:,} | {top['oi']:,} | {vol_oi_str} "
            f"| {fmt_money(top['notional'])} | {top['dte']} | {dist_str} "
            f"| {flags_str} | {cp_str} | {nadv_str} "
            f"| {oi_str} | {streak_str} |"
        )
    return "\n".join(out)


def _format_contract_short(r: dict) -> str:
    strike = r["strike"]
    strike_str = f"{int(strike)}" if strike == int(strike) else f"{strike:g}"
    type_letter = "C" if r["type"] == "call" else "P"
    return f"${strike_str}{type_letter} {r['expiry'][5:]}"


def _format_oi_change(d: dict) -> str:
    delta_str = f"{d['prior_oi']:,} → {d['today_oi']:,}"
    if d.get("delta_pct") is not None:
        delta_str += f" ({d['delta_pct']:+.0f}%)"
    return delta_str


def _format_spot_drift(spot: float | None, prior_spot: float | None) -> str:
    if spot is None or prior_spot is None or prior_spot == 0:
        return "—"
    pct = (spot / prior_spot - 1.0) * 100.0
    return f"${prior_spot:,.2f} → ${spot:,.2f} ({pct:+.1f}%)"


def _format_prior_date_label(prior_date: date, today: date) -> str:
    days = (today - prior_date).days
    if days == 1:
        return f"{prior_date.isoformat()} (1 day ago)"
    return f"{prior_date.isoformat()} ({days} days ago)"


def render_confirmed_section(prior_snapshot: list[dict] | None,
                             oi_deltas: dict[tuple, dict],
                             spots: dict[str, float],
                             prior_spots: dict[str, float],
                             prior_date: date | None,
                             today: date) -> str:
    """Three sections: ✅ Confirmed (≥+20%), ≈ Partial (5-20%), ❌ Closed out
    (<+5%). Counts add up to total joined so the math is auditable. Each table
    shows the spot drift from prior_date to today so the user can see whether
    the OI move was already 'priced in'."""
    if not prior_snapshot or not prior_date:
        return ""
    confirmed, partial, closed = [], [], []
    for r in prior_snapshot:
        try:
            k = contract_key(r)
        except (KeyError, ValueError, TypeError):
            continue
        d = oi_deltas.get(k)
        if not d:
            continue
        bucket = {"✅": confirmed, "≈": partial, "❌": closed}.get(d["status"])
        if bucket is not None:
            bucket.append((r, d))
    if not (confirmed or partial or closed):
        return ""
    total = len(confirmed) + len(partial) + len(closed)
    label = _format_prior_date_label(prior_date, today)
    lines = [f"## Cross-day OI confirmation — vs {label}"]
    lines.append("")
    lines.append(f"_{total} of prior flags re-joined: "
                 f"{len(confirmed)} ✅ strong growth · "
                 f"{len(partial)} ≈ partial · "
                 f"{len(closed)} ❌ closed-out_")
    lines.append("")

    def _emit_table(title: str, rows: list[tuple[dict, dict]], cap: int):
        if not rows:
            return
        lines.append(f"### {title} ({len(rows)})")
        lines.append("")
        lines.append(
            "| Ticker | Contract | Prior Vol | Prior→Today OI | Spot drift |")
        lines.append("|---|---|---|---|---|")
        for r, d in rows[:cap]:
            spot_str = _format_spot_drift(spots.get(r["ticker"]),
                                          prior_spots.get(r["ticker"]))
            lines.append(f"| **{r['ticker']}** | {_format_contract_short(r)} "
                         f"| {(d.get('prior_vol') or 0):,} | {_format_oi_change(d)} "
                         f"| {spot_str} |")
        if len(rows) > cap:
            lines.append(f"| _...{len(rows) - cap} more_ | | | | |")
        lines.append("")

    _emit_table("✅ Strong growth (OI ≥ +20%) — position built and held",
                confirmed, cap=20)
    _emit_table("≈ Partial growth (OI +5% to +20%) — mixed; some retained, some closed",
                partial, cap=10)
    _emit_table("❌ Closed out (OI < +5%) — day-trade churn, not accumulation",
                closed, cap=10)
    return "\n".join(lines)


def render_repeat_offenders(today_contracts: list[dict],
                            streaks: dict[tuple, int],
                            min_streak: int = 2) -> str:
    """Contracts whose streak (= prior consecutive appearances) ≥ min_streak.
    Default min_streak=2 means "appeared in at least 2 prior runs", i.e. today
    is the 3rd+ consecutive day flagged. Section header reflects this."""
    repeats = [(c, streaks[contract_key(c)]) for c in today_contracts
               if streaks[contract_key(c)] >= min_streak]
    if not repeats:
        return ""
    repeats.sort(key=lambda x: x[1], reverse=True)
    lines = ["## Repeat offenders (3+ days flagged)"]
    for c, s in repeats[:15]:
        strike = c["strike"]
        strike_str = f"{int(strike)}" if strike == int(
            strike) else f"{strike:g}"
        type_letter = "C" if c["type"] == "call" else "P"
        contract = f"${strike_str}{type_letter} {c['expiry'][5:]}"
        vol_oi = c.get("vol_oi")
        vol_oi_str = f"{vol_oi:.1f}" if vol_oi is not None else "new"
        lines.append(f"- **{c['ticker']} {contract}** — "
                     f"flagged {s + 1} days running. "
                     f"Today: vol {c['vol']:,}, OI {c['oi']:,}, "
                     f"Vol/OI {vol_oi_str}.")
    lines.append("")
    return "\n".join(lines)


# -----------------------------------------------------------------------
# main().
# -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Unusual options activity scan (daily, EOD).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--min-vol-oi", type=float, default=3.0)
    ap.add_argument("--min-contract-vol", type=int, default=500)
    ap.add_argument("--min-contract-notional", type=float, default=50_000)
    ap.add_argument("--num-expiries", type=int, default=2)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--far-otm-pct", type=float, default=10.0)
    ap.add_argument("--cp-ratio-extreme", type=float, default=3.0)
    ap.add_argument("--notional-adv-mult", type=float, default=0.5)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--min-market-cap", type=float, default=5e9)
    ap.add_argument("--min-volume", type=int, default=1_000_000)
    ap.add_argument("--universe-count", type=int, default=None,
                    help="Cap universe size. Default: pull all matches.")
    ap.add_argument("--refresh-universe", dest="refresh_universe",
                    action="store_true", default=None)
    ap.add_argument("--no-refresh-universe", dest="refresh_universe",
                    action="store_false")
    ap.add_argument("--show-history", action="store_true")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--allow-same-day", action="store_true")
    ap.add_argument("--no-sectors", action="store_true")
    ap.add_argument("--format", default="markdown",
                    choices=("markdown", "json"))
    ap.add_argument("--max-workers", type=int, default=16)
    args = ap.parse_args()

    if args.clear_history:
        if HISTORY_DIR.exists():
            for p in HISTORY_DIR.glob("*.md"):
                p.unlink()
            print(f"Cleared {HISTORY_DIR}.", file=sys.stderr)
        return

    if args.show_history:
        show_history_summary()
        return

    today = datetime.now(MARKET_TZ).date()

    universe = load_universe(args.min_market_cap, args.min_volume,
                             args.universe_count, args.refresh_universe)
    if args.universe_count:
        universe = universe[:args.universe_count]

    # Equity bars first — gives us ADV + today's spot. Prior-date spot is
    # computed lazily after we know prior_date (it depends on the snapshot
    # history, not just today's bars).
    bars = fetch_equity_bars(universe)
    spots = compute_spot(bars, universe)
    advs = compute_advs(bars, universe)

    # Options chains for tickers that have both spot and ADV.
    chain_universe = [t for t in universe if t in spots and t in advs]
    chains = fetch_all_chains(chain_universe, args.num_expiries,
                              args.max_dte, args.max_workers, today)

    # Flatten + enrich + filter at the contract level.
    flat_contracts: list[dict] = []
    for ticker, contracts in chains.items():
        flat_contracts.extend(contracts)
    print(f"Funnel: {len(universe)} universe → {len(chains)} with options "
          f"coverage → {len(flat_contracts)} contracts fetched", file=sys.stderr)

    flagged = filter_contracts(flat_contracts, spots,
                               args.min_vol_oi, args.min_contract_vol,
                               args.min_contract_notional)
    print(f"  → {len(flagged)} contracts passed Vol/OI + vol + notional gates",
          file=sys.stderr)

    # Ticker aggregates from ALL contracts (not just flagged).
    ticker_aggs = compute_ticker_aggregates(chains, spots, advs)

    # Assign flags per row.
    for c in flagged:
        c["flags"] = assign_flags(c, ticker_aggs[c["ticker"]],
                                  args.far_otm_pct, args.cp_ratio_extreme,
                                  args.notional_adv_mult)
        agg = ticker_aggs[c["ticker"]]
        c["ticker_cp_ratio"] = agg["cp_ratio"]
        c["ticker_total_notional"] = agg["total_notional"]
        c["ticker_notional_adv_mult"] = agg["notional_adv_mult"]

    # Yesterday's snapshot (if any) for OI delta + streaks. `prior_date` is the
    # actual ET date of the most recent prior snapshot — may be 2+ days back if
    # a session was skipped. The cross-day section label reflects this so users
    # don't read "yesterday" when they're really looking at last Friday's data.
    history_dates = list_history_dates()
    prior_dates = [d for d in history_dates if d < today]
    prior_date = prior_dates[-1] if prior_dates else None
    prior_snapshot = parse_snapshot(prior_date) if prior_date else None
    # Per-ticker close on prior_date specifically — NOT iloc[-2]. If prior_date
    # is 3 days back, we want the 3-day spot drift, not yesterday's 1-day move.
    prior_spots = (compute_close_on_date(bars, universe, prior_date)
                   if prior_date else {})
    # today_by_key is built from ALL raw chains (not just flagged) so a contract
    # that was flagged yesterday but has normal vol today can still be joined
    # for OI confirmation — that's the highest-information case.
    today_by_key = {contract_key(c): c
                    for ticker_chains in chains.values()
                    for c in ticker_chains}
    oi_deltas = compute_oi_deltas(today_by_key, prior_snapshot)
    streaks = compute_streaks(flagged, history_dates, today)

    # Sectors (cosmetic).
    by_ticker = select_top_per_ticker(flagged)
    sectors = {}
    if not args.no_sectors and by_ticker:
        sectors = load_sectors()
        sectors = refresh_sectors(list(by_ticker.keys()), sectors)

    # Rank tickers → top-N.
    ticker_priority = []
    for ticker, rows in by_ticker.items():
        score = ticker_score(rows, ticker_aggs[ticker])
        ticker_priority.append((ticker, rows, ticker_aggs[ticker], score))
    ticker_priority.sort(key=lambda x: x[3], reverse=True)
    top_tickers = [(t, rs, agg)
                   for t, rs, agg, _ in ticker_priority[:args.top_n]]

    # Save snapshot BEFORE rendering (so cross-day signals reflect persisted state).
    # Skip non-trading days unless --allow-same-day overrides; consistent with sister
    # skills. Same-day re-runs always overwrite — there's only one canonical snapshot
    # per ET date.
    if not args.no_save and (is_nyse_trading_day(today) or args.allow_same_day):
        write_snapshot(today, flagged, {
            "min_vol_oi": args.min_vol_oi,
            "min_contract_vol": args.min_contract_vol,
            "min_contract_notional": args.min_contract_notional,
            "num_expiries": args.num_expiries,
            "max_dte": args.max_dte,
        })
        print(f"Snapshot saved: {history_path(today)}", file=sys.stderr)

    # Output.
    if args.format == "json":
        payload = {
            "run_date": today.isoformat(),
            "prior_date": prior_date.isoformat() if prior_date else None,
            "params": vars(args),
            "universe_size": len(universe),
            "with_options": len(chains),
            "flagged_contracts": len(flagged),
            "top_tickers": [
                {
                    "ticker": t,
                    "sector": sectors.get(t, {}).get("sector"),
                    "spot": spots.get(t),
                    "prior_spot": prior_spots.get(t),
                    # ATM IV exposed at the top so consumers don't need to
                    # dig into ticker_agg — it's the most-asked-for derived
                    # ticker stat and the seed for future IV-percentile work.
                    "atm_iv": agg.get("atm_iv"),
                    "ticker_agg": agg,
                    "contracts": rs,
                    "oi_delta_top": oi_deltas.get(contract_key(rs[0])),
                    "streak": streaks.get(contract_key(rs[0]), 0),
                }
                for t, rs, agg in top_tickers
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    # Markdown.
    now_utc = datetime.now(timezone.utc)
    lines = []
    lines.append(
        f"# Unusual options activity — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"**Params**: min_vol_oi={args.min_vol_oi}, "
                 f"min_contract_vol={args.min_contract_vol}, "
                 f"min_notional={fmt_money(args.min_contract_notional)}, "
                 f"num_expiries={args.num_expiries}, max_dte={args.max_dte}")
    lines.append(f"**Universe**: {len(universe)} tickers "
                 f"· **With options**: {len(chains)} "
                 f"· **Flagged tickers**: {len(by_ticker)} ({len(top_tickers)} displayed) "
                 f"· **Prior runs**: {len(prior_dates)}")
    spy_close = spots.get("SPY")
    if spy_close:
        lines.append(f"**Regime**: SPY {spy_close:.1f} "
                     f"(informational only — UOA signal not regime-gated)")
    lines.append("")
    lines.append(f"## Top {len(top_tickers)}")
    lines.append("")
    lines.append(render_table(top_tickers, sectors, oi_deltas, streaks))
    lines.append("")

    confirmed = render_confirmed_section(prior_snapshot, oi_deltas,
                                         spots, prior_spots,
                                         prior_date, today)
    if confirmed:
        lines.append(confirmed)

    repeats = render_repeat_offenders(flagged, streaks, min_streak=2)
    if repeats:
        lines.append(repeats)

    print("\n".join(lines))


def show_history_summary():
    """Quick stats — # snapshots, date range, recent flag counts."""
    dates = list_history_dates()
    if not dates:
        print("No history yet.")
        return
    print(f"# UOA history summary")
    print(
        f"\nSnapshots: {len(dates)}  ({dates[0].isoformat()} → {dates[-1].isoformat()})\n")
    print("| Date | Flagged contracts | Top tickers |")
    print("|---|---|---|")
    for d in dates[-14:]:
        snap = parse_snapshot(d)
        if not snap:
            continue
        tickers = sorted(set(r["ticker"] for r in snap if r.get("ticker")))
        print(f"| {d.isoformat()} | {len(snap)} | "
              f"{', '.join(tickers[:8])}{' ...' if len(tickers) > 8 else ''} |")


if __name__ == "__main__":
    main()
