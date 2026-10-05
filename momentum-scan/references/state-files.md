# State files: reference

Full schemas, write semantics and sizes for everything under `state/`. SKILL.md keeps a one-line summary per file; read this before editing a state file by hand, when a column's meaning matters to an answer, or when the `**Data**` line reports a rebuilt or stale session.

## `state/history.csv`

One snapshot per US market day (America/New_York) × every ticker that passed the filter that day (all kept picks, not only the displayed top-N; the below-cutoff rows preserve near-miss context, and persistence stats filter to rank ≤ top-N at read time).

Columns: `run_id, run_date, ticker, rank, score_rank, score, return_pct, max_dd_pct, ann_vol_pct, from_high_pct, close, vol_ratio_20d, dollar_vol_20d_m, dist_days_25d, data_asof`.

`close` and the three volume fields after it (added 2026-07-30) exist for exit-rule research:

- `close` is the adjusted close the scan saw at run time (empty for rows before the upgrade and left unfilled by design, since later corporate actions rewrite the adjusted series and delistings erase it).
- `vol_ratio_20d` is the latest session's volume over its trailing 20-session average (latest session excluded from the base, so a climax day doesn't dilute its own signal).
- `dollar_vol_20d_m` is the 20-session average dollar volume in $M.
- `dist_days_25d` is the O'Neil distribution-day count (down ≥0.2% on higher volume than the prior session) over 25 sessions.

For pre-upgrade rows, the volume trio comes from a one-time yfinance backfill keyed to each run's last *completed* session; live mid-session runs likewise compute the trio on completed bars only (`close` still records the partial bar).

`data_asof` (added 2026-10-05) is the session the run's daily bars reached, as `YYYYMMDD`. See "Data freshness" below.

Re-running the same ET day overwrites that day's rows (newer prices replace older), so streak counts scan-days rather than scan invocations. Writes are atomic (tmp file + rename) so a crash mid-write can't truncate the file. The skill's value builds up in this file over time: the first run carries little persistence signal, and each subsequent run adds more.

Storage growth: each run adds one row per filter-passing name: at default params that's ~50–130 rows × ~80 bytes ≈ 4–10 KB. A year of daily runs is ~2 MB, weekly is ~300 KB. Negligible for years of typical use; if it ever matters, prune by `run_date` with any CSV tool.

`--clear-history` wipes only this file (no confirmation prompt; pair with `git` if irreversibility matters).

### Data freshness

Since 2026-09-02 Yahoo has often not published the just-closed session's daily bar by the time the 20:00 ET scan runs: the row comes back with a NaN close, and on 2026-09-23 it was still missing at 09:00 ET the next morning. It fills in by the following evening. Before 2026-10-05 nothing noticed: the scan dropped the NaN and ranked on the prior session's closes under today's `run_id`, so the rows from 2026-09-02 to 2026-10-02 are a session behind their label (their `close` matches the prior session's bar for 96% of top-10 rows) and carry a blank `data_asof`.

`scan.py` now checks each run against the latest NYSE session whose 16:00 ET close has passed. Names whose daily series stops short of it get that session rebuilt from 30-minute intraday bars: open of the first bar, high and low across the bars, close of the last. On the 2026-10-02 replay that rebuilt all 1013 names in ~30 s, with closes a median 0.02% (p99 0.2%) from the official ones. Volume is left blank for the rebuilt session, because intraday bars miss the closing auction and ran a median 19% under the settled daily figure; a short volume would read as a quiet day and hide a distribution day. So on a rebuilt day, `vol_ratio_20d` is empty and the latest session can't count toward `dist_days_25d`.

The report's `**Data**` line says which session the numbers reflect, adds a note when it was rebuilt, and turns into a `⚠️ Stale data` warning when the rebuild also fails. Either way the run still saves: `run_id` stays the publication day and `data_asof` records the session the bars reached, so a stale row reads as `data_asof < run_id`. `run_id` is deliberately not relabeled to the data date: `compute_benchmark.py` treats a `run_id` as the first day its board could be traded, and relabeling would hand the curve a board a session before it was published.

SPY in the Regime banner is downloaded separately and gets the same rebuild, so the trend read and the breadth read beside it stay on the same session.

## `state/universe.txt`

Cached universe list, auto-refreshed every 7 days via Yahoo's screener.

## `state/sectors.json`

Per-ticker `{sector, industry, ts}` cache. 30-day TTL per ticker. The script fetches sectors on demand for the top-N picks (not the full universe), so the cache grows as different names cycle through leadership. Deleting the file forces a clean refresh on next run.

## `state/benchmark.json`

Board-vs-index equity curves written by `scripts/compute_benchmark.py`: an equal-weight portfolio of each run-day's top 10 (`--board-n`, deliberately narrower than the displayed board), rebalanced each run-day (day *t* applies day *t−1*'s published board to close(t−1) → close(t), so no lookahead) alongside SPY and QQQ over the same dates, plus per-day price coverage.

All three lines are idealized close fills, but not equally: the board swaps ~1.8 of its 10 names a day and resets weights on the rest, while SPY / QQQ are bought once and held, so zero-cost trading subsidizes the board alone (~1.7pp over 91 run-days at 10bp round-trip). Read the gap, then discount it.

Feeds the dashboard's benchmark panel only, and the panel is simply omitted when the file is absent. `scan.py` rebuilds it right after every history save (skip with `--no-benchmark`; a failure there warns and leaves the old file rather than failing the scan), so the curves normally cover the run just recorded. Run the script by hand after a `--no-benchmark` scan, or to rebuild for a different `--board-n` / `--top-n`.

The file records both cutoffs: `board_n` is the curve's and is what the panel's label shows; `top_n` decides whose prices the `px` block carries, so an ad-hoc `--top-n 10` scan leaves the file alone rather than shrink the hover cards' prices to ten names.

Tracked in git (unlike `history.html`, which is regenerated from it): `history.csv` is tracked too, so a fresh checkout renders the dashboard straight away, and without this file it would come up missing its top panel. Written one value per line so the daily rewrite reads as an append.

The same bars also yield a `px` block (per board member × run-day: close, 14-day ATR and the highest close since the previous run-day), which is where the dashboard's hover cards get entry price, stop and trailing stop. It lives here rather than in `history.csv` for two reasons: it covers the whole recorded history the day it's added instead of filling forward from a new column, and every value comes off one auto-adjusted series, so a split between the entry day and today rescales both ends and "up 11.7% since it listed" stays arithmetic. `history.csv`'s `close` remains the as-seen record; the two disagree on names that have split since. The block dominates the file's size (three arrays per member vs four curves) and appends the same way: ~260 KB at 51 run-days × 137 members, growing with the product of the two, so expect single-digit MB after a year of daily runs.

## `state/history.html`

Self-contained HTML dashboard rendered from `history.csv` + `sectors.json` (+ `benchmark.json` when present) by `scripts/render_history_html.py`: KPI row, a board-vs-SPY/QQQ benchmark panel, a rank bump chart over every recorded run, sector-composition stacks, a date × ticker rank heatmap, a per-ticker summary table, and an EN/简中/繁中/日/한 language menu.

Hovering a trajectory vertex or a heatmap cell also gives the trade's prices, when `benchmark.json` carries the `px` block: the entry close of the spell being hovered (each spell measured against its own cost basis, so a name that left and came back doesn't read against its first ever listing), that day's close with the gain since entry, the stop a buy that day would sit behind (`close − mult × ATR`, at scan.py's own `--atr-stop-mult` default), and a trailing stop anchored to the highest close since entry.

Below-cutoff cells report the closed trade instead of a live stop: the dropout day carries the sale (under the skill's one validated exit that close IS the fill), and each grey day after it adds where the name went once it was sold, the per-name form of finding #1 in SKILL.md's "Backtested outcomes". A cell from before the name first made the board has no cost basis to measure from, so it shows its close and nothing else. Each row drops out on its own where the data doesn't reach.

The trailing stop is drawn as context (how much of an open gain is exposed), not as a rule; see "Should the trailing stop be a rule?" in SKILL.md. No external assets or network; regenerable at any time, so it's gitignored. Re-render after a scan when the user wants the visual view of the leaderboard history.
