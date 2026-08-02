"""Pure-logic tests for the snapback outcome ledger — no network.

Run: uv run --with 'pandas>=2' --with pytest pytest test_backtest_outcomes.py
"""
import csv
import json

import pandas as pd
import pytest

import backtest_outcomes as bo


def _bars(rows: list[tuple], dates: list[str]) -> pd.DataFrame:
    """rows: (open, high, low, close) per date."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame(
        {"Open": [r[0] for r in rows], "High": [r[1] for r in rows],
         "Low": [r[2] for r in rows], "Close": [r[3] for r in rows]}, index=idx)


def _flat(closes: list[float], dates: list[str], span: float = 1.0):
    """Bars whose High/Low straddle the close by ±span."""
    return _bars([(c, c + span, c - span, c) for c in closes], dates)


def _sig(**kw) -> bo.Signal:
    base = dict(packet_date="2026-07-30", ticker="AAA", rank=1, score=60.0,
                rsi2=2.0, dist_200dma=10.0, listing_age=1, freq_60d=0,
                sector="Technology", regime="RISK-ON", entry=100.0,
                stop=90.0, target=110.0, armed=True)
    base.update(kw)
    return bo.Signal(**base)


# --------------------------------------------------------------------------- #
# load_signals — packets are the history; day_of_packet counts the episode
# --------------------------------------------------------------------------- #
def _packet(date: str, kegs: list[dict], regime="RISK-ON") -> dict:
    return {"today": date, "regime": {"state": regime}, "kegs": kegs}


def _keg(ticker, **kw):
    base = dict(ticker=ticker, rank=1, score=60.0, rsi2=2.0,
                dist_200dma_pct=10.0, listing_age_runs=1, freq_60d=0,
                sector="Technology", latest_close=100.0, signal_day_low=90.0,
                mr_target=110.0, armed=True, sparks=[], ignited=False,
                quiet_warning=False, down_streak=3, ret_5d_pct=-6.0,
                down_gaps_5d=1, vol_ratio_5d_20d=1.3)
    base.update(kw)
    return base


def test_load_signals_counts_consecutive_packets(tmp_path):
    for d, tks in [("2026-07-28", ["RIDE", "GAPPY"]),
                   ("2026-07-29", ["RIDE"]),
                   ("2026-07-30", ["RIDE", "GAPPY", "NEW"])]:
        (tmp_path / f"{d}.json").write_text(
            json.dumps(_packet(d, [_keg(t) for t in tks])))

    signals, days = bo.load_signals(tmp_path)
    assert days == ["2026-07-28", "2026-07-29", "2026-07-30"]
    got = {(s.ticker, s.packet_date): s.day_of_packet for s in signals}
    assert got[("RIDE", "2026-07-30")] == 3      # three straight packets
    assert got[("GAPPY", "2026-07-30")] == 1     # missing 07-29 resets it
    assert got[("NEW", "2026-07-30")] == 1


def test_load_signals_skips_keg_without_entry_price(tmp_path):
    (tmp_path / "2026-07-30.json").write_text(json.dumps(_packet(
        "2026-07-30", [_keg("OK"), _keg("NOPX", latest_close=None,
                                        signal_close=None)])))
    signals, _ = bo.load_signals(tmp_path)
    assert [s.ticker for s in signals] == ["OK"]


def test_load_signals_falls_back_to_signal_close(tmp_path):
    (tmp_path / "2026-07-30.json").write_text(json.dumps(_packet(
        "2026-07-30", [_keg("FB", latest_close=None, signal_close=88.0)])))
    signals, _ = bo.load_signals(tmp_path)
    assert signals[0].entry == 88.0


def test_unreadable_packet_is_skipped_not_fatal(tmp_path):
    (tmp_path / "2026-07-29.json").write_text("{ not json")
    (tmp_path / "2026-07-30.json").write_text(
        json.dumps(_packet("2026-07-30", [_keg("OK")])))
    signals, days = bo.load_signals(tmp_path)
    assert [s.ticker for s in signals] == ["OK"] and days == ["2026-07-30"]


# --------------------------------------------------------------------------- #
# spark_kind — one dominant label, own print outranks a co-occurring verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kinds,expected", [
    ({"own_earnings", "sector_verdict", "marketwide_verdict"}, "own_earnings"),
    ({"sector_verdict", "marketwide_verdict"}, "sector_verdict"),
    ({"macro"}, "macro"),
    ({"marketwide_verdict"}, "unarmed"),
    (set(), "unarmed"),
])
def test_spark_kind_priority(kinds, expected):
    assert _sig(spark_kinds=kinds).spark_kind == expected


# --------------------------------------------------------------------------- #
# resolve_signal — the protocol's convention: entry/invalidation/target
# --------------------------------------------------------------------------- #
DATES = ["2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05",
         "2026-08-06"]


def test_target_hit_is_a_win_measured_at_the_limit():
    bars = _bars([(100, 101, 99, 100),      # anchor
                  (100, 105, 99, 104),
                  (104, 112, 103, 111)],    # High 112 >= target 110
                 DATES[:3])
    o, st = bo.resolve_signal(_sig(), bars, window=5)
    assert st == "resolved" and o.outcome == "WON" and o.days == 2
    assert o.result == pytest.approx(10.0)          # limit fill at 110
    assert o.gap_result == pytest.approx(10.0)      # open 104 < target


def test_gap_up_open_fills_better_than_the_limit():
    bars = _bars([(100, 101, 99, 100), (115, 120, 114, 118)], DATES[:2])
    o, _ = bo.resolve_signal(_sig(), bars, window=5)
    assert o.outcome == "WON"
    assert o.result == pytest.approx(10.0)          # canonical: the limit
    assert o.gap_result == pytest.approx(15.0)      # real fill: the open


def test_invalidation_hit_is_a_loss_and_gap_down_fills_worse():
    bars = _bars([(100, 101, 99, 100), (85, 88, 84, 86)], DATES[:2])
    o, _ = bo.resolve_signal(_sig(), bars, window=5)
    assert o.outcome == "LOST" and o.days == 1
    assert o.result == pytest.approx(-10.0)         # canonical: the stop
    assert o.gap_result == pytest.approx(-15.0)     # real fill: the open


def test_same_day_double_touch_counts_won_but_is_flagged():
    bars = _bars([(100, 101, 99, 100), (100, 112, 88, 95)], DATES[:2])
    o, _ = bo.resolve_signal(_sig(), bars, window=5)
    assert o.outcome == "WON" and o.ambiguous


def test_expired_measures_the_window_end_close():
    bars = _flat([100, 101, 102, 103, 99, 104], DATES)
    o, st = bo.resolve_signal(_sig(), bars, window=5)
    assert st == "resolved" and o.outcome == "EXPIRED" and o.days == 5
    assert o.result == pytest.approx(4.0)           # last close 104 vs 100
    assert o.fwd_ret == pytest.approx(4.0)


def test_window_not_yet_elapsed_is_open_not_expired():
    o, st = bo.resolve_signal(_sig(), _flat([100, 101, 102], DATES[:3]),
                              window=5)
    assert st == "open" and o is None


def test_mae_is_the_worst_intraday_drawdown_before_resolution():
    bars = _bars([(100, 101, 99, 100),
                  (100, 101, 92, 95),      # -8% intraday, no stop touch
                  (95, 112, 94, 111)], DATES[:3])
    o, _ = bo.resolve_signal(_sig(), bars, window=5)
    assert o.outcome == "WON" and o.mae == pytest.approx(-8.0)


def test_mae_is_zero_when_the_trade_never_traded_below_entry():
    # Straight up from the open: adverse excursion is zero, never positive.
    bars = _bars([(100, 101, 99, 100), (105, 112, 104, 111)], DATES[:2])
    o, _ = bo.resolve_signal(_sig(), bars, window=5)
    assert o.outcome == "WON" and o.mae == 0.0


def test_missing_levels_are_not_resolved():
    for kw in ({"stop": None}, {"target": None}):
        o, st = bo.resolve_signal(_sig(**kw), _flat([100] * 6, DATES), 5)
        assert st == "no_levels" and o is None


def test_signal_before_the_series_starts_has_no_bars():
    bars = _flat([100, 101], ["2026-08-10", "2026-08-11"])
    o, st = bo.resolve_signal(_sig(), bars, window=5)
    assert st == "no_bars" and o is None


# --------------------------------------------------------------------------- #
# re-anchoring — dividend re-adjustment must not resolve on wrong units
# --------------------------------------------------------------------------- #
def test_dividend_readjustment_rescales_target_and_stop():
    # Every price re-adjusted down 2%: the recorded 100/110/90 becomes
    # 98/107.8/88.2 in series units. A 3% rally must still be a WON.
    r = 0.98
    bars = _bars([(100 * r, 101 * r, 99 * r, 100 * r),
                  (104 * r, 111 * r, 103 * r, 110 * r)], DATES[:2])
    o, st = bo.resolve_signal(_sig(), bars, window=5)
    assert st == "resolved" and o.outcome == "WON"
    assert o.result == pytest.approx(10.0)   # ratio cancels out of the %


def test_anchor_stays_on_the_packet_date_when_it_is_within_tolerance():
    # The packet-date bar is off by 1.5% (inside tolerance) while an older
    # bar happens to match exactly. Anchoring on the older bar would open the
    # window before the signal and grade the setup on its own decline.
    bars = _bars([(100, 101, 99, 100.0),    # 07-28 — a coincidental match
                  (104, 105, 103, 104),     # 07-29
                  (101, 102, 100, 101.5),   # 07-30 — the packet date
                  (101, 112, 100, 111)], ["2026-07-28", "2026-07-29",
                                          "2026-07-30", "2026-07-31"])
    o, st = bo.resolve_signal(_sig(packet_date="2026-07-30"), bars, window=5)
    assert st == "resolved"
    # Anchored on 07-30, so the only post bar is 07-31 and the target is hit
    # on day 1 — not day 3, which is what an anchor on 07-28 would report.
    assert o.outcome == "WON" and o.days == 1


def test_anchor_walks_back_to_the_bar_that_matches_the_recorded_close():
    # A run recorded Thursday's close but the packet is stamped Saturday;
    # the anchor must land on the matching bar, not the newest one.
    bars = _bars([(100, 101, 99, 100),      # 07-29 — the recorded close
                  (108, 109, 107, 108),     # 07-30 — a bar that ran away
                  (108, 112, 107, 111)], ["2026-07-29", "2026-07-30",
                                          "2026-07-31"])
    o, st = bo.resolve_signal(_sig(packet_date="2026-07-30"), bars, window=5)
    assert st == "resolved"
    # Anchored on 07-29 (close 100), so the window starts 07-30 and the
    # target is hit on the second post bar.
    assert o.outcome == "WON" and o.days == 2


def test_unit_mismatch_beyond_five_percent_is_dropped():
    bars = _flat([50, 51, 52, 53, 54, 55], DATES)   # split, recorded 100
    o, st = bo.resolve_signal(_sig(), bars, window=5)
    assert st == "unit_mismatch" and o is None


# --------------------------------------------------------------------------- #
# next-open entry — the honest-execution mode
# --------------------------------------------------------------------------- #
def test_next_open_entry_measures_from_the_open():
    bars = _bars([(100, 101, 99, 100), (102, 112, 101, 111)], DATES[:2])
    o, st = bo.resolve_signal(_sig(), bars, window=5, entry_mode="next-open")
    assert st == "resolved" and o.outcome == "WON"
    assert o.result == pytest.approx((110 / 102 - 1) * 100)


def test_next_open_skips_a_keg_that_gapped_past_its_target():
    bars = _bars([(100, 101, 99, 100), (112, 115, 111, 114)], DATES[:2])
    o, st = bo.resolve_signal(_sig(), bars, window=5, entry_mode="next-open")
    assert st == "gap_skip" and o is None


def test_next_open_skips_a_keg_that_gapped_through_invalidation():
    bars = _bars([(100, 101, 99, 100), (88, 89, 85, 86)], DATES[:2])
    o, st = bo.resolve_signal(_sig(), bars, window=5, entry_mode="next-open")
    assert st == "gap_skip" and o is None


# --------------------------------------------------------------------------- #
# bucket — a missing attribute belongs to no stratum
# --------------------------------------------------------------------------- #
def test_bucket_excludes_missing_values_instead_of_coercing_them():
    outs = [bo.Outcome(_sig(vol_ratio=v), "EXPIRED", 5, 0.0, 0.0, False, 0.0,
                       0.0) for v in (0.8, 1.2, None)]
    groups = bo.bucket(outs, lambda s: s.vol_ratio,
                       [("quiet", -9e9, 1.0), ("busy", 1.0, 9e9)])
    assert [len(g[1]) for g in groups] == [1, 1]     # the None joins neither


# --------------------------------------------------------------------------- #
# aggregation + csv
# --------------------------------------------------------------------------- #
def _out(outcome, result, **sigkw):
    return bo.Outcome(_sig(**sigkw), outcome, 2, result, result, False,
                      result, -3.0)


def test_agg_win_rate_is_over_decisive_signals_only():
    outs = [_out("WON", 10.0), _out("LOST", -10.0), _out("EXPIRED", 1.0),
            _out("EXPIRED", 1.0)]
    a = bo.agg(outs)
    assert a["n"] == 4 and a["dec"] == 2 and a["win"] == pytest.approx(50.0)
    assert a["exp"] == pytest.approx(0.5)


def test_agg_handles_a_stratum_with_no_decisive_signals():
    a = bo.agg([_out("EXPIRED", 1.0)])
    assert a["dec"] == 0 and a["win"] is None and a["days_to_t"] is None


def test_rr_is_the_median_ratio_not_the_ratio_of_the_means():
    # (T=+1%, S=-10%) and (T=+10%, S=-1%): the two means both land at 5.5%,
    # so a ratio-of-means reads a tidy 1.0 while neither signal is anywhere
    # near it. The median of the per-signal ratios is (0.1 + 10)/2 = 5.05.
    outs = [_out("EXPIRED", 0.0, entry=100.0, target=101.0, stop=90.0),
            _out("EXPIRED", 0.0, entry=100.0, target=110.0, stop=99.0)]
    a = bo.agg(outs)
    assert a["tgt_dist"] == pytest.approx(5.5)
    assert a["stop_dist"] == pytest.approx(-5.5)
    assert a["rr"] == pytest.approx(5.05)


def test_rr_skips_a_signal_whose_invalidation_equals_its_entry():
    outs = [_out("EXPIRED", 0.0, entry=100.0, target=110.0, stop=100.0),
            _out("EXPIRED", 0.0, entry=100.0, target=110.0, stop=90.0)]
    assert bo.agg(outs)["rr"] == pytest.approx(1.0)


def test_median_of_an_empty_or_all_none_sequence_is_none():
    assert bo.median([]) is None and bo.median([None, None]) is None
    assert bo.median([3, 1, 2]) == 2 and bo.median([4, 1, 2, 3]) == 2.5


def test_csv_roundtrip_carries_the_stratifying_attributes(tmp_path):
    outs = [_out("WON", 10.0, ticker="MU", armed=True, ignited=True,
                 spark_kinds={"sector_verdict"})]
    path = tmp_path / "outcomes.csv"
    bo.write_outcomes_csv(outs, path)
    row = next(iter(csv.DictReader(path.open())))
    assert row["ticker"] == "MU" and row["outcome"] == "WON"
    assert row["armed"] == "1" and row["ignited"] == "1"
    assert row["spark_kind"] == "sector_verdict"
    assert row["result_pct"] == "10.0" and row["regime"] == "RISK-ON"


def test_sample_banner_estimates_in_resolved_signals_not_armed_kegs():
    # 20 signals over 2 run days = 10/day. 10 resolved, so 10 are pending and
    # will resolve on their own; the gap to 50 is 50-10-10 = 30, i.e. 3 more
    # run days. Estimating off the armed rate (here 2/day, since only 4 of
    # the 20 are armed) would have claimed 20 run days.
    sigs = [_sig(armed=i < 4) for i in range(20)]
    warn = bo.sample_banner(10, sigs, ["2026-07-30", "2026-07-31"])
    assert "SAMPLE TOO SMALL TO READ (10 resolved" in warn
    assert "10 are already in flight" in warn
    assert "10.0 signals per run day" in warn
    assert "30 need roughly 3 more run days" in warn
    assert "week" not in warn          # 3 days is not "~0 weeks"


def test_sample_banner_switches_to_weeks_only_when_the_gap_is_that_long():
    # 1 signal/run day, 49 short → 49 run days ≈ 10 weeks.
    sigs = [_sig()]
    warn = bo.sample_banner(0, sigs, ["2026-07-30"])
    assert "10 weeks" in warn


def test_sample_banner_says_no_new_runs_needed_when_pending_covers_the_gap():
    sigs = [_sig() for _ in range(bo.READABLE_N)]
    warn = bo.sample_banner(2, sigs, ["2026-07-30"])
    assert "cover the gap" in warn and "more run days" not in warn


def test_sample_banner_clears_once_the_sample_is_readable():
    assert bo.sample_banner(bo.READABLE_N, [_sig()], ["2026-07-30"]) == ""


def test_sample_banner_survives_an_empty_run_day_list():
    warn = bo.sample_banner(1, [_sig()], [])
    assert "SAMPLE TOO SMALL" in warn and "more run days" not in warn
