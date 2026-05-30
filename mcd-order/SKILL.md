---
name: mcd-order
description: >-
  Find the cheapest way for a group to order McDonald's China (麦当劳): treat
  combos as cheap component containers, redistribute their parts across people,
  and use coupons (优惠券) and points (积分) to cover everyone's exact items for the
  least cash. Use when several people order McDonald's together and want the
  cheapest split / 拼单最省 / "cheapest way to order X, Y, Z". Runs on the mcd-mcp
  MCP server.
---

# McDonald's Order Optimizer

The insight this skill automates: a McDonald's combo (套餐) is priced *below* the
sum of its parts, so a combo is really a **cheap container of components**. When
several people order together, the naive "one combo each + extra singles" often
pays for items nobody wanted. If you instead treat the group's order as one
**multiset of items to cover** and let combos' components be redistributed across
people (using each combo's free in-combo swaps), you can usually cover the same
food for less — and coupons + points push it lower still.

Finding that cheapest combination by hand is slow combinatorics. A bundled
solver (`scripts/optimize.py`, stdlib only) does it deterministically; your job
is to gather the real menu/coupon/points data from the **mcd-mcp** MCP server,
shape it into the solver's input, verify the top candidates' true prices, and
present the plan. **This skill is an advisor by default — it does NOT place the
order** unless the user explicitly asks afterward.

A note on language: this SKILL.md and `references/modeling.md` are instructions
for you (the model) and are written in English. Chinese appears only where it's
functional — mcd-mcp's own domain terms (glossed in English) and the menu/coupon
data, which comes back in Chinese. **Anything you show the END USER (the install
prompt below, the final plan in Step 8) should match the language they're writing
in** — Chinese for a Chinese speaker, English for an English one — not hardcoded.

`<SKILL_DIR>` below is the directory containing this `SKILL.md` — substitute its
absolute path when running.

## Step 0 — Require the mcd-mcp server (install gate)

Everything here depends on the **mcd-mcp** server (tools named `mcp__mcd-mcp__*`,
e.g. `mcp__mcd-mcp__now-time-info`). Before doing anything else, confirm it's
connected. If those tools are unavailable / a call errors with "no such tool" or
a connection failure, **stop and help the user install it** instead of trying to
proceed without data. Tell them (in their conversation language) the gist below:

> This skill needs the McDonald's China MCP server (`mcd-mcp`), which isn't
> connected yet. Setup (see https://github.com/M-China/mcd-mcp-server):
>
> 1. Get a token: visit **https://open.mcd.cn/mcp**, sign in with phone
>    verification, open the console, and activate your MCP Token.
> 2. Register the server with Claude Code, e.g.:
>    ```bash
>    claude mcp add-json mcd-mcp '{"type":"streamablehttp","url":"https://mcp.mcd.cn","headers":{"Authorization":"Bearer YOUR_MCP_TOKEN"}}'
>    ```
>    (or add the same block under `mcpServers` in your MCP config). It's a
>    remote/hosted server — nothing to download. Replace `YOUR_MCP_TOKEN`.
> 3. Restart Claude Code so the `mcd-mcp` tools load, then ask again.

Don't fabricate menu, price, coupon, or points data to work around a missing
server — the whole point is real numbers.

## What you'll do (pipeline)

```
0. Confirm mcd-mcp is connected (else: install gate above)
1. Gather the order: who's eating + each person's exact items, the scenario, store/address
2. Lock scenario + store  -> orderType / beType / storeCode / beCode
3. Pull the menu          -> query-meals (+ query-meal-detail for combo components & swaps)
4. Pull coupons           -> query-store-coupons (usable here) + query-my-coupons
5. Pull points            -> query-my-account (balance + expiring) + mall-points-products
6. Build the solver input JSON  -> run scripts/optimize.py  -> top-K candidate baskets
7. Verify real prices     -> calculate-price on each top-K basket; re-rank by true cash
8. Present the plan        (advisor only; offer to place it on explicit request)
```

The exact JSON shapes for steps 6–7 and the field-by-field mapping from each
mcd-mcp tool live in **`references/modeling.md`** — read it before building the
input; don't reconstruct the schema from memory.

## Step 1 — Gather the order

Get, in the user's own words:

- **People and their exact items** — e.g. "person A: Big Mac + medium fries;
  person B: McSpicy + medium Coke". You translate this colloquial wishlist into
  canonical menu SKUs later (against the live menu) — the user never has to speak
  in product codes.
- **Scenario** — one of: dine-in (到店自取, beType 1) / drive-through (得来速,
  beType 5) / McDelivery (麦乐送, beType 2) / corporate group-meal (团餐,
  beType 6). Prices and coupons differ by scenario, so this must be fixed up front.
- **Store or delivery address**, and any **budget / dietary** constraints.

Two decisions are baked in (don't re-litigate them unless asked):

- **Strict SKU** — each person gets *exactly* their specified item; no
  cross-person substitution. A combo's **free in-combo swap** (e.g. default cola
  → sprite at no charge) counts as legal, because the recipient still ends up
  with their exact SKU. Only ask the user when their phrasing is **ambiguous**
  (just "cola" → which size? zero-sugar?) or when a **size/spec conflicts** (wants
  a *large* coke but the combo only includes a *medium*) — surface it, don't guess.
- **Waste allowed + reported** — the optimizer may buy a combo and leave a
  component unused if that's still cheapest; whatever's unused is reported as a
  leftover list so the user decides.

## Step 2 — Lock scenario and store

- dine-in / drive-through → `query-nearby-stores` (searchType 2 with city+keyword
  to search, or searchType 1 for favorites). beType 1 returns stores with **no**
  `beCode`; beType 5 returns stores **with** `beCode` — keep it.
- McDelivery / group-meal → `delivery-query-addresses` (or
  `delivery-create-address` to add one), then `delivery-query-stores` to get the
  deliverable store + `beCode`.

Record `orderType` (1 for dine-in/drive-through, 2 for delivery/group-meal),
`beType`, `storeCode`, and `beCode` (when present). Every later call needs them,
and a SKU's `productCode` can differ across scenarios — so re-pull the menu if
the scenario changes.

## Step 3 — Pull the menu and combo composition

- `query-meals` (with the locked orderType/beType/storeCode/beCode) → the list of
  buyable products with prices and `productCode`s. These become the solver's
  `single` and `combo` options.
- For each **combo** the group might use, `query-meal-detail` → its components and
  any swappable slots. This is what lets the solver redistribute components and
  model free swaps (interpretation A). Without it a combo is an opaque price and
  the core insight can't fire.

Normalize both demand-side items and combo components to **one SKU namespace**
(prefer `productCode`; fall back to `name|size` when a combo's component code
doesn't line up with its standalone code) — see `references/modeling.md`.

## Step 4 — Pull coupons

- `query-store-coupons` (with the locked store + orderType/beType) → coupons
  **actually usable** for this order. Use these to create coupon-discounted
  option variants and the coupon resource pool.
- `query-my-coupons` → what the user already holds. `available-coupons` /
  `auto-bind-coupons` can surface/claim coupons from the 麦麦省 coupon center
  first, if the user wants to grab everything claimable before optimizing.

Model order-level threshold discounts (满减) / stacking conservatively in the
local solver — the **authoritative** coupon math comes from `calculate-price` in
step 7.

## Step 5 — Pull points (Plan A)

- `query-my-account` → points balance and how much is **expiring soon**.
- `mall-points-products` (+ `mall-product-detail` as needed) → point-redeemable
  items/coupons. Turn the ones relevant to this order into options with
  `cashPrice: 0` and `pointsCost: <cost>`.

Points policy is **Plan A**: minimize cash, treat **expiring points as free** to
spend, and charge **non-expiring points their opportunity cost** so they're only
spent here when that beats holding them for a better redemption.

Derive that opportunity cost from the mall — don't guess a number. Scan
`mall-points-products` and, for each redeemable, estimate its **cash value per
point** = (what you'd otherwise pay for it, in fen) ÷ (its points cost). Gift
cards give an exact rate (face value ÷ points); product coupons use the granted
item's menu price. Take the **best** rate as `r*` (fen per point) and pass
`pointsPolicy.minCashSavedPer100Points = round(r* × 100)`. The solver then spends
a non-expiring point on this order only when doing so saves more cash than `r*` —
i.e., only when this order is a better use of the point than the best redemption.

If the user has **no non-expiring points**, or you genuinely can't estimate any
redemption value, **omit** `minCashSavedPer100Points` entirely — the solver then
spends only the free expiring points and never burns the stash on a guess.

## Step 6 — Run the solver

Build the input JSON exactly as specified in `references/modeling.md` (all cash
in **fen**), then:

```bash
python3 <SKILL_DIR>/scripts/optimize.py --input /tmp/mcd_problem.json --pretty
# or pipe the JSON in on stdin
```

It returns `plans` (top-K cheapest baskets), a priced `baseline`, and `savings`
vs the naive baseline and vs all-singles. If `meta.uncoverable` is non-empty, a
demanded SKU has no buyable option — tell the user (likely a normalization miss
or an item that store doesn't sell). If `meta.optimal` is false the search hit
its node cap and the result is a good-but-unproven solution; say so.

## Step 7 — Verify true prices with calculate-price

Local cash is an estimate; real coupon stacking and threshold discounts (满减) are
only correct from the server. For each of the top-K plans, call `calculate-price`
with that plan's non-points items (productCodes, quantities, and any
`couponId`/`couponCode`) at the locked store/orderType. Re-rank by the returned
real total. Prefer the plan whose **verified** price is lowest; if verification
reorders them, trust the verified numbers, not the local estimate.

## Step 8 — Present the plan

Render the result **in the user's conversation language** — don't hardcode a
language. (Product, combo, and coupon names come straight from the menu data, so
they'll appear in Chinese regardless; that's fine.) Lead with the win, then the
details, roughly this structure:

```
[Cheapest plan]  (scenario · store)
Lowest: ¥XX.X   ·   saves ¥X.X (YY%) vs "one combo each"   ·   ¥X.X (ZZ%) vs all à la carte

[What to buy]
- <combo> ×1  (drink swapped to cola)   ¥30.0
- <burger> ×1 (coupon C1 applied)        ¥15.0
Coupons used: C1 ×1   Points used: 0   (800 expiring & unused — see below)

[Who gets what]
- person A: <burger> + <fries>  (from <combo>)
- person B: <burger> (à la carte, couponed) + <cola> (combo swap)

[Leftover / waste]: none
```

Convert all fen → 元 (÷100) for display. Always show the **savings vs the naive
plan** — that's the user's whole insight, quantified. One guard: if
`baseline.covered` is `false`, the baseline you built doesn't actually feed
everyone, so `savings.vsBaseline` is comparing against an under-spec'd order and
overstates the win — fix the baseline (or omit that comparison) rather than
quoting it. List any **leftover** items honestly, and note **expiring points**
that went unused (so they can decide to spend them). If `plans` has more than one
close option, mention the runner-up so the user can trade (e.g. "¥1 more but no
coupon needed").

End with a single soft offer in the user's language (e.g. "Want me to place this
order for you?") — and only call `create-order` if the user explicitly says yes
(it spends real money). Default is advice only.

## Notes

- **Units**: the solver speaks **fen** (integer) end to end, matching
  `calculate-price`; you do the ÷100 for the user.
- **Scenario changes the data**: switching dine-in ↔ delivery changes prices,
  coupons, and sometimes `productCode` — re-pull menu/coupons and re-run, don't reuse.
- **Don't invent data**: if a tool fails, report it and retry/ask — never fill
  in plausible prices or coupons. A wrong number defeats the purpose.
- **Re-runs are cheap**: tweaking the group's items, scenario, or the points
  threshold just means rebuilding the JSON and re-running `optimize.py`.
- For the full input/output schema and the mcd-mcp field mapping, read
  `references/modeling.md`.
