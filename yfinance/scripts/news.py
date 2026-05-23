#!/usr/bin/env python3
"""Fetch yfinance Ticker.news for one or more tickers and print as JSON.

See `news.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; each entry has a list of articles (title, summary,
pub_date, provider, content_type, url, is_premium, editors_pick). Failed
tickers carry an "error" field instead of articles so a single bad
symbol does not poison the batch.
"""
from __future__ import annotations
import yfinance as yf
from helpers import RESULT_META, emit_json_or_ndjson, safe_bool, safe_str, with_retry

import argparse
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Per-article output fields, in emit order. title / summary / pub_date /
# provider / url answer "what happened, when, who said it, where do I read
# more". content_type distinguishes STORY vs VIDEO so callers can decide
# whether a written summary is even meaningful. The two booleans surface
# editorial picks (`editors_pick`) and access tier (`is_premium`) so
# callers can prioritize / annotate as needed. Dropped from the raw payload:
#   thumbnail        — LLMs can't see images, big token cost
#   description      — overlaps summary, often raw HTML
#   id               — Yahoo-internal opaque id, no consumer use
#   storyline        — related-stories sub-list, noisy per article
#   clickThroughUrl  — differs from canonicalUrl ~40–70% of the time and
#                      is null for paywalled originals. canonicalUrl is
#                      the publisher's original URL; clickThroughUrl is
#                      the Yahoo-hosted mirror. We expose canonicalUrl
#                      as `url` because it's always populated and the
#                      more "canonical" reference; bring back as a
#                      separate field if a fallback-when-paywalled use
#                      case shows up.
#   displayTime      — verified across 80 samples (8 tickers, 2026-05):
#                      VIDEO 9/9 empty; STORY 70/71 == pubDate. Fully
#                      redundant with pubDate (which is always populated)
#                      so we drop it.
ARTICLE_FIELDS = (
    "title", "summary", "pub_date", "provider", "content_type",
    "url", "is_premium", "editors_pick",
)

# Per-ticker fields that carry through to every CSV row for that ticker
# (article rows AND the empty-result row). RESULT_META covers the failure
# metadata; `note` covers ambiguous-but-successful state (empty Yahoo
# response). Same convention as earnings.py / financials.py — see
# SKILL.md "`note` field convention" for the cross-mode contract.
_CSV_CARRY_KEYS = (*RESULT_META, "note")


def _project_article(item: dict) -> dict:
    """Pull the LLM-relevant fields out of a single news payload."""
    c = item.get("content") or {}
    provider = (c.get("provider") or {}).get("displayName")
    canonical = (c.get("canonicalUrl") or {}).get("url")
    metadata = c.get("metadata") or {}
    finance = (c.get("finance") or {}).get("premiumFinance") or {}
    return {
        "title": safe_str(c.get("title")),
        "summary": safe_str(c.get("summary")),
        "pub_date": safe_str(c.get("pubDate")),
        "provider": safe_str(provider),
        "content_type": safe_str(c.get("contentType")),
        "url": safe_str(canonical),
        "is_premium": safe_bool(finance.get("isPremiumNews")),
        "editors_pick": safe_bool(metadata.get("editorsPick")),
    }


def fetch(symbol: str, *, limit: int | None) -> dict:
    raw, err_kind, attempts = with_retry(lambda: yf.Ticker(symbol).news)
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    # Empty list is NOT an error: Yahoo returns [] for bogus tickers AND
    # for low-coverage real ones (e.g., obscure CN A-shares with no English
    # press) AND occasionally for transient gaps. We can't distinguish, so
    # report as success-with-zero-articles + a note rather than error_kind:
    # not_found. Caller can decide what to do (try info.py to verify the
    # ticker exists, etc.).
    if not raw:
        out = {
            "symbol": symbol,
            "count": 0,
            "articles": [],
            "note": "no news returned (delisted, low-coverage ticker, or transient gap — try info.py to verify the symbol resolves)",
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out
    items = raw if limit is None else raw[:limit]
    out = {
        "symbol": symbol,
        "count": len(items),
        "articles": [_project_article(it) for it in items],
    }
    if attempts > 1:
        out["attempts"] = attempts
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch recent Yahoo Finance news headlines for one or more tickers.\n\n"
            f"Per article ({len(ARTICLE_FIELDS)} fields): {', '.join(ARTICLE_FIELDS)}.\n"
            "Yahoo returns up to ~10 items per ticker; use --limit to cap further.\n"
            "Works for any ticker type (equity / ETF / index / crypto / FX /\n"
            "future) — unlike info, news is not equity-only."
        ),
        epilog=(
            "Examples:\n"
            "  news.py AAPL\n"
            "  news.py --limit 3 AAPL MSFT TSLA\n"
            "  news.py --format ndjson --limit 5 0700.HK BTC-USD\n"
            "  news.py --format csv AAPL MSFT     # one row per article\n"
            "\n"
            "See references/news.md for the field schema, presentation guidance,\n"
            "and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap articles per ticker (default: keep all Yahoo returns, ~10).")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = one row per ARTICLE (symbol col repeats per ticker; "
                         "tickers with no articles emit a single row carrying "
                         "the symbol + the `note` column + meta fields).")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix.")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    results = [fetch(s.strip().upper(), limit=args.limit)
               for s in args.symbols if s.strip()]
    _emit(results, args.format)


def _emit(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # csv: one row per article (symbol col repeats). Tickers with
    # errors / no news emit a single row carrying the symbol + `note` +
    # meta fields so they're not silently dropped from the table.
    import csv as _csv
    cols = ["symbol", *ARTICLE_FIELDS, "note", *RESULT_META]
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        articles = r.get("articles") or []
        carry = {k: r[k] for k in _CSV_CARRY_KEYS if k in r}
        # Both branches build a dict keyed on `cols` then emit via
        # `.get(c, "")` so any column missing from the merged dict
        # collapses to empty string in CSV.
        if not articles:
            writer.writerow([{"symbol": symbol, **carry}.get(c, "")
                            for c in cols])
            continue
        for a in articles:
            writer.writerow(
                [{"symbol": symbol, **a, **carry}.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
