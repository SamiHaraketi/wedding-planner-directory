#!/usr/bin/env python3
"""HTTP API for the Wedding Planner app.

Run:  WEDDING_DB=live.sqlite SECRET=change-me ADMIN_TOKEN=change-me uvicorn api:app --host 0.0.0.0 --port 8000

GET  /search?city=Lisboa&country=PT&categories=venue,photographer&budget=25000&guests=80&styles=rustic,garden&date=2027-06-12
GET  /packages?...same params...        -> complete wedding packages inside the budget
GET  /go/{id}?to=website|call|email|map[&format=json] -> logs the click, then sends the couple straight
                                         to the vendor (302), or returns {"url"} for native apps
POST /reviews                            -> couples' own reviews (held for moderation)
GET  /listing/{id}/live                  -> live Google rating/photos/hours (needs GOOGLE_API_KEY), never stored
Vendor self-service (link carries an HMAC token; send it via /admin/claim-link):
POST /vendor/{id}/availability?token=    {"busy": ["2027-06-12"], "free": [...]}
POST /vendor/{id}/price?token=           {"price_min": 3500, "price_max": 6000, "currency": "EUR", "unit": "total"}
POST /vendor/{id}/ical?token=            {"url": "https://.../calendar.ics"}
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import List, Optional

import ranker
import nlq
import suggest

DB_PATH = os.environ.get("WEDDING_DB", "live.sqlite")
SECRET = os.environ.get("SECRET", "dev-secret-change-me").encode()
ADMIN = os.environ.get("ADMIN_TOKEN", "dev-admin")
app = FastAPI(title="Wedding Planner API")


def conn():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ranker.init(db)
    return db


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def token_for(lid):
    return hmac.new(SECRET, str(lid).encode(), hashlib.sha256).hexdigest()[:24]


def check_token(lid, token):
    if not hmac.compare_digest(token_for(lid), token or ""):
        raise HTTPException(403, "bad token")


def parse_q(city, country, lat, lng, categories, budget, guests, styles, date, currency, radius_km, priorities):
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    pr = {}
    for part in (priorities or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                pr[k.strip()] = float(v)
            except ValueError:
                pass
    return dict(city=city, country=country, lat=lat, lng=lng,
                categories=[c for c in (categories or "").split(",") if c] or None,
                budget=budget, guests=guests, styles=[s for s in (styles or "").split(",") if s],
                date=date, currency=currency, radius_km=radius_km, priorities=pr)


def log_events(db, sid, q, items_by_cat):
    rows = []
    for cat, items in items_by_cat.items():
        for pos, it in enumerate(items):
            rows.append((now(), sid, "impression", it["id"], cat, pos, None, json.dumps(q),
                         json.dumps(it["_features"])))
    db.executemany("""INSERT INTO events(ts,sid,type,listing_id,category,position,target,query_json,features_json)
                      VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    db.commit()


COMMON = dict(city=Query(None), country=Query(None), lat=Query(None), lng=Query(None),
              categories=Query("venue,photographer,music,catering,decoration"), budget=Query(None),
              guests=Query(80), styles=Query(""), date=Query(None), currency=Query("EUR"),
              radius_km=Query(30), priorities=Query(""))


@app.get("/search")
def search(request: Request, city: Optional[str] = None, country: Optional[str] = None,
           lat: Optional[float] = None, lng: Optional[float] = None,
           categories: str = "venue,photographer,music,catering,decoration", budget: Optional[float] = None,
           guests: int = 80, styles: str = "", date: Optional[str] = None, currency: str = "EUR",
           radius_km: float = 30, priorities: str = "", limit: int = 10, sid: Optional[str] = None):
    sid = sid or uuid.uuid4().hex[:16]
    q = parse_q(city, country, lat, lng, categories, budget, guests, styles, date, currency, radius_km, priorities)
    if not city and lat is None:
        raise HTTPException(400, "city or lat/lng required")
    db = conn()
    try:
        ctx, res = ranker.search(db, q)
    except ValueError as e:
        raise HTTPException(404, str(e))
    shown = {c: items[:limit] for c, items in res.items()}
    for items in shown.values():
        ranker.attach_photos(db, items)
    log_events(db, sid, q, shown)
    base = str(request.base_url).rstrip("/")
    return {"sid": sid, "weights": {k: round(v, 3) for k, v in ctx["weights"].items()},
            "learned_influence": round(ctx["learned_alpha"], 2),
            "results": {c: [ranker.public(it, base, sid, i) for i, it in enumerate(items)]
                        for c, items in shown.items()},
            "counts": {c: len(v) for c, v in res.items()}}


@app.get("/packages")
def packages(request: Request, city: Optional[str] = None, country: Optional[str] = None,
             lat: Optional[float] = None, lng: Optional[float] = None,
             categories: str = "venue,photographer,music,catering,decoration", budget: Optional[float] = None,
             guests: int = 80, styles: str = "", date: Optional[str] = None, currency: str = "EUR",
             radius_km: float = 30, priorities: str = "", sid: Optional[str] = None):
    sid = sid or uuid.uuid4().hex[:16]
    q = parse_q(city, country, lat, lng, categories, budget, guests, styles, date, currency, radius_km, priorities)
    if not city and lat is None:
        raise HTTPException(400, "city or lat/lng required")
    db = conn()
    try:
        ctx, pk = ranker.build_packages(db, q, base=str(request.base_url).rstrip("/"), sid=sid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    # log impressions of the vendors shown in packages (features are re-derived by /search later)
    return {"sid": sid, "packages": pk}


@app.get("/go/{lid}")
def go(lid: int, to: str = "website", sid: str = "", pos: int = 0, format: str = "redirect"):
    """Log the click, then send the couple straight to the vendor. Only stored vendor URLs are ever used."""
    db = conn()
    r = db.execute("SELECT * FROM public_listings WHERE id=?", (lid,)).fetchone()
    if not r:
        raise HTTPException(404, "listing not available")
    target = None
    if to == "website" and r["website"]:
        target = r["website"]
    elif to == "call" and r["phone"]:
        target = "tel:" + re.sub(r"[^\d+]", "", r["phone"])
    elif to == "email" and r["email"]:
        target = "mailto:" + r["email"]
    elif to == "map":
        target = r["source_url"] or f"https://www.google.com/maps/search/?api=1&query={r['lat']},{r['lng']}"
    if not target or not re.match(r"^(https?://|tel:|mailto:)", target, re.I):
        raise HTTPException(404, "no such contact for this vendor")
    db.execute("""INSERT INTO events(ts,sid,type,listing_id,category,position,target)
                  VALUES (?,?,?,?,?,?,?)""", (now(), sid, "click", lid, r["category"], pos, to))
    db.commit()
    if format == "json":                      # native apps: open the tel:/mailto:/https link yourself
        return {"url": target}
    return RedirectResponse(target, status_code=302)


class ReviewIn(BaseModel):
    listing_id: int
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=2000)
    sid: str = ""


@app.post("/reviews")
def add_review(r: ReviewIn):
    db = conn()
    if not db.execute("SELECT 1 FROM listings WHERE id=?", (r.listing_id,)).fetchone():
        raise HTTPException(404, "unknown listing")
    db.execute("INSERT INTO reviews(listing_id,rating,text,sid,created_at,status) VALUES (?,?,?,?,?, 'pending')",
               (r.listing_id, r.rating, r.text, r.sid, now()))
    db.commit()
    return {"status": "pending moderation"}


@app.post("/admin/reviews/{rid}/approve")
def approve(rid: int, admin: str):
    if not hmac.compare_digest(admin, ADMIN):
        raise HTTPException(403)
    db = conn()
    db.execute("UPDATE reviews SET status='approved' WHERE id=?", (rid,))
    db.commit()
    return {"ok": True}


@app.get("/admin/claim-link/{lid}")
def claim_link(lid: int, admin: str, request: Request):
    if not hmac.compare_digest(admin, ADMIN):
        raise HTTPException(403)
    base = str(request.base_url).rstrip("/")
    return {"availability": f"{base}/vendor/{lid}/availability?token={token_for(lid)}", "token": token_for(lid)}


class AvailIn(BaseModel):
    busy: List[str] = []
    free: List[str] = []


@app.post("/vendor/{lid}/availability")
def vendor_availability(lid: int, body: AvailIn, token: str):
    check_token(lid, token)
    db = conn()
    rows = []
    for status, days in (("busy", body.busy), ("free", body.free)):
        for d in days:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise HTTPException(400, f"bad date {d}")
            rows.append((lid, d, status, "vendor", now()))
    db.executemany("INSERT OR REPLACE INTO availability(listing_id,day,status,source,updated_at) VALUES (?,?,?,?,?)", rows)
    db.execute("UPDATE listings SET vendor_verified=1 WHERE id=?", (lid,))
    db.commit()
    return {"saved": len(rows)}


class PriceIn(BaseModel):
    price_min: float = Field(gt=0, lt=1_000_000)
    price_max: Optional[float] = None
    currency: str = "EUR"
    unit: str = "total"


@app.post("/vendor/{lid}/price")
def vendor_price(lid: int, body: PriceIn, token: str):
    check_token(lid, token)
    if body.unit not in ("total", "per_person"):
        raise HTTPException(400, "unit must be total or per_person")
    db = conn()
    db.execute("""UPDATE listings SET price_min=?, price_max=?, currency=?, price_unit=?,
                  price_source_url='vendor-submitted', price_checked_at=?, vendor_verified=1 WHERE id=?""",
               (body.price_min, body.price_max, body.currency.upper(), body.unit, now()[:10], lid))
    db.commit()
    return {"ok": True}


class IcalIn(BaseModel):
    url: str


@app.post("/vendor/{lid}/ical")
def vendor_ical(lid: int, body: IcalIn, token: str):
    check_token(lid, token)
    if not re.match(r"^https?://", body.url, re.I):
        raise HTTPException(400, "url must be http(s)")
    db = conn()
    db.execute("UPDATE listings SET ical_url=?, vendor_verified=1 WHERE id=?", (body.url, lid))
    db.commit()
    return {"ok": True}


_live_cache = {}


@app.get("/listing/{lid}/live")
def live(lid: int):
    """Live Google details (rating, hours, Maps link). Fetched on demand and held in memory for 10 minutes only:
    Google's terms do not allow storing this content, only the place_id."""
    import enrich_google
    db = conn()
    r = db.execute("SELECT google_place_id FROM public_listings WHERE id=?", (lid,)).fetchone()
    if not r or not r["google_place_id"]:
        raise HTTPException(404, "no Google place linked")
    hit = _live_cache.get(lid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    data = enrich_google.live_details(r["google_place_id"])
    _live_cache[lid] = (time.time(), data)
    return data


@app.get("/suggest/cities")
def suggest_cities(q: str = "", limit: int = 8):
    """Typeahead for the city field. Returns suggestions plus `ghost`, the greyed inline completion."""
    return suggest.suggest_cities(conn(), q[:60], min(limit, 20))


@app.get("/parse")
def parse_text(q: str, use_llm: bool = True):
    """Turn free text ('rustic vineyard near Sintra under 20k for 80 guests in June') into validated filters."""
    db = conn()
    cities = [r[0] for r in db.execute("SELECT DISTINCT city FROM public_listings WHERE city IS NOT NULL")]
    filters, method = nlq.parse(q[:500], cities, use_llm)
    return {"filters": filters, "method": method}


@app.get("/ask")
def ask(request: Request, q: str, sid: Optional[str] = None, use_llm: bool = True):
    """One box for everything: parse the couple's sentence, then return packages for it."""
    sid = sid or uuid.uuid4().hex[:16]
    db = conn()
    cities = [r[0] for r in db.execute("SELECT DISTINCT city FROM public_listings WHERE city IS NOT NULL")]
    f, method = nlq.parse(q[:500], cities, use_llm)
    missing = [k for k in ("city", "budget", "guests") if k not in f]
    if "city" not in f:
        return {"sid": sid, "filters": f, "method": method, "missing": missing, "packages": [],
                "message": "Which city is the wedding in?"}
    query = dict(city=f["city"], country=None, lat=None, lng=None, categories=f.get("categories"),
                 budget=f.get("budget"), guests=f.get("guests", 80), styles=f.get("styles", []),
                 date=f.get("date"), currency=f.get("currency", "EUR"), radius_km=30,
                 priorities=f.get("priorities", {}))
    ctx, pk = ranker.build_packages(db, query, base=str(request.base_url).rstrip("/"), sid=sid)
    return {"sid": sid, "filters": f, "method": method, "missing": missing, "packages": pk,
            "message": None if not missing else "Add " + " and ".join(missing) + " to sharpen the results."}
