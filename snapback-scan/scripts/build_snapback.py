"""snapback-scan: powder-keg × spark-calendar join — the deterministic half.

The concept (born 2026-07-30): the violent post-capitulation snapback is not
predictable by DAY, but it is identifiable by SETUP. The night before the semis
epicenter ripped +15-23%, mean-reversion-scan's top-3 were SNDK/MU/AMD (scores
79/79/78, MU 60-day-first listing) — the bell rang; what was missing was the
join against the one thing that gives the bounce a DATE: the scheduled catalyst
calendar (MSFT's capex verdict was on it for weeks). This script builds that
join:

  POWDER KEGS  = mean-reversion-scan's latest run, filtered by the backtested
                 high-conviction profile (Score >= 40, freshly listed <= 2 runs)
  SPARKS       = scheduled narrative-flipping events within the next N trading
                 days: the keg's OWN earnings, same-sector megacap "verdict
                 prints", and high-impact US macro events (FOMC/CPI/NFP/GDP)
  ARMED KEG    = keg with >= 1 spark — a candidate with a date attached

Reuses, never recomputes (sister-scan caches):
  mean-reversion-scan/state/history.csv   the keg list + score/stop/target
  mean-reversion-scan/state/sectors.json  ticker -> sector
  regime-scan/state/history.csv           regime state (sizing gate) + VIX

Backtest-informed rules encoded here (see SKILL.md for the receipts):
  - Score >= 40 AND listing age <= 2 runs  -> the 3x-baseline profile
  - deep RSI(2) gets NO extra weight       -> the backtest found it inverse
  - quiet-tape signals carry a warning     -> panic-day signals are the edge;
    quiet-day oversold is knife-catching
  - already-ignited (> chase threshold since signal) -> chase-guard flag

Everything degrades cleanly: one dead source never sinks the packet; the
`errors` list records what failed so the briefing layer can say so honestly.

Usage:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' python build_snapback.py
  ... --window-days 3     # spark window (trading days, default 3)
  ... --min-score 40      # keg gate (backtest default 40)
  ... --max-age 2         # max listing age in runs (backtest default 2)
  ... --format table      # human table instead of JSON
  ... --no-save           # don't write state/runs/<date>.json
"""
import argparse
import json
import sys
import urllib.request
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd
import yfinance as yf
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
    USThanksgivingDay, nearest_workday,
)


class _NYSECalendar(AbstractHolidayCalendar):
    """Mirrors regime-scan / premarket-brief — a weekday-only window would
    count holidays as spark days and mislabel a holiday-eve run's AMC gate."""
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday,
                start_date="2022-06-19"),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_ROOT = SKILL_DIR.parent
STATE_DIR = SKILL_DIR / "state"
RUNS_DIR = STATE_DIR / "runs"
MR_HISTORY = SKILLS_ROOT / "mean-reversion-scan" / "state" / "history.csv"
MR_SECTORS = SKILLS_ROOT / "mean-reversion-scan" / "state" / "sectors.json"
REGIME_HISTORY = SKILLS_ROOT / "regime-scan" / "state" / "history.csv"
MARKET_TZ = ZoneInfo("America/New_York")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
# ForexFactory publishes THIS week only (the nextweek variant 404s) — when the
# spark window crosses into next week, macro coverage is partial and the packet
# says so via `macro_note` instead of erroring.
FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Macro events that can flip a whole-market narrative. Matched as substrings
# against ForexFactory High-impact US titles — deliberately short list; a spark
# must be a VERDICT (rate path / inflation / growth / jobs), not every release.
MACRO_VERDICTS = ("FOMC", "Federal Funds Rate", "CPI", "PPI", "Non-Farm",
                  "Unemployment Rate", "GDP", "PCE", "Jackson Hole",
                  "Retail Sales", "ISM")

# Static SEED for reporter-sector lookup (yfinance sector labels). A crashed
# sector's snapback usually needs someone ELSE to flip the narrative —
# 2026-07-30's semis rip was owned by MSFT's capex print, not by any chip
# name's own report. Sector resolution for reporters runs four tiers deep
# (see reporter_sectors): the MR sectors.json cache is lazily built from
# names that GOT oversold, so steady megacaps like MSFT/META/AMZN are
# systematically absent from it — this seed covers exactly those, and any
# name missing from every tier gets one yfinance lookup, persisted forever.
FALLBACK_SECTORS = {
    "MSFT": "Technology", "AVGO": "Technology", "ORCL": "Technology",
    "CRM": "Technology", "ADBE": "Technology", "IBM": "Technology",
    "NOW": "Technology", "PLTR": "Technology", "ASML": "Technology",
    "GOOGL": "Communication Services", "GOOG": "Communication Services",
    "META": "Communication Services", "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "AMZN": "Consumer Cyclical", "TSLA": "Consumer Cyclical",
    "HD": "Consumer Cyclical", "MCD": "Consumer Cyclical",
    "BRK-B": "Financial Services", "BRK.B": "Financial Services",
    "JPM": "Financial Services", "V": "Financial Services",
    "MA": "Financial Services", "BAC": "Financial Services",
    "WFC": "Financial Services", "GS": "Financial Services",
    "MS": "Financial Services",
    "LLY": "Healthcare", "UNH": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "NVO": "Healthcare",
    "TMO": "Healthcare",
    "WMT": "Consumer Defensive", "PG": "Consumer Defensive",
    "KO": "Consumer Defensive", "COST": "Consumer Defensive",
    "PEP": "Consumer Defensive",
    "XOM": "Energy", "CVX": "Energy",
    "GE": "Industrials", "CAT": "Industrials", "RTX": "Industrials",
    "BA": "Industrials", "HON": "Industrials",
}
VERDICT_MKTCAP_FLOOR = 1.5e11      # calendar-confirmed size floor
MARKETWIDE_MKTCAP = 8e11           # this big, the print is a verdict for everyone
SECTOR_CACHE = STATE_DIR / "sectors_cache.json"


def reporter_sectors(symbols: list[str], mr_sectors: dict, errors: list) -> dict:
    """Resolve a sector for each big reporter, cheapest source first:
    1. mean-reversion's sectors.json (free, but lazily built — only names
       that got oversold are in it, so steady megacaps are absent)
    2. this skill's own persistent cache
    3. the static FALLBACK_SECTORS seed
    4. one yfinance lookup for anything still unknown, persisted to the
       cache — so a newly-emerged megacap costs one call, once, ever."""
    try:
        cache = json.loads(SECTOR_CACHE.read_text())
    except Exception:
        cache = {}
    out, dirty = {}, False
    for s in symbols:
        sec = mr_sectors.get(s) or cache.get(s) or FALLBACK_SECTORS.get(s)
        if not sec:
            try:
                sec = yf.Ticker(s).info.get("sector")
            except Exception as e:
                errors.append(f"reporter_sector/{s}: {e}")
                sec = None
            if sec:
                cache[s], dirty = sec, True
        if sec:
            out[s] = sec
    if dirty:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SECTOR_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))
        except Exception:
            pass
    return out

CHASE_THRESHOLD_PCT = 7.0          # since-signal move that flips the chase-guard
PANIC_RET5D_PCT = -4.0             # 5d return above this = quiet tape, warn
QUIET_VIX = 17.0                   # regime VIX below this = quiet tape, warn


def http_json(url: str, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def next_trading_days(start: date, n: int) -> list[date]:
    """The next n NYSE trading days strictly AFTER start."""
    out, d = [], start
    while len(out) < n:
        d = d + timedelta(days=1)
        if is_trading_day(d):
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Powder kegs — mean-reversion-scan's latest run, backtest-profile filtered
# --------------------------------------------------------------------------- #
def load_kegs(min_score: float, max_age: int, top_n: int, errors: list,
              today: date) -> tuple[list[dict], dict]:
    if not MR_HISTORY.exists():
        errors.append(f"kegs: {MR_HISTORY} not found (run /mean-reversion-scan)")
        return [], {}
    df = pd.read_csv(MR_HISTORY)
    if df.empty:
        errors.append("kegs: mean-reversion history is empty")
        return [], {}
    runs = sorted(df["run_id"].astype(str).unique())
    latest = runs[-1]
    rows = df[df["run_id"].astype(str) == latest]
    # Listing age = consecutive runs (ending at the latest) the ticker appears
    # in. The backtest's edge lives in FRESH listings (<= 2); a name camped on
    # the oversold list for a week is a downtrend, not a panic.
    appear = {r: set(df[df["run_id"].astype(str) == r]["ticker"]) for r in runs[-6:]}
    def listing_age(tk: str) -> int:
        age = 0
        for r in reversed(runs[-6:]):
            if tk in appear[r]:
                age += 1
            else:
                break
        return age
    kegs = []
    for _, r in rows.iterrows():
        score = float(r["score"])
        if score < min_score:
            continue
        age = listing_age(r["ticker"])
        if age > max_age:
            continue
        kegs.append({
            "ticker": r["ticker"],
            "rank": int(r["rank"]),
            "score": score,
            "rsi2": float(r["rsi2"]),
            "dist_200dma_pct": float(r["dist_200dma_pct"]),
            "signal_close": float(r["last_close"]),
            "mr_target": float(r["target_price"]),
            "mr_stop": float(r["stop_price"]),
            "signal": r.get("signal", ""),
            "listing_age_runs": age,
            "freq_60d": int(r["freq_60d"]),
        })
    kegs.sort(key=lambda k: k["score"], reverse=True)
    # Staleness against the MARKET-tz date (passed in), not the machine's
    # local date — a Beijing machine is a day ahead of ET every evening.
    mr_date = datetime.strptime(latest, "%Y%m%d").date()
    meta = {"run_id": latest, "run_date": mr_date.isoformat(),
            "stale_days": (today - mr_date).days,
            "rows_in_run": len(rows), "kegs_after_filter": len(kegs)}
    prior = {"run_id": runs[-2], "tickers": sorted(appear.get(runs[-2], set()))} \
        if len(runs) > 1 else None
    meta["prior_run"] = prior
    return kegs[:top_n], meta


def sector_map(errors: list) -> dict:
    try:
        d = json.loads(MR_SECTORS.read_text())
        return {tk: v.get("sector") for tk, v in d.items()}
    except Exception as e:
        # Say so loudly: with an empty map every keg has sector=None, the
        # sector-verdict join silently goes dark, and `armed` produces false
        # negatives across the board — the same silent-degrade failure mode
        # the premarket prev_close pollution taught us to flag, not swallow.
        errors.append(f"sectors: {MR_SECTORS} unreadable ({e}) — "
                      f"sector-verdict join disabled this run")
        return {}


# --------------------------------------------------------------------------- #
# Sparks — the scheduled catalysts that can give the bounce a date
# --------------------------------------------------------------------------- #
def earnings_sparks(days: list[date], errors: list) -> list[dict]:
    out = []
    for d in days:
        try:
            data = http_json(NASDAQ_EARNINGS.format(d=d.isoformat()),
                             headers={"Accept": "application/json, text/plain, */*"})
            rows = (data.get("data") or {}).get("rows") or []
        except Exception as e:
            errors.append(f"sparks/earnings {d}: {e}")
            continue
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            try:
                cap = float((r.get("marketCap") or "").replace("$", "").replace(",", ""))
            except Exception:
                cap = 0.0
            t = r.get("time", "")
            slot = "BMO" if "pre-market" in t else ("AMC" if "after-hours" in t else "?")
            out.append({"symbol": sym, "date": d.isoformat(), "slot": slot,
                        "mktcap": cap, "eps_forecast": r.get("epsForecast", "")})
    return out


def macro_sparks(window: list[date], errors: list) -> list[dict]:
    out, dates = [], {d.isoformat() for d in window}
    try:
        data = http_json(FF_THIS_WEEK)
    except Exception as e:
        errors.append(f"sparks/forexfactory: {e}")
        data = []
    for ev in data:
        try:
            if ev.get("country") != "USD" or ev.get("impact") != "High":
                continue
            dt = datetime.fromisoformat(ev["date"]).astimezone(MARKET_TZ)
            title = ev.get("title", "")
            if dt.date().isoformat() not in dates:
                continue
            if not any(k.lower() in title.lower() for k in MACRO_VERDICTS):
                continue
            out.append({"date": dt.date().isoformat(),
                        "time_et": dt.strftime("%H:%M"), "title": title})
        except Exception:
            continue
    seen, dedup = set(), []
    for ev in out:
        key = (ev["date"], ev["title"])
        if key not in seen:
            seen.add(key)
            dedup.append(ev)
    return sorted(dedup, key=lambda e: (e["date"], e["time_et"]))


def join_sparks(kegs: list[dict], by_symbol: dict, verdicts: list[dict],
                macro: list[dict]) -> None:
    """Attach sparks to each keg and decide `armed`. Pure logic, no I/O.

    A marketwide megacap print moves the tape, but it cannot flip a crashed
    name's OWN narrative — arming requires a spark scoped to the keg (its own
    print, its sector's verdict, or a macro verdict). Marketwide sparks stay
    listed as context so the brief can time around them."""
    for k in kegs:
        sparks = []
        own = by_symbol.get(k["ticker"])
        if own:
            sparks.append({"type": "own_earnings", "symbol": k["ticker"],
                           "date": own["date"], "slot": own["slot"],
                           "detail": f"{k['ticker']} reports {own['date']} "
                                     f"{own['slot']} (eps est {own['eps_forecast'] or '?'}) "
                                     f"— a coin flip, NOT a verdict: earnings-gap "
                                     f"rule says zero directional trust"})
        for v in verdicts:
            if v["symbol"] == k["ticker"]:
                continue
            v_sector = v.get("sector")
            if v_sector and v_sector == k.get("sector"):
                sparks.append({"type": "sector_verdict", "symbol": v["symbol"],
                               "date": v["date"], "slot": v["slot"],
                               "detail": f"{v['symbol']} ({v_sector}, "
                                         f"${v['mktcap']/1e9:.0f}B) reports "
                                         f"{v['date']} {v['slot']}"})
            elif v["mktcap"] >= MARKETWIDE_MKTCAP:
                sparks.append({"type": "marketwide_verdict", "symbol": v["symbol"],
                               "date": v["date"], "slot": v["slot"],
                               "detail": f"{v['symbol']} (${v['mktcap']/1e9:.0f}B) "
                                         f"reports {v['date']} {v['slot']}"})
        for m in macro:
            sparks.append({"type": "macro", "symbol": None, "date": m["date"],
                           "detail": f"{m['title']} {m['date']} {m['time_et']} ET"})
        k["sparks"] = sorted(sparks, key=lambda s: s["date"])
        k["armed"] = any(s["type"] in ("own_earnings", "sector_verdict", "macro")
                         for s in sparks)


def regime_state(errors: list) -> dict | None:
    try:
        df = pd.read_csv(REGIME_HISTORY)
        r = df.iloc[-1].to_dict()
        return {"snapshot": str(r.get("run_id")), "state": r.get("state"),
                "score": r.get("score"), "vix": r.get("vix"),
                "spy_vs_200_pct": r.get("spy_vs_200_pct"),
                "flags": r.get("flags") if isinstance(r.get("flags"), str) else ""}
    except Exception as e:
        errors.append(f"regime: {e}")
        return None


# --------------------------------------------------------------------------- #
# Tape check — forced-seller signature + chase-guard (one batched download)
# --------------------------------------------------------------------------- #
def tape_check(kegs: list[dict], signal_date: date | None, errors: list) -> None:
    """Two distinct reads per keg, split at the SIGNAL day:

    - The SETUP metrics (down streak, 5d return, down-gaps, volume ratio,
      signal-day low) are computed on bars up to and including the signal day —
      they describe the panic that scored the keg, and must not be diluted by
      whatever happened after.
    - The IGNITION metrics (latest close, since-signal %) are computed on the
      full series — they answer "did this keg already blow?", which drives the
      chase-guard: the morning after ignition is historically the WORST entry
      of the cycle (AMD 07-27, TSM 07-17); post-ignition the play is the
      retest, never the chase."""
    tickers = sorted({k["ticker"] for k in kegs})
    if not tickers:
        return
    try:
        df = yf.download(tickers, period="1mo", interval="1d", auto_adjust=False,
                         progress=False, threads=True, group_by="ticker")
    except Exception as e:
        errors.append(f"tape: batch download failed: {e}")
        return
    for k in kegs:
        try:
            sub = df[k["ticker"]] if len(tickers) > 1 else df
            bars = sub.dropna(subset=["Close"])
            last = float(bars["Close"].iloc[-1])
            k["latest_close"] = round(last, 2)
            k["since_signal_pct"] = round((last / k["signal_close"] - 1) * 100, 2)
            k["ignited"] = k["since_signal_pct"] >= CHASE_THRESHOLD_PCT

            setup = bars[bars.index.date <= signal_date] if signal_date else bars
            if setup.empty:
                setup = bars
            closes = setup["Close"]
            streak = 0
            for v in reversed(list(closes.pct_change().dropna())):
                if v < 0:
                    streak += 1
                else:
                    break
            k["down_streak"] = streak
            k["ret_5d_pct"] = round(float(closes.iloc[-1] / closes.iloc[-6] - 1) * 100, 2) \
                if len(closes) > 6 else None
            lows, opens = setup["Low"], setup["Open"]
            k["down_gaps_5d"] = int(sum(
                1 for i in range(max(1, len(setup) - 5), len(setup))
                if float(opens.iloc[i]) < float(lows.iloc[i - 1])))
            vols = setup["Volume"].astype(float)
            k["vol_ratio_5d_20d"] = round(float(vols.tail(5).mean() /
                                                vols.tail(20).mean()), 2) \
                if len(vols) >= 20 and vols.tail(20).mean() else None
            k["signal_day_low"] = round(float(lows.iloc[-1]), 2)
            # Quiet-tape warning: the backtest found quiet-day signals are knife
            # catches; the edge needs panic (a violent 5d decline into signal).
            k["quiet_warning"] = (k["ret_5d_pct"] is None
                                  or k["ret_5d_pct"] > PANIC_RET5D_PCT)
        except Exception as e:
            errors.append(f"tape/{k['ticker']}: {e}")


def prior_review(prior_mr: dict | None, errors: list, today: date) -> dict | None:
    """Grade what got flagged last time — full sample, both tails.

    Two choices born of the first live round:
    - Grade THIS skill's previous packet when one exists (state/runs/): the
      MR run is the keg SOURCE, not this skill's call — grading the whole MR
      list credits calls the skill never made. Fallback to the prior MR run
      only when no own history exists yet.
    - Report full-sample stats plus best AND worst tails. The first
      implementation returned the top-15 by return — a winners-side sample
      the synthesis layer duly read as "15/15 up, avg +10%" — exactly the
      self-flattering horoscope the grading loop exists to prevent."""
    base, src = [], None
    try:
        prior_packets = [p for p in sorted(RUNS_DIR.glob("*.json"))
                         if p.stem < today.isoformat()]
        if prior_packets:
            pk = json.loads(prior_packets[-1].read_text())
            src = f"snapback:{pk.get('today')}"
            for k in pk.get("kegs", []):
                bp = k.get("latest_close") or k.get("signal_close")
                if bp:
                    base.append({"ticker": k["ticker"], "base": float(bp),
                                 "armed": bool(k.get("armed"))})
    except Exception as e:
        errors.append(f"prior_review/packet: {e}")
    if not base and prior_mr:
        try:
            df = pd.read_csv(MR_HISTORY)
            rows = df[df["run_id"].astype(str) == str(prior_mr["run_id"])]
            src = f"mr:{prior_mr['run_id']}"
            base = [{"ticker": r["ticker"], "base": float(r["last_close"]),
                     "armed": None} for _, r in rows.iterrows()]
        except Exception as e:
            errors.append(f"prior_review/mr: {e}")
    if not base:
        return None
    tks = sorted({b["ticker"] for b in base})
    try:
        px = yf.download(tks, period="1mo", interval="1d", auto_adjust=False,
                         progress=False, threads=True, group_by="ticker")
    except Exception as e:
        errors.append(f"prior_review/prices: {e}")
        return None
    rated = []
    for b in base:
        try:
            sub = px[b["ticker"]] if len(tks) > 1 else px
            last = float(sub["Close"].dropna().iloc[-1])
            rated.append({**b, "since_pct": round((last / b["base"] - 1) * 100, 2)})
        except Exception:
            continue
    if not rated:
        return None

    def stats(rows):
        pcts = sorted(r["since_pct"] for r in rows)
        mid = len(pcts) // 2
        median = pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2
        return {"n": len(pcts),
                "pct_positive": round(100 * sum(1 for p in pcts if p > 0) / len(pcts), 1),
                "avg_pct": round(sum(pcts) / len(pcts), 2),
                "median_pct": round(median, 2)}

    ranked = sorted(rated, key=lambda r: r["since_pct"], reverse=True)
    tail = lambda rows: [{"ticker": r["ticker"], "since_pct": r["since_pct"]}
                         for r in rows]
    out = {"source": src, **stats(rated),
           "best": tail(ranked[:3]), "worst": tail(ranked[-3:][::-1])}
    armed = [r for r in rated if r.get("armed")]
    if armed:
        out["armed_subset"] = stats(armed)
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build(window_days: int, min_score: float, max_age: int, top_n: int) -> dict:
    errors: list = []
    now = datetime.now(MARKET_TZ)
    today = now.date()
    kegs, mr_meta = load_kegs(min_score, max_age, top_n, errors, today)
    sectors = sector_map(errors)
    for k in kegs:
        k["sector"] = sectors.get(k["ticker"])

    # Tonight's AMC prints are the NEAREST spark of all — include today in the
    # window while they still lie ahead (before ~20:00 ET the AMC cluster
    # hasn't finished printing). BMO/unknown slots for today are already past.
    window = next_trading_days(today, window_days)
    include_today_amc = is_trading_day(today) and now.hour < 20
    fetch_days = ([today] if include_today_amc else []) + window
    earnings = earnings_sparks(fetch_days, errors) if kegs else []
    if include_today_amc:
        earnings = [e for e in earnings
                    if e["date"] != today.isoformat() or e["slot"] == "AMC"]
    macro = macro_sparks(window, errors) if kegs else []
    macro_note = ("ForexFactory covers the current week only — macro sparks "
                  "beyond Sunday not visible"
                  if window and window[-1].isocalendar()[1] != today.isocalendar()[1]
                  else "")
    regime = regime_state(errors)

    by_symbol = {e["symbol"]: e for e in earnings}
    # Verdict eligibility is size + a resolvable sector — no curated list.
    # The mktcap floor is the real gate; sector resolution (4-tier, see
    # reporter_sectors) exists because the Nasdaq calendar carries no sector.
    verdicts = [e for e in earnings if e["mktcap"] >= VERDICT_MKTCAP_FLOOR]
    v_sectors = reporter_sectors(sorted({v["symbol"] for v in verdicts}),
                                 sectors, errors)
    for v in verdicts:
        v["sector"] = v_sectors.get(v["symbol"])

    join_sparks(kegs, by_symbol, verdicts, macro)

    sig_date = None
    if mr_meta.get("run_id"):
        sig_date = datetime.strptime(mr_meta["run_id"], "%Y%m%d").date()
    tape_check(kegs, sig_date, errors)
    review = prior_review(mr_meta.get("prior_run"), errors, today)

    # Window honesty: an unarmed keg means "no spark IN THIS WINDOW", not "no
    # catalyst exists" — a real earnings date 2 weeks out is information, not
    # absence (iteration-2 fix: NBIS read as 'no dated catalyst' while its own
    # print sat just beyond the window). Best-effort, unarmed kegs only.
    for k in kegs:
        if k["armed"]:
            continue
        try:
            cal = yf.Ticker(k["ticker"]).calendar or {}
            eds = cal.get("Earnings Date") or []
            nxt = next((d for d in sorted(str(x) for x in eds)
                        if str(d) >= today.isoformat()), None)
            k["next_own_earnings"] = nxt
        except Exception:
            k["next_own_earnings"] = None

    return {
        "as_of": now.isoformat(),
        "today": today.isoformat(),
        "spark_window": ([today.isoformat() + " (AMC only)"] if include_today_amc
                         else []) + [d.isoformat() for d in window],
        "macro_note": macro_note,
        "regime": regime,
        "mr_run": {k: v for k, v in mr_meta.items() if k != "prior_run"},
        "params": {"min_score": min_score, "max_age_runs": max_age,
                   "chase_threshold_pct": CHASE_THRESHOLD_PCT,
                   "panic_ret5d_pct": PANIC_RET5D_PCT},
        "kegs": kegs,
        "macro_calendar": macro,
        "prior_run_review": review,
        "errors": errors,
    }


def as_table(p: dict) -> str:
    lines = [f"snapback-scan {p['today']}  regime={p['regime']['state'] if p['regime'] else '?'}"
             f"  MR run {p['mr_run'].get('run_id')} (stale {p['mr_run'].get('stale_days')}d)"
             f"  window {p['spark_window'][0]}..{p['spark_window'][-1]}", ""]
    hdr = f"{'TICKER':7}{'SCORE':6}{'AGE':4}{'5D%':7}{'SINCE%':8}{'ARMED':6}SPARKS  [FLAGS]"
    lines.append(hdr)
    for k in p["kegs"]:
        # Flags ride at the END of the line: emoji are double-width in most
        # terminals, so a fixed-width flags column skews every column after it.
        flags = ("🔥" if k.get("ignited") else "") + \
                ("😴" if k.get("quiet_warning") else "")
        sparks = "; ".join(
            ("~" if s["type"] == "marketwide_verdict" else "")
            + f"{s.get('symbol') or 'macro'}@{s['date']}" for s in k["sparks"]) or "—"
        if not k["armed"] and k.get("next_own_earnings"):
            sparks += f"  (next own: {k['next_own_earnings']})"
        lines.append(f"{k['ticker']:7}{k['score']:<6.1f}{k['listing_age_runs']:<4}"
                     f"{(k.get('ret_5d_pct') if k.get('ret_5d_pct') is not None else 0):<7.1f}"
                     f"{(k.get('since_signal_pct') if k.get('since_signal_pct') is not None else 0):<8.1f}"
                     f"{'YES' if k['armed'] else 'no':6}{sparks}"
                     + (f"  [{flags}]" if flags else ""))
    r = p["prior_run_review"]
    if r:
        lines += ["", f"prior review [{r['source']}]: n={r['n']}  "
                      f"win {r['pct_positive']}%  avg {r['avg_pct']:+.2f}%  "
                      f"med {r['median_pct']:+.2f}%",
                  "  best:  " + "  ".join(f"{x['ticker']} {x['since_pct']:+.1f}%"
                                          for x in r["best"]),
                  "  worst: " + "  ".join(f"{x['ticker']} {x['since_pct']:+.1f}%"
                                          for x in r["worst"])]
        if r.get("armed_subset"):
            a = r["armed_subset"]
            lines.append(f"  armed subset: n={a['n']}  win {a['pct_positive']}%  "
                         f"avg {a['avg_pct']:+.2f}%")
    if p["errors"]:
        lines += ["", f"errors: {p['errors']}"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window-days", type=int, default=3)
    ap.add_argument("--min-score", type=float, default=40.0)
    ap.add_argument("--max-age", type=int, default=2)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--format", choices=["json", "table"], default="json")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    packet = build(args.window_days, args.min_score, args.max_age, args.top_n)
    out = json.dumps(packet, indent=2, default=str)
    if not args.no_save:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (RUNS_DIR / f"{packet['today']}.json").write_text(out)
    print(out if args.format == "json" else as_table(packet))
    if packet["errors"]:
        print(f"\n{len(packet['errors'])} source(s) degraded — see errors",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
