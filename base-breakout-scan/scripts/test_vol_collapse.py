"""Tests for the vol-collapse filter on base-breakout-scan.

The filter excludes acquisition targets that look like perfect bases (tight
width, vol dryup, BB squeeze, MAs aligned) but are actually locked at a
cash deal price. Without this filter, base-breakout-scan flags MASI-style
mergers as #1 breakout candidates.

Also covers the score_rank persistence semantics that pair with the filter.

Run from the skill root via:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
      --with 'pytest' pytest scripts/
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import scan


def _series_from_returns(daily_returns: list[float],
                         start_price: float = 100.0) -> pd.Series:
    """Build a price series from a daily-return list. bdate_range index so
    the series has a sensible date axis for debugging."""
    prices = [start_price]
    for r in daily_returns:
        prices.append(prices[-1] * (1 + r))
    dates = pd.bdate_range("2026-01-01", periods=len(prices))
    return pd.Series(prices, index=dates)


def _pick(ticker: str, rank: int = 1, base_score: float = 50.0) -> dict:
    """Mirror what score_tickers attaches: rank, score_rank, base_score, plus
    the base-detail fields the renderer / history uses."""
    return {
        "ticker": ticker, "rank": rank, "score_rank": rank,
        "base_score": base_score, "base_weeks": 8.0, "width_pct": 5.0,
        "bb_pctile": 5.0, "vol_dryup_ratio": 0.7,
        "rs_slope_pct_per_wk": 0.5, "to_pivot_pct": -1.0,
        "pivot_price": 100.0, "signal": "📊",
    }


# ---- compute_vol_halves ----------------------------------------------------


def test_vol_halves_too_short_returns_none():
    series = _series_from_returns([0.01] * 15)
    assert scan.compute_vol_halves(series) is None


def test_vol_halves_first_half_dominant():
    # MASI-like pattern: noisy first half, locked second half.
    first = [0.05 if i % 2 == 0 else -0.05 for i in range(30)]
    second = [0.00001] * 30
    series = _series_from_returns(first + second)
    v1, v2 = scan.compute_vol_halves(series)
    assert v1 > 50
    assert v2 < 1
    assert v2 / v1 < 0.05


# ---- filter_vol_collapse ---------------------------------------------------


def _closes_with(returns_by_ticker: dict[str, list[float]]) -> dict[str, pd.Series]:
    return {t: _series_from_returns(r) for t, r in returns_by_ticker.items()}


def test_filter_disabled_returns_all_picks_unchanged():
    picks = [_pick("A", 1), _pick("B", 2)]
    closes = _closes_with({"A": [0.01] * 60, "B": [0.01] * 60})
    kept, excluded = scan.filter_vol_collapse(
        picks, closes, lookback_months=3, ratio_threshold=0)
    assert kept is picks
    assert excluded == []


def test_filter_excludes_acquisition_target_keeps_real_base():
    # ACQ: MASI-like — gap-then-pin. v1 huge (gap), v2 tiny (locked).
    # BASE: a real base — modest noise throughout (no collapse).
    rng = np.random.RandomState(0)
    acq_returns = (
        list(rng.normal(0.005, 0.04, 30))  # pre-deal trading
        + [0.00001] * 30                    # post-deal pin
    )
    # Real base: ~12% annualized vol throughout, no collapse.
    base_returns = list(np.random.RandomState(1).normal(0.001, 0.008, 60))
    closes = _closes_with({"ACQ": acq_returns, "BASE": base_returns})
    picks = [_pick("ACQ", 1, base_score=88.0),
             _pick("BASE", 2, base_score=60.0)]
    kept, excluded = scan.filter_vol_collapse(
        picks, closes, lookback_months=3, ratio_threshold=0.2)

    assert len(excluded) == 1
    assert excluded[0]["ticker"] == "ACQ"
    assert excluded[0]["vol_ratio"] < 0.2
    assert excluded[0]["rank"] is None
    assert excluded[0]["pre_filter_rank"] == 1
    assert excluded[0]["score_rank"] == 1  # immutable

    assert len(kept) == 1
    assert kept[0]["ticker"] == "BASE"
    # Kept pick is re-ranked to 1 (display); score_rank stays at 2.
    assert kept[0]["rank"] == 1
    assert kept[0]["score_rank"] == 2


def test_filter_first_half_below_floor_kept():
    # Both halves at very low vol → first-half below 5% floor → not flagged.
    flat = [0.0001] * 60
    closes = _closes_with({"FLAT": flat})
    picks = [_pick("FLAT", 1)]
    kept, excluded = scan.filter_vol_collapse(
        picks, closes, lookback_months=3, ratio_threshold=0.2)
    assert excluded == []
    assert len(kept) == 1


def test_filter_missing_ticker_kept_silently():
    closes = _closes_with({"A": [0.01] * 60})
    picks = [_pick("MISSING", 1)]
    kept, excluded = scan.filter_vol_collapse(
        picks, closes, lookback_months=3, ratio_threshold=0.2)
    assert excluded == []
    assert kept[0]["ticker"] == "MISSING"


def test_filter_renumbers_kept_contiguously():
    rng = np.random.RandomState(0)
    normal_a = list(rng.normal(0.005, 0.02, 60))
    collapsed_b = (
        list(np.random.RandomState(1).normal(0.005, 0.04, 30))
        + [0.00001] * 30
    )
    normal_c = list(rng.normal(0.005, 0.02, 60))
    closes = _closes_with({"A": normal_a, "B": collapsed_b, "C": normal_c})
    picks = [_pick("A", 1, base_score=80),
             _pick("B", 2, base_score=70),
             _pick("C", 3, base_score=60)]
    kept, excluded = scan.filter_vol_collapse(
        picks, closes, lookback_months=3, ratio_threshold=0.2)
    assert [p["ticker"] for p in kept] == ["A", "C"]
    assert [p["rank"] for p in kept] == [1, 2]
    assert [p["score_rank"] for p in kept] == [1, 3]
    assert excluded[0]["pre_filter_rank"] == 2


# ---- argparse validator ----------------------------------------------------


def test_argparse_ratio_validator_rejects_above_one():
    ap = scan.build_argparser()
    with pytest.raises(SystemExit):
        ap.parse_args(["--vol-collapse-ratio", "5"])


def test_argparse_ratio_validator_accepts_one_and_below():
    ap = scan.build_argparser()
    assert ap.parse_args(["--vol-collapse-ratio", "1.0"]).vol_collapse_ratio == 1.0
    assert ap.parse_args(["--vol-collapse-ratio", "0.2"]).vol_collapse_ratio == 0.2
    assert ap.parse_args(["--vol-collapse-ratio", "0"]).vol_collapse_ratio == 0.0
    assert ap.parse_args(["--vol-collapse-ratio", "-1"]).vol_collapse_ratio == -1.0


# ---- enrich_with_persistence: score_rank semantics ------------------------


def _history_row(run_id: str, run_date: str, ticker: str, rank: int,
                 score_rank=None) -> dict:
    row = {
        "run_id": run_id,
        "run_date": pd.to_datetime(run_date, utc=True),
        "ticker": ticker, "rank": rank, "base_score": 50.0,
        "base_weeks": 8.0, "width_pct": 5.0, "bb_pctile": 5.0,
        "vol_dryup_ratio": 0.7, "rs_slope_pct_per_wk": 0.5,
        "to_pivot_pct": -1.0, "pivot_price": 100.0, "signal": "📊",
    }
    if score_rank is not None:
        row["score_rank"] = score_rank
    return row


def test_enrich_uses_score_rank_when_present():
    # Yesterday: ABC display=1, score_rank=2 (some name was excluded above).
    # Today: ABC still score_rank=2. rank_delta should be 0, NOT +1.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "ABC",
                     rank=1, score_rank=2),
    ])
    picks = [{"ticker": "ABC", "rank": 1, "score_rank": 2}]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    assert out[0]["rank_delta"] == 0
    assert out[0]["prev_rank"] == 1


def test_enrich_falls_back_to_rank_when_score_rank_missing():
    # Old-schema row: no score_rank column. Falls back to rank for delta.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "ABC", rank=2),
    ])
    assert "score_rank" not in history.columns
    picks = [{"ticker": "ABC", "rank": 1, "score_rank": 2}]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    assert out[0]["rank_delta"] == 0  # 2 (fallback rank) - 2 (cur score_rank)


def test_enrich_falls_back_per_row_on_nan_score_rank():
    # Mixed-schema: one row has score_rank, the other has NaN.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "OLD", rank=3),
        _history_row("D1", "2026-05-13T00:00:00Z", "NEW", rank=4, score_rank=4),
    ])
    history.loc[0, "score_rank"] = float("nan")
    picks = [
        {"ticker": "OLD", "rank": 1, "score_rank": 3},
        {"ticker": "NEW", "rank": 2, "score_rank": 4},
    ]
    out = scan.enrich_with_persistence(picks, history, current_run_id="D2")
    deltas = {p["ticker"]: p["rank_delta"] for p in out}
    assert deltas["OLD"] == 0
    assert deltas["NEW"] == 0


# ---- append_history schema migration ---------------------------------------


@pytest.fixture
def history_file(tmp_path, monkeypatch):
    f = tmp_path / "history.csv"
    monkeypatch.setattr(scan, "HISTORY_FILE", f)
    return f


def test_dropouts_with_reason_labels_excluded_as_vol_collapse():
    """A ticker excluded by vol-collapse this run, that was in prior run's
    top-N, should be labeled `vol_collapse` — not `faded`. Priority is
    higher than dedup (a vol-collapsed ticker isn't really 'deduped' even
    if it happens to be in SAME_ISSUER_PAIRS too)."""
    # Prior run had MASI at rank 1; current run excluded it.
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "MASI",
                     rank=1, score_rank=1)
        | {"pivot_price": 175.0, "signal": "🔥"},
    ])
    current_picks: set[str] = set()  # MASI not in current top-N
    excluded = {"MASI"}
    # current_prices reflects the locked price — close to pivot.
    current_prices = {"MASI": pd.Series([175.0] * 5)}
    drops = scan.dropouts_with_reason(
        history, current_picks, current_run_id="D2", top_n=30,
        current_prices=current_prices,
        excluded_tickers=excluded,
    )
    assert len(drops) == 1
    assert drops[0]["ticker"] == "MASI"
    assert drops[0]["reason"] == "vol_collapse"


def test_dropouts_with_reason_no_excluded_falls_back_to_other_reasons():
    """When excluded_tickers is None or empty, classification falls through
    to the existing reasons (faded/broke_out/broke_down/deduped)."""
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", "AAA",
                     rank=1, score_rank=1)
        | {"pivot_price": 100.0, "signal": "📊"},
    ])
    current_prices = {"AAA": pd.Series([100.5] * 5)}  # slightly above pivot
    drops = scan.dropouts_with_reason(
        history, current_picks=set(), current_run_id="D2",
        top_n=30, current_prices=current_prices,
        excluded_tickers=None,
    )
    assert drops[0]["reason"] == "faded"  # +0.5% is at the threshold edge


def test_dropouts_vol_collapse_takes_precedence_over_dedup():
    """Vol-collapse priority is higher than dedup. A ticker that's in both
    SAME_ISSUER_PAIRS *and* the excluded set should be labeled vol_collapse,
    not deduped. (Hypothetical — PBR-A getting acquired wouldn't realistically
    happen, but defensive coverage.)"""
    pbr, pbr_a = scan.SAME_ISSUER_PAIRS[0]
    history = pd.DataFrame([
        _history_row("D1", "2026-05-13T00:00:00Z", pbr_a,
                     rank=2, score_rank=2)
        | {"pivot_price": 20.0, "signal": "📊"},
    ])
    current_picks = {pbr}  # PBR still in current top-N
    excluded = {pbr_a}
    current_prices = {pbr_a: pd.Series([20.0] * 5)}
    drops = scan.dropouts_with_reason(
        history, current_picks, current_run_id="D2", top_n=30,
        current_prices=current_prices,
        excluded_tickers=excluded,
    )
    assert drops[0]["reason"] == "vol_collapse"


def test_single_ticker_pipeline_attaches_vol_collapse_warning(monkeypatch):
    """`--ticker` mode must populate `vol_collapse_warning` BEFORE any early
    return (TT-fail, no-base) — otherwise a failing ticker silently misses
    the warning. The check runs early in the pipeline, right after
    last_close is captured."""
    # Stub fetch_bars / extract_field with enough data to clear
    # MIN_BARS_REQUIRED (~220), then put the MASI-like gap-then-lock pattern
    # in the LAST 3 months — that's the window compute_vol_halves slices.
    rng = np.random.RandomState(0)
    pre = list(rng.normal(0.0, 0.02, 200))   # 200 bars of normal trading
    noisy = list(rng.normal(0.0, 0.05, 32))  # 32 bars of pre-deal noise (1st half of last 63)
    locked = [0.00001] * 31                   # 31 bars locked (2nd half of last 63)
    series = _series_from_returns(pre + noisy + locked)
    assert len(series) >= 220, f"need ≥ 220 bars, got {len(series)}"

    def fake_fetch_bars(tickers, history_months=14):
        # Return a MultiIndex DataFrame mimicking yf.download shape so
        # extract_field picks it up. Index is the trading-day range from
        # our synthetic series.
        idx = series.index
        cols = pd.MultiIndex.from_product([tickers, ["Close", "High", "Low",
                                                     "Volume"]])
        data = {}
        for t in tickers:
            data[(t, "Close")] = series.values
            data[(t, "High")] = series.values
            data[(t, "Low")] = series.values
            data[(t, "Volume")] = [1_000_000] * len(series)
        return pd.DataFrame(data, index=idx, columns=cols)

    monkeypatch.setattr(scan, "fetch_bars", fake_fetch_bars)
    # Avoid network for SPY too.
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *a, **kw: pd.DataFrame())
    # Sectors fetch — avoid network.
    monkeypatch.setattr(scan, "refresh_sectors",
                        lambda tickers, cache, max_workers=1: cache)

    args = scan.build_argparser().parse_args(["--ticker", "FAKE"])
    result = scan._run_single_ticker_pipeline(args)

    # Warning must be present even if other stages short-circuit.
    assert "vol_collapse_warning" in result, (
        f"vol_collapse_warning missing. Result keys: {list(result.keys())}")
    w = result["vol_collapse_warning"]
    assert w["vol_first_pct"] > 50  # noisy first half
    assert w["vol_second_pct"] < 1   # locked second half
    assert w["vol_ratio"] < 0.05


def test_main_integration_call_chain_in_source():
    """Belt-and-suspenders: confirm main() calls filter_vol_collapse and
    passes excluded_tickers to dropouts_with_reason. Catches a refactor that
    inlines or removes these calls."""
    import inspect
    try:
        src = inspect.getsource(scan.main)
    except (OSError, TypeError):
        pytest.skip("source unavailable for inspect.getsource(scan.main)")
    assert "filter_vol_collapse" in src, (
        "main() should call filter_vol_collapse on picks")
    assert "excluded_tickers=" in src, (
        "main() should pass excluded_tickers= to dropouts_with_reason")


def test_prune_non_trading_days_reindexes_old_schema_file(history_file, monkeypatch):
    """When prune drops rows from an old-schema file, the rewrite should
    normalize columns to canonical order (HISTORY_COLS prefix). Without the
    reindex I added, an old-schema file would stay old-schema, inconsistent
    with what append_history produces. Regression test for the prune path.
    """
    # Construct: one trading-day row, one weekend row. Old schema (no
    # score_rank). After prune the weekend row should be gone and the file
    # should have canonical column order (which would add score_rank as
    # a NaN column at the canonical position).
    sat = datetime(2026, 5, 9, 20, 0, 0, tzinfo=timezone.utc)   # Saturday
    fri = datetime(2026, 5, 8, 20, 0, 0, tzinfo=timezone.utc)   # Friday (trading)
    rows = pd.DataFrame([
        {
            "run_id": "20260508",
            "run_date": fri.isoformat(),
            "ticker": "AAPL", "rank": 1, "base_score": 60.0,
            "base_weeks": 8.0, "width_pct": 5.0, "bb_pctile": 5.0,
            "vol_dryup_ratio": 0.7, "rs_slope_pct_per_wk": 0.5,
            "to_pivot_pct": -1.0, "pivot_price": 100.0, "signal": "📊",
        },
        {
            "run_id": "20260509",
            "run_date": sat.isoformat(),
            "ticker": "AAPL", "rank": 1, "base_score": 60.0,
            "base_weeks": 8.0, "width_pct": 5.0, "bb_pctile": 5.0,
            "vol_dryup_ratio": 0.7, "rs_slope_pct_per_wk": 0.5,
            "to_pivot_pct": -1.0, "pivot_price": 100.0, "signal": "📊",
        },
    ])
    history_file.write_text(rows.to_csv(index=False))
    # Seed file is old-schema (no score_rank column).
    seeded = pd.read_csv(history_file)
    assert "score_rank" not in seeded.columns
    assert len(seeded) == 2

    rows_removed, run_ids_removed = scan.prune_non_trading_days()
    assert rows_removed == 1
    assert run_ids_removed == 1

    df = pd.read_csv(history_file)
    # Saturday row gone.
    assert len(df) == 1
    assert df.iloc[0]["run_id"] == 20260508
    # Column order normalized to canonical (score_rank inserted between
    # `rank` and `base_score`, even though no row has a value for it yet).
    assert df.columns.tolist()[:len(scan.HISTORY_COLS)] == scan.HISTORY_COLS


def test_check_vol_collapse_helper_returns_consistent_shape():
    """Pure helper signature: (triggered, v1, v2, ratio). Three exit cases:
    disabled, too-short, below-floor, normal. All return 4-tuple."""
    # Disabled
    assert scan._check_vol_collapse(pd.Series([100.0] * 100), 0) == (
        False, None, None, None)
    # Too short
    assert scan._check_vol_collapse(pd.Series([100.0] * 5), 0.2) == (
        False, None, None, None)
    # Below floor (low vol throughout)
    flat = _series_from_returns([0.0001] * 60)
    triggered, v1, v2, ratio = scan._check_vol_collapse(flat, 0.2)
    assert triggered is False
    assert v1 is not None and v1 < 5.0  # below the 5% floor
    assert ratio is None  # not meaningfully comparable

    # Normal case: collapsed pattern triggers
    rng = np.random.RandomState(0)
    collapsed = list(rng.normal(0.005, 0.04, 30)) + [0.00001] * 30
    series = _series_from_returns(collapsed)
    triggered, v1, v2, ratio = scan._check_vol_collapse(series, 0.2)
    assert triggered is True
    assert v1 > 50
    assert v2 < 1
    assert ratio is not None and ratio < 0.2


def test_append_history_migrates_old_schema_file(history_file):
    """Old-schema history.csv (no score_rank) + new write → output has
    score_rank in canonical position (after `rank`, not at the end)."""
    # Hand-write an old-schema seed (missing score_rank).
    old_rows = pd.DataFrame([
        {
            "run_id": "20260511",
            "run_date": datetime(2026, 5, 11, 20, 0, 0,
                                  tzinfo=timezone.utc).isoformat(),
            "ticker": "AAPL", "rank": 1, "base_score": 60.0,
            "base_weeks": 8.0, "width_pct": 5.0, "bb_pctile": 5.0,
            "vol_dryup_ratio": 0.7, "rs_slope_pct_per_wk": 0.5,
            "to_pivot_pct": -1.0, "pivot_price": 100.0, "signal": "📊",
        },
    ])
    history_file.write_text(old_rows.to_csv(index=False))
    seeded = pd.read_csv(history_file)
    assert "score_rank" not in seeded.columns

    # Append one new-schema row (has score_rank).
    new_pick = _pick("MSFT", 1, base_score=75.0)
    run = datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)
    scan.append_history([new_pick], "20260512", run)

    df = pd.read_csv(history_file)
    # score_rank column present, populated for new row, NaN for old.
    assert "score_rank" in df.columns
    aapl = df[df["ticker"] == "AAPL"].iloc[0]
    msft = df[df["ticker"] == "MSFT"].iloc[0]
    assert pd.isna(aapl["score_rank"])
    assert int(msft["score_rank"]) == 1
    # Column order: HISTORY_COLS prefix preserved.
    assert df.columns.tolist()[:len(scan.HISTORY_COLS)] == scan.HISTORY_COLS
