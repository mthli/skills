"""Tests for the vol-collapse filter (compute_vol_halves, filter_vol_collapse).

Also covers the rank-semantics interactions that depend on the filter:
- enrich_with_persistence fallback when history has mixed schema
- show_history_summary climber/dropper computation using score_rank
- the short-window stderr warning emitted from main()

Run from the skill root via:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
      --with 'pytest' pytest scripts/
"""
import numpy as np
import pandas as pd
import pytest

import scan


def _series_from_returns(daily_returns: list[float],
                         start_price: float = 100.0) -> pd.Series:
    """Build a price series from a daily-return list. Index is bdate_range
    so the series has a meaningful date axis (filter_vol_collapse uses
    `prices.tail()` which is order-based, but a real index aids debugging)."""
    prices = [start_price]
    for r in daily_returns:
        prices.append(prices[-1] * (1 + r))
    dates = pd.bdate_range("2026-01-01", periods=len(prices))
    return pd.Series(prices, index=dates)


def _pick(ticker: str, rank: int = 1, score: float = 5.0) -> dict:
    """Shape mirrors score_tickers output (with score_rank baked in)."""
    return {
        "ticker": ticker, "rank": rank, "score_rank": rank, "score": score,
        "return_pct": 50.0, "max_dd_pct": -10.0,
        "ann_vol_pct": 30.0, "from_high_pct": -1.0,
    }


# ---- compute_vol_halves --------------------------------------------------


def test_vol_halves_too_short_returns_none():
    # 15 returns is below 2 * MIN_RETURNS_PER_HALF (10) = 20
    series = _series_from_returns([0.01] * 15)
    assert scan.compute_vol_halves(series) is None


def test_vol_halves_balanced_low_vol():
    # Identical daily returns → both halves have std ~ 0 (one tick of
    # rounding noise but well under 1%)
    series = _series_from_returns([0.01] * 30)
    halves = scan.compute_vol_halves(series)
    assert halves is not None
    v1, v2 = halves
    assert v1 < 0.5 and v2 < 0.5


def test_vol_halves_first_half_dominant():
    # First half: noisy ±5% alternating. Second half: dead flat.
    first = [0.05 if i % 2 == 0 else -0.05 for i in range(30)]
    second = [0.00001] * 30
    series = _series_from_returns(first + second)
    v1, v2 = scan.compute_vol_halves(series)
    # First-half noise should yield ~80% annualized; second-half near zero.
    assert v1 > 50
    assert v2 < 1
    assert v2 / v1 < 0.05


def test_vol_halves_second_half_dominant():
    # Symmetric case: gap in the second half. Filter design implication:
    # `vol_collapse_ratio` should not trigger here (v2/v1 > 1).
    first = [0.001] * 30
    second = [0.05 if i % 2 == 0 else -0.05 for i in range(30)]
    series = _series_from_returns(first + second)
    v1, v2 = scan.compute_vol_halves(series)
    assert v2 > v1 * 10


# ---- filter_vol_collapse -------------------------------------------------


def _prices_with(returns_by_ticker: dict[str, list[float]]) -> pd.DataFrame:
    """Build a wide prices DataFrame where each column is built from its
    returns list. All columns share the same bdate_range index."""
    series_map = {t: _series_from_returns(r) for t, r in returns_by_ticker.items()}
    return pd.DataFrame(series_map)


def test_filter_disabled_returns_all_picks_unchanged():
    picks = [_pick("A", 1), _pick("B", 2)]
    prices = _prices_with({"A": [0.01] * 60, "B": [0.01] * 60})
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0)
    assert kept is picks  # short-circuit returns the same list
    assert excluded == []


def test_filter_excludes_collapsed_name_keeps_normal_name():
    # COLLAPSED: noisy first half, flat second half (the MASI-like pattern)
    # NORMAL: consistent noise throughout (typical momentum name)
    rng_normal = np.random.RandomState(42)
    rng_collapsed_first = np.random.RandomState(0)
    collapsed_returns = (
        list(rng_collapsed_first.normal(0.005, 0.04, 30))
        + [0.00001] * 30
    )
    normal_returns = list(rng_normal.normal(0.005, 0.02, 60))
    prices = _prices_with({
        "COLLAPSED": collapsed_returns,
        "NORMAL": normal_returns,
    })
    picks = [_pick("COLLAPSED", 1, score=10.0),
             _pick("NORMAL", 2, score=5.0)]
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0.2)

    assert len(excluded) == 1
    assert excluded[0]["ticker"] == "COLLAPSED"
    assert excluded[0]["vol_ratio"] < 0.2
    assert excluded[0]["rank"] is None  # cleared after exclusion
    assert excluded[0]["pre_filter_rank"] == 1  # preserved
    assert excluded[0]["score_rank"] == 1  # untouched

    assert len(kept) == 1
    assert kept[0]["ticker"] == "NORMAL"
    # Kept pick gets renumbered to rank=1, but score_rank stays at 2.
    assert kept[0]["rank"] == 1
    assert kept[0]["score_rank"] == 2


def test_filter_first_half_below_floor_kept():
    # Both halves have very low vol — first-half below 5% annualized floor.
    # Even if v2/v1 is < 0.2, the filter is too noise-prone to trust at
    # this size, so we keep the name.
    flat_returns = [0.0001] * 60
    prices = _prices_with({"FLAT": flat_returns})
    picks = [_pick("FLAT", 1)]
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0.2)
    assert excluded == []
    assert len(kept) == 1


def test_filter_gap_in_second_half_not_excluded():
    # Real failure mode: announcement near the end of the window puts the
    # gap in the second half, where it inflates v2 above v1. Filter does
    # NOT exclude — this is the documented limitation, not a bug.
    pre_deal = list(np.random.RandomState(0).normal(0.001, 0.02, 50))
    gap_and_pin = [0.30] + [0.00001] * 9  # +30% gap then 9 flat days
    prices = _prices_with({"LATE_DEAL": pre_deal + gap_and_pin})
    picks = [_pick("LATE_DEAL", 1)]
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0.2)
    assert excluded == []
    assert len(kept) == 1


def test_filter_renumbers_kept_picks_contiguously():
    # Three picks; middle one excluded. Surviving #1 stays #1, #3 becomes #2.
    rng = np.random.RandomState(0)
    normal_a = list(rng.normal(0.005, 0.02, 60))
    collapsed_b = (
        list(np.random.RandomState(1).normal(0.005, 0.04, 30))
        + [0.00001] * 30
    )
    normal_c = list(rng.normal(0.005, 0.02, 60))
    prices = _prices_with({"A": normal_a, "B": collapsed_b, "C": normal_c})
    picks = [_pick("A", 1, score=10.0),
             _pick("B", 2, score=8.0),
             _pick("C", 3, score=6.0)]
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0.2)

    assert [p["ticker"] for p in kept] == ["A", "C"]
    assert [p["rank"] for p in kept] == [1, 2]
    # score_rank reflects underlying score-based position pre-filter.
    assert [p["score_rank"] for p in kept] == [1, 3]
    assert [p["ticker"] for p in excluded] == ["B"]
    assert excluded[0]["pre_filter_rank"] == 2


def test_filter_ticker_not_in_prices_kept_silently():
    # Defensive: if a pick references a ticker missing from prices (shouldn't
    # happen given the pipeline, but log a regression if it ever does), the
    # filter keeps the pick rather than raising.
    prices = _prices_with({"A": [0.01] * 60})
    picks = [_pick("MISSING", 1)]
    kept, excluded = scan.filter_vol_collapse(
        picks, prices, window_months=3, ratio_threshold=0.2)
    assert excluded == []
    assert kept[0]["ticker"] == "MISSING"
    assert kept[0]["rank"] == 1


def test_filter_argparse_ratio_validator_rejects_above_one():
    # argparse exits the process on type-check failure; pytest captures it.
    ap = scan.build_argparser()
    with pytest.raises(SystemExit):
        ap.parse_args(["--vol-collapse-ratio", "5"])


def test_filter_argparse_ratio_validator_accepts_one_point_zero():
    ap = scan.build_argparser()
    args = ap.parse_args(["--vol-collapse-ratio", "1.0"])
    assert args.vol_collapse_ratio == 1.0


def test_filter_argparse_ratio_validator_accepts_zero_and_negative():
    ap = scan.build_argparser()
    assert ap.parse_args(["--vol-collapse-ratio", "0"]).vol_collapse_ratio == 0.0
    assert ap.parse_args(["--vol-collapse-ratio", "-1"]).vol_collapse_ratio == -1.0


# ---- enrich_with_persistence: mixed-schema fallback ----------------------


def _history_row(run_id: str, run_date: str, ticker: str, rank: int,
                 score_rank=None) -> dict:
    """Build a minimal history row. When score_rank is None, simulates the
    pre-upgrade schema where that column either didn't exist or was NaN."""
    row = {
        "run_id": run_id,
        "run_date": pd.to_datetime(run_date, utc=True),
        "ticker": ticker,
        "rank": rank,
        "score": 5.0,
        "return_pct": 50.0,
        "max_dd_pct": -10.0,
        "ann_vol_pct": 30.0,
        "from_high_pct": -1.0,
    }
    if score_rank is not None:
        row["score_rank"] = score_rank
    return row


def test_enrich_persistence_uses_score_rank_when_present():
    # Yesterday: NOK at display=1 but score_rank=2 (some name was excluded
    # above it). Today: same — NOK still score_rank=2. Delta should be 0,
    # NOT +1 (which is what raw rank would give: 1 - 1).
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "NOK", rank=1, score_rank=2),
    ])
    picks = [{"ticker": "NOK", "rank": 1, "score_rank": 2}]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    assert out[0]["rank_delta"] == 0
    assert out[0]["prev_rank"] == 1  # display rank from history


def test_enrich_persistence_falls_back_to_rank_when_score_rank_missing():
    # Old-schema history: no score_rank column at all. Should fall back
    # silently to rank for the delta arithmetic.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "NOK", rank=2),
    ])
    assert "score_rank" not in history.columns
    picks = [{"ticker": "NOK", "rank": 1, "score_rank": 2}]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    assert out[0]["rank_delta"] == 0  # 2 - 2 (current score_rank)


def test_enrich_persistence_falls_back_per_row_on_nan_score_rank():
    # Mixed-schema file: one row has score_rank, another has NaN. Fallback
    # should be per-row, not all-or-nothing.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "OLD", rank=3),
        _history_row("D1", "2026-05-13T00:00:00Z", "NEW", rank=4, score_rank=4),
    ])
    # Force a NaN in OLD's score_rank (simulating mid-upgrade state).
    history.loc[0, "score_rank"] = float("nan")
    picks = [
        {"ticker": "OLD", "rank": 1, "score_rank": 3},
        {"ticker": "NEW", "rank": 2, "score_rank": 4},
    ]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    deltas = {p["ticker"]: p["rank_delta"] for p in out}
    assert deltas["OLD"] == 0  # 3 (fallback to rank) - 3 (current score_rank)
    assert deltas["NEW"] == 0  # 4 (score_rank) - 4 (current score_rank)


# ---- show_history_summary: same fix applied --------------------------------


def _show_history_climber_lines(captured: str) -> list[str]:
    """Extract just the climber lines from a show_history_summary output.
    Each climber line has format `    TICKER #N → #M (+K)`."""
    in_climbers = False
    out = []
    for line in captured.splitlines():
        if "Biggest climbers" in line:
            in_climbers = True
            continue
        # Any non-indented header line ends the section.
        if in_climbers and line and not line.startswith("    "):
            break
        if in_climbers and "→" in line and "(+" in line:
            out.append(line)
    return out


def test_show_history_summary_uses_score_rank_for_delta(capsys):
    # Two runs. In D2, ticker A was excluded by filter, B moved up to display
    # rank 1 but score_rank is still 2. Without the fix, biggest climber
    # would show B as +1; with the fix, B has score-delta 0 → no climber.
    rows = [
        # D1: no filter active; rank == score_rank
        _history_row("D1", "2026-05-12T00:00:00Z", "A", rank=1, score_rank=1),
        _history_row("D1", "2026-05-12T00:00:00Z", "B", rank=2, score_rank=2),
        # D2: A excluded by filter (so not in history), B at display=1
        # but score_rank=2 (since A still scored higher, just got filtered)
        _history_row("D2", "2026-05-13T00:00:00Z", "B", rank=1, score_rank=2),
    ]
    history = pd.DataFrame(rows)
    scan.show_history_summary(history)
    captured = capsys.readouterr().out
    # With the fix, B's delta is 2 - 2 = 0, so no climber line for B.
    # Without the fix, B would have delta 2 - 1 = +1 → climber line.
    climbers = _show_history_climber_lines(captured)
    assert not any("B " in line for line in climbers), (
        f"B should NOT appear as climber when score_rank stays at 2 across "
        f"runs. Climber lines: {climbers}\nFull output:\n{captured}")


def test_show_history_summary_works_with_old_schema_no_score_rank_column(capsys):
    # Regression test for the duplicate-column bug: if the history DataFrame
    # has no score_rank column, the climber/dropper computation must still
    # work without KeyError. Falls back to display rank for delta.
    rows = [
        _history_row("D1", "2026-05-12T00:00:00Z", "X", rank=2),  # no score_rank
        _history_row("D1", "2026-05-12T00:00:00Z", "Y", rank=3),
        _history_row("D2", "2026-05-13T00:00:00Z", "X", rank=1),
        _history_row("D2", "2026-05-13T00:00:00Z", "Y", rank=4),
    ]
    history = pd.DataFrame(rows)
    assert "score_rank" not in history.columns
    # Must not raise (was a KeyError pre-fix due to duplicate-column rename).
    scan.show_history_summary(history)
    captured = capsys.readouterr().out
    # X rose 2 → 1 (delta +1, climber); Y fell 3 → 4 (delta -1, dropper).
    climbers = _show_history_climber_lines(captured)
    assert any("X " in line for line in climbers), (
        f"X should be a climber (rank 2 → 1). Climber lines: {climbers}\n"
        f"Output:\n{captured}")


# ---- Short-window warning helper ------------------------------------------


def test_short_window_warning_emitted_below_two_months():
    msg = scan._maybe_short_window_warning(1, 0.2)
    assert msg is not None
    assert "Warning" in msg
    assert "window-months 1" in msg


def test_short_window_warning_silent_when_filter_disabled():
    # Filter disabled (ratio = 0): no warning even at short window.
    assert scan._maybe_short_window_warning(1, 0) is None
    assert scan._maybe_short_window_warning(1, -0.5) is None


def test_short_window_warning_silent_at_two_months():
    # Threshold is strict <2; exactly 2 months passes without warning.
    assert scan._maybe_short_window_warning(2, 0.2) is None
    assert scan._maybe_short_window_warning(3, 0.2) is None


def test_short_window_warning_helper_is_what_main_uses():
    # Belt-and-suspenders: confirm main() invokes the helper rather than
    # re-implementing the predicate inline. This guards against the helper
    # drifting out of sync with main()'s actual behavior.
    import inspect
    try:
        main_src = inspect.getsource(scan.main)
    except (OSError, TypeError):
        # Source unavailable (e.g., running from compiled .pyc-only install).
        # Skip rather than fail — the assertion below is a sanity check, not
        # load-bearing for the warning's runtime behavior.
        pytest.skip("source unavailable for inspect.getsource(scan.main)")
    assert "_maybe_short_window_warning" in main_src, (
        "main() should call _maybe_short_window_warning, not inline the "
        "predicate.")
