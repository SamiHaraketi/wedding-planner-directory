#!/usr/bin/env python3
"""Exact wedding-package optimiser (open source: Google OR-Tools CP-SAT).

Problem: choose exactly ONE vendor per category so that total cost <= budget and the weighted match score
is maximal. This is a multiple-choice knapsack; CP-SAT solves it to proven optimality, so unlike a greedy or
beam search it cannot miss the best combination. Distance between vendors matters, so for each candidate venue
(the anchor) every other vendor is re-scored by its distance to THAT venue and the model is solved again.

Three packages are returned:
  Best match   : maximise score within budget
  Best value   : maximise score minus a price term (VALUE_TRADE: 1 score point is worth VALUE_TRADE % of budget)
  Lowest price : cheapest package that keeps at least CHEAP_KEEP of the best score
Each package also gets `swaps`: the best alternative vendor per category that still fits the budget.
"""
from ortools.sat.python import cp_model

import ranker

QUOTE_MARGIN = 1.15      # vendors without a published price: peer-median cost x 1.15 (conservative)
VALUE_TRADE = 2.0        # 1 score point == 2% of budget saved
CHEAP_KEEP = 0.85
SCALE = 100


def _cost(ctx, it, cat):
    c = it["cost"]["amount"]
    if c is not None:
        return float(c), False
    est = ctx["peer"].get(cat) or ((ctx["caps"].get(cat) or 0) * 0.9)
    return float(est) * QUOTE_MARGIN, True


def _value_expr(score, cost, budget):
    """score*budget - cost*K: one weighted score point (=100 units) equals VALUE_TRADE % of the budget."""
    if not budget:
        return score
    return score * int(budget) - cost * int(100 * 100 / VALUE_TRADE)


def _solve(pools, budget, mode, floor_score=None, time_limit=3.0):
    """pools: {cat: [(item, cost_int, score_int)]}. Returns (choice {cat: idx}, score_sum, cost_sum) or None."""
    m = cp_model.CpModel()
    x = {c: [m.NewBoolVar(f"x_{c}_{i}") for i in range(len(p))] for c, p in pools.items()}
    for c in pools:
        m.AddExactlyOne(x[c])
    cost = sum(pools[c][i][1] * x[c][i] for c in pools for i in range(len(pools[c])))
    score = sum(pools[c][i][2] * x[c][i] for c in pools for i in range(len(pools[c])))
    if budget:
        m.Add(cost <= int(budget))
    if mode == "match":
        m.Maximize(score)
    elif mode == "value":
        m.Maximize(_value_expr(score, cost, budget))
    elif mode == "cheap":
        m.Add(score >= int(floor_score))
        m.Minimize(cost)
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = time_limit
    s.parameters.num_workers = 4
    st = s.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    pick = {c: next(i for i in range(len(pools[c])) if s.Value(x[c][i])) for c in pools}
    return pick, sum(pools[c][pick[c]][2] for c in pools), sum(pools[c][pick[c]][1] for c in pools)


def build(db, q, base="", sid="", top_n=3, venues_to_try=10, per_cat=40):
    ctx = ranker.make_ctx(db, q)
    cats = ctx["cats"]
    first = {c: ranker.rank_category(db, ctx, c, limit=per_cat) for c in cats}
    others = [c for c in cats if c != "venue"]
    anchors = first["venue"][:venues_to_try] if "venue" in cats else [None]
    if "venue" in cats and not anchors:
        return ctx, []
    imp = {c: ranker.CAT_IMPORTANCE[c] for c in cats}
    imp_tot = sum(imp.values())
    budget = ctx["budget"]
    sols = []                                         # (mode, score, cost, items, unpriced, pools, pick)
    for v in anchors:
        pools = {}
        if v:
            pools["venue"] = [(v, *_pt(ctx, v, "venue", imp, imp_tot))]
        for c in others:
            if v:
                loc = (v["_row"]["lat"], v["_row"]["lng"])
                by_id = {it["id"]: it for it in first[c]}
                rs = []
                for it in first[c]:
                    r = ranker.score_row(ctx, it["_row"], c, loc, own=None, avail=None)
                    if r:
                        r["availability"] = by_id[r["id"]]["availability"]
                        r["rating"], r["rating_count"] = by_id[r["id"]]["rating"], by_id[r["id"]]["rating_count"]
                        rs.append(r)
            else:
                rs = first[c]
            if not rs:
                pools = None
                break
            pools[c] = [(r, *_pt(ctx, r, c, imp, imp_tot)) for r in rs]
        if not pools:
            continue
        best = _solve(pools, budget, "match")
        over = False
        if not best:                                  # nothing fits: cheapest possible, flagged
            over = True
            best = _solve(pools, None, "cheap", floor_score=0)
            if not best:
                continue
        sols.append(("match", best, pools, v, over))
        if not over:
            val = _solve(pools, budget, "value")
            if val:
                sols.append(("value", val, pools, v, False))
            ch = _solve(pools, budget, "cheap", floor_score=CHEAP_KEEP * best[1])
            if ch:
                sols.append(("cheap", ch, pools, v, False))
    if not sols:
        return ctx, []

    def pick_best(mode, key):
        c = [s for s in sols if s[0] == mode] or [s for s in sols if s[0] == "match"]
        return max(c, key=key)

    chosen = [("Best match", pick_best("match", lambda s: s[1][1])),
              ("Best value", pick_best("value", lambda s: _value_expr(s[1][1], s[1][2], budget))),
              ("Lowest price", pick_best("cheap", lambda s: -s[1][2]))]
    packages, used = [], set()
    for label, (mode, (pick, sc, cost), pools, v, over) in chosen:
        items = [pools[c][pick[c]][0] for c in pools]
        key = tuple(sorted(i["id"] for i in items))
        if key in used:
            continue
        used.add(key)
        unpriced = [i["category"] for i in items if i["cost"]["amount"] is None]
        # swaps: best other vendor per category that still fits the budget
        swaps = {}
        for c in pools:
            room = (budget - cost + pools[c][pick[c]][1]) if budget else None
            cur_id = pools[c][pick[c]][0]["id"]
            source = pools[c] if c != "venue" else [(it, *_pt(ctx, it, "venue", imp, imp_tot)) for it in first["venue"]]
            alts = [p for p in source if p[0]["id"] != cur_id and (room is None or p[1] <= room)]
            alts.sort(key=lambda p: -p[2])
            swaps[c] = [ranker.public_full(db, a[0], base, sid, 0) for a in alts[:3]]
        packages.append({
            "label": label, "score": round(_avg_score(items, imp, imp_tot)),
            "estimated_total": round(cost), "currency": ctx["cur"], "budget": budget,
            "budget_left": round(budget - cost) if budget else None,
            "over_budget": bool(over or (budget and cost > budget)),
            "needs_quote": sorted(set(unpriced)),
            "items": [ranker.public_full(db, i, base, sid, n) for n, i in enumerate(items)],
            "swaps": swaps, "optimal": True,
            "note": "Solved exactly. Totals use published prices; vendors in needs_quote use a peer-median estimate "
                    f"+{round((QUOTE_MARGIN - 1) * 100)}% safety margin and must confirm.",
        })
    return ctx, packages[:top_n]


def _pt(ctx, it, cat, imp, imp_tot):
    c, _ = _cost(ctx, it, cat)
    return int(round(c)), int(round(imp[cat] / imp_tot * it["score"] * SCALE))


def _avg_score(items, imp, imp_tot):
    return sum(imp[i["category"]] * i["score"] for i in items) / imp_tot
