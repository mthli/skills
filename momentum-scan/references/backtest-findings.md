# Backtested outcomes: full findings

Full evidence behind SKILL.md's "Backtested outcomes" section. Two replays, both re-run quarterly:

- **Live history** (`scripts/backtest_outcomes.py`) replays what the scan actually published, `state/history.csv`, under the skill's canonical convention: an episode enters at the close of its first top-30 day and sells at the close of the day its dropout is observed, the scan's only built-in exit. Point-in-time by construction, but only as long as the history. Last calibration: 2026-05-14 → 2026-07-30, 228 episodes over 50 run-days (198 closed, 30 still listed), one regime.
- **Four-year replay** (`scripts/replay_board.py`) rebuilds the board from prices for every session since 2023-01-03, importing scan.py's own scoring defaults, on a point-in-time universe: today's US equities above $500M (so names that were large then and shrank since stay in), filtered each day on market cap at the time and 63-session average volume. Over the 92 run-days the two overlap, the rebuilt board shares ~94% of its names per day with the published one, and its top-N curves track the dashboard's (top 3 best, top 10 worst, SPY / QQQ identical). Last run: 2023-01-03 → 2026-10-02, 3682 episodes (3652 closed), net of a 10bp round trip.

## The board as a portfolio (four-year replay)

Equal-weight, rebalanced every session. Close fills hold the board published at close t from close t to close t+1 (compute_benchmark.py's convention); next-open fills buy at open t+1, which is what a 20:00 ET scan can actually execute.

| strategy | CAGR (close) | vol | max DD | alpha/yr vs SPY (t) | CAGR (next open) | turnover/yr |
|---|---|---|---|---|---|---|
| top 3 | −10.5% | 42% | −62.6% | −25.7% (−1.3) | −5.4% | 122× |
| top 5 | +4.8% | 36% | −36.2% | −11.2% (−0.7) | +3.5% | 105× |
| top 10 | +16.3% | 29% | −27.2% | −1.3% (−0.1) | +12.9% | 90× |
| top 30 | +20.3% | 25% | −25.1% | +1.0% (+0.1) | +19.4% | 84× |
| enter at top 10, sell below top 30 | +20.7% | 27% | −26.0% | +1.5% (+0.1) | +19.8% | 57× |
| top 10, RISK-ON sessions only | +12.4% | 28% | −23.9% | −1.5% (−0.1) | +9.4% | 82× |
| equal-weight universe | +17.2% | 16% | −19.2% | −3.5% (−1.0) | +17.4% | 4× |
| SPY | +22.3% | 15% | −18.8% | — | +22.1% | — |
| QQQ | +33.0% | 20% | −22.8% | +3.6% (+1.0) | — | — |

By calendar year (close fills), the top 30 made +24.3% / +13.7% / +24.7% / +13.2% in 2023 / 2024 / 2025 / 2026 to date, against SPY's +26.0% / +25.3% / +18.2% / +13.5%. The top 10 lost 0.9% in 2024 while SPY made 25.3%. The top 3 lost money every year except 2026 (+31.9%).

What the table says:

- **No variant's alpha is distinguishable from zero, and the board runs 1.7–2.8× SPY's volatility.** Against the fairest stock-picking baseline, an equal-weight portfolio of the same point-in-time universe, the top 30 added ~3pt a year at 1.6× the volatility.
- **Concentration is a lottery.** The top 3 lost 34% over the period. The live sample's strong top 3 (2026-05 → 10) sits inside its only good year.
- **Profits come from a handful of names.** The top 10's ten best names (SMCI, TXG, MRVL, VST, NVDA, DELL, APP, VRT, ...) supplied 71% of its gross contribution. The top 10 trailed SPY in 60% of rolling six-month windows, by up to 30pt (window ending 2024-07-09).
- **Holding until the dropout beat rebalancing to the top 10.** Buying a name when it enters the top 10 and selling only when it leaves the top 30 cut turnover by a third and was the best variant.
- **The RISK-ON gate cost ~4pt a year.** Most of it came in 2023 (+15.0% vs +25.0%), when the gate sat out the recovery from the 2022 bear market while SPY's 200DMA was still falling.
- **Costs bite at this turnover.** A 10bp round trip on ~90× a year is ~4.5pt; even gross of costs no variant clears SPY convincingly.

Sensitivity:

- **Survivorship.** The same replay restricted to today's large caps (`--universe-file state/universe.txt`) gives the top 10 +28.8% a year and the top 30 +30.2%, with alphas near +9% (t 0.7–0.8). Survivorship inflates this board's CAGR by 10–13pt, an order of magnitude more than the 1–2% the skill used to assume. Any multi-year test of this board needs a point-in-time universe.
- **Share counts.** Point-in-time market cap is split-adjusted close × today's share count, which overstates past caps for heavy issuers. `--dilution-check` drops the 332 names whose share count grew >25% since early 2023 by anything other than a split; that run gives top 10 +20.7%, top 30 +20.8%, enter-10 / sell-below-30 +24.3% (t = 0.3), top 3 +3.2%. Dropping future issuers is itself a look-ahead (issuers tend to lag after they sell stock), so the truth lies between the two runs: top 10 at 16–21% a year, top 30 at ~20–21%, alpha insignificant either way.

## Episode findings

Each finding states the live-history evidence first, then the four-year verdict.

1. **The dropout exit is a mild stop; the "star dropout" signal didn't replicate.** Live: selling on dropout beat holding 10 more sessions by **+0.90pt** per episode overall and **+1.71pt** for episodes that had reached the top 10; post-dropout drift was flat for 5 sessions, then −0.90% by +10d and −1.77% by +20d, with former top-10 names worst (−1.91% / −4.01%) while SPY was flat. Four years: drift −0.3% by +10d and −0.7% by +20d (selling beat holding 10 more sessions by +0.3pt); former top-10 names drifted +0.3% / −0.0%, so holding them on would have done marginally better; in 2025 dropped names rose (+0.9% / +1.1%). The dropout remains the only exit with any evidence, but don't present a former leader's dropout as a sell-off forecast.
2. **Entry-day distribution days are a weak persistence hint.** Live: clean entrants (≤1 dist day in 25 sessions) ran 10.3-run tenures with 47% top-10 reach vs 4.7 runs and 18% for loaded entrants (4+), and were the only cohort pairing a positive held-to-dropout return with a >50% win rate (the middling 2–3 bucket posted the highest *mean*, +2.30% on n=53, from a few large winners). Four years: 7.8 vs 6.2 sessions and 35% vs 26% top-10 reach; held-to-dropout +0.4% vs −0.0% with medians both near −0.8% and the sign flipping year to year; only 167 of 3652 closed episodes (5%) entered clean.
3. **The volume-surge half of the entry-quality tag was convention-inflated** (live history only; the replay doesn't tag volume). The recorded +9.0% (surge) vs +3.1% (quiet) calibration reproduced exactly in episode counts (n=34/76/17) but only under the original convention's two flaws: exits at the last *listed* day's close (look-ahead: you can't know it's the last day until the next scan) and still-open winners marked at their then-current paper gains. Under the honest convention surge runs +0.43% vs quiet −0.08%: same ordering, but the gap shrinks ~12× (5.9pt → 0.5pt). The original convention stays reproducible via `backtest_outcomes.py --exit last-listed` (a labeled reconciliation mode; don't use it to judge the strategy).
4. **Entry Score buys persistence, not entry-point return.** Live: top-tercile scores at entry tripled tenure (9.5 vs 2.9 runs) and dominated top-10 reach (52% vs 3%) but ran the *worst* held-to-dropout return (−1.45% vs +1.24% for the bottom tercile). Four years, replicated: top tercile 7.7 vs 5.7 sessions, 42% vs 21% top-10 reach, held-to-dropout −0.4% vs +0.1% (bottom) and +0.7% (middle). High-score entrants arrive extended and give back more at the exit; size by score, enter on pullbacks (`Sig`).
5. **Closed episodes are the losers by construction; judge the system on both halves.** Live: closed episodes averaged −0.31% (41% win) while the 30 still-listed episodes carried +6.42% unrealized on 10.2-run tenures. Four years: closed +0.1% mean, −0.7% median, 39% win; the 30 still listed at the end carried +10.1% on 14-session tenures. The carry is skew: dropouts get cut while the open winners ride.

## Conventions and caveats

- Live history: fills at the close by default (`--fills next-open` for next-session-open execution). The day-1 cohort (30 episodes already listed when tracking began) is left-censored (its "entries" are artifacts of the tracking start); it counts in aggregates and also breaks out as its own stratum. H2 entrants ran worse than H1 (−2.73% vs −0.47% held-to-dropout), one reason the four-year replay exists.
- Four-year replay: names delisted, acquired, or now below the $500M pool floor are still absent; share counts are today's (see Sensitivity); a gap inside a name's scoring window breaks the two returns around it where scan.py's `dropna()` bridges it, which only matters for names with 1–3 missing sessions. The whole sample is one mega-cap-led stretch in which even a random equal-weight portfolio trailed SPY by 5pt a year; expect the board-vs-index gap to look different in a broad market.
