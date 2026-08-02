---
name: snapback-scan
description: Powder-keg × spark-calendar scan for violent post-capitulation snapbacks. Joins mean-reversion-scan's freshest high-score oversold names against the next few days of scheduled catalysts (own earnings, same-sector megacap verdict prints, FOMC/CPI-class macro) and writes a pre-committed entry protocol (tranche, invalidation price, add-trigger) per armed name. Use whenever the user wants to buy panic or capitulation, e.g. "buy when nobody dares", "catch the falling knife", "snapback", "powder keg", or any ask about buying names that just got destroyed, in any language, including after a multi-day sector rout. The layer above mean-reversion-scan: that one lists what's oversold; this one says which oversold names have a DATED catalyst and how to act without dying early. NOT for raw oversold lists (mean-reversion-scan), market health (regime-scan), momentum leaders (momentum-scan), or single-ticker fundamentals (yfinance).
---

# snapback-scan

**Buy when nobody dares, but with a date, a size, and an invalidation price.**
The violent post-capitulation snapback is unpredictable by day, yet
identifiable by setup, and the setup only becomes *tradable* when a
scheduled catalyst gives it a date.

Origin, 2026-07-30: the night before the semis epicenter ripped +15–23%,
mean-reversion-scan's top-3 were SNDK / MU / AMD (scores 79/79/78, MU a
60-day-first listing, the backtest's highest-conviction profile). The bell
rang at the bottom; the missing piece was the join against the one thing
that made the NEXT day the day: MSFT's capex verdict, on the earnings
calendar for weeks. Powder keg × spark calendar = this skill.

Five components, each with a backtest receipt:

1. **Powder keg**: mean-reversion-scan's latest run (violent decline,
   structure intact; its 200DMA gates enforce this). A crash above a rising
   200DMA is a coiled spring; below it, a falling knife.
2. **Freshness**: Score ≥ 40 AND listed ≤ 2 runs = +1.83%/signal, 3× the
   baseline. This is exactly the stratum mean-reversion-scan surfaces as
   its **⭐️ Validated pocket**, so the keg list and that section name the
   same names. A name camped on the oversold list for a week is a downtrend,
   not a panic. Two backtested inversions to respect: deep RSI(2) earns NO
   bonus, and quiet-tape oversold is a knife catch; the edge needs panic.
3. **Spark calendar**: the scheduled events that can flip the narrative.
   The keg's own earnings (a *coin flip*, never a verdict; the earnings-gap
   rule), same-sector megacap **verdict prints** (MSFT's report owned the
   07-30 rip, not any chip name's own), and FOMC/CPI-class macro.
4. **Seller mechanism**: down-gaps, streaks, elevated volume mark
   price-insensitive selling. Forced flows stop all at once; that vacuum
   (the forced seller finishing while buyers are still scared off) is the
   fuel.
5. **Survival math**: last cycle's base rate killed 5 of 6 bounce attempts.
   The protocol exists to make being early survivable: +1.83%/signal is the
   honest mean, +16% days are the right tail. The goal is systematic
   exposure to that tail, not bottom-ticking.

`<SKILL_DIR>` is the directory containing this file. Dependencies auto-fetch
via `uv` (`yfinance>=1.3,<2`, `pandas>=2`). Reuses sister caches (best run
after the post-close `daily-market-scan`; the packet says if they're stale).

## Run

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/build_snapback.py            # JSON (default)
... --format table        # human-scannable table
... --window-days 5       # widen the spark window (default 3 trading days)
... --min-score 30        # loosen the keg gate when nothing qualifies (default 40)
... --max-age 3           # widen the freshness gate (default 2 = the backtested
                          #   "listed ≤ 2 runs" cutoff; widening dilutes the
                          #   +1.83%/signal edge; say so in the brief if you do)
... --top-n 30            # cap on kegs pulled from the MR list (default 20)
... --no-save             # don't write state/runs/<date>.json (exploration)
```

The spark window is the next N trading days PLUS tonight's AMC prints when
the run happens before ~20:00 ET (they're the nearest spark of all); that's
the `(AMC only)` entry in `spark_window`.

The packet saves to `state/runs/<date>.json`. Per-keg fields that matter:

- `armed`: has ≥ 1 spark scoped to it (own print / sector verdict / macro;
  a marketwide megacap print is context and cannot arm a keg)
- `sparks[]`: dated, with reporter symbol and slot
- `ignited`: already moved ≥ +7% since signal (chase-guard)
- `quiet_warning` 😴: no panic in the 5d tape (knife risk)
- `signal_day_low` / `mr_stop`: invalidation candidates; `mr_target`
- `next_own_earnings`: for unarmed kegs, the next known date beyond the window
- `down_streak`, `down_gaps_5d`, `vol_ratio_5d_20d`: seller-mechanism reads
- `prior_run_review`: full-sample grade of the previous flag list (n, win
  rate, avg/median, best AND worst tails, armed subset), sourced from this
  skill's own prior packet when one exists, else the prior MR run. A scanner
  nobody grades is a horoscope, and one that only shows its winners is worse

## Synthesize: the if-then brief

Write for a reader with **no finance background**, in the conversation's
language, translating each term in place: a "keg" is a stock that just got
hammered but whose long-term structure is intact; a "spark" is a scheduled,
dated event that can flip its story; "tranche 1: 1/4 size" is "first buy,
a quarter of what you'd eventually hold"; the invalidation price is "the
number that means you were wrong: exit, never average down past it".

Open with the packet strip, readable in ten seconds:

```
**Packet**: ⭐️<n> armed · 👀<n> watch · 🔥<n> ignited · 😴<n> quiet · regime <🟢/🟡/🔴> → <sizing consequence>
**Base rate**: most kegs don't explode upward on schedule; last cycle killed 5 of 6 bounces. The protocol makes the five cheap and the one caught.
**Prior run**: <one line from prior_run_review: did the bell ring true or false last time?>
```

Then one brief per **armed** keg; unarmed kegs get one line each in a
**"no spark in window"** watch list. Phrase it that way verbatim, never
"no catalyst": the window is 3 days, not the world, and
`next_own_earnings` gives the next known date beyond it (a real print two
weeks out is information, not absence). Never a bare "buy X". Each brief:

```
### <TICKER> · score <s> · day <age> on list  <flags: 🔥 ignited / 😴 quiet / ⚠️ crowded>
KEG    why it crashed + the seller-mechanism read (streak/gaps/volume: forced or drift?)
SPARK  <date + event>: what verdict it delivers, branched BOTH ways
PLAN   tranche 1: 1/4 size at ~<latest_close>, invalidation <signal_day_low>
       (hard exit, no averaging down below it)
       add: only AFTER the spark confirms (<specific observable>, e.g. "the
       verdict name holds its gap into the close"); pay up for confirmation
       target 1: <mr_target> (the bounce-back level); beyond it, trail
       abort: spark fizzles or breaks against → flat, the thesis expires with its date
```

Rules that make this honest. Apply every one:

- **The protocol trio is all-or-nothing.** Every brief carries a concrete
  tranche fraction, a concrete invalidation PRICE (a number, not a rule like
  "the earnings-day low"), and a concrete observable add-trigger. A name you
  can't write all three numbers for goes to the watch list; a half-protocol
  is worse than none, because the missing half gets improvised at 10am. This
  binds the 5th name on the list as tightly as the 1st.
- **The regime gate sizes everything.** RISK-ON → protocol as written.
  CAUTION → halve tranche 1 and gate every entry on spark confirmation.
  RISK-OFF → watch-only briefs, no entries; say so outright. The strip
  states the consequence once; each brief still carries its own sized
  numbers, so no plan reads right without the regime baked in.
- **🔥 Ignited kegs get a retest plan, never an entry.** Since-signal ≥ +7%
  means the snapback already fired; in the sample so far, the morning after
  ignition is the worst entry of the cycle (AMD 07-27, TSM 07-17). Write
  the retest level and what would invalidate the whole episode.
- **Demote 😴 quiet kegs** to the watch list, with the reason: the
  backtest says quiet-day oversold is a knife catch; the edge needs panic.
- **Own-earnings sparks are coin flips**: branch the brief BOTH ways
  (earnings-gap rule: zero directional trust in the print). A sector-verdict
  spark is the better structure: someone ELSE re-prices the narrative and
  you act on the confirmed side.
- **Never cut the strip's base-rate line.** It is the sentence that keeps
  the whole packet honest.
- **Grade the prior run out loud**: the strip's third line comes from
  `prior_run_review`. Calibration compounds.
- **State what's missing.** Stale MR cache (`stale_days` > 1), empty spark
  calendar, `errors`: name them instead of papering over. No qualifying
  kegs is a valid, useful output: say so and stop; never pad.

## Interaction with the sisters

- `mean-reversion-scan` supplies the kegs; never rescan here. If its cache
  is stale, say so and suggest running it.
- `regime-scan` supplies the sizing gate.
- `premarket-brief` is the morning-of companion: on a spark day its packet
  grades whether the verdict confirmed (gap-keep, hour-one survival); the
  "add" trigger in these briefs tends to be observable there.

## Outcome ledger

`prior_run_review` grades one packet against the next; it cannot see whether
the *rules* work. The packets are already the history, so
`scripts/backtest_outcomes.py` replays `state/runs/*.json` into
`state/outcomes.csv`. The trade convention is this skill's protocol, not the
MR scan's: entry at `latest_close`, hard exit at `signal_day_low`, target at
`mr_target`.

```bash
uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  python <SKILL_DIR>/scripts/backtest_outcomes.py
... --window 5          # trading days to resolve (3/5/10 sensitivity always printed)
... --entry next-open   # honest execution: fill at the next open, skip what gapped away
... --refresh-prices    # bypass the price cache
```

It exists to settle the three claims stated above and never tested: does a
dated spark pay (armed vs unarmed), is a sector verdict really the better
structure than the keg's own coin-flip print, and what does the 🔥 chase
actually cost. Two reading rules:

- **T% / S% / R:R before any Win%.** An ignited keg is bought after it ran,
  so `mr_target` sits ~1.5% overhead while the invalidation sits ~14% below.
  That geometry manufactures a high win rate out of a bad payoff — an
  ignited row beating the coiled row is not evidence the chase-guard is wrong.
- **Exp% is a floor, not the return.** The replay closes at target 1; the
  protocol trails past it. `NoExit%` is the untruncated read and only fills
  in once a signal's full window has elapsed.

Until the header banner clears (50+ resolved signals) the tables are a
plumbing check, not evidence — the script prints how many more run days it
needs. Never quote a stratum row into this file before then; rows with
n < 10 are marked ⚠︎ for the same reason.

Cadence follows that banner: **every couple of weeks while it's still up**
(the packets accrue daily, so the gap closes fast and the ledger is cheap to
re-run), then **quarterly** alongside the sisters' `backtest_outcomes.py`
once the sample is readable.

## Tests

```bash
cd <SKILL_DIR>/scripts && uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' \
  --with pytest pytest -q
```

Pure-logic tests (no network; every network call site is stubbed) cover the
keg age/score filters, arming rules (marketwide prints never arm; own
earnings and macro do), signal-day slicing + the ignited chase-guard, the
prior-run review's full-sample stats, and trading-day arithmetic across
NYSE-observed holidays. The ledger's own suite covers the protocol's
win/loss/expiry resolution with gap-aware fills, dividend re-anchoring (and
the refusal to anchor before the signal day, which would grade a setup on
its own decline), and the exclusion of missing attributes from every
stratum.
