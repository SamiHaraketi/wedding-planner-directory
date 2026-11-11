#!/usr/bin/env python3
"""Merge regional SQLite files into one, drop dead listings, export CSV + count report.

Usage: python merge_export.py OUT.sqlite part1.sqlite part2.sqlite ...
"""
import csv
import sqlite3
import sys

from import_osm import SCHEMA

out, parts = sys.argv[1], sys.argv[2:]
db = sqlite3.connect(out)
db.executescript(SCHEMA)
cols = [r[1] for r in db.execute("PRAGMA table_info(listings)") if r[1] != "id"]
c = ",".join(cols)
for p in parts:
    db.execute("ATTACH ? AS p", (p,))
    db.execute(f"INSERT OR IGNORE INTO listings ({c}) SELECT {c} FROM p.listings")
    db.commit()
    db.execute("DETACH p")

db.execute("DELETE FROM listings WHERE status='dead'")
# cross-region dedupe
db.execute("""DELETE FROM listings WHERE id NOT IN (
    SELECT MIN(id) FROM listings GROUP BY dedupe_key)""")
db.commit()

with open(out.replace(".sqlite", ".csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(cols)
    w.writerows(db.execute(f"SELECT {c} FROM listings"))

print("category, status, count")
for r in db.execute("SELECT category, status, COUNT(*) FROM listings GROUP BY 1,2 ORDER BY 1,2"):
    print(*r, sep=", ")
print("TOTAL", db.execute("SELECT COUNT(*) FROM listings").fetchone()[0])
