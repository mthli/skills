# Modeling reference — input/output schema & mcd-mcp field mapping

Read this before building the solver input. It covers: the input JSON the solver
expects, the output it returns, and exactly how to map each mcd-mcp tool's fields
into that input. The solver is `scripts/optimize.py`.

This doc is in English (it's an instruction for you). The Chinese you'll see is
only: mcd-mcp's own domain terms (glossed) and the menu/coupon data itself, which
the API returns in Chinese — so option `name` strings in the examples are shown
as real Chinese display names, while everything structural is English.

## Contents

1. [Global conventions](#1-global-conventions)
2. [Input schema](#2-input-schema)
3. [Output schema](#3-output-schema)
4. [Mapping mcd-mcp tools → input](#4-mapping-mcd-mcp-tools--input)
5. [Verifying with calculate-price](#5-verifying-with-calculate-price)
6. [What the local model does NOT handle](#6-what-the-local-model-does-not-handle)

---

## 1. Global conventions

- **Money is fen (分), integer, everywhere.** mcd-mcp returns prices in fen;
  keep them in fen through the solver and only ÷100 to yuan (元) when showing the
  user.
- **`sku` is the canonical product identifier.** Demand-side items and combo
  components must use the *same* namespace or strict matching silently fails.
  Prefer `productCode`. When a combo component's code doesn't line up with the
  standalone product's code, fall back to a normalized `name|size` key
  (e.g. `Coke|medium`) and use it on both sides consistently.
- **Every option carries both `cashPrice` and `pointsCost`.** This unifies
  singles, combos, coupon-discounted variants, and points redemptions into one
  shape. Usually one of the two is 0.
- **Coupons are a shared resource pool** (`resources.coupons`); options reference
  what they consume via `consumes`, so one coupon shared across several products
  is capped correctly.
- **Free in-combo swaps** (interpretation A) are expressed with a swap slot in a
  combo's `contents`; the solver picks the fill that best covers demand at no
  extra cash.

---

## 2. Input schema

Top-level keys: `config`, `resources`, `people`, `options`, `baseline` (optional).
Option `name` values below are shown in Chinese on purpose — that's how the menu
data actually arrives; the solver treats `name` as an opaque display label.

```jsonc
{
  "config": {
    "currency": "fen",
    "orderType": 1,            // 1 dine-in/drive-through, 2 delivery/group-meal (echoed back)
    "beType": 1,               // 1 dine-in, 5 drive-through, 2 McDelivery, 6 group-meal
    "storeCode": "1234",
    "allowWaste": true,        // informational; solver always allows + reports waste
    "topK": 3,                 // how many candidate baskets to return for calculate-price
    "nodeCap": 500000,         // search safety cap; meta.optimal=false if exceeded
    "pointsPolicy": {          // Plan A. Expiring points are ALWAYS spent first
                               // (free in the objective) — that's not a toggle.
      // Shadow price for NON-expiring points = their opportunity cost r* (fen per
      // 100 points): r* = best cash-per-point redemption in mall-points-products,
      // × 100. See SKILL.md Step 5. OMIT this field to spend ONLY expiring points
      // (the solver then never touches the non-expiring stash). 200 below is just
      // illustrative (r* ≈ 2 fen/point), not a real rate — compute it per run.
      "minCashSavedPer100Points": 200
    }
  },

  "resources": {
    "points": { "balance": 1200, "expiringSoon": 500 },  // total, and the slice expiring soon
    "coupons": { "C1": 1 }                               // couponKey -> count available
  },

  // Demand, organized by person (strict SKU). The solver aggregates to a total
  // multiset, solves coverage, then assigns purchased units back to each person.
  "people": [
    { "id": "personA", "items": [ {"sku":"BIGMAC","qty":1}, {"sku":"MFRIES","qty":1} ] },
    { "id": "personB", "items": [ {"sku":"MCSPICY","qty":1}, {"sku":"MCOKE","qty":1} ] }
  ],

  // Supply: every buyable unit as one option.
  "options": [
    // single: contents = exactly one fixed SKU, qty 1, no points -> also used as the
    // "all-singles" reference price, so model true à-la-carte items this way.
    { "id":"s_bigmac", "name":"巨无霸(单点)", "type":"single",
      "cashPrice":2400, "pointsCost":0,
      "contents":[ {"sku":"BIGMAC","qty":1} ] },

    // combo: fixed components + a swappable slot. `swap[0]` is the default fill
    // (what shows up as leftover if the slot isn't needed). The solver only
    // materializes fills that are demanded, plus the default.
    { "id":"c_bigmac_set", "name":"巨无霸套餐", "type":"combo",
      "cashPrice":3000, "pointsCost":0,
      "contents":[
        {"sku":"BIGMAC","qty":1},
        {"sku":"MFRIES","qty":1},
        {"slot":true, "qty":1, "swap":["MCOKE","SPRITE"]}
      ] },

    // coupon-discounted variant: a cheaper price for a product that consumes a coupon.
    { "id":"s_mcspicy_c1", "name":"麦辣鸡腿堡(coupon C1)", "type":"single+coupon",
      "cashPrice":1500, "pointsCost":0,
      "contents":[ {"sku":"MCSPICY","qty":1} ],
      "consumes":{ "coupon":"C1", "qty":1 } },

    // points redemption: cashPrice 0, pointsCost > 0. Contents = what you receive.
    { "id":"p_mfries", "name":"中薯(points redeem)", "type":"points",
      "cashPrice":0, "pointsCost":800,
      "contents":[ {"sku":"MFRIES","qty":1} ] }
  ],

  // OPTIONAL: the naive plan to beat, priced with the same local model so the
  // output can report "you save X vs this". Usually "one combo per person".
  // `label` is just a display string — set it in the user's language.
  "baseline": {
    "label":"one combo each",
    "items":[ {"optionId":"c_bigmac_set","qty":1}, {"optionId":"c_mcspicy_set","qty":1} ]
  }
}
```

Field notes:

- `contents[].sku` + `qty` — a fixed component. `qty` defaults to 1.
- `contents[]` with `"slot": true` — a swap slot: `qty` units that may become any
  SKU in `swap`. List the **default component first**; the solver treats `swap[0]`
  as the default fill for leftover accounting.
- `consumes: {coupon, qty}` — couponKey (matching a key in `resources.coupons`)
  and how many that one purchase uses.
- A "single" for the all-singles reference must be exactly one fixed SKU, qty 1,
  `pointsCost` 0, and no `consumes` (sticker à-la-carte only) and no slot. If some
  demanded SKU has no such single, `savings.vsAllSingles` comes back `null` (fine).

---

## 3. Output schema

`from` / `notes` carry neutral English annotations (`(swap)`, `swap slot ->`);
re-render them for the user in their language. `name` strings stay as the menu's
Chinese display names.

```jsonc
{
  "meta": {
    "currency": "fen",
    "optimal": true,            // false => hit nodeCap; result is good but unproven
    "statesExplored": 65,
    "echoConfig": { /* your config, echoed */ },
    "uncoverable": ["NUGGETS"]  // present only if some demanded SKU has no covering option
  },

  "plans": [                    // top-K, cheapest first (by Plan-A effective cost)
    {
      "rank": 1,
      "totalCash": 4500,        // local estimate (fen) — verify with calculate-price
      "totalPoints": 0,
      "pointsExpiringUsed": 0,  // min(totalPoints, expiringSoon)
      "items": [
        { "optionId":"c_bigmac_set", "name":"巨无霸套餐", "qty":1,
          "cashEach":3000, "cashTotal":3000, "pointsEach":0, "pointsTotal":0,
          "slotChoices":[ {"slotIndex":0, "chosen":"MCOKE"} ] },
        { "optionId":"s_mcspicy_c1", "name":"麦辣鸡腿堡(coupon C1)", "qty":1,
          "cashEach":1500, "cashTotal":1500, "pointsEach":0, "pointsTotal":0,
          "consumes":{"coupon":"C1","qty":1} }
      ],
      "couponsUsed": { "C1":1 },
      "allocation": [           // proves coverage; 'from' is the source option (+(swap) if via swap)
        { "person":"personA", "receives":[
            {"sku":"BIGMAC","from":"c_bigmac_set"},
            {"sku":"MFRIES","from":"c_bigmac_set"} ] },
        { "person":"personB", "receives":[
            {"sku":"MCSPICY","from":"s_mcspicy_c1"},
            {"sku":"MCOKE","from":"c_bigmac_set(swap)"} ] }
      ],
      "leftover": [],           // bought-but-unassigned units (the waste report)
      "notes": ["巨无霸套餐 swap slot -> MCOKE"],
      "warnings": []            // present only if a person's item couldn't be covered
    }
  ],

  "baseline": {
    "label":"one combo each", "totalCash":5800, "totalPoints":0,
    "leftover":[ {"sku":"MFRIES","qty":1}, {"sku":"MCOKE","qty":1} ],
    "covered": true
  },

  "savings": {
    "vsBaseline":   { "absolute":1300, "percent":22.4 },
    "vsAllSingles": { "absolute":2200, "percent":32.8 }   // or null if not computable
  }
}
```

`plans[0]` is the recommended plan. `plans[].totalCash` is a local estimate —
always re-price the top-K via `calculate-price` (section 5) before trusting the
ranking, since coupon stacking / threshold discounts (满减) can reorder them.

---

## 4. Mapping mcd-mcp tools → input

| Input piece | Source tool(s) | How to map |
|---|---|---|
| `config.orderType/beType/storeCode/beCode` | `query-nearby-stores` (dine-in/drive-through) or `delivery-query-stores` (delivery/group-meal) | Lock the scenario first. beType 1 has no beCode; beType 5/2/6 do — keep it for later calls. |
| `options` (singles & combos) + `sku` namespace | `query-meals` | Each product → an option. Use `productCode` as `sku`/option contents. Price (fen) → `cashPrice`. |
| combo `contents` + swap slots | `query-meal-detail` (per combo) | Components → fixed `{sku,qty}`; a choosable component (drink/side) → a `{slot,swap:[...]}` with the default listed first. |
| `resources.coupons` + coupon options | `query-store-coupons` (usable here), `query-my-coupons` (owned), `available-coupons`/`auto-bind-coupons` (claim from the 麦麦省 coupon center) | Each usable coupon → a `resources.coupons` entry (count) **and** a discounted option variant (`cashPrice` = couponed price, `consumes` that coupon). |
| `resources.points` | `query-my-account` | `balance` and the expiring-soon amount → `points.balance` / `points.expiringSoon`. |
| points-redemption options | `mall-points-products` (+ `mall-product-detail` for SKUs) | Relevant redeemables → options with `cashPrice:0`, `pointsCost:<cost>`, `contents` = what you receive. |
| `config.pointsPolicy.minCashSavedPer100Points` (the points opportunity cost r*) | `mall-points-products` (full list) | Best cash-per-point redemption × 100 (fen / 100 pts). Omit when the user has no non-expiring points or no rate is estimable. See SKILL.md Step 5. |
| `baseline` | you construct it | The naive plan to beat — typically one per-person combo each. Reference option ids you already built; set `label` in the user's language. |

Namespace alignment (the one real pitfall): `query-meals` gives standalone
products; `query-meal-detail` gives combo components. If the same physical drink
has different `productCode`s in the two sources, exact matching fails. Resolve by
normalizing **both sides** to `productCode` when they agree, else to a
`name|size` key — and use the chosen key consistently in demand, options, and
combo contents.

Disambiguation: because matching is strict, when the user says something vague
("cola") resolve it against the menu to a specific SKU (size, zero-sugar?) and
confirm if unsure; when a requested spec can't be met by a combo's component
(wants large, combo includes medium), surface it rather than silently mismatching.

---

## 5. Verifying with calculate-price

The local model approximates coupon math; `calculate-price` is authoritative.
For each top-K plan, call it with the plan's **non-points** items at the locked
store/orderType:

- `storeCode`, `orderType`, `beType`, `beCode` (when the scenario needs it).
- `items[]`: `{ productCode, quantity, couponId?, couponCode? }` — include the
  coupon fields for couponed lines (`consumes` tells you which).
- **Combo swaps can't be priced or ordered through this MCP.** `calculate-price`
  (and `create-order`) take a flat `productCode` and price/order the combo's
  **default** configuration — there is no field for a per-round choice
  (e.g. 麦乐鸡4块 → 辣翅). So the verified combo price is always the *default* price.
  Model a **free** in-combo swap in the plan for redistribution (the recipient still
  ends up with their exact SKU), but present the swap as an in-app selection and say
  the price was verified for the default config — never fabricate a swapped price.

Returned prices are in **fen** — re-rank the plans by the verified total and
present the verified numbers, not the local estimate. (Points redemptions are
handled separately via the mall, not through `calculate-price`.)

---

## 6. What the local model does NOT handle

Keep these in mind; lean on `calculate-price` and your own judgment for them:

- **Order-level threshold discounts (满减) / coupon stacking rules** — modeled only
  loosely locally; trust `calculate-price`.
- **Delivery fees & minimum-order thresholds** — not in the core objective. Add
  the delivery fee to your presented total, and if a min-order isn't met, note it
  (the optimizer won't pad the basket to reach a threshold).
- **Paid upgrades / add-ons** (e.g. upsize fries) — only *free* swaps are modeled.
  Model a paid upgrade, if needed, as a separate option with its own price.
- **Combos whose composition you didn't fetch** — without `query-meal-detail` a
  combo is an opaque price and its components can't be redistributed.
- **`query-meal-detail` shows only the DEFAULT choice per round**, not the full
  swap menu — a "选择汉堡" round returns just the default burger, a 小食 round just
  its default snack. You can't enumerate every legal swap from it, so lean on known
  combo templates (三件套 = 主食 + 中薯 + 可乐; 四件套随心选 = 主食 + 中薯 +
  第二份小食 + 可乐) plus the user's stated swap, and verify the *default* price.
- **Cross-scenario comparison** (dine-in vs delivery cheaper?) — run the optimizer
  once per scenario and compare the results yourself.
