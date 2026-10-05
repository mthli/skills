"""Tests for the daily-bar freshness check (ensure_latest_session).

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




def test_banner_names_the_rebuild_and_the_missing_volume():
    fr = {"expected": SESSION, "asof": SESSION,
          "repaired": ["AAA", "BBB"], "missing": []}
    line = scan.render_data_freshness(fr)
    assert line.startswith("**Data**: daily bars through 2026-10-02")
    assert "2 names" in line and "no 🚀" in line


def test_with_session_close_appends_the_rebuilt_index_close(monkeypatch):
    spy = pd.Series([500.0, 501.0], index=pd.bdate_range(end="2026-10-01", periods=2))
    monkeypatch.setattr(scan, "session_bars_from_intraday",
                        lambda tickers, session: {"SPY": {"Close": 505.0}})
    out = scan.with_session_close(spy, "SPY", SESSION)
    assert out.index[-1] == pd.Timestamp(SESSION) and out.iloc[-1] == 505.0
    assert len(spy) == 2  # the caller's series is untouched
    assert scan.with_session_close(out, "SPY", SESSION) is out  # already there


def test_recent_breakout_ignores_a_day_with_unknown_volume():
    idx = pd.bdate_range(end="2026-10-02", periods=30)
    volume = pd.Series([1e6] * 29 + [np.nan], index=idx)
    close = pd.Series(np.linspace(90, 100, 30), index=idx)
    # Before: NaN volume made vol_ratio NaN, `NaN < 1.7` is False, and the
    # day came back as a confirmed breakout.
    assert scan.detect_recent_breakout(close, volume, lookback_days=1) is None


def test_recent_breakout_skips_an_unknown_volume_day_but_sees_older_ones():
    n = scan.RECENT_BREAKOUT_MIN_BASE_BARS + 5
    idx = pd.bdate_range(end="2026-10-02", periods=n)
    close = pd.Series([100.0] * (n - 2) + [110.0, 112.0], index=idx)
    volume = pd.Series([1e6] * (n - 2) + [3e6, np.nan], index=idx)
    rb = scan.detect_recent_breakout(close, volume, lookback_days=3)
    assert rb is not None and rb["days_since_breakout"] == 2
    assert rb["breakout_vol_ratio"] == 3.0


def test_history_records_data_asof_without_float_noise(tmp_path, monkeypatch):
    f = tmp_path / "history.csv"
    monkeypatch.setattr(scan, "HISTORY_FILE", f)
    pick = {"ticker": "AAA", "rank": 1, "score_rank": 1, "base_score": 60.0}
    scan.append_history([pick], "20261001",
                        datetime(2026, 10, 1, 20, 0, tzinfo=ET))
    scan.append_history([pick], "20261002",
                        datetime(2026, 10, 2, 20, 0, tzinfo=ET),
                        data_asof=date(2026, 10, 1))
    lines = f.read_text().splitlines()
    assert lines[0].endswith(",data_asof")
    assert lines[1].endswith(",")  # blank, not "nan"
    assert lines[2].endswith(",20261001")  # not "20261001.0"
