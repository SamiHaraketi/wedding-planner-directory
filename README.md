# Wedding directory builder

Builds a real, source-linked directory of wedding venues, photographers, music,
catering and decoration/florists from OpenStreetMap, for Europe, Canada and the USA.

## Run it (about 1-3 hours, free)
1. Create a GitHub repo and upload everything in this folder (keep the .github folder).
2. Repo > Actions > "Build wedding directory database" > Run workflow.
3. When finished, open the run and download the artifact "wedding-directory":
   - final.sqlite  (the database)
   - final.csv     (same data, opens in Excel / imports to Supabase, Webflow CMS, Airtable)
   - report.txt    (live counts by category and status)

## Status meaning
- imported: real OSM business, no contact info
- verified_contact: has name + address/city + a phone, email or website
- verified_web: website answered the liveness check
Dead websites are removed. Use verified_web + verified_contact in public results.

## Add more places
Add entries to regions.json (Geofabrik URLs). US: one entry per state.

## Known limits
- OSM is thin for DJs/bands and has no prices or ratings. Fill those with Google Places /
  Foursquare / Yelp APIs (needs keys) and by supplier submissions.
- Data licence: ODbL. Keep the "(c) OpenStreetMap contributors" credit in the app.

## Keeping it fresh, non-stop (refresh engine)
After the first build, the "Refresh and re-verify listings" workflow runs on demandours:
- re-checks the stalest listings first (never-checked, then failing, then oldest)
- reads each supplier's own website: is it alive, is it a parked domain, does the page show the business
  name and the phone/email we have, does it publish a "from EUR x" price or JSON-LD price range
- scores confidence 0-1; 3 failed checks in a row hides a listing; a later success brings it back
- prices are stored only when the page states them, with the page URL and date, and expire after 60 days
- the app should read ONLY the view `public_listings` (confidence >= 0.6, checked in the last 90 days)
- the monthly build adds newly found businesses without erasing any verification history
- every status/price change is logged in table `changes`
Download the latest data any time from the GitHub Release named "database" (live.sqlite, public_listings.csv).

Honest limits: this is fresh within hours-to-days, not live to the second. Businesses that block bots or
have no website stay at lower confidence. Availability and booked dates cannot be known without the
supplier or a booking API; add a supplier-update form or calendar integrations for that.

---
# The matching engine (ranking, packages, click-through, learning)

## What each file does
| File | Job |
|---|---|
| `ranker.py` | Scores every vendor on 8 transparent features, builds full packages inside the budget, explains each result |
| `api.py` | HTTP API for the app: `/search`, `/packages`, `/go/{id}` (click-through), reviews, vendor self-service |
| `learn.py` | Learns ranking weights from real clicks/calls/emails (position-debiased) |
| `refresh_engine.py` | Re-verifies listings on demand; reads price, ratings, photo, style from the vendor's own site |
| `overture_import.py` | Biggest free growth source: Overture Maps places (global, with websites and phones) |
| `sync_ical.py` | Reads vendors' public calendar links into availability |
| `enrich_google.py` | Links Google place_ids; live Google rating/hours/photos at display time (Google terms: not stored) |
| `apply_release.py` | Pulls each refreshed listings release into the API database without losing clicks/reviews/availability |
| `test_engine.py` | 13 end-to-end tests. Run `python test_engine.py` |

## Run the API
```
pip install -r requirements.txt
gh release download database -p live.sqlite -O app.sqlite     # first time only
WEDDING_DB=app.sqlite SECRET=<long-random> ADMIN_TOKEN=<long-random> GOOGLE_API_KEY=<optional> \
  uvicorn api:app --host 0.0.0.0 --port 8000
```
Example: `/search?city=Lisboa&country=PT&categories=venue,photographer&budget=25000&guests=80&styles=rustic,garden&date=2027-06-12`
Every result has `actions.website`, `actions.call`, `actions.email`: put those links on the buttons. They log the click
(this is the training signal) and send the couple to the vendor. Native apps can add `&format=json` to get the link back.

## Keep it running (cron on the API host)
- daily:  `gh release download database -p live.sqlite -O release.sqlite && python apply_release.py app.sqlite release.sqlite`
- daily:  `python sync_ical.py app.sqlite`            (availability from vendor calendars)
- weekly: `python learn.py app.sqlite`                (re-learn weights from behaviour)
- weekly: `python enrich_google.py link app.sqlite --limit 2000`   (needs GOOGLE_API_KEY)
GitHub Actions already handle: monthly OpenStreetMap build, monthly Overture growth (`grow.yml`), 6-hourly re-verification.

## How the scoring works (all features 0..1, weights sum to 1)
price fit .24 | quality .22 | style .16 | proximity .10 | verification confidence .10 | availability .08 |
contact completeness .06 | freshness .04. Couples can pass `priorities=price:2,style:1.5` to re-weight.
After enough clicks, learned weights are blended in (max 50% influence). New vendors get a small exploration bonus
so they are shown and can earn clicks. Unknown price/rating/availability is scored as unknown, never guessed.

## Where prices, availability, reviews and photos come from (honest version)
- Prices: only if the vendor's own page states them (with URL + date), or the vendor submits them (claim link).
  Many vendors publish none; they show "request a quote".
- Availability: vendor calendar link (iCal) or vendor form. No source exists for it otherwise, in any country.
- Reviews: (1) the rating schema vendors publish on their own site, (2) your couples' reviews inside the app (moderated),
  (3) Google live via `/listing/{id}/live`. Google, Yelp and Tripadvisor forbid copying their reviews into your own database.
- Photos: the vendor's own site image (link + credit). Do not copy photos from Google.

## Making it the biggest directory
1. Overture + OSM monthly (automatic). 2. Add national registries (Companies House UK, SIRENE France, KvK NL...).
3. Vendor claim links: `GET /admin/claim-link/{id}?admin=...` and email vendors; claimed vendors add price + calendar.
4. A "suggest a vendor" form for couples. 5. Couples' reviews: this becomes your own data nobody else has.

## Before real traffic
Move from SQLite to Postgres/Supabase, put the API behind HTTPS, set SECRET/ADMIN_TOKEN, rate-limit /reviews,
and update FX rates live (`ranker.FX_TO_EUR`).

---
# AI optimiser, natural-language search and the continuous collector (all open source)

## The AI pieces
| Piece | Open-source tech | What it does |
|---|---|---|
| `optimizer.py` | Google OR-Tools CP-SAT | Solves "best vendor per category inside the budget" to PROVEN optimality (not a heuristic). Re-scores distance for each candidate venue. Returns Best match / Best value / Lowest price plus swap options per category |
| `ranker.py` + `learn.py` | pure Python | 8 transparent features; weights re-learned from real clicks, calls and emails |
| `nlq.py` | Ollama + any open-weights LLM (llama3.1, qwen2.5, mistral) with a rule-based fallback in 6 languages | "rustic vineyard near Sintra under 20k for 80 guests in June" -> validated filters. Model output is checked against the database, so it cannot invent a city, style or budget |
| `suggest.py` | pure Python | City autocomplete: accent/typo/alias tolerant (Lisbon = Lisboa), ghost-text completion, vendor counts |
API: `/suggest/cities?q=lis`, `/parse?q=...`, `/ask?q=...` (sentence in, packages out), `/packages`, `/search`, `/go/{id}`.

## The collector (`collector.py`)
Visits each vendor's OWN website (and its contact / gallery / prices / about pages) and stores: phone, email, Instagram,
Facebook, TikTok, Pinterest, YouTube, LinkedIn, WhatsApp, up to 8 photos (hot-linked, credited), published rating
(schema.org), "from" price, capacity, address, style tags. Listings with no website are searched with SearXNG
(self-hosted, open source) and the result is only accepted if the page proves it is the same business
(name + phone, or name + city/coordinates). Every field keeps its source URL in `field_sources`.
Polite by design: obeys robots.txt, 1 request per domain per 1.5 s, identifies itself, blocks private/internal addresses.
Set COLLECTOR_UA to your real site/contact before running at scale.

## Two ways to keep it running non-stop (pick one)
A. Free, GitHub only: `collect.yml` every third UTC day, `refresh.yml` on demand, `grow.yml` on demand. They share one lock so they
   never overwrite each other. The app database pulls the result daily with `apply_release.py`.
B. Always-on server: `docker compose up -d` starts API + collector (runs forever) + verifier + SearXNG + Ollama + weekly learner
   on one small VPS (4 GB RAM; 16 GB if you run the LLM). The collector then works directly on the app database.
`tests.yml` runs both test files on every push: `python test_engine.py` and `python test_collector.py`.

## What it will NOT do (on purpose)
- Scrape Google, Yelp or Tripadvisor ratings/reviews/photos: their terms forbid it, and it gets IPs banned. Ratings come
  from the vendor's own published markup and from your couples' reviews; Google is shown live via `/listing/{id}/live`.
- Guess a website, phone or rating. If identity cannot be verified, nothing is stored.


## GitHub Actions schedule

The public repository uses standard Ubuntu runners. Tests run first. A passing push to the default branch starts the directory build, which publishes a listings database under the `database` release. Collection and re-verification run together every third UTC day at 04:17 UTC. The individual refresh and Overture growth workflows can also be run manually.

The release is public. Use it to seed the app's separate private database. Keep application reviews, click history, accounts, credentials and private data in the production environment.
