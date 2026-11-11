#!/usr/bin/env python3
"""Continuous refresh engine for the wedding directory.

It never "trusts" a record forever. Every listing gets re-checked on a schedule, evidence is
collected from the supplier's OWN website, a confidence score is computed, and anything that
cannot be confirmed fades out of public results automatically.

Commands
  python refresh_engine.py refresh  DB.sqlite [--batch 3000] [--workers 40]
  python refresh_engine.py merge-new LIVE.sqlite NEW.sqlite     # add newly built listings, keep history
  python refresh_engine.py report   DB.sqlite

Rules the engine enforces
  * Only evidence found on the supplier's site (or OSM) is stored. Nothing is guessed.
  * A price is stored only if the page states it ("from EUR 2,500", JSON-LD priceRange), together with
    the page URL and the check date. Prices expire after PRICE_TTL_DAYS.
  * 3 consecutive failed checks -> status 'dead' (hidden). A later success revives it.
  * Public results = view public_listings (confidence >= MIN_CONFIDENCE, checked within FRESH_DAYS).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import urllib.parse

from styles import tag_styles

UA = "WeddingDirectoryVerifier/1.0 (supplier listing check)"
MAX_BYTES = 600_000
FAILS_TO_DEAD = 3
MIN_CONFIDENCE = 0.60
FRESH_DAYS = 90
PRICE_TTL_DAYS = 60
HALF_LIFE_DAYS = 120

NEW_COLS = {
    "last_checked_at": "TEXT", "fail_count": "INTEGER DEFAULT 0", "content_hash": "TEXT",
    "confidence": "REAL DEFAULT 0", "last_changed_at": "TEXT",
    "price_min": "REAL", "price_max": "REAL", "currency": "TEXT",
    "price_source_url": "TEXT", "price_checked_at": "TEXT",
    "evidence": "TEXT",   # JSON: which checks passed on the last run
    "price_unit": "TEXT", "rating": "REAL", "rating_count": "INTEGER", "rating_source": "TEXT",
    "description_source_url": "TEXT", "photo_url": "TEXT", "style_tags": "TEXT", "capacity_max": "INTEGER",
    "google_place_id": "TEXT", "ical_url": "TEXT", "vendor_verified": "INTEGER DEFAULT 0",
    "tiktok": "TEXT", "pinterest": "TEXT", "youtube": "TEXT", "linkedin": "TEXT", "whatsapp": "TEXT",
    "collected_at": "TEXT", "collect_next_at": "TEXT", "collect_fail": "INTEGER DEFAULT 0",
}

PARKED = re.compile(
    r"domain (is )?for sale|buy this domain|this domain (may be|is) (for sale|parked)|"
    r"parked (free|domain)|sedo\.com|hugedomains|dan\.com/buy|account suspended|"
    r"future home of|coming soon.{0,40}(godaddy|namecheap)", re.I)

FROM_PRICE = re.compile(
    r"(?:from|starting at|starts at|packages? from|desde|a partir de|ab|dès|a partire da|vanaf)\s*"
    r"(€|£|\$|eur|gbp|usd|cad|chf)\s?([0-9]{1,3}(?:[.,  ]?[0-9]{3})*)|"
    r"(?:from|starting at|starts at|packages? from|desde|a partir de|ab|dès|a partire da|vanaf)\s*"
    r"([0-9]{1,3}(?:[.,  ]?[0-9]{3})*)\s?(€|£|\$|eur|gbp|usd|cad|chf)", re.I)
CUR = {"€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "$": "USD", "usd": "USD",
       "cad": "CAD", "chf": "CHF"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.replace(microsecond=0).isoformat()


def migrate(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(listings)")}
    for c, t in NEW_COLS.items():
        if c not in have:
            db.execute(f"ALTER TABLE listings ADD COLUMN {c} {t}")
    db.execute("""CREATE TABLE IF NOT EXISTS changes(
        id INTEGER PRIMARY KEY, listing_id INTEGER, field TEXT, old TEXT, new TEXT, at TEXT)""")
    db.execute("DROP VIEW IF EXISTS public_listings")
    db.execute(f"""CREATE VIEW public_listings AS
        SELECT id, category, name, lat, lng, city, country, address, phone, email, website,
               instagram, facebook, opening_hours, description, description_source_url, source_url, last_checked_at, confidence, status,
               rating, rating_count, rating_source, photo_url, style_tags, capacity_max,
               google_place_id, ical_url, vendor_verified,
               tiktok, pinterest, youtube, linkedin, whatsapp, collected_at,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN price_min END AS price_min,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN price_max END AS price_max,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN currency END AS currency,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN price_unit END AS price_unit,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN price_source_url END AS price_source_url,
               CASE WHEN price_checked_at >= date('now','-{PRICE_TTL_DAYS} day') THEN price_checked_at END AS price_checked_at
        FROM listings
        WHERE status != 'dead' AND confidence >= {MIN_CONFIDENCE}
          AND last_checked_at >= date('now','-{FRESH_DAYS} day')""")
    db.commit()


# ---------- evidence collection ----------
def fetch(url):
    """Return (ok, final_url, text, error). ok=False means a real failure (not a bot block)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read(MAX_BYTES)
            enc = r.headers.get_content_charset() or "utf-8"
            return True, r.geturl(), raw.decode(enc, "replace"), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 503):      # alive but blocking bots: neutral, not a failure
            return True, url, "", f"blocked:{e.code}"
        return False, url, "", f"http:{e.code}"
    except Exception as e:
        return False, url, "", type(e).__name__


def digits(s):
    return re.sub(r"\D", "", s or "")


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def jsonld(text):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                out.append(d)
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stack.extend(d)
    return out


def parse_amount(s):
    s = s.replace(" ", "").replace(" ", "")
    if re.search(r"[.,]\d{3}$", s) or re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def extract(row, final_url, text):
    """Collect hard evidence from the page. Returns dict of signals (all booleans/values found)."""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    plain = re.sub(r"<[^>]+>", " ", body)
    low = norm(plain)
    ev = {"parked": bool(PARKED.search(text[:20000]))}
    name_tokens = [t for t in norm(row["name"]).split() if len(t) > 2]
    ev["name_match"] = bool(name_tokens) and sum(t in low for t in name_tokens) / len(name_tokens) >= 0.6
    d = digits(plain) + " " + " ".join(digits(x) for x in re.findall(r'href="tel:([^"]+)"', text))
    ph = digits(row["phone"])[-8:] if row["phone"] else ""
    ev["phone_match"] = bool(ph) and ph in d.replace(" ", "")
    em = (row["email"] or "").lower()
    ev["email_match"] = bool(em) and em in text.lower()
    lds = jsonld(text)
    ev["jsonld"] = bool(lds)
    price = None
    m = None
    for o in lds:                                   # explicit structured price range
        pr = o.get("priceRange")
        if isinstance(pr, str):
            m = re.search(r"(€|£|\$|[A-Z]{3})\s?([0-9][0-9.,]*)\s*[-–]\s*(?:€|£|\$|[A-Z]{3})?\s?([0-9][0-9.,]*)", pr)
            if m:
                lo, hi = parse_amount(m.group(2)), parse_amount(m.group(3))
                if lo and hi:
                    price = (lo, hi, CUR.get(m.group(1).lower(), m.group(1)))
                    break
    if not price:
        m = FROM_PRICE.search(plain)
        if m:
            cur = (m.group(1) or m.group(4) or "").lower()
            amt = parse_amount(m.group(2) or m.group(3))
            if amt and 50 <= amt <= 500_000 and cur in CUR:
                price = (amt, None, CUR[cur])
    unit = None
    if price and price[1] is None and m:
        ctx_txt = plain[m.start():m.end() + 45]
        unit = "per_person" if re.search(
            r"per (person|guest|head|adult)|\bpp\b|/ ?(person|pax|guest|head)|por pessoa|par personne|"
            r"pro person|a persona|per persoon|por convidado", ctx_txt, re.I) else "total"
    ev["price"] = price
    ev["price_unit"] = unit
    rating = None
    for o in lds:
        ar = o.get("aggregateRating")
        if isinstance(ar, dict):
            try:
                rv = float(str(ar.get("ratingValue")).replace(",", "."))
                rc = int(float(ar.get("reviewCount") or ar.get("ratingCount") or 0))
                best = float(ar.get("bestRating") or 5)
                if best > 0 and rc >= 3 and 0 < rv <= best:
                    rating = (round(rv / best * 5, 2), rc)
                    break
            except (TypeError, ValueError):
                pass
    ev["rating"] = rating
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', text, re.I)
    ev["photo"] = urllib.parse.urljoin(final_url, og.group(1)) if og else None
    ev["styles"] = tag_styles(row["name"] + " " + plain[:30000])
    ev["hash"] = hashlib.sha1(low[:50000].encode()).hexdigest()[:16]
    return ev


# ---------- scoring ----------
def score(row, ev, ok, fails):
    """Confidence 0..1 from evidence. Missing evidence lowers it; nothing is assumed."""
    s = 0.15                                        # exists in a public source (OSM)
    if row["city"] or row["address"]:
        s += 0.10
    if ok and not (ev or {}).get("parked"):
        s += 0.20                                   # site answers and is not a parked domain
    if ev:
        s += 0.25 * ev["name_match"] + 0.15 * (ev["phone_match"] or ev["email_match"]) + 0.05 * ev["jsonld"]
        if ev["parked"]:
            s -= 0.30
    if not row["website"]:
        s = min(s, 0.45)                            # no website = cannot reach "verified"
    s -= 0.15 * min(fails, FAILS_TO_DEAD)
    return round(max(0.0, min(1.0, s)), 3)


def log(db, lid, field, old, new):
    if (old or None) != (new or None):
        db.execute("INSERT INTO changes(listing_id,field,old,new,at) VALUES (?,?,?,?,?)",
                   (lid, field, None if old is None else str(old), None if new is None else str(new), iso(now())))


def check_one(row):
    if not row["website"]:
        return row, None, None, False
    ok, final, text, err = fetch(row["website"])
    ev = extract(row, final, text) if (ok and text) else None
    return row, ev, final, ok


def refresh(path, batch, workers):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    migrate(db)
    # Priority: never checked -> failing (recheck fast) -> oldest check. Dead ones rechecked last.
    rows = db.execute("""SELECT * FROM listings
        ORDER BY (status='dead') ASC,
                 (last_checked_at IS NOT NULL) ASC,
                 (COALESCE(fail_count,0)>0) DESC,
                 last_checked_at ASC
        LIMIT ?""", (batch,)).fetchall()
    t = iso(now())
    stats = dict(checked=0, alive=0, failed=0, revived=0, died=0, prices=0, changed=0)
    with cf.ThreadPoolExecutor(workers) as ex:
        for row, ev, final, ok in ex.map(check_one, rows):
            lid, fails, old_status = row["id"], row["fail_count"] or 0, row["status"]
            if row["website"]:
                fails = 0 if ok else fails + 1
            status = old_status
            if row["website"] and not ok and fails >= FAILS_TO_DEAD:
                status = "dead"
            elif ok and row["website"] and not (ev and ev["parked"]):
                status = "verified_web"
            elif ev and ev["parked"]:
                status = "dead"
            elif not row["website"] and old_status != "dead":
                status = old_status
            conf = score(row, ev, ok, fails)
            if status == "verified_web" and conf < MIN_CONFIDENCE:
                status = "verified_contact"         # reachable but identity not confirmed on the page
            h = ev["hash"] if ev else row["content_hash"]
            if h and row["content_hash"] and h != row["content_hash"]:
                db.execute("UPDATE listings SET last_changed_at=? WHERE id=?", (t, lid))
                stats["changed"] += 1
            log(db, lid, "status", old_status, status)
            if ev and ev["price"]:
                lo, hi, cur = ev["price"]
                log(db, lid, "price_min", row["price_min"], lo)
                db.execute("""UPDATE listings SET price_min=?, price_max=?, currency=?, price_unit=?,
                              price_source_url=?, price_checked_at=? WHERE id=?""",
                           (lo, hi, cur, ev["price_unit"], final, t, lid))
                stats["prices"] += 1
            if ev:
                rt = ev["rating"]
                db.execute("""UPDATE listings SET
                              rating=COALESCE(?,rating), rating_count=COALESCE(?,rating_count),
                              rating_source=CASE WHEN ? IS NOT NULL THEN 'vendor-site' ELSE rating_source END,
                              photo_url=COALESCE(?,photo_url),
                              style_tags=COALESCE(?,style_tags) WHERE id=?""",
                           (rt[0] if rt else None, rt[1] if rt else None, rt[0] if rt else None,
                            ev["photo"], ",".join(ev["styles"]) or None, lid))
            if old_status == "dead" and status != "dead":
                stats["revived"] += 1
            if status == "dead" and old_status != "dead":
                stats["died"] += 1
            db.execute("""UPDATE listings SET status=?, fail_count=?, confidence=?, content_hash=?,
                          last_checked_at=?, verified_at=CASE WHEN ?='verified_web' THEN ? ELSE verified_at END,
                          evidence=? WHERE id=?""",
                       (status, fails, conf, h, t, status, t[:10],
                        json.dumps({k: v for k, v in (ev or {}).items() if k != "hash"}), lid))
            stats["checked"] += 1
            stats["alive" if ok else "failed"] += 1
    db.commit()
    print(json.dumps(stats))


def merge_new(live, new):
    """Add listings from a freshly built DB into the live DB. Existing rows keep their history."""
    db = sqlite3.connect(live)
    db.executescript(open(__file__.replace("refresh_engine.py", "import_osm.py")).read()
                     .split('SCHEMA = """')[1].split('"""')[0])
    migrate(db)
    db.execute("ATTACH ? AS n", (new,))
    cols = [r[1] for r in db.execute("PRAGMA table_info(n.listings)") if r[1] not in ("id", "status")]
    c = ",".join(cols)
    before = db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    db.execute(f"""INSERT INTO listings ({c}, status) SELECT {c}, 'imported' FROM n.listings
                   WHERE dedupe_key NOT IN (SELECT dedupe_key FROM main.listings)""")
    db.commit()
    print(f"added {db.execute('SELECT COUNT(*) FROM listings').fetchone()[0] - before} new listings")


def report(path):
    db = sqlite3.connect(path)
    print("PUBLIC listings (visible in app):")
    for r in db.execute("SELECT category, COUNT(*) FROM public_listings GROUP BY 1 ORDER BY 1"):
        print(f"  {r[0]:<14}{r[1]}")
    print("ALL listings by status:")
    for r in db.execute("SELECT status, COUNT(*) FROM listings GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r[0]:<18}{r[1]}")
    print("with a current price:",
          db.execute("SELECT COUNT(*) FROM public_listings WHERE price_min IS NOT NULL").fetchone()[0])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("refresh"); a.add_argument("db"); a.add_argument("--batch", type=int, default=3000)
    a.add_argument("--workers", type=int, default=40)
    b = sub.add_parser("merge-new"); b.add_argument("live"); b.add_argument("new")
    c = sub.add_parser("report"); c.add_argument("db")
    x = ap.parse_args()
    if x.cmd == "refresh":
        refresh(x.db, x.batch, x.workers)
    elif x.cmd == "merge-new":
        merge_new(x.live, x.new)
    else:
        report(x.db)
