# Briefing template & output rules

The output spec for premarket-brief's step 5 (synthesize). SKILL.md is the
process; this is how the finished brief should look and read. Follow the
structure, then apply the game-plan framing and honesty rules below.

## Template

Write the briefing in **English**. Use this structure — evidence first, the call
up top, the game plan last. The ordering is deliberate: leading with the one-line
call forces a committed read, and burying suggestions under the evidence guards
against headline-driven hand-waving.

```markdown
# Premarket Brief — <YYYY-MM-DD ET> (as of <HH:MM ET>)

## 1. The call

<Risk-on / Risk-off / Wait-and-see + confidence (high/med/low)>. One sentence
fusing the regime backdrop with today's event overlay. e.g. "Structure still
constructive but the cache is stale; overnight Asia rout + oil spike, wait for
the 8:30 CPI, and SPY 737 is the line that matters."

## 2. Overnight tape

A compact table: futures (ES/NQ/YM/RTY gap %), VIX (level + live term structure —
detailed in §4; flag a lone spike on thin overnight volume),
Asia/Europe (Nikkei/HSI/Shanghai/DAX/FTSE), yields (13wk/5y/10y/30y level +
change — note Yahoo exposes no clean 2y, so read the short end off 13wk+5y), DXY,
oil/gold/copper, BTC. Then 2–3 lines reading the tape: who's leading, risk-on vs
risk-off tilt, any divergence.

## 3. Today's catalysts ⭐

The unique core. From the calendar + earnings:
- Econ data: time (ET) — event — forecast vs previous — why it matters (rate
  path? growth?). If none: say "no major US data today (quiet day)" — that itself
  sets a calmer character.
- Earnings: pre/after-market megacaps + any of YOUR names or watchlist names
  (call those out).
- Overnight headlines (`headlines`): the macro / geopolitical / central-bank /
  M&A catalysts the calendar can't schedule — what broke while you slept. Lead
  with anything genuinely market-moving (war, central-bank surprise, big M&A);
  each is timestamped ET so you can gauge freshness. Skip the section if it's all
  noise rather than padding with filler headlines.
- Fed speakers / special days (OpEx, quarter-end, half-day) from special_days.

## 4. Sentiment

F&G score + rating + trend (vs prior day / week / month) — is fear/greed
stretched or turning? + VIX term structure, read LIVE off `overnight_tape.vix`:
`vix_3m_ratio` = VIX/VIX3M, **> 1 = backwardation = acute near-term stress,
< 1 = contango = calm** (`shape` says which). Cross-check regime.vix_term (same
convention, but end-of-day — on a fast overnight move the live ratio wins). The
classic trap: a lone VIX9D spike on thin overnight volume while VIX/VIX3M is
still in contango is a stale print, not real stress — let the *ratio*, not the
level, lead. Then the gap's tone. Quantified, not "sentiment is cautious".

## 5. Sectors

Premarket sector ETF moves (sectors_premarket) + regime-scan's rotation read
(def_off_pct, rsp_spy). Leaders/laggards, rotation direction, which sector has
an event driver today.

## 6. Index levels

SPY/QQQ/IWM: prior close, premarket gap (index_premarket), regime's 50/200DMA
context (spy_vs_200_pct). Cross-check the gap against the ES/NQ/RTY futures in
section 2 — futures are the cleaner overnight read; index-ETF premarket prints
are thin. The levels that matter today — gap fill, resistance above / support
below.

## 7. Focus names

cross-scan overlap names (overlap_count ≥ 2) ∩ premarket movers ∩ your positions.
The handful worth attention today and *why* (consensus signal? gapping on news?
reporting? in your book?). Keep it short — this is a focus list, not a re-scan.

**Market-wide gappers** (`premarket_gappers`): the biggest premarket movers
*outside* your book/watchlist — a name gapping on an FDA nod, M&A, or guidance
cut you'd otherwise be blind to. Surface 2–3 only if they're real (respect the
volume floor; thin pre-8:00 ET prints are noise — see honesty rules) and say why
each moved if it's knowable. Don't relist names already covered above.

**Analyst actions** (`rating_changes`): fresh upgrades/downgrades + price-target
moves on your names or the watchlist — these gap singles pre-open. Call the
direction and the PT delta (e.g. "ROKU PT 150→170, MS reit Overweight"); weight
genuine up/down-grades over a routine "maintains + PT nudge".

## 8. Watch-outs

The traps: event timing (don't get caught before the 8:30 print), thin premarket
liquidity in single names, OpEx pinning, stale-cache caveats, any data gaps.

## 9. Game plan

See the framing below. Conditional, event-gated, tied to positions/watchlist,
with explicit invalidation. NOT a directional call.

---

*Sources: <one-line provenance + freshness footer>. e.g. "yfinance
(tape/VIX/movers/ratings), ForexFactory (calendar — empty), Nasdaq (earnings),
CNN F&G, TradingView (gappers), CNBC RSS (headlines), regime-scan + cross-scan
caches (Fri 6/05, 3d stale). `errors`: none."*
```

The closing `*Sources: …*` footer is **required**: a single italic line after a
`---` rule (keep the blank line between the rule and the footer). It names which
sources actually fed this run, flags any that came back empty/unavailable, dates
the regime/cross-scan caches with their staleness, and ends with the `errors`
count. This is the at-a-glance provenance + freshness stamp — it complements the
per-section honesty notes (§3/§7/§8), it doesn't replace them.

---

## Writing the game plan (section 9) — read this carefully

This is the section that can do harm if written lazily, so frame it honestly:

- **Conditional and event-gated, not directional.** On a CPI/FOMC/NFP day the
  tape before the print is a coin flip — the useful instruction is *"size down /
  wait for the print; if SPY holds X with yields down afterward, momentum names
  get a green light; if it breaks X, defensives lead."* Write "if → then → what
  invalidates it", never a bare "buy NVDA". Explain the *why* so the user can
  adapt when reality diverges from the scenarios.
- **Anchored to the user, not the market in the abstract.** Generic "the market
  may be volatile" advice is noise. Tie every suggestion to either a **position**
  (event risk on a name they hold, a stop exposed to the gap, an extended winner
  into earnings) or a **watchlist name** from the overlap list. When
  `positions.md` is empty, this section is watchlist- and regime-level only —
  say so plainly rather than inventing position advice.
- **P&L-aware when cost basis is available.** With `avg_cost`, you can be
  specific and careful: "+35% into tonight's print — a partial trim caps event
  risk vs. round-tripping the gain; invalidation: ...". Without it, stay at
  event-risk flagging. Never fabricate a basis.
- **Respect the regime.** If the structural read (even if cautiously stale) plus
  the overnight tape say risk-off with stacking divergences, the plan is "what's
  holding up, sized small, defense" — not chasing. Thread the regime into sizing.

The honest test for this section: would it still read as sound *after* the day
plays out either direction? If it only looks smart in one outcome, it's a
prediction dressed as a plan — rewrite it as scenarios.

---

## Output honesty rules

- **Run window first (`session`).** If `session.valid` is false you should have
  already **stopped at SKILL.md step 3** — an out-of-window run does not build or
  archive a briefing. The only time you reach this template out-of-window is on an
  **explicit user-requested** out-of-window read; in that case lead with
  `session.warning` and treat **every** premarket block as **void** (single names,
  sectors, indices, market-wide gappers — each carries the stamped warning), not
  as a gap read. `intraday` → `premkt` is a live regular-hours price;
  `after-hours` → an AH price; `pre-dawn` → still the prior session's close.
  Futures + the overnight tape stay valid; fall back to them. Never present a 2pm
  tick as a pre-open gap — and even then, don't archive it as the day's briefing.
- **Pre-market single-stock prints are thin and noisy.** Weight the futures gap,
  Europe, and sector ETFs over individual premarket moves; a single name's
  premarket % can be one odd-lot trade. The packet's `premarket_movers.note`
  tells you when there's no premarket data yet (e.g. run before 4:00 ET).
- **Check the premarket `as_of` date.** Each mover carries a timestamp. If you
  run before ~4:00 ET (or on a Monday pre-dawn), the "last bar" is the *prior*
  session's after-hours print, NOT live premarket — its date won't be today.
  When that's the case, label those numbers as the prior close/after-hours read,
  not as today's premarket gap, or you'll mistake yesterday's move for today's.
- **Don't pad.** A quiet Monday with no US data, a benign tape, and a calm
  regime should produce a *short* briefing that says "low-event day, range-bound
  likely, no action needed" — that's a valid and useful output. Length should
  track how much is actually happening.
- **State what was missing.** Surface `errors`, stale caches, and unavailable
  sources in the relevant section. A briefing that hides its blind spots is
  worse than one that names them.
- **Times are ET.** The calendar is ET-stamped; keep everything in ET and note
  the "as of" time so the user knows how fresh the premarket read is.
