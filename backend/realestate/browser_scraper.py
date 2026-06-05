"""Browser-based scraper for nehnutelnosti.sk results pages.

Why a browser: the results page is a client-rendered Next.js app behind an F5 ASM
challenge. The HTML shell carries no listings, the `__next_f` payload carries no
listings, and the JSON API (`/api/v2/advertisement/listing`) 403s every non-page
caller — including the page's own in-page fetch. The only thing that yields data is
a real browser rendering the page. See RECON_FINDINGS.md for the full investigation.

Extraction is off the rendered DOM. Each listing card exposes ordered
`[data-test-id="text"]` elements — badge, title, address, category, area, price,
unit-price — which we classify by pattern (robust to the hashed MUI class names).
Geo (city/district/region) is derived from the sweep's slug via schemas.locality_geo
(the cards show the address too, but the slug is authoritative and always present).

Captures structured FACTS only (id, price, area, rooms, category, url) — never the
description prose (agency copyright).

Requires Playwright + Chromium:  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import re

import polars as pl

from realestate.schemas import locality_geo

BASE = "https://www.nehnutelnosti.sk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SWEEP_COLS = ["detail_id", "detail_url", "title", "category", "rooms", "area_m2",
              "price_eur", "price_per_m2", "price_on_request",
              "deal", "locality", "city", "district", "region"]

# Field classifiers over a card's data-test-id="text" values.
_AREA_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*m²\s*$")
_UNIT_RE = re.compile(r"€\s*/\s*m²", re.IGNORECASE)
_PRICE_RE = re.compile(r"^\s*\d[\d\s]*\s*€\s*$")          # bare € amount (sale)
_RENT_RE = re.compile(r"^\s*\d[\d\s]*\s*€\s*/\s*mes", re.IGNORECASE)  # 'X €/mesiac' (rent)
_CAT_RE = re.compile(r"(?i)^\s*(garsónka|dvojgarsónka|\d+\s*izbový\s*byt|mezonet|"
                     r"apartmán|loft|iný\s+byt|\d+\s*a?\s*viac\s*izbový\s*byt)\s*$")
_ROOMS_RE = re.compile(r"(\d+)\s*izb", re.IGNORECASE)
_DETAIL_ID_RE = re.compile(r"/detail/([^/]+)/")

# In-page DOM extraction: per unique /detail/ link, the card's ordered text cells.
_EXTRACT_JS = r"""() => {
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll("a[href*='/detail/']")) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/detail\/([^\/]+)\//);
    if (!m) continue;
    const id = m[1];
    if (seen.has(id)) continue;
    // climb to the smallest ancestor that holds both the price (€) and the area (m²)
    let el = a, card = null;
    for (let i = 0; i < 10 && el; i++) {
      const t = el.innerText || '';
      if (t.includes('€') && t.includes('m²')) { card = el; break; }
      el = el.parentElement;
    }
    if (!card) continue;
    seen.add(id);
    const texts = [...card.querySelectorAll('[data-test-id="text"]')]
        .map(e => (e.innerText || '').trim()).filter(Boolean);
    out.push({ id, href: a.href, texts });
  }
  return out;
}"""


def _to_float(s: str) -> float | None:
    """Parse a Slovak-formatted number: '189 900' -> 189900, '4 997,37' -> 4997.37,
    '117.12' -> 117.12. Spaces (incl. NBSP/narrow) are thousands separators."""
    if not s:
        return None
    s = re.sub(r"[^\d.,]", "", s.replace(" ", "").replace(" ", "").replace(" ", ""))
    if not s:
        return None
    if "," in s and "." in s:           # e.g. '1.234,56' -> dot thousands, comma decimal
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                       # '4997,37' -> decimal comma
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _results_url(locality: str, deal: str, ptype: str, page: int) -> str:
    base = f"{BASE}/vysledky/{deal}/{ptype}/{locality}/"
    return base if page <= 1 else f"{base}?p={page}"


def _parse_card(card: dict, locality: str, deal: str) -> dict | None:
    """Map one card's {id, href, texts} to a listing row. None if no id."""
    detail_id = card.get("id")
    if not detail_id:
        return None
    texts = card.get("texts") or []

    area = price = ppm2 = category = rent = None
    for t in texts:
        if category is None and _CAT_RE.match(t):
            category = t.strip()
        elif area is None and _AREA_RE.match(t):
            area = _to_float(t)
        elif ppm2 is None and _UNIT_RE.search(t):       # 'X €/m²' (unit price)
            ppm2 = _to_float(t)
        elif price is None and _PRICE_RE.match(t):       # bare 'X €' (sale price)
            price = _to_float(t)
        elif rent is None and _RENT_RE.match(t):         # 'X €/mesiac' (monthly rent)
            rent = _to_float(t)

    # Rentals have no bare price — the monthly rent is the price. (Sale cards may
    # also show a mortgage 'od X €/mesačne', but their bare price wins, so rent is
    # only used when no sale price was found.)
    if price is None and rent is not None:
        price = rent
    if ppm2 is None and price and area:
        ppm2 = round(price / area, 2)
    rooms = None
    if category:
        rm = _ROOMS_RE.search(category)
        rooms = int(rm.group(1)) if rm else (1 if "garsón" in category.lower() else None)

    geo = locality_geo(locality)
    return {
        "detail_id": detail_id,
        "detail_url": card.get("href"),
        "title": texts[1] if len(texts) > 1 else None,
        "category": category,
        "rooms": rooms,
        "area_m2": area,
        "price_eur": price,
        "price_per_m2": ppm2,
        "price_on_request": price is None,
        "deal": deal,
        "locality": locality,
        "city": geo["city"],
        "district": geo["district"],
        "region": geo["region"],
    }


def fetch_listings(locality: str, deal: str, *, ptype: str = "byty", max_pages: int = 1,
                   headless: bool = True, nav_timeout_ms: int = 60000,
                   settle_ms: int = 3000) -> tuple[list[dict], int, str | None]:
    """Render `max_pages` of one (locality, deal) and parse the cards.
    Returns (rows, n_pages, error). Imports Playwright lazily so the module stays
    importable on hosts without it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return [], 0, f"playwright not installed: {e}"

    rows: list[dict] = []
    seen: set[str] = set()
    n_pages = 0
    error = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="sk-SK", user_agent=UA)
        page = ctx.new_page()
        try:
            for n in range(1, max_pages + 1):
                page.goto(_results_url(locality, deal, ptype, n),
                          wait_until="networkidle", timeout=nav_timeout_ms)
                page.wait_for_timeout(settle_ms)
                cards = page.evaluate(_EXTRACT_JS)
                n_pages += 1
                page_new = 0
                for c in cards:
                    if c["id"] in seen:
                        continue
                    seen.add(c["id"])
                    row = _parse_card(c, locality, deal)
                    if row:
                        rows.append(row)
                        page_new += 1
                if page_new == 0:        # no new listings -> past the last page
                    break
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        finally:
            browser.close()
    return rows, n_pages, error


def sweep_unit(locality: str, deal: str, max_pages: int = 1, ptype: str = "byty",
               headless: bool = True, **_ignored) -> dict:
    """Sweep one (locality, deal) unit via a headless browser.
    Returns {df, status, n_pages, error} — same shape the eval/ledger expect."""
    rows, n_pages, error = fetch_listings(locality, deal, ptype=ptype,
                                          max_pages=max_pages, headless=headless)
    status = "done" if (rows or not error) else "error"
    df = (pl.DataFrame(rows).select([c for c in SWEEP_COLS if c in pl.DataFrame(rows).columns])
          if rows else pl.DataFrame({c: [] for c in SWEEP_COLS}))
    return {"df": df, "status": status, "n_pages": n_pages, "error": error}
