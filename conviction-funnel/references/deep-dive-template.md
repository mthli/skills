# Deep-dive agent template (Step 5)

Spawn one subagent per finalist, in parallel — or, if your harness can't spawn subagents, run the briefs yourself one name at a time (same template, just slower; don't drop a finalist for lack of parallelism). Copy the template below and fill in the `{{...}}` blanks from the scan output you already have. The goal is a tight, numeric, *standard-depth* brief — enough to decide entry/stop/size/invalidation, not an equity-research report.

## Filling in the blanks

- `{{TICKER}}` / `{{NAME}}` / `{{SECTOR_DESC}}` — e.g. `ELV` / `Elevance Health` / `healthcare / managed care`.
- `{{SCAN_CONTEXT}}` — the consensus line + momentum read you already have: which scans + ranks/scores, 3-month return, max drawdown, RSI, MA20%, AnnVol, the ATR stop level and approx spot, the `Sig`. Hand this over so the agent anchors instead of re-deriving.
- `{{REGIME_LINE}}` — one sentence: today's regime state + the narrow/broad read (e.g. "🟢 RISK-ON but mega-cap-led, breadth mid-50s% — size for a healthy-but-not-broad tape").
- `{{WSB_CLAUSE}}` — see the conditional rule below; include the "run it" or the "skip it" variant per finalist.
- For **foreign private issuers** (ADRs like RIO, BHP, ING): tell the agent there's no Form 4 / 10-Q — they file 6-K / 20-F, and US-style insider data likely won't exist. Point it at the production report / half-year results instead.

## The conditional WSB rule

Crowding is a *fragility* check, not a buy signal: a name being actively pumped on WSB is more prone to a sharp unwind, so low crowding is good for risk/reward and high crowding is a yellow flag. But it only has signal when the name *could* be crowded — and the risk/reward lens biases finalists toward sleepy institutional names where the answer is a foregone "low". So gate it:

**Run the WSB check for a finalist only if ANY of:**
- it's in a hot retail theme — semis / AI / software, nuclear, space / defense, crypto-adjacent, EV, biotech-momentum;
- AnnVol > ~70%;
- a big recent run with hot RSI (high momentum score *and* RSI > 70).

**Otherwise skip it** — default the Crowding section to "low / not a retail-crowded name (institutional profile)" without spending the call. When it does run, the lightweight "is this name on WSB's radar / in the WSB index at all" read suffices; only browse actual threads if the user later asks for sentiment detail.

So `{{WSB_CLAUSE}}` is one of:
- **Run variant:** "Crowding/sentiment: this name plausibly attracts retail ({{WHY}}), so check it — use the wallstreetbets skill (Skill tool, skill=\"wallstreetbets\") or a web search for recent WSB chatter / index membership on {{TICKER}}."
- **Skip variant:** "Crowding: {{TICKER}} is a low-vol/institutional name unlikely to be retail-crowded — default the Crowding section to 'low, not a fragile crowd' and DON'T spend a tool call on it unless you stumble onto evidence otherwise."

---

## Per-agent prompt template

```
You are doing a "standard depth" risk/reward due-diligence brief on ONE US-listed stock: **{{TICKER}} ({{NAME}}, {{SECTOR_DESC}})**. The user is a swing/position trader whose lens is "best CURRENT risk/reward" — entry quality, distance to stop, event risk — NOT a long-term buy-and-hold thesis.

Context from today's quant scans — anchor to these, don't re-derive:
{{SCAN_CONTEXT}}
Market regime today: {{REGIME_LINE}}

TOOLS:
- Market data: run Python directly via `uv run --with 'yfinance>=1.3,<2' --with 'pandas>=2' python -c '...'` for current price, history, fast_info, .info (forwardPE, P/S or P/B, growth, analyst targets). For the NEXT earnings date use **`t.calendar['Earnings Date']` as the primary path — it needs no extra deps**; `t.get_earnings_dates()` also works but ONLY if you add `--with lxml` to the uv invocation (without it, it raises "Import lxml failed"). (Or the yfinance skill.)
- SEC filings + insider activity: load EDGAR MCP tools via ToolSearch (query "edgartools", max_results 15), then edgar_company / edgar_ownership / edgar_filing / edgar_read (latest 10-Q/10-K/8-K). **Heads-up on insiders:** `edgar_ownership` returns Form-4 *metadata* (filer, date, accession) — NOT the transaction code or share count — so you can't read net buy/sell directly from it. Infer direction from the *pattern*: a tight cluster of filings dated right after the annual meeting / proxy is almost always routine RSU grant + tax-withholding (codes A/F), not conviction; scattered one-off filings are more likely open-market buys/sells (codes P/S). If a single filing looks pivotal, open that one Form 4 (edgar_read) to confirm the code. {{FPI_NOTE if applicable}}
- News / catalysts: WebSearch + WebFetch.
- {{WSB_CLAUSE}}

Deliver a tight markdown brief, EXACTLY these sections:
1. **Snapshot** — current price; % vs 20/50/200-day MA; distance below 52-wk high; ATR(14); a sensible ATR-based stop (price + % from spot).
2. **⚠️ Earnings / event date** — the NEXT earnings (or results/production) date; state confirmed vs estimated. Explicitly flag if within ~4 weeks (event risk for a swing entry). Note an imminent ex-dividend if relevant. This is the single most important risk item — do not skip it.
3. **Fundamentals** — forward P/E (and P/S or, for a bank, P/B); revenue & EPS growth trend; analyst mean target + implied upside/downside; buy/hold/sell counts. Flag if yfinance trailing figures lag a recent beat.
4. **SEC & insider** — date + 1-line takeaway of the latest 10-Q/10-K (or 6-K for an ADR); insider activity over ~6 months read from the *filing pattern* (routine post-proxy grant/withholding cluster vs scattered open-market trades — see the heads-up above; say "inferred from clustering" rather than overclaiming a net buy/sell you didn't read off transaction codes); any material 8-K.
5. **Catalyst & bear case** — why it's moving now (the driver); then the strongest bear argument / what would invalidate the setup.
6. **Crowding** — per the instruction above.
7. **Risk/reward verdict** — proposed entry zone; stop (price + %); a rough R-multiple to a reasonable upside target; a one-line position-sizing note given the regime; a one-line invalidation condition.

Be concrete and numeric. If a data point is unavailable, say so briefly rather than guessing. Keep the whole brief under ~400 words. Your final message IS the brief (it's returned to the orchestrator, not shown to a human directly) — no preamble, just the structured brief.
```

## What the orchestrator does with the briefs

Collect all N, then build the side-by-side comparison table and the per-name verdicts (see the main SKILL.md "Output" section). The most valuable synthesis move: call out where the deep-dive *changed the picture* relative to the raw scan overlap — e.g. a 3-scan consensus name that's genuinely clean vs a high-flow name whose reward is capped at the analyst target or whose commodity backdrop is weakening. "In N scans" earns a *look*, not a buy; the deep-dive is what separates the two.
