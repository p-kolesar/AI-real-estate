"""Sweep one (locality, deal) unit of nehnutelnosti.sk into a bronze slice.

Two layers, deliberately separated because they have very different stability:

  * Fetch layer (stable): a polite, paged HTTP session — browser headers, robots
    honored, jittered inter-page delays, stop-on-403/429 (+ Retry-After), a hard
    per-run page cap, and the page-33 / ~990-result cap guard from the charter.
    This part is correct regardless of how the site renders listings.

  * Parse layer (confirmed Task 0): turning a results page into BRONZE rows. The
    site is a Next.js app with NO server-rendered cards and NO ld+json <script>
    blocks; listings are schema.org objects embedded in the streamed `__next_f` RSC
    payload, each keyed under `"item": {...}` with a /detail/<id>/ url. Parsing is
    isolated in `_reconstruct_stream` → `_extract_listings_from_html` → `_parse_item`;
    `recon()` dumps the real payload so it can be re-fixed if the site changes.

Geo (city/district/region) is derived from the sweep's slug (the results payload
doesn't carry the listing locality) via schemas.locality_geo — each sweep targets
exactly one locality. Captures structured FACTS only; description prose is never
read (agency copyright).

Config (env, with timeout-safe defaults — see SCRAPE_* below). NOTE: host.json caps
a function execution at 5 min; the charter's 20–40 s inter-page delay only fits if
you raise functionTimeout. Defaults here (4–9 s) keep a ~33-page sweep inside 5 min.

ALL timestamps are UTC (see schemas.py).
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

from realestate.schemas import BRONZE_SCHEMA, DEALS, LOCALITY_SLUGS, TYPE, locality_geo

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
# One push() call: self.__next_f.push([<n>,"<JS-string payload>"]). The capture group
# is the JSON string literal (incl. its quotes), so json.loads decodes the escaping.
_NEXT_F_RE = re.compile(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)')
_DETAIL_ID_RE = re.compile(r"/detail/([^/]+)/")


def _extract_jsonld_blocks(html: str) -> list:
    """Parse every <script type="application/ld+json"> block. Returns parsed objects.
    (nehnutelnosti.sk renders none on results pages — kept for recon completeness.)"""
    blocks = []
    for raw in _JSONLD_RE.findall(html):
        try:
            blocks.append(json.loads(raw.strip()))
        except json.JSONDecodeError:
            continue
    return blocks


def _reconstruct_stream(html: str) -> str:
    """Reconstruct the Next.js RSC stream by decoding every __next_f push() string."""
    parts = []
    for m in _NEXT_F_RE.finditer(html):
        try:
            decoded = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, str):
            parts.append(decoded)
    return "".join(parts)


def _balanced_object(s: str, start: int) -> str | None:
    """Substring of the brace-balanced JSON object starting at `start` (a '{'), or None."""
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
    return None


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


def _date_or_none(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _parse_item(item: dict, slug: str) -> dict | None:
    """Map one schema.org listing object (from the RSC stream) to a partial bronze row.

    Geo is derived from the sweep's slug — the results-page objects don't carry the
    listing locality. Captures structured FACTS only; description prose is never read.
    Returns None if there's no /detail/ url to key on.
    """
    url = item.get("url")
    if not url or "/detail/" not in url:
        return None
    m = _DETAIL_ID_RE.search(url)
    detail_id = m.group(1) if m else None
    if not detail_id:
        return None

    price = _num((item.get("priceSpecification") or {}).get("price")) or \
        _num((item.get("offers") or {}).get("price"))
    area = _num((item.get("floorSize") or {}).get("value"))
    rooms = item.get("numberOfRooms")
    try:
        rooms = int(rooms) if rooms is not None else None
    except (TypeError, ValueError):
        rooms = None

    geo = locality_geo(slug)
    offers = item.get("offers") or {}
    return {
        "detail_id": detail_id,
        "detail_url": url,
        "title": item.get("name"),
        "category": item.get("category"),
        "rooms": rooms,
        "area_m2": area,
        "price_eur": price,
        "region": geo["region"],
        "district": geo["district"],
        "city": geo["city"],
        "street": None,  # not in the results payload; left for the GPS phases
        "valid_from": _date_or_none(offers.get("validFrom") or item.get("datePosted")),
    }


def _extract_listings_from_html(html: str, slug: str, deal: str) -> tuple[list[dict], list[str]]:
    """Turn one results page into partial bronze rows. Returns (rows, warnings).

    Listings are schema.org objects embedded in the Next.js __next_f RSC stream,
    each keyed under `"item": {...}` and carrying a /detail/<id>/ url. We reconstruct
    the stream, then balance-match each `item` object and read its structured fields.
    """
    warnings: list[str] = []
    rows: list[dict] = []
    seen: set[str] = set()

    stream = _reconstruct_stream(html)
    for m in re.finditer(r'"item"\s*:\s*\{', stream):
        brace = stream.index("{", m.end() - 1)
        obj = _balanced_object(stream, brace)
        if not obj or "/detail/" not in obj:
            continue
        try:
            item = json.loads(obj)
        except json.JSONDecodeError:
            continue
        row = _parse_item(item, slug)
        if row and row["detail_id"] not in seen:
            seen.add(row["detail_id"])
            rows.append(row)

    if not rows:
        warnings.append(
            "no listings parsed from __next_f — site payload may have changed; "
            "run recon(full=True) and re-check the `item` object shape"
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
        rooms = p.get("rooms")
        if rooms is None:
            rooms = _derive_rooms(category)  # fallback when numberOfRooms is absent
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
                "rooms": rooms,
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

# Candidate substrings that, if present in the __next_f payload, point at listing
# data. Used to locate the listing array without knowing the exact field names yet.
_RECON_MARKERS = (
    "/detail/", "detail_url", "transactionType", "advertisement", "advertismentType",
    "realEstate", "realty", "price", "totalPrice", "unitPrice", "priceInfo",
    "rooms", "roomCount", "area", "usableArea", "floorArea", "category",
    "title", "name", "locality", "gps", "latitude", "longitude", "slug", "EUR",
)


def recon(slug: str, deal: str, page: int = 1, *, sample_chars: int = 4000,
          full: bool = False) -> dict:
    """Fetch one results page and report its structure so the parser can be fixed
    against live data (no Blob writes).

    The site renders listings in the Next.js `__next_f` Flight payload (no JSON-LD),
    so this scans the joined payload for listing-data markers, returns a window
    around the first strong hit, and (with full=True) the entire joined payload.
    """
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

    next_f = _NEXT_F_RE.findall(html)
    out["next_f_chunk_count"] = len(next_f)
    joined = ""
    if next_f:
        joined = "".join(json.loads(c) for c in next_f if c)
        out["next_f_length"] = len(joined)

        # Marker scan: which listing-data tokens appear, and where first.
        markers = {}
        for m in _RECON_MARKERS:
            idx = joined.find(m)
            if idx != -1:
                markers[m] = {"count": joined.count(m), "first_index": idx}
        out["next_f_markers"] = markers

        # Window around the first strong marker so the listing-object shape is visible.
        strong = ("/detail/", "transactionType", "advertisement", "realEstate",
                  "priceInfo", "totalPrice", "usableArea")
        anchor = min((markers[m]["first_index"] for m in strong if m in markers), default=None)
        if anchor is None and markers:
            anchor = min(v["first_index"] for v in markers.values())
        if anchor is not None:
            start = max(0, anchor - 500)
            out["next_f_window"] = joined[start:start + 3000]
        out["next_f_head"] = joined[:sample_chars]

        if full:
            out["next_f_full"] = joined  # entire joined payload (~30 KB)

    parsed, warns = _extract_listings_from_html(html, slug, deal)
    out["parsed_row_count"] = len(parsed)
    out["parse_warnings"] = warns
    if parsed:
        out["parsed_sample"] = parsed[0]
    return out
