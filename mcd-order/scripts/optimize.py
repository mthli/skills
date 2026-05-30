#!/usr/bin/env python3
"""
McDonald's group-order cost optimizer.

Reads a JSON problem spec on stdin (or --input FILE) and writes a JSON result on
stdout: the top-K cheapest baskets that cover the group's exact-SKU demand using
singles, combos (with free in-combo swaps), coupons, and points — plus a priced
baseline and the savings delta.

Model (the agreed design):
  - Strict SKU matching: a demanded SKU is only covered by an identical SKU.
  - Waste allowed: coverage is ">= demand"; anything bought-but-unassigned is
    reported as `leftover` so the caller can surface it.
  - Points = Plan A: minimize cash. Expiring points are free to spend; non-
    expiring points carry a shadow price = their opportunity cost r* (the best
    cash-per-point redemption available in the mall), passed as config.
    pointsPolicy.minCashSavedPer100Points (fen per 100 points), so they're only
    spent here when that beats the best alternative use. If it's absent, only
    the free expiring points are spent — never the stash on a guess.
  - Coupons are a shared resource pool; options consume from it (shared caps).
  - Combo free-swaps (interpretation A): a swappable slot may become any SKU in
    its swap group at no extra cash, so the recipient still gets their exact SKU.

Local cash here is an ESTIMATE. The real coupon / threshold (满减) math is
authoritative only via the mcd-mcp `calculate-price` tool, which the caller runs
on the top-K baskets to get true prices before showing the user.

Stdlib only — no install.
"""

import argparse
import itertools
import json
import sys
from collections import Counter, defaultdict


# --------------------------------------------------------------------------- #
# Option expansion (combos with swappable slots -> concrete-content variants)
# --------------------------------------------------------------------------- #
def _content_parts(contents):
    """Split a contents list into (fixed Counter, [(qty, swap_list), ...])."""
    fixed = Counter()
    slots = []
    for c in contents or []:
        if c.get("slot"):
            slots.append((int(c.get("qty", 1)), list(c.get("swap", []))))
        else:
            fixed[c["sku"]] += int(c.get("qty", 1))
    return fixed, slots


def _mk_variant(gid, name, cash, points, consumes, contents, slot_choices):
    return {
        "gid": gid,
        "name": name,
        "cash": cash,
        "points": points,
        "consumes": consumes,                 # (couponId, qty) or None
        "contents": Counter(contents),        # ALL bought SKUs incl. slot fill
        "slot_choices": slot_choices,         # [{slotIndex, chosen}]
        "swap_skus": {c["chosen"] for c in slot_choices},
    }


def expand_options(options, demand):
    """Each option becomes one or more concrete-content variants.

    A swappable slot is materialized only into fills that are actually demanded,
    plus the combo's default fill (first listed swap option). The default keeps
    a defined-and-reportable leftover when the combo is bought purely for its
    other components. Picking a non-demanded, non-default fill is never useful,
    so we don't generate those variants.
    """
    variants = []
    for opt in options:
        gid = opt["id"]
        name = opt.get("name", gid)
        cash = int(opt.get("cashPrice", 0))
        points = int(opt.get("pointsCost", 0))
        consumes = None
        if opt.get("consumes"):
            consumes = (opt["consumes"]["coupon"], int(
                opt["consumes"].get("qty", 1)))
        fixed, slots = _content_parts(opt.get("contents", []))

        if not slots:
            variants.append(_mk_variant(gid, name, cash,
                            points, consumes, fixed, []))
            continue

        per_slot = []
        for (qty, swap) in slots:
            cands, seen = [], set()
            for t in swap:                                   # demanded fills first
                if demand.get(t, 0) > 0 and t not in seen:
                    cands.append(t)
                    seen.add(t)
            # combo's default fill
            default = swap[0] if swap else None
            if default is not None and default not in seen:
                cands.append(default)
                seen.add(default)
            if not cands:
                # slot with no info
                cands = [None]
            per_slot.append([(qty, t) for t in cands])

        for combo in itertools.product(*per_slot):
            contents = Counter(fixed)
            choices = []
            for i, (qty, t) in enumerate(combo):
                if t is not None:
                    contents[t] += qty
                    choices.append({"slotIndex": i, "chosen": t})
            variants.append(_mk_variant(gid, name, cash,
                            points, consumes, contents, choices))
    return variants


def relevant(variants, demand):
    """Drop variants that touch no demanded SKU — buying them only adds cost."""
    return [v for v in variants if any(demand.get(s, 0) > 0 for s in v["contents"])]


# --------------------------------------------------------------------------- #
# Core search: min-cost multiset cover via branch & bound
# --------------------------------------------------------------------------- #
def optimize(demand, variants, resources, policy, topK, node_cap):
    """Return (top-K solutions, nodes_explored, optimal_flag, uncoverable_skus).

    Branching rule: always attack the first still-uncovered demanded SKU and try
    every variant that contains it. This enumerates all distinct purchase
    multisets without permutation duplicates, and terminates because each chosen
    variant strictly reduces that SKU's remaining count.
    """
    demanded = sorted(s for s in demand if demand[s] > 0)
    cover = {s: [i for i, v in enumerate(variants) if v["contents"].get(s, 0) > 0]
             for s in demanded}
    uncoverable = [s for s in demanded if not cover[s]]

    balance = resources["points"]["balance"]
    expiring = resources["points"]["expiringSoon"]
    # fen per 100 non-expiring pts
    mcs = policy.get("minCashSavedPer100Points")
    if mcs is None:
        # No opportunity-cost estimate -> only the free expiring points are
        # spendable; never burn the user's non-expiring stash on a guess.
        balance = min(balance, expiring)
        rate = 0.0
    else:
        rate = mcs / 100.0                          # shadow fen per point
    coupons0 = dict(resources.get("coupons", {}))

    def eff(cash, points):
        return cash + max(0, points - expiring) * rate    # Plan A objective

    # key -> (eff, cash, points, counts)
    solutions = {}
    bound = [float("inf")]
    nodes = [0]
    capped = [False]

    def refresh_bound():
        if len(solutions) >= topK:
            bound[0] = sorted(v[0] for v in solutions.values())[topK - 1]

    def dfs(remaining, counts, cash, points, coupons):
        if capped[0]:
            return
        nodes[0] += 1
        if nodes[0] > node_cap:
            capped[0] = True
            return

        e = next((s for s in demanded if remaining[s] > 0), None)
        if e is None:                                     # all demand covered
            key = tuple(sorted(counts.items()))
            ev = eff(cash, points)
            if key not in solutions or ev < solutions[key][0]:
                solutions[key] = (ev, cash, points, dict(counts))
                refresh_bound()
            return

        if eff(cash, points) > bound[0]:
            return

        for vi in cover[e]:
            v = variants[vi]
            if v["consumes"]:
                cid, q = v["consumes"]
                if coupons.get(cid, 0) < q:
                    continue
            if points + v["points"] > balance:
                continue
            if eff(cash + v["cash"], points + v["points"]) > bound[0]:
                continue

            nr = remaining.copy()
            for s, q in v["contents"].items():
                if nr.get(s, 0) > 0:
                    nr[s] = max(0, nr[s] - q)
            counts[vi] = counts.get(vi, 0) + 1
            nc = coupons
            if v["consumes"]:
                cid, q = v["consumes"]
                nc = dict(coupons)
                nc[cid] -= q

            dfs(nr, counts, cash + v["cash"], points + v["points"], nc)

            counts[vi] -= 1
            if counts[vi] == 0:
                del counts[vi]

    if not uncoverable:
        dfs({s: demand[s] for s in demanded}, {}, 0, 0, coupons0)

    sols = sorted(solutions.values(), key=lambda x: (x[0], x[1], x[2]))[:topK]
    return sols, nodes[0], (not capped[0]), uncoverable


# --------------------------------------------------------------------------- #
# Plan / baseline / savings construction
# --------------------------------------------------------------------------- #
def build_plan(rank, sol, variants, people, resources):
    _, cash, points, counts = sol
    expiring = resources["points"]["expiringSoon"]

    items, pool = [], []
    coupons_used = Counter()
    for vi, cnt in sorted(counts.items()):
        v = variants[vi]
        line = {
            "optionId": v["gid"], "name": v["name"], "qty": cnt,
            "cashEach": v["cash"], "cashTotal": v["cash"] * cnt,
            "pointsEach": v["points"], "pointsTotal": v["points"] * cnt,
        }
        if v["slot_choices"]:
            line["slotChoices"] = v["slot_choices"]
        if v["consumes"]:
            cid, q = v["consumes"]
            line["consumes"] = {"coupon": cid, "qty": q * cnt}
            coupons_used[cid] += q * cnt
        items.append(line)
        for _ in range(cnt):
            for sku, qy in v["contents"].items():
                via = sku in v["swap_skus"]
                pool.extend([[sku, v["gid"], via]] * qy)

    by_sku = defaultdict(list)
    for unit in pool:
        by_sku[unit[0]].append(unit)

    allocation, warnings = [], []
    for person in people:
        receives = []
        for it in person.get("items", []):
            for _ in range(int(it.get("qty", 1))):
                if by_sku[it["sku"]]:
                    _, gid, via = by_sku[it["sku"]].pop()
                    receives.append({"sku": it["sku"],
                                     "from": gid + ("(swap)" if via else "")})
                else:
                    receives.append(
                        {"sku": it["sku"], "from": None, "uncovered": True})
                    warnings.append(
                        f"{person.get('id')}'s {it['sku']} not covered")
        allocation.append({"person": person.get("id"), "receives": receives})

    leftover = [{"sku": s, "qty": len(u), "from": u[0][1]}
                for s, u in by_sku.items() if u]
    notes = [f"{ln['name']} swap slot -> {sc['chosen']}"
             for ln in items for sc in ln.get("slotChoices", [])]

    plan = {
        "rank": rank,
        "totalCash": cash,
        "totalPoints": points,
        "pointsExpiringUsed": min(points, expiring),
        "items": items,
        "couponsUsed": dict(coupons_used),
        "allocation": allocation,
        "leftover": leftover,
    }
    if notes:
        plan["notes"] = notes
    if warnings:
        plan["warnings"] = warnings
    return plan


def price_basket(basket_items, options_by_id, demand):
    cash = points = 0
    contents = Counter()
    coupons_used = Counter()
    for bi in basket_items:
        opt = options_by_id.get(bi["optionId"])
        if not opt:
            continue
        qty = int(bi.get("qty", 1))
        cash += int(opt.get("cashPrice", 0)) * qty
        points += int(opt.get("pointsCost", 0)) * qty
        if opt.get("consumes"):
            coupons_used[opt["consumes"]["coupon"]
                         ] += int(opt["consumes"].get("qty", 1)) * qty
        fixed, slots = _content_parts(opt.get("contents", []))
        for s, q in fixed.items():
            contents[s] += q * qty
        for (sq, swap) in slots:                          # baseline takes default fill
            if swap:
                contents[swap[0]] += sq * qty

    rem = Counter(demand)
    leftover = []
    for s, q in contents.items():
        take = min(rem.get(s, 0), q)
        rem[s] -= take
        if q - take > 0:
            leftover.append({"sku": s, "qty": q - take})
    uncovered = {s: c for s, c in rem.items() if c > 0}
    return {"cash": cash, "points": points, "leftover": leftover,
            "covered": not uncovered, "uncovered": uncovered,
            "couponsUsed": dict(coupons_used)}


def all_singles_cost(demand, options):
    """Cheapest cash to buy every demanded SKU à la carte at sticker price.

    Returns None if any demanded SKU has no plain single. Coupon-discounted
    variants (those with `consumes`) and point redemptions are excluded — this
    reference is the no-deals, no-combos baseline the savings are measured against.
    """
    total = 0
    for sku, q in demand.items():
        best = None
        for opt in options:
            c = opt.get("contents", [])
            if (len(c) == 1 and not c[0].get("slot") and c[0]["sku"] == sku
                    and int(c[0].get("qty", 1)) == 1 and int(opt.get("pointsCost", 0)) == 0
                    and not opt.get("consumes")):
                price = int(opt.get("cashPrice", 0))
                best = price if best is None else min(best, price)
        if best is None:
            return None
        total += best * q
    return total


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="McDonald's group-order cost optimizer")
    ap.add_argument("--input", help="JSON spec file (default: stdin)")
    ap.add_argument("--pretty", action="store_true",
                    help="pretty-print output")
    args = ap.parse_args()

    spec = json.loads(open(args.input, encoding="utf-8").read() if args.input
                      else sys.stdin.read())

    config = spec.get("config", {})
    topK = max(1, int(config.get("topK", 3)))   # at least one plan
    node_cap = int(config.get("nodeCap", 500000))

    # Expiring points are always spent first (they're free in eff()); that's
    # baked into the objective, not a toggle. The shadow price for non-expiring
    # points is their opportunity cost r* (best cash-per-point redemption in the
    # mall), computed by the caller from mall-points-products and passed as
    # minCashSavedPer100Points. No hardcoded default — absent it, the solver
    # spends only the free expiring points (see optimize()).
    policy = dict(config.get("pointsPolicy") or {})

    resources = spec.get("resources", {}) or {}
    resources.setdefault("points", {})
    resources["points"].setdefault("balance", 0)
    resources["points"].setdefault("expiringSoon", 0)
    resources.setdefault("coupons", {})

    people = spec.get("people", [])
    demand = Counter()
    for p in people:
        for it in p.get("items", []):
            demand[it["sku"]] += int(it.get("qty", 1))

    options = spec.get("options", [])
    options_by_id = {o["id"]: o for o in options}

    variants = relevant(expand_options(options, demand), demand)
    sols, nodes, optimal, uncoverable = optimize(
        demand, variants, resources, policy, topK, node_cap)

    plans = [build_plan(i + 1, s, variants, people, resources)
             for i, s in enumerate(sols)]

    result = {
        "meta": {
            "currency": config.get("currency", "fen"),
            "optimal": optimal,
            "statesExplored": nodes,
            "echoConfig": config,
        },
        "plans": plans,
    }
    if uncoverable:
        result["meta"]["uncoverable"] = uncoverable

    if spec.get("baseline"):
        b = spec["baseline"]
        priced = price_basket(b.get("items", []), options_by_id, demand)
        result["baseline"] = {
            "label": b.get("label", "baseline"),
            "totalCash": priced["cash"],
            "totalPoints": priced["points"],
            "leftover": priced["leftover"],
            "covered": priced["covered"],
        }
        if priced["uncovered"]:
            result["baseline"]["uncovered"] = priced["uncovered"]

    savings = {}
    if plans:
        best = plans[0]["totalCash"]
        if "baseline" in result and result["baseline"]["totalCash"] > 0:
            bc = result["baseline"]["totalCash"]
            savings["vsBaseline"] = {"absolute": bc - best,
                                     "percent": round((bc - best) / bc * 100, 1)}
        asc = all_singles_cost(demand, options)
        savings["vsAllSingles"] = (
            {"absolute": asc - best,
                "percent": round((asc - best) / asc * 100, 1)}
            if asc and asc > 0 else None)
    result["savings"] = savings

    print(json.dumps(result, ensure_ascii=False,
          indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
