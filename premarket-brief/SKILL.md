---
name: premarket-brief
description: Pre-open US market briefing ~30 min before the bell — today's character, overnight tape, catalysts (econ calendar + earnings + headlines), sentiment, sectors, index levels, and an event-gated game plan tied to YOUR positions. Reuses regime-scan (structural backdrop) and the momentum / mean-reversion caches (watchlist), adding the overnight/event layer they miss; each run reconciles the prior briefing and archives it so the regime call calibrates over time. Triggers on "premarket", "morning brief", "daily game plan", "today's catalysts / economic calendar / earnings", "what to watch before the open", "premarket movers". Do NOT use for: a pure market-health read (use regime-scan), scanning for names to buy (momentum-scan / conviction-funnel), single-ticker price or fundamentals (yfinance), or a post-close recap.
---

# premarket-brief

The **pre-open event + overnight overlay**: the layer that answers *"what is
today's character and how do I play the open?"* It runs ~30 minutes before the
US open and covers the fast, event-driven ground the structural scans miss:

- `regime-scan` answers *"is the market structurally healthy?"* from end-of-day
  internals (breadth, VIX term structure, credit, rotation); it moves slowly.
- `premarket-brief` answers *"what changed overnight and what fires today?"*
  with the futures gap, Asia/Europe, the econ calendar, who reports, and the
  headline sentiment gauge. None of that lives in any sister scan.

This skill **reuses, never recomputes**: it reads regime-scan's latest state
as the structural backdrop and the sister scans' validated pockets as the
watchlist (momentum's leaderboard plus mean-reversion's fresh score≥40
listings; the watchlist replaced cross-scan's consensus overlaps when that
skill was retired 2026-07 on its overlap backtest). On top it layers the
overnight tape, catalysts, and your positions. The output is a dashboard-first
briefing (glyph dashboard → plain-language read with an explicit action line →
if/then playbook → capped appendix; 2026-08-01 redesign after the dense
300-line long-form went unread), archived daily, with an expected-vs-actual
feedback loop that grades each regime call against the next session's tape.

`<SKILL_DIR>` below is the directory containing this file.

## Dependencies

`build_packet.py` auto-fetches its deps via `uv`: `yfinance>=1.3,<2` and
`pandas>=2` (yfinance powers the overnight tape, premarket movers, the live VIX
term structure, and analyst rating changes). The HTTP data sources use the
Python stdlib, no API keys: ForexFactory econ calendar, Nasdaq earnings, Nasdaq
economic-events backup, CNN Fear & Greed, TradingView premarket gappers, and
CNBC headline RSS. The two web-scanner/RSS sources (TradingView, CNBC) are
unofficial endpoints; every source here is best-effort, and a failed one lands
in `errors` while the briefing degrades gracefully. Reading the sister-scan
caches needs them to exist (run the post-close `daily-market-scan` first,
ideally); the briefing says so if they're stale.

---

## The run: six steps, in order

### 1. Reconcile the previous briefing first (the feedback loop)

Before building today's, close the loop on what's still open. Look in
`archive/` for **every** `YYYY-MM-DD.md` dated a *past* trading day that has no
`## Reconciliation` section yet. Normally that's one file, but a skipped run
must not orphan the briefing before it. For each one (oldest first):

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/build_packet.py --actuals <that-date> --tickers <names the briefing flagged>
```

Compare what that briefing **called** (regime direction, which sectors, whether
the index key levels held/broke, the focus names) against what the tape did.
Append a `## Reconciliation` section to that archive file: expected vs actual
in 3–5 lines, then **one honest
calibration line** naming what the call got right, what it missed, and *why*
(e.g. "over-weighted a VIX spike that futures never confirmed"). Append a
one-row summary to `calibration.md` (create it if missing) so the hit/miss
pattern stays scannable over time. **When the same lesson shows up in 2+
calibration rows, promote it:** fold the rule into
`references/briefing-template.md` (standing doctrine, playbook framing, or
honesty rules, wherever it belongs) so hardened doctrine lives in the spec
rather than in history. An ungraded briefing is a horoscope; the grading is
why the archive exists.

If there's no un-reconciled briefing (first run, or already caught up), skip
to step 2.

### 2. Build the data packet

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/build_packet.py
```

It prints the packet JSON to stdout and, **only when the run is in-window**,
saves a copy to `state/packets/<today>.json` (an out-of-window run prints but
is not saved; see step 3). **First check `session.valid` in the stdout (step
3).** If valid, **read the saved file**: it's the single source of facts for
everything below. It contains: the overnight tape (incl. live VIX term
structure: VIX9D/VIX/VIX3M levels plus the VIX/VIX3M ratio and `shape`),
premarket movers, market-wide premarket gappers (`premarket_gappers`, the top
large-cap movers outside your book, via TradingView), today's econ calendar +
earnings, recent analyst rating changes on your names + watchlist
(`rating_changes`), overnight macro headlines (`headlines`, CNBC RSS), Fear &
Greed, the regime-scan state row (incl. `confirmed_state` / `first_day_flip`,
the 2-day whipsaw filter), the sister-scan watchlist (`names.watchlist`:
momentum leaderboard + fresh high-score mean-reversion listings), your parsed
positions (with `reports_today` / `on_watchlist` joins), special-day flags, an
`errors` list, and a `data_quality` list (cross-source premarket disagreements
+ prev-close provenance; see step 3).

### 3. Gate on the run window (STOP if out-of-window)

Check `session.valid` first. The skill is built for the 04:00–09:30 ET
pre-open window. If `session.valid` is **false** (phase `intraday` /
`after-hours` / `pre-dawn` / `overnight` / `non-trading-day` /
`date-override`), the `premkt` fields are **not** overnight gaps; they're live
RTH / after-hours / stale prices, and there is no valid pre-open tape to brief
on. In that case **do not build, synthesize, or archive a briefing**:

- Report `session.warning` to the user in 1–2 lines (which phase, why it's
  being skipped), and **STOP**: skip steps 4–6 entirely.
- The script refuses to save an out-of-window packet to `state/packets/`, so
  there's nothing to clean up.
- Step 1's reconciliation still stands: grading a *past* briefing is
  independent of today's window, so keep that result; only today's forward
  briefing is skipped.
- **Override only on an explicit user request** for an out-of-window read.
  Then say so plainly, lead with `session.warning`, treat **all** premarket
  blocks as void, lean on futures + the overnight tape, and still **do not**
  archive it as the day's official briefing.

If `session.valid` is **true**, continue. The packet is raw facts; your job is
to not get fooled by them. Check these:

- **Internal consistency.** The classic trap: a big `vix.pct` move with the
  futures barely budging. ^VIX overnight/early prints are thin and can be
  stale or spike on low volume. If VIX says +40% but ES/NQ are ±0.3% and
  Europe is flat, the VIX print is suspect; flag it, don't headline it. Let
  the *futures gap and Europe* lead the risk read; treat a lone VIX number as
  corroboration, not the driver.
- **Data quality (`data_quality`).** Every premarket gap % is computed against
  official daily closes (`prev_close_source: "daily"`); fast_info prevs proved
  pollution-prone pre-open (2026-07-30: SPY prev 732.39 vs official 729.46,
  FTNT +0.15% shown on a real +11% gap). Any entry in `data_quality`, whether
  a cross-source premarket % disagreement (yfinance vs TradingView) or a
  fast_info fallback, marks that number as suspect: verify it against the
  official close before grading it, and carry the caveat into the brief.
- **Futures understate AMC-earnings gaps.** A big after-hours print gets
  folded into the 17:00 ET futures settle, so next morning's ES/NQ pct can
  understate, or even invert vs, the true overnight gap (2026-07-30: NQ
  +0.99% vs QQQ +1.86% after the MSFT/META prints). When last night had a
  market-moving AMC print, grade gap *size* off the index ETFs vs official
  closes; futures keep only the risk-tone vote.
- **Staleness.** `regime.stale_days > 1` means the structural backdrop
  predates recent action; say so and lean on the live tape. (A "RISK-ON"
  cache captured before a −4% day is worse than no read if quoted
  uncritically.)
- **Chatter.** Frame the day off `regime.confirmed_state` (the 2-run-day
  confirmed state), not the raw `regime.state`: the raw label chatters at the
  score threshold (6 of the first 10 logged transitions were single-day
  whipsaws; see regime-scan's **Signal quality & outcomes**). When
  `regime.first_day_flip` is true, say so in those terms: "regime printed
  CAUTION yesterday, unconfirmed; framing stays RISK-ON, flip confirms if it
  holds today". The exception is a trend-gate break (SPY losing its 200DMA):
  that's mechanical, respect it same-day.
- **Degradation.** If `errors` is non-empty or the calendar source is
  `unavailable`, the briefing must *name the missing inputs* rather than drop
  them without comment. Honest gaps beat invisible ones.

### 4. Enrich positions (only if positions.md is non-empty)

The packet flags earnings-today and watchlist membership for each position. If
you can locate the user's investment-notes repo, read the relevant
`distill-ticker` snapshot for thesis/conviction context (this skill's
`positions.md` holds *facts only*). If you can't find it, proceed with packet
facts; don't block on it.

### 5. Synthesize the briefing

Read **`references/briefing-template.md`** and follow it. It holds the
four-layer structure (① glyph dashboard → ② plain-language read with an
explicit action line → ③ if/then playbook → ④ capped appendix), the language
& length contract (plain English for a no-finance-background reader, terms
explained in place, hard line caps, conversation delivery in the session's
language), the playbook framing (③ is the section that can do harm if written
lazily: conditional, event-gated, tied to positions, never a directional
call), the standing doctrine promoted from calibration, and the output honesty
rules.

Then read **`calibration.md`** (at least the last ~10 rows) and carry its
standing rules into today's synthesis; the lessons column is accumulated
doctrine, not history. Cite a rule's date when applying it (e.g. "the 07-15
flag-vs-board rule") so tomorrow's reconciliation can grade whether the
doctrine itself keeps paying.

### 6. Archive it

Only reached when `session.valid` was true (step 3 stops out-of-window runs
before here). Write the finished briefing to `archive/<today>.md`. That file
is the durable, git-tracked record the next run reconciles against. A same-day
re-run overwrites it: one briefing per day, last run wins.
