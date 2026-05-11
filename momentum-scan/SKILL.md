---
name: momentum-scan
description: Scan US large-cap equities for smooth uptrends — high trailing return paired with shallow drawdown — and track which names persist across runs. Use when the user wants to find what's working in the market, scan for momentum, discover the next NVDA / LITE / MU-style breakout before headlines, spot leading sectors or themes (AI infra, semis, defense, lithium, etc.), surface persistent winners across runs, or compare current leaders to a prior run. Also covers re-runs and parameter tweaks ("run it again", "anything new showing up", "3 month window", "include small caps"). Do NOT use for single-ticker price or fundamentals lookups, ETF holdings, chart generation, value-investing screens, or generic explanations of momentum investing — those need other tools or plain answers.
---

# momentum-scan

Find US equities in **smooth uptrends** — high trailing return with shallow drawdown — and surface which names are durable leaders vs single-week pops. The value over a one-shot screener is **persistence tracking**: each US market day (America/New_York) is logged once to `state/history.csv` (re-running the same day refreshes that day's snapshot rather than appending), so each subsequent run can compute streak, rank changes, dropouts, and new entrants.

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2` (the script uses `format="ISO8601"`, added in pandas 2.0), `numpy`. No persistent venv needed.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`. Substitute the absolute path when running.

## Run

```bash
# Standard run — 6mo window, top 30
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy' \
  python <SKILL_DIR>/scripts/scan.py

# Shorter window catches earlier-stage breakouts
... python <SKILL_DIR>/scripts/scan.py --window-months 3

# Inspect the run history (no new scan)
... python <SKILL_DIR>/scripts/scan.py --show-history

# Machine-readable JSON output
... python <SKILL_DIR>/scripts/scan.py --format json
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--window-months` | 6 | Lookback for return + max drawdown. Shorter = earlier signals, more noise. |
| `--top-n` | 30 | How many names to display + log to history. |
| `--min-return-pct` | 30 | Filter floor on trailing return over the window. |
| `--max-dd-pct` | 20 | Filter ceiling on max drawdown (absolute value). |
| `--min-market-cap` | 5e9 | Universe market-cap floor. Lower = include small-cap rockets but more noise. |
| `--min-volume` | 1e6 | Universe avg-3mo-volume floor (liquidity filter). |
| `--universe-count` | 250 | Universe size pulled from Yahoo's screener. **Hard cap: 250** — `yf.screen` raises `ValueError` above that. Going higher requires paginating with `offset`. |
| `--refresh-universe` | (auto, 7d TTL) | Force-refresh universe (ignore cache). |
| `--no-refresh-universe` | — | Use cached universe even if past TTL (offline / testing). |
| `--show-history` | — | Dump history summary, no new scan. |
| `--clear-history` | — | Wipe `state/history.csv`. |
| `--no-save` | — | Run but don't append to history (useful for one-off exploration). |
| `--allow-same-day` | — | Keep existing rows for today's ET date instead of overwriting them (debugging / forcing multiple snapshots). |
| `--format` | markdown | `markdown` or `json`. |

## Output shape

A markdown table of the top N, plus three discovery sections. Sample (truncated):

```
# Momentum scan — 2026-05-11 16:07 UTC

**Params**: window=3mo, min_return=30.0%, max_dd=20.0%, mcap>5e+09
**Universe**: 250 tickers · **Passed filter**: 23 · **Prior runs**: 1

## Top 10

| # | Ticker | 3m% | MaxDD% | Score | Streak | RankΔ | FirstSeen | FromHigh% |
|---|---|---|---|---|---|---|---|---|
| 1 | **NOK**  | +92.4  | -8.0  | 11.6 | 2 | +5 ↗ | 2026-05-11 | 0.0  |
| 2 | **MRVL** | +109.4 | -10.8 | 10.1 | 1 | 🆕   | 🆕         | 0.0  |
| 3 | **DELL** | +107.8 | -10.8 | 10.0 | 1 | 🆕   | 🆕         | -3.8 |
| 4 | **AMD**  | +115.0 | -11.6 | 9.9  | 1 | 🆕   | 🆕         | 0.0  |
| 5 | **CIEN** | +103.5 | -16.8 | 6.2  | 2 | -7 ↘ | 2026-05-11 | 0.0  |
...

## Dropouts since last run (6)
- **ASX** (was #2, 6m=+126.6%)
- **TTE** (was #3, 6m=+45.8%)
...

## New entrants (6)
- **MRVL** at #2 (3m +109.4%, MaxDD -10.8%)
- **DELL** at #3 (3m +107.8%, MaxDD -10.8%)
...

## Persistent leaders (streak ≥ 3 runs)
- **CIEN** — streak 4, first seen 2026-04-21, now #5
```

Column meanings:

- **Score** — return ÷ |max drawdown|. Higher = more return per unit of pain.
- **Streak** — consecutive prior runs this ticker was in the top N (1 = first appearance).
- **RankΔ** — previous rank minus current rank. Positive ↗ = rising; negative ↘ = slipping; 🆕 = new entrant.
- **FirstSeen** — earliest date this ticker appeared in any past run.

The three discovery sections (dropouts / new entrants / persistent leaders) are computed against the most recent prior run.

## How to interpret (Claude's job after running)

The script gives you data; the user wants signal. Add a short interpretation pass — apply judgment, don't recite the principles below blindly.

1. **Sector clusters beat individual names.** Momentum arrives as a theme (AI infra, semis, defense, lithium, etc.). Group the top 10–15 by sector and call out the cluster — that's what the user can research, hedge, or fade. New entrants joining an existing cluster confirm the theme; isolated newcomers in unrelated sectors are more likely noise.

2. **Streak ≥ 4 and top-5 dropouts are the real signals.** Long streaks have survived multiple periods of market noise — these are the durable trends the rank score alone can't surface. A name leaving the top 5 usually marks a broken trend (max drawdown blew through the filter) and is often the leading edge of a regime shift.

3. **Never recommend specific buys.** Frame results as "names worth investigating", not "you should buy". Always flag that momentum strategies carry multi-year underperformance risk — 2023 was a textbook momentum crash where the 2022 leaders (energy) lost to a completely different cohort (mega-cap tech) for the entire year.

## State files

- `state/history.csv` — one snapshot per US market day (America/New_York) × every top-N ticker. Columns: `run_id, run_date, ticker, rank, score, return_pct, max_dd_pct, ann_vol_pct, from_high_pct`. Re-running the same ET day overwrites that day's rows (newer prices replace older), so streak counts scan-days rather than scan invocations. Writes are atomic (tmp file + rename) so a crash mid-write can't truncate the file. The whole point of the skill is to build this up over time — first run is informationally thin; the skill gets more useful with each subsequent run.
- `state/universe.txt` — cached universe list, auto-refreshed every 7 days via Yahoo's screener.

If the user wants to start fresh, `--clear-history` wipes only `history.csv` (no confirmation prompt — pair with `git` if irreversibility matters). The universe cache regenerates automatically.

Storage growth: at default `--top-n 30`, each run adds ~30 rows × ~120 bytes ≈ 3.6 KB. A year of daily runs is ~1.3 MB, weekly is ~190 KB. Negligible for years of typical use; if it ever matters, prune by `run_date` with any CSV tool.

Tests for the history I/O live next to the script at `scripts/test_history.py`. Run from the skill root with:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy' \
  --with 'pytest' pytest scripts/
```

## Cadence

Cadence-agnostic by design. At most one snapshot is kept per US market day (America/New_York), so streak always counts **consecutive prior scan-days** containing this ticker — running multiple times on the same ET day just refreshes that day's entry. Aligning to ET date instead of UTC matches what the underlying data represents (US market sessions) and behaves predictably across DST transitions. The `FirstSeen` dates tell you the natural granularity:

- Daily runs → streak unit is days. Finest granularity but typically adds limited extra signal over weekly.
- Weekly runs → streak unit is weeks. Recommended sweet spot — captures trend formation 4× faster than monthly while smoothing daily noise.
- Monthly runs → streak unit is months. Smoothest, slowest signal; matches the cadence of the original backtest in this conversation.

For automatic recurring runs, use a local scheduler (macOS `launchd` LaunchAgent, or `cron`) pointed at `scripts/scan.py`. The `schedule` skill runs *remote* agents in Anthropic-managed sandboxes that can't see this local `state/` directory, so it doesn't fit this use case.

## Known limitations

- **Survivorship bias** — universe is current US large caps; delisted names (Lehman, SVB, etc.) are absent. Backtested CAGR is 1–2% optimistic vs. a true point-in-time universe.
- **Pre-cost** — no transaction costs, slippage, or taxes modeled. Real execution shaves another ~0.5–1% CAGR.
- **mcap floor at $5B** — small-cap moonshots are excluded by default; bump `--min-market-cap 1e9` to widen if the user wants to see them.
- **6mo window misses fresh breakouts** — use `--window-months 3` for earlier-stage signals at the cost of more noise.
- **Yahoo data quirks** — rare missing bars, occasional late dividend adjustments. If a single name looks wrong, sanity-check it via the `yfinance` skill's `fast_info` mode.
