"""
premarket-brief: assemble the pre-open data packet.

This is the deterministic half of premarket-brief. It answers "what does TODAY
look like, 30 minutes before the US open" — the overnight tape, the day's
catalysts (econ calendar + earnings), the headline sentiment gauge — and folds
in two things the sister scans already compute so we don't recompute them:

  - regime-scan's latest 🟢/🟡/🔴 state (the structural backdrop)  [read state/history.csv]
  - cross-scan's consensus overlap names (the watchlist)          [shell out, JSON]

Everything price-related is best-effort and degrades to None cleanly (mirrors
regime-scan's philosophy): one dead source must never sink the whole packet —
the `errors` list records what failed so the briefing can say so honestly.

The output is ONE JSON object on stdout (also saved to state/packets/). The
LLM half (SKILL.md) reads it, layers in positions + the regime/names caches,
and writes the actual briefing.

Usage:
  uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' python build_packet.py
  ... python build_packet.py --date 2026-06-08  # override "today" (testing/backfill)
  ... python build_packet.py --no-save          # don't write state/packets/
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
import warnings
from datetime import date, datetime, timedelta, timezone
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

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_ROOT = SKILL_DIR.parent                      # GitHub/skills — sister scans live here
STATE_DIR = SKILL_DIR / "state"
PACKET_DIR = STATE_DIR / "packets"
POSITIONS_FILE = SKILL_DIR / "positions.md"
REGIME_HISTORY = SKILLS_ROOT / "regime-scan" / "state" / "history.csv"
CROSS_SCAN = SKILLS_ROOT / "cross-scan" / "scripts" / "aggregate.py"
MARKET_TZ = ZoneInfo("America/New_York")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# --- The overnight tape -----------------------------------------------------
# fast_info gives last_price + previous_close in one cheap call. For futures
# prev_close is the prior settlement, so pct = the overnight move — exactly the
# "futures pointing up/down X%" tell. For Asia/Europe indices it's today's
# session change. One consistent mechanism across every instrument here.
FUTURES = {"ES=F": "S&P 500", "NQ=F": "Nasdaq 100", "YM=F": "Dow", "RTY=F": "Russell 2000"}
GLOBAL = {"^N225": "Nikkei", "^HSI": "Hang Seng", "000001.SS": "Shanghai",
          "^KS11": "KOSPI", "^GDAXI": "DAX", "^FTSE": "FTSE 100",
          "^STOXX50E": "Euro Stoxx 50", "^FCHI": "CAC 40"}
YIELDS = {"^IRX": "13wk", "^FVX": "5y", "^TNX": "10y", "^TYX": "30y"}
FX = {"DX-Y.NYB": "DXY", "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY"}
COMMODITIES = {"CL=F": "WTI crude", "BZ=F": "Brent", "GC=F": "Gold",
               "SI=F": "Silver", "HG=F": "Copper", "NG=F": "Nat gas"}
CRYPTO = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum"}
# Live VIX term structure. VIX/VIX3M > 1 = front above back = backwardation =
# acute near-term stress; < 1 = contango = calm. SAME convention as regime-scan's
# vix_term (VIX/VIX3M, >1 stress) so the brief never carries two opposite reads.
VIX_TERM = {"^VIX9D": "VIX9D", "^VIX": "VIX", "^VIX3M": "VIX3M"}

SECTOR_ETFS = {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
               "XLV": "Health Care", "XLI": "Industrials", "XLY": "Cons. Disc.",
               "XLP": "Cons. Staples", "XLU": "Utilities", "XLB": "Materials",
               "XLRE": "Real Estate", "XLC": "Comm. Svcs"}
INDEX_PROXIES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000",
                 "DIA": "Dow"}

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
NASDAQ_ECON = "https://api.nasdaq.com/api/calendar/economicevents?date={d}"
CNN_FG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
# Market-wide premarket gappers — TradingView's public web scanner (free, no key).
TV_SCANNER = "https://scanner.tradingview.com/america/scan"
# Overnight macro / M&A headlines the econ calendar + earnings list miss (free RSS).
CNBC_FEEDS = {
    "https://www.cnbc.com/id/100003114/device/rss/rss.html": "Top News",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html": "Finance",
}


# --------------------------------------------------------------------------- #
# NYSE trading-day / special-day calendar (mirrors regime-scan & momentum-scan)
# --------------------------------------------------------------------------- #
class _NYSECalendar(AbstractHolidayCalendar):
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


def third_friday(year: int, month: int) -> date:
    """Monthly options expiration = the 3rd Friday."""
    d = date(year, month, 1)
    # weekday: Mon=0..Fri=4. First Friday, then +14 days.
    first_friday = d.replace(day=1 + (4 - d.weekday()) % 7)
    return first_friday.replace(day=first_friday.day + 14)


def is_last_trading_day_of_month(d: date) -> bool:
    nxt = d
    for _ in range(7):
        nxt = nxt.fromordinal(nxt.toordinal() + 1)
        if nxt.month != d.month:
            return True  # no trading day left in d's month after d
        if is_trading_day(nxt):
            return False
    return False


def _prev_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d = d.fromordinal(d.toordinal() - 1)
    return d


def special_days(d: date) -> list[str]:
    flags = []
    # OpEx/witching land on the 3rd Friday — but when that Friday is an NYSE
    # holiday (e.g. Juneteenth 2026-06-19) expiration shifts to the prior
    # trading day (Thursday), so anchor the flag there, not on the closed Friday.
    tf = third_friday(d.year, d.month)
    opex = tf if is_trading_day(tf) else _prev_trading_day(tf)
    if d == opex:
        if d.month in (3, 6, 9, 12):
            flags.append("Quadruple witching (index + stock futures & options expire — "
                         "elevated volume & pinning into the close)")
        else:
            flags.append("Monthly options expiration (OpEx — pinning / elevated volume into the close)")
    if is_last_trading_day_of_month(d):
        if d.month in (3, 6, 9, 12):
            flags.append("Quarter-end (rebalancing flows / window dressing)")
        else:
            flags.append("Month-end (rebalancing flows)")
    # Known NYSE half-days (1pm close): day after Thanksgiving, Christmas Eve, July 3.
    if (d.month == 7 and d.day == 3) or (d.month == 12 and d.day == 24):
        flags.append("Likely NYSE half-day (1pm ET early close — thin afternoon liquidity)")
    if d.month == 11 and d.weekday() == 4 and 23 <= d.day <= 29:
        flags.append("Likely NYSE half-day (day after Thanksgiving, 1pm ET early close)")
    return flags


# --------------------------------------------------------------------------- #
# yfinance tape
# --------------------------------------------------------------------------- #
def quote(ticker: str) -> dict | None:
    """last_price + previous_close + pct via fast_info, the cheap path.
    Use ATTRIBUTE access — FastInfo.get() uses camelCase keys ('lastPrice'),
    so fi.get('last_price') silently returns None. The attributes are snake_case
    and work. Returns None when Yahoo has no data (degrades cleanly). For futures
    prev_close is the prior settlement, so pct is the overnight move."""
    try:
        fi = yf.Ticker(ticker).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if not prev:
            return None
        return {"last": round(last, 4), "prev_close": round(prev, 4),
                "pct": round((last / prev - 1) * 100, 2)}
    except Exception:
        return None


def tape_block(mapping: dict[str, str]) -> dict:
    out = {}
    for tk, label in mapping.items():
        q = quote(tk)
        out[tk] = {"label": label, **(q or {"last": None, "prev_close": None, "pct": None})}
    return out


def premarket_movers(tickers: list[str]) -> dict:
    """Best-effort premarket prints for a focused set (watchlist + positions +
    sectors + index proxies). Premarket single-stock data is thin and noisy —
    callers should weight it lightly — but 30 min before the open it's the only
    read on overnight gaps in individual names. Uses 1m prepost bars; degrades
    to {} with a note when the session has no premarket data yet."""
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {"note": "no tickers to fetch", "movers": {}}
    try:
        df = yf.download(tickers, period="1d", interval="1m", prepost=True,
                         auto_adjust=False, progress=False, threads=True,
                         group_by="ticker")
    except Exception as e:
        return {"note": f"premarket fetch failed: {e}", "movers": {}}
    movers = {}
    for tk in tickers:
        try:
            sub = df[tk] if len(tickers) > 1 else df
            closes = sub["Close"].dropna()
            if closes.empty:
                continue
            last = float(closes.iloc[-1])
            prev = quote(tk)
            prev_close = prev["prev_close"] if prev else None
            if not prev_close:
                continue
            movers[tk] = {"premkt": round(last, 4),
                          "prev_close": round(prev_close, 4),
                          "pct": round((last / prev_close - 1) * 100, 2),
                          "as_of": str(closes.index[-1])}
        except Exception:
            continue
    note = "" if movers else "no premarket prints yet (pre-session or market closed)"
    return {"note": note, "movers": movers}


def vix_term_structure() -> dict:
    """Live VIX term structure — VIX9D (9-day) / VIX (30-day) / VIX3M (3-month).
    The SHAPE is the read, not any single level: VIX/VIX3M > 1 means the front is
    above the back (backwardation) = acute near-term stress; < 1 = contango =
    calm. Same convention as regime-scan's vix_term, but this is the LIVE
    overnight read — regime.vix_term is an end-of-day cache that can lag a fast
    overnight risk shift. Degrades per-leg via tape_block (None where Yahoo has
    no print); a lone level is suspect overnight, the ratio is the corroboration."""
    levels = tape_block(VIX_TERM)

    def lvl(tk):
        return (levels.get(tk) or {}).get("last")

    vix9d, vix, vix3m = lvl("^VIX9D"), lvl("^VIX"), lvl("^VIX3M")
    term = round(vix / vix3m, 3) if (vix and vix3m) else None   # >1 = backwardation
    near = round(vix9d / vix, 3) if (vix9d and vix) else None   # >1 = front-loaded
    shape = ("backwardation" if term > 1 else "contango") if term is not None else None
    return {"levels": levels, "vix_3m_ratio": term, "vix_9d_ratio": near,
            "shape": shape}


def premarket_gappers(errors: list, min_mktcap: float = 2e9,
                      min_premkt_vol: int = 50_000, top_n: int = 10) -> dict:
    """Market-wide premarket movers via TradingView's public scanner — the names
    gapping that AREN'T necessarily in your book or the watchlist (FDA, M&A,
    guidance cuts, analyst bombs). Free, no API key. Unofficial endpoint, so it
    degrades cleanly to empty on any failure. The market-cap and premarket-volume
    floors strip illiquid one-print noise so a single odd-lot trade can't headline
    the brief. NOTE: thin / near-zero in the hours before ~8:00 ET — weight lightly
    early; it's real by ~30 min before the open (when this skill runs)."""
    cols = ["name", "description", "premarket_change", "premarket_volume",
            "close", "volume", "market_cap_basic", "sector"]
    flt = [
        {"left": "premarket_change", "operation": "nempty"},
        {"left": "market_cap_basic", "operation": "egreater", "right": min_mktcap},
        {"left": "premarket_volume", "operation": "egreater", "right": min_premkt_vol},
    ]

    def _scan(order):
        body = {"filter": flt, "columns": cols,
                "sort": {"sortBy": "premarket_change", "sortOrder": order},
                "range": [0, top_n]}
        data = http_json(TV_SCANNER, method="POST", body=body)
        out = []
        for r in (data.get("data") or []):
            d = r.get("d") or []
            if len(d) < len(cols):
                continue
            rec = dict(zip(cols, d))
            pct = rec.get("premarket_change")
            out.append({
                "ticker": rec.get("name"),
                "name": rec.get("description"),
                "pct": round(pct, 2) if pct is not None else None,
                "premkt_vol": int(rec["premarket_volume"]) if rec.get("premarket_volume") else None,
                "mktcap": rec.get("market_cap_basic"),
                "sector": rec.get("sector"),
            })
        return out

    def _safe(order):
        try:
            return _scan(order)
        except Exception as e:
            errors.append(f"premarket_gappers/tradingview({order}): {e}")
            return None

    # Each side degrades on its own — a failed losers scan must not discard the
    # gainers we already have (this module's "one dead source never sinks the
    # rest" rule, applied within the source too).
    gainers, losers = _safe("desc"), _safe("asc")
    if gainers is None and losers is None:
        return {"source": "unavailable", "note": "", "gainers": [], "losers": []}
    gainers, losers = gainers or [], losers or []
    # Dedup: when few names clear the filter (thin pre-dawn hours) the top-N and
    # bottom-N windows overlap, so a mid-pack name can land in BOTH lists. Keep it
    # as a gainer and drop it from losers — nothing is simultaneously today's
    # biggest gainer and biggest loser.
    gainer_syms = {g["ticker"] for g in gainers}
    losers = [l for l in losers if l["ticker"] not in gainer_syms]
    return {"source": "tradingview", "note": "",
            "filters": {"min_mktcap": min_mktcap, "min_premkt_vol": min_premkt_vol},
            "gainers": gainers, "losers": losers}


def overnight_headlines(now: datetime, errors: list, lookback_h: int = 18,
                        top_n: int = 12) -> dict:
    """Top business headlines from the overnight window — the macro / geopolitical
    / central-bank / M&A catalysts the econ calendar and earnings list miss. CNBC
    RSS (free, no key), stdlib XML parse. Filtered to the last `lookback_h` hours
    and de-duped by title so it surfaces what actually broke overnight, not stale
    items."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    cutoff = now - timedelta(hours=lookback_h)
    items, seen = [], set()
    for url, feed in CNBC_FEEDS.items():
        try:
            root = ET.fromstring(http_text(url))
        except Exception as e:
            errors.append(f"headlines/{feed}: {e}")
            continue
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            if not title or title in seen:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(MARKET_TZ)
            except Exception:
                continue
            if dt < cutoff:
                continue
            seen.add(title)
            items.append({"_ts": dt, "time_et": dt.strftime("%a %H:%M ET"),
                          "title": title, "feed": feed,
                          "link": (it.findtext("link") or "").strip()})
    items.sort(key=lambda x: x["_ts"], reverse=True)
    for x in items:
        x.pop("_ts", None)
    return {"source": "cnbc", "window_h": lookback_h, "headlines": items[:top_n]}


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only — no requests dependency)
# --------------------------------------------------------------------------- #
def http_json(url: str, headers: dict | None = None, timeout: int = 20,
              method: str | None = None, body: dict | None = None):
    """GET by default; pass `body` (a dict) to POST it as JSON — used for the
    TradingView scanner. Backward-compatible: existing GET callers are unchanged."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, **(headers or {})}
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_text(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    """Raw text fetch (for RSS/XML, which isn't JSON)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def econ_calendar(today: date, errors: list) -> dict:
    """US-relevant econ events for today. ForexFactory weekly JSON is primary
    (clean, ET-stamped, impact-rated, no key); Nasdaq is the backup."""
    # Primary: ForexFactory faireconomy weekly feed.
    try:
        data = http_json(FF_CALENDAR_URL)
        events = []
        for ev in data:
            # Guard each row: a single malformed date must not discard every
            # other event and silently drop us to the weaker backup. Skip the
            # bad row, keep the rest (matches this module's degrade-cleanly rule).
            try:
                country = ev.get("country", "")
                impact = ev.get("impact", "")
                if country not in ("USD", "All"):
                    continue
                if impact not in ("High", "Medium"):
                    continue
                dt = datetime.fromisoformat(ev["date"]).astimezone(MARKET_TZ)
                if dt.date() != today:
                    continue
                events.append({
                    "time_et": dt.strftime("%H:%M"),
                    "title": ev.get("title", ""),
                    "impact": impact,
                    "scope": "US" if country == "USD" else "Global",
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                })
            except Exception:
                continue
        events.sort(key=lambda e: e["time_et"])
        # Trust ForexFactory on success even when empty — an empty US High/Med
        # list is a real "no major US data today" signal (common on Mondays),
        # not a failure. Only fall to the backup if the fetch itself threw.
        return {"source": "forexfactory", "events": events}
    except Exception as e:
        errors.append(f"econ_calendar/forexfactory: {e}")
    # Backup: Nasdaq economic events (only when ForexFactory is unreachable).
    try:
        data = http_json(NASDAQ_ECON.format(d=today.isoformat()),
                         headers={"Accept": "application/json, text/plain, */*"})
        rows = (data.get("data") or {}).get("rows") or []
        events = [{
            "time_et": r.get("gmt", ""),     # Nasdaq labels this column "Time"
            "title": r.get("eventName", ""),
            "impact": "",
            "scope": r.get("country", ""),
            "forecast": r.get("consensus", "").strip(),
            "previous": r.get("previous", ""),
        } for r in rows if (r.get("country") or "").lower() in ("united states", "")]
        return {"source": "nasdaq-backup", "events": events}
    except Exception as e:
        errors.append(f"econ_calendar/nasdaq: {e}")
    return {"source": "unavailable", "events": []}


def _parse_mktcap(s: str) -> float:
    try:
        return float(s.replace("$", "").replace(",", ""))
    except Exception:
        return 0.0


def earnings_today(today: date, watchlist: set, positions: set, errors: list) -> dict:
    """Who reports today, split pre/after. Keep the megacaps (they move the
    index) plus anything in your watchlist or book (those move YOU)."""
    try:
        data = http_json(NASDAQ_EARNINGS.format(d=today.isoformat()),
                         headers={"Accept": "application/json, text/plain, */*"})
        rows = (data.get("data") or {}).get("rows") or []
    except Exception as e:
        errors.append(f"earnings/nasdaq: {e}")
        return {"source": "unavailable", "before": [], "after": [],
                "watchlist_matches": [], "position_matches": []}

    def slot(r):
        t = r.get("time", "")
        if "pre-market" in t:
            return "before"
        if "after-hours" in t:
            return "after"
        return "unknown"

    parsed = []
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        parsed.append({
            "symbol": sym,
            "name": r.get("name", ""),
            "slot": slot(r),
            "mktcap": _parse_mktcap(r.get("marketCap", "")),
            "eps_forecast": r.get("epsForecast", ""),
        })
    by_cap = sorted(parsed, key=lambda x: x["mktcap"], reverse=True)
    # Notable = top 15 by market cap; always also surface watchlist/position hits.
    notable_syms = {x["symbol"] for x in by_cap[:15]}
    wl = sorted({x["symbol"] for x in parsed} & {s.upper() for s in watchlist})
    pos = sorted({x["symbol"] for x in parsed} & {s.upper() for s in positions})
    keep = [x for x in by_cap if x["symbol"] in notable_syms
            or x["symbol"] in wl or x["symbol"] in pos]
    return {
        "source": "nasdaq",
        "before": [x for x in keep if x["slot"] == "before"],
        "after": [x for x in keep if x["slot"] == "after"],
        "unknown_time": [x for x in keep if x["slot"] == "unknown"],
        "watchlist_matches": wl,
        "position_matches": pos,
        "total_reporting": len(parsed),
    }


def rating_changes(tickers: list[str], today: date, errors: list,
                   lookback_days: int = 4) -> dict:
    """Recent analyst actions — upgrades / downgrades and price-target moves — for
    your book + watchlist. These gap single names hard pre-open. yfinance scrapes
    Yahoo's history (free, existing dep); it's one call per name, so callers cap
    the list. Filtered to the last `lookback_days` so it's fresh actions, not a
    year of history. Degrades per-ticker — one dead name never sinks the rest."""
    tickers = sorted(set(t.upper() for t in tickers if t))
    if not tickers:
        return {"source": "yfinance", "lookback_days": lookback_days, "changes": []}
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=lookback_days)
    changes = []
    for tk in tickers:
        try:
            ud = yf.Ticker(tk).upgrades_downgrades
            if ud is None or ud.empty:
                continue
            idx = ud.index
            # GradeDate index is usually tz-naive; strip tz if present so the
            # comparison against a tz-naive cutoff can't raise.
            recent = ud[idx.tz_localize(None) >= cutoff] if getattr(idx, "tz", None) else ud[idx >= cutoff]
            for ts, row in recent.iterrows():
                changes.append({
                    "ticker": tk,
                    "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
                    "firm": row.get("Firm", ""),
                    "action": row.get("Action", ""),
                    "from_grade": row.get("FromGrade", ""),
                    "to_grade": row.get("ToGrade", ""),
                    "pt_action": row.get("priceTargetAction", ""),
                    "pt": row.get("currentPriceTarget"),
                    "prior_pt": row.get("priorPriceTarget"),
                })
        except Exception as e:
            errors.append(f"ratings/{tk}: {e}")
            continue
    changes.sort(key=lambda c: c["date"], reverse=True)
    return {"source": "yfinance", "lookback_days": lookback_days, "changes": changes}


def fear_greed(errors: list) -> dict | None:
    try:
        data = http_json(CNN_FG, headers={
            "Accept": "application/json",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
            "Origin": "https://www.cnn.com",
        })
        fg = data.get("fear_and_greed") or {}
        return {
            "score": round(float(fg.get("score")), 1) if fg.get("score") is not None else None,
            "rating": fg.get("rating"),
            "prev_close": round(float(fg["previous_close"]), 1) if fg.get("previous_close") is not None else None,
            "week_ago": round(float(fg["previous_1_week"]), 1) if fg.get("previous_1_week") is not None else None,
            "month_ago": round(float(fg["previous_1_month"]), 1) if fg.get("previous_1_month") is not None else None,
            "year_ago": round(float(fg["previous_1_year"]), 1) if fg.get("previous_1_year") is not None else None,
            "as_of": fg.get("timestamp"),
        }
    except Exception as e:
        errors.append(f"fear_greed/cnn: {e}")
        return None


# --------------------------------------------------------------------------- #
# Sister-scan caches (read, don't recompute)
# --------------------------------------------------------------------------- #
def regime_state(today: date, errors: list) -> dict | None:
    """Latest row of regime-scan's history.csv — the structural backdrop."""
    if not REGIME_HISTORY.exists():
        errors.append(f"regime: {REGIME_HISTORY} not found (run /regime-scan)")
        return None
    try:
        df = pd.read_csv(REGIME_HISTORY)
        if df.empty:
            return None
        row = df.iloc[-1].to_dict()
        snap = str(row.get("run_id", ""))
        snap_date = None
        try:
            snap_date = datetime.strptime(snap, "%Y%m%d").date()
        except Exception:
            pass
        stale = (today - snap_date).days if snap_date else None
        return {
            "snapshot": snap,
            "stale_days": stale,
            "state": row.get("state"),
            "score": row.get("score"),
            "vix": row.get("vix"),
            "vix_term": row.get("vix_term"),
            "breadth_50_pct": row.get("breadth_50_pct"),
            "breadth_200_pct": row.get("breadth_200_pct"),
            "rsp_spy_pct": row.get("rsp_spy_pct"),
            "credit_pct": row.get("credit_pct"),
            "def_off_pct": row.get("def_off_pct"),
            "spy_vs_200_pct": row.get("spy_vs_200_pct"),
            "ma200_slope_pct": row.get("ma200_slope_pct"),
            "flags": row.get("flags") if isinstance(row.get("flags"), str) else "",
        }
    except Exception as e:
        errors.append(f"regime: {e}")
        return None


def cross_scan_names(errors: list, top_n: int = 40) -> dict:
    """Consensus overlap names from cross-scan (reads its caches; no refresh)."""
    if not CROSS_SCAN.exists():
        errors.append(f"names: {CROSS_SCAN} not found")
        return {"source": "unavailable", "overlaps": []}
    try:
        out = subprocess.run(
            [sys.executable, str(CROSS_SCAN), "--format", "json", "--top-n", str(top_n)],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            errors.append(f"names/cross-scan rc={out.returncode}: {out.stderr[-300:]}")
            return {"source": "error", "overlaps": []}
        data = json.loads(out.stdout)
        overlaps = [{
            "ticker": o["ticker"],
            "sector": o.get("sector"),
            "overlap_count": o.get("overlap_count"),
            "scans": o.get("scans"),
            "read": o.get("composite_read"),
        } for o in data.get("overlaps", [])]
        return {"source": "cross-scan",
                "scan_freshness": data.get("scans"),
                "overlaps": overlaps}
    except Exception as e:
        errors.append(f"names/cross-scan: {e}")
        return {"source": "error", "overlaps": []}


def load_positions(errors: list) -> list[dict]:
    """positions.md is a tiny CSV: ticker,shares,avg_cost,stop,opened,tag.
    Everything past `ticker` is optional — the briefing degrades to event-risk
    flags only when shares/cost are blank. Holdings/cost live HERE; thesis &
    conviction live in your notes repo (read the distill-ticker snapshot)."""
    if not POSITIONS_FILE.exists():
        return []
    out = []
    try:
        for ln in POSITIONS_FILE.read_text().splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or s.lower().startswith("ticker,"):
                continue
            parts = [p.strip() for p in s.split(",")]
            parts += [""] * (6 - len(parts))
            tk, shares, avg, stop, opened, tag = parts[:6]
            # Only accept a plausible ticker symbol as the first field — this is
            # a doc-heavy file, so ignore any stray prose line that slipped past
            # the `#` filter rather than inventing a phantom position from it.
            if not re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,9}", tk or ""):
                continue

            def num(x):
                try:
                    return float(x)
                except Exception:
                    return None
            out.append({"ticker": tk.upper(), "shares": num(shares),
                        "avg_cost": num(avg), "stop": num(stop),
                        "opened": opened or None, "tag": tag or None})
    except Exception as e:
        errors.append(f"positions: {e}")
    return out


# --------------------------------------------------------------------------- #
# Defensive guard — is this run inside the pre-open window?
# --------------------------------------------------------------------------- #
def session_phase(now: datetime, today: date) -> dict:
    """Classify the run time vs the NYSE session so the briefing knows whether
    its premarket data is valid. premarket-brief is built for the pre-open
    window (04:00–09:30 ET). Run intraday/after-hours and the `premkt` fields
    are really LIVE / after-hours prices, not overnight gaps — flag it loudly so
    the briefing can void them instead of mistaking a 2pm tick for a pre-open gap."""
    # --date backfill: the packet date is overridden but the tape is fetched LIVE
    # at `now`, so a session-window check against `today` is meaningless — and the
    # premarket figures are today's live tape mislabeled as `today`. Flag both.
    if today != now.date():
        return {"phase": "date-override", "et_time": f"{now:%H:%M}", "valid": False,
                "intended_window": "04:00–09:30 ET (pre-open)",
                "warning": (f"⚠️ --date backfill: packet dated {today.isoformat()} but the "
                            f"tape is fetched LIVE at {now:%H:%M} ET on {now.date().isoformat()} "
                            f"— premarket figures are TODAY's data, not {today.isoformat()}'s; "
                            f"the session-window check does not apply.")}
    if not is_trading_day(today):
        phase, valid = "non-trading-day", False
    else:
        mins = now.hour * 60 + now.minute
        if mins < 4 * 60:                 # last bar is still the prior session's AH
            phase, valid = "pre-dawn", False
        elif mins < 9 * 60 + 30:          # the intended window ✅
            phase, valid = "pre-open", True
        elif mins < 16 * 60:              # `premkt` = live regular-hours price
            phase, valid = "intraday", False
        elif mins < 20 * 60:              # `premkt` = after-hours price
            phase, valid = "after-hours", False
        else:                             # next session not forming yet
            phase, valid = "overnight", False
    msg = {
        "pre-open": "",
        "pre-dawn": "premarket prints may still be the prior session's after-hours "
                    "bar, not today's gap — check each mover's as_of date",
        "intraday": "market is OPEN — `premkt` fields are LIVE intraday prices, "
                    "NOT overnight gaps; the gap read is void",
        "after-hours": "market has CLOSED — `premkt` fields are after-hours prices, "
                       "NOT a pre-open gap (premarket-brief isn't a post-close recap)",
        "overnight": "premarket for the next session has not formed yet",
        "non-trading-day": "not a NYSE trading day — the whole tape is stale",
    }[phase]
    warning = "" if valid else (
        f"⚠️ OUT-OF-WINDOW: run at {now:%H:%M} ET ({phase}) — {msg}. premarket-brief "
        f"is built for the 04:00–09:30 ET pre-open window.")
    return {"phase": phase, "et_time": f"{now:%H:%M}", "valid": valid,
            "intended_window": "04:00–09:30 ET (pre-open)", "warning": warning}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build(today: date, now: datetime | None = None) -> dict:
    errors: list = []
    # Caller passes the same `now` it derived `today` from, so the session-window
    # check and the date never straddle a midnight tick between two now() reads.
    now = now if now is not None else datetime.now(MARKET_TZ)
    session = session_phase(now, today)

    positions = load_positions(errors)
    pos_tickers = {p["ticker"] for p in positions}
    names = cross_scan_names(errors)
    watchlist = {o["ticker"] for o in names.get("overlaps", [])}

    earn = earnings_today(today, watchlist, pos_tickers, errors)

    # Annotate positions with today's event risk (cheap joins).
    reporting = {x["symbol"] for x in earn.get("before", []) + earn.get("after", [])
                 + earn.get("unknown_time", [])}
    for p in positions:
        p["reports_today"] = p["ticker"] in reporting
        p["in_overlap"] = p["ticker"] in watchlist

    # Analyst-action universe: every position + the strongest overlap names. Capped
    # because rating_changes is one yfinance call per name (positions always
    # included — those move YOU; watchlist trimmed to the top consensus handful).
    overlap_order = [o["ticker"] for o in names.get("overlaps", [])]
    ratings_universe = sorted(pos_tickers) + overlap_order[:15]

    packet = {
        "as_of": now.isoformat(),
        "today_et": today.isoformat(),
        "trading_day": is_trading_day(today),
        "session": session,
        "special_days": special_days(today),
        "overnight_tape": {
            "futures": tape_block(FUTURES),
            "vix": vix_term_structure(),
            "global": tape_block(GLOBAL),
            "yields": tape_block(YIELDS),
            "fx": tape_block(FX),
            "commodities": tape_block(COMMODITIES),
            "crypto": tape_block(CRYPTO),
        },
        "index_premarket": premarket_movers(list(INDEX_PROXIES)),
        "sectors_premarket": premarket_movers(list(SECTOR_ETFS)),
        "premarket_movers": premarket_movers(sorted(watchlist | pos_tickers)),
        "premarket_gappers": premarket_gappers(errors),
        "calendar": econ_calendar(today, errors),
        "earnings": earn,
        "rating_changes": rating_changes(ratings_universe, today, errors),
        "sentiment": {"fear_greed": fear_greed(errors)},
        "headlines": overnight_headlines(now, errors),
        "regime": regime_state(today, errors),
        "names": names,
        "positions": positions,
        "errors": errors,
    }

    # When the run is out-of-window, stamp the warning straight onto every premarket
    # block the briefing reads (single names, sectors, indices, and the market-wide
    # gappers) — so a live/AH/stale price can't pass for a fresh pre-open gap.
    if not session["valid"]:
        for key in ("index_premarket", "sectors_premarket", "premarket_movers",
                    "premarket_gappers"):
            blk = packet[key]
            blk["note"] = (session["warning"] + (f" | {blk['note']}" if blk.get("note") else "")).strip()

    return packet


def realized_moves(target: date, extra: list[str]) -> dict:
    """Actual session result for a past day — index proxies + sectors (+ any
    names the briefing flagged). This is the reconciliation half: compare what
    the briefing CALLED against what the tape actually DID, so the regime call
    gets calibrated over time. Daily close vs the prior trading day's close."""
    tickers = sorted(set(INDEX_PROXIES) | set(SECTOR_ETFS) | set(t.upper() for t in extra if t))
    try:
        df = yf.download(tickers, period="1mo", interval="1d",
                         auto_adjust=True, progress=False, threads=True,
                         group_by="ticker")
    except Exception as e:
        return {"date": target.isoformat(), "error": str(e), "moves": {}}
    tgt = pd.Timestamp(target)
    moves = {}
    for tk in tickers:
        try:
            closes = (df[tk]["Close"] if len(tickers) > 1 else df["Close"]).dropna()
            if tgt not in closes.index or len(closes.loc[:tgt]) < 2:
                continue
            upto = closes.loc[:tgt]
            close, prev = float(upto.iloc[-1]), float(upto.iloc[-2])
            moves[tk] = {"close": round(close, 4), "pct": round((close / prev - 1) * 100, 2),
                         "label": INDEX_PROXIES.get(tk) or SECTOR_ETFS.get(tk) or tk}
        except Exception:
            continue
    return {"date": target.isoformat(), "moves": moves}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="Override today (YYYY-MM-DD)")
    ap.add_argument("--no-save", action="store_true",
                    help="Don't write a copy to state/packets/")
    ap.add_argument("--actuals", default=None, metavar="YYYY-MM-DD",
                    help="Reconciliation mode: print the realized session result "
                         "for that past day (index proxies + sectors), not a packet.")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated extra tickers to include in --actuals.")
    args = ap.parse_args(argv)

    if args.actuals:
        target = datetime.strptime(args.actuals, "%Y-%m-%d").date()
        extra = [t for t in args.tickers.split(",") if t.strip()]
        print(json.dumps(realized_moves(target, extra), indent=2, default=str))
        return 0

    now = datetime.now(MARKET_TZ)
    today = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
             else now.date())

    packet = build(today, now)
    out = json.dumps(packet, indent=2, default=str)

    # Out-of-window warning to stderr (the packet carries the structured version).
    # Same-day re-runs intentionally overwrite the packet snapshot, silently.
    if not packet["session"]["valid"]:
        print(f"WARNING: {packet['session']['warning']}", file=sys.stderr)

    if not args.no_save:
        PACKET_DIR.mkdir(parents=True, exist_ok=True)
        (PACKET_DIR / f"{today.isoformat()}.json").write_text(out)

    print(out)
    if packet["errors"]:
        print(f"\n{len(packet['errors'])} source(s) degraded — see packet.errors",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
