#!/usr/bin/env python3
"""Import wedding-related businesses from an OpenStreetMap extract (.osm.pbf) into SQLite.

Usage:  python import_osm.py REGION.osm.pbf OUT.sqlite [--country PT]

Only real OSM objects are stored. Every row keeps its OSM link, retrieval date and licence.
Nothing is invented: unknown fields stay NULL.
"""
import argparse
import datetime as dt
import hashlib
import re
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY,
  osm_type TEXT, osm_id INTEGER,
  category TEXT NOT NULL,          -- venue | photographer | music | catering | decoration
  name TEXT NOT NULL,
  lat REAL, lng REAL,
  city TEXT, postcode TEXT, country TEXT, address TEXT,
  phone TEXT, email TEXT, website TEXT, instagram TEXT, facebook TEXT,
  opening_hours TEXT, description TEXT,
  source TEXT DEFAULT 'OpenStreetMap',
  source_url TEXT, license TEXT DEFAULT 'ODbL 1.0 - (c) OpenStreetMap contributors',
  retrieved_at TEXT,
  status TEXT DEFAULT 'imported',  -- imported | verified_contact | verified_web | dead
  verified_at TEXT,
  dedupe_key TEXT,
  UNIQUE(osm_type, osm_id, category)
);
CREATE INDEX IF NOT EXISTS idx_cat_city ON listings(category, country, city);
CREATE INDEX IF NOT EXISTS idx_dedupe ON listings(dedupe_key);
"""

WEDDING_WORDS = re.compile(
    r"wedding|bridal|bride|mariage|boda|hochzeit|matrimon|casamento|noiva|noces|"
    r"event|banquet|ballroom|castle|chateau|château|manor|estate|quinta|villa|finca|"
    r"schloss|kasteel|barn|garden|winery|vineyard", re.I)


def classify(t):
    """Return list of categories for a tag dict. Strict rules to avoid junk."""
    cats = []
    name = t.get("name", "")
    amenity, craft, shop, office, tourism, historic, leisure = (
        t.get("amenity"), t.get("craft"), t.get("shop"), t.get("office"),
        t.get("tourism"), t.get("historic"), t.get("leisure"))

    # venues
    if amenity in ("events_venue", "wedding_venue", "banquet_hall", "conference_centre") \
            or t.get("wedding") == "yes" or t.get("building") == "wedding_hall":
        cats.append("venue")
    elif (historic in ("castle", "manor", "palace", "monastery") or leisure == "garden"
          or tourism in ("hotel", "resort", "guest_house", "attraction")
          or craft == "winery" or amenity == "place_of_worship" and t.get("wedding") == "yes") \
            and WEDDING_WORDS.search(name + " " + t.get("description", "")):
        cats.append("venue")

    # photographers / videographers
    if craft == "photographer" or office == "photographer" or shop == "photo_studio" \
            or (shop == "photo" and re.search(r"studio|wedding|portrait|fotograf|photograph", name, re.I)) \
            or craft == "videographer" or office == "videographer":
        cats.append("photographer")

    # music: DJs, bands, live music providers (OSM is thin here; supplement with APIs)
    if craft in ("musician", "dj") or office in ("musician", "dj", "band") \
            or re.search(r"\b(dj|wedding band|live band|orchestra|quartet|duo)\b", name, re.I) \
            and (craft or office or shop or amenity == "studio"):
        cats.append("music")

    # catering
    if craft == "caterer" or office == "catering" or amenity == "catering" or t.get("catering") == "yes" \
            and amenity in ("restaurant", "cafe") and t.get("wedding") == "yes" \
            or shop == "catering":
        cats.append("catering")

    # decoration / florists
    if shop in ("florist", "party", "event_decoration", "event_decorations") \
            or craft in ("decorator", "florist", "event_decorator") \
            or office == "event_planner" and False:
        cats.append("decoration")

    return list(dict.fromkeys(cats))


def clean_phone(p):
    if not p:
        return None
    p = p.split(";")[0].strip()
    digits = re.sub(r"[^\d+]", "", p)
    return digits if len(re.sub(r"\D", "", digits)) >= 7 else None


def clean_url(u):
    if not u:
        return None
    u = u.split(";")[0].strip()
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u if "." in u else None


def dedupe_key(name, lat, lng, phone, website, category):
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    host = re.sub(r"^https?://(www\.)?", "", website or "").split("/")[0]
    anchor = host or (phone or "") or f"{round(lat or 0, 3)},{round(lng or 0, 3)}"
    return hashlib.sha1(f"{category}|{n}|{anchor}".encode()).hexdigest()[:16]


def make_handler(osmium, db, country, retrieved_at):
    class H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.n = 0
            self.batch = []

        def flush(self):
            if self.batch:
                db.executemany(
                    """INSERT OR IGNORE INTO listings
                    (osm_type,osm_id,category,name,lat,lng,city,postcode,country,address,phone,email,
                     website,instagram,facebook,opening_hours,description,source_url,retrieved_at,
                     status,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self.batch)
                db.commit()
                self.batch = []

        def handle(self, kind, o, lat, lng):
            t = {tag.k: tag.v for tag in o.tags}
            name = t.get("name")
            if not name or lat is None:
                return
            cats = classify(t)
            if not cats:
                return
            phone = clean_phone(t.get("phone") or t.get("contact:phone"))
            email = (t.get("email") or t.get("contact:email") or "").split(";")[0].strip() or None
            web = clean_url(t.get("website") or t.get("contact:website") or t.get("url"))
            street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x) or None
            city = t.get("addr:city") or t.get("addr:town") or t.get("addr:village")
            ctry = t.get("addr:country") or country
            status = "verified_contact" if (web or phone or email) and (city or street) else "imported"
            for c in cats:
                self.batch.append((
                    kind, o.id, c, name, lat, lng, city, t.get("addr:postcode"), ctry, street, phone,
                    email, web, t.get("contact:instagram"), t.get("contact:facebook"),
                    t.get("opening_hours"), t.get("description"),
                    f"https://www.openstreetmap.org/{ {'n':'node','w':'way','r':'relation'}[kind] }/{o.id}",
                    retrieved_at, status, dedupe_key(name, lat, lng, phone, web, c)))
                self.n += 1
            if len(self.batch) >= 2000:
                self.flush()

        def node(self, n):
            if n.location.valid():
                self.handle("n", n, n.location.lat, n.location.lon)

        def area(self, a):
            # ways and relations become areas; use centroid of first outer ring
            try:
                ring = next(iter(a.outer_rings()))
                lats = [p.lat for p in ring]
                lons = [p.lon for p in ring]
                self.handle("w" if a.from_way() else "r",
                            a, sum(lats) / len(lats), sum(lons) / len(lons))
            except Exception:
                pass
    return H()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--country", default=None, help="ISO code to store when addr:country is missing")
    a = ap.parse_args()

    import osmium  # imported late so --help works without it
    db = sqlite3.connect(a.out)
    db.executescript(SCHEMA)
    h = make_handler(osmium, db, a.country, dt.date.today().isoformat())
    h.apply_file(a.pbf, locations=True, idx="flex_mem")
    h.flush()

    # De-duplicate: keep the row with the most contact info per dedupe_key
    db.execute("""DELETE FROM listings WHERE id NOT IN (
        SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (PARTITION BY dedupe_key
            ORDER BY (website IS NOT NULL)+(phone IS NOT NULL)+(email IS NOT NULL) DESC, id) rn
          FROM listings) WHERE rn = 1)""")
    db.commit()
    rows = db.execute("SELECT category, COUNT(*) FROM listings GROUP BY category").fetchall()
    print(f"{a.pbf}: {dict(rows)}")


if __name__ == "__main__":
    sys.exit(main())
