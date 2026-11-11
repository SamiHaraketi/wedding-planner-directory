#!/usr/bin/env python3
"""Wedding ranking + package optimisation engine.

Reads ONLY the verified view `public_listings`. For one couple it:
  1. hard-filters (distance, busy on their date, venue too small),
  2. scores every candidate on 8 transparent features (0..1),
  3. blends default weights with weights LEARNED from real click/contact behaviour,
  4. builds complete packages (venue + photographer + music + catering + decor) inside the budget,
     re-scoring distance relative to the chosen venue,
  5. explains every result, and returns one-tap actions (website / call / email) routed through /go.

Nothing here invents data: unknown price/rating/availability is scored as unknown (neutral or lower)
and labelled, never filled in as if it were a fact.
"""
import itertools
import json
import math
import sqlite3
import statistics
import datetime as dt

from styles import STYLE_NAMES

CATS = ["venue", "photographer", "music", "catering", "decoration"]
FEATURES = ["price", "quality", "style", "proximity", "confidence", "availability", "completeness", "freshness"]
DEFAULT_W = {"price": .24, "quality": .22, "style": .16, "proximity": .10, "confidence": .10,
             "availability": .08, "completeness": .06, "freshness": .04}
DEFAULT_SPLIT = {"venue": .40, "catering": .27, "photographer": .12, "music": .08, "decoration": .13}
CAT_IMPORTANCE = {"venue": .35, "catering": .25, "photographer": .15, "music": .10, "decoration": .15}
# Approximate rates. Replace with a live feed (e.g. ECB) in production: ranker.FX_TO_EUR.update({...})
FX_TO_EUR = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92, "CAD": 0.67, "CHF": 1.05}

ENGINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, sid TEXT, type TEXT, listing_id INTEGER, category TEXT,
  position INTEGER, target TEXT, query_json TEXT, features_json TEXT);
CREATE INDEX IF NOT EXISTS ix_ev ON events(type, listing_id);
CREATE INDEX IF NOT EXISTS ix_ev2 ON events(sid, listing_id);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY, listing_id INTEGER, rating INTEGER CHECK(rating BETWEEN 1 AND 5),
  text TEXT, sid TEXT, created_at TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS availability(
  listing_id INTEGER, day TEXT, status TEXT CHECK(status IN ('busy','free')), source TEXT, updated_at TEXT,
  PRIMARY KEY(listing_id, day));
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, listing_id INTEGER, url TEXT, alt TEXT, source_url TEXT, fetched_at TEXT,
  UNIQUE(listing_id, url));
CREATE INDEX IF NOT EXISTS ix_photos ON photos(listing_id);
CREATE TABLE IF NOT EXISTS field_sources(
  listing_id INTEGER, field TEXT, value TEXT, source_url TEXT, method TEXT, fetched_at TEXT,
  PRIMARY KEY(listing_id, field));
CREATE TABLE IF NOT EXISTS learned_weights(name TEXT PRIMARY KEY, w REAL, n INTEGER, updated_at TEXT);
"""


def init(db):
    from refresh_engine import migrate
    db.row_factory = sqlite3.Row
    migrate(db)
    db.executescript(ENGINE_SCHEMA)
    db.commit()


# ---------------------------------------------------------------- helpers
def haversine(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def fx(amount, cur_from, cur_to):
    a, b = FX_TO_EUR.get((cur_from or "EUR").upper()), FX_TO_EUR.get((cur_to or "EUR").upper())
    if not a or not b:
        return amount
    return amount * a / b


def clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def centroid(db, city, country):
    q = "SELECT AVG(lat), AVG(lng), COUNT(*) FROM public_listings WHERE lower(city)=lower(?)"
    args = [city]
    if country:
        q += " AND country=?"
        args.append(country)
    r = db.execute(q, args).fetchone()
    if not r[2]:
        q = q.replace("lower(city)=lower(?)", "lower(city) LIKE '%'||lower(?)||'%'")
        r = db.execute(q, args).fetchone()
    if not r[2]:
        raise ValueError(f"No verified listings found for city '{city}'. Pass lat/lng instead.")
    return r[0], r[1]


def load_weights(db):
    w = dict(DEFAULT_W)
    rows = db.execute("SELECT name, w, n FROM learned_weights").fetchall()
    if not rows:
        return w, 0.0
    n = max(r[2] for r in rows)
    alpha = min(0.5, n / (n + 500.0))          # learned weights never exceed 50% influence
    learned = {r[0]: max(0.0, r[1]) for r in rows if r[0] in FEATURES}
    tot = sum(learned.values())
    if tot <= 0:
        return w, 0.0
    for f in FEATURES:
        w[f] = (1 - alpha) * DEFAULT_W[f] + alpha * learned.get(f, 0.0) / tot
    return w, alpha


def user_weights(base, priorities):
    w = dict(base)
    for k, mult in (priorities or {}).items():
        if k in w:
            w[k] *= max(0.0, float(mult))
    tot = sum(w.values()) or 1.0
    return {k: v / tot for k, v in w.items()}


# ---------------------------------------------------------------- context
def make_ctx(db, q):
    cur = (q.get("currency") or "EUR").upper()
    guests = int(q.get("guests") or 80)
    if q.get("lat") is not None and q.get("lng") is not None:
        anchor = (float(q["lat"]), float(q["lng"]))
    else:
        anchor = centroid(db, q["city"], q.get("country"))
    cats = [c for c in (q.get("categories") or CATS) if c in CATS]
    budget = float(q["budget"]) if q.get("budget") else None
    split_tot = sum(DEFAULT_SPLIT[c] for c in cats) or 1.0
    caps = {c: (budget * DEFAULT_SPLIT[c] / split_tot if budget else None) for c in cats}
    wanted = [s for s in (q.get("styles") or []) if s in STYLE_NAMES]
    base_w, alpha = load_weights(db)
    ctx = dict(cur=cur, guests=guests, anchor=anchor, cats=cats, budget=budget, caps=caps,
               wanted=wanted, date=q.get("date"), radius=float(q.get("radius_km") or 30),
               weights=user_weights(base_w, q.get("priorities")), learned_alpha=alpha, now=dt.date.today())
    # peer price medians (stated prices only), per category within the country if known
    ctx["peer"] = {}
    for c in cats:
        rows = db.execute("""SELECT price_min, price_max, currency, price_unit FROM public_listings
                             WHERE category=? AND price_min IS NOT NULL""", (c,)).fetchall()
        vals = [cost_of(r, guests, cur)[0] for r in rows]
        vals = [v for v in vals if v]
        ctx["peer"][c] = statistics.median(vals) if len(vals) >= 5 else None
    rated = db.execute("SELECT AVG(rating) FROM public_listings WHERE rating IS NOT NULL").fetchone()[0]
    ctx["prior_rating"] = rated or 4.2
    imps = dict(db.execute("SELECT listing_id, COUNT(*) FROM events WHERE type='impression' GROUP BY 1"))
    ctx["imps"], ctx["imps_total"] = imps, sum(imps.values())
    return ctx


def cost_of(row, guests, cur):
    """Expected cost in the couple's currency, or (None, None) if the vendor publishes no price."""
    lo, hi = row["price_min"], row["price_max"]
    if lo is None:
        return None, None
    base = (lo + hi) / 2 if hi else lo
    if row["price_unit"] == "per_person":
        base *= guests
    return fx(base, row["currency"], cur), row["price_unit"]


def candidates(db, cat, anchor, radius_km):
    lat, lng = anchor
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    rows = db.execute("""SELECT * FROM public_listings WHERE category=?
                         AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?""",
                      (cat, lat - dlat, lat + dlat, lng - dlng, lng + dlng)).fetchall()
    return rows


def own_reviews(db, ids):
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r[0]: (r[1], r[2]) for r in db.execute(
        f"SELECT listing_id, AVG(rating), COUNT(*) FROM reviews WHERE status='approved' "
        f"AND listing_id IN ({q}) GROUP BY 1", ids)}


def availability_for(db, ids, day):
    if not day or not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in db.execute(
        f"SELECT listing_id, status FROM availability WHERE day=? AND listing_id IN ({q})", [day, *ids])}


# ---------------------------------------------------------------- scoring
WHY = {
    "price": "fits your budget", "quality": "well rated", "style": "matches your style",
    "proximity": "close to your location", "confidence": "details recently verified",
    "availability": "free on your date", "completeness": "complete contact details",
    "freshness": "checked recently",
}


def score_row(ctx, row, cat, anchor, own=None, avail=None):
    """Return a result dict, or None if a hard filter removes the vendor."""
    d = haversine(anchor[0], anchor[1], row["lat"], row["lng"])
    radius = ctx["radius"] * (1.0 if cat == "venue" else 2.0)
    if d > radius:
        return None
    if avail == "busy":
        return None
    if cat == "venue" and row["capacity_max"] and row["capacity_max"] < ctx["guests"]:
        return None

    f = {}
    # price
    cost, unit = cost_of(row, ctx["guests"], ctx["cur"])
    cap = ctx["caps"].get(cat)
    ref = cap or ((ctx["peer"].get(cat) or 0) * 1.3 or None)
    if cost is None:
        f["price"] = 0.45
        basis = "unknown"
    elif ref:
        r = cost / ref
        f["price"] = 0.7 + 0.3 * (1 - r) if r <= 1 else max(0.0, 0.7 - 1.5 * (r - 1))
        basis = "stated"
    else:
        f["price"] = 0.5
        basis = "stated"
    f["price"] = clip(f["price"])

    # quality: Bayesian average of vendor-site rating and approved couple reviews
    own_avg, own_n = own or (None, 0)
    site_n = min(row["rating_count"] or 0, 50) if row["rating"] else 0
    if own_n or site_n:
        m, C = 8.0, ctx["prior_rating"]
        num = (own_n * 2 * (own_avg or 0)) + site_n * (row["rating"] or 0) + m * C
        den = own_n * 2 + site_n + m
        R = num / den
        f["quality"] = clip((R - 3.0) / 2.0)
    else:
        f["quality"] = 0.40


    if own_n:
        rating, rating_n, rating_source = round(own_avg, 2), own_n, "Wedding Planner couples"
    else:
        rating, rating_n, rating_source = row["rating"], row["rating_count"] or 0, row["rating_source"]

    # style
    tags = [t for t in (row["style_tags"] or "").split(",") if t]
    if not ctx["wanted"]:
        f["style"] = 0.5
    elif not tags:
        f["style"] = 0.35
    else:
        f["style"] = len(set(ctx["wanted"]) & set(tags)) / len(ctx["wanted"])

    f["proximity"] = math.exp(-d / (0.5 * radius))
    f["confidence"] = clip(row["confidence"] or 0)
    f["availability"] = {"free": 1.0, None: 0.5}.get(avail, 0.5)
    comp = (0.3 * bool(row["website"]) + 0.2 * bool(row["phone"]) + 0.1 * bool(row["email"]) +
            0.1 * bool(row["photo_url"]) + 0.2 * (cost is not None) + 0.1 * bool(row["address"]))
    f["completeness"] = comp
    try:
        days = (ctx["now"] - dt.date.fromisoformat((row["last_checked_at"] or "")[:10])).days
        f["freshness"] = math.exp(-max(days, 0) / 60.0)
    except ValueError:
        f["freshness"] = 0.3

    w = ctx["weights"]
    base = sum(w[k] * f[k] for k in FEATURES)
    n_i = ctx["imps"].get(row["id"], 0)
    explore = min(0.05, 0.03 * math.sqrt(math.log(ctx["imps_total"] + 2) / (n_i + 1)))  # new vendors get seen
    score = clip(base + explore)

    contrib = sorted(((w[k] * f[k], k) for k in FEATURES if f[k] >= 0.6), reverse=True)
    why = [WHY[k] for _, k in contrib[:3]]
    caveats = []
    if basis == "unknown":
        caveats.append("No published price: request a quote")
    if avail is None and ctx["date"]:
        caveats.append("Availability for your date not confirmed: ask the vendor")
    if rating is None:
        caveats.append("No ratings yet")

    item = {
        "id": row["id"], "category": cat, "name": row["name"], "city": row["city"],
        "score": round(score * 100), "distance_km": round(d, 1),
        "cost": {"amount": round(cost) if cost else None, "currency": ctx["cur"], "basis": basis,
                 "unit": unit, "source_url": row["price_source_url"], "checked_at": row["price_checked_at"]},
        "rating": rating, "rating_count": rating_n, "rating_source": rating_source, "styles": tags,
        "description": row["description"],
        "description_source_url": (row["description_source_url"] or row["source_url"]) if row["description"] else None,
        "availability": {"free": "available", None: "unknown"}.get(avail, "unknown"),
        "why": why, "caveats": caveats, "photo": row["photo_url"],
        "confidence": row["confidence"], "last_checked_at": row["last_checked_at"],
        "source_url": row["source_url"], "google_place_id": row["google_place_id"],
        "_row": row, "_features": {k: round(v, 4) for k, v in f.items()},
    }
    return item


def rank_category(db, ctx, cat, anchor=None, limit=None):
    anchor = anchor or ctx["anchor"]
    radius = ctx["radius"] * (1.0 if cat == "venue" else 2.0)
    rows = candidates(db, cat, anchor, radius)
    ids = [r["id"] for r in rows]
    own, av = own_reviews(db, ids), availability_for(db, ids, ctx["date"])
    items = [it for it in (score_row(ctx, r, cat, anchor, own.get(r["id"]), av.get(r["id"])) for r in rows) if it]
    items.sort(key=lambda x: (-x["score"], x["distance_km"]))
    return items[:limit] if limit else items


def attach_photos(db, items, per=6):
    """Photos are hot-linked from the vendor's own site: always show the source link and credit the vendor."""
    ids = [i["id"] for i in items]
    if not ids:
        return
    q = ",".join("?" * len(ids))
    ph = {}
    for lid, url, alt, src in db.execute(
            f"SELECT listing_id, url, alt, source_url FROM photos WHERE listing_id IN ({q}) ORDER BY id", ids):
        if len(ph.setdefault(lid, [])) < per:
            ph[lid].append({"url": url, "alt": alt, "source": src})
    for i in items:
        i["photos"] = ph.get(i["id"], [])


def public_full(db, item, base="", sid="", pos=0):
    attach_photos(db, [item])
    return public(item, base, sid, pos)


def public(item, base="", sid="", pos=0):
    """Strip internals and attach one-tap actions. All actions route via /go so clicks are learned from."""
    r = item["_row"]
    out = {k: v for k, v in item.items() if not k.startswith("_")}
    g = lambda to: f"{base}/go/{item['id']}?to={to}&sid={sid}&pos={pos}"
    acts = {}
    if r["website"]:
        acts["website"] = g("website")
    if r["phone"]:
        acts["call"] = g("call")
    if r["email"]:
        acts["email"] = g("email")
    acts["map"] = g("map")
    out["actions"] = acts
    out["contact_preview"] = {"phone": r["phone"], "email": r["email"], "website": r["website"]}
    keys = r.keys()
    out["socials"] = {k: r[k] for k in ("instagram", "facebook", "tiktok", "pinterest", "youtube", "linkedin",
                                         "whatsapp") if k in keys and r[k]}
    out.setdefault("photos", [])
    return out


# ---------------------------------------------------------------- search + packages
def search(db, q, base="", sid="", limit=10):
    ctx = make_ctx(db, q)
    result = {}
    for cat in ctx["cats"]:
        items = rank_category(db, ctx, cat)
        result[cat] = items
    return ctx, result


def _est_cost(ctx, item, cat):
    """Cost used for budget feasibility. Unpriced vendors use the peer median, else the category budget share."""
    c = item["cost"]["amount"]
    if c is not None:
        return c, False
    peer = ctx["peer"].get(cat)
    if peer:
        return peer, True
    cap = ctx["caps"].get(cat)
    return (cap * 0.9 if cap else 0), True


def build_packages(db, q, base="", sid="", top_n=3, pool=6, venues_to_try=8, method="auto"):
    """method: 'auto' uses the exact CP-SAT optimiser when OR-Tools is installed, else the beam search below."""
    if method in ("auto", "cpsat"):
        try:
            import optimizer
            return optimizer.build(db, q, base, sid, top_n)
        except ImportError:
            if method == "cpsat":
                raise
    ctx = make_ctx(db, q)
    cats = ctx["cats"]
    first = {c: rank_category(db, ctx, c, limit=15) for c in cats}
    others = [c for c in cats if c != "venue"]
    anchors = first["venue"][:venues_to_try] if "venue" in cats else [None]
    if "venue" in cats and not anchors:
        return ctx, []
    imp_tot = sum(CAT_IMPORTANCE[c] for c in cats)
    combos, seen = [], set()
    for v in anchors:
        pools = []
        for c in others:
            if v:
                loc = (v["_row"]["lat"], v["_row"]["lng"])
                rs = [score_row(ctx, it["_row"], c, loc, own=None, avail=None) for it in first[c]]
                # keep availability/own review info from the first pass
                rs = [r for r in rs if r]
                by_id = {it["id"]: it for it in first[c]}
                for r in rs:
                    r["availability"] = by_id[r["id"]]["availability"]
                    r["rating"], r["rating_count"] = by_id[r["id"]]["rating"], by_id[r["id"]]["rating_count"]
                rs.sort(key=lambda x: -x["score"])
            else:
                rs = first[c]
            pools.append(rs[:pool])
        if any(not p for p in pools):
            continue
        for combo in itertools.product(*pools) if pools else [()]:
            items = ([v] if v else []) + list(combo)
            key = tuple(sorted(i["id"] for i in items))
            if key in seen:
                continue
            seen.add(key)
            total, unpriced = 0.0, []
            for it in items:
                c, est = _est_cost(ctx, it, it["category"])
                total += c
                if est:
                    unpriced.append(it["category"])
            obj = sum(CAT_IMPORTANCE[i["category"]] * i["score"] for i in items) / imp_tot
            combos.append((obj, total, items, unpriced))
    if not combos:
        return ctx, []
    budget = ctx["budget"]
    feas = [c for c in combos if budget is None or c[1] <= budget]
    over = False
    if not feas:                                   # nothing fits: show the closest, clearly flagged
        feas, over = sorted(combos, key=lambda c: c[1])[:20], True
    best = max(feas, key=lambda c: c[0])
    value = max(feas, key=lambda c: c[0] / max(c[1], 1))
    cheap_pool = [c for c in feas if c[0] >= 0.75 * best[0]] or feas
    cheap = min(cheap_pool, key=lambda c: c[1])
    packages, used = [], set()
    for label, pick in (("Best match", best), ("Best value", value), ("Lowest price", cheap)):
        key = tuple(sorted(i["id"] for i in pick[2]))
        if key in used:
            continue
        used.add(key)
        packages.append({
            "label": label, "score": round(pick[0]), "estimated_total": round(pick[1]),
            "currency": ctx["cur"], "budget": budget,
            "budget_left": round(budget - pick[1]) if budget else None,
            "over_budget": bool(over or (budget and pick[1] > budget)),
            "needs_quote": sorted(set(pick[3])),
            "items": [public(i, base, sid, n) for n, i in enumerate(pick[2])],
            "note": "Totals mix stated prices and peer-median estimates; vendors marked needs_quote must confirm.",
        })
    return ctx, packages[:top_n]
