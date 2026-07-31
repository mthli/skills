# Vol-collapse filter: reference

Deep documentation for `--vol-collapse-ratio` (flag semantics and raise/lower guidance live in the SKILL.md parameter table). Read this file when an exclusion fires, the user asks where a name went, or you suspect a buyout leaked into the table.

## Banner and section placement

When `--vol-collapse-ratio > 0` (default 0.2), the universe banner reflects the filter state in a unified pattern:

- **Disabled** (`--vol-collapse-ratio 0`): `**Passed filter**: 83`
- **Active, 0 excluded**: `**Passed filter**: 83 (vol-collapse: 0 excluded)`
- **Active, N excluded**: `**Passed filter**: 82 (vol-collapse: 1 excluded of 83)`

The **"Excluded by vol-collapse filter"** section prints *between the Regime banner and the Top-N table*. This placement is deliberate: exclusions are warnings about names that look like momentum but aren't, and they print even when `--regime-gate strict` suppresses the Top-N, so the user always sees them. The section lists each ticker with its `1st-half% → 2nd-half%` annualized vol and the resulting ratio, so you can sanity-check the trigger and recognize the underlying situation (most often a cash buyout pending shareholder vote).

## JSON schema for excluded entries

Excluded entries carry only the score-stage fields plus the vol-collapse fields: `ticker`, `score`, `score_rank` (immutable score-based rank), `pre_filter_rank` (= `score_rank` at exclusion time; redundant but explicit), `rank: null` (display rank is meaningless), `return_pct`, `max_dd_pct`, `from_high_pct`, `ann_vol_pct` (annualized over the *full* scoring window, distinct from `vol_first_pct` / `vol_second_pct`, the half-window slices), `vol_first_pct`, `vol_second_pct`, `vol_ratio`. The script computes sector, pullback, and ATR fields only on kept top-N picks to save HTTP; excluded entries don't get them.

## Short-window caveat

At `--window-months 1`, each half has only ~10 returns (the `MIN_RETURNS_PER_HALF` constant); std estimates at this size are noisy and the filter can flag legitimate names. The script prints a `Warning` to stderr at any `--window-months < 2`. The recommended minimum for reliable filtering is 2 months. (More on short-window behavior generally: `references/short-windows.md`.)

## Lifecycle of an excluded ticker

The scan never saves excluded names to history, so a ticker appears in the **Dropouts since last run** section *exactly once*: on the first run after the filter starts excluding it (in the prior run's saved top-N but not this run's). The dropout line gets a `*filtered by vol-collapse this run*` annotation. On subsequent runs the ticker is absent from both prior and current top-N (still excluded, never saved), so it stops appearing in Dropouts. It does, however, continue to appear in the **Excluded by vol-collapse filter** section every day the pattern persists, so that's where you look for "what's the status of MASI" after the initial dropout. Once the vol-collapse pattern resolves (deal closes, stock delists, or vol normalizes for real), the ticker returns to the top-N (assuming it still passes the score filter) with **its pre-exclusion history intact**: `prev_rank` and `first_seen` point to the last-saved top-N appearance rather than the run before. `streak` starts fresh at 1 because the exclusion gap broke the run-id chain (the streak counter walks backward through run_ids and stops at the first run missing the ticker). The ticker is **not** flagged as a new entrant: `prev_rank` fills from the old history rows.

## Window-position sensitivity

`--vol-collapse-ratio` compares the realized vol of the window's two halves (mid = `n // 2` of the daily-return series). The filter works when the announcement gap falls in the **first** half: `v1` gets inflated by the gap-day return plus normal pre-deal trading, `v2` is post-event pin, ratio → ~0.02, name excluded. It **fails** when the gap day lands in the second half, because the single huge gap return dominates whichever half it's in:

- **Recent announcements**: a deal announced ≤ halfway into the window puts the gap day in the second half. Result: `v2` inflated by the gap, `v1` is normal pre-deal trading, ratio > 1, name passes through. *The failure mode is late detection, not a silent miss*: by the next run (or within a few weeks) the gap drifts into the first half and the filter catches it.
- **Longer windows**: with `--window-months 6` and a ~3-month-old announcement, the gap lands right at the half boundary and tends to fall into the second half. The 6mo scan can leak a name the 3mo scan filters cleanly. Cross-checked on MASI (Feb 17 announcement, May 14 scan): 3mo window excludes (`vol 98% → 2%, ratio 0.02`); 6mo window does *not* exclude (gap is in second half; MASI happens to fall below the 6mo top-30 cut for unrelated reasons, but a higher-return buyout with the same structure would leak into the table).
- **Mitigations**: if you suspect a leak, the lock-in fingerprint is **`MaxDD%` too small to be natural** (`> -3%`, sometimes `> -1%`) for a name that's also +30% / +50% / +100% over the window. `FromHigh%` of 0.0 *alone* isn't suspicious; it's the default state for any momentum leader making fresh highs. It becomes diagnostic in combination: `FromHigh% = 0.0` **AND** `MaxDD% > -3%` is the price-pin fingerprint (the stock sits at the deal price with almost no daily noise). Cross-check by chaining the `yfinance` skill: the **smoking guns** are `sec_filings --type PREM14A,DEFM14A` (the "M" stands for Merger and confirms a definitive deal); `sec_filings --type 8-K,DEFA14A` is suggestive but not conclusive (8-Ks cover many events, DEFA14A is supplementary proxy materials that may or may not relate to a merger).

**False-positive direction**: names that gapped on an *earnings beat* and then drifted higher on low vol (e.g., a defensive utility post-strong-quarter) can compress 2nd-half vol enough to trip the filter. Lower `--vol-collapse-ratio` toward 0.15 to reduce these. The 5%-annualized minimum first-half vol guard is a constant (not a flag) that keeps already-low-vol names from tripping on noise; bumping `--min-market-cap` doesn't change this. The filter doesn't catch tender-offer situations that *don't* involve a single-day gap (e.g., a slow accumulation of shares at small premiums); those produce a normal-looking price chart with no anomaly to detect.
