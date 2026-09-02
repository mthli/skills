# skills

Personal skills used in Claude Code 🤖

## Coding

- `commit-context` - Commit staged changes with a rich git message derived from the current conversation — captures the *why* alongside the diff, plus structured `MODULE`-tagged Decision blocks for downstream distillation.
- `distill-module` - Roll up `MODULE: <id>` Decision blocks from `git log` into a per-module `.claude/decisions/<id>.md` snapshot — the current-consensus view that future sessions read before touching the module.
- `distill-memory` - Scan `.claude/decisions/**` and the last month of `MODULE:`-tagged commits, then propose a handful of candidate Claude Code memory entries (cross-module patterns, recurring mistakes, binding constraints) for the user to review one-by-one.
- `fable-mind` - Operating doctrine distilled from Claude Fable 5 as its retirement gift — Six Laws, a plan-act-verify loop, judgment heuristics, and a 14-entry failure-mode field guide that let smaller models do Fable-grade work; load it on hard or high-stakes tasks ("think like Fable").
- `map-module` - Create, fully refresh, or incrementally maintain a source-verified "how it works today" architecture map per module under `.claude/maps/` — research and adversarial verification run in read-only Explore subagents, the map pairs with the module's `.claude/decisions/` history, and targeted refreshes keep it current after code changes.
- `review-iterate` - Multi-round structured review of work-in-progress code or docs, with severity-tagged findings the user prioritizes and a stopping rule that prevents padding nitpicks.
- `sparse-checkout` - Personally hide files or directories from a git repo's working tree via `git sparse-checkout` — per-clone, reversible, and invisible to teammates (no `.gitignore` changes).

## Finance

Three layers, top to bottom: a **gate** (is the market healthy?), three **finders** (which names?), and three **action layers** (what do I actually do?). The 2026-05→07 outcome backtests shaped this — two skills built on a refuted consensus premise (`cross-scan`, `unusual-options-scan`) were retired; each survivor's validated edge is noted on its line.

### Gate

- `regime-scan` - Gauge whole-market health once a day — fold index trend, breadth, VIX term structure, credit spreads, and defensive rotation into one 🟢/🟡/🔴 state plus divergence flags, and log each reading so a sentiment turn shows up in the slope across runs. Signal-graded by `scripts/backtest_outcomes.py`: 6 of 10 raw state transitions were threshold whipsaws, so the banner now carries a 2-day-confirmed state that downstream sizing reads (first-day flips are "watch, not act"); the defensive-rotation flag — chronic at 83% of days as a level alarm — now requires the rotation to be deepening (both fixed 2026-07-31); SPY forward-return grading is plumbed and accrues with the log.

### Finders

- `momentum-scan` - Scan US large-cap equities for smooth uptrends and track which names persist across runs. Backtested edges (re-runnable via `scripts/backtest_outcomes.py`): sell-on-dropout beats holding through it (+0.9pt/episode, +1.7pt on former top-10 names; dropped names are weakest for ~2 weeks), and clean ≤1-distribution-day entries double tenure and top-10 reach. The once-quoted +9.0% vs +3.1% volume-tier gap was convention-inflated — re-measured at ~0.5pt.
- `mean-reversion-scan` - Scan US large-cap equities for short-term oversold reversals inside confirmed long-term uptrends (Connors-style RSI(2) setups), and track running win rates on past picks. Backtested edge: Score ≥ 40 on a fresh (1st–2nd day) listing ran +1.83%/signal, ~3× baseline.
- `base-breakout-scan` - Scan US large-cap equities for tight pre-breakout bases and track which setups persist across runs. Backtested edge: BaseWks ≥ 20 ran 75% winners vs the 45% baseline (the composite score did not discriminate).

### Action layers

- `conviction-funnel` - Run the whole scan-to-picks pipeline end to end — regime gate, momentum names, the sister scans' validated pockets — then deep-dive the top N (default 3) into actionable entry / stop / size / invalidation briefs with the market regime threaded through sizing. Every run ends by logging finalists, runner-ups, and deep-dive rejections to `state/runs/`, graded quarterly by `scripts/grade_outcomes.py` (selection ordering + mechanical entry/stop replay).
- `premarket-brief` - The pre-open event + overnight overlay, ~30 min before the bell — reuse regime-scan (structural backdrop) and the finder caches (watchlist), then layer on the futures gap, Asia/Europe, the econ calendar + earnings + headlines, sentiment, and your positions into a 9-section briefing with an event-gated game plan, archived daily and reconciled against the tape so the regime call calibrates over time.
- `snapback-scan` - Powder-keg × spark-calendar join for violent post-capitulation snapbacks — filter mean-reversion-scan's freshest high-score oversold names (the backtested 3× profile) against the next few days of scheduled catalysts (own earnings, same-sector megacap verdict prints, FOMC/CPI-class macro), then emit a pre-committed entry protocol per armed name (tranche / invalidation price / add-trigger) with chase-guard flags, graded against the prior run's full sample — buy when nobody dares, but with a date, a size, and an exit.

### Journal & data

- `yfinance` - Fetch stock/ETF/index quotes and historical OHLCV data from Yahoo Finance.
- [`edgartools`](https://github.com/dgunning/edgartools) - Access and analyze SEC Edgar filings, XBRL financial statements, 10-K, 10-Q, and 8-K reports.
- [`IBKR`](https://www.interactivebrokers.com/en/trading/ai-integrations.php) - Analyze portfolios, research investments, monitor risk, and generate trade instructions using AI.

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
