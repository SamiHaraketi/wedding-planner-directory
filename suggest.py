#!/usr/bin/env python3
"""City autocomplete: instant, accent-insensitive, alias-aware (Lisbon = Lisboa = Lissabon), typo tolerant,
ranked by how many verified vendors the city has. Also returns `ghost`, the greyed-out inline completion."""
import difflib
import math
import time
import unicodedata

ALIASES = {
    "lisboa": ["lisbon", "lissabon", "lisbonne", "lisbona"], "porto": ["oporto"],
    "munchen": ["munich", "monaco di baviera"], "koln": ["cologne", "colonia", "köln"],
    "firenze": ["florence", "florenz", "florencia"], "roma": ["rome", "rom"],
    "venezia": ["venice", "venedig", "venecia"], "milano": ["milan", "mailand"],
    "napoli": ["naples", "neapel"], "wien": ["vienna", "viena"], "praha": ["prague", "prag", "praga"],
    "kobenhavn": ["copenhagen", "kopenhagen"], "bruxelles": ["brussels", "brussel", "bruselas"],
    "sevilla": ["seville", "siviglia"], "athina": ["athens", "athen", "atenas"],
    "warszawa": ["warsaw", "varsovia"], "krakow": ["cracow", "cracovia"], "gdansk": ["danzig"],
    "wroclaw": ["breslau"], "zurich": ["zürich"], "geneve": ["geneva", "genf", "ginebra"],
    "den haag": ["the hague", "la haye"], "antwerpen": ["antwerp", "anvers"], "gent": ["ghent", "gand"],
    "torino": ["turin"], "genova": ["genoa", "genua"], "beograd": ["belgrade"], "bucuresti": ["bucharest"],
    "montreal": ["montréal"], "new york": ["nyc", "new york city"], "saint petersburg": ["st petersburg"],
}
_REV = {}
for canon, alts in ALIASES.items():
    for a in alts:
        _REV.setdefault(a, canon)

_cache = {"t": 0, "key": None, "rows": []}


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()
    return " ".join("".join(ch if ch.isalnum() or ch == " " else " " for ch in s).split())


def flag(cc):
    cc = (cc or "").upper()
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc) if len(cc) == 2 and cc.isalpha() else ""


def _cities(db, ttl=600):
    key = id(db)
    if time.time() - _cache["t"] > ttl or _cache["key"] != key:
        rows = db.execute("""SELECT city, country, COUNT(*) FROM public_listings
                             WHERE city IS NOT NULL AND city != '' GROUP BY city, country""").fetchall()
        _cache.update(t=time.time(), key=key, rows=[(r[0], r[1], r[2], fold(r[0])) for r in rows])
    return _cache["rows"]


def reset_cache():
    _cache["t"] = 0


def suggest_cities(db, q, limit=8):
    fq = fold(q)
    rows = _cities(db)
    if not fq:                                         # nothing typed: the biggest markets
        top = sorted(rows, key=lambda r: -r[2])[:limit]
        return {"query": q, "ghost": "", "suggestions": [_fmt(r) for r in top]}
    canon = _REV.get(fq)                               # whole-word alias: "lisbon" -> lisboa
    alias_hits = {c for a, c in _REV.items() if a.startswith(fq)} | ({canon} if canon else set())
    scored = {}
    for city, country, n, f in rows:
        s = None
        if f.startswith(fq):
            s = 3.0
        elif f in alias_hits or any(w.startswith(fq) for w in f.split()):
            s = 2.5 if f in alias_hits else 2.0
        elif fq in f:
            s = 1.0
        elif len(fq) >= 3 and difflib.SequenceMatcher(None, fq, f[:len(fq)]).ratio() >= 0.8:
            s = 0.5                                    # typo tolerance: "barcelon", "lisbao"
        if s is not None:
            scored[(city, country)] = (s + math.log10(n + 1) / 10, n)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])[:limit]
    out = [_fmt((c, k, v[1], fold(c))) for (c, k), v in ranked]
    ghost = ""
    if out and fold(out[0]["city"]).startswith(fq) and len(q) <= len(out[0]["city"]):
        ghost = out[0]["city"][len(q):]
    return {"query": q, "ghost": ghost, "suggestions": out}


def _fmt(r):
    city, country, n = r[0], r[1], r[2]
    return {"city": city, "country": country, "vendors": n,
            "label": f"{flag(country)} {city}, {country}".strip(), "sub": f"{n} verified vendors"}
