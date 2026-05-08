[← back to SKILL.md](../SKILL.md)

# `news` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`news.py --limit 2 AAPL` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Presenting news results](#presenting-news-results) · [Mode-specific caveats](#mode-specific-caveats)

Recent Yahoo Finance news headlines for one or more tickers. Yahoo
returns up to ~10 articles per ticker. Works for **any** quote type
(equity / ETF / index / crypto / FX / future) — unlike `info`, news is
not equity-only.

## Run

```bash
# Default: full Yahoo response (~10 articles), pretty JSON
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py AAPL

# Cap to top 3, multiple tickers
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py --limit 3 AAPL MSFT TSLA

# CSV — one row per article (symbol col repeats per ticker)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py --limit 5 --format csv AAPL MSFT

# NDJSON — one JSON object per ticker per line
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/news.py --format ndjson AAPL 0700.HK BTC-USD
```

Tickers are positional args.

## CLI arguments

- `--limit N` — cap articles per ticker. Default: keep everything Yahoo
  returns (~10). Useful for tightening context when you have many
  tickers in one call. **Important**: Yahoo orders articles by editorial
  / relevance signal, **not strictly chronologically** (verified
  empirically — see "Mode-specific caveats"). So `--limit 3` gives you
  Yahoo's top-3 picks, not the 3 most recent. If you want strict
  newest-first, sort the consumer-side output by `pub_date`.
- `--format json|ndjson|csv` — output format. `json` (default) is the
  pretty JSON array, one record per ticker. `ndjson` emits one JSON
  record per ticker per line (already grouped by ticker — handy for
  `grep`/`jq` filters across a multi-ticker batch). `csv` emits one row
  per ARTICLE, with the `symbol` column repeating across a ticker's
  articles. CSV column order (left to right): `symbol`, then the 8
  article fields (in their schema-table order below), then `note`,
  then the 3 meta fields (`error`, `error_kind`, `attempts`).
  Tickers with zero articles get a row with the symbol + the `note`
  column populated (so the empty-result case is visible in the table,
  not just a blank row); tickers with errors get a row with the
  symbol + meta columns populated.

**No `--summary` mode.** Most other wrappers (`history` / `info` /
`earnings` / `financials`) have a `--summary` flag; news doesn't, and
neither does `fast_info` (which is already flat). News is fundamentally
a list per ticker rather than headline numerics for peer comparison,
so the same projection shape doesn't apply. Use `--limit N` to tighten
context instead.

**Note: `count` is JSON-only.** The JSON / NDJSON output carries a
per-ticker `count` field; CSV does not (its rows are per-article, so
there's no natural place for a per-ticker rollup). If you need
counts in CSV, group by `symbol` after the fact.

## Output schema

Per ticker (illustrative — strings below are placeholders, not a real
captured snapshot):

```json
[
  {
    "symbol": "AAPL",
    "count": 2,
    "articles": [
      {
        "title": "<headline string>",
        "summary": "<1–3 sentence plaintext summary>",
        "pub_date": "2026-05-07T21:04:15Z",
        "provider": "<source name e.g. Reuters / Yahoo Finance Video>",
        "content_type": "VIDEO",
        "url": "https://www.example.com/articles/<slug>",
        "is_premium": false,
        "editors_pick": true
      },
      ...
    ]
  }
]
```

Per article (8 fields):

| Field | Type | Notes |
|---|---|---|
| `title` | str | Article headline. |
| `summary` | str | Plaintext summary, usually 1–3 sentences. Cleaner than the raw `description` field (which Yahoo sometimes serves with HTML); we expose `summary` only. |
| `pub_date` | str | ISO 8601 with `Z` (UTC). E.g. `"2026-05-07T21:04:15Z"`. Always populated in samples. **Yahoo's list is NOT strictly sorted by `pub_date`** — sort consumer-side if you need strict newest-first (see "Mode-specific caveats"). |
| `provider` | str | Source name. Examples observed in samples: `"Yahoo Finance Video"`, `"Yahoo Finance"`, `"Investor's Business Daily"`, `"Motley Fool"`, `"24/7 Wall St."`, `"Simply Wall St."`, `"Barrons.com"`, `"CBS News"`. |
| `content_type` | str | Only `"STORY"` and `"VIDEO"` observed across 80 samples (8 tickers, 2026-05). Treat unknown values defensively if they ever appear. |
| `url` | str | Yahoo's `canonicalUrl` — the **publisher's original URL** (e.g. `cbsnews.com/...`, `barrons.com/...`, `247wallst.com/...`). Always populated. Yahoo also exposes `clickThroughUrl` (a Yahoo-hosted mirror, e.g. `finance.yahoo.com/markets/stocks/articles/...`); we drop it because (a) the publisher original is the more "canonical" reference, (b) `clickThroughUrl` is `null` for paywalled originals (e.g. Barron's), and (c) for ~40–70% of articles the two URLs differ — exposing both would force callers to pick. Bring `clickThroughUrl` back as a separate field if a "fallback when canonical is paywalled" use case shows up. |
| `is_premium` | bool / null | True iff Yahoo flags the article as paywalled premium content. Note: paywalled URLs still resolve in a browser (the paywall is at the publisher); filtering is a presentation choice, not a hard requirement. |
| `editors_pick` | bool / null | Yahoo's `metadata.editorsPick` flag — surfaces curated headlines. |

**Empty result is success, not error.** Empty Yahoo response →
`count: 0, articles: []` + a `note` string; **no** `error_kind` is
set. See [Empty list is ambiguous](#empty-list-ambiguous) below for
the rationale and how to disambiguate via `info.py`.

**Retry surfacing.** Same as the other modes: a top-level `attempts` key
appears only when the call retried (transient 429 / network), absent on
clean first-attempt success.

A failed ticker (real network error, classified as rate_limit / network /
unknown) looks like:

```json
{
  "symbol": "AAPL",
  "error": "fetch failed (rate_limit, after 3 attempt(s))",
  "error_kind": "rate_limit",
  "attempts": 3
}
```

See SKILL.md "Cross-cutting caveats" for retry semantics and the full
`error_kind` enum.

## Presenting news results

**Single ticker, headline scan.** Date-prefixed bullets work well —
strip the time, lead with the title, italicize the provider. Format
template (placeholder text — illustrative shape, not real headlines):

> **\<TICKER\> — recent news**
> - **\<YYYY-MM-DD\>** — \<headline string\> (_\<provider\>_)
> - **\<YYYY-MM-DD\>** — \<headline string\> (_\<provider\>_)
> - **\<YYYY-MM-DD\>** — \<headline string\> (_\<provider\>_)

If multiple items share a date, you can promote the date to a heading
and nest titles below it (real "grouped by date") — useful for longer
batches where the same date repeats 3+ times. For shorter scans the
date-prefixed flat list above is less visually heavy.

Include the URL only if the user asked to read more, or if the summary
is too short to convey the story. Yahoo titles are usually
self-contained — extra summary text is often noise.

**Multi-ticker.** Group by ticker first, then by date within. Don't
interleave — the user almost always cares about one ticker at a time
even when querying several.

**Filtering hints (when the result is too long):**
- Drop `content_type: "VIDEO"` if a written summary is needed (videos
  give a `summary` blurb but the substance is in the clip).
- Sort by `editors_pick: true` first to surface curated picks.
- Cap to `--limit 3` for an "at-a-glance" scan; bump to 10 for "what's
  the news cycle look like".
- Sort by `pub_date` descending if the user explicitly asks for
  "most recent" — Yahoo's default order is editorial relevance, not
  strict chronology.

**Date formatting.** `pub_date` is UTC. For US tickers an ET conversion
reads more natural (`21:04 UTC` → `17:04 ET`); for non-US tickers, prefer
the local exchange's tz (HKT for `0700.HK`, JST for `7203.T`). When in
doubt, just show the date — most users don't care about minute-level
timing of news.

**Escape `$` as `\$` in titles and summaries** if rendering as markdown
prose — same rationale as the other modes (`$237.30` may get swallowed
by math-mode parsing). Titles often contain dollar amounts.

## Mode-specific caveats

- **News is not equity-restricted.** Crypto / FX / index / futures all
  return news. Index news tends to be macro / market-recap; crypto news
  is broader (industry coverage, not just the specific coin). For an
  ETF, news mixes fund-specific items with the underlying-sector cycle.
- **Coverage outside US-listed tickers: different mix, mostly similar
  volume.** US large-caps return up to 10 items (consistently 10
  across AAPL / MSFT / TSLA / GOOGL / NVDA in our 2026-05 sample),
  dominated by Yahoo Finance Video, Investor's Business Daily, Motley
  Fool, and similar US financial press / in-house Yahoo content. Non-US large-caps usually also return the
  full ~10 items, with a more varied provider mix. Volume drops
  occasionally for lower-coverage names (e.g. 600519.SS returned 5
  in our sample). Verified empirically across 0700.HK, 9988.HK,
  600519.SS, BMW.DE, 7203.T, 005930.KS, BARC.L in 2026-05:
  - Major international press: Reuters, Bloomberg, Wall Street
    Journal, Financial Times.
  - Aggregators: Simply Wall St., Insider Monkey, MT Newswires,
    Motley Fool, 24/7 Wall St., Investing.com, Zacks.
  - Some English-language local press: South China Morning Post (HK
    names), PA Media (LSE).
  
  What you generally **don't** get is native-language local press —
  no Chinese / Japanese / Korean / German publishers even for tickers
  from those markets. So coverage is "global English finance press"
  rather than "local market press in translation".
- <a id="empty-list-ambiguous"></a>**Empty list is ambiguous (full discussion).** Yahoo doesn't
  distinguish "ticker doesn't exist" from "ticker exists but no recent
  news" — both return `[]`. We deliberately don't promote empty to
  `error_kind: not_found` (low-coverage real tickers shouldn't read as
  errors) and instead emit `count: 0, articles: []` plus a `note`
  string carrying the disambiguation hint. The CSV path projects `note`
  into a dedicated column so empty rows still carry signal in tabular
  output. To check whether a symbol actually resolves, call
  `info.py` (which uses `quoteType` as a robust not-found signal).
- **Yahoo's order is editorial / relevance, NOT strictly chronological.**
  Verified across 6 tickers in 2026-05: 4 of 6 had a non-monotone
  `pub_date` sequence, including AAPL, MSFT, BTC-USD, and ^GSPC. The
  top item is Yahoo's "most relevant" pick, not necessarily the most
  recent. Two consequences:
  - `--limit N` returns Yahoo's top N by editorial signal, not the N
    most recent. If the user explicitly asks for "most recent", sort
    consumer-side by `pub_date` descending.
  - "What's the latest news on X" maps reasonably well to Yahoo's
    default order in practice (the relevance signal heavily weights
    recency), but it's not a hard guarantee.
- **Rate of change.** News updates frequently during US market hours
  (empirical frequency not measured precisely; expect new items
  intra-session). Two consecutive calls on the same ticker can return
  different `id`s for the top item even minutes apart. Don't dedupe
  across calls; just refetch.
- **`description` and `summary` overlap.** Yahoo emits both. We expose
  `summary` only because `description` is sometimes raw HTML (`<p>...
  <a href=...>`) which is hostile for prose rendering. If you ever need
  the linked entities (other tickers mentioned in the body), add a
  `--include-description` flag — but for the default "what's the news"
  question, `summary` is enough.
- **No date filtering.** The script doesn't expose a `--since` flag.
  Since Yahoo's list isn't strictly chronological (see above), a
  consumer-side `--since` filter would also need to sort by `pub_date`
  before truncating. Adding the flag is cheap if a real use case shows
  up.
