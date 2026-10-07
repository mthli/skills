# Backtested outcomes: full findings

Two tests. The six-year replay (2021-01 → 2026-09) is the verdict; the live-history backtest (2026-05-14 → 2026-07-29) below it is in-sample and mostly didn't replicate.

## Six-year replay (2021-01-04 → 2026-09-28)

`scripts/replay_signals.py --eval-start 2021-01-04` (2026-10-07 run). Pool: the 2,971 US equities Yahoo listed above $500M on 2026-10-06; each day's universe is the names with market cap at the time (split-adjusted close × today's share count) above $5B and 63-session average volume above 1M, ~800 names/day. Picks rebuilt per session with scan.py's own functions and defaults (pinned session by session by `test_replay_signals.py`), ~40/day, 57,241 signals. Trades follow the canonical convention (target 5DMA, stop 2.5 ATR, 5-session window, same-session double touch counted WON; there were none). Cross-check against the live ledger over the 56 evening run-days before the 2026-09-02 data lag: pick-set Jaccard 0.92, outcome agreement 99%, +0.98% vs +0.98% per signal on the same days.

**The measure.** Excess over SPY across each trade's own holding span, at next-open fills (the scan publishes after the close), averaged within each signal day and then across days, with t on the day means. Signals from one day move together: a washout day lists 100+ names that bounce with the index, so a per-signal average overweights those days and its naive t is inflated several-fold.

| stratum | n | win% | exp (close) | next-open vs SPY by day (t) | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| all picks | 57,241 | 84% | +0.32% | −0.04% (−0.8) | −0.24 | +0.08 | +0.04 | +0.01 | −0.04 | −0.08 |
| old ⭐ pocket (Score ≥ 40 & day ≤ 2) | 9,105 | 82% | +0.71% | −0.14% (−1.2) | −0.83 | +0.00 | −0.14 | +0.19 | −0.38 | +0.47 |
| Score < 40 | 39,477 | 85% | +0.21% | −0.00% (−0.0) | | | | | | |
| Score 40-55 | 12,123 | 84% | +0.44% | −0.09% (−1.1) | | | | | | |
| Score 55-70 | 4,876 | 83% | +0.60% | −0.19% (−1.2) | | | | | | |
| **Score ≥ 70** | 765 | 89% | +2.49% | **+1.06% (+2.7)** | −0.06 | **−2.73** | +2.28 | +0.51 | +2.20 | +2.01 |

Year columns are the excess measure, in %. The other strata, all against SPY by day: 🔵 −0.05%, 🟢 −0.10%, 🟡 −0.00%; day-of-spell 1st −0.05%, 2nd −0.03%, 3rd −0.09%, 4th+ +0.07%; 5DMA gap ≤ −7% −0.12%, −7..−4% −0.10%; Freq60d 0 −0.17%, 1-2 −0.03%, 3-5 +0.09%; thin days (<30 picks) −0.12% (t −1.6), washout days (>60) +0.10% (t +1.3), the old pocket on thin days −0.58% (t −2.7); not RISK-ON −0.09%.

**Score ≥ 70, unpacked.** Finer bands (2023-26): 60-65 +0.17%, 65-70 −0.36%, 70-75 +0.98%, 75-80 +2.70%, 80+ +3.57%: nothing below 70, then a jump. The component behind it is the trend buffer: names ≥ 50% above their 200DMA ran +0.57% over SPY (t +2.2) where every lower buffer band is ≈ 0, and Score ≥ 70 needs a deep 5DMA gap and a low RSI(2) on top of a big buffer. So it buys the first violent drop in the market's hottest names (2025-26: quantum, nuclear, AI hardware; 2021: GME, AMC, RIOT, PLUG). 2022 shows the failure mode: 42 of its 59 signals came in Q2, mostly energy names flushed in the June commodity crash, −5.8%/signal against SPY; gating on RISK-ON doesn't fix it (2022 RISK-ON −3.5%, off −2.4%). Trades (2023-26): 62% expire (avg −0.9%), 34% hit the target (avg +10.3%), 3% stop out (avg −15.9%), 4.2 sessions held on average. ~130 signals a year on ~60 distinct days, two-thirds of them 3rd-day-or-later listings (the edge doesn't need the day ≤ 2 condition).

**Portfolios** (net of 10bp round trip; K slots of 1/K equity, a day's signals fill free slots by score, one position per ticker):

| 2021-01 → 2026-09, next-open fills | CAGR | maxDD | alpha/yr vs SPY (t), close fills | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| all picks, every open trade equal-weighted | +7.1% | −34% | +0.2% (0.0) | +1.5 | −9.2 | +22.1 | +13.5 | +9.8 | +5.5 |
| old ⭐ pocket, 10 slots, idle in SPY | +7.8% | −48% | −4.1% (−0.5) | −8.0 | −27.6 | +6.4 | +23.1 | +19.8 | +46.8 |
| Score ≥ 70, 10 slots, idle cash | +17.5% | −21% | +13.4% (+1.9) | +33.3 | −15.1 | −1.9 | +10.2 | +43.4 | +44.1 |
| Score ≥ 70, 10 slots, idle in SPY | +29.5% | −37% | +11.7% (+1.8) | +55.3 | −31.0 | +24.7 | +32.4 | +58.1 | +57.5 |
| SPY | +15.1% | −25% | | +28.7 | −18.2 | +26.2 | +24.9 | +17.7 | +14.5 |

Over 2023-26 only (the first run, before the 2020 download), Score ≥ 70 / 10 slots / idle in SPY was +45.1% CAGR vs SPY +22.1%; the six-year view is what adds 2022. In that window the old pocket's 10-slot curve (+30.4%) also came from filling slots by score: filled at random it made +15.5%, under SPY.

**Bias checks.** (a) Survivorship: rerun with the pool restricted to `state/universe.txt` (today's large caps), 2023-26: Score ≥ 70 +1.81% vs +2.01% per signal over SPY (close fills), the old pocket +0.10% vs +0.07%; small for this scan, unlike momentum's 10-13pt/yr. (b) Share count: today's count overstates past market cap for heavy issuers, admitting them early. 19% of Score ≥ 70 trades are in names momentum's replay flagged as issuing >25% more stock (QBTS, IONQ, OKLO and the like); without them Score ≥ 70 drops from +1.76% to +1.42% (t 2.8) over 2023-26, every year still positive. Dropping future issuers is itself look-ahead, so the truth sits between. (c) The pool can't contain names that fell below $500M or delisted, which is where 2021-22's busted SPACs and meme names went; the real 2021-22 was worse than shown.

**Caveats.** Six years, one bear year. Score ≥ 70 is a small, clustered sample (765 signals on ~330 days); its t of 2.7 is after many strata were examined, though the ≥ 70 band was set in `backtest_outcomes.py` before this replay. Exits weren't re-tested (finding #7 below stands on the live sample alone).

## Live-history backtest (2026-05-14 → 2026-07-29 sample): in-sample, superseded

Verdicts from the replay in brackets: [✗] didn't replicate, [◐] direction held at a fraction of the size, [✓] held, [–] not re-tested.

`scripts/backtest_outcomes.py` replays `state/history.csv` with the exact convention the running win-rate stat uses (entry at the signal-day close, limit at the 5DMA target, stop at the ATR level, 5-day window), then stratifies by everything recorded at signal time. Sample: 1,584 resolved signals (983 decisive) over a mostly RISK-ON tape. Findings, strongest first:

1. [✓, and the expectancy is the index's] **The win rate is real but not the point; expectancy is.** 92% of decisive signals hit the target (avg 2.2 days), far above Connors's published 70-75%, because the resolver is charitable (intraday-high touch, 2.5-ATR-wide stop). But the payoff structure gives most of it back: avg win **+3.71%** at the target (n=903), avg loss **−9.98%** at the stop (n=80), and, the bigger drag, the 38% expired bucket drifts **−2.44%** on average, so the whole system nets **+0.68% per signal**. Judge every filter below by expectancy, not win rate. Two execution robustness checks: gap-aware fills (limit fills at max(target, open), stop at min(stop, open)) came out *better* (+0.78%), and realistic next-open entry (`--entry next-open`; the scan output only exists after the close) trims the aggregate to +0.61% and the combined filter (#2) to +1.77%, with 60 untradable overnight gap-throughs skipped. **The edge is not an entry-timing artifact.**
2. [✗] **Score ≥ 40 plus fresh-or-second-day listing was the validated entry filter.** Absolute Score ≥ 40: +1.29%/signal (n=569) vs +0.33% below 40 (n=1,020), monotone across bands (40-55: +1.18%, ≥55: +1.60%). Combine with day-of-spell ≤ 2 and it's **+1.83%/signal on n=404 (95% win rate), nearly 3× the unfiltered baseline**, stable across both halves of the sample. The *absolute* score matters, not the daily rank: on days when nothing scores 40, that day's top-ranked names still lose (see #6).
3. [✗] **"Stuck oversold" looked like data, not doctrine.** 1st day on the list: +0.76%. 2nd consecutive day: +0.93%, no decay. **3rd+ consecutive day: −0.12%**, and the dropoff holds inside every score band. The `--persistent-min-streak 3` default sits right where the cliff is: day-2 listings stay tradable, day-3+ are review-only, confirmed by outcome data.
4. [✗] **Pullback depth looked bimodal: washouts pay, the middle is the trap.** 5DMA gap ≤ −7%: **+2.28%/signal**, and the target/stop structure earns its keep there (no-exit close-to-close only +0.84%; selling the dead-cat bounce at the 5DMA is the right call). The −7..−4% middle zone: **−0.22%, the worst pocket** (−1.79% when also stuck 3+ days). Shallow >−2% dips: +0.83% with exits but +1.79% without; the 5DMA target sits so close it caps winners, making these the one pocket where the canonical exit costs money.
5. [✗] **Deeper RSI(2) is NOT better: 🔵 underperformed 🟢.** 🔵 deep (RSI2 < 2.5): +0.37%/signal vs 🟢 +0.78% and 🟡 +0.75%. Win rate is a hair higher (94%) but wins are smaller and 45% expire flat; within every score band the deep-RSI cells are the worst (score ≥55 & RSI2 < 2.5: +0.13%). RSI(2) < 2.5 more often marks real-news damage than pure noise. The Score's biggest component (RSI depth, 40pts) is therefore dead weight in-sample; the pullback/trend-buffer/frequency components carry the edge. This contradicts doctrine, so re-validate before re-weighting the score.
6. [◐] **Signal breadth is a regime dial inside RISK-ON.** Days when the scan emits <30 signals: **−1.39%/signal, and score ≥40 doesn't save you (−1.62%)**: a name that's oversold when nothing else is tends to have its own bad news. Days with >60 signals (market-wide washout): +1.29%, and score ≥40 on those days +2.27%. Market-driven panic mean-reverts; idiosyncratic oversold doesn't. The banner's "Passed filter" count is itself a signal.
7. [–] **The 5-day window is the right default, and cutting early is the expensive mistake.** Every holding period replayed over ONE fixed set of trades (same signals, same target, same stop — only the deadline moves), with each delta paired per trade against the live rule:

   **Time stops** — n=1,584, live rule +0.68%/signal. "Per-spell Δ" repeats the paired delta over day-1 listings only (n=1,005), one trade per oversold spell: the table's intervals count each consecutive day as fresh evidence and it isn't.

   | Exit by | Exp%/signal | Δ vs 5d (paired) | 95% CI | Per-spell Δ | ⭐ pocket Δ | Exp/day |
   |---|---|---|---|---|---|---|
   | 1d | +0.11% | **−0.575%** | −0.79 ~ −0.36 | **−0.798%** | **−1.366%** | +0.109% |
   | 2d | +0.48% | **−0.206%** | −0.39 ~ −0.02 | **−0.418%** | **−0.582%** | +0.269% |
   | 3d | +0.65% | −0.037% | −0.18 ~ +0.11 | **−0.230%** | −0.142% | +0.271% |
   | 4d | +0.75% | +0.067% | −0.03 ~ +0.16 | −0.032% | +0.058% | +0.258% |
   | **5d (live)** | **+0.68%** | — | — | — | — | +0.204% |

   **Longer windows** — a separate, smaller sample (n=1,529; the extra horizons need more bars), so its live row is not the +0.68% above and the two tables must not be read across:

   | Exit by | Exp%/signal | Δ vs 5d (paired) | 95% CI | Per-spell Δ | ⭐ pocket Δ | Exp/day |
   |---|---|---|---|---|---|---|
   | **5d (live)** | **+0.81%** | — | — | — | — | +0.245% |
   | 7d | +0.61% | **−0.197%** | −0.32 ~ −0.08 | **−0.159%** | **−0.620%** | +0.153% |
   | 10d | +0.29% | **−0.516%** | −0.70 ~ −0.33 | **−0.511%** | **−1.433%** | +0.061% |

   The ⭐ pocket's own per-spell deltas (day-1 listings at Score ≥ 40, n=233) run harder in the same direction: 1d −1.724%, 2d −0.868%, 3d −0.287%, 4d −0.130%, 7d −0.977%, 10d −1.999%.

   Only the 4-day cut is a genuine tie: 3d ties on the full sample but loses −0.230% (interval excludes zero) once each spell counts once, and 4d beats 5d by less than its own interval while the two halves of the sample disagree on which wins — noise, not a reason to move the default. Every sign and ordering survives `--entry next-open`. The exit rule is therefore **target, stop, or the 5th close — whichever comes first**: 57% leave at the target (avg +3.71%), 5% at the stop (−9.98%), 38% at the deadline (−2.44%), average time in trade 3.35 sessions.

   **Two traps this finding exists to kill.** (a) The 38% expired bucket is the system's biggest drag, which makes "cut the ones that haven't moved" irresistible — but the loss those trades carry is already taken by day 2, cutting locks it in, and 25% of all winners print on days 3-5. (b) The outcomes ledger's `days_to_resolve` cannot be used to derive an exit rule: bucketing resolved trades by how long they took conditions on the future, and it gives the opposite answer (those buckets read "still open after day 2 → −1.5% expectancy", which is a statement about trades measured from entry, not about what the next three days pay). A shorter rule does earn more *per day held* (2d +0.27%/day vs +0.20% at 5d; ⭐ pocket +0.68% vs +0.49%), which is a real trade-off only when the freed capital has another signal to take — it is a ratio on a fixed signal set here, not a portfolio backtest with concurrency and capital limits.
8. [✗] **Frequency penalty, sector skew tape-driven.** Freq60d 3-5 (noisy names): +0.13% vs +0.84% for 1-2. By sector, Tech/Healthcare ran +1.2-1.5% while Energy/Materials/Defensive ran −0.3..−0.5%, matching those sectors' tapes over the sample; read it as regime-dependent, not structural.

**Caveats**: one 2.5-month window, one (mostly RISK-ON) regime; consecutive-day rows of the same oversold spell are correlated (the day-1 subset, n=1,005 at +0.76%, is the independent per-spell view); the intraday-high WON convention is an upper bound on realizable results (entry timing, by contrast, is quantified; see #1's next-open numbers); current-large-cap universe adds survivorship optimism; the breadth cutoffs (30/60) come from in-sample choices. Re-run quarterly as history accumulates, above all the RSI-depth inversion (#5) and the breadth thresholds (#6), and re-test #4's middle-zone trap in a weaker tape.
