#!/usr/bin/env python3
"""End-to-end tests on a synthetic database. Run: python test_engine.py"""
import datetime as dt
import json
import os
import random
import sqlite3
import tempfile
import threading
import http.server
import functools

random.seed(7)
tmp = tempfile.mkdtemp()
DB = os.path.join(tmp, "live.sqlite")
os.environ["WEDDING_DB"] = DB

import ranker
from import_osm import SCHEMA

db = sqlite3.connect(DB)
db.executescript(SCHEMA)
ranker.init(db)
today = dt.date.today().isoformat()
STY = ["rustic", "garden", "modern", "beach", "classic", "vineyard", "luxury"]
n = 0
for cat, count, base_price in (("venue", 40, 5000), ("catering", 30, 60), ("photographer", 40, 1800),
                               ("music", 25, 900), ("decoration", 30, 700)):
    for i in range(count):
        n += 1
        has_price = random.random() < 0.6
        pu = "per_person" if cat == "catering" else "total"
        db.execute("""INSERT INTO listings(osm_type,osm_id,category,name,lat,lng,city,country,phone,email,website,
            status,dedupe_key,confidence,last_checked_at,price_min,currency,price_unit,price_checked_at,
            rating,rating_count,rating_source,style_tags,photo_url,capacity_max)
            VALUES('n',?,?,?,?,?,?,?,?,?,?,'verified_web',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (n, cat, f"{cat.title()} {i}", 38.72 + random.uniform(-.15, .15), -9.14 + random.uniform(-.15, .15),
                    "Lisboa", "PT", f"+3519{random.randint(10000000, 99999999)}", f"info{n}@ex.com",
                    f"https://ex{n}.com", str(n), round(random.uniform(.6, 1), 2), today + "T00:00:00",
                    round(base_price * random.uniform(.5, 2.2)) if has_price else None, "EUR", pu,
                    today if has_price else None,
                    round(random.uniform(3.5, 5), 1) if random.random() < .5 else None, random.randint(3, 80),
                    "vendor-site", ",".join(random.sample(STY, random.choice([0, 1, 2]))) or None,
                    f"https://ex{n}.com/p.jpg", 150 if cat == "venue" else None))
db.commit()

Q = dict(city="Lisboa", country="PT", budget=25000, guests=80, styles=["rustic", "garden"],
         currency="EUR", date="2027-06-12")

# ---- 1. search ranks, explains, and respects hard filters
ctx, res = ranker.search(db, dict(Q))
assert set(res) == set(ranker.CATS), res.keys()
for cat, items in res.items():
    assert items, cat
    scores = [i["score"] for i in items]
    assert scores == sorted(scores, reverse=True)
top = res["venue"][0]
assert top["why"] or top["caveats"]
print("1 ok  top venue:", top["name"], top["score"], top["why"], top["caveats"])

# busy date excludes a vendor; free date boosts one
vid = res["venue"][0]["id"]
db.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (vid, Q["date"], "busy", "vendor", today))
db.commit()
_, res2 = ranker.search(db, dict(Q))
assert vid not in [i["id"] for i in res2["venue"]], "busy vendor must be excluded"
second = res2["venue"][0]["id"]
db.execute("INSERT INTO availability VALUES (?,?,?,?,?)", (second, Q["date"], "free", "vendor", today))
db.commit()
_, res3 = ranker.search(db, dict(Q))
assert [i for i in res3["venue"] if i["id"] == second][0]["availability"] == "available"
print("2 ok  availability filter and boost")

# capacity hard filter
db.execute("UPDATE listings SET capacity_max=20 WHERE id=?", (second,))
db.commit()
_, res4 = ranker.search(db, dict(Q))
assert second not in [i["id"] for i in res4["venue"]]
db.execute("UPDATE listings SET capacity_max=150 WHERE id=?", (second,))
db.commit()
print("3 ok  venue capacity filter")

# price matters: expensive-over-budget scores lower than cheap, all else equal
ctx = ranker.make_ctx(db, dict(Q))
row = db.execute("SELECT * FROM public_listings WHERE category='photographer' LIMIT 1").fetchone()
cheap = dict(row); expensive = dict(row)
cheap["price_min"], expensive["price_min"] = 500, 9000
cheap["price_max"] = expensive["price_max"] = None
cheap["price_unit"] = expensive["price_unit"] = "total"
class R(dict):
    pass
a = ranker.score_row(ctx, cheap, "photographer", ctx["anchor"])
b = ranker.score_row(ctx, expensive, "photographer", ctx["anchor"])
assert a["score"] > b["score"], (a["score"], b["score"])
assert a["_features"]["price"] > b["_features"]["price"]
print("4 ok  price fit: cheap", a["score"], "> over budget", b["score"])

# ---- 5. packages stay inside budget and are complete
ctx, pk = ranker.build_packages(db, dict(Q))
assert pk, "no packages"
for p in pk:
    cats = [i["category"] for i in p["items"]]
    assert sorted(cats) == sorted(ranker.CATS), cats
    assert p["estimated_total"] <= Q["budget"] and not p["over_budget"], p["estimated_total"]
    for i in p["items"]:
        assert "actions" in i and "map" in i["actions"]
print("5 ok  packages:", [(p["label"], p["estimated_total"], p["score"]) for p in pk])

# tiny budget -> flagged, never silently over
ctx, pk2 = ranker.build_packages(db, dict(Q, budget=500))
assert pk2 and all(p["over_budget"] for p in pk2)
print("6 ok  impossible budget flagged over_budget")

# ---- 7. API: search -> go redirect -> logged click -> learning
from fastapi.testclient import TestClient
import api
c = TestClient(api.app)
r = c.get("/search", params=dict(city="Lisboa", country="PT", categories="venue,photographer", budget=25000,
                                 guests=80, styles="rustic,garden", limit=5))
assert r.status_code == 200, r.text
body = r.json()
sid = body["sid"]
first = body["results"]["venue"][0]
g = c.get(first["actions"]["website"], follow_redirects=False)
assert g.status_code == 302 and g.headers["location"].startswith("https://ex"), g.headers
g2 = c.get(first["actions"]["call"] + "&format=json")
assert g2.json()["url"].startswith("tel:+"), g2.text
g3 = c.get(first["actions"]["email"] + "&format=json")
assert g3.json()["url"].startswith("mailto:"), g3.text
assert c.get("/go/999999").status_code == 404
d = sqlite3.connect(DB)
assert d.execute("SELECT COUNT(*) FROM events WHERE type='click'").fetchone()[0] == 3
assert d.execute("SELECT COUNT(*) FROM events WHERE type='impression'").fetchone()[0] == 10
print("7 ok  /search, /go redirects (website, call, email) and logging")

pk_r = c.get("/packages", params=dict(city="Lisboa", country="PT", budget=25000, guests=80, styles="rustic"))
assert pk_r.status_code == 200 and pk_r.json()["packages"]
assert c.get("/search", params=dict(city="Nowhere")).status_code == 404
print("8 ok  /packages and unknown city handling")

ask_response = c.post("/ask", json={"q": "Wedding in Lisboa under 25000 for 80 guests", "use_llm": False})
assert ask_response.status_code == 200, ask_response.text
first_turn = ask_response.json()
follow = c.post("/ask", json={"q": "Make it 100 guests", "filters": first_turn["filters"],
                              "sid": first_turn["sid"], "use_llm": False})
assert follow.status_code == 200, follow.text
follow_turn = follow.json()
assert follow_turn["sid"] == first_turn["sid"]
assert follow_turn["filters"]["city"] == "Lisboa"
assert follow_turn["filters"]["budget"] == 25000
assert follow_turn["filters"]["guests"] == 100
assert follow_turn["packages"], follow_turn
print("8b ok  conversational follow-up keeps the city and budget and rebuilds packages")


# vendor self-service: token required
lid = first["id"]
assert c.post(f"/vendor/{lid}/availability?token=bad", json={"busy": ["2027-07-01"]}).status_code == 403
tok = api.token_for(lid)
assert c.post(f"/vendor/{lid}/availability?token={tok}", json={"busy": ["2027-07-01"], "free": []}).status_code == 200
assert c.post(f"/vendor/{lid}/price?token={tok}",
              json={"price_min": 4200, "currency": "EUR", "unit": "total"}).status_code == 200
assert c.post(f"/vendor/{lid}/ical?token={tok}", json={"url": "javascript:alert(1)"}).status_code == 400
assert c.post("/reviews", json={"listing_id": lid, "rating": 5, "text": "great", "sid": sid}).status_code == 200
assert c.post("/reviews", json={"listing_id": lid, "rating": 9}).status_code == 422
print("9 ok  vendor token auth, price update, iCal URL validation, review moderation queue")

# ---- 10. learning: simulate couples who always click style-matching vendors
imps = []
users = 400
for u in range(users):
    s = f"u{u}"
    ctx = ranker.make_ctx(db, dict(Q))
    items = ranker.rank_category(db, ctx, "photographer", limit=8)
    for pos, it in enumerate(items):
        db.execute("""INSERT INTO events(ts,sid,type,listing_id,category,position,query_json,features_json)
                      VALUES (?,?,?,?,?,?,?,?)""", (today, s, "impression", it["id"], "photographer", pos, "{}",
                                                   json.dumps(it["_features"])))
        # behaviour: they click when style matches (80%), almost never otherwise
        p_click = 0.8 if it["_features"]["style"] >= 0.5 and "rustic" in it["styles"] + ["garden"] and it["styles"] else 0.03
        if random.random() < p_click * (1.0 if pos < 3 else 0.7):
            db.execute("INSERT INTO events(ts,sid,type,listing_id,category,position,target) VALUES (?,?,?,?,?,?,?)",
                       (today, s, "click", it["id"], "photographer", pos, "website"))
db.commit()
import learn
before = ranker.load_weights(db)[0]["style"]
learn.train(DB)
after, alpha = ranker.load_weights(sqlite3.connect(DB))
assert alpha > 0, "learned weights should be active"
assert after["style"] > before, (before, after["style"])
assert abs(sum(after.values()) - 1) < 1e-6
print(f"10 ok learning: style weight {before:.3f} -> {after['style']:.3f} (influence {alpha:.2f})")

# ---- 11. iCal parser
import sync_ical
ics = f"""BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:{(dt.date.today()+dt.timedelta(days=30)).strftime('%Y%m%d')}
DTEND;VALUE=DATE:{(dt.date.today()+dt.timedelta(days=32)).strftime('%Y%m%d')}
SUMMARY:Booked
END:VEVENT
BEGIN:VEVENT
DTSTART:{(dt.date.today()+dt.timedelta(days=50)).strftime('%Y%m%d')}T150000Z
DTEND:{(dt.date.today()+dt.timedelta(days=50)).strftime('%Y%m%d')}T230000Z
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:{(dt.date.today()+dt.timedelta(days=60)).strftime('%Y%m%d')}
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR"""
days = sync_ical.parse_ics(ics)
exp = {(dt.date.today() + dt.timedelta(days=d)).isoformat() for d in (30, 31, 50)}
assert days == exp, (days, exp)
print("11 ok iCal parsing (all-day range, timed event, cancelled ignored)")

# ---- 12. Google linking with mocked API (only place_id stored)
import enrich_google
os.environ["GOOGLE_API_KEY"] = "test"
enrich_google._post = lambda url, body, mask: {"places": [{"id": "ChIJtest", "displayName": {"text": body["textQuery"] + " Lda"}}]}
enrich_google.link(DB, limit=5)
d = sqlite3.connect(DB)
assert d.execute("SELECT COUNT(*) FROM listings WHERE google_place_id='ChIJtest'").fetchone()[0] == 5
print("12 ok Google place_id linking (mocked)")

# ---- 13. Overture import on a local parquet with the Overture schema
import duckdb
pq = os.path.join(tmp, "places.parquet")
con = duckdb.connect()
con.execute(f"""COPY (SELECT * FROM (VALUES
  ('id1', {{'primary':'Quinta do Lago Weddings'}}, {{'primary':'wedding_venue','alternate':['event_venue']}},
   {{'xmin':-9.2,'xmax':-9.2,'ymin':38.7,'ymax':38.7}}, ['https://quintadolago.example'], ['+351911000111'],
   ['hello@quintadolago.example'], [{{'freeform':'Rua 1','locality':'Lisboa','postcode':'1000','country':'PT'}}], 0.9),
  ('id2', {{'primary':'Foto Joao'}}, {{'primary':'photographer','alternate':[]::VARCHAR[]}},
   {{'xmin':-9.1,'xmax':-9.1,'ymin':38.8,'ymax':38.8}}, ['https://fotojoao.example'], []::VARCHAR[],
   []::VARCHAR[], [{{'freeform':'Rua 2','locality':'Sintra','postcode':'2710','country':'PT'}}], 0.8),
  ('id3', {{'primary':'Padaria Central'}}, {{'primary':'bakery','alternate':[]::VARCHAR[]}},
   {{'xmin':-9.1,'xmax':-9.1,'ymin':38.8,'ymax':38.8}}, []::VARCHAR[], []::VARCHAR[], []::VARCHAR[],
   [{{'freeform':'x','locality':'Y','postcode':'1','country':'PT'}}], 0.9),
  ('id4', {{'primary':'Quinta Verde Farm'}}, {{'primary':'farm','alternate':[]::VARCHAR[]}},
   {{'xmin':-9.1,'xmax':-9.1,'ymin':38.8,'ymax':38.8}}, []::VARCHAR[], []::VARCHAR[], []::VARCHAR[],
   [{{'freeform':'x','locality':'Y','postcode':'1','country':'PT'}}], 0.9)
) t(id, names, categories, bbox, websites, phones, emails, addresses, confidence))
 TO '{pq}' (FORMAT PARQUET)""")
OV = os.path.join(tmp, "ov.sqlite")
import subprocess, sys
out = subprocess.run([sys.executable, "overture_import.py", OV, "--parquet", pq], capture_output=True, text=True)
print(out.stdout.strip(), out.stderr.strip()[-300:])
o = sqlite3.connect(OV)
rows = o.execute("SELECT category,name,status,website FROM listings ORDER BY name").fetchall()
assert ("venue", "Quinta do Lago Weddings", "verified_contact", "https://quintadolago.example") in rows, rows
assert any(r[0] == "photographer" for r in rows)
assert not any(r[1] == "Padaria Central" for r in rows), "bakery must not import"
assert not any(r[1] == "Quinta Verde Farm" for r in rows), "plain farm without wedding signal must not import"
print("13 ok Overture import:", rows)

print("\nALL TESTS PASSED")
