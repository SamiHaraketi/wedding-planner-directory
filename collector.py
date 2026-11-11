#!/usr/bin/env python3
"""Continuous web collector (open source only: Python, BeautifulSoup, extruct, phonenumbers, SearXNG).

For every listing it works out, from the vendor's OWN public website:
  website (found via a self-hosted SearXNG metasearch if missing), phone, email, social profiles
  (Instagram, Facebook, TikTok, Pinterest, YouTube, LinkedIn, WhatsApp), photos, published rating
  (schema.org aggregateRating), "from" price, capacity, address, style tags.

Rules it never breaks
  * robots.txt is obeyed; one request per domain every COLLECTOR_DELAY seconds; identifies itself.
  * Private/internal addresses are never fetched (SSRF guard).
  * IDENTITY GATE: a page's data is attached to a listing only if the page really is that business
    (name on the page + phone, city or coordinates matching). A wrong website is rejected, not stored.
  * Every stored field records where it came from (field_sources: url, method, date).
  * Existing data is never silently overwritten; empty fields are filled.
  * Ratings come ONLY from what the vendor publishes (schema.org). Google/Yelp/Tripadvisor are not scraped (their
    terms forbid it); use enrich_google.py for live Google display.

Usage
  python collector.py DB.sqlite --batch 300 --workers 8            # one pass
  python collector.py DB.sqlite --forever --sleep 120               # never stops
Env: SEARXNG_URL (e.g. http://localhost:8080), COLLECTOR_UA, COLLECTOR_DELAY (default 1.5)
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import extruct
import phonenumbers
from bs4 import BeautifulSoup

import ranker
import refresh_engine as RE

UA = os.environ.get("COLLECTOR_UA", "WeddingDirectoryBot/1.0 (+https://example.com/bot; contact: bot@example.com)")
UA_TOKEN = "WeddingDirectoryBot"
DELAY = float(os.environ.get("COLLECTOR_DELAY", "1.5"))
ALLOW_PRIVATE = os.environ.get("COLLECTOR_ALLOW_PRIVATE") == "1"      # tests only
MAX_PAGES = 6
MAX_BYTES = 900_000
MAX_PHOTOS = 8
RECHECK_DAYS = 30

DIRECTORIES = re.compile(
    r"(facebook|instagram|linkedin|tiktok|pinterest|youtube|twitter|x)\.com|yelp\.|tripadvisor\.|google\.|"
    r"booking\.com|airbnb\.|wikipedia\.|zola\.com|theknot\.com|weddingwire\.|bodas\.net|casamentos\.pt|hitched\.co|"
    r"pagesjaunes\.|paginasamarillas\.|yell\.com|foursquare\.|mapcarta\.|wedding-spot\.|mariages\.net|matrimonio\.com|"
    r"hochzeit\.com|trustpilot\.|bing\.com|yahoo\.|duckduckgo\.|eventbrite\.|expedia\.|kayak\.|mapquest\.|"
    r"bestweddingvenues|weddingvenuedirectory|yelp\b", re.I)

LINK_KEYWORDS = [  # page types worth visiting, best first
    ("contact", ["contact", "contacto", "contacte", "contato", "kontakt", "contatti", "impressum", "fale-", "reach"]),
    ("gallery", ["gallery", "galeria", "galerie", "portfolio", "photos", "fotos", "weddings", "real-wedding"]),
    ("price", ["price", "pricing", "prices", "packages", "pacotes", "preços", "precios", "tarifs", "prezzi",
               "preise", "investment", "rates"]),
    ("about", ["about", "sobre", "quem-somos", "a-propos", "nosotros", "ueber", "chi-siamo", "story", "venue"]),
]

SOCIAL = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|reel|explore|accounts|stories|tv/|share)"
                            r"([A-Za-z0-9_.]{2,30})/?(?:[?#]|$)", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.|pt-br\.)?facebook\.com/(?!sharer|share|dialog|tr\?|plugins|"
                           r"login|groups|events|watch|hashtag|policies|help)((?:pages/[^/?#]+/\d+)|(?:profile\.php\?id=\d+)|"
                           r"(?:[A-Za-z0-9.\-]{3,}))/?(?:[?#]|$)", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/(@[A-Za-z0-9_.]{2,30})", re.I),
    "pinterest": re.compile(r"https?://(?:[a-z]{2,3}\.)?pinterest\.[a-z.]{2,6}/(?!pin/|pin-builder|ideas|search)"
                            r"([A-Za-z0-9_\-]{2,30})/?(?:[?#]|$)", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/((?:@|channel/|c/|user/)[A-Za-z0-9_.\-]{2,40})", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/((?:company|in)/[A-Za-z0-9_\-%]{2,60})", re.I),
    "whatsapp": re.compile(r"https?://(?:wa\.me/(\+?\d{7,15})|api\.whatsapp\.com/send\?phone=(\d{7,15}))", re.I),
}
SOCIAL_URL = {
    "instagram": "https://www.instagram.com/{}/", "facebook": "https://www.facebook.com/{}",
    "tiktok": "https://www.tiktok.com/{}", "pinterest": "https://www.pinterest.com/{}/",
    "youtube": "https://www.youtube.com/{}", "linkedin": "https://www.linkedin.com/{}",
    "whatsapp": "https://wa.me/{}",
}
BAD_IMG = re.compile(r"logo|icon|sprite|avatar|favicon|pixel|spinner|loading|placeholder|blank|badge|flag|"
                     r"arrow|button|emoji|captcha|gravatar|1x1|tracking", re.I)
BAD_EMAIL = re.compile(r"example\.|sentry|wixpress|domain\.com|email\.com|yourname|@2x|\.(png|jpg|jpeg|gif|webp|svg)$|"
                       r"noreply|no-reply|donotreply", re.I)
CAPACITY = re.compile(
    r"(?:up to|hasta|até|ate|jusqu[’']?à|bis zu|fino a|capacity of|capacidade (?:de|para)|capacité de|"
    r"capacidad (?:de|para))\s*(\d{2,4})\s*(?:guests?|people|persons|pax|convidados|pessoas|invités|personnes|"
    r"invitados|personas|gäste|ospiti|invitati)?", re.I)

_local = threading.local()
_host_lock = threading.Lock()
_host_last = {}
_robots = {}


# ------------------------------------------------------------------ safe, polite HTTP
def public_host(host):
    if ALLOW_PRIVATE:
        return True
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def throttle(host):
    with _host_lock:
        wait = _host_last.get(host, 0) + DELAY - time.time()
        _host_last[host] = max(time.time(), _host_last.get(host, 0) + DELAY)
    if wait > 0:
        time.sleep(wait)


def http_get(url, accept="text/html,application/xhtml+xml,*/*", max_bytes=MAX_BYTES, polite=True):
    """Never raises. Returns (status, final_url, content_type, text). status 0 = network error / blocked."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or not public_host(p.hostname):
        return 0, url, "", ""
    if polite:
        throttle(p.netloc)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                                   "Accept-Language": "en,pt;q=0.8,fr;q=0.7,es;q=0.7"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ct = r.headers.get("Content-Type", "")
            raw = r.read(max_bytes)
            enc = r.headers.get_content_charset() or "utf-8"
            final = r.geturl()
            fp = urllib.parse.urlparse(final)
            if not fp.hostname or not public_host(fp.hostname):     # redirect into the internal network
                return 0, final, ct, ""
            return r.status, final, ct, raw.decode(enc, "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, "", ""
    except Exception:
        return 0, url, "", ""


def allowed(url):
    """robots.txt check (cached per host). Unreachable robots.txt means allowed, 401/403 means disallowed."""
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        status, _, _, text = http_get(base + "/robots.txt", accept="text/plain,*/*", max_bytes=300_000)
        if status in (401, 403):
            rp.disallow_all = True
        elif status == 200:
            rp.parse(text.splitlines())
        else:
            rp.allow_all = True
        _robots[base] = rp
    return rp.can_fetch(UA_TOKEN, url)


# ------------------------------------------------------------------ discovery of the right website
def search_web(query):
    base = os.environ.get("SEARXNG_URL")
    if not base:
        return []
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    status, _, _, text = http_get(url, accept="application/json", polite=False)
    if status != 200:
        return []
    try:
        return json.loads(text).get("results", [])[:8]
    except ValueError:
        return []


def name_tokens(name):
    return [t for t in RE.norm(name).split() if len(t) > 2]


def find_website(row):
    """Return (website_url, socials{}) found via search. The website is only returned if the page verifies."""
    q = f'{row["name"]} {row["city"] or ""} wedding'.strip()
    toks = name_tokens(row["name"])
    socials, seen = {}, set()
    for res in search_web(q):
        url = res.get("url") or ""
        host = urllib.parse.urlparse(url).netloc.lower()
        text = RE.norm((res.get("title") or "") + " " + url)
        overlap = (sum(t in text for t in toks) / len(toks)) if toks else 0
        for kind, rx in SOCIAL.items():
            m = rx.match(url)
            if m and overlap >= 0.6 and kind not in socials:
                socials[kind] = SOCIAL_URL[kind].format(next(g for g in m.groups() if g))
        if DIRECTORIES.search(host) or overlap < 0.6 or host in seen:
            continue
        seen.add(host)
        site = f"{urllib.parse.urlparse(url).scheme}://{host}/"
        if not allowed(site):
            continue
        status, final, ct, html = http_get(site)
        if status != 200 or "html" not in ct.lower():
            continue
        ev = RE.extract(row, final, html)
        if ev["parked"] or not ev["name_match"]:
            continue
        if ev["phone_match"] or location_match(row, html):
            return final, socials
    return None, socials


def location_match(row, html):
    if row["city"] and RE.norm(row["city"]) and RE.norm(row["city"]) in RE.norm(BeautifulSoup(html, "lxml").get_text(" ")):
        return True
    return geo_close(row, html)


def geo_close(row, html):
    for blk in re.findall(r'"latitude"\s*:\s*"?(-?\d+\.?\d*)"?\s*,\s*"longitude"\s*:\s*"?(-?\d+\.?\d*)"?', html):
        try:
            if ranker.haversine(row["lat"], row["lng"], float(blk[0]), float(blk[1])) < 3.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


# ------------------------------------------------------------------ extraction
def page_links(base_url, soup):
    host = urllib.parse.urlparse(base_url).netloc.replace("www.", "")
    ranked = {}
    for a in soup.find_all("a", href=True):
        u = urllib.parse.urljoin(base_url, a["href"]).split("#")[0]
        pu = urllib.parse.urlparse(u)
        if pu.scheme not in ("http", "https") or pu.netloc.replace("www.", "") != host:
            continue
        blob = (pu.path + " " + a.get_text(" ")).lower()
        for rank, (_, kws) in enumerate(LINK_KEYWORDS):
            if any(k in blob for k in kws):
                ranked.setdefault(u, rank)
                break
    return [u for u, _ in sorted(ranked.items(), key=lambda kv: kv[1])][: MAX_PAGES - 1]


def parse_phones(text, soup, region):
    out = []
    for a in soup.find_all("a", href=re.compile(r"^tel:", re.I)):
        raw = urllib.parse.unquote(a["href"][4:]).split("?")[0]
        try:
            n = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(n):
                out.append(phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164))
        except phonenumbers.NumberParseException:
            pass
    if not out:
        for m in phonenumbers.PhoneNumberMatcher(text[:60000], region, leniency=phonenumbers.Leniency.VALID):
            out.append(phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164))
            if len(out) >= 3:
                break
    return list(dict.fromkeys(out))


def parse_emails(html, soup, site_host):
    found = [urllib.parse.unquote(a["href"][7:]).split("?")[0].strip().lower()
             for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I))]
    found += [e.lower() for e in re.findall(r"[A-Za-z0-9._%+\-]{1,40}@[A-Za-z0-9.\-]{3,60}\.[A-Za-z]{2,10}", html)]
    found = [e for e in dict.fromkeys(found) if not BAD_EMAIL.search(e)]
    found.sort(key=lambda e: 0 if site_host and e.split("@")[1].endswith(site_host.replace("www.", "")) else 1)
    return found[:3]


def parse_socials(soup):
    out = {}
    for a in soup.find_all("a", href=True):
        for kind, rx in SOCIAL.items():
            m = rx.match(a["href"].strip())
            if m and kind not in out:
                out[kind] = SOCIAL_URL[kind].format(next(g for g in m.groups() if g))
    return out


def parse_photos(base_url, soup, md):
    urls = []
    for og in soup.find_all("meta", attrs={"property": "og:image"}):
        if og.get("content"):
            urls.append((urllib.parse.urljoin(base_url, og["content"]), soup.title.string.strip() if soup.title and soup.title.string else None))
    for it in md.get("json-ld", []):
        img = it.get("image") if isinstance(it, dict) else None
        for v in (img if isinstance(img, list) else [img]):
            v = v.get("url") if isinstance(v, dict) else v
            if isinstance(v, str):
                urls.append((urllib.parse.urljoin(base_url, v), None))
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        u = urllib.parse.urljoin(base_url, src)
        w = re.sub(r"\D", "", im.get("width") or "") or "0"
        h = re.sub(r"\D", "", im.get("height") or "") or "0"
        if (int(w) and int(w) < 300) or (int(h) and int(h) < 200):
            continue
        urls.append((u, (im.get("alt") or "").strip() or None))
    out, seen = [], set()
    for u, alt in urls:
        path = urllib.parse.urlparse(u).path.lower()
        if BAD_IMG.search(u) or not re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", path + "?") or u in seen:
            continue
        seen.add(u)
        out.append((u, alt))
    return out


def geo_address(md):
    for it in md.get("json-ld", []):
        a = it.get("address") if isinstance(it, dict) else None
        if isinstance(a, dict) and (a.get("streetAddress") or a.get("addressLocality")):
            return ", ".join(x for x in (a.get("streetAddress"), a.get("postalCode"), a.get("addressLocality")) if x)
    return None


def collect_site(row, website):
    """Crawl a vendor's site and return everything found, or {'ok': False, 'why': ...}."""
    if not allowed(website):
        return {"ok": False, "why": "robots"}
    status, final, ct, html = http_get(website)
    if status != 200 or "html" not in ct.lower():
        return {"ok": False, "why": f"http:{status}"}
    host = urllib.parse.urlparse(final).netloc
    region = (row["country"] or "").upper() if row["country"] and len(row["country"]) == 2 else None
    pages = [(final, html)]
    soup0 = BeautifulSoup(html, "lxml")
    for u in page_links(final, soup0):
        if allowed(u):
            s, f, c, h = http_get(u)
            if s == 200 and "html" in c.lower():
                pages.append((f, h))
    data = {"ok": True, "phones": [], "emails": [], "socials": {}, "photos": [], "rating": None, "price": None,
            "styles": [], "capacity": None, "address": None, "pages": [p[0] for p in pages], "identity": False}
    identity = bool(row["website"])        # a website we already hold for this listing is trusted
    for url, h in pages:
        soup = BeautifulSoup(h, "lxml")
        text = soup.get_text(" ", strip=True)
        ev = RE.extract(row, url, h)
        if ev["parked"]:
            return {"ok": False, "why": "parked"}
        if ev["name_match"] and (ev["phone_match"] or location_match(row, h) or row["website"]):
            identity = True
        try:
            md = extruct.extract(h, base_url=url, syntaxes=["json-ld", "microdata", "opengraph"], uniform=True)
        except Exception:
            md = {"json-ld": []}
        data["phones"] += parse_phones(text, soup, region)
        data["emails"] += parse_emails(h, soup, host)
        for k, v in parse_socials(soup).items():
            data["socials"].setdefault(k, v)
        data["photos"] += parse_photos(url, soup, md)
        data["rating"] = data["rating"] or ev["rating"]
        if ev["price"] and not data["price"]:
            data["price"] = (ev["price"], ev["price_unit"], url)
        data["styles"] = list(dict.fromkeys(data["styles"] + ev["styles"]))[:4]
        data["address"] = data["address"] or geo_address(md)
        cm = CAPACITY.search(text)
        if cm and not data["capacity"] and 10 <= int(cm.group(1)) <= 3000:
            data["capacity"] = int(cm.group(1))
    data["identity"] = identity
    data["phones"] = list(dict.fromkeys(data["phones"]))
    data["emails"] = list(dict.fromkeys(data["emails"]))[:3]
    seen, uniq = set(), []
    for u, a in data["photos"]:
        if u not in seen:
            seen.add(u)
            uniq.append((u, a))
    data["photos"] = uniq[:MAX_PHOTOS]
    data["final_url"] = final
    return data


def work(row):
    """Runs in a worker thread: network only, no database access."""
    res = {"id": row["id"], "found_website": None, "found_socials": {}}
    website = row["website"]
    if not website:
        website, soc = find_website(row)
        res["found_website"], res["found_socials"] = website, soc
        if not website:
            res["data"] = {"ok": False, "why": "no-website-found"}
            return res
    res["data"] = collect_site(row, website)
    return res


# ------------------------------------------------------------------ storing results
def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def source(db, lid, field, value, url, method):
    db.execute("INSERT OR REPLACE INTO field_sources VALUES (?,?,?,?,?,?)", (lid, field, str(value)[:500], url, method, now()))


def apply(db, row, res):
    lid, t = row["id"], now()
    d = res["data"]
    stats = {"fields": 0, "photos": 0}
    if res["found_website"]:
        db.execute("UPDATE listings SET website=? WHERE id=? AND website IS NULL", (res["found_website"], lid))
        source(db, lid, "website", res["found_website"], res["found_website"], "search+identity-verified")
        stats["fields"] += 1
    for k, v in res["found_socials"].items():
        if not row[k]:
            db.execute(f"UPDATE listings SET {k}=? WHERE id=?", (v, lid))
            source(db, lid, k, v, v, "search-result")
            stats["fields"] += 1
    if not d["ok"] or not d.get("identity"):
        fails = (row["collect_fail"] or 0) + 1
        nxt = (dt.date.today() + dt.timedelta(days=min(RECHECK_DAYS, 2 ** fails))).isoformat()
        db.execute("UPDATE listings SET collect_fail=?, collect_next_at=? WHERE id=?", (fails, nxt, lid))
        db.execute("""INSERT INTO changes(listing_id,field,old,new,at) VALUES (?,?,?,?,?)""",
                   (lid, "collect", None, d.get("why") or "identity-not-verified", t))
        return stats
    src = d["final_url"]
    sets = {}
    if d["phones"] and not row["phone"]:
        sets["phone"] = d["phones"][0]
    if d["emails"] and not row["email"]:
        sets["email"] = d["emails"][0]
    for k, v in d["socials"].items():
        if not row[k]:
            sets[k] = v
    if d["address"] and not row["address"]:
        sets["address"] = d["address"]
    if d["capacity"] and not row["capacity_max"] and row["category"] == "venue":
        sets["capacity_max"] = d["capacity"]
    if d["rating"]:
        sets.update(rating=d["rating"][0], rating_count=d["rating"][1], rating_source="vendor-site")
    if d["styles"]:
        sets["style_tags"] = ",".join(d["styles"])
    if d["photos"] and not row["photo_url"]:
        sets["photo_url"] = d["photos"][0][0]
    if d["price"]:
        (lo, hi, cur), unit, purl = d["price"]
        sets.update(price_min=lo, price_max=hi, currency=cur, price_unit=unit, price_source_url=purl,
                    price_checked_at=t[:10])
    sets.update(collected_at=t, collect_fail=0,
                collect_next_at=(dt.date.today() + dt.timedelta(days=RECHECK_DAYS)).isoformat())
    cols = ",".join(f"{k}=?" for k in sets)
    db.execute(f"UPDATE listings SET {cols} WHERE id=?", (*sets.values(), lid))
    for k, v in sets.items():
        if k not in ("collected_at", "collect_fail", "collect_next_at"):
            source(db, lid, k, v, src, "site-crawl")
            stats["fields"] += 1
    if d["photos"]:
        db.execute("DELETE FROM photos WHERE listing_id=?", (lid,))
        db.executemany("INSERT OR IGNORE INTO photos(listing_id,url,alt,source_url,fetched_at) VALUES (?,?,?,?,?)",
                       [(lid, u, a, src, t) for u, a in d["photos"]])
        stats["photos"] = len(d["photos"])
    return stats


def due(db, batch):
    need_search = bool(os.environ.get("SEARXNG_URL"))
    where = "status != 'dead' AND (collect_next_at IS NULL OR collect_next_at <= date('now'))"
    if not need_search:
        where += " AND website IS NOT NULL"
    return db.execute(f"""SELECT * FROM listings WHERE {where}
        ORDER BY ((phone IS NULL)+(email IS NULL)+(instagram IS NULL)+(photo_url IS NULL)+(rating IS NULL)
                  +(price_min IS NULL)+(website IS NULL)*2) DESC, confidence DESC
        LIMIT ?""", (batch,)).fetchall()


def run_batch(path, batch=300, workers=8):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    ranker.init(db)
    rows = due(db, batch)
    tot = {"listings": len(rows), "fields": 0, "photos": 0, "rejected": 0, "websites_found": 0}
    by_id = {r["id"]: r for r in rows}
    with cf.ThreadPoolExecutor(workers) as ex:
        for res in ex.map(work, rows):
            row = by_id[res["id"]]
            st = apply(db, row, res)
            tot["fields"] += st["fields"]
            tot["photos"] += st["photos"]
            tot["websites_found"] += bool(res["found_website"])
            tot["rejected"] += (not res["data"]["ok"]) or (not res["data"].get("identity"))
            db.commit()
    print(json.dumps(tot), flush=True)
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--forever", action="store_true")
    ap.add_argument("--sleep", type=int, default=120, help="seconds to wait when nothing is due")
    a = ap.parse_args()
    while True:
        tot = run_batch(a.db, a.batch, a.workers)
        if not a.forever:
            break
        if tot["listings"] == 0:
            time.sleep(a.sleep)


if __name__ == "__main__":
    main()
