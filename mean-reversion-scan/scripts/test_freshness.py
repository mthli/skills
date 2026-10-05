"""Tests for the daily-bar freshness check (ensure_latest_session) and
the signal-bar anchoring the outcome resolver uses.

Run:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
      --with 'pytest' pytest scripts/test_freshness.py
"""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import scan

ET = scan.MARKET_TZ
SESSION = date(2026, 10, 2)  # a Friday
AFTER_CLOSE = datetime(2026, 10, 2, 20, 0, tzinfo=ET)


def daily_bars(end: str, tickers=("AAA", "BBB"), periods=30) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=periods)
    cols = {}
    for i, t in enumerate(tickers):
        base = 100.0 * (i + 1)
        cols[(t, "Open")] = [base] * periods
        cols[(t, "High")] = [base + 1] * periods
        cols[(t, "Low")] = [base - 1] * periods
        cols[(t, "Close")] = [base] * periods
        cols[(t, "Volume")] = [1_000_000] * periods  # int64, like yfinance
    df = pd.DataFrame(cols, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def intraday_frame(session: date, tickers=("AAA", "BBB")) -> pd.DataFrame:
    idx = pd.date_range(f"{session} 09:30", f"{session} 15:30", freq="30min",
                        tz=ET)
    n = len(idx)
    cols = {}
    for i, t in enumerate(tickers):
        base = 100.0 * (i + 1)
        path = np.linspace(base, base * 1.05, n)
        cols[(t, "Open")] = path - 0.1
        cols[(t, "High")] = path + 0.5
        cols[(t, "Low")] = path - 0.5
        cols[(t, "Close")] = path
        cols[(t, "Volume")] = [10_000] * n
    df = pd.DataFrame(cols, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


@pytest.fixture
def intraday(monkeypatch):
    """Route yf.download to a fake intraday response; records each call."""
    calls = []

    def install(frame):
        def fake(tickers, **kw):
            calls.append((list(tickers), kw))
            return frame
        monkeypatch.setattr(scan.yf, "download", fake)
        return calls
    return install


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 2, 20, 0, tzinfo=ET), date(2026, 10, 2)),  # Fri after close
    (datetime(2026, 10, 2, 15, 59, tzinfo=ET), date(2026, 10, 1)),  # Fri mid-session
    (datetime(2026, 10, 3, 12, 0, tzinfo=ET), date(2026, 10, 2)),  # Saturday
    (datetime(2026, 10, 5, 9, 0, tzinfo=ET), date(2026, 10, 2)),  # Mon pre-market
    (datetime(2026, 7, 6, 9, 0, tzinfo=ET), date(2026, 7, 2)),  # after Jul 3 holiday
    # 20:00 ET is already the next UTC day; the ET date must win.
    (datetime(2026, 10, 3, 0, 0, tzinfo=scan.timezone.utc), date(2026, 10, 2)),
])
def test_expected_session(now, expected):
    assert scan.expected_session(now) == expected


def test_healthy_day_fetches_nothing(intraday):
    calls = intraday(intraday_frame(SESSION))
    bars = daily_bars("2026-10-02")
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB"], AFTER_CLOSE)
    assert calls == []
    assert fr == {"expected": SESSION, "asof": SESSION,
                  "repaired": [], "missing": []}
    assert out is bars


def test_nan_close_row_is_rebuilt_from_intraday(intraday):
    calls = intraday(intraday_frame(SESSION))
    bars = daily_bars("2026-10-02")
    bars.loc[pd.Timestamp(SESSION), ("AAA", "Close")] = np.nan
    bars.loc[pd.Timestamp(SESSION), ("BBB", "Close")] = np.nan
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB"], AFTER_CLOSE)

    assert calls[0][1]["interval"] == scan.INTRADAY_REPAIR_INTERVAL
    assert calls[0][1]["prepost"] is False
    assert fr["asof"] == SESSION and fr["repaired"] == ["AAA", "BBB"]
    row = out.loc[pd.Timestamp(SESSION)]
    assert row[("AAA", "Close")] == pytest.approx(105.0)  # last bar's close
    assert row[("AAA", "Open")] == pytest.approx(99.9)  # first bar's open
    assert row[("AAA", "High")] == pytest.approx(105.5)
    assert row[("AAA", "Low")] == pytest.approx(99.5)
    # Intraday volume undercounts; the session's volume stays unknown.
    assert np.isnan(row[("AAA", "Volume")])
    # Earlier sessions are untouched.
    assert out[("AAA", "Volume")].iloc[-2] == 1_000_000


def test_missing_row_is_appended(intraday):
    intraday(intraday_frame(SESSION))
    bars = daily_bars("2026-10-01")
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB"], AFTER_CLOSE)
    assert out.index[-1] == pd.Timestamp(SESSION)
    assert out.index.is_monotonic_increasing
    assert fr["asof"] == SESSION
    assert out[("AAA", "Close")].dropna().index[-1] == pd.Timestamp(SESSION)


def test_failed_rebuild_reports_stale(intraday, capsys):
    intraday(pd.DataFrame())
    bars = daily_bars("2026-10-01")
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB"], AFTER_CLOSE)
    assert fr["asof"] == date(2026, 10, 1)
    assert fr["missing"] == ["AAA", "BBB"] and fr["repaired"] == []
    assert "WARNING" in capsys.readouterr().err
    banner = scan.render_data_freshness(fr)
    assert "Stale data" in banner and "2026-10-01" in banner


def test_partial_rebuild_keeps_majority_asof(intraday):
    intraday(intraday_frame(SESSION, tickers=("AAA", "BBB")))
    bars = daily_bars("2026-10-01", tickers=("AAA", "BBB", "CCC"))
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB", "CCC"],
                                         AFTER_CLOSE)
    assert fr["asof"] == SESSION
    assert fr["missing"] == ["CCC"]
    assert np.isnan(out.loc[pd.Timestamp(SESSION), ("CCC", "Close")])


def test_long_dead_ticker_is_not_fetched(intraday):
    calls = intraday(intraday_frame(SESSION))
    bars = daily_bars("2026-10-02", tickers=("AAA", "DEAD"))
    bars.loc[bars.index[-20]:, ("DEAD", "Close")] = np.nan
    out, fr = scan.ensure_latest_session(bars, ["AAA", "DEAD"], AFTER_CLOSE)
    assert calls == []
    assert fr["missing"] == []


def test_mid_session_run_is_not_stale(intraday):
    calls = intraday(intraday_frame(SESSION))
    bars = daily_bars("2026-10-02")  # today's partial bar present
    mid = datetime(2026, 10, 2, 11, 0, tzinfo=ET)
    out, fr = scan.ensure_latest_session(bars, ["AAA", "BBB"], mid)
    assert calls == []
    assert fr["expected"] == date(2026, 10, 1) and fr["asof"] == SESSION
    line = scan.render_data_freshness(fr)
    assert "Stale" not in line and "session in progress" in line


def test_banner_names_the_rebuild():
    fr = {"expected": SESSION, "asof": SESSION,
          "repaired": ["AAA", "BBB"], "missing": []}
    line = scan.render_data_freshness(fr)
    assert line.startswith("**Data**: daily bars through 2026-10-02")
    assert "rebuilt" in line and "2 names" in line


def test_regime_rebuilds_spy_session(monkeypatch):
    idx = pd.bdate_range(end="2026-10-01", periods=260)
    spy = pd.DataFrame({"Close": np.linspace(500, 600, len(idx))}, index=idx)
    monkeypatch.setattr(scan.yf, "download", lambda *a, **k: spy)
    monkeypatch.setattr(scan, "session_bars_from_intraday",
                        lambda tickers, session: {"SPY": {"Close": 650.0}})
    regime = scan.compute_regime({}, session=SESSION)
    assert regime["spy_last"] == 650.0




def test_history_records_data_asof_without_float_noise(tmp_path, monkeypatch):
    f = tmp_path / "history.csv"
    monkeypatch.setattr(scan, "HISTORY_FILE", f)
    pick = {"ticker": "AAA", "rank": 1, "score_rank": 1, "score": 50.0,
            "rsi2": 3.0, "dist_5dma_pct": -4.0, "dist_50dma_pct": 2.0,
            "dist_200dma_pct": 10.0, "last_close": 100.0,
            "target_price": 103.0, "stop_price": 95.0, "signal": "🟢",
            "freq_60d": 1}
    # An older row without the column, then a new one with it.
    scan.append_history([pick], "20261001",
                        datetime(2026, 10, 1, 20, 0, tzinfo=ET))
    scan.append_history([pick], "20261002",
                        datetime(2026, 10, 2, 20, 0, tzinfo=ET),
                        data_asof=date(2026, 10, 1))
    lines = f.read_text().splitlines()
    assert lines[0].endswith(",data_asof")
    assert lines[1].endswith(",")  # blank, not "nan"
    assert lines[2].endswith(",20261001")  # not "20261001.0"


# ------------------------------------------------ signal bar / outcomes

def closes_series(values, end="2026-10-02"):
    return pd.Series(values, index=pd.bdate_range(end=end, periods=len(values)),
                     dtype=float)


RUN = pd.Timestamp("2026-10-03 00:02", tz="UTC")  # 10-02 20:02 ET


def test_signal_bar_prefers_data_asof():
    c = closes_series([90.0, 100.0, 110.0])  # 09-30, 10-01, 10-02
    assert scan.signal_bar(c, RUN, 110.0, data_asof=20261001) == \
        pd.Timestamp("2026-10-01")


def test_signal_bar_matches_lagged_close_to_the_prior_bar():
    c = closes_series([90.0, 100.0, 100.8])  # a quiet 0.8% day
    # The old 2%-threshold rule kept 10-02 here.
    assert scan.signal_bar(c, RUN, 100.0) == pd.Timestamp("2026-10-01")
    assert scan.signal_bar(c, RUN, 100.8) == pd.Timestamp("2026-10-02")


def test_lagged_row_is_watched_from_the_session_after_its_bar():
    # Signal read 10-01's close (100) under run day 10-02. On 10-02 the name
    # bounced through its 103 target; the old resolver entered at 100 but
    # started watching 10-05, missing the touch.
    row = {"run_id": "20261002", "run_date": RUN, "ticker": "AAA",
           "target_price": 103.0, "stop_price": 95.0, "last_close": 100.0,
           "signal": "🟢"}
    idx = pd.bdate_range("2026-09-30", periods=9)  # 09-30 .. 10-12
    highs = [100, 100.5, 104, 102, 102, 102, 102, 102, 102]
    lows = [98, 99.5, 100, 99, 99, 99, 99, 99, 99]
    closes = [99, 100, 103.5, 101, 101, 101, 101, 101, 101]
    bars = pd.DataFrame({("AAA", "High"): highs, ("AAA", "Low"): lows,
                         ("AAA", "Close"): closes}, index=idx).astype(float)
    hist = pd.DataFrame([row])
    o = scan.resolve_outcomes(hist, bars, 5, full=True)[0]
    assert o["outcome"] == "WON" and o["days_to_resolve"] == 1
    assert o["result_pct"] == 3.0


def test_signal_bar_ignores_later_dividend_readjustment():
    seen = [103, 102.5, 102, 101.5, 101.01, 100.0]  # 09-25 .. 10-02
    c = closes_series([v * 0.99 for v in seen])
    dist = round((100.0 / (sum(seen[1:]) / 5) - 1) * 100, 2)
    assert scan.signal_bar(c, RUN, 100.0, dist_5dma_pct=dist) == \
        pd.Timestamp("2026-10-02")
    # Without dist, the re-adjusted prior bar would have won.
    assert scan.signal_bar(c, RUN, 100.0) == pd.Timestamp("2026-10-01")


def test_resolver_rescales_target_after_a_dividend():
    # Signal on 10-01 at 100 (target 103, stop 95). A 2% dividend since has
    # rescaled 10-01 and earlier by 0.98; the bounce to 101 adjusted (=103
    # as-seen) is a touch, not a miss.
    run = pd.Timestamp("2026-10-02 00:02", tz="UTC")  # 10-01 20:02 ET
    row = {"run_id": "20261001", "run_date": run, "ticker": "AAA",
           "target_price": 103.0, "stop_price": 95.0, "last_close": 100.0,
           "dist_5dma_pct": None, "signal": "🟢"}
    idx = pd.bdate_range("2026-09-30", periods=7)
    closes = [97.0, 98.0, 100.5, 100.5, 100.5, 100.5, 100.5]
    highs = [97.5, 98.5, 101.0, 101, 101, 101, 101]
    lows = [96.5, 97.5, 99.0, 99, 99, 99, 99]
    bars = pd.DataFrame({("AAA", "High"): highs, ("AAA", "Low"): lows,
                         ("AAA", "Close"): closes}, index=idx).astype(float)
    o = scan.resolve_outcomes(pd.DataFrame([row]), bars, 5, full=True)[0]
    assert o["outcome"] == "WON" and o["days_to_resolve"] == 1
    assert o["result_pct"] == 3.0  # same as the as-seen trade
