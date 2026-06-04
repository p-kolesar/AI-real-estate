"""Sweep one (locality, deal) unit of nehnutelnosti.sk into a bronze slice.

Two layers, deliberately separated because they have very different stability:

  * Fetch layer (stable): a polite, paged HTTP session — browser headers, robots
    honored, jittered inter-page delays, stop-on-403/429 (+ Retry-After), a hard
    per-run page cap, and the page-33 / ~990-result cap guard from the charter.
    This part is correct regardless of how the site renders listings.

  * Parse layer (UNCONFIRMED — Task 0): turning a results page into BRONZE rows.
    The site is a Next.js app; listings live in JSON-LD <script> blocks and/or the
    `__next_f` RSC payload, whose exact field names aren't verified yet. So parsing
    is isolated in `_extract_listings_from_html` + `_parse_jsonld_listing`, and
    `recon()` dumps the real payload structure so the parser can be fixed against
    live data without touching the fetch layer.

Config (env, with timeout-safe defaults — see SCRAPE_* below). NOTE: host.json caps
a function execution at 5 min; the charter's 20–40 s inter-page delay only fits if
you raise functionTimeout. Defaults here (4–9 s) keep a ~33-page sweep inside 5 min.

ALL timestamps are UTC (see schemas.py). Description prose is never stored (copyright);
only the parsed `street` token is kept.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import polars as pl
import requests

from realestate.schemas import BRONZE_SCHEMA, DEALS, LOCALITY_SLUGS, TYPE

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.nehnutelnosti.sk"

# Search-results URL template. UNCONFIRMED (Task 0): confirm the real path/slug shape
# against a live URL, then fix only this one line. `{page}` is 1-based.
#   observed-ish pattern: /vysledky/<deal>/<type>/<locality-slug>/?p=<page>
SEARCH_URL_TEMPLATE = "/vysledky/{deal}/{type}/{slug}/?p={page}"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


MIN_DELAY_S = _cfg_float("SCRAPE_MIN_DELAY_S", 4.0)
MAX_DELAY_S = _cfg_float("SCRAPE_MAX_DELAY_S", 9.0)
MAX_PAGES = _cfg_int("SCRAPE_MAX_PAGES", 33)        # ~990 results @ ~30/page (charter cap)
PAGE_SIZE = _cfg_int("SCRAPE_PAGE_SIZE", 30)         # expected listings per full page
REQUEST_TIMEOUT_S = _cfg_int("SCRAPE_REQUEST_TIMEOUT_S", 30)


# ---------------------------------------------------------------------------
# HTTP session + politeness
# ---------------------------------------------------------------------------

def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.getenv("SCRAPE_USER_AGENT", DEFAULT_USER_AGENT),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return s


_robots_cache: dict[str, RobotFileParser] = {}


def _robots_allowed(url: str, user_agent: str) -> bool:
    """Best-effort robots.txt check. On any failure we fail OPEN (allow) — the
    hard politeness guarantees are the per-page delay and stop-on-403/429."""
    try:
        rp = _robots_cache.get(BASE_URL)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(urljoin(BASE_URL, "/robots.txt"))
            rp.read()
            _robots_cache[BASE_URL] = rp
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def _search_url(slug: str, deal: str, page: int) -> str:
    return urljoin(BASE_URL, SEARCH_URL_TEMPLATE.format(deal=deal, type=TYPE, slug=slug, page=page))


class _Blocked(Exception):
    """Raised on 403/429 so the sweep stops immediately (block-avoidance)."""


def _fetch_page(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=REQUEST_TIMEOUT_S)
    if resp.status_code in (403, 429):
        retry_after = resp.headers.get("Retry-After", "?")
        raise _Blocked(f"{resp.status_code} on {url} (Retry-After={retry_after})")
    resp.raise_for_status()
    return resp.text


def _sleep_jitter() -> None:
    time.sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))


# ---------------------------------------------------------------------------
# Parse layer  (UNCONFIRMED — Task 0). Keep all site-shape assumptions in here.
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NEXT_F_RE = re.compile(r'self\.__next_f\.push\(\[\d+,\s*(".*?")\]\)', re.DOTALL)

_LISTING_TYPES = {
    "product", "offer", "realestatelisting", "residence", "apartment",
    "singlefamilyresidence", "house", "accommodation",
}


def _extract_jsonld_blocks(html: str) -> list:
    """Parse every <script type="application/ld+json"> block. Returns parsed objects."""
    blocks = []
    for raw in _JSONLD_RE.findall(html):
        try:
            blocks.append(json.loads(raw.strip()))
        except json.JSONDecodeError:
            continue
    return blocks


def _walk_listing_nodes(node):
    """Yield dict nodes that look like a real-estate listing/offer (by @type)."""
    if isinstance(node, dict):
        t = node.get("@type")
        types = {t.lower()} if isinstance(t, str) else {x.lower() for x in t} if isinstance(t, list) else set()
        if types & _LISTING_TYPES:
            yield node
        for v in node.values():
            yield from _walk_listing_nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_listing_nodes(v)


_DIGIT_RE = re.compile(r"(\d+)")


def _derive_rooms(category: str | None) -> int | None:
    """Best-effort rooms from a Slovak category label. None when not derivable."""
    if not category:
        return None
    low = category.lower()
    if "garsón" in low or "garzón" in low:  # studio
        return 1
    m = _DIGIT_RE.search(category)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None  # 'Mezonet' etc. -> null per the contract


def _num(value) -> float | None:
    """Coerce a price/area-ish value (which may carry units/spaces) to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d\s.,]+", str(value))
    if not m:
        return None
    cleaned = m.group(0).replace("\xa0", "").replace(" ", "").replace(",", ".")
    # drop thousands dots if there are several
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_jsonld_listing(node: dict, slug: str, deal: str) -> dict | None:
    """Map one JSON-LD listing node to a (partial) bronze row.

    UNCONFIRMED key names — fix here once recon() shows the real payload.
    Returns None if no stable id can be found.
    """
    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    detail_url = node.get("url") or offers.get("url")
    detail_id = (
        str(node.get("sku") or node.get("productID") or node.get("identifier") or "").strip()
        or _id_from_url(detail_url)
    )
    if not detail_id:
        return None

    price = _num(offers.get("price") or node.get("price"))
    # area: schema.org floorSize.value, or a custom field
    floor = node.get("floorSize") or {}
    area = _num(floor.get("value") if isinstance(floor, dict) else floor)

    addr = node.get("address") or {}
    if not isinstance(addr, dict):
        addr = {}

    return {
        "detail_id": detail_id,
        "title": node.get("name"),
        "category": node.get("category") or node.get("@type") if isinstance(node.get("@type"), str) else node.get("category"),
        "area_m2": area,
        "price_eur": price,
        "region": addr.get("addressRegion"),
        "district": addr.get("addressCounty") or addr.get("addressRegion"),
        "city": addr.get("addressLocality"),
        "street": addr.get("streetAddress"),
        "valid_from": _date_or_none(offers.get("validFrom") or node.get("datePosted")),
        "detail_url": detail_url,
    }


def _id_from_url(url: str | None) -> str:
    if not url:
        return ""
    m = re.search(r"/([A-Za-z0-9]+)/?$", url.rstrip("/"))
    return m.group(1) if m else ""


def _date_or_none(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _extract_listings_from_html(html: str, slug: str, deal: str) -> tuple[list[dict], list[str]]:
    """Turn one results page into partial bronze rows. Returns (rows, warnings).

    Strategy: prefer JSON-LD (stable schema.org), which is the Task-0 target.
    The `__next_f` fallback is left as a marked TODO returning nothing until the
    payload is confirmed by recon().
    """
    warnings: list[str] = []
    rows: list[dict] = []

    for block in _extract_jsonld_blocks(html):
        for node in _walk_listing_nodes(block):
            row = _parse_jsonld_listing(node, slug, deal)
            if row:
                rows.append(row)

    if not rows:
        warnings.append(
            "no listings parsed from JSON-LD — confirm payload via recon() (Task 0); "
            "__next_f fallback not yet implemented"
        )
    return rows, warnings


# ---------------------------------------------------------------------------
# Row assembly → bronze frame
# ---------------------------------------------------------------------------

def _finalize_rows(partial: list[dict], slug: str, deal: str, scraped_at: datetime) -> pl.DataFrame:
    """Normalize partial rows to the full BRONZE_SCHEMA and compute derived fields.
    Dedups within the unit by detail_id (bronze dedups within a unit only)."""
    scraped_date = scraped_at.date()
    seen: set[str] = set()
    out: list[dict] = []
    for p in partial:
        did = p.get("detail_id")
        if not did or did in seen:
            continue
        seen.add(did)
        price = p.get("price_eur")
        area = p.get("area_m2")
        ppm2 = (price / area) if (price and area) else None
        category = p.get("category")
        out.append(
            {
                "scraped_at": scraped_at,
                "scraped_date": scraped_date,
                "type": TYPE,
                "deal": deal,
                "source_slug": slug,
                "detail_id": did,
                "title": p.get("title"),
                "category": category,
                "rooms": _derive_rooms(category),
                "area_m2": area,
                "price_eur": price,
                "price_per_m2": ppm2,
                "price_on_request": not bool(price),
                "region": p.get("region"),
                "district": p.get("district"),
                "city": p.get("city"),
                "street": p.get("street"),
                "valid_from": p.get("valid_from"),
                "detail_url": p.get("detail_url"),
            }
        )
    if not out:
        return pl.DataFrame(schema=BRONZE_SCHEMA)
    return pl.DataFrame(out, schema=BRONZE_SCHEMA)


# ---------------------------------------------------------------------------
# Public: sweep one unit
# ---------------------------------------------------------------------------

def sweep_unit(
    slug: str,
    deal: str,
    *,
    scraped_at: datetime | None = None,
    session: requests.Session | None = None,
    max_pages: int | None = None,
) -> dict:
    """Sweep one (locality, deal) unit to a bronze frame.

    Returns a dict: {df, n_pages, n_rows, cap_hit, status, error}.
    status ∈ {'done', 'blocked', 'error'}. Never raises for HTTP/parse problems —
    the caller (ledger/timer) decides retry from `status`.
    """
    if slug not in LOCALITY_SLUGS:
        return _result(pl.DataFrame(schema=BRONZE_SCHEMA), 0, False, "error", f"unknown slug {slug}")
    if deal not in DEALS:
        return _result(pl.DataFrame(schema=BRONZE_SCHEMA), 0, False, "error", f"unknown deal {deal}")

    scraped_at = scraped_at or datetime.now(timezone.utc)
    cap = max_pages or MAX_PAGES
    sess = session or _new_session()
    ua = sess.headers.get("User-Agent", DEFAULT_USER_AGENT)

    all_partial: list[dict] = []
    warnings: list[str] = []
    n_pages = 0
    cap_hit = False

    for page in range(1, cap + 1):
        url = _search_url(slug, deal, page)
        if not _robots_allowed(url, ua):
            return _result(_finalize_rows(all_partial, slug, deal, scraped_at), n_pages, False,
                           "blocked", f"robots.txt disallows {url}")
        try:
            html = _fetch_page(sess, url)
        except _Blocked as e:
            # Stop the whole sweep on a block signal; the unit stays pending and retries.
            return _result(_finalize_rows(all_partial, slug, deal, scraped_at), n_pages, cap_hit,
                           "blocked", str(e))
        except Exception as e:
            return _result(_finalize_rows(all_partial, slug, deal, scraped_at), n_pages, cap_hit,
                           "error", f"fetch {url}: {e}")

        n_pages += 1
        rows, warns = _extract_listings_from_html(html, slug, deal)
        warnings.extend(warns)
        if not rows:
            break  # empty page => past the last result page (or parser needs Task 0)
        all_partial.extend(rows)
        if len(rows) < PAGE_SIZE:
            break  # short page => last page
        if page == cap:
            cap_hit = True  # hit the page-33/~990 guard with a still-full page
        else:
            _sleep_jitter()

    df = _finalize_rows(all_partial, slug, deal, scraped_at)
    error = "; ".join(dict.fromkeys(warnings)) or None if len(df) == 0 else None
    if cap_hit:
        error = f"WARNING page cap {cap} hit (≈{cap * PAGE_SIZE} results)" + (f"; {error}" if error else "")
    return _result(df, n_pages, cap_hit, "done", error)


def _result(df, n_pages, cap_hit, status, error) -> dict:
    return {"df": df, "n_pages": n_pages, "n_rows": len(df), "cap_hit": cap_hit,
            "status": status, "error": error}


# ---------------------------------------------------------------------------
# Public: recon (Task 0) — dump the real payload structure, parse NOTHING for keeps
# ---------------------------------------------------------------------------

def recon(slug: str, deal: str, page: int = 1, *, sample_chars: int = 4000) -> dict:
    """Fetch one results page and report its structure so the parser can be fixed
    against live data. Returns counts + truncated samples (no Blob writes)."""
    sess = _new_session()
    url = _search_url(slug, deal, page)
    out: dict = {"url": url, "slug": slug, "deal": deal, "page": page}
    try:
        resp = sess.get(url, timeout=REQUEST_TIMEOUT_S)
        out["status_code"] = resp.status_code
        out["final_url"] = resp.url
        out["content_length"] = len(resp.text)
        if resp.status_code != 200:
            out["retry_after"] = resp.headers.get("Retry-After")
            return out
        html = resp.text
    except Exception as e:
        out["error"] = str(e)
        return out

    blocks = _extract_jsonld_blocks(html)
    out["jsonld_block_count"] = len(blocks)
    out["jsonld_types"] = [
        (b.get("@type") if isinstance(b, dict) else type(b).__name__) for b in blocks
    ]
    listing_nodes = [n for b in blocks for n in _walk_listing_nodes(b)]
    out["jsonld_listing_node_count"] = len(listing_nodes)
    if listing_nodes:
        out["jsonld_listing_sample"] = json.dumps(listing_nodes[0], ensure_ascii=False)[:sample_chars]
    elif blocks:
        out["jsonld_first_block_sample"] = json.dumps(blocks[0], ensure_ascii=False)[:sample_chars]

    next_f = _NEXT_F_RE.findall(html)
    out["next_f_chunk_count"] = len(next_f)
    if next_f:
        joined = "".join(json.loads(c) for c in next_f if c)
        out["next_f_length"] = len(joined)
        out["next_f_sample"] = joined[:sample_chars]

    parsed, warns = _extract_listings_from_html(html, slug, deal)
    out["parsed_row_count"] = len(parsed)
    out["parse_warnings"] = warns
    if parsed:
        out["parsed_sample"] = parsed[0]
    return out
