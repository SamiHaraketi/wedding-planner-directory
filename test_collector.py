#!/usr/bin/env python3
"""Collector tests against local fake vendor websites. Run: python test_collector.py"""
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import urllib.parse

os.environ.update(COLLECTOR_ALLOW_PRIVATE="1", COLLECTOR_DELAY="0")

tmp = tempfile.mkdtemp()


def site(files):
    """Start a tiny web server whose root serves `files` {path: content}. Returns base URL."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            if p.path == "/search" and "__search__" in files:
                q = urllib.parse.parse_qs(p.query)["q"][0]
                body = files["__search__"](q)
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(body).encode()); return
            key = p.path if p.path in files else (p.path.rstrip("/") + "/" if p.path.rstrip("/") + "/" in files else None)
            if key is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain" if key == "/robots.txt" else "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(files[key].encode())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


# ---- vendor 1: has a website; contact/gallery/prices pages; JSON-LD rating; socials
v1 = site({
    "/robots.txt": "User-agent: *\nDisallow: /private/\n",
    "/": """<html><head><title>Quinta Alegria</title><meta property="og:image" content="/img/hero.jpg">
<script type="application/ld+json">{"@type":"EventVenue","name":"Quinta Alegria","telephone":"+351 912 345 678",
"address":{"streetAddress":"Rua das Flores 12","postalCode":"1200-001","addressLocality":"Lisboa"},
"aggregateRating":{"ratingValue":"4.8","reviewCount":"57"},"image":["/img/a.jpg"]}</script></head>
<body><h1>Quinta Alegria</h1><p>Rustic garden wedding venue in Lisboa. Rustic barn, garden ceremonies, garden lunch.
Our estate holds up to 150 guests.</p>
<a href="/contact/">Contact</a> <a href="/gallery/">Gallery</a> <a href="/prices/">Prices</a> <a href="/private/x">x</a>
<a href="https://www.instagram.com/quintaalegria/">IG</a> <a href="https://www.facebook.com/quintaalegria.lisboa">FB</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a> <a href="https://www.instagram.com/p/ABC123/">post</a></body></html>""",
    "/contact/": """<html><body><h1>Contact Quinta Alegria</h1><a href="tel:+351912345678">Call</a>
<a href="mailto:hello@quintaalegria.example">Mail</a> <a href="mailto:sentry@sentry.io">bad</a></body></html>""",
    "/gallery/": """<html><body><h1>Quinta Alegria gallery</h1>
<img src="/img/g1.jpg" alt="Garden ceremony" width="1200"><img src="/img/g2.jpg" alt="Dinner" width="1200">
<img src="/img/logo.png"><img src="/img/tiny.jpg" width="50" height="50"><img src="/img/g3.webp" alt="Barn"></body></html>""",
    "/prices/": "<html><body><h1>Quinta Alegria prices</h1><p>Wedding packages from €2,500</p></body></html>",
})

# ---- vendor 2: no website in our data; must be FOUND via search and verified (name + phone)
v2 = site({
    "/robots.txt": "User-agent: *\nAllow: /\n",
    "/": """<html><head><title>Foto Marta Lisboa Photography</title></head><body><h1>Foto Marta Lisboa Photography</h1>
<p>Wedding photographer in Lisboa. Call +351 933 333 333</p><a href="https://www.instagram.com/fotomarta/">IG</a>
<img src="/p1.jpg" alt="Bride" width="900"></body></html>""",
})

# ---- wrong business: same words in the name, different city and phone -> must be REJECTED
wrong = site({
    "/robots.txt": "User-agent: *\nAllow: /\n",
    "/": """<html><head><title>Studio Zeta Wedding Films</title></head><body><h1>Studio Zeta Wedding Films</h1>
<p>Madrid based. +34 600 111 222</p></body></html>""",
})

# ---- parked domain
parked = site({"/robots.txt": "User-agent: *\nAllow: /\n",
               "/": "<html><body>This domain is for sale! Buy this domain today.</body></html>"})

# ---- vendor that forbids bots
blocked = site({"/robots.txt": "User-agent: *\nDisallow: /\n", "/": "<html><body>Blocked Venue +351900000000</body></html>"})

search = site({"__search__": lambda q: {"results": (
    [{"url": "https://www.facebook.com/fotomarta.lisboa", "title": "Foto Marta Lisboa Photography | Facebook"},
     {"url": "https://www.tripadvisor.com/x", "title": "Foto Marta Lisboa Photography - Tripadvisor"},
     {"url": v2 + "/", "title": "Foto Marta Lisboa Photography"}] if "Marta" in q else
    [{"url": wrong + "/", "title": "Studio Zeta Wedding Films"}] if "Zeta" in q else [])}})
os.environ["SEARXNG_URL"] = search

import ranker
import collector
from import_osm import SCHEMA

DB = os.path.join(tmp, "c.sqlite")
db = sqlite3.connect(DB)
db.executescript(SCHEMA)
ranker.init(db)


def add(i, cat, name, city, phone, website):
    db.execute("""INSERT INTO listings(osm_type,osm_id,category,name,lat,lng,city,country,phone,website,status,
        dedupe_key,confidence,last_checked_at) VALUES('n',?,?,?,38.7,-9.1,?, 'PT', ?,?,'verified_web',?,0.8,date('now'))""",
               (i, cat, name, city, phone, website, str(i)))


add(1, "venue", "Quinta Alegria", "Lisboa", None, v1 + "/")
add(2, "photographer", "Foto Marta Lisboa Photography", "Lisboa", "+351933333333", None)
add(3, "photographer", "Studio Zeta", "Porto", "+351966666666", None)
add(4, "venue", "Parked Venue", "Lisboa", None, parked + "/")
add(5, "venue", "Blocked Venue", "Lisboa", None, blocked + "/")
db.commit()
db.close()

tot = collector.run_batch(DB, batch=50, workers=4)
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row
g = lambda i: db.execute("SELECT * FROM listings WHERE id=?", (i,)).fetchone()

a = g(1)
assert a["phone"] == "+351912345678", a["phone"]
assert a["email"] == "hello@quintaalegria.example", a["email"]
assert a["instagram"] == "https://www.instagram.com/quintaalegria/", a["instagram"]
assert a["facebook"] == "https://www.facebook.com/quintaalegria.lisboa", a["facebook"]
assert a["rating"] == 4.8 and a["rating_count"] == 57 and a["rating_source"] == "vendor-site"
assert a["capacity_max"] == 150 and a["price_min"] == 2500 and a["address"].startswith("Rua das Flores 12")
assert "rustic" in a["style_tags"] and "garden" in a["style_tags"]
photos = [r[0] for r in db.execute("SELECT url FROM photos WHERE listing_id=1")]
assert any(p.endswith("/img/g1.jpg") for p in photos) and not any("logo" in p or "tiny" in p for p in photos), photos
assert len(photos) >= 4
fs = {r[0]: r[1] for r in db.execute("SELECT field, source_url FROM field_sources WHERE listing_id=1")}
assert fs["phone"].startswith(v1) and "email" in fs and "instagram" in fs
print("1 ok  vendor 1: phone, email, socials, rating, capacity, price, address, styles, photos + provenance;",
      "private page, share links, logo and tiny images ignored; photos:", len(photos))

b = g(2)
assert b["website"] == v2 + "/", b["website"]
assert b["facebook"] == "https://www.facebook.com/fotomarta.lisboa", b["facebook"]
assert b["instagram"] == "https://www.instagram.com/fotomarta/"
assert db.execute("SELECT method FROM field_sources WHERE listing_id=2 AND field='website'").fetchone()[0] == "search+identity-verified"
print("2 ok  vendor 2: website FOUND through search and identity-verified (name + phone); Facebook captured; Tripadvisor not used as website")

c = g(3)
assert c["website"] is None and c["collect_fail"] >= 0
print("3 ok  wrong business with same words in the name was REJECTED")

assert g(4)["phone"] is None and g(4)["collect_fail"] == 1
print("4 ok  parked domain rejected")
assert g(5)["phone"] is None and g(5)["collect_fail"] == 1
assert db.execute("SELECT new FROM changes WHERE listing_id=5").fetchone()[0] == "robots"
print("5 ok  robots.txt Disallow respected (nothing fetched from that site)")

# re-run: nothing is due again (next check scheduled), so the loop does no redundant work
again = collector.run_batch(DB, batch=50, workers=4)
assert again["listings"] == 0, again
print("6 ok  schedule: second pass has nothing due (rechecks are spaced out, failures back off)")

# SSRF guard
collector.ALLOW_PRIVATE = False
assert collector.http_get("http://127.0.0.1:9/")[0] == 0
assert collector.http_get("http://169.254.169.254/latest/meta-data/")[0] == 0
assert collector.http_get("file:///etc/passwd")[0] == 0
collector.ALLOW_PRIVATE = True
print("7 ok  SSRF guard blocks loopback, cloud-metadata and file: URLs")

# collected data flows into the API results
os.environ["WEDDING_DB"] = DB
from fastapi.testclient import TestClient
import api
r = TestClient(api.app).get("/search", params={"city": "Lisboa", "country": "PT", "categories": "venue",
                                               "limit": 5})
assert r.status_code == 200, r.text
body = r.json()
v = [x for x in body["results"]["venue"] if x["id"] == 1]
print("API sample:", {k: (v[0][k] if k != "photos" else len(v[0][k])) for k in ("name", "socials", "photos", "rating")} if v else body["counts"])
assert v and v[0]["photos"] and v[0]["socials"]["instagram"] and v[0]["rating"]
print("8 ok  /search returns photos, socials, rating from the collector")
print("\nCOLLECTOR TESTS PASSED")
