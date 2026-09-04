# Briefing template & output rules

The output spec for premarket-brief's step 5 (synthesize). SKILL.md is the
process; this file is how the finished brief should look and read.

2026-08-01 redesign: the old 9-section long-form averaged ~300 dense lines and
the reader stopped reading it. The brief now leads with a glyph dashboard and
a plain-language read, and every block below them carries a hard cap.

## Language & length contract

- **Write the briefing in plain English for a reader with no finance
  background.** Explain every term of art in place, the moment it first
  appears: "VIX 17.3 (the market's fear gauge; under 20 is calm)", "contango
  (near-term protection priced cheaper than long-term: the market sees no
  immediate threat)", "breadth (how many stocks are still rising)",
  "defensive rotation (money hiding in utilities / groceries / pharma)",
  "hawkish (pushing rate hikes to fight inflation)".
- **Conversation delivery follows the session's language.** The archived brief
  stays English; when presenting it in conversation, restate layers ① + ② in
  the conversation's language (same convention as the sister scans).
- **Hard length caps: quiet day ≤ 60 lines total, heavy day ≤ 120.** The caps
  bind: cut detail to fit them; never pad to reach them. Depth lives in the
  packet JSON and the caches; the brief is the readable layer.
- **Every block leads with its conclusion in plain language.** Numbers support
  the sentence; they don't replace it.
- **Glyphs over prose for state**: 🟢/🟡/🔴 for the call and regime,
  ↑↗→↘↓ for direction, ⭐ for backtest-validated-pocket names, ⚠️ for a
  suspect or unverified number. Same conventions as the sister scans.
- **Times are ET with Beijing time alongside** (header + event rows); the
  reader acts on Beijing time.

## Template

Four layers, in order: dashboard, plain-language read, gradeable playbook,
capped reference detail. Layers ① + ② must stand alone; a reader who stops
after them has the day.

```markdown
# Premarket Brief · <YYYY-MM-DD> (ET) · built <HH:MM> ET / <HH:MM> Beijing

## ① At a glance

**<🟢 participate / 🟡 look before moving / 🔴 stand down> · confidence
<high/med/low>**: <one plain sentence on today's character>

| Signal | Reading | Plain read |
|---|---|---|
| Overnight | QQQ +0.85% ↑ · SPY +0.19% ↗ | tech gaps up hard; broad market barely |
| Fear gauge VIX | 17.3 → · calm structure | market not nervous (under 20 = calm) |
| Mood | fear 39/100 (prev 39 →) | crowd still scared; nobody chasing |
| Backdrop | 🟢 RISK-ON (2-day confirmed) | structure healthy; participation allowed |
| Today's events | 08:30 ECI · 10:00 inflation expectations (Beijing 20:30 / 22:00) | data day: don't move before 10:00 |
| Today's exam | QQQ close above 684.23? | hold above = repair confirmed, adds OK |

## ② What it means

<3–8 connected sentences: what happened overnight → what kind of day today is
→ where the biggest risk sits. Terms explained in place; suspect numbers
tagged ⚠️ with one reason.>

**Action:** <one explicit line, e.g. "small adds OK in the ⭐ pocket
(ODFL/VTR); don't chase chips already +18%; do nothing before the 10:00
print". On a no-action day say so: "nothing to do today".>

## ③ Playbook (if / then)

- **If** <observable trigger, e.g. "QQQ closes above 684.23"> → **then**
  <action>. **Void if:** <what cancels this line>.
<≤ 5 lines, ≤ 2 rows each; tied to positions or watchlist names, no bare
directional calls.>

## ④ Appendix

### Overnight tape
<small table, ≤ 7 rows: futures / Asia / Europe / rates / dollar + oil + gold
/ BTC, one row each, each ending in a short plain read.>

### Today's catalysts ⭐
<econ + earnings + overnight headlines, ≤ 8 bullets total, one line each,
times ET (+ Beijing). Call out your positions and watchlist names. Skip the
whole block if it's all noise; don't pad.>

### Sectors & index levels
<leaders / laggards one line each; SPY/QQQ/IWM level table: prior close ·
premkt · gap · resistance above / support below.>

### Focus names
<≤ 6 bullets, one line each: name + why it matters today. ⭐ marks
backtest-validated-pocket names; analyst actions only for genuine
up/downgrades and raise-walls (see doctrine).>

### Watch-outs
<≤ 5 bullets: event-timing traps, thin premarket liquidity, suspect numbers,
stale caches, data gaps.>

---

*Sources: <one-line provenance + freshness footer>*
```

The closing `*Sources: …*` footer is **required**: a single italic line after
a `---` rule naming which sources fed this run, which came back empty or
unavailable, the regime/momentum/MR cache dates with staleness, and the
`errors` count plus any `data_quality` flags. It complements the inline ⚠️
caveats; it doesn't replace them.

## Writing ③ Playbook (read this carefully)

This is the section that can do harm if written lazily, so frame it honestly:

- **Conditional and event-gated, not directional.** On a CPI/FOMC/NFP day the
  tape before the print is a coin flip; the useful instruction is "size down /
  wait for the print; if SPY then holds X with yields falling, momentum names
  get a green light; if X breaks, defensives lead". Write if → then → void-if,
  never a bare "buy NVDA". Explain the *why* so the reader can adapt when
  reality diverges from the scenarios.
- **Anchored to the user, not the market in the abstract.** Tie every line to
  a **position** (event risk on a held name, a stop exposed to the gap) or a
  **watchlist name**. When `positions.md` is empty, the playbook is watchlist-
  and regime-level only; say so plainly rather than inventing position advice.
- **P&L-aware when cost basis is available.** With `avg_cost` you can be
  specific in the conversation delivery: "+35% into tonight's print: a partial
  trim caps event risk instead of round-tripping the gain; void if …". In the
  **archived** brief the same line goes qualitative ("well in profit into
  tonight's print: trim case") — see the public-archive honesty rule. Without
  a basis, stay at event-risk flagging. Never fabricate one.
- **Respect the regime.** If the structural read plus the overnight tape say
  risk-off with stacking divergences, the plan is "what's holding up, sized
  small, defense", not chasing. Thread the regime into sizing.
- The honest test: would the playbook still read as sound *after* the day
  plays out either direction? If it only looks smart in one outcome, it's a
  prediction dressed as a plan; rewrite it as scenarios.

## Standing doctrine (promoted from calibration; cite with dates)

- **AMC-earnings settle artifact (07-30).** Futures fold a big after-hours
  print into the 17:00 ET settle, so next-morning ES/NQ % can understate or
  invert the true gap (07-30: NQ +0.99% vs QQQ +1.86% after MSFT/META). On
  those mornings grade gap *size* off index ETFs vs official closes; futures
  keep only the risk-tone vote. Default otherwise: futures are the cleaner
  overnight read (ETF premarket prints are thin).
- **Lone-VIX-spike trap.** A big VIX % move with futures ±0.3% and Europe flat
  is a thin or stale print: flag it ⚠️, don't headline it. Let the VIX/VIX3M
  *ratio* lead over the level: > 1 (inverted) = acute near-term stress, < 1
  (contango) = calm; on a fast overnight move the live ratio beats the
  end-of-day cache.
- **Raise-wall no-chase (TSM 07-17, AMD 07-27, FTNT 07-30, CORT 07-31; all
  round-tripped).** Multiple same-morning analyst price-target raises stacked
  on an already-large premarket gap is a crowding gauge, not confirmation:
  stalk the pullback, don't chase. Weight genuine upgrades and downgrades over
  a routine "maintains + PT nudge". **Boundary (07-31): this is a
  don't-chase-the-*gap* rule, not a bearish-on-the-*name* rule — it needs a gap
  to apply to.** A raise-wall on a name printing *flat* premarket is the
  pullback the rule tells you to stalk, not a name to avoid: FTNT −0.16%
  premarket → **+4.99%** close on the day the brief filed it under avoid.
  **Second boundary (08-27): it does not apply to a name gapping on its own
  guidance** — that is the gap-is-a-floor rule below, and applying raise-wall
  to four earnings gaps got the direction wrong on all four.
- **Grade index-level tests with breadth attached (07-30, 07-31).** A
  close-graded level test answers *did it hold*, not *on what*. Pair every
  verdict with RSP-vs-SPY and the green-sector count: a level held on
  participation is a real signal; the same close carried by one megacap is a
  weak one. 07-31 passed both tests while **RSP closed −0.17% against SPY
  +0.72%** with 6 of 11 sectors red and AMZN alone inside it — the level held,
  the "capex board bid underneath" attribution was false. **The check attaches
  to BOTH sides of the exam (08-18, 08-24):** a bear branch written as
  "spreading" without its own breadth test mislabels a contained rotation —
  08-24's QQQ floor-break landed on an 8/11-green, defensives-led board
  (contained-rotation shape, 07-02), and the distinction governs whether the
  non-epicenter half of the book stays adds-eligible. **Both gauges, always — RSP−SPY is a
  *rotation* gauge, not a *breadth* gauge (09-01).** When the average stock and
  the index fall together it reads ~flat by construction: 09-01 printed
  **−0.13pp** while the board went 4/11 green, defensives-led, and the regime's
  own breadth reading fell 51% → 47% above the 50-day on the way to its first
  CAUTION print since 07-29. A verdict carrying only the spread is
  under-sampled; carry the green-sector count with it.
- **A stale regime cache does not vote; the exam does (09-02, 09-03;
  promoted on 2nd instance).** When `regime.stale_days > 1`, the index exam's
  two breadth gauges *are* the day's regime grade — say so in ①, never "the
  cache confirms tonight". 09-02: the post-close scan rolled back on
  unfinalized Yahoo bars and never graded the CAUTION flip; 9/11 green graded
  it a scare. 09-03: the scan re-ran on 09-02 bars (its run_id is SPY's last
  *finalized* bar) and printed CAUTION **confirmed** off the two sessions
  *before* an 8/11-green shelf reclaim. Quote `confirmed_state` as the
  structural backdrop with its data date, name the disagreement when the
  tape and the instrument point opposite ways, and let the close arbitrate.
- **Grade catalyst claims by % of gap kept at the close, per name.** > 70%
  kept = the catalyst is real for that name; < 30% = it traded like sympathy
  (07-20 SIMO inverted inside the catalyst bucket; 07-30 FTNT kept 10% of its
  own print's gap).
- **Every close-graded test ships three branches, and the middle prescribes an
  action (07-17, 07-20, 08-11 SPCX, 08-12 SOXX).** Two-branch tests keep
  landing in their own gap: 08-11 SPCX fell −3.9% into an unwritten middle,
  08-12 SOXX kept **65%** of its gap — squarely between the ≥70% "ride" and
  <30% "tighten" branches. For a level test the middle is a band (a finish
  within ~0.2% of the line = "unconfirmed, next session decides"); for a
  gap-keep test the 30–70% band is the *modal* outcome, not an edge case, so
  name what it means (usually: the thesis is intact but unconfirmed — hold
  size, no adds, the far line still governs). A middle branch that only says
  "regrade tomorrow" is half-written. **Every relative test ships as a SPREAD
  with a numeric threshold (08-26, 08-27; promoted on 2nd instance).** Three
  branches cover the *primary* variable's middle and say nothing about a
  *secondary* condition inverting — both of 08-26's misses were that cell
  (QQQ's exam demanded `RSP green AND most sectors green` and got one of two;
  CIBR's demanded "green on a **red** XLK" while XLK closed green, so no
  branch existed at all). Rewritten as spreads the same morning, **both landed
  on mapped branches within one session**: QQQ cleared its shelf by 1.2% while
  **RSP trailed SPY by 0.96pp**, past the ~0.5pp threshold → the "warning
  dressed as a rally, no adds" branch, on a board that would again have been
  unmapped by signs (RSP red, 1 of 11 sectors green); **CIBR minus XLK =
  +4.45pp** → cyber bid idiosyncratic, sleeve rides. So: name the two legs,
  subtract, and set a number. A secondary variable may *describe* a branch,
  never *define* one. **The three-branch requirement applies to the spread leg
  too (09-01).** A spread shipped as two signed branches ("≥+0.5pp contained /
  ≤−0.5pp spreading") rebuilds the same unwritten middle one level down the
  tree, and 09-01 landed in it at **−0.13pp**. Give the spread a named middle
  band with an action, exactly as the primary level test gets one.
- **A knife-edge deferral needs a terminal condition (08-24, 08-25).** The
  0.2%-band protocol is 5-for-5 at what it was built for — refusing false
  verdicts (three OpEx closes 08-21, COIN 08-24, SOXX 08-25 all carried
  cleanly). But a line that pins *twice* has stopped discriminating, and a
  deferral that only ever says "next session decides" is the half-written
  middle branch above wearing a protocol's clothes. So: a **2nd knife-edge on
  the same line**, or **any knife-edge whose next grading session sits behind
  a scheduled binary** (an own-earnings print, a sector-defining verdict, a
  FOMC/CPI-class macro release), converts the deferral into a **forced
  decision before that event** — the minimum fired action executes on
  schedule and the escalation resolves pre-print, never post. Deferring
  through a binary is not a plan; it's an unowned position with a countdown.
- **A guidance-driven single-name earnings gap is a floor, not the move — in
  BOTH directions (08-25, 08-27; promoted on 2nd instance).** DKS gapped
  −20.6% and closed −30.7% on the low, keeping **149%**. The positive twin
  printed four-wide on 08-27: **CRWD 203% · CRM 190% · NVDA 139% · OKTA 121%
  of gap kept**, all closing at or near their highs. Expect >100% kept and
  frame the premarket print as the day's floor, never as the damage or the
  exit price. Containment and magnitude stay two separate claims — "one
  retailer's problem" grades whether it *spread* (DKS didn't: XLY −0.30%) and
  says nothing about how far the name itself travels; name which one you're
  claiming. **Boundary against raise-wall (08-27):** raise-wall is scoped to
  *sympathy / crowding* gaps — a wall of PT nudges stacked on a name that did
  not itself report. A name gapping on **its own guidance** is this rule's
  territory, not raise-wall's; 08-27 filed all four earnings gaps under
  "don't chase" and every one of them ran.
  **Size boundary (GTLB 45% 09-02, SNOW 71% 09-03; promoted on 2nd
  instance): at ≥ +20% the gap IS the move, not the floor.** Expect 45–75%
  kept and frame the premarket print as the day's *range*, not its start;
  the >100% floor framing applies to gaps under ~15% (107–230% on the six
  that size). **Watch (09-03, 1st instance): the negative leg went 0-for-3**
  on a broad +1% tape with a same-day catalyst-owned bid running under the
  group (HPE −8.2% open → **+5.0%**, NTAP −10.4% → **+2.6%**, both inverted;
  AVGO 65–75%, DELL day-2 +4.9%) — scope, not size, may be the boundary:
  a negative own-guidance gap inside a group being bought is two-sided.
- **Premarket single-stock prints are thin.** Weight the futures gap, Europe,
  and sector ETFs over individual moves; respect the gappers' volume floor,
  and treat pre-8:00 ET thin prints as noise.
- **No-fresh-driver gap-down = snapback candidate (06-12, 07-09, 08-03).** On
  a confirmed risk-on tape, a premarket-red name or group with no
  identifiable owning trigger (rate shock, rejected print, unwind vacuum) is
  a snapback candidate, not confirmation of an unwind — and an Asia give-back
  of a verified prior-day spike is *backward-looking*, not a fresh driver
  (06-09 KOSPI). 08-03: chips gapped down on KOSPI −5.1% (after +17.9%) with
  the regime at its cycle-best score; the brief headlined "day 2 of the
  unwind" and SOXX closed +0.55% / NVDA +2.93% with QQQ the leading index.
  Frame such gaps two-sided and let the close grade them.
- **Gap-is-half-the-move symmetry (unwind 07-01; risk-on 06-29, 07-30,
  08-04).** In a crowded unwind the premarket gap ≈ half the closing damage;
  on a corroborated catalyst-owned risk-on morning the gap ≈ half the day's
  gain (08-04: QQQ +1.16% gap → +3.40% close, SOXX +4.26 → +6.80, AMD +3.99
  → +7.00, gap-keeps >100% board-wide). Never frame a big corroborated gap
  as "the move already happened" or as "the exit price" — treat the morning
  print as a floor for the day's move, and grade the claim at the close with
  the >70%-kept rule.
- **Premarket tells are provisional; only the close grades them (08-06,
  08-10).** Any read derived from the premarket board — a "splitting" tell
  (one name red, its group flat), a shrugged downgrade, board color, a mild
  no-driver red — is a hypothesis, not a verdict: 08-06's 9/11-green
  premarket board closed 8/11 red and BBY's premarket shrug inverted −6.5pp
  by the close; 08-10's SOXX flat-vs-INTC "splitting, sleeve safe" closed at
  its dead low through the trim trigger while XE's mild red closed −8.9%
  through its void. Attach a pre-committed, close-graded price line (trigger
  + void) to every tell; the lines, not the reads, do the work.
  **Extends to the ① character line (08-31, 09-03; promoted on 2nd
  instance).** A character claim derived from the premarket *index* board is
  a tell like any other: 08-31's "zero dispersion" (uniform gap) closed with
  QQQ +0.05% vs IWM −1.96%; 09-03's "rotation morning" (IWM +0.64% vs QQQ
  +0.09% at 09:00) closed megacap-led — QQQ +1.19% the leader, IWM +0.40%
  the laggard, RSP −0.39pp — while every pre-committed branch resolved as
  written. Write the character as a hypothesis with the exam's branch
  attached ("if the close is X-led, the day was Y"), never as the verdict.
- **A fired line is not re-litigated by a green premarket (08-10, 08-11,
  08-12, 08-13; 1-for-4).** Once a close-graded exit or trim has fired, the
  next morning's bounce above that line is a *better fill*, not a reprieve: XE
  bounced over 20.80 three mornings running and closed below it every time —
  08-12's 21.04 open beat the close by 3.3% and the day's low by 7%. Holding
  through a fired line is a **new** trade and needs its own written thesis.
  **Refinement (08-13; 1-for-2 after 08-14 — watch):** the catalyst
  differentiator (a bounce with a *fresh dated catalyst* is a legitimate
  re-trade, a catalyst-less one is a better fill) held for XE's +154% print
  (close +11.7%) but failed 08-14, when REMX stuck **without** any catalyst
  (+3.18% → 78.55, sector-wide and driverless). Second watch (08-14, 1st
  instance): the doctrine's three confirming cases were all *exit* lines on a
  broken post-earnings name — a fired **trim** on an intact-uptrend name may
  not behave the same way. Either way a held-through line gets a **new
  written line**, which is what makes the distinction gradeable instead of
  hopeful.
- **The premarket sector board inverts unless a catalyst owns the sector
  (07-15, 06-25, 08-12).** Premarket sector-ETF leadership is as gap-unreliable
  as the index ETFs — 08-12: XLB +0.56% premarket → **−1.24%, worst sector**;
  XLE −0.56% → +0.16%; XLP −0.48% → +0.46%. The single survivor was **XLK**,
  the sector a dated catalyst (CPI + the AI-infra prints) actually owned. So:
  treat the premarket board as noise by default, and promote exactly the
  sector whose catalyst you can name to a real read. **The promotion is a
  close-grade line, never a pre-open verdict (08-17):** XLE with a named
  catalyst (ceasefire expiry) got demoted off a +0.1% premarket print —
  "a catalyst without a move" — and led the entire board at +1.26% by the
  close. A flat premarket does not veto a catalyst-owned sector; it means
  the grading clock hasn't run yet, same as a sympathy gap on a name.
  **Promotion picks WHICH sector to grade, never WHICH WAY it goes (08-17,
  08-26; promoted on 2nd instance).** Both graded promotions closed *opposite*
  to their premarket sign — XLE +0.1% premkt → **+1.26%, board leader**; XLK
  −0.79% premkt → **+0.61%, 2nd-best of eleven** on the day a software wreck
  supposedly owned it. So write the promotion as "this is the one sector whose
  close is a real read", never as "this sector is today's drag/leader". And
  weigh the catalyst's *scope*: a sector-wide catalyst (tariffs, a ceasefire,
  a rate move) genuinely owns a sector; three companies' prints own three
  companies — 08-26's epicenter names kept 26–34% of their gaps while the
  sector went green. **First non-inverting instance (08-27) confirms the scope
  clause:** XLK +1.89% premkt → **+3.16%, the only green sector of eleven**,
  owned by four beats spanning chips, software and cyber — a genuinely
  sector-wide catalyst, and the one promotion of three that did not flip.
  **Second consecutive non-inverting instance (08-31) hardens the scope
  clause into the load-bearing part of this rule:** XLE, owned by a shooting
  conflict over an oil chokepoint, ran +1.79% premarket → **+2.04% close vs
  SPY −0.30% = a +2.34pp spread**, past its own ≥+1.5pp branch and keeping
  >100% of the gap. So the inversion risk tracks the catalyst's *scope*, not
  the promotion mechanism: sector-wide catalysts (a war, a tariff, a rate
  move) have now promoted cleanly twice running, while the three inversions
  on record were all sectors "owned" by a handful of single-name prints.
  Check scope first; if you cannot name a catalyst that hits the whole
  sector, do not promote it at all.
- **Containment beats the event — the posture must follow the brief's own
  evidence (08-03, 08-13).** When an overnight negative is loud but the
  dashboard says it was *contained* (the group didn't follow it down, a
  corroborating market rallied, the regime score is healthy), the containment
  is the read and the headline stance has to match it. 08-03 headlined "day 2
  of the chip unwind" off a backward-looking KOSPI give-back and QQQ finished
  the leading index; 08-13 headlined "it is not an adding day" after writing
  SOXX-flat, NVDA-green and KOSPI +3.6% into its own table, and **both indices
  closed at records through the resistance it had mapped to the tick.** Three
  reporting names blowing up is a fact about those three names until the
  complex confirms it. Ask before filing the call: *does my own dashboard
  contradict my headline?* **4th instance (09-02):** KOSPI −4% / Nikkei −3% with no driver named in
  any source, US futures and Europe flat → the brief filed containment,
  explicitly refused to retrofit a driver, and SPY closed +0.44% on 9 of 11
  sectors green with IWM leading; the likely driver (a BoJ-hawk yen surge,
  Bloomberg 09-02) surfaced a day later and would have been backward-looking
  for US risk anyway. Corollary: an unexplained foreign selloff gets no
  invented cause — the containment test is the read, and a driver that
  appears later re-grades the *next* session, not this one.
- **Name which constraint binds (08-13).** "Not an adding day" is two claims
  wearing one coat — *no venue is open* (the scan is stale, the ⭐ pocket's
  listing age can't be verified, the book is full) and *the tape doesn't merit
  adds*. They are graded differently and they fail differently: on 08-13 only
  the first was true, and XLRE closed the 2nd-best sector with the stood-down
  REITs inside it. Say which one is doing the work, every time.
- **Execution is the binding constraint — the playbook leads with the ledger
  (08-19, 08-20; promoted on 2nd instance).** Two consecutive graded sessions
  the calls resolved on mapped branches while **zero** fired sells filled
  (XE idle 8 sessions through a −7.9% day that *opened at the prescribed
  fill*), and the only executions were counter-plan buys (SPCX add-lot 08-17;
  COIN bought during "add nothing" 08-20, then again premarket 08-21 ~8%
  higher into a +5% gap). Doctrine: ③ opens with an **overdue-execution
  ledger** — each fired line, its fired date, sessions overdue, and cumulative
  cost since firing — ranked above any new decision; a fired instruction
  repeats verbatim until filled or explicitly re-written as a new trade with
  its own thesis; and a **counter-plan fill is named in the ① dashboard and
  handed a written two-sided line the same day** — an unplanned position is
  still a position, and leaving it lineless is how one bad fill becomes two.
  **Escalation (08-27): the clause extends verbatim to a NEW position, not
  just an add.** On the day the brief called the best exit tape in weeks and
  wrote "add nothing", zero of three sells filled while two buys did,
  mid-session, twenty minutes apart — a third add to a name whose line said
  "riding, not building", and a **brand-new position in the one sector the
  brief had named as carrying the day's only dated risk**. The gap is no
  longer passive but *funded*: the capital three fired lines said should leave
  is what opens the unplanned ones. Corollary on timing: **a ledger line's
  cost-since-firing mean-reverts, so the session it nearly closes is the
  session to fill** — SOXX's trim went ≈−0.9% (its best mark in eight
  sessions, named as such in the brief), unfilled, then ≈−3.8% and back under
  its exit line one session later, and **every prescribed fill printed for
  three consecutive sessions** (SPCX 137.00 to the cent 08-26, XE 19.10 and
  18.58, SPCX 139.99 to 0.06%). The fills are not the constraint.
- **⭐ MR pocket has zero day-1 edge on broad risk-on days (08-03, 08-04).**
  08-03 the pocket lost 2.4pp to SPY on a broad green day; 08-04 it matched
  SPY to the basis point. Its validated KPI is 5-day expectancy (score≥40 +
  ≤2d listed = +1.83%/signal), not the day-1 print: on a trend day the
  pocket is beta — size it small, never bill it as the day's alpha venue,
  and don't grade its day-1 move as pass/fail.

## Output honesty rules

- **The archive is public: tickers yes, account numbers never.** The archived
  brief may name held tickers and their event risk, but must never carry
  share counts, cost basis, stop levels, dollar P&L, or precise P&L
  percentages — a percentage plus a public price back-solves the cost basis.
  Qualitative only in the archive ("deep underwater", "modest gain"); precise
  numbers live in positions.md, the stdout packet, and the conversation
  delivery. The saved packet enforces its own half of this rule
  (build_packet's `redact_positions`).
- **Run window first (`session`).** If `session.valid` is false you should
  have stopped at SKILL.md step 3. The only time you reach this template
  out-of-window is on an **explicit user-requested** read; then lead with
  `session.warning`, treat **every** premarket block as void (fall back to
  futures + overnight tape), and do not archive it as the day's briefing.
- **Check the premarket `as_of` date.** A run before ~4:00 ET (or Monday
  pre-dawn) sees the *prior* session's after-hours print, not live premarket;
  label those numbers as such or you'll mistake yesterday's move for today's.
- **Don't pad.** A quiet day should produce a *short* brief: dashboard +
  "nothing to do today" + a thin appendix is a valid, useful output. Length
  tracks how much is happening, never the cap.
- **State what was missing, and what disagreed.** Surface `errors`, stale
  caches, and every `data_quality` flag in the relevant block with an inline
  ⚠️ plus one plain-language reason ("two sources disagree on this number;
  don't trust it yet"). A briefing that hides its blind spots is worse than
  one that names them.
- **The header carries the "built" timestamp** so freshness is visible.
