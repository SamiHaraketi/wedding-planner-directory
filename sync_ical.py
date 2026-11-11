#!/usr/bin/env python3
"""Pull busy dates from vendors' public iCal (.ics) calendars into the availability table.

Vendors paste their Google/Apple/Outlook calendar's public iCal link (POST /vendor/{id}/ical).
Every event day counts as 'busy'. Previous iCal rows are replaced each run, so cancelled bookings free up.

Usage: python sync_ical.py DB.sqlite
"""
import datetime as dt
import re
import sqlite3
import sys
import urllib.request

import ranker

UA = "WeddingDirectoryCalendarSync/1.0"


def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_ics(text, horizon_days=800):
    """Return the set of busy ISO dates. DTEND for all-day events is exclusive (per RFC 5545)."""
    busy = set()
    today = dt.date.today()
    last = today + dt.timedelta(days=horizon_days)
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfold(text), re.S):
        if re.search(r"^STATUS:CANCELLED", ev, re.M | re.I) or re.search(r"^TRANSP:TRANSPARENT", ev, re.M | re.I):
            continue
        s = re.search(r"^DTSTART[^:]*:(\d{8})(T\d{6}Z?)?", ev, re.M)
        e = re.search(r"^DTEND[^:]*:(\d{8})(T\d{6}Z?)?", ev, re.M)
        if not s:
            continue
        d0 = dt.datetime.strptime(s.group(1), "%Y%m%d").date()
        d1 = dt.datetime.strptime(e.group(1), "%Y%m%d").date() if e else d0
        all_day = not s.group(2)
        end = d1 if all_day and d1 > d0 else d1 + dt.timedelta(days=1 if not all_day or d1 == d0 else 0)
        d = d0
        while d < max(end, d0 + dt.timedelta(days=1)) and d <= last:
            if d >= today:
                busy.add(d.isoformat())
            d += dt.timedelta(days=1)
    return busy


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def main(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    ranker.init(db)
    ok = bad = 0
    for r in db.execute("SELECT id, ical_url FROM listings WHERE ical_url IS NOT NULL").fetchall():
        try:
            days = parse_ics(fetch(r["ical_url"]))
        except Exception as e:
            bad += 1
            print("failed", r["id"], type(e).__name__)
            continue
        db.execute("DELETE FROM availability WHERE listing_id=? AND source='ical'", (r["id"],))
        t = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        db.executemany("INSERT OR REPLACE INTO availability VALUES (?,?,?,?,?)",
                       [(r["id"], d, "busy", "ical", t) for d in sorted(days)])
        ok += 1
    db.commit()
    print(f"calendars synced={ok} failed={bad}")


if __name__ == "__main__":
    main(sys.argv[1])
