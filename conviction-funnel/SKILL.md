---
name: conviction-funnel
description: End-to-end "scan → validated pockets → buyable picks" funnel — chains regime-scan (market gate) → momentum-scan (names) → a direct read of base-breakout / mean-reversion state for their backtest-validated pockets (BaseWks ≥ 20 bases; fresh high-score oversold listings), then deep-dives the top N (default 3) into actionable entry / stop / size / invalidation briefs with regime threaded into sizing. Use whenever the user wants the whole pipeline from "what's the market doing" to "3 names I could actually buy, with where to get in and bail" — e.g. "what should I buy today", "give me 3 high-conviction picks", "run the funnel", "scan to picks", "best risk/reward setups with entries and stops". The orchestration layer ABOVE the individual scans — NOT for a single scan re-run (use that scan directly), a single-ticker lookup (use yfinance), or a pure market-health read (use regime-scan).
---

# conviction-funnel

Turn a market full of noise into a *small* set of researched, actionable names. The premise: any single scan can fire on a fluke, and a momentum name is usually already extended by the time it ranks — so picking off one screener tends to buy tops. The funnel runs the market gate, pulls the name lists, cross-references them, then spends real research effort on only a handful — ending in a side-by-side table of where to enter, where the stop goes, how big to size, and what kills the thesis.

⚠️ **How the 2026-05→07 backtests changed the middle step.** The sister scans carry outcome backtests, and the old "2–3 scans agree = conviction" premise did not survive them: overlap count ranked conviction **backwards** (3-scan names −4.5% excess at T+10 vs −1.5% for single-scan), with unusual-options co-flags marking *froth*, not smart money. The two skills built on that premise — cross-scan and unusual-options-scan — were **retired in 2026-07** on those numbers. The funnel keeps the same shape but the middle step is now a pure file read: momentum's list plus each surviving scan's *validated pockets* form the candidate pool, and the per-scan validated filters (BaseWks ≥ 20; MR Score ≥ 40 on a fresh listing; volume-backed momentum entries) do the ranking that overlap counting used to do.

It orchestrates skills the user already has rather than re-implementing anything:

```
regime-scan   ── market gate: 🟢/🟡/🔴 + divergence flags  (are we even adding risk?)
momentum-scan ── primary name list + per-name buyability (Sig), stops, persistence
sister CSVs   ── base-breakout + mean-reversion validated pockets (file read, no re-scan)
   │
   ▼  select top-N by a risk/reward lens
yfinance + edgartools + web + (conditional) wallstreetbets
   │
   ▼  per-name entry / stop / size / invalidation, regime threaded into sizing
   │
   ▼  state/runs/<date>.json ledger → scripts/grade_outcomes.py (quarterly)
```

Default N is **3**. The user can ask for more ("give me 5") or fewer.

`<SKILLS_DIR>` below is the parent directory holding the sister-scan folders. Default to `~/.claude/skills` (the individual scan folders there are symlinks into the user's skills repo, so reading/writing state is consistent). If the scans aren't found there, fall back to the repo those symlinks point at — follow `~/.claude/skills/momentum-scan` to its target (on this install, `/Users/matthew/GitHub/skills`).

## Why this order (don't reshuffle without reason)

The sequence isn't arbitrary — each step changes how you read the next:

1. **regime-scan first** because it's a *gate*, and it's cheap (~516 tickers, one batched download). If the market is 🔴 RISK-OFF with stacking divergences, the whole exercise changes character (you're looking for what's *holding up*, sized tiny, not what to chase) — and you might decide to defer the expensive deep-dives entirely. Knowing the regime first means you read the name list with a frame already in place, instead of picking names and then discovering the tape is rolling over. regime-scan is also a strictly richer read than momentum-scan's built-in `--regime-gate` banner: the banner is a 2-state trend+breadth gate, while regime-scan adds the 🟡 middle state, the divergence/turn flags, VIX term structure, credit, and cross-day slope. Use the banner as a free sanity check; use regime-scan for the actual gate.
2. **momentum-scan second** because it's the primary name source *and* the only place you get the per-name buyability signals (the `Sig` column, MA20%, RSI, ATR stop) that the selection step leans on — the sister CSVs carry ranks/scores, not `Sig`/stop fields, so you must run momentum-scan itself to see them.
3. **sister pockets third** because they change how you read the momentum list — a momentum name also sitting in a validated base (BaseWks ≥ 20) or a fresh high-score mean-reversion listing has a better entry story, and a name on *all three* lists gets a crowding warning, not a conviction bonus. This is a pure file read of the `history.csv` snapshots the daily job already writes — no network, no re-scan.

## Step 1 — regime gate

Check whether today's snapshot already exists; if so you can read it instead of re-fetching. **It's already scanned today if the latest `run_date` in `<SKILLS_DIR>/regime-scan/state/history.csv` matches today's ET date** — in that case use `--show-history` to read the state + slope without a re-fetch. Otherwise run it:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILLS_DIR>/regime-scan/scripts/scan.py
# read-only (today already scanned, just want the state + slope):
# ... python <SKILLS_DIR>/regime-scan/scripts/scan.py --show-history
```

**Read the banner and decide the tone for everything downstream — sizing reads the `Confirmed (2-day)` line, not the raw daily state.** The raw label chatters at the score threshold (6 of the first 10 logged transitions were single-day whipsaws — regime-scan's **Signal quality & outcomes** section has the numbers), so the banner carries both: when they disagree, today is a first-day flip — size off the confirmed state and note the flip in the output ("regime flipped to CAUTION today, unconfirmed — sizing still per RISK-ON, tighten if it confirms tomorrow").

- 🟢 **RISK-ON** (confirmed) → proceed normally, normal sizing.
- 🟡 **CAUTION** (confirmed; ≥2 divergences) → still proceed, but bias hard toward 🟢/🔵 pullback entries, smaller size, tighter stops. This is exactly the state a risk/reward lens cares most about — not "don't act", but "be choosier".
- 🔴 **RISK-OFF** (confirmed; gate off, or ≥4 internals broken under intact price) → consider stopping here. If you continue, frame finalists as "what's holding up", size minimal, and say so plainly. **Exception: a raw first-day flip to RISK-OFF with the gate off (SPY lost its 200DMA) is not a whipsaw candidate to wait out — the trend gate is mechanical; respect it same-day.**

Also note the **breadth** number even when no divergence fires: breadth in the mid-50s% with RSP/SPY narrowing is a *healthy-but-narrow, mega-cap-led* tape — not a flag, but a reason to deliberately diversify the final picks away from whatever's crowded (usually tech). Carry this conclusion forward; it feeds both selection (step 4) and sizing (step 5).

## Step 2 — momentum name list

```bash
# --verbose: the funnel's selection lens reads the diagnostic columns
# (MA20%, RSI, AnnVol%) that the slim default table hides
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/momentum-scan/scripts/scan.py --verbose
```

Note for later: the `Sig` column is the per-name buyability read (🟢 buy zone / 🔵 deep pullback / 🟡 in-trend / 🟠 stretched / 🔴 overextended), and momentum lists frequently come back mostly 🔴 — which is *itself* the warning that buying the raw leaderboard means chasing. Note the sector concentration too (e.g. "Tech 23 of 30") — it tells you which way to diversify in step 4.

Ignore the breadth figure in momentum-scan's own `Regime` banner — it uses a different, tech-tilted pool and a different MA, so it can print something alarming like "~25% > 200DMA" right next to regime-scan's "58%". They're not contradictory; defer to regime-scan's breadth (step 1) and don't let the momentum banner's lower number trigger a false 🔴 scare.

## Step 3 — sister-scan validated pockets (file read)

No script to run here — read the two sister CSVs the daily job already writes, filtered to their latest `run_date`:

```
<SKILLS_DIR>/base-breakout-scan/state/history.csv    # base_weeks, to_pivot_pct, base_score
<SKILLS_DIR>/mean-reversion-scan/state/history.csv   # score (freshness derived from prior run_ids)
```

Extract three things:

- **Base pocket** — names with `base_weeks ≥ 20` (the one validated base edge; the composite `base_score` did not discriminate outcomes), noting `to_pivot_pct` for entry proximity.
- **MR pocket** — names with `score ≥ 40` listed for the **1st–2nd consecutive run** (check whether the ticker appears under the immediately-preceding `run_id`s; 3rd+ consecutive listings ran negative).
- **Crowding check** — names on momentum's list AND both pockets simultaneously. The mom+base+mr triple was the *worst* labeled cell in the overlap backtest (n=15, −8.0% xT+10, Beat10 10%) — treat as a crowding warning, never a conviction bonus.

Freshness rule: if a CSV's latest `run_date` is >3 sessions old (weekend save-skips are normal), either re-run that scan or downgrade its pocket to informational and say so.

## Step 4 — select the top N (risk/reward lens)

This is judgment, not a formula — but the priority order below was rebuilt on the sister scans' 2026-05→07 outcome backtests (each scan's **Backtested outcomes** section carries the full numbers). It's tuned for "best *current* risk/reward" — entry quality and tight invalidation, not the highest-octane name:

1. **Build the candidate pool from momentum's list plus the step-3 pockets — never from overlap counting.** A momentum name that also sits in one pocket is a fine candidate (those cells ran ~neutral in the overlap backtest, Beat10 54–55%) — **best when the co-listing is fresh**: 1st–2nd-session overlaps ran −1.6% xT+10 vs −4.4% by the 4th+ consecutive session (check the prior `run_id`s in the sister CSV). A base+MR co-listing with no momentum presence is the *weakest* pair (−2.65% xT+10, Beat10 37%, n=55) — admit it only when both rule-3 and rule-4 gates pass. A strong single-scan name that passes its own validated filter (rules 3–5) beats a stale co-listing that doesn't.
2. **A name on all three lists is the crowding representative, not the standout.** Don't auto-lead with it — see the step-3 crowding numbers. It makes the finalists only if it independently passes rules 3–6, and its brief must say the crowd is already there.
3. **Rank base-pocket names by base length, not base score.** BaseWks ≥ 20 is base-breakout's one big validated edge (75% win at +20 sessions vs 45% baseline); the composite base Score did **not** discriminate outcomes. Rank on **`base_weeks` first, then smaller `to_pivot_pct`** (entry trigger near = tight invalidation). Don't prize deep volume dry-up (inverted in-sample) and skip any entry that gaps >3% past the pivot. Base-only candidates lack a momentum `Sig`/ATR-stop — say so in their brief.
4. **Gate mean-reversion-pocket names on Score ≥ 40 and a 1st–2nd-day listing** (step 3 already applies this — re-verify, don't re-derive). That combined filter ran +1.83%/signal (~3× baseline); 3rd+ consecutive listings ran negative. A `momentum+mean-reversion` "pullback in a leader" that fails this gate isn't a buyable dip — it's a name that stopped bouncing ("stuck oversold").
5. **Prefer momentum `Sig` 🟢/🔵; downgrade 🔴 overextended.** A name that only clears via a vertical, RSI-80 move is a worse entry than one basing quietly. For fresh momentum entrants, check the entry-quality tag — the validated half is **`dist_days_25d` ≤ 1** (clean entries doubled tenure and top-10 reach in momentum's re-runnable backtest); the volume half (`vol_ratio_20d`) is only a weak priority hint — the once-quoted +9.0% vs +3.1% gap was convention-inflated, re-measured at ~0.5pt (see momentum-scan's **Backtested outcomes** #2–3). Both fields sit on the name's *first* run-day row in `<SKILLS_DIR>/momentum-scan/state/history.csv`.
6. **Prefer a tight ATR stop (≤ ~8%) and low AnnVol.** Tight invalidation is the whole point.
7. **Diversify sectors, and step out of the crowded cohort.** If momentum is tech-heavy and regime flagged a narrow tape, deliberately favor non-tech candidates — concentration risk is real and a narrowing tape pulls sponsorship from the crowded names first.

Then **thread the regime conclusion in**: 🟢 → these are buy candidates at normal size; 🟡 → only the pullback-entry ones, smaller; 🔴 → observe, or minimal size with an explicit caveat.

Name the finalists with one line each on *why they made the cut*, and name the runner-ups so the user can swap one out before the expensive deep-dive runs.

## Step 5 — standard-depth deep-dive on the finalists

Deep-dive the N finalists **in parallel** — spawn one subagent per finalist (they're independent, and a single agent doing all N serially is much slower). Hand each agent the scan context you already have (ranks, scores, current `Sig`, approximate spot from the ATR-stop math) so it doesn't re-derive, plus the regime conclusion so it frames sizing. **If you can't spawn subagents** (some harnesses don't allow it), just run the same per-finalist brief yourself, one name at a time — the template is identical, it's only slower. Don't skip a finalist for lack of parallelism.

The full per-agent prompt template — including the exact 7-section brief structure and the **conditional WSB rule** — is in `references/deep-dive-template.md`. Read it and fill in the per-ticker blanks before spawning. The headline points:

- Every brief leads with **trend/stop snapshot**, then the **next earnings/event date** (the single biggest hidden risk for a swing entry — an entry days before a print is a different trade), then fundamentals, SEC filings + insider activity, the catalyst-and-bear-case, crowding, and a **risk/reward verdict** (entry zone, stop, rough R-multiple, sizing note, one-line invalidation).
- **The WSB crowding check is conditional, not automatic.** Crowding is a fragility signal — it only matters when a name is plausibly a retail darling. Run it only when a finalist is in a hot retail theme (semis/AI/software, nuclear, space/defense, crypto-adjacent, EV, biotech-momentum), OR has high AnnVol (>~70%), OR is a big recent run with hot RSI (>70). For a sleepy institutional name (low vol, value sector, modest RSI) skip it and default to "low crowding" — it would be a near-tautological no-op and isn't worth the call. When it *does* run, the lightweight "is this name on WSB's radar at all" read is enough; only browse actual threads (the full wallstreetbets skill) if the user wants the sentiment detail.

## Output — the comparison table

Synthesize the briefs into one side-by-side table so the user can compare at a glance (lead with the visual; this user reads compact tables faster than prose). Use these rows, adapting as needed:

```
| | <T1> | <T2> | <T3> |
|---|---|---|---|
| Sector | | | |
| Signals | (which scans list it + which validated pockets it passes; ⚠️ if on all three) | | |
| Spot | | | |
| Trend | (vs 20/50/200DMA, dist from high) | | |
| ⚠️ Earnings | (date + weeks out; flag if <4wk) | | |
| Valuation | | | |
| Analyst vs spot | | | |
| Stop | (price, %) | | |
| Risk/reward | (R-multiple + glyph 🟢/🟡/🔴) | | |
| Crowding | | | |
| Key risk | | | |
| Verdict | ✅ / ⚠️ + one line | | |
```

For the **Risk/reward** row, anchor the R-multiple to a *defensible upside* — the analyst high target or a chart level — and state which. Don't anchor it to the analyst *mean* target when price already sits there: that's exactly the "reward capped at consensus" case (it makes R look like ~0), and the right move is to say the mean is already reached and measure R to a higher bull-case level instead.

Follow the table with a 2–3 sentence-per-name plain-language verdict, then a short **"what the funnel did"** recap that makes the value explicit — e.g. how the deep-dive *changed* the picture vs the raw scan signals (a name that looked great on signal agreement but turned out to have capped reward or a deteriorating fundamental backdrop). That recap is often the most useful part: it shows why "appears in N scans" ≠ "buy" — now backtest-quantified, since overlap count alone ranked outcomes backwards.

Close with concrete next-step offers: swap a finalist for a runner-up and re-dive; persist the theses (`/commit-invest` if available); or go deeper on one name (full `/deep-research`).

## Step 6 — write the run ledger (non-optional)

The funnel is the only layer of the pipeline that says "buy here, bail there" — and until 2026-07 its output evaporated the moment it was printed, so "does the funnel add value over the raw scans" was unanswerable. Every run now ends by transcribing the final table into `state/runs/<YYYY-MM-DD>.json`. **A run that isn't logged never happened**: the sample for the quarterly grade only accrues when the file is written.

Log **all three roles** — the finalists with their full plans, the runner-ups, and the names the deep-dive *rejected* (with a one-line `reason`). Rejections are predictions too: if rejected names outperform finalists, the deep-dive is subtracting value, and only the ledger can catch that. Record what the table *said*, not what you hope happens — entry type/level, stop, and size exactly as briefed.

```json
{
  "run_date": "2026-07-31",
  "regime": {"state": "RISK-ON", "score": 6, "flags": "..."},
  "picks": [
    {"ticker": "ELV", "role": "finalist", "tags": ["momentum", "mr-pocket"],
     "spot": 412.5, "verdict": "✅",
     "entry": {"type": "pullback-limit", "level": 405.0},
     "stop": 389.0, "size": "normal",
     "upside_ref": {"level": 460.0, "basis": "analyst high"},
     "invalidation": "loses 200DMA on volume"},
    {"ticker": "CIEN", "role": "runner-up", "tags": ["momentum"], "spot": 118.0},
    {"ticker": "MU", "role": "rejected", "tags": ["momentum", "mr-pocket"],
     "spot": 739.0, "reason": "earnings in 2 days, reward capped at consensus"}
  ]
}
```

Field rules (the validator is the source of truth — the full schema lives in `scripts/grade_outcomes.py`'s docstring): `role` ∈ finalist / runner-up / rejected; `tags` ⊆ momentum / base-pocket / mr-pocket; `spot` = the reference close you worked from; finalists additionally require `verdict` (✅/⚠️), `entry` (`market` fills next open — omit `level`; `pullback-limit` / `pivot-stop` require it), `stop` (below the entry reference), and `size` (normal / half / minimal, regime-threaded). Two conventions the schema can't enforce: `entry.level` takes **one number, not a zone** — when the brief gives an entry zone, record its midpoint (or the specific trigger the brief named, if it named one) so hand-written ledgers stay comparable; and the filename is one-per-ET-date, so a second funnel run the same day **deliberately overwrites** that day's ledger — the later run is the day's view of record. After writing, self-check:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/conviction-funnel/scripts/grade_outcomes.py \
  --validate <SKILLS_DIR>/conviction-funnel/state/runs/<date>.json
```

Quarterly (with the sister scans' `backtest_outcomes.py` reruns), grade the accumulated ledger — selection quality (finalists should beat runner-ups should beat rejected, all vs SPY) and execution quality (fill rate, stop-hit rate, realized R with gap-aware exits):

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/conviction-funnel/scripts/grade_outcomes.py
```

Until the ledger holds ~30+ finalists across regimes, read the grade's per-run recap rather than its aggregate tables — the script says so in its own legend.

## Honesty rules (carry these through the whole funnel)

- **Never frame output as "buy this".** These are *prioritized research candidates with risk parameters*, not advice. Say so.
- **Quote the backtest numbers when rejecting a crowded name.** "In 3 scans" *sounds* bullish; the measured record says the opposite (−4.5% xT+10). Stating the number is what keeps the funnel honest against its own old framing — and it explains to the user why the obvious-looking pick got demoted.
- **Report the tape faithfully.** If regime is 🟡/🔴, lead with that, don't bury it under exciting names.
- **Surface what the deep-dive killed.** The funnel's job is as much to *reject* plausible names as to surface good ones — a leaderboard name with reward already capped at the analyst target, or a fresh oversold listing whose sector backdrop is still deteriorating, is a finding worth stating loudly.
- **The single-run caveat:** the scans get sharper with history (streaks, slopes). A first-ever run is informationally thin; lean harder on the fundamental deep-dive when the scan history is short.
- **No ledger, no run.** Step 6 is part of the funnel, not an optional epilogue — skipping it silently deletes the sample the quarterly grade needs, and it's precisely the runs that felt unremarkable that keep the grade honest.

## Quick reference — the whole funnel

```bash
# 1. regime gate (read --show-history if today already scanned)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILLS_DIR>/regime-scan/scripts/scan.py

# 2. momentum name list (+ per-name Sig / stops; --verbose for the
#    MA20/RSI/AnnVol diagnostics the selection lens uses)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/momentum-scan/scripts/scan.py --verbose

# 3. sister pockets — pure file reads, no script (see Step 3)
#    base-breakout-scan/state/history.csv   → base_weeks ≥ 20 pocket
#    mean-reversion-scan/state/history.csv  → score ≥ 40 fresh-listing pocket

# 4. select top-N by risk/reward lens (judgment — see Step 4)
# 5. parallel deep-dive subagents per finalist (see references/deep-dive-template.md)

# 6. write state/runs/<date>.json (finalists + runner-ups + rejected), then self-check:
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/conviction-funnel/scripts/grade_outcomes.py --validate \
  <SKILLS_DIR>/conviction-funnel/state/runs/<date>.json
# quarterly: grade the accumulated ledger (no args)
```
