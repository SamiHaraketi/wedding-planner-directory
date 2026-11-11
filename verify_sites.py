#!/usr/bin/env python3
"""Check that each listing's website is alive. Promotes status to verified_web, or marks dead.

Usage: python verify_sites.py DB.sqlite [--limit 20000] [--workers 40]
Polite: one request per listing, short timeout, identifies itself generically.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import sqlite3
import urllib.request

UA = "WeddingDirectoryVerifier/1.0 (listing liveness check)"


def check(url):
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status < 400
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):   # site exists but blocks bots
                return True
            if method == "HEAD" and e.code in (405, 501):
                continue
            return False
        except Exception:
            if method == "HEAD":
                continue
            return False
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--workers", type=int, default=40)
    a = ap.parse_args()
    db = sqlite3.connect(a.db)
    rows = db.execute("""SELECT id, website FROM listings
                         WHERE website IS NOT NULL AND status IN ('imported','verified_contact')
                         LIMIT ?""", (a.limit,)).fetchall()
    today = dt.date.today().isoformat()
    ok = bad = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for (id_, _), alive in zip(rows, ex.map(lambda r: check(r[1]), rows)):
            db.execute("UPDATE listings SET status=?, verified_at=? WHERE id=?",
                       ("verified_web" if alive else "dead", today, id_))
            ok += alive
            bad += (not alive)
    db.commit()
    print(f"alive={ok} dead={bad}")


if __name__ == "__main__":
    main()
