"""
Momentum scan: find US equities in smooth uptrends, track persistence across runs.

Cadence-agnostic: every invocation logs a timestamp. "Streak" is consecutive
runs (interpret the granularity from the actual dates).

Self-contained — uses yfinance directly, no cross-skill dependencies.

Usage:
  python scan.py                        # full run with default params
  python scan.py --window-months 6      # smoother, slower-moving leaders
  python scan.py --top-n 50             # show more names
  python scan.py --no-refresh-universe  # use cached universe regardless of TTL
  python scan.py --show-history         # dump history.csv summary
  python scan.py --clear-history        # wipe history.csv (no confirmation)
"""
import argparse
import json
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay,
    USThanksgivingDay, nearest_workday,
)
from yfinance import EquityQuery

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
UNIVERSE_FILE = STATE_DIR / "universe.txt"
HISTORY_FILE = STATE_DIR / "history.csv"
UNIVERSE_TTL_DAYS = 7
MARKET_TZ = ZoneInfo("America/New_York")  # one snapshot per US market day

HISTORY_COLS = [
    "run_id", "run_date", "ticker", "rank", "score",
    "return_pct", "max_dd_pct", "ann_vol_pct", "from_high_pct",
]


class _NYSECalendar(AbstractHolidayCalendar):
    """NYSE-observed holidays. Pandas's USFederalHolidayCalendar isn't quite
    right (NYSE observes Good Friday but not Columbus / Veterans Day). Rare
    one-off closures (e.g. presidential funerals, 9/11) aren't included —
    we'd save a stale snapshot on those (≤1 per year) and accept the noise."""
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday,
                start_date="2022-06-19"),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def is_nyse_trading_day(d: date) -> bool:
    """True iff `d` is a weekday and not an NYSE-observed holiday."""
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return _NYSECalendar().holidays(start=ts, end=ts).empty


def refresh_universe(min_market_cap: float, min_volume: int, count: int) -> list[str]:
    """Pull current US large caps via yfinance's screener. Caches to file."""
    query = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("gt", ["intradaymarketcap", min_market_cap]),
        EquityQuery("gt", ["avgdailyvol3m", min_volume]),
    ])
    raw = yf.screen(query, sortField="intradaymarketcap", sortAsc=False, size=count)
    quotes = raw.get("quotes") or []
    tickers = [q.get("symbol") for q in quotes if q.get("symbol")]
    if not tickers:
        raise RuntimeError(
            "Yahoo screener returned no results — possibly rate-limited or "
            "API drift. Try again in a few minutes."
        )
    UNIVERSE_FILE.write_text("\n".join(tickers))
    return tickers


def load_universe(min_market_cap: float, min_volume: int, count: int,
                  refresh_mode) -> list[str]:
    """refresh_mode: None = TTL-based (default), True = force, False = use cache as-is."""
    if not UNIVERSE_FILE.exists():
        return refresh_universe(min_market_cap, min_volume, count)
    if refresh_mode is True:
        return refresh_universe(min_market_cap, min_volume, count)
    cached = [t for t in UNIVERSE_FILE.read_text().splitlines() if t.strip()]
    if refresh_mode is False:
        return cached
    age_days = (datetime.now().timestamp() - UNIVERSE_FILE.stat().st_mtime) / 86400
    if age_days > UNIVERSE_TTL_DAYS:
        print(f"Universe cache stale ({age_days:.1f}d), refreshing...", file=sys.stderr)
        return refresh_universe(min_market_cap, min_volume, count)
    return cached


def fetch_prices(tickers: list[str], window_months: int) -> pd.DataFrame:
    period = f"{max(window_months + 1, 4)}mo"
    print(f"Fetching {len(tickers)} tickers, period={period}...", file=sys.stderr)
    df = yf.download(
        tickers, period=period, interval="1d", auto_adjust=True,
        progress=False, threads=True, group_by="ticker",
    )
    closes = {}
    for t in tickers:
        try:
            s = df[(t, "Close")].dropna() if (t, "Close") in df.columns else None
            if s is not None and len(s) > 60:
                closes[t] = s
        except Exception:
            pass
    return pd.DataFrame(closes).sort_index()


def score_tickers(prices: pd.DataFrame, window_months: int,
                  min_return_pct: float, max_dd_pct: float) -> list[dict]:
    cutoff = prices.index[-1] - pd.Timedelta(days=int(window_months * 30.5))
    window = prices.loc[cutoff:]
    results = []
    for t in window.columns:
        s = window[t].dropna()
        if len(s) < 60:
            continue
        ret = (s.iloc[-1] / s.iloc[0] - 1) * 100
        max_dd = ((s / s.cummax() - 1).min()) * 100
        from_high = (s.iloc[-1] / s.max() - 1) * 100
        daily = s.pct_change().dropna()
        ann_vol = daily.std() * np.sqrt(252) * 100
        # Clamp denominator at 1.0 so a near-zero drawdown doesn't blow up the
        # score (or produce inf when max_dd == 0). Means "score = return per
        # 1% drawdown, treating sub-1% pullbacks as 1%".
        score = ret / max(abs(max_dd), 1.0)
        if ret > min_return_pct and max_dd > -abs(max_dd_pct):
            results.append({
                "ticker": t,
                "return_pct": round(ret, 1),
                "max_dd_pct": round(max_dd, 1),
                "from_high_pct": round(from_high, 1),
                "ann_vol_pct": round(ann_vol, 0),
                "score": round(score, 2),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def make_run_id(now: datetime, allow_same_day: bool = False) -> str:
    """Default runs get a date-only ET-anchored id (one scan per scan-day).
    --allow-same-day intentionally records multiple snapshots per ET date, so
    fall back to second precision to keep run_id unique per row."""
    if allow_same_day:
        return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return now.astimezone(MARKET_TZ).strftime("%Y%m%d")


def clear_history():
    """Remove history.csv and any leftover .tmp from a crashed atomic write."""
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    if tmp_path.exists():
        tmp_path.unlink()


def prune_non_trading_days() -> tuple[int, int]:
    """Drop history rows whose run_date ET-date is not an NYSE trading day.
    Cleans up snapshots that were saved before the non-trading-day guard
    existed (or with --save-stale). Returns (rows_removed, run_ids_removed)."""
    history = load_history()
    if history.empty:
        return (0, 0)
    et_dates = history["run_date"].dt.tz_convert(MARKET_TZ).dt.date
    min_d, max_d = et_dates.min(), et_dates.max()
    # Precompute the trading-day set once over the whole span — cheaper than
    # calling is_nyse_trading_day per row for histories with many rows.
    cal = _NYSECalendar()
    holidays = set(cal.holidays(start=pd.Timestamp(min_d),
                                end=pd.Timestamp(max_d)).date)
    trading_mask = et_dates.map(
        lambda d: d.weekday() < 5 and d not in holidays
    )
    rows_removed = int((~trading_mask).sum())
    if rows_removed == 0:
        return (0, 0)
    run_ids_removed = int(history.loc[~trading_mask, "run_id"].nunique())
    kept = history[trading_mask].copy()
    # load_history parsed run_date to tz-aware Timestamp; re-serialize so the
    # CSV round-trips identically to the original isoformat strings.
    kept["run_date"] = kept["run_date"].apply(lambda dt: dt.isoformat())
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    kept.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)
    return (rows_removed, run_ids_removed)


def load_history() -> pd.DataFrame:
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=HISTORY_COLS)
    df = pd.read_csv(HISTORY_FILE)
    if df.empty:
        return df
    # format='ISO8601' tolerates rows with and without microseconds in the same
    # file (pre-upgrade rows used microsecond precision; new ones don't).
    df["run_date"] = pd.to_datetime(df["run_date"], utc=True, format="ISO8601")
    return df


def append_history(picks: list[dict], run_id: str, run_date: datetime,
                   allow_same_day: bool = False):
    """At most one snapshot per America/New_York calendar day — aligning the
    dedup unit with US market sessions, since that's what the data measures.
    If history already has rows for today's ET date, they're replaced, so
    re-running same-day refreshes the snapshot without inflating streak (which
    then naturally counts scan-days, not scan invocations).

    Empty picks lists are treated as no-ops rather than wipes: a failed/filtered
    scan shouldn't erase a good prior snapshot for the same day. To bypass the
    dedup entirely (e.g. for debugging), pass allow_same_day=True.

    Writes go through a sibling .tmp file + atomic rename so a crash mid-write
    can't truncate history.csv."""
    if not picks:
        return

    new_rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "run_date": run_date.isoformat(),
                "ticker": p["ticker"],
                "rank": p["rank"],
                "score": p["score"],
                "return_pct": p["return_pct"],
                "max_dd_pct": p["max_dd_pct"],
                "ann_vol_pct": p["ann_vol_pct"],
                "from_high_pct": p["from_high_pct"],
            }
            for p in picks
        ],
        columns=HISTORY_COLS,
    )

    has_existing = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    if has_existing:
        existing = pd.read_csv(HISTORY_FILE)
    else:
        existing = pd.DataFrame(columns=HISTORY_COLS)

    if not allow_same_day and not existing.empty:
        today_et = run_date.astimezone(MARKET_TZ).date()
        existing_dates = (pd.to_datetime(existing["run_date"],
                                         utc=True, format="ISO8601")
                          .dt.tz_convert(MARKET_TZ).dt.date)
        mask = existing_dates == today_et
        replaced = int(mask.sum())
        if replaced:
            print(
                f"Replacing {replaced} existing rows for {today_et} "
                f"(same America/New_York date).",
                file=sys.stderr,
            )
        existing = existing.loc[~mask]

    combined = pd.concat([existing, new_rows], ignore_index=True)
    # Sort so the on-disk file is chronological — upserts append new rows at
    # the end, which would otherwise leave the file out of order after a
    # back-fill or any non-monotonic write.
    combined = (combined
                .sort_values(["run_date", "rank"], kind="stable")
                .reset_index(drop=True))
    tmp_path = HISTORY_FILE.with_suffix(".csv.tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(HISTORY_FILE)


def enrich_with_persistence(picks: list[dict], history: pd.DataFrame,
                            current_run_id: str) -> list[dict]:
    """Add streak / first_seen / rank_delta columns based on prior runs."""
    if history.empty:
        for p in picks:
            p.update({"streak": 1, "first_seen": "—", "prev_rank": None,
                      "rank_delta": None})
        return picks

    prior = history[history["run_id"] != current_run_id].copy()
    if prior.empty:
        for p in picks:
            p.update({"streak": 1, "first_seen": "—", "prev_rank": None,
                      "rank_delta": None})
        return picks

    run_ids_ordered = (prior.sort_values("run_date")["run_id"]
                       .drop_duplicates().tolist())

    for p in picks:
        t = p["ticker"]
        appearances = prior[prior["ticker"] == t].sort_values("run_date")
        if appearances.empty:
            p.update({"streak": 1, "first_seen": "🆕", "prev_rank": None,
                      "rank_delta": None})
            continue
        p["first_seen"] = appearances["run_date"].iloc[0].strftime("%Y-%m-%d")
        prev = appearances.iloc[-1]
        p["prev_rank"] = int(prev["rank"])
        p["rank_delta"] = int(prev["rank"]) - p["rank"]  # positive = rising
        ticker_runs = set(appearances["run_id"].tolist())
        streak = 1
        for rid in reversed(run_ids_ordered):
            if rid in ticker_runs:
                streak += 1
            else:
                break
        p["streak"] = streak
    return picks


def dropouts(history: pd.DataFrame, current_picks: set[str],
             current_run_id: str, top_n: int) -> list[dict]:
    """Names that were in prior run's top N but missing now."""
    prior = history[history["run_id"] != current_run_id]
    if prior.empty:
        return []
    last_run_id = prior.sort_values("run_date")["run_id"].iloc[-1]
    last_run = prior[prior["run_id"] == last_run_id]
    last_run = last_run[last_run["rank"] <= top_n]
    dropped = last_run[~last_run["ticker"].isin(current_picks)]
    return [
        {"ticker": r["ticker"], "prev_rank": int(r["rank"]),
         "prev_return_pct": r["return_pct"]}
        for _, r in dropped.iterrows()
    ]


def render_table(picks: list[dict], top_n: int, window_months: int) -> str:
    rows = picks[:top_n]
    if not rows:
        return "(no picks passed the filter)"
    headers = ["#", "Ticker", f"{window_months}m%", "MaxDD%", "Score", "Streak",
               "RankΔ", "FirstSeen", "FromHigh%"]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for p in rows:
        delta = p.get("rank_delta")
        if delta is None:
            delta_str = "🆕"
        elif delta > 0:
            delta_str = f"+{delta} ↗"
        elif delta < 0:
            delta_str = f"{delta} ↘"
        else:
            delta_str = "—"
        out.append("| " + " | ".join([
            str(p["rank"]),
            f"**{p['ticker']}**",
            f"{p['return_pct']:+.1f}",
            f"{p['max_dd_pct']:.1f}",
            f"{p['score']:.1f}",
            str(p.get("streak", 1)),
            delta_str,
            p.get("first_seen", "—"),
            f"{p['from_high_pct']:.1f}",
        ]) + " |")
    return "\n".join(out)


def show_history_summary(history: pd.DataFrame):
    if history.empty:
        print("History is empty.")
        return
    runs = history.sort_values("run_date")["run_id"].drop_duplicates().tolist()
    print(f"Total runs: {len(runs)}")
    print(f"Date range: {history['run_date'].min().date()} → "
          f"{history['run_date'].max().date()}")
    counts = history.groupby("ticker").size().sort_values(ascending=False)
    print(f"\nTop 20 most-frequent tickers across all runs:")
    for t, c in counts.head(20).items():
        print(f"  {t:<8} {c} appearances")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-months", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--min-return-pct", type=float, default=30.0)
    ap.add_argument("--max-dd-pct", type=float, default=20.0)
    ap.add_argument("--min-market-cap", type=float, default=5e9)
    ap.add_argument("--min-volume", type=int, default=1_000_000)
    ap.add_argument("--universe-count", type=int, default=250)
    ap.add_argument("--refresh-universe", dest="refresh_universe",
                    action="store_const", const=True, default=None,
                    help="Force-refresh universe (ignore cache).")
    ap.add_argument("--no-refresh-universe", dest="refresh_universe",
                    action="store_const", const=False,
                    help="Use cached universe even if past TTL.")
    ap.add_argument("--show-history", action="store_true")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--prune-non-trading-days", action="store_true",
                    help=("Drop history rows whose run_date ET-date is not an "
                          "NYSE trading day (cleanup for pre-guard or "
                          "--save-stale runs)."))
    ap.add_argument("--no-save", action="store_true",
                    help="Don't append this run to history.")
    ap.add_argument("--save-stale", action="store_true",
                    help=("Save to history even when today's ET date is a "
                          "weekend or NYSE-observed holiday. Default skips "
                          "the save so streak counts don't inflate from "
                          "duplicate-data days. Pre-market runs on a real "
                          "trading day are always saved."))
    ap.add_argument("--allow-same-day", action="store_true",
                    help=("Append even if a row already exists for today's "
                          "America/New_York date. Default behavior overwrites "
                          "today's snapshot."))
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear_history:
        clear_history()
        print("history.csv cleared.")
        return

    if args.prune_non_trading_days:
        rows, run_ids = prune_non_trading_days()
        print(f"Pruned {rows} row(s) across {run_ids} run_id(s) "
              f"with non-trading-day ET dates.")
        return

    if args.show_history:
        show_history_summary(load_history())
        return

    universe = load_universe(args.min_market_cap, args.min_volume,
                             args.universe_count, args.refresh_universe)
    prices = fetch_prices(universe, args.window_months)
    picks = score_tickers(prices, args.window_months,
                          args.min_return_pct, args.max_dd_pct)
    history = load_history()
    now = datetime.now(timezone.utc)
    run_id = make_run_id(now, allow_same_day=args.allow_same_day)
    picks = enrich_with_persistence(picks, history, run_id)
    current_set = {p["ticker"] for p in picks[: args.top_n]}
    drops = dropouts(history, current_set, run_id, args.top_n)

    # Non-trading-day guard: yfinance happily returns the last available
    # session's close on weekends/holidays. Recording it under today's ET
    # run_id would let streak counts inflate from duplicate data.
    # Pre-market runs on a trading day still save: today is a real trading
    # day from the streak's perspective even if we ran before the open.
    today_et = now.astimezone(MARKET_TZ).date()
    today_is_trading = is_nyse_trading_day(today_et)

    if not args.no_save:
        if not today_is_trading and not args.save_stale:
            print(f"Skipping history save: {today_et} is not an NYSE trading "
                  f"day (weekend or market holiday). Pass --save-stale to "
                  f"override.", file=sys.stderr)
        else:
            append_history(picks, run_id, now, allow_same_day=args.allow_same_day)

    if args.format == "json":
        print(json.dumps({
            "run_id": run_id,
            "run_date": now.isoformat(),
            "params": vars(args),
            "universe_size": len(universe),
            "passed_filter": len(picks),
            "top_n": args.top_n,
            "picks": picks[: args.top_n],
            "dropouts_since_last_run": drops,
        }, indent=2, default=str))
        return

    n_prior = len(history["run_id"].drop_duplicates()) if not history.empty else 0
    print(f"# Momentum scan — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n**Params**: window={args.window_months}mo, "
          f"min_return={args.min_return_pct}%, "
          f"max_dd={args.max_dd_pct}%, mcap>{args.min_market_cap:.0e}")
    print(f"**Universe**: {len(universe)} tickers · "
          f"**Passed filter**: {len(picks)} · "
          f"**Prior runs**: {n_prior}")
    print(f"\n## Top {args.top_n}\n")
    print(render_table(picks, args.top_n, args.window_months))

    w = args.window_months
    if drops:
        print(f"\n## Dropouts since last run ({len(drops)})")
        for d in drops:
            print(f"- **{d['ticker']}** (was #{d['prev_rank']}, "
                  f"{w}m={d['prev_return_pct']:+.1f}%)")

    if not history.empty:
        new_entries = [p for p in picks[: args.top_n] if p.get("prev_rank") is None]
        if new_entries:
            print(f"\n## New entrants ({len(new_entries)})")
            for p in new_entries:
                print(f"- **{p['ticker']}** at #{p['rank']} "
                      f"({w}m {p['return_pct']:+.1f}%, MaxDD {p['max_dd_pct']:.1f}%)")
        sticky = [p for p in picks[: args.top_n] if p.get("streak", 1) >= 3]
        if sticky:
            print(f"\n## Persistent leaders (streak ≥ 3 runs)")
            for p in sorted(sticky, key=lambda x: -x["streak"]):
                print(f"- **{p['ticker']}** — streak {p['streak']}, "
                      f"first seen {p.get('first_seen', '—')}, now #{p['rank']}")


if __name__ == "__main__":
    main()
