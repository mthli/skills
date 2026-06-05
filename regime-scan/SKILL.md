---
name: regime-scan
description: Scan the whole US market once a day for trend direction and sentiment turns — folds index trend, breadth, VIX term structure, credit spreads, and defensive rotation into one 🟢/🟡/🔴 state plus divergence flags, logging each day so the slope (the real turn signal) shows across runs. Use to gauge market direction and whether sentiment is rolling over (index near highs but internals weakening) before adding risk vs raising cash. Triggers on "scan the market", "market trend", "sentiment turn", "market health check", "is the market topping", "risk-on or risk-off", "breadth", "market regime", "tape health", "should I de-risk". The level ABOVE momentum-scan — that finds which NAMES work; this judges whether the MARKET is healthy. NOT for single-ticker analysis (use yfinance), picking stocks to buy (use momentum-scan / base-breakout-scan), or generic market-timing explanations.
---

# regime-scan

A **market-level** daily read — the layer above the four name-level scans. It answers three questions that move at different speeds:

1. **Trend** — where is the primary trend? (slow: weeks–months)
2. **Breadth** — is the trend healthy or narrowing? (the early-warning layer — divergences show up here first)
3. **Sentiment / Credit** — is fear/greed stretched, is positioning fragile? (fast: days)

The core insight it encodes: **a turn almost always shows up as "internals deteriorate while the index still prints highs."** So beyond a snapshot, it raises explicit **divergence flags** (breadth not confirming, equal-weight lagging, credit rolling over, defensive rotation, vol-curve inversion) — and because each US market day is logged once to `state/history.csv`, the **slope** of breadth / VIX / credit across days (the real turn signal) is visible, not just today's number.

This formalizes the "regime gate + cohort dashboard" idea already scattered through `methodology.md` (2026-06-03) and the breadth/narrowing observations in `macro.md` into one daily action.

## What it pulls (all yfinance, ~516 tickers, one batched download)

- **Indices**: SPY, QQQ vs 50/200DMA + 200DMA slope; RSP/SPY (equal- vs cap-weight = the narrowing proxy)
- **Breadth**: % of the **S&P 500 universe** (`state/breadth_universe.txt`, ~500 names across all 11 sectors, regenerated quarterly by `build_universe.py`) above their 50/200DMA, plus 52-week new-highs − new-lows
- **Vol / sentiment**: ^VIX level + 5-day change; ^VIX/^VIX3M **term structure** (backwardation = acute stress)
- **Credit / rotation**: HYG/LQD (HY vs IG — credit usually leads equities at turns); defensive (XLU/XLP/XLV) vs offensive (XLK/XLY/XLC) relative strength

**Dependencies** (auto-fetched by `uv run --with`): Python ≥ 3.10, `yfinance>=1.3,<2`, `pandas>=2` (which pulls in numpy transitively — the script uses only pandas/yfinance). No persistent venv.

`<SKILL_DIR>` below is the directory containing this `SKILL.md`.

## Run

```bash
# Standard daily run
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/scan.py

# Inspect the daily state log (no new scan) — watch the slope of state/breadth/vix
... python <SKILL_DIR>/scripts/scan.py --show-history

# Machine-readable
... python <SKILL_DIR>/scripts/scan.py --format json

# Longer slope/relative-strength window (default 20 sessions ≈ 1 trading month)
... python <SKILL_DIR>/scripts/scan.py --lookback 30
```

## Parameters

| Flag | Default | Notes |
|---|---|---|
| `--lookback` | 20 | Sessions for slope / relative-strength windows (RSP/SPY, credit, rotation, 200DMA slope). ~1 trading month. Raise for slower, less noisy reads. |
| `--format` | markdown | `markdown` or `json`. |
| `--show-history` | — | Print the daily state log and exit. |
| `--clear-history` | — | Wipe `history.csv` (no confirmation). |
| `--no-save` | — | Don't append this run to history. |
| `--save-stale` | — | Save even on a weekend / NYSE holiday (default skips so duplicate-data days don't pollute the trajectory). |

## Refreshing the breadth universe

The breadth pool is the **live S&P 500 constituents**, pulled from Wikipedia — yfinance can't return index membership (`^GSPC` exposes no constituents attribute; an ETF's `funds_data.top_holdings` caps at the top 10). It's a **quarterly snapshot, not a per-run fetch**: breadth's whole signal is the *slope* of "% above 50DMA" across days, so churning the universe between runs would inject compositional noise into that slope. Freeze it within a quarter; regenerate only at quarter boundaries.

```bash
# Regenerate state/breadth_universe.txt  (+ a dated state/breadth_universe.<YYYY>-Q<N>.txt snapshot)
uv run --with 'pandas>=2' --with lxml --with requests \
  python <SKILL_DIR>/scripts/build_universe.py

# Preview the per-sector breakdown, write nothing
... python <SKILL_DIR>/scripts/build_universe.py --dry-run
```

The generator dash-normalizes class shares (`BRK.B`→`BRK-B`), prints a per-sector count, and backs the prior list up to `breadth_universe.bak.txt`. As a guard it **refuses to overwrite the live file if it parses fewer than 400 names** (a truncated fetch or a Wikipedia re-layout) — so a bad fetch can't silently shrink the breadth pool the daily scan depends on. The original hand-curated 97-name list is preserved at `state/breadth_universe.legacy97.txt`. `scan.py` reads every non-`#` line of `breadth_universe.txt`, so you can also hand-edit it (a `#` header is ignored).

## How to read the output

**State banner** — one of three, in escalation order (mirrors the methodology ladder):

- 🟢 **RISK-ON** — trend gate on + layers confirm + ≤1 divergence. *Trend healthy, hold per rules; new money can scale in on pullbacks.*
- 🟡 **CAUTION** — trend still up but ≥2 divergence flags (or breadth weakening). *Tighten trail stops, raise cash buffer; new money only on pullbacks, never chase 🔴.*
- 🔴 **RISK-OFF** — either the trend gate is off (SPY below / rolling 200DMA), **or** price is still above 200DMA but ≥4 internals broke (the "price hasn't dropped yet but internals are already rotting" late-stage top). *Cut gross exposure, let trail stops take over; don't bottom-fish an unconfirmed bounce.*

**Score** — sum of 10 per-signal votes (🟢 +1 / ⚪ 0 / 🔴 −1) across the four layers. A blunt gauge; the **divergence flags and the trajectory matter more than the absolute score**.

**⚠️ Turn warnings (divergence flags)** — the turn detector. Each fires *only while the uptrend is intact* (a broken internal under an already-broken tape is just the bear, not a divergence):
- Breadth divergence — SPY near its 52w high but < 50% of names above their 50DMA
- Narrowing rally — RSP/SPY falling (mega-cap-only rally)
- Credit weakening — HYG/LQD rolling over while stocks hold
- Defensive rotation — defensives outrunning offensives
- Vol-curve inversion — VIX > VIX3M
- VIX 5-day spike

None alone is a sell; **2–3 stacking = de-risk**. This is the layer the user explicitly wanted: catch sentiment *turning* before price confirms.

**Recent trajectory** — last ~6 runs of state / breadth / RSP-SPY / VIX / credit. **Read the slope, not the single point** — a one-day snapshot can't tell you if sentiment is turning; the multi-day drift can.

## How it ties to the journal rules

- The trend gate (SPY > rising 200DMA) is the mechanical **"market top"** from `methodology.md` 2026-06-03 — it doesn't predict the top, it flips you out.
- The divergence flags are the **early warning** that says *tighten trail stops / raise cash* (🟡), not *dump everything*. Consistent with "don't predict the top — build a position that survives without predicting it."
- Breadth < ~60% + gate-off is the **kill switch** (`methodology.md` 2026-06-03): actively cut gross exposure.
- Pairs with `regime-scan` → `momentum-scan`: read the **market** here first (are we in? add or trim?), then read **names** there.

## Known limitations

- **Breadth universe is the S&P 500 (~500 names), a quarterly snapshot** — not the full ~4,000-name market, and it applies *today's* membership to past prices (a mild survivorship bias that's standard and acceptable for a forward-looking gauge, not a backtest). Broad enough to smooth the breadth % and self-refreshing via `build_universe.py`; edit `state/breadth_universe.txt` to retune.
- **No intraday / real-time** — uses daily closes (last available session on weekends/holidays; those aren't saved unless `--save-stale`).
- **Votes are mechanical** — thresholds are conventional, not optimized. Treat the output as a structured *dashboard to interpret*, not a trade signal. The signal compounds across **days of history**; a single run is just a snapshot.
- **Credit via HYG/LQD ETF ratio** is a proxy for the OAS spread, good enough for direction but not a substitute for actual HY option-adjusted spreads.

## Tests

```bash
cd <SKILL_DIR>/scripts && uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  --with pytest pytest test_classify.py -q
```

Pure-logic tests (no network) cover the state machine, every divergence flag, the vote helper, and the breadth/new-high-low math.
