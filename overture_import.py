#!/usr/bin/env python3
"""Import wedding-related businesses from Overture Maps 'places' (open, global, includes websites/phones).

This is the biggest free source for growing the directory beyond OpenStreetMap.
Needs:  pip install duckdb
Find the current release id at https://docs.overturemaps.org/getting-data/  (looks like 2025-10-22.0)

Usage:
  python overture_import.py OUT.sqlite --release RELEASE --bbox xmin,ymin,xmax,ymax --country CA
  python overture_import.py OUT.sqlite --parquet local_places.parquet           # for local files / tests

Category matching is by keyword on taxonomy primary, hierarchy, and alternates, so it adapts to taxonomy changes.
Check the printed per-keyword counts on the first run and adjust KEYWORDS if one matches junk.
"""
import argparse
import datetime as dt
import hashlib
import re
import sqlite3
import sys

from import_osm import SCHEMA, dedupe_key, clean_phone, clean_url

KEYWORDS = {
    "venue": ["wedding_venue", "event_venue", "banquet", "wedding_hall", "reception_hall", "castle", "winery",
              "vineyard", "farm", "ballroom", "chateau", "manor"],
    "photographer": ["photographer", "photography", "photo_studio", "videographer", "videography"],
    "music": ["dj", "band", "musician", "orchestra", "wedding_music"],
    "catering": ["caterer", "catering", "wedding_catering", "banquet_catering"],
    "decoration": ["florist", "flowers", "event_decoration", "party_supply", "decorator", "party_equipment_rental"],
}
RX = {c: re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, kws)) + r")(?![a-z0-9])", re.I)
      for c, kws in KEYWORDS.items()}
# venue keywords like 'farm'/'castle' are only accepted together with an event/wedding signal
VENUE_NEEDS_SIGNAL = re.compile(r"wedding|event|banquet|reception|ballroom", re.I)


def classify(primary, alternate, name):
    text = " ".join([primary or "", *(alternate or [])])
    cats = []
    for c, rx in RX.items():
        if rx.search(text):
            if c == "venue" and not re.search(r"wedding_venue|event_venue|banquet|wedding_hall|reception_hall|ballroom",
                                              text, re.I) and not VENUE_NEEDS_SIGNAL.search(name or ""):
                continue
            cats.append(c)
    return cats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--release")
    ap.add_argument("--bbox")
    ap.add_argument("--country", default=None)
    ap.add_argument("--parquet")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    a = ap.parse_args()

    import duckdb
    con = duckdb.connect()
    if a.parquet:
        src = f"read_parquet('{a.parquet}')"
        where = "1=1"
    else:
        if not (a.release and a.bbox):
            sys.exit("need --release and --bbox (or --parquet)")
        con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
        src = f"read_parquet('s3://overturemaps-us-west-2/release/{a.release}/theme=places/type=place/*', hive_partitioning=1)"
        x0, y0, x1, y1 = [float(v) for v in a.bbox.split(",")]
        where = f"bbox.xmin BETWEEN {x0} AND {x1} AND bbox.ymin BETWEEN {y0} AND {y1}"
    columns = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    if "taxonomy" in columns:
        category = "taxonomy.primary"
        alternates = "list_concat(taxonomy.hierarchy, taxonomy.alternates)"
    else:
        category = "categories.primary"
        alternates = "categories.alternate"
    if "operating_status" in columns:
        where += " AND coalesce(operating_status, '') NOT IN ('permanently_closed', 'temporarily_closed')"
    pat = "(^|[^a-z0-9])(" + "|".join(k for kws in KEYWORDS.values() for k in kws) + ")([^a-z0-9]|$)"
    q = f"""
      SELECT id, names.primary AS name, {category} AS cat, {alternates} AS alt,
             (bbox.ymin+bbox.ymax)/2 AS lat, (bbox.xmin+bbox.xmax)/2 AS lng,
             websites, phones, emails, addresses, confidence
      FROM {src}
      WHERE {where} AND names.primary IS NOT NULL AND confidence >= {a.min_confidence}
        AND regexp_matches(coalesce({category},'') || ' ' || array_to_string(coalesce({alternates}, []), ' '),
                           '{pat}', 'i')"""
    db = sqlite3.connect(a.out)
    db.executescript(SCHEMA)
    today = dt.date.today().isoformat()
    batch, counts = [], {}
    cur = con.execute(q)
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for (oid, name, cat, alt, lat, lng, webs, phones, emails, addrs, conf) in rows:
            cats = classify(cat, alt, name)
            if not cats:
                continue
            web = clean_url(webs[0]) if webs else None
            phone = clean_phone(phones[0]) if phones else None
            email = emails[0] if emails else None
            ad = (addrs[0] if addrs else None) or {}
            city, country = ad.get("locality"), ad.get("country") or a.country
            street, post = ad.get("freeform"), ad.get("postcode")
            status = "verified_contact" if (web or phone or email) and (city or street) else "imported"
            n = int(hashlib.sha1(oid.encode()).hexdigest()[:15], 16)
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
                batch.append(("ovt", n, c, name, lat, lng, city, post, country, street, phone, email, web,
                              None, None, None, None, f"https://overturemaps.org/ (place id {oid})", today,
                              status, dedupe_key(name, lat, lng, phone, web, c)))
        db.executemany("""INSERT OR IGNORE INTO listings
            (osm_type,osm_id,category,name,lat,lng,city,postcode,country,address,phone,email,website,
             instagram,facebook,opening_hours,description,source_url,retrieved_at,status,dedupe_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        db.commit()
        batch = []
    db.execute("UPDATE listings SET source='Overture Maps', license='CDLA-Permissive-2.0 (verify per release)' "
               "WHERE osm_type='ovt'")
    db.commit()
    print("imported by category:", counts)


if __name__ == "__main__":
    main()
