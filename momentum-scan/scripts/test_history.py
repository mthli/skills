"""Tests for append_history's same-ET-day upsert behavior.

Run from the skill root via:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy' \
      --with 'pytest' pytest scripts/
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

import scan


def _pick(ticker: str, rank: int) -> dict:
    return {
        "ticker": ticker, "rank": rank, "score": 1.0,
        "return_pct": 50.0, "max_dd_pct": -10.0,
        "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }


@pytest.fixture
def history_file(tmp_path, monkeypatch):
    f = tmp_path / "history.csv"
    monkeypatch.setattr(scan, "HISTORY_FILE", f)
    return f


def test_first_write_creates_file(history_file):
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "AAPL"


def test_same_et_day_overwrites(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)  # 16:00 ET 5/12
    scan.append_history([_pick("AAPL", 1), _pick("MSFT", 2)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)  # 18:00 ET 5/12
    scan.append_history([_pick("NVDA", 1)], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_different_et_day_appends(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("MSFT", 1)], "20260513", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 2
    assert set(df["ticker"]) == {"AAPL", "MSFT"}


def test_utc_late_night_still_same_et_day(history_file):
    # 03:00 UTC May 13 = 23:00 EDT May 12 — still ET date 5/12
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)  # 16:00 ET 5/12
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 13, 3, 0, 0, tzinfo=timezone.utc)   # 23:00 ET 5/12
    scan.append_history([_pick("NVDA", 1)], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1, "both runs share ET date 2026-05-12 despite straddling UTC midnight"
    assert df.iloc[0]["ticker"] == "NVDA"


def test_empty_picks_does_not_wipe(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)
    scan.append_history([], "20260512", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1, "empty picks should not wipe existing rows"
    assert df.iloc[0]["ticker"] == "AAPL"


def test_allow_same_day_appends(history_file):
    run1 = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run1)
    run2 = datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("NVDA", 1)], "20260512", run2,
                        allow_same_day=True)
    df = pd.read_csv(history_file)
    assert len(df) == 2
    assert set(df["ticker"]) == {"AAPL", "NVDA"}


def test_dst_spring_forward(history_file):
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT. Both runs below are 2026-03-08 ET.
    run1 = datetime(2026, 3, 8, 5, 0, 0, tzinfo=timezone.utc)   # 00:00 EST
    scan.append_history([_pick("AAPL", 1)], "20260308", run1)
    run2 = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc)   # 03:00 EDT
    scan.append_history([_pick("NVDA", 1)], "20260308", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_dst_fall_back(history_file):
    # 2026-11-01: 02:00 EDT rolls back to 01:00 EST. Both occurrences are 11/1 ET.
    run1 = datetime(2026, 11, 1, 5, 30, 0, tzinfo=timezone.utc)  # 01:30 EDT (1st)
    scan.append_history([_pick("AAPL", 1)], "20261101", run1)
    run2 = datetime(2026, 11, 1, 6, 30, 0, tzinfo=timezone.utc)  # 01:30 EST (2nd)
    scan.append_history([_pick("NVDA", 1)], "20261101", run2)
    df = pd.read_csv(history_file)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"


def test_atomic_write_leaves_no_tmp(history_file):
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("AAPL", 1)], "20260512", run)
    tmp = history_file.with_suffix(".csv.tmp")
    assert not tmp.exists(), "tmp file should be renamed away after successful write"


def test_make_run_id_default_is_et_date(history_file):
    # 03:00 UTC on 5/13 = 23:00 EDT on 5/12 → ET date 2026-05-12
    now = datetime(2026, 5, 13, 3, 0, 0, tzinfo=timezone.utc)
    assert scan.make_run_id(now) == "20260512"


def test_make_run_id_allow_same_day_is_second_precision(history_file):
    now = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    assert scan.make_run_id(now, allow_same_day=True) == "20260512T200000Z"


def test_history_sorted_after_backfill(history_file):
    # Write in reverse chronological order — file should still come out sorted.
    run_late = datetime(2026, 5, 13, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("LATE", 1)], "20260513", run_late)
    run_early = datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([_pick("EARLY", 1)], "20260511", run_early)
    df = pd.read_csv(history_file)
    dates = pd.to_datetime(df["run_date"], utc=True).tolist()
    assert dates == sorted(dates), "history.csv rows should be in chronological order"


def test_clear_history_removes_tmp(history_file):
    # Simulate a crashed atomic write leaving a stale .tmp alongside the main file
    history_file.write_text("dummy\n")
    tmp = history_file.with_suffix(".csv.tmp")
    tmp.write_text("partial\n")

    scan.clear_history()

    assert not history_file.exists(), "main history file should be gone"
    assert not tmp.exists(), "stale .tmp should also be gone"


def test_clear_history_safe_when_nothing_exists(history_file):
    # No files present — should not raise
    scan.clear_history()
