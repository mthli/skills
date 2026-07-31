"""Unit tests for backtest_outcomes.py's pure logic: episode construction
from history.csv (grouping, dedup, top-N membership, both censoring edges,
re-entry flags, attribute parsing), the entry-quality tiering, and the
bar-lookup helper. No network, no prices.

Run:
  uv run --with 'pandas>=2' --with 'numpy>=1.24,<3' --with 'pytest' \
    pytest test_backtest_outcomes.py
"""

import pandas as pd
import pytest

from backtest_outcomes import entry_tier, load_episodes, pos_of

HDR = ("run_id,run_date,ticker,rank,score_rank,score,return_pct,max_dd_pct,"
       "ann_vol_pct,from_high_pct,close,vol_ratio_20d,dollar_vol_20d_m,"
       "dist_days_25d")

# Five run-days spanning a weekend (Jul 3-4 2026 = Fri/Sat; 06 = Monday).
DAYS = ["20260701", "20260702", "20260703", "20260706", "20260707"]


def row(run_day, ticker, rank, score="10", close="", vol_ratio="",
        dist_days=""):
    return (f"{run_day},2026-07-01T00:00:00+00:00,{ticker},{rank},{rank},"
            f"{score},50.0,-5.0,30.0,0.0,{close},{vol_ratio},100.0,"
            f"{dist_days}")


@pytest.fixture
def history(tmp_path):
    def write(rows):
        p = tmp_path / "history.csv"
        # FILL anchors every run-day so run-day gaps in a test ticker's
        # appearances are real gaps, not missing scan days.
        anchors = [row(d, "FILL", 1) for d in DAYS]
        p.write_text("\n".join([HDR] + anchors + rows) + "\n")
        return p
    return write


def eps_of(episodes, ticker):
    return [e for e in episodes if e.ticker == ticker]


def test_consecutive_days_form_one_episode(history):
    eps, run_days = load_episodes(
        history([row(DAYS[1], "AAA", 5),
                 row(DAYS[2], "AAA", 3),
                 row(DAYS[3], "AAA", 7)]), top_n=30)
    assert run_days == DAYS
    (e,) = eps_of(eps, "AAA")
    assert (e.start_day, e.last_day, e.dropout_day) == (
        DAYS[1], DAYS[3], DAYS[4])
    assert e.tenure == 3
    assert e.peak_rank == 3
    assert e.entry_rank == 5
    assert not e.left_censored
    assert not e.reentry


def test_gap_splits_episodes_and_flags_reentry(history):
    eps, _ = load_episodes(
        history([row(DAYS[0], "BBB", 2),
                 row(DAYS[1], "BBB", 4),
                 row(DAYS[3], "BBB", 9),
                 row(DAYS[4], "BBB", 6)]), top_n=30)
    first, second = eps_of(eps, "BBB")
    assert first.dropout_day == DAYS[2]
    assert first.tenure == 2
    assert first.left_censored          # starts on history's first run-day
    assert not first.reentry
    assert second.start_day == DAYS[3]
    assert second.reentry
    assert not second.left_censored
    assert second.dropout_day is None   # still listed on the last run-day


def test_rank_above_top_n_breaks_membership(history):
    # Day 1 at rank 31 is recorded in history (kept pick) but out of the
    # displayed top-30 — the episode must break there, and the rank-31 day
    # must still anchor the run-day grid.
    eps, run_days = load_episodes(
        history([row(DAYS[0], "CCC", 10),
                 row(DAYS[1], "CCC", 31),
                 row(DAYS[2], "CCC", 8)]), top_n=30)
    assert run_days == DAYS
    first, second = eps_of(eps, "CCC")
    assert first.dropout_day == DAYS[1]
    assert second.start_day == DAYS[2]
    assert second.dropout_day == DAYS[3]
    # With a wider cutoff the same rows are one continuous episode.
    eps50, _ = load_episodes(
        history([row(DAYS[0], "CCC", 10),
                 row(DAYS[1], "CCC", 31),
                 row(DAYS[2], "CCC", 8)]), top_n=50)
    (e,) = eps_of(eps50, "CCC")
    assert e.tenure == 3 and e.peak_rank == 8


def test_same_day_duplicate_keeps_last_row(history):
    eps, _ = load_episodes(
        history([row(DAYS[1], "DDD", 20, vol_ratio="0.5"),
                 row(DAYS[1], "DDD", 12, vol_ratio="2.0")]), top_n=30)
    (e,) = eps_of(eps, "DDD")
    assert e.entry_rank == 12
    assert e.entry_vol_ratio == 2.0


def test_entry_attributes_parse_and_default_to_none(history):
    eps, _ = load_episodes(
        history([row(DAYS[1], "EEE", 5, score="13.75", close="101.5",
                     vol_ratio="1.34", dist_days="3"),
                 row(DAYS[1], "FFF", 6)]), top_n=30)
    (e,) = eps_of(eps, "EEE")
    assert (e.entry_score, e.entry_close_rec,
            e.entry_vol_ratio, e.entry_dist_days) == (13.75, 101.5, 1.34, 3.0)
    (f,) = eps_of(eps, "FFF")
    assert f.entry_close_rec is None
    assert f.entry_vol_ratio is None
    assert f.entry_dist_days is None


def test_entry_tier_thresholds_match_scan():
    assert entry_tier(1.5, 1) == "🟢 surge+clean"
    assert entry_tier(2.0, 3) == "🔵 surge"
    assert entry_tier(0.79, 0) == "🟠 quiet drift-in"
    assert entry_tier(0.8, 2) == "⚪ neutral"
    assert entry_tier(1.49, 0) == "⚪ neutral"
    assert entry_tier(None, 1) == "n/a (fields missing)"
    assert entry_tier(2.0, None) == "n/a (fields missing)"


def test_pos_of_exact_hole_and_gap():
    idx = pd.DatetimeIndex(["2026-07-01", "2026-07-02", "2026-07-06",
                            "2026-07-20"])
    bars = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    assert pos_of(bars, pd.Timestamp("2026-07-02")) == 1
    # Weekend/holiday hole: nearest prior bar within 5 calendar days.
    assert pos_of(bars, pd.Timestamp("2026-07-05")) == 1
    # Before the series starts.
    assert pos_of(bars, pd.Timestamp("2026-06-30")) is None
    # Data gap wider than 5 calendar days: refuse rather than misprice.
    assert pos_of(bars, pd.Timestamp("2026-07-15")) is None
