"""Read-only query layer over silver/gold — the single data seam.

Both the HTTP API (function_app.py) and the agent tools (agent/tools.py) go through
here, so query semantics can't drift between the SPA and the brief. Every function
degrades gracefully: if a blob doesn't exist yet (fresh deploy, no data), it returns
an empty list / sane defaults rather than raising — the API stays 200 from day one.

All arithmetic already happened in build.py; this layer only filters, sorts, and
bounds. Output is always JSON-serializable and row-capped.
"""

from __future__ import annotations

import polars as pl

from realestate.schemas import (
    CONTAINER,
    DEALS,
    GOLD_SEGMENT_WEEKLY_CITY_PATH,
    GOLD_SEGMENT_WEEKLY_OKRES_PATH,
    GOLD_YIELD_SEGMENT_CITY_PATH,
    GOLD_YIELD_SEGMENT_OKRES_PATH,
    LOCALITIES,
    SILVER_PATH,
    brief_path,
)
from storage.blobs import blob_exists, read_json, read_parquet

DEFAULT_ROW_CAP = 2000
LISTINGS_CAP = 150  # agent query_listings / API listings hard cap


def _grain_geo_col(grain: str) -> str:
    return "city" if grain == "city" else "district"


def _segment_path(grain: str) -> str:
    return GOLD_SEGMENT_WEEKLY_CITY_PATH if grain == "city" else GOLD_SEGMENT_WEEKLY_OKRES_PATH


def _yield_path(grain: str) -> str:
    return GOLD_YIELD_SEGMENT_CITY_PATH if grain == "city" else GOLD_YIELD_SEGMENT_OKRES_PATH


def _safe_read(path: str) -> pl.DataFrame | None:
    if not blob_exists(CONTAINER, path):
        return None
    try:
        return read_parquet(CONTAINER, path)
    except Exception:
        return None


def _rows(df: pl.DataFrame | None, cap: int = DEFAULT_ROW_CAP) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    return df.head(cap).to_dicts()


def _apply_filters(df: pl.DataFrame, *, geo_col: str, area=None, category=None,
                   deal=None, week=None) -> pl.DataFrame:
    if area:
        df = df.filter(pl.col(geo_col) == area)
    if category:
        df = df.filter(pl.col("category") == category)
    if deal and "deal" in df.columns:
        df = df.filter(pl.col("deal") == deal)
    if week:
        df = df.filter(pl.col("week").cast(pl.Utf8) == str(week))
    return df


# ---------------------------------------------------------------------------
# Bootstrap / enums / coverage
# ---------------------------------------------------------------------------

def bootstrap() -> dict:
    """Enum lists, available weeks, coverage, data presence — drives SPA init."""
    seg = _safe_read(_segment_path("city"))
    silver = _safe_read(SILVER_PATH)
    weeks, categories = [], []
    if seg is not None and len(seg):
        weeks = sorted({str(w) for w in seg["week"].to_list()})
        categories = sorted({c for c in seg["category"].to_list() if c})
    elif silver is not None and len(silver):
        categories = sorted({c for c in silver["category"].to_list() if c})

    coverage = _coverage()
    return {
        "deals": list(DEALS),
        "grains": ["city", "okres"],
        "categories": categories,
        "localities": [{"slug": l["slug"], "name": l["name"], "kind": l["kind"]} for l in LOCALITIES],
        "weeks": weeks,
        "latest_week": weeks[-1] if weeks else None,
        "coverage": coverage,
        "silver_listings": 0 if silver is None else len(silver),
        "has_data": bool(weeks),
    }


def _coverage() -> dict:
    """Today's ingestion coverage from the ledger (best-effort)."""
    from realestate.schemas import LEDGER_PATH  # local import avoids cycle at module load
    ledger = read_json(CONTAINER, LEDGER_PATH, default={}) or {}
    if not ledger:
        return {"date": None, "coverage_pct": 0.0, "done": 0, "total": 0}
    date = max(ledger.keys())
    day = ledger.get(date, {})
    done = sum(1 for u in day.values() if u.get("status") == "done")
    return {"date": date, "coverage_pct": round(100.0 * done / len(day), 1) if day else 0.0,
            "done": done, "total": len(day)}


# ---------------------------------------------------------------------------
# Gold reads (HTTP + agent share these)
# ---------------------------------------------------------------------------

def segments(grain="city", area=None, category=None, deal=None, week=None,
             limit=DEFAULT_ROW_CAP) -> list[dict]:
    """segment_weekly rows for a grain, optionally filtered."""
    df = _safe_read(_segment_path(grain))
    if df is None:
        return []
    df = _apply_filters(df, geo_col=_grain_geo_col(grain), area=area, category=category,
                        deal=deal, week=week).sort("week")
    return _rows(df, limit)


def yield_segments(grain="city", area=None, category=None, week=None,
                   limit=DEFAULT_ROW_CAP) -> list[dict]:
    """yield_segment rows for a grain, optionally filtered."""
    df = _safe_read(_yield_path(grain))
    if df is None:
        return []
    df = _apply_filters(df, geo_col=_grain_geo_col(grain), area=area, category=category,
                        week=week).sort("week")
    return _rows(df, limit)


def trend(grain="city", area=None, category=None, deal=None, metric="median_ppm2") -> dict:
    """Time-series of one metric for a segment (area × category × deal)."""
    df = _safe_read(_segment_path(grain))
    geo_col = _grain_geo_col(grain)
    if df is None or metric not in (df.columns if df is not None else []):
        return {"grain": grain, "area": area, "category": category, "deal": deal,
                "metric": metric, "series": []}
    df = _apply_filters(df, geo_col=geo_col, area=area, category=category, deal=deal).sort("week")
    series = [
        {"week": str(r["week"]), "value": r.get(metric),
         "listing_count": r.get("listing_count"), "low_confidence": r.get("low_confidence")}
        for r in df.to_dicts()
    ]
    return {"grain": grain, "area": area, "category": category, "deal": deal,
            "metric": metric, "series": series}


def geo(metric="median_ppm2", deal="predaj", category=None, week=None) -> dict:
    """Per-area metric values at city grain + borough GeoJSON if present in Blob.

    The choropleth join (boundaries) is done client-side or here if a
    `geo/boroughs.geojson` blob has been uploaded; otherwise just the values ship.
    """
    df = _safe_read(_segment_path("city"))
    if df is None:
        return {"metric": metric, "week": week, "areas": [], "geojson": None}
    if week is None and len(df):
        week = str(df["week"].max())
    df = _apply_filters(df, geo_col="city", category=category, deal=deal, week=week)
    # collapse to one value per area (median across categories if category not pinned)
    if metric in df.columns and len(df):
        agg = df.group_by("city").agg(
            pl.col(metric).median().alias("value"),
            pl.col("listing_count").sum().alias("listing_count"),
            pl.col("low_confidence").all().alias("low_confidence"),
        )
        areas = agg.to_dicts()
    else:
        areas = []
    geojson = read_json(CONTAINER, "geo/boroughs.geojson", default=None)
    return {"metric": metric, "deal": deal, "category": category, "week": week,
            "areas": areas, "geojson": geojson}


# ---------------------------------------------------------------------------
# Silver reads (listings)
# ---------------------------------------------------------------------------

def query_listings(grain="city", area=None, category=None, deal=None,
                   active_only=True, limit=LISTINGS_CAP) -> list[dict]:
    """Capped listing rows from silver (UC3/UC5 drill-down + agent query_listings)."""
    df = _safe_read(SILVER_PATH)
    if df is None:
        return []
    geo_col = _grain_geo_col(grain)
    if active_only and "is_active" in df.columns:
        df = df.filter(pl.col("is_active"))
    df = df.filter(~pl.col("is_excluded")) if "is_excluded" in df.columns else df
    df = _apply_filters(df, geo_col=geo_col, area=area, category=category, deal=deal)
    cols = [c for c in ("detail_id", "title", "category", "rooms", "area_m2", "price_eur",
                        "price_per_m2", "city", "district", "deal", "first_seen_date",
                        "last_seen_date", "is_active", "detail_url") if c in df.columns]
    return _rows(df.select(cols).sort("price_per_m2", nulls_last=True), min(limit, LISTINGS_CAP))


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def get_brief(date: str | None = None) -> dict | None:
    """Latest brief, or a specific date. None if none exist yet."""
    from storage.blobs import list_blobs
    if date:
        return read_json(CONTAINER, brief_path(date), default=None)
    briefs = sorted(n for n in list_blobs(CONTAINER, "agent/briefs/") if n.endswith(".json"))
    if not briefs:
        return None
    return read_json(CONTAINER, briefs[-1], default=None)


# ---------------------------------------------------------------------------
# Agent-facing compact helpers
# ---------------------------------------------------------------------------

def segment_stats(grain="city", area=None, category=None, deal=None, week=None) -> list[dict]:
    """Compact segment_weekly slice for the agent's deep-dive (a few key columns)."""
    rows = segments(grain=grain, area=area, category=category, deal=deal, week=week, limit=200)
    keep = ("week", "city", "district", "category", "deal", "median_ppm2", "p25_ppm2",
            "p75_ppm2", "listing_count", "new_count", "removed_count", "ppm2_wow_pct",
            "ppm2_mom_pct", "low_confidence")
    return [{k: r[k] for k in keep if k in r} for r in rows]


def latest_segment_table(grain="city", max_rows=60) -> list[dict]:
    """Top segments (by listing_count) in the latest week — the agent screening input."""
    df = _safe_read(_segment_path(grain))
    if df is None or len(df) == 0:
        return []
    latest = df["week"].max()
    df = df.filter(pl.col("week") == latest).sort("listing_count", descending=True).head(max_rows)
    geo_col = _grain_geo_col(grain)
    keep = ("week", geo_col, "category", "deal", "median_ppm2", "listing_count",
            "ppm2_wow_pct", "low_confidence")
    return [{k: r[k] for k in keep if k in r} for r in df.to_dicts()]
