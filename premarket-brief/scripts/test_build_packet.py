"""Pure-logic tests for build_packet's data-quality layer — no network.

Run: uv run --with pandas --with 'yfinance>=1.3,<2' --with pytest pytest test_build_packet.py
(yfinance is imported by the module; every network call site is stubbed here.)
"""
from datetime import date

import pandas as pd

import build_packet as bp

TODAY = date(2026, 7, 30)


def _daily_frame(closes_by_ticker: dict[str, list], dates: list[str],
                 tz: str | None = None, ohlc_spread: float = 0.0) -> pd.DataFrame:
    """MultiIndex (ticker, field) daily frame like yf.download(group_by='ticker').
    ohlc_spread widens Open/High/Low around Close so OHLC math is testable."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    if tz:
        idx = idx.tz_localize(tz)
    tickers = list(closes_by_ticker)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close"]])
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for tk, closes in closes_by_ticker.items():
        for i, c in enumerate(closes):
            if c is None:
                continue
            df.loc[idx[i], (tk, "Open")] = c + ohlc_spread
            df.loc[idx[i], (tk, "High")] = c + 2 * ohlc_spread
            df.loc[idx[i], (tk, "Low")] = c - 2 * ohlc_spread
            df.loc[idx[i], (tk, "Close")] = c
    return df


# --------------------------------------------------------------------------- #
# daily_prev_closes — the official-close gap basis (the 07-30 pollution fix)
# --------------------------------------------------------------------------- #
def test_prev_close_skips_todays_partial_bar(monkeypatch):
    """Intraday, today's forming bar is in the frame — prev must be yesterday."""
    frame = _daily_frame({"SPY": [727.0, 729.46, 733.0],
                          "QQQ": [670.0, 661.73, 668.0]},
                         ["2026-07-28", "2026-07-29", "2026-07-30"])
    monkeypatch.setattr(bp.yf, "download", lambda *a, **k: frame)
    errors = []
    out = bp.daily_prev_closes(["SPY", "QQQ"], TODAY, errors)
    assert out == {"SPY": 729.46, "QQQ": 661.73}
    assert errors == []


def test_prev_close_tz_aware_index(monkeypatch):
    """yfinance daily bars can come back tz-localized — the date filter must hold."""
    frame = _daily_frame({"SPY": [729.46, 733.0], "QQQ": [661.73, 668.0]},
                         ["2026-07-29", "2026-07-30"], tz="America/New_York")
    monkeypatch.setattr(bp.yf, "download", lambda *a, **k: frame)
    out = bp.daily_prev_closes(["SPY", "QQQ"], TODAY, [])
    assert out == {"SPY": 729.46, "QQQ": 661.73}


def test_prev_close_batch_failure_degrades_to_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("throttled")
    monkeypatch.setattr(bp.yf, "download", boom)
    errors = []
    assert bp.daily_prev_closes(["SPY"], TODAY, errors) == {}
    assert len(errors) == 1 and "daily_prev_closes" in errors[0]


def test_prev_close_missing_ticker_skipped(monkeypatch):
    frame = _daily_frame({"SPY": [729.46, 733.0], "NOPE": [None, None]},
                         ["2026-07-29", "2026-07-30"])
    monkeypatch.setattr(bp.yf, "download", lambda *a, **k: frame)
    out = bp.daily_prev_closes(["SPY", "NOPE"], TODAY, [])
    assert out == {"SPY": 729.46}


# --------------------------------------------------------------------------- #
# premarket_movers — daily prev primary, flagged fast_info fallback
# --------------------------------------------------------------------------- #
def _minute_frame(last_by_ticker: dict[str, float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-30 09:00", tz="America/New_York")])
    cols = pd.MultiIndex.from_product([list(last_by_ticker), ["Close"]])
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for tk, px in last_by_ticker.items():
        df.loc[idx[0], (tk, "Close")] = px
    return df


def test_movers_use_daily_prev_and_source_tag(monkeypatch):
    monkeypatch.setattr(bp.yf, "download",
                        lambda *a, **k: _minute_frame({"FTNT": 170.11, "LRCX": 286.0}))
    monkeypatch.setattr(bp, "quote", lambda tk: (_ for _ in ()).throw(AssertionError(
        "fast_info must not be touched when the daily prev exists")))
    quality = []
    out = bp.premarket_movers(["FTNT", "LRCX"],
                              prev_map={"FTNT": 153.22, "LRCX": 252.79},
                              quality=quality)
    ftnt = out["movers"]["FTNT"]
    assert ftnt["prev_close"] == 153.22 and ftnt["prev_close_source"] == "daily"
    assert ftnt["pct"] == 11.02          # the real +11% gap, not fast_info's +0.15%
    assert quality == []


def test_movers_fallback_is_flagged(monkeypatch):
    monkeypatch.setattr(bp.yf, "download",
                        lambda *a, **k: _minute_frame({"FTNT": 170.11, "LRCX": 286.0}))
    monkeypatch.setattr(bp, "quote", lambda tk: {"prev_close": 169.85})
    quality = []
    out = bp.premarket_movers(["FTNT", "LRCX"], prev_map={"LRCX": 252.79},
                              quality=quality)
    assert out["movers"]["FTNT"]["prev_close_source"] == "fast_info"
    assert len(quality) == 1 and "FTNT" in quality[0]
    # quality=None (batch-failure mode) must suppress the per-ticker flag spam
    out2 = bp.premarket_movers(["FTNT", "LRCX"], prev_map={}, quality=None)
    assert out2["movers"]["FTNT"]["prev_close_source"] == "fast_info"


# --------------------------------------------------------------------------- #
# cross_source_check — yfinance vs TradingView disagreement flags
# --------------------------------------------------------------------------- #
def _blocks(yf_pct: dict[str, float], gainers: list, losers: list = ()):
    movers = {"movers": {tk: {"pct": p} for tk, p in yf_pct.items()}}
    gaps = {"gainers": [{"ticker": t, "pct": p} for t, p in gainers],
            "losers": [{"ticker": t, "pct": p} for t, p in losers]}
    return movers, gaps


def test_cross_source_flags_only_material_divergence():
    movers, gaps = _blocks({"ARM": 6.59, "MU": 6.7},
                           gainers=[("ARM", 15.46), ("MU", 7.1)])
    quality = []
    bp.cross_source_check(movers, gaps, quality)
    assert len(quality) == 1 and "ARM" in quality[0]      # Δ8.9pp yes, Δ0.4pp no


def test_cross_source_covers_losers_and_ignores_gaps_in_coverage():
    movers, gaps = _blocks({"SIRI": -3.88},
                           gainers=[("MKTX", 29.9)],       # not in movers → skip
                           losers=[("SIRI", -7.95), ("CROX", None)])
    quality = []
    bp.cross_source_check(movers, gaps, quality)
    assert len(quality) == 1 and "SIRI" in quality[0]


# --------------------------------------------------------------------------- #
# realized_moves — reconciliation math on official (unadjusted) bars
# --------------------------------------------------------------------------- #
def test_realized_moves_ohlc_and_gap(monkeypatch):
    seen = {}

    def fake_download(*a, **k):
        seen.update(k)
        return _daily_frame({tk: [100.0, 101.0] for tk in
                             sorted(set(bp.INDEX_PROXIES) | set(bp.SECTOR_ETFS))},
                            ["2026-07-28", "2026-07-29"], ohlc_spread=1.0)

    monkeypatch.setattr(bp.yf, "download", fake_download)
    out = bp.realized_moves(date(2026, 7, 29), [])
    spy = out["moves"]["SPY"]
    assert spy["pct"] == 1.0                      # 101 vs 100
    assert spy["open"] == 102.0 and spy["gap_pct"] == 2.0
    assert spy["high"] == 103.0 and spy["low"] == 99.0
    # The ex-div trap: reconciliation grades official levels, never adjusted.
    assert seen["auto_adjust"] is False
