# Vol-collapse filter: reference

Deep documentation for `--vol-collapse-ratio` (flag semantics and raise/lower guidance live in the SKILL.md parameter table). Read this when an exclusion fires, a name goes missing, or you suspect a buyout leaked into the table, and above all before trusting a `--ticker` verdict on a recently-gapping name.

## Why a locked stock looks like a perfect base

A stock pinned at a cash buyout offer looks **identical to a textbook base**: tight width, volume dry-up, BB squeeze, high RS rating (post-gap), Trend Template passing (all MAs aligned below the gapped-up price). Without the filter, base-breakout-scan would flag every announced-but-not-closed merger as a top breakout candidate. Verified on real data: MASI on 2026-05-14 scored 88 (would have been #1) but the default ratio excludes it; its split-half vols were 98% → 2% annualized, ratio 0.02, vs a real base where both halves are similar.

Excluded entries print in a dedicated section above the Top-N table with their pre/post vol ratio; in JSON they carry `rank: null`, `pre_filter_rank`, `score_rank`, `vol_first_pct`, `vol_second_pct`, `vol_ratio`.

## The gap-in-second-half blind spot

`--vol-collapse-ratio` compares the realized vol of two halves of a fixed 3-month lookback. It catches lock-ins when the announcement gap falls in the **first** half (typical case: gap happened 1-3 months ago). When the gap day lands in the **second** half (announcement was within the last ~6 weeks), the gap inflates `v2` above `v1`, ratio > 1, name passes through. The failure mode is **late detection, not a silent miss**: within a few weeks the gap drifts into the first half and the filter catches it. The 5%-annualized minimum first-half vol guard keeps already-low-vol names from tripping on noise.

**False-positive direction**: names that gapped on an earnings beat and then drifted higher on low vol with orderly daily ranges can compress 2nd-half vol enough to trip the filter; lower `--vol-collapse-ratio` to 0.15 to reduce these. The filter doesn't catch tender-offer situations that don't involve a single-day gap (slow accumulation of shares at small premiums); those produce a normal-looking price chart with no anomaly to detect.

**Identification heuristic for a leak**: any base with `MaxDD%` too small to be natural (< -1%) *and* `width%` < 2% is suspect. Cross-check with the `yfinance` skill: `sec_filings --type PREM14A,DEFM14A` is the smoking gun (proxy filings relating to a merger); `8-K` / `DEFA14A` is suggestive but not conclusive.

## Single-ticker mode: same blind spot, worse practical impact

Asking the scanner "is MASI a good base?" via `--ticker` on the day a merger is announced will pass through without the warning: the gap sits at the end of the window, inflates `v2`, ratio > 1, no trigger. The universe scan is forgiving (many tickers, signal redundancy) but single-ticker mode answers a high-stakes "should I trade this name today?" question. By the time the gap drifts into the first half (~2-3 weeks later) the warning fires, but the day-1 false negative is the worst failure mode.

There's no clean fix using only the split-half geometry: a complementary "did the most recent N days have a single move > 20%?" check would catch announcement-day gaps, but adds a separate detection pattern. For now, when running `--ticker` on any recently-gapping name (visible from a quick `history` chart check), verify via `sec_filings --type PREM14A,DEFM14A` before treating it as a tradeable base.
