# Short windows (1–2mo): reference

Full mechanics behind the "short windows answer a different question" limitation in SKILL.md. Read this before running (or interpreting) `--window-months 1` or `2`.

The 3mo/6mo windows surface durable leaders; a 1mo window surfaces *what the last four weeks of flow is rotating into*, which is often a different cohort (e.g. a 3mo table dominated by semis can flip to a 1mo table dominated by biotech mid-rotation). The trade-offs are steep and intrinsic to the short lookback:

- **(a) Stretched cohort**: the cohort skews toward names fresh off a gap and already stretched; expect a 🟠/🔴-heavy pullback `Sig` column and `AnnVol%` at 100%+ on many rows (elevated even when below that).
- **(b) Rank instability**: `RankΔ` swings ±100 between runs because a single event dominates a 21-session window.
- **(c) The binding filter flips to the return floor**: over ~21 sessions far fewer names compound past the +30% `--min-return-pct` than over ~63 (169 names passed a 3mo run vs 26 on the same-day 1mo run), so that floor is what shrinks the table. The `--max-dd-pct` ceiling is *looser* at 1mo, not stricter: a shorter end-aligned window's max drawdown is ≤ the longer window's (it's a sub-interval, so the running peak can only be lower), so a name that clears 20% over 3mo clears it over 1mo too (e.g. ALAB 1mo MaxDD -12.8% vs 3mo -13.9%). To populate a fuller 1mo table, lower `--min-return-pct`, *not* `--max-dd-pct`.
- **(d) The vol-collapse filter weakens**: at 1mo each half has only ~10 returns, the `MIN_RETURNS_PER_HALF=10` floor itself (the 21-session window yields 20 returns, mid-split 10/10), so 1mo sits at the edge and the script warns on stderr (`--window-months < 2`); at 2mo each half has ~20, well above the floor, so no warning fires. (See also `references/vol-collapse.md`.)

Historical note: prior to the `min(60, trading_days - 3)` fix in `score_tickers`, any window shorter than ~3mo *returned zero picks with no warning* (a hard-coded `len(s) < 60` guard that `tail(trading_days)` could not satisfy), so older runs of this skill could not use 1mo/2mo at all; the close-extraction step kept its own hard-coded 60-session floor for a while longer, which excluded sub-3-month listings from short windows until a later fix wired it to the same scaled guard.
