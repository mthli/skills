---
name: conviction-funnel
description: End-to-end "scan → consensus → buyable picks" funnel — chains regime-scan (market gate) → momentum-scan (names) → cross-scan (overlap map: clean tech+tech confirmation vs options-froth crowding), then deep-dives the top N (default 3) into actionable entry / stop / size / invalidation briefs with regime threaded into sizing. Use whenever the user wants the whole pipeline from "what's the market doing" to "3 names I could actually buy, with where to get in and bail" — e.g. "what should I buy today", "give me 3 high-conviction picks", "run the funnel", "scan to picks", "best risk/reward setups with entries and stops". The orchestration layer ABOVE the individual scans — NOT for a single scan re-run (use that scan directly), a single-ticker lookup (use yfinance), or a pure market-health read (use regime-scan).
---

# conviction-funnel

Turn a market full of noise into a *small* set of researched, actionable names. The premise: any single scan can fire on a fluke, and a momentum name is usually already extended by the time it ranks — so picking off one screener tends to buy tops. The funnel runs the market gate, pulls the name lists, cross-references them, then spends real research effort on only a handful — ending in a side-by-side table of where to enter, where the stop goes, how big to size, and what kills the thesis.

⚠️ **How the 2026-05→07 backtests changed the middle step.** The five sister skills now carry outcome backtests, and the old "2–3 scans agree = conviction" premise did not survive them: overlap count ranked conviction **backwards** (3-scan names −4.5% excess at T+10 vs −1.5% for single-scan), mostly because co-appearing in unusual-options marks *froth*, not smart money (see cross-scan's **Backtested outcomes**). The funnel keeps the same pipeline but uses convergence differently: technical+technical overlaps and each scan's *validated pockets* form the candidate pool, the `uoa` column acts as a red flag, and the per-scan validated filters (BaseWks ≥ 20; MR Score ≥ 40 on a fresh listing; volume-backed momentum entries) do the ranking that overlap counting used to do.

It orchestrates skills the user already has rather than re-implementing anything:

```
regime-scan   ── market gate: 🟢/🟡/🔴 + divergence flags  (are we even adding risk?)
momentum-scan ── primary name list + per-name buyability (Sig), stops, persistence
cross-scan    ── which names show up in 2+ of the four sister scans (the overlap map)
   │
   ▼  select top-N by a risk/reward lens
yfinance + edgartools + web + (conditional) wallstreetbets
   │
   ▼  per-name entry / stop / size / invalidation, regime threaded into sizing
```

Default N is **3**. The user can ask for more ("give me 5") or fewer.

`<SKILLS_DIR>` below is the parent directory holding the sister-scan folders. Default to `~/.claude/skills` (the individual scan folders there are symlinks into the user's skills repo, so reading/writing state is consistent). If the scans aren't found there, fall back to the repo those symlinks point at — follow `~/.claude/skills/momentum-scan` to its target (on this install, `/Users/matthew/GitHub/skills`).

## Why this order (don't reshuffle without reason)

The sequence isn't arbitrary — each step changes how you read the next:

1. **regime-scan first** because it's a *gate*, and it's cheap (~516 tickers, one batched download). If the market is 🔴 RISK-OFF with stacking divergences, the whole exercise changes character (you're looking for what's *holding up*, sized tiny, not what to chase) — and you might decide to defer the expensive deep-dives entirely. Knowing the regime first means you read the name list with a frame already in place, instead of picking names and then discovering the tape is rolling over. regime-scan is also a strictly richer read than momentum-scan's built-in `--regime-gate` banner: the banner is a 2-state trend+breadth gate, while regime-scan adds the 🟡 middle state, the divergence/turn flags, VIX term structure, credit, and cross-day slope. Use the banner as a free sanity check; use regime-scan for the actual gate.
2. **momentum-scan second** because it's the primary name source *and* the only place you get the per-name buyability signals (the `Sig` column, MA20%, RSI, ATR stop) that the selection step leans on. cross-scan only reads each scan's saved `history.csv` (ranks/scores) — it does **not** carry the `Sig`/stop fields — so you must run momentum-scan itself to see them.
3. **cross-scan third** because the overlap map changes how you read the name list — it tells you which momentum names are *cleanly confirmed* by a second technical signal and which are sitting in options-froth territory. It re-reads all four scans' latest snapshots and surfaces the 2+-scan overlap. Run it with `--refresh` so any *other* sister scan (base-breakout / mean-reversion / unusual-options) that's gone stale gets regenerated first — momentum you just ran, so it won't be re-run.

## Step 1 — regime gate

Check whether today's snapshot already exists; if so you can read it instead of re-fetching. **It's already scanned today if the latest `run_date` in `<SKILLS_DIR>/regime-scan/state/history.csv` matches today's ET date** — in that case use `--show-history` to read the state + slope without a re-fetch. Otherwise run it:

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILLS_DIR>/regime-scan/scripts/scan.py
# read-only (today already scanned, just want the state + slope):
# ... python <SKILLS_DIR>/regime-scan/scripts/scan.py --show-history
```

**Read the banner and decide the tone for everything downstream:**

- 🟢 **RISK-ON** → proceed normally, normal sizing.
- 🟡 **CAUTION** (≥2 divergences) → still proceed, but bias hard toward 🟢/🔵 pullback entries, smaller size, tighter stops. This is exactly the state a risk/reward lens cares most about — not "don't act", but "be choosier".
- 🔴 **RISK-OFF** (gate off, or ≥4 internals broken under intact price) → consider stopping here. If you continue, frame finalists as "what's holding up", size minimal, and say so plainly.

Also note the **breadth** number even when no divergence fires: breadth in the mid-50s% with RSP/SPY narrowing is a *healthy-but-narrow, mega-cap-led* tape — not a flag, but a reason to deliberately diversify the final picks away from whatever's crowded (usually tech). Carry this conclusion forward; it feeds both selection (step 4) and sizing (step 5).

## Step 2 — momentum name list

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/momentum-scan/scripts/scan.py
```

Note for later: the `Sig` column is the per-name buyability read (🟢 buy zone / 🔵 deep pullback / 🟡 in-trend / 🟠 stretched / 🔴 overextended), and momentum lists frequently come back mostly 🔴 — which is *itself* the warning that buying the raw leaderboard means chasing. Note the sector concentration too (e.g. "Tech 23 of 30") — it tells you which way to diversify in step 4.

Ignore the breadth figure in momentum-scan's own `Regime` banner — it uses a different, tech-tilted pool and a different MA, so it can print something alarming like "~25% > 200DMA" right next to regime-scan's "58%". They're not contradictory; defer to regime-scan's breadth (step 1) and don't let the momentum banner's lower number trigger a false 🔴 scare.

## Step 3 — consensus convergence

```bash
python <SKILLS_DIR>/cross-scan/scripts/aggregate.py --refresh --scans-dir <SKILLS_DIR>
```

Pass `--scans-dir <SKILLS_DIR>` explicitly: cross-scan's own default is `~/.claude/skills`, so if `<SKILLS_DIR>` resolved to the fallback repo path, the default would read a *different* directory than the one steps 1–2 just wrote state into. Passing it keeps cross-scan pointed at the same folders (a no-op when `<SKILLS_DIR>` already is `~/.claude/skills`).

`--refresh` only regenerates sister scans whose snapshot is >3 days old (or missing); anything fresh today is skipped, so this is a no-op on the scans you just ran and a safety net on the ones you didn't. Read the freshness header — if a scan is still STALE after refresh (e.g. a weekend save-skip), treat its contribution as informational.

The output gives you tickers in **≥3 scans** (typically a handful) and **≥2 scans**, each with per-scan ranks and a `Composite read` label. Note the ⚠️ line cross-scan now prints under the ≥3 tier: its overlap backtest found overlap count ranks conviction **backwards** (3-scan names −4.5% xT+10 vs −1.5% single-scan), with UOA co-membership as the main contaminant. So read the tables as a **context map, not a leaderboard**: tech+tech rows are the candidates, UOA-touched rows are de-risking flags on names the user holds, and the ⚠️ labels (`froth tell` / `crowding tell` / `bearish positioning` / `backtest-worst cell`) mean exactly what they say.

The 2-scan table is **capped at `--top-n` rows (default 30)** and will say e.g. "29 of 45 shown" — the rest are hidden. If you want the full overlap (e.g. to make sure a non-tech name lower down isn't invisible to the diversification step), re-run with `--top-n 60` or `--format json` (the JSON always carries the complete set). Worth doing whenever the hidden tail is larger than a handful.

## Step 4 — select the top N (risk/reward lens)

This is judgment, not a formula — but the priority order below was rebuilt on the sister scans' 2026-05→07 outcome backtests (each scan's **Backtested outcomes** section carries the full numbers). It's tuned for "best *current* risk/reward" — entry quality and tight invalidation, not the highest-octane name:

1. **Build the candidate pool from tech+tech overlaps and validated single-scan pockets — not from overlap count.** The pool: `momentum+base` and `momentum+mean-reversion` overlaps — the ~neutral cells in the overlap backtest (the no-UOA within-scan rows ran Beat10 54-55%) — **preferably in their 1st–2nd overlap session**: fresh overlaps ran −1.6% xT+10 vs −4.4% by the 4th+ consecutive session. Check freshness by re-running `aggregate.py --date <prior session> --scans-dir <SKILLS_DIR>` and seeing whether the name was already overlapped (cross-scan itself is a single-day join and doesn't track spell). `base+mean-reversion` is the *weakest* pair (its cell ran −2.65% xT+10, Beat10 37%, n=55) — admit it only when both rule-4 and rule-5 gates pass. Also add any single-scan name that passes its own scan's validated filter (rules 4–6): a strong single-scan name that passes beats a stale two-scan overlap that doesn't.
2. **A 3+-scan overlap is the crowding representative, not the standout.** Don't auto-lead with it — in-sample, 3-scan names ran −4.5% xT+10 (Beat10 36%) and the mom+base+mr triple was the worst labeled cell (n=15, −8.0% xT+10, Beat10 10%). It makes the finalists only if it independently passes rules 3–7, and its brief must say the crowd is already there.
3. **Treat the `uoa` column as a red flag on any row.** Call-heavy co-flag = crowding tell (−3.0% xT+10); put-heavy = the one backtest-validated warning (−5.3% xT+10). A UOA-touched name needs an explicit, verified reason to stay a finalist (a dated near-term catalyst, not "smart money is buying") — and **never after a >+10% five-session rip**: post-rip UOA flags were the single worst cell in the UOA backtest (−2.4% xT+5, 38% beating the universe). Default: push UOA-touched names to runner-up and mention them as de-risking flags if the user holds them.
4. **Rank base-side names by base length, not base score.** BaseWks ≥ 20 is base-breakout's one big validated edge (75% win at +20 sessions vs 45% baseline); the composite base Score did **not** discriminate outcomes. Among `base-breakout + *` rows — and base-only candidates, which lack a momentum `Sig`/ATR-stop — read `<SKILLS_DIR>/base-breakout-scan/state/history.csv` and rank on **`base_weeks` ≥ 20 first, then smaller `to_pivot`** (entry trigger near = tight invalidation). Don't prize deep volume dry-up (inverted in-sample) and skip any entry that gaps >3% past the pivot.
5. **Gate mean-reversion-side names on Score ≥ 40 and a 1st–2nd-day listing.** That combined filter ran +1.83%/signal (~3× baseline); 3rd+ consecutive listings ran negative. Read the listing age off the `Streak` column in mean-reversion's own scan output — its history.csv has no streak column, so if you only have the CSV, derive it from whether the ticker appears under the immediately-preceding `run_id`s. A `momentum+mean-reversion` "pullback in a leader" that fails this gate isn't a buyable dip — it's a name that stopped bouncing ("stuck oversold").
6. **Prefer momentum `Sig` 🟢/🔵; downgrade 🔴 overextended.** A name that only clears via a vertical, RSI-80 move is a worse entry than one basing quietly. For fresh momentum entrants, check the entry-volume tier — the `Entry volume` column on momentum's history-dashboard roster, or in raw form the `vol_ratio_20d` field on the name's *first* run-day row in `<SKILLS_DIR>/momentum-scan/state/history.csv` — volume-backed entries ran +9.0% episodes vs +3.1% for quiet ones in momentum's backtest.
7. **Prefer a tight ATR stop (≤ ~8%) and low AnnVol.** Tight invalidation is the whole point.
8. **Diversify sectors, and step out of the crowded cohort.** If momentum is tech-heavy and regime flagged a narrow tape, deliberately favor non-tech candidates — concentration risk is real and a narrowing tape pulls sponsorship from the crowded names first.

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
| Consensus | (which scans + overlap spell; ⚠️ if UOA-touched or 3+) | | |
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

Follow the table with a 2–3 sentence-per-name plain-language verdict, then a short **"what the funnel did"** recap that makes the value explicit — e.g. how the deep-dive *changed* the picture vs the raw overlap (a name that looked great on signal agreement but turned out to have capped reward or a deteriorating fundamental backdrop). That recap is often the most useful part: it shows why "appears in N scans" ≠ "buy" — now backtest-quantified, since overlap count alone ranked outcomes backwards.

Close with concrete next-step offers: swap a finalist for a runner-up and re-dive; persist the theses (`/commit-invest` if available); or go deeper on one name (full `/deep-research`).

## Honesty rules (carry these through the whole funnel)

- **Never frame output as "buy this".** These are *prioritized research candidates with risk parameters*, not advice. Say so.
- **Quote the backtest numbers when rejecting a crowded name.** "In 3 scans" *sounds* bullish; the measured record says the opposite (−4.5% xT+10). Stating the number is what keeps the funnel honest against its own old framing — and it explains to the user why the obvious-looking pick got demoted.
- **Report the tape faithfully.** If regime is 🟡/🔴, lead with that, don't bury it under exciting names.
- **Surface what the deep-dive killed.** The funnel's job is as much to *reject* plausible names as to surface good ones — a consensus name with reward already capped at the analyst target, or a bullish options flow sitting on top of a weakening commodity, is a finding worth stating loudly.
- **The single-run caveat:** the scans get sharper with history (streaks, slopes). A first-ever run is informationally thin; lean harder on the fundamental deep-dive when the scan history is short.

## Quick reference — the whole funnel

```bash
# 1. regime gate (read --show-history if today already scanned)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILLS_DIR>/regime-scan/scripts/scan.py

# 2. momentum name list (+ per-name Sig / stops)
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' --with 'numpy>=1.24,<3' \
  python <SKILLS_DIR>/momentum-scan/scripts/scan.py

# 3. consensus overlap (auto-refresh stale sisters)
python <SKILLS_DIR>/cross-scan/scripts/aggregate.py --refresh --scans-dir <SKILLS_DIR>

# 4. select top-N by risk/reward lens (judgment — see Step 4)
# 5. parallel deep-dive subagents per finalist (see references/deep-dive-template.md)
```
