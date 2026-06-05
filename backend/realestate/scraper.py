"""Real-estate scraper helpers for the /scrape-realestate route.

nehnutelnosti.sk is a Next.js app: the result pages render listings
client-side, so there are no server-rendered <a href="/detail/…"> cards to
scrape. The listing data is, however, embedded in the page as schema.org
JSON-LD (an ItemList of Product/Offer objects) inside the streamed
`self.__next_f.push([...])` RSC payload. `_parse` reconstructs that payload and
reads the structured fields directly — far more robust than DOM/text scraping.

Captures structured FACTS only (price, area, rooms, title, url) — not the
listing description prose, which is the agencies' copyright.

Storage config (any one of):
  DATAIN_STORAGE                    -> a full storage connection string, OR
  AzureWebJobsStorage               -> reused if it's a connection string, OR
  AzureWebJobsStorage__accountName  -> managed-identity path (Flex default)
"""

import csv
import io
import json
import os
import re
import time

import polars as pl
import requests
from azure.storage.blob import BlobServiceClient

from realestate.schemas import locality_geo

UA = "workshop-realestate-probe/0.1 (educational; contact: you@example.com)"
CONTAINER = "datain"
BASE = "https://www.nehnutelnosti.sk"

# One push() call: self.__next_f.push([<n>,"<JS-string payload>"]). The payload
# is a JSON string literal, so json.loads decodes the escaping for us.
RE_PUSH = re.compile(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)')
RE_DETAIL_ID = re.compile(r"/detail/([^/]+)/")
RE_ROOMS = re.compile(r"(\d+)\s*izb")  # "3 izbový byt" -> 3 (advertised room count)


def _robots_allows(url: str) -> bool:
    from urllib import robotparser
    from urllib.parse import urlparse

    p = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(UA, url)
    except Exception:
        return True  # if robots is unreadable, proceed cautiously (low volume)


def _balanced_object(s: str, start: int) -> str | None:
    """Return the substring of the brace-balanced JSON object starting at `start`
    (which must index a '{'), or None if unbalanced."""
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : j + 1]
    return None


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse(html: str, base_url: str):
    """Extract listing rows from the embedded schema.org JSON-LD payload.

    `base_url` is accepted for signature compatibility with the route; listing
    URLs in the payload are already absolute.
    """
    # 1) Reconstruct the RSC stream by decoding each push() string argument.
    parts = []
    for m in RE_PUSH.finditer(html):
        decoded = _safe_load(m.group(1))
        if isinstance(decoded, str):
            parts.append(decoded)
    stream = "".join(parts)

    # 2) Pull out each schema.org product object (keyed under "item": {...}).
    out, seen = [], set()
    for m in re.finditer(r'"item"\s*:\s*\{', stream):
        brace = stream.index("{", m.end() - 1)
        obj = _balanced_object(stream, brace)
        if not obj or "/detail/" not in obj:
            continue
        try:
            item = json.loads(obj)
        except json.JSONDecodeError:
            continue

        url = item.get("url")
        if not url or "/detail/" not in url:
            continue
        mid = RE_DETAIL_ID.search(url)
        detail_id = mid.group(1) if mid else None
        if detail_id in seen:
            continue
        seen.add(detail_id)

        price = _num((item.get("priceSpecification") or {}).get("price")) or _num(
            (item.get("offers") or {}).get("price")
        )
        area = _num((item.get("floorSize") or {}).get("value"))
        ppm2 = round(price / area, 2) if price and area else None

        out.append(
            {
                "detail_id": detail_id,
                "detail_url": url,
                "title": item.get("name"),
                "category": item.get("category"),
                "rooms": item.get("numberOfRooms"),
                "area_m2": area,
                "price_eur": price,
                "price_per_m2": ppm2,
                "price_on_request": price is None,
            }
        )
    return out


def _safe_load(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


SWEEP_COLS = [
    "detail_id", "detail_url", "title", "category", "rooms",
    "area_m2", "price_eur", "price_per_m2", "price_on_request",
    "deal", "locality", "city", "district", "region", "valid_from",
]


def _results_url(locality: str, deal: str, ptype: str, page: int) -> str:
    """Build a results URL. Template per recon finding #1:
    /vysledky/<deal>/<type>/<locality>/  (page 1), then ?p=<n>."""
    base = f"{BASE}/vysledky/{deal}/{ptype}/{locality}/"
    return base if page <= 1 else f"{base}?p={page}"


def sweep_unit(locality: str, deal: str, max_pages: int = 1,
               ptype: str = "byty", **kwargs) -> dict:
    """Sweep one (locality, deal) unit. Delegates to the browser scraper — the
    HTTP path below is dead (the page renders client-side behind F5; see
    RECON_FINDINGS.md). Kept for the same {df, status, n_pages, error} contract."""
    from realestate.browser_scraper import sweep_unit as _browser_sweep
    return _browser_sweep(locality, deal, max_pages=max_pages, ptype=ptype, **kwargs)


def _sweep_unit_http_DEAD(locality: str, deal: str, max_pages: int = 1,
                          ptype: str = "byty", page_pause_s: float = 1.0) -> dict:
    """DEAD: plain-HTTP fetch+parse. The results page carries no listings (client-
    rendered behind F5), so this always yields 0 rows. Retained only as a record of
    the ruled-out approach; `sweep_unit` above delegates to the browser scraper."""
    geo = locality_geo(locality)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows: list[dict] = []
    n_pages = 0
    status, error = "done", None

    session = requests.Session()
    try:
        for page in range(1, max_pages + 1):
            url = _results_url(locality, deal, ptype, page)
            if page == 1 and not _robots_allows(url):
                status, error = "blocked", f"robots.txt disallows {url}"
                break
            r = session.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "sk,en;q=0.8"},
                timeout=20,
            )
            if r.status_code in (401, 403, 429):
                status = "blocked"
                error = f"HTTP {r.status_code} on {r.url} (auth/rate guard)"
                break
            r.raise_for_status()
            n_pages += 1

            parsed = _parse(r.text, url)
            for rec in parsed:
                rec.update({
                    "deal": deal, "locality": locality,
                    "city": geo["city"], "district": geo["district"],
                    "region": geo["region"], "valid_from": ts,
                })
                rows.append(rec)

            if not parsed:  # nothing on this page -> no point paging further
                break
            if page < max_pages:
                time.sleep(page_pause_s)
    except Exception as e:  # network/parse failure
        status = "error"
        error = f"{type(e).__name__}: {e}"

    df = pl.DataFrame(rows, schema={c: None for c in SWEEP_COLS}) if rows \
        else pl.DataFrame({c: [] for c in SWEEP_COLS})
    return {"df": df, "status": status, "n_pages": n_pages, "error": error}


def _blob_service() -> BlobServiceClient:
    conn = os.environ.get("DATAIN_STORAGE")
    if conn:
        return BlobServiceClient.from_connection_string(conn)
    awjs = os.environ.get("AzureWebJobsStorage", "")
    if "AccountKey=" in awjs or "DefaultEndpointsProtocol=" in awjs:
        return BlobServiceClient.from_connection_string(awjs)
    # managed-identity path (typical on Flex Consumption)
    acct = os.environ.get("AzureWebJobsStorage__accountName") or os.environ.get("DATAIN_ACCOUNT_NAME")
    uri = os.environ.get("AzureWebJobsStorage__blobServiceUri") or (
        f"https://{acct}.blob.core.windows.net" if acct else None)
    if not uri:
        raise RuntimeError("No storage config: set DATAIN_STORAGE or AzureWebJobsStorage__accountName.")
    from azure.identity import DefaultAzureCredential
    return BlobServiceClient(account_url=uri, credential=DefaultAzureCredential())


def _write_csv(rows, ts) -> str:
    cols = ["scraped_at", "locality", "deal", "detail_id", "title", "category",
            "rooms", "area_m2", "price_eur", "price_per_m2", "price_on_request", "detail_url"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    name = f"nehnutelnosti/{ts.replace(':', '').replace('-', '')}.csv"
    svc = _blob_service()
    cc = svc.get_container_client(CONTAINER)
    try:
        cc.create_container()
    except Exception:
        pass  # already exists
    cc.upload_blob(name=name, data=buf.getvalue().encode("utf-8"), overwrite=True)
    return name


def _coverage(rows):
    n = len(rows) or 1
    def pct(k):
        return round(sum(1 for r in rows if r.get(k) is not None) / n, 2)
    return {"rows": len(rows), "price_eur": pct("price_eur"),
            "area_m2": pct("area_m2"), "price_per_m2": pct("price_per_m2"),
            "rooms": pct("rooms")}
