"""Tests for refresh_universe pagination behavior.

Mocks yf.screen so we can exercise the page-size / offset / stop-condition
logic without hitting the network.

Run from the skill root via:
    uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
      --with 'pytest' pytest scripts/
"""
import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan


def _page(symbols, total=None):
    """Build a fake yf.screen response."""
    out = {"quotes": [{"symbol": s} for s in symbols]}
    if total is not None:
        out["total"] = total
    return out


@pytest.fixture
def universe_file(tmp_path, monkeypatch):
    """Redirect UNIVERSE_FILE to tmp + zero out the inter-page sleep so the
    suite stays fast (otherwise each multi-page test pays 0.2s × N)."""
    f = tmp_path / "universe.txt"
    monkeypatch.setattr(scan, "UNIVERSE_FILE", f)
    monkeypatch.setattr(scan, "SCREENER_PAGE_SLEEP_SEC", 0)
    return f


@pytest.fixture
def mock_screen(monkeypatch):
    """Helper: install a MagicMock for scan.yf.screen with the given pages
    as side_effect, and return the mock so tests can assert call_count /
    call_args. Pass either a list of page dicts (returned in order) or a
    single callable (used as side_effect directly)."""
    def _install(pages_or_callable):
        mock = MagicMock(side_effect=pages_or_callable)
        monkeypatch.setattr(scan.yf, "screen", mock)
        return mock
    return _install


def test_explicit_count_paginates_with_offset(universe_file, mock_screen):
    """count=500 → two 250-row requests, second one with offset=250."""
    page1 = _page([f"T{i:04d}" for i in range(250)])
    page2 = _page([f"T{i:04d}" for i in range(250, 500)])
    mock = mock_screen([page1, page2])

    tickers = scan.refresh_universe(5e9, 1_000_000, 500)

    assert len(tickers) == 500
    assert tickers[0] == "T0000"
    assert tickers[-1] == "T0499"
    assert mock.call_count == 2
    assert mock.call_args_list[0].kwargs["offset"] == 0
    assert mock.call_args_list[1].kwargs["offset"] == 250


def test_count_none_uses_response_total(universe_file, mock_screen):
    """count=None reads `total` from the first response and caps there."""
    page1 = _page([f"T{i:04d}" for i in range(250)], total=300)
    page2 = _page([f"T{i:04d}" for i in range(250, 300)])
    mock = mock_screen([page1, page2])

    tickers = scan.refresh_universe(5e9, 1_000_000, None)

    assert len(tickers) == 300
    assert mock.call_count == 2
    # Second page should request exactly the remainder, not a full 250.
    assert mock.call_args_list[1].kwargs["size"] == 50


def test_explicit_count_caps_below_total(universe_file, mock_screen):
    """User-supplied count wins even if Yahoo would return more."""
    page = _page([f"T{i:04d}" for i in range(100)], total=1000)
    mock = mock_screen([page])

    tickers = scan.refresh_universe(5e9, 1_000_000, 100)

    assert len(tickers) == 100
    assert mock.call_count == 1
    assert mock.call_args_list[0].kwargs["size"] == 100


def test_short_page_stops_when_total_missing(universe_file, mock_screen):
    """No `total` in response → short page triggers natural end of results."""
    page1 = _page([f"T{i:04d}" for i in range(250)])
    page2 = _page([f"T{i:04d}" for i in range(250, 280)])  # 30 < 250
    mock = mock_screen([page1, page2])

    tickers = scan.refresh_universe(5e9, 1_000_000, None)

    assert len(tickers) == 280
    assert mock.call_count == 2


def test_zero_added_stops_when_total_missing(universe_file, mock_screen):
    """All-duplicate page → 0 added → break (the loop-prevention guard)."""
    page1 = _page([f"T{i:04d}" for i in range(250)])
    page2 = _page([f"T{i:04d}" for i in range(250)])  # identical to page1
    mock = mock_screen([page1, page2])

    tickers = scan.refresh_universe(5e9, 1_000_000, None)

    assert len(tickers) == 250
    assert mock.call_count == 2


def test_max_pages_backstop(universe_file, mock_screen, monkeypatch, capsys):
    """No `total`, every page is full + fresh → only SCREENER_MAX_PAGES stops it.
    Also verifies the backstop emits a stderr warning AND tags the final
    "Refreshed universe" line as TRUNCATED so users can tell the universe was
    cut short rather than naturally exhausted."""
    monkeypatch.setattr(scan, "SCREENER_MAX_PAGES", 3)
    counter = {"n": 0}

    def fake_screen(*args, **kwargs):
        i = counter["n"]
        counter["n"] += 1
        return _page([f"P{i}_T{j:04d}" for j in range(250)])

    mock_screen(fake_screen)

    tickers = scan.refresh_universe(5e9, 1_000_000, None)

    assert counter["n"] == 3  # exactly the cap, not 4+
    assert len(tickers) == 750
    captured = capsys.readouterr()
    assert "SCREENER_MAX_PAGES=3" in captured.err
    assert "backstop" in captured.err
    assert "TRUNCATED" in captured.err


def test_normal_completion_not_marked_truncated(universe_file, mock_screen, capsys):
    """Counter-test for TRUNCATED tag: a normal exhaustive run must NOT carry
    the truncation marker, otherwise users would lose the signal-to-noise."""
    page = _page(["AAPL", "MSFT"], total=2)
    mock_screen([page])

    scan.refresh_universe(5e9, 1_000_000, None)

    captured = capsys.readouterr()
    assert "Refreshed universe:" in captured.err  # plain form, not "(TRUNCATED)"
    assert "TRUNCATED" not in captured.err


def test_empty_first_page_raises(universe_file, mock_screen):
    """Yahoo returning zero results is treated as an error, not an empty cache.
    The cache file must not be written (a transient outage shouldn't poison it)."""
    mock_screen([_page([])])

    with pytest.raises(RuntimeError, match="returned no results"):
        scan.refresh_universe(5e9, 1_000_000, None)

    assert not universe_file.exists()


def test_writes_universe_file_atomically(universe_file, mock_screen):
    """Successful refresh persists the ticker list to UNIVERSE_FILE and
    leaves no .tmp file behind."""
    page = _page(["AAPL", "MSFT", "NVDA"], total=3)
    mock_screen([page])

    scan.refresh_universe(5e9, 1_000_000, None)

    assert universe_file.read_text().splitlines() == ["AAPL", "MSFT", "NVDA"]
    assert not universe_file.with_suffix(".txt.tmp").exists()


def test_positive_int_argparse_validator():
    """--universe-count must reject 0 / negative / non-int with the right exception."""
    assert scan._positive_int("250") == 250
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        scan._positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        scan._positive_int("-5")
    with pytest.raises(argparse.ArgumentTypeError, match="integer"):
        scan._positive_int("abc")


def test_load_universe_refreshes_when_cache_smaller_than_count(
    universe_file, mock_screen, capsys
):
    """If the cache has fewer rows than the requested count, force-refresh
    even within TTL — otherwise the user silently gets the smaller pool."""
    universe_file.write_text("AAA\nBBB\nCCC")
    fresh = _page([f"T{i:02d}" for i in range(10)], total=10)
    mock = mock_screen([fresh])

    tickers = scan.load_universe(5e9, 1_000_000, count=10, refresh_mode=None)

    assert len(tickers) == 10
    assert tickers[0] == "T00"
    assert mock.call_count == 1  # refresh was triggered
    captured = capsys.readouterr()
    assert "3 tickers but 10 requested" in captured.err


def test_old_yfinance_falls_back_to_single_page(universe_file, mock_screen, capsys):
    """Older yfinance versions don't accept the `offset` kwarg. The screener
    helper should detect the specific TypeError (mentioning 'offset'), retry
    without `offset`, and then stop after page 1 since we can't paginate.

    This is the only stop condition where we collect data and then
    deliberately don't try further — but it's a normal end-of-results, NOT
    a truncation, so the final "Refreshed universe" log must NOT carry the
    (TRUNCATED) marker (otherwise users would think something went wrong)."""
    def fake_screen(query, **kwargs):
        if "offset" in kwargs:
            raise TypeError("screen() got an unexpected keyword argument 'offset'")
        return _page([f"T{i:04d}" for i in range(250)])

    mock = mock_screen(fake_screen)

    tickers = scan.refresh_universe(5e9, 1_000_000, 500)

    assert len(tickers) == 250
    # Two yf.screen calls: first with offset (TypeError), second without (success).
    assert mock.call_count == 2
    assert "offset" in mock.call_args_list[0].kwargs
    assert "offset" not in mock.call_args_list[1].kwargs
    captured = capsys.readouterr()
    assert "Refreshed universe:" in captured.err
    assert "TRUNCATED" not in captured.err


def test_unrelated_typeerror_propagates(universe_file, mock_screen):
    """The fallback only handles the specific 'unexpected keyword argument
    offset' TypeError. Any other TypeError (e.g. wrong query type) must
    propagate — silently degrading bugs into single-page scans would hide
    real problems. The cache file must also stay untouched (a transient
    upstream bug shouldn't poison the on-disk universe)."""
    def fake_screen(query, **kwargs):
        raise TypeError("EquityQuery rejected: bad operator 'xyz'")

    mock_screen(fake_screen)

    with pytest.raises(TypeError, match="bad operator"):
        scan.refresh_universe(5e9, 1_000_000, 500)

    assert not universe_file.exists()
