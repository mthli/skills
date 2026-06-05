# skills

Personal skills used in Claude Code 🤖

## Coding

- `commit-context` - Commit staged changes with a rich git message derived from the current conversation — captures the *why* alongside the diff, plus structured `MODULE`-tagged Decision blocks for downstream distillation.
- `distill-module` - Roll up `MODULE: <id>` Decision blocks from `git log` into a per-module `.claude/decisions/<id>.md` snapshot — the current-consensus view that future sessions read before touching the module.
- `distill-memory` - Scan `.claude/decisions/**` and the last month of `MODULE:`-tagged commits, then propose a handful of candidate Claude Code memory entries (cross-module patterns, recurring mistakes, binding constraints) for the user to review one-by-one.
- `review-iterate` - Multi-round structured review of work-in-progress code or docs, with severity-tagged findings the user prioritizes and a stopping rule that prevents padding nitpicks.
- `sparse-checkout` - Personally hide files or directories from a git repo's working tree via `git sparse-checkout` — per-clone, reversible, and invisible to teammates (no `.gitignore` changes).

## Finance

- `base-breakout-scan` - Scan US large-cap equities for tight pre-breakout bases and track which setups persist across runs.
- `mean-reversion-scan` - Scan US large-cap equities for short-term oversold reversals inside confirmed long-term uptrends (Connors-style RSI(2) setups), and track running win rates on past picks.
- `momentum-scan` - Scan US large-cap equities for smooth uptrends and track which names persist across runs.
- `unusual-options-scan` - Scan US large-cap equities for unusual options activity (Vol/OI spikes, far-OTM short-DTE accumulation, extreme call/put skew) and confirm yesterday's flags via overnight OI growth.
- `cross-scan` - Cross-reference outputs from the four sister scans (momentum, base-breakout, mean-reversion, unusual-options) to surface tickers appearing in 2+ on the same day — the highest-conviction "agreement" picks.
- `regime-scan` - Gauge whole-market health once a day — fold index trend, breadth, VIX term structure, credit spreads, and defensive rotation into one 🟢/🟡/🔴 state plus divergence flags, and log each reading so a sentiment turn shows up in the slope across runs.
- `conviction-funnel` - Run the whole scan-to-picks pipeline end to end — chain regime-scan → momentum-scan → cross-scan to converge on consensus names, then deep-dive the top N (default 3) into actionable entry / stop / size / invalidation briefs with the market regime threaded through sizing — the orchestration layer above the four scans.
- `commit-invest` - Distill an investment discussion into structured Thesis / Observation / Macro / Lesson / Methodology blocks, append them to per-ticker or per-topic files, and commit — the journal layer of the investment knowledge loop.
- `distill-ticker` - Roll up `positions/<TICKER>.md` (plus ticker-tagged lessons in `postmortems.md`) into a per-ticker current-consensus snapshot at `.claude/snapshots/positions/<TICKER>.md` — the view future discussions load before forming new opinions.
- `distill-theme` - Aggregate Thesis/Observation blocks across all per-ticker journals and `RELATED_THEMES`-tagged Macro blocks into a per-theme snapshot at `.claude/snapshots/themes/<theme>.md` — the cross-ticker view (which names express the theme, what's driving it).
- `yfinance` - Fetch stock/ETF/index quotes and historical OHLCV data from Yahoo Finance.
- [`edgartools`](https://github.com/dgunning/edgartools) - Access and analyze SEC Edgar filings, XBRL financial statements, 10-K, 10-Q, and 8-K reports.

## Misc

- `hackernews` - Browse Hacker News from inside Claude Code — top stories and other feeds (new/best/Ask/Show/jobs), threaded comment views for any story, and keyword search, via a dependency-free script over the HN Firebase + Algolia APIs.
- `wallstreetbets` - Browse r/wallstreetbets from inside Claude Code — read DD (due-diligence) posts, filter by any flair (News/Gain/Loss/YOLO/Daily Discussion), keyword-search the sub, and open any post's full body or comments, via a dependency-free script over Reddit's RSS feeds (no auth, no blocked JSON API).
- `mcd-order` - Find the cheapest way for a group to order McDonald's China — treats combos as cheap component containers, redistributes their parts across people (via free in-combo swaps), and folds in coupons and points to cover everyone's exact items for the least cash, with a bundled min-cost-cover solver doing the combinatorics over live `mcd-mcp` data.
- `statusline-vocab` - Surface a "word of the conversation" segment on the Claude Code statusline — a `Stop` hook picks one English word worth learning from each turn and renders `{emoji} {word} /IPA/ pos. {translation}` (translation language configurable; Chinese by default) so you passively build vocabulary from your own work.

## License

```text
MIT License

Copyright (c) 2026 Matthew Lee
```
