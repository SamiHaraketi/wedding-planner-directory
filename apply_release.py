#!/usr/bin/env python3
"""Merge a freshly refreshed listings database (the GitHub Release live.sqlite) into the API's own database
WITHOUT losing app-side data: clicks, reviews, availability, learned weights and vendor-submitted fields.

  gh release download database -p live.sqlite --clobber -O release.sqlite
  python apply_release.py APP.sqlite release.sqlite

Listing ids are stable across releases (new listings are appended), so app-side tables keep pointing at the
right vendor. Vendor-verified rows keep the price/calendar the vendor submitted themselves.
"""
import sqlite3
import sys

import ranker

KEEP = ["price_min", "price_max", "currency", "price_unit", "price_source_url", "price_checked_at",
        "ical_url", "vendor_verified"]


def main(app, rel):
    db = sqlite3.connect(app)
    ranker.init(db)
    db.execute("ATTACH ? AS rel", (rel,))
    # rel may predate newer columns: add them to rel's view of the world by selecting only common columns
    app_cols = [r[1] for r in db.execute("PRAGMA main.table_info(listings)")]
    rel_cols = {r[1] for r in db.execute("PRAGMA rel.table_info(listings)")}
    cols = [c for c in app_cols if c in rel_cols]
    keep_rows = db.execute(f"SELECT id, {','.join(KEEP)}, google_place_id FROM main.listings "
                           f"WHERE vendor_verified=1 OR google_place_id IS NOT NULL").fetchall()
    c = ",".join(cols)
    db.execute(f"INSERT OR REPLACE INTO main.listings ({c}) SELECT {c} FROM rel.listings")
    for r in keep_rows:
        if r[KEEP.index("vendor_verified") + 1]:
            sets = ",".join(f"{k}=?" for k in KEEP)
            db.execute(f"UPDATE main.listings SET {sets} WHERE id=?", (*r[1:1 + len(KEEP)], r[0]))
        if r[-1]:
            db.execute("UPDATE main.listings SET google_place_id=? WHERE id=?", (r[-1], r[0]))
    rel_tables = {r[0] for r in db.execute("SELECT name FROM rel.sqlite_master WHERE type='table'")}
    if "photos" in rel_tables:
        db.execute("DELETE FROM main.photos")
        db.execute("INSERT OR IGNORE INTO main.photos(listing_id,url,alt,source_url,fetched_at) "
                   "SELECT listing_id,url,alt,source_url,fetched_at FROM rel.photos")
    if "field_sources" in rel_tables:
        db.execute("INSERT OR REPLACE INTO main.field_sources SELECT * FROM rel.field_sources")
    db.commit()
    print("listings in app database:", db.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
          "| public:", db.execute("SELECT COUNT(*) FROM public_listings").fetchone()[0])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
