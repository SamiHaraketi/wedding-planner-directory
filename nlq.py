#!/usr/bin/env python3
"""Natural-language request -> structured wedding search. Open source only.

Primary: a local open-weights LLM served by Ollama (https://ollama.com), e.g. llama3.1:8b, qwen2.5:7b, mistral.
  OLLAMA_URL (default http://localhost:11434)   OLLAMA_MODEL (default llama3.1:8b)
Fallback (no LLM, no network): multilingual rule parser. The result is ALWAYS validated against the database:
the city must exist in our verified listings, styles/categories must be known, numbers are range-checked,
so a model hallucination can never produce a search the engine cannot run.

  parse("rustic vineyard near Sintra under 20k for 80 guests in June", cities)
  -> {"city": "Sintra", "budget": 20000, "guests": 80, "styles": ["rustic","vineyard"], "month": 6, ...}
"""
import datetime as dt
import difflib
import json
import os
import re
import unicodedata
import urllib.request

from styles import STYLES, STYLE_NAMES
import suggest

CATS = ["venue", "photographer", "music", "catering", "decoration"]
CAT_WORDS = {
    "venue": ["venue", "location", "locale", "espaço", "espaco", "salle", "lugar", "quinta", "finca", "castle"],
    "photographer": ["photographer", "photography", "photo", "video", "fotógrafo", "fotografo", "fotografia",
                     "photographe", "fotograf"],
    "music": ["dj", "band", "music", "musician", "música", "musica", "musique", "orchestra"],
    "catering": ["catering", "caterer", "food", "buffet", "traiteur", "comida", "banquete", "menu"],
    "decoration": ["decor", "decoration", "flowers", "florist", "flores", "fleurs", "decoração", "decoracion",
                   "décoration"],
}
MONTHS = {}
for i, names in enumerate([
    "january jan janeiro janvier enero gennaio januar", "february feb fevereiro février febrero febbraio februar",
    "march mar março mars marzo märz", "april apr abril avril aprile", "may mai maio mayo maggio",
    "june jun junho juin junio giugno juni", "july jul julho juillet julio luglio juli",
    "august aug agosto août agosto august", "september sep sept setembro septembre septiembre settembre",
    "october oct outubro octobre octubre ottobre oktober", "november nov novembro novembre noviembre novembre",
    "december dec dezembro décembre diciembre dicembre dezember"], start=1):
    for w in names.split():
        MONTHS[w] = i


def fold(s):
    return re.sub(r"[^a-z0-9 ]", "", unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()).strip()


def _num(s):
    return float(s.replace(",", "."))


def rule_parse(text, cities=()):
    t = text.lower()
    out = {}
    # budget: "20k", "€20,000", "under 15000 euros", "budget of 18 000"
    m = re.search(r"(?:budget|presupuesto|or[cç]amento|preventivo|under|up to|max(?:imum)?|around|about|até|ate|jusqu'?à|hasta|bis)\D{0,12}"
                  r"([€£$]?\s?\d[\d.,\s]*\d|[€£$]?\s?\d)\s?(k|mil|m)?", t) or \
        re.search(r"([€£$]\s?\d[\d.,\s]*\d|\d[\d.,\s]*\d?\s?(?:k|mil)\b|\d[\d.,\s]*\s?(?:€|eur|euros|usd|gbp|\$|£))", t)
    if m:
        raw = re.sub(r"[€£$\s]", "", m.group(1))
        mult = 1000 if (m.lastindex and m.lastindex >= 2 and m.group(2) in ("k", "mil")) or raw.lower().endswith("k") else 1
        raw = raw.lower().rstrip("k")
        raw = re.sub(r"[.,](?=\d{3}(\D|$))", "", raw)
        try:
            v = _num(raw) * mult
            if 500 <= v <= 1_000_000:
                out["budget"] = v
        except ValueError:
            pass
    g = re.search(r"(\d{1,4})\s*(?:guests?|people|persons|pax|pessoas|convidados|invités|invites|personas|invitados|ospiti|gäste)", t)
    if g and 2 <= int(g.group(1)) <= 2000:
        out["guests"] = int(g.group(1))
    d = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t)
    if d:
        out["date"] = d.group(0)
    else:
        for w, mi in MONTHS.items():
            if re.search(rf"\b{w}\b", t) and len(w) >= 3:
                if w == "mar" and not re.search(r"\bmar\b", t):
                    continue
                out["month"] = mi
                y = re.search(r"\b(20\d{2})\b", t)
                out["year"] = int(y.group(1)) if y else None
                break
    styles = [s for s, kws in STYLES.items() if any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", t) for k in kws)]
    if styles:
        out["styles"] = styles[:4]
    cats = [c for c, ws in CAT_WORDS.items() if any(re.search(rf"\b{re.escape(w)}\b", t) for w in ws)]
    if cats:
        out["categories"] = cats
    pr = {}
    if re.search(r"cheap|affordable|low[- ]cost|tight budget|barato|econ[oó]mic|pas cher", t):
        pr["price"] = 2.0
    if re.search(r"best reviews|top rated|highly rated|best rated|melhores avalia", t):
        pr["quality"] = 2.0
    if re.search(r"\bnear\b|\bclose\b|perto|proche|cerca", t):
        pr["proximity"] = 1.5
    if pr:
        out["priorities"] = pr
    # city: longest known city name contained in the text (accent-insensitive)
    ft = " " + fold(text) + " "
    best = None
    for c in cities:
        fc = fold(c)
        if len(fc) >= 3 and f" {fc} " in ft and (best is None or len(fc) > len(fold(best))):
            best = c
    if not best:                                   # English/other names: "Lisbon", "Munich", "Cologne"
        by_fold = {fold(c): c for c in cities}
        for alias, canon in suggest._REV.items():
            if f" {fold(alias)} " in ft and canon in by_fold:
                best = by_fold[canon]
                break
    if best:
        out["city"] = best
    return out


SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": ["string", "null"]}, "budget": {"type": ["number", "null"]},
        "guests": {"type": ["integer", "null"]}, "date": {"type": ["string", "null"]},
        "month": {"type": ["integer", "null"]}, "styles": {"type": "array", "items": {"type": "string"}},
        "categories": {"type": "array", "items": {"type": "string"}},
        "currency": {"type": ["string", "null"]},
    },
    "required": ["city", "budget", "guests", "styles", "categories"],
}


def llm_parse(text, timeout=8):
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/chat"
    body = {
        "model": os.environ.get("OLLAMA_MODEL", "llama3.1:8b"), "stream": False, "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content":
                "Extract a wedding search from the user's message. Output JSON only. Use null when the user did "
                "not say it. Never guess. styles must be from: " + ", ".join(STYLE_NAMES) +
                ". categories must be from: " + ", ".join(CATS) + ". date only if a full date is given "
                "(YYYY-MM-DD), otherwise month (1-12). budget is a number in the stated currency."},
            {"role": "user", "content": text}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(json.loads(r.read())["message"]["content"])


def sanitize(d, cities):
    """Validate anything that came from a model or a rule. Unknown or out-of-range values are dropped."""
    out = {}
    city = d.get("city")
    if city:
        fc = fold(str(city))
        known = {fold(c): c for c in cities}
        fc = fc if fc in known else suggest._REV.get(fc, fc)
        if fc in known:
            out["city"] = known[fc]
        else:
            close = difflib.get_close_matches(fc, list(known), n=1, cutoff=0.82)
            if close:
                out["city"] = known[close[0]]
    try:
        if d.get("budget") is not None and 500 <= float(d["budget"]) <= 1_000_000:
            out["budget"] = float(d["budget"])
        if d.get("guests") is not None and 2 <= int(d["guests"]) <= 2000:
            out["guests"] = int(d["guests"])
    except (TypeError, ValueError):
        pass
    if d.get("date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d["date"])):
        try:
            dt.date.fromisoformat(d["date"])
            out["date"] = d["date"]
        except ValueError:
            pass
    if d.get("month") and str(d["month"]).isdigit() and 1 <= int(d["month"]) <= 12:
        out["month"] = int(d["month"])
        if d.get("year"):
            out["year"] = int(d["year"])
    st = [s for s in (d.get("styles") or []) if s in STYLE_NAMES]
    if st:
        out["styles"] = st
    ct = [c for c in (d.get("categories") or []) if c in CATS]
    if ct:
        out["categories"] = ct
    if d.get("currency") and str(d["currency"]).upper() in ("EUR", "GBP", "USD", "CAD", "CHF"):
        out["currency"] = str(d["currency"]).upper()
    if isinstance(d.get("priorities"), dict):
        out["priorities"] = {k: float(v) for k, v in d["priorities"].items()
                             if k in ("price", "quality", "style", "proximity") and 0 < float(v) <= 4}
    return out


def parse(text, cities=(), use_llm=True):
    """Return (filters, method). Rules always run; the LLM, if reachable, fills what the rules missed or got wrong."""
    rules = sanitize(rule_parse(text, cities), cities)
    if use_llm and os.environ.get("OLLAMA_DISABLE") != "1":
        try:
            llm = sanitize(llm_parse(text), cities)
            merged = {**rules, **{k: v for k, v in llm.items() if v not in (None, [], {})}}
            if rules.get("priorities"):
                merged["priorities"] = rules["priorities"]
            return merged, "llm+rules"
        except Exception:
            pass
    return rules, "rules"
