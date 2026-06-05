# Scraping recon — nehnutelnosti.sk (Task 0)

**Objective (measurable):** pull listing data from one small locality — even a single
results page — for **both `predaj` (sell) and `prenajom` (rent)**, with the fields the
medallion pipeline needs (`detail_id`, `price`, `area`, `price/m²`, `category`, `city`).
The runnable yardstick for this is [`backend/eval_scrape.py`](backend/eval_scrape.py)
(exit 0 = we can pull the data, 1 = we can't yet, with diagnostics).

**Status: SOLVED via a headless browser. The eval PASSES.** Not pullable by any HTTP
client (data isn't in the HTML/RSC; the JSON API is F5-gated even from the page's own
fetch), but a real Chromium render exposes the listings in the DOM. Implemented in
[`backend/realestate/browser_scraper.py`](backend/realestate/browser_scraper.py) (Playwright,
extracting the per-card `data-test-id="text"` cells). `eval_scrape.py` now returns **PASS**
for both `predaj` and `prenajom` on `bratislava-ruzinov` (31 / 30 rows) and `stupava`
(30 / 17) — all required fields ≥97%. Remaining work is **hosting** (Chromium needs
Functions-on-Container-Apps, not Flex Consumption — see cost table in chat) and wiring the
scraper into the bronze/ledger pipeline. Investigation details below.

> **Update (verified by running the eval).** The plumbing the eval needs now exists
> (`realestate.scraper.sweep_unit` + `realestate.schemas.locality_geo`), so
> `eval_scrape.py` runs end-to-end instead of crashing on import. Result: **FAIL with
> `status=done`, `rows=0`** for both deals. The fetch *succeeds* (HTTP 200, 111 KB) — the
> page just carries no listings, which empirically confirms the conclusion below. The one
> framing correction the run forced is in finding B (see note there).

---

## What works

| # | Finding | Evidence |
|---|---------|----------|
| 1 | **Results URL template is correct.** `https://www.nehnutelnosti.sk/vysledky/<deal>/byty/<locality-slug>/` (redirects to `?p=<page>`). | `HTTP 200`, 111 KB HTML. |
| 2 | **Locality slugs are correct** (e.g. `bratislava-ruzinov`, corridor towns). | Page loads, no 404. |
| 3 | **The data API endpoint is identified.** NestJS REST API at base `https://www.nehnutelnosti.sk/api/v2`, listings at **`/advertisement/listing`** (+ `/advertisement/listing/count`, + URL→filter converters `/advertisement/listing/filter/internal-from-url`). | Found in the JS bundles (`NEXT_PUBLIC_NEHNUTELNOSTI_NEST_API_URL`). |
| 4 | **The data shape is known-ish** (GraphQL/REST field names): `advertisementsList.results[]`, `priceInfo`, `category`, `numberOfRooms`, `floorSize`, listing `url` → `/detail/<id>/`. | Bundle symbols + your earlier working parser. |
| 5 | **Geo can be derived from the slug** — the results payload does *not* carry the listing locality, but each sweep targets one locality, so `city`/`district`/`region` come from `schemas.locality_geo()`. | Now implemented in `realestate/schemas.py` (BA boroughs + corridor towns; always returns a non-null city). *Was not present in the repo at first recon — the eval couldn't import until `schemas.locality_geo` and `scraper.sweep_unit` were added.* |

## What does NOT work

| # | Finding | Evidence | Why it matters |
|---|---------|----------|----------------|
| A | **No JSON-LD on results pages.** | `jsonld_block_count: 0`. | The original parser's `<script ld+json>` strategy returns nothing. |
| B | **The HTML is a client-rendered shell with no listings — and that's not a caching artifact.** A *fresh origin render* (`X-Cache-Status: MISS`, `X-Served-By: cla-nginx-cache-production-02`) also returns 0 listings. The earlier `HIT` was incidental; busting the cache does not help. | Re-fetched live: 200, 111 KB, `MISS`, still 0× `/detail/`, 0× `priceSpecification`/`floorSize`. | There is no server-rendered listing data to scrape from HTML by any HTTP-cache trick — the listings are injected client-side. |
| C | **The `__next_f` RSC payload contains no listing data** — only framework module declarations, fonts, and the GDPR/TCF consent script (~30 KB total). The ~26 `push()` calls *are* present and `_parse` reconstructs/decodes them cleanly — there are simply no `"item":{…/detail/…}` objects to extract. | Marker scan on a live page: ~26 `__next_f.push`, but 0× `/detail/`, `priceSpecification`, `floorSize`, `numberOfRooms`. | Reconstructing the Flight payload won't yield listings either — the parser works, the data isn't there. |
| D | **The JSON API is auth-guarded — `403 {"supportID": "..."}`** for every anonymous attempt. | Probed repeatedly. | This is the blocker. |
| E | **403 is NOT fixed by headers, browser TLS impersonation, or a residential IP.** Tried: full browser headers (`sec-ch-ua`, `Origin`, `Referer`…), `curl_cffi` Chrome JA3 impersonation, and the *same call from your own machine where the browser works*. All 403. | 3 independent probes. | Rules out header/TLS/IP causes. |
| F | **The 403 is the NestJS app's own guard, not a 3rd-party WAF.** Response has only helmet.js security headers + `Connection: close`, no Cloudflare/Imperva signature. | 403 headers. | The API expects an **app-acquired token**. |

## Root cause (revised — verified with a real headless browser)

The listings are **injected client-side by the page's own JS**, from data that only a
fully-presenting browser receives; the HTML shell (cache `HIT` *or* `MISS`) and the RSC
payload never contain them. Critically, **there is no listings API request to replicate**:
a real Chromium session loading the results page makes only these `/api/v2` calls —

```
GET  /api/v2/notification/center/notification?section=SEARCH_RESULTS   200
GET  /api/v2/session                                                   401 (logged-in only)
POST /api/v2/advertisement/listing/url/internal-filter                 201  (no listings in body)
POST /api/v2/advertisement/listing/count                               201  (returns just a number)
```

— **no `/advertisement/listing` data call, no GraphQL call.** The `count` and
`url/internal-filter` endpoints are open (they answer plain `curl` POSTs too), but they
don't return listings. The actual `/advertisement/listing` data endpoint returns
`403 {"supportID"}` to **every** non-page caller — plain `curl`, `curl_cffi` (Chrome JA3),
*and even the real browser's own in-page `fetch()`** (`page.evaluate(fetch …)`) and
Playwright's `APIRequestContext`. It is gated by an **F5 BIG-IP ASM** challenge (cookie
`TS01ff63df`, JS-minted — no `Set-Cookie`, so a cookie jar can't capture it) plus request
signing the app does internally. Net: the JSON API is **not** practically replaceable by
direct HTTP.

**What does work:** a headless browser **rendering the page** exposes the data in the DOM —
verified: **159 `a[href*="/detail/"]` links + visible `€` prices** on
`bratislava-ruzinov` `predaj`. The detail URL even encodes facts:
`/detail/<id>/predaj-2-izbovy-byt-38-m2-bratislava-ruzinov` → id, deal, rooms, m², locality.

---

## Options to actually pull the data (pick one — decision needed)

1. **Headless browser (Playwright/Chromium) — the only route that works.** Render the
   results page and scrape the DOM (detail cards carry id/price/area/rooms; the rest is in
   the card text). *Proven.* *Cost:* Chromium does **not** fit Azure **Flex Consumption** —
   ingestion must move to a **Container App / VM / GitHub Actions runner** (or a scheduled
   container), writing bronze to the same Blob lake.
2. ~~Replicate the token / call the API directly~~ — **ruled out.** There's no listings API
   call to mimic; the data endpoint is F5-gated even from the real browser's own fetch.
3. **Official API / data access from nehnutelnosti.sk (United Classifieds)** — slowest to
   arrange, but the only fully ToS-clean, durable source for a workshop artifact.

**Recommendation:** go with **option 1** via a small Playwright DOM scraper running off
Flex Consumption (Container App or a scheduled GitHub Action), feeding the existing Blob
lake. In parallel, send United Classifieds a request for official access (option 3) as the
durable path. `realestate/scraper.py`'s HTTP fetch layer is the wrong shape and will be
replaced by the browser scraper; the geo-from-slug helper, ledger, and bronze plumbing stay.

---

## Endpoints discovered (reference)

```
Base (NestJS):  https://www.nehnutelnosti.sk/api/v2
  /advertisement/listing                                  # the listings (auth-guarded)
  /advertisement/listing/count                            # result count
  /advertisement/listing/count/total-rounded
  /advertisement/listing/featured
  /advertisement/listing/filter/internal-from-url         # public URL -> internal filter
  /advertisement/listing/url/public-filter
Other hosts:
  https://user.nehnutelnosti.sk                           # OAuth / anonymous token (NEXT_PUBLIC_OAUTH_API_URL)
  https://admin-api.nehnutelnosti.sk/v1                    # admin API
  https://img.nehnutelnosti.sk                            # images
  https://plt.unitedclassifieds.sk/parameter/api/v1       # platform parameters
Request headers the app sets on API calls: x-api-key, x-user-id, Content-Type: application/json (POST)
```

## How to reproduce the eval

```powershell
cd backend
.venv\Scripts\python.exe eval_scrape.py                 # both deals, default locality
.venv\Scripts\python.exe eval_scrape.py --locality stupava
```

Today it FAILs, but **not at the fetch step** — the fetch returns HTTP 200 (111 KB). It
fails at the **parse step**: the page carries no listings, so `_parse` yields 0 rows and
every coverage check reads 0%. The run reports `status=done, rows=0` for both deals (a 403
on the guarded JSON API would instead surface as `status=blocked` — that path isn't even
reached, because the page never calls the API server-side). Once a working fetch
(option 1 or 2) is wired into `scraper.sweep_unit`, this same eval flips to PASS with no
changes to the eval itself.

> **Note on the live route.** The deployed `/scrape-realestate` route in
> `function_app.py` builds the results URL as `/vysledky/{type}/{loc}/{deal}` — the wrong
> segment order. The verified template (finding #1, and what `sweep_unit` uses) is
> `/vysledky/{deal}/{type}/{loc}/`. The route would mis-route regardless of the data
> problem; worth fixing alongside the fetch-layer replacement.
