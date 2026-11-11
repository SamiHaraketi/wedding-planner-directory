#!/usr/bin/env python3
"""Google Places enrichment (needs GOOGLE_API_KEY, billing enabled, Places API (New)).

IMPORTANT, Google's terms: you may store the place_id indefinitely, but not cache ratings, reviews, photos,
phone or website beyond short-lived display use. So this module does exactly that:
  * `link`  : finds the Google place_id for a listing and stores ONLY the place_id.
  * `live_details` : fetches rating / review count / opening hours / website / Maps link / photos at
                     display time. api.py serves it via /listing/{id}/live with a 10 min in-memory cache.
  Show Google's attribution next to anything you display from it.

Usage: python enrich_google.py link DB.sqlite [--limit 500]
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request

SEARCH = "https://places.googleapis.com/v1/places:searchText"
DETAIL = "https://places.googleapis.com/v1/places/{}"


def _key():
    k = os.environ.get("GOOGLE_API_KEY")
    if not k:
        raise RuntimeError("Set GOOGLE_API_KEY (Places API (New) enabled)")
    return k


def _post(url, body, mask):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": _key(), "X-Goog-FieldMask": mask})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _get(url, mask):
    req = urllib.request.Request(url, headers={"X-Goog-Api-Key": _key(), "X-Goog-FieldMask": mask})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_place_id(name, lat, lng, radius_m=400):
    data = _post(SEARCH, {
        "textQuery": name, "maxResultCount": 3,
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}},
    }, "places.id,places.displayName")
    for p in data.get("places", []):
        a, b = _norm(name), _norm(p.get("displayName", {}).get("text"))
        if a and b and (a in b or b in a):
            return p["id"]
    return None


def live_details(place_id):
    d = _get(DETAIL.format(place_id),
             "rating,userRatingCount,websiteUri,nationalPhoneNumber,googleMapsUri,"
             "regularOpeningHours.weekdayDescriptions,priceLevel,photos.name,reviews.rating,"
             "reviews.text,reviews.relativePublishTimeDescription,reviews.authorAttribution")
    d["attribution"] = "Data provided by Google. Show the Google Maps link and author attributions."
    return d


def link(path, limit=500):
    import ranker
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    ranker.init(db)
    rows = db.execute("""SELECT id, name, lat, lng FROM public_listings
                         WHERE google_place_id IS NULL ORDER BY confidence DESC LIMIT ?""", (limit,)).fetchall()
    n = 0
    for r in rows:
        try:
            pid = find_place_id(r["name"], r["lat"], r["lng"])
        except Exception as e:
            print("error", r["id"], type(e).__name__)
            continue
        if pid:
            db.execute("UPDATE listings SET google_place_id=? WHERE id=?", (pid, r["id"]))
            n += 1
    db.commit()
    print(f"linked {n}/{len(rows)}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "link":
        link(sys.argv[2], int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 500)
    else:
        print(__doc__)
