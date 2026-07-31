"""Pure-logic tests for backtest_outcomes.py: history parsing with
day-of-spell chains, canonical / gap-aware / next-open resolution
(WON / LOST / EXPIRED / open / gap_skip / unit_mismatch), and the
aggregation helpers. No network, no prices.

Run (from this directory):
  uv run --with 'pandas>=2' --with 'numpy>=1.24,<3' --with pytest \
    pytest test_backtest_outcomes.py
"""
import pandas as pd
import pytest

from backtest_outcomes import (Outcome, Signal, agg, bucket, load_signals,
                               resolve_signal)

HDR = ("run_id,run_date,ticker,rank,score_rank,score,rsi2,dist_5dma_pct,"
       "dist_50dma_pct,dist_200dma_pct,last_close,target_price,stop_price,"
       "signal,freq_60d")


def _row(run_day, ticker, rank="1", stop="95.0"):
    # 21:00 UTC = 17:00 ET — the post-close automated run convention
    return (f"{run_day},2026-07-01T21:00:00+00:00,{ticker},{rank},{rank},"
            f"50.0,3.0,-4.0,2.0,10.0,100.0,103.0,{stop},🟢,1")


def _write_history(tmp_path, lines):
    p = tmp_path / "history.csv"
    p.write_text("\n".join([HDR] + lines) + "\n")
    return p


# ---------------------------------------------------------------- loading

def test_load_signals_day_of_spell_chains(tmp_path):
    p = _write_history(tmp_path, [
        _row("20260727", "AAA"),
        _row("20260728", "AAA"),
        _row("20260729", "BBB"),      # AAA absent → spell broken
        _row("20260730", "AAA"),
    ])
    signals, run_days = load_signals(p)
    assert run_days == ["20260727", "20260728", "20260729", "20260730"]
    aaa = sorted((s for s in signals if s.ticker == "AAA"),
                 key=lambda s: s.run_day)
    assert [s.day_of_spell for s in aaa] == [1, 2, 1]
    assert all(s.day_of_spell == 1 for s in signals if s.ticker == "BBB")


def test_load_signals_parses_fields(tmp_path):
    p = _write_history(tmp_path, [_row("20260727", "AAA", stop="")])
    (s,), _ = load_signals(p)
    assert s.stop is None                       # empty stop → no stop order
    assert s.et_date == pd.Timestamp("2026-07-01")  # 21:00 UTC = same ET day
    assert s.target == 103.0 and s.last_close == 100.0 and s.signal == "🟢"


# ---------------------------------------------------------------- resolving

def _sig(**over):
    base = dict(run_day="20260706", et_date=pd.Timestamp("2026-07-06"),
                ticker="AAA", rank=1, score=50.0, rsi2=3.0, dist_5dma=-4.0,
                dist_50dma=2.0, dist_200dma=10.0, last_close=100.0,
                target=103.0, stop=95.0, signal="🟢", freq_60d=1)
    base.update(over)
    return Signal(**base)


def _bars(open_, high, low, close, start="2026-07-06"):
    idx = pd.bdate_range(start, periods=len(open_))
    return pd.DataFrame({"Open": [float(v) for v in open_],
                         "High": [float(v) for v in high],
                         "Low": [float(v) for v in low],
                         "Close": [float(v) for v in close]}, index=idx)


def test_resolve_won_canonical_and_gap_fill():
    # signal day + 5 post days; day 2 gaps up through the target
    bars = _bars([100, 100, 104, 104, 104, 104],
                 [100, 102, 105, 105, 105, 105],
                 [99, 100, 103, 103, 103, 103],
                 [100, 101, 104, 104, 104, 104])
    out, status = resolve_signal(_sig(), bars, window=5)
    assert status == "resolved" and out.outcome == "WON" and out.days == 2
    assert out.result == pytest.approx(3.0)      # limit fills at the target
    assert out.gap_result == pytest.approx(4.0)  # gap-up open fills better
    assert not out.ambiguous


def test_resolve_lost_gap_down_fills_worse():
    bars = _bars([100, 92, 100, 100, 100, 100],
                 [100, 96, 101, 101, 101, 101],
                 [99, 90, 99, 99, 99, 99],
                 [100, 93, 100, 100, 100, 100])
    out, status = resolve_signal(_sig(), bars, window=5)
    assert status == "resolved" and out.outcome == "LOST" and out.days == 1
    assert out.result == pytest.approx(-5.0)     # canonical: stop level
    assert out.gap_result == pytest.approx(-8.0)  # gap-down open, worse


def test_resolve_same_day_both_is_ambiguous_won():
    bars = _bars([100, 100, 100, 100, 100, 100],
                 [100, 104, 101, 101, 101, 101],
                 [99, 94, 99, 99, 99, 99],
                 [100, 100, 100, 100, 100, 100])
    out, status = resolve_signal(_sig(), bars, window=5)
    assert status == "resolved" and out.outcome == "WON"
    assert out.ambiguous  # numpy bool from the bar comparison — truthiness is the contract


def test_resolve_expired_carries_drift_and_fwd_ret():
    bars = _bars([100] * 6, [101] * 6, [99] * 6,
                 [100, 100, 100, 100, 100, 99])
    out, status = resolve_signal(_sig(), bars, window=5)
    assert status == "resolved" and out.outcome == "EXPIRED" and out.days == 5
    assert out.result == pytest.approx(-1.0)
    assert out.fwd_ret == pytest.approx(-1.0)


def test_resolve_open_and_no_bars():
    bars = _bars([100, 100, 100], [101, 101, 101], [99, 99, 99],
                 [100, 100, 100])
    assert resolve_signal(_sig(), bars, window=5)[1] == "open"
    assert resolve_signal(_sig(et_date=pd.Timestamp("2026-06-01")),
                          bars, window=5)[1] == "no_bars"


def test_resolve_unit_mismatch_rejected():
    bars = _bars([100] * 6, [101] * 6, [99] * 6, [100] * 6)
    out, status = resolve_signal(_sig(last_close=50.0), bars, window=5)
    assert out is None and status == "unit_mismatch"


def test_resolve_next_open_entry_and_gap_skip():
    # next open 100.5 (tradable): entry moves to the open
    bars = _bars([100, 100.5, 104, 104, 104, 104],
                 [100, 102, 105, 105, 105, 105],
                 [99, 100, 103, 103, 103, 103],
                 [100, 101, 104, 104, 104, 104])
    out, status = resolve_signal(_sig(), bars, window=5, entry_mode="next-open")
    assert status == "resolved" and out.outcome == "WON"
    assert out.result == pytest.approx((103.0 / 100.5 - 1) * 100)
    # next open already past the target → untradable overnight bounce
    gap = _bars([100, 103.5, 104, 104, 104, 104],
                [100, 105, 105, 105, 105, 105],
                [99, 103, 103, 103, 103, 103],
                [100, 104, 104, 104, 104, 104])
    assert resolve_signal(_sig(), gap, window=5,
                          entry_mode="next-open")[1] == "gap_skip"


# ---------------------------------------------------------------- reporting

def test_agg_math():
    outs = [Outcome(_sig(), "WON", 1, 3.0, 3.5, False, 4.0),
            Outcome(_sig(), "WON", 3, 3.0, 3.0, False, 2.0),
            Outcome(_sig(), "LOST", 2, -5.0, -8.0, False, -6.0),
            Outcome(_sig(), "EXPIRED", 5, -1.0, -1.0, False, -1.0)]
    a = agg(outs)
    assert a["n"] == 4 and a["dec"] == 3
    assert a["win"] == pytest.approx(100 * 2 / 3)
    assert a["expired"] == pytest.approx(25.0)
    assert a["days_to_t"] == pytest.approx(2.0)
    assert a["exp"] == pytest.approx((3.0 + 3.0 - 5.0 - 1.0) / 4)
    assert a["gap_exp"] == pytest.approx((3.5 + 3.0 - 8.0 - 1.0) / 4)


def test_agg_empty_and_no_decisive():
    assert agg([])["win"] is None
    a = agg([Outcome(_sig(), "EXPIRED", 5, -1.0, -1.0, False, None)])
    assert a["win"] is None and a["expired"] == 100.0 and a["fwd"] is None


def test_bucket_bounds_are_half_open():
    outs = [Outcome(_sig(score=s), "WON", 1, 1.0, 1.0, False, None)
            for s in (10.0, 40.0, 55.0)]
    groups = dict(bucket(outs, lambda s: s.score,
                         [("lo", 0, 40), ("mid", 40, 55), ("hi", 55, 999)]))
    assert [o.sig.score for o in groups["lo"]] == [10.0]
    assert [o.sig.score for o in groups["mid"]] == [40.0]   # 40 goes up, not down
    assert [o.sig.score for o in groups["hi"]] == [55.0]
