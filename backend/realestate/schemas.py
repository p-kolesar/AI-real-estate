"""Data contract for the real-estate medallion pipeline.

One source of truth for every Parquet/JSON artifact the project writes, shared by
the scraper, the silver/gold builder, the read-only API, and the agent. Defines:

  * the Blob container + path conventions (Hive-partitioned bronze, flat silver/gold),
  * the Polars schema of every table (so writers and readers can't drift),
  * the scrape enums (deals, type, locality slugs),
  * the shape of the two JSON sidecars (ingest ledger, run metadata is Parquet).

Layers (medallion):
  bronze  — one listing as seen in one sweep of one (locality, deal). Immutable,
            append-only, Hive-partitioned. Everything else is recomputable from it.
  silver  — one row per listing (detail_id): latest attributes + lifecycle + flags.
            Rebuilt each run from the full bronze history.
  gold    — analytics aggregates (segment_weekly, yield_segment). Rebuilt each run.

Polars dtypes are used directly so callers can build/validate frames with
`pl.DataFrame(schema=BRONZE_SCHEMA)` and `empty_df(BRONZE_SCHEMA)`.
"""

from __future__ import annotations

import polars as pl

# ---------------------------------------------------------------------------
# Container + scrape enums
# ---------------------------------------------------------------------------

CONTAINER = "realestate"  # single container; layers live under prefixes (see paths below)

TYPE = "byty"  # v1 scope: apartments only. Kept as a bronze partition for future types.

DEALS = ("predaj", "prenajom")  # sale / rent

# The 21 scrape localities = 17 Bratislava mestské časti (boroughs) + 4 corridor towns.
# `slug` is the nehnutelnosti.sk locality slug used in the search URL and is the unit
# of work in the ledger; `name` is the human label (and the canonical `city` value
# written to bronze — the choropleth grain); `okres` is the district (gold okres grain);
# `kind` distinguishes the choropleth boroughs from the corridor polygons.
#
# The search-results JSON-LD does NOT carry the listing locality, so city/district/region
# are derived from the sweep's slug (each sweep targets exactly one locality). Confirmed
# against live payloads in Task 0: slugs + URL template are correct; listings live in the
# __next_f RSC payload as schema.org objects (see scraper._extract_listings_from_html).
LOCALITIES: tuple[dict, ...] = (
    # --- 17 Bratislava boroughs (choropleth regions) ---
    {"slug": "bratislava-stare-mesto",          "name": "Staré Mesto",          "okres": "okres Bratislava I",   "kind": "borough"},
    {"slug": "bratislava-ruzinov",              "name": "Ružinov",              "okres": "okres Bratislava II",  "kind": "borough"},
    {"slug": "bratislava-vrakuna",              "name": "Vrakuňa",              "okres": "okres Bratislava II",  "kind": "borough"},
    {"slug": "bratislava-podunajske-biskupice", "name": "Podunajské Biskupice", "okres": "okres Bratislava II",  "kind": "borough"},
    {"slug": "bratislava-nove-mesto",           "name": "Nové Mesto",           "okres": "okres Bratislava III", "kind": "borough"},
    {"slug": "bratislava-raca",                 "name": "Rača",                 "okres": "okres Bratislava III", "kind": "borough"},
    {"slug": "bratislava-vajnory",              "name": "Vajnory",              "okres": "okres Bratislava III", "kind": "borough"},
    {"slug": "bratislava-karlova-ves",          "name": "Karlova Ves",          "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-dubravka",             "name": "Dúbravka",             "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-lamac",                "name": "Lamač",                "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-devin",                "name": "Devín",                "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-devinska-nova-ves",    "name": "Devínska Nová Ves",    "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-zahorska-bystrica",    "name": "Záhorská Bystrica",    "okres": "okres Bratislava IV",  "kind": "borough"},
    {"slug": "bratislava-petrzalka",            "name": "Petržalka",            "okres": "okres Bratislava V",   "kind": "borough"},
    {"slug": "bratislava-jarovce",              "name": "Jarovce",              "okres": "okres Bratislava V",   "kind": "borough"},
    {"slug": "bratislava-rusovce",              "name": "Rusovce",              "okres": "okres Bratislava V",   "kind": "borough"},
    {"slug": "bratislava-cunovo",               "name": "Čunovo",               "okres": "okres Bratislava V",   "kind": "borough"},
    # --- 4 corridor towns (separate polygons, not Bratislava boroughs; okres Malacky) ---
    {"slug": "stupava",  "name": "Stupava",  "okres": "okres Malacky", "kind": "corridor"},
    {"slug": "marianka", "name": "Marianka", "okres": "okres Malacky", "kind": "corridor"},
    {"slug": "borinka",  "name": "Borinka",  "okres": "okres Malacky", "kind": "corridor"},
    {"slug": "lozorno",  "name": "Lozorno",  "okres": "okres Malacky", "kind": "corridor"},
)

LOCALITY_SLUGS = tuple(loc["slug"] for loc in LOCALITIES)

# All v1 localities sit in Bratislavský kraj (incl. okres Malacky).
REGION = "Bratislavský kraj"

_LOCALITY_BY_SLUG = {loc["slug"]: loc for loc in LOCALITIES}


def locality_geo(slug: str) -> dict:
    """Geo attributes for a slug, written to bronze when the payload lacks them.
    Returns {city, district, region, kind} (empty strings for an unknown slug)."""
    loc = _LOCALITY_BY_SLUG.get(slug)
    if not loc:
        return {"city": None, "district": None, "region": REGION, "kind": None}
    return {"city": loc["name"], "district": loc["okres"], "region": REGION, "kind": loc["kind"]}

# The full daily work set is the cartesian product (locality, deal): 21 × 2 = 42 units.
def scrape_units() -> list[tuple[str, str]]:
    """All (locality_slug, deal) units a full day must cover. 42 units in v1."""
    return [(slug, deal) for slug in LOCALITY_SLUGS for deal in DEALS]


def unit_key(slug: str, deal: str) -> str:
    """Ledger key for one work unit, e.g. 'bratislava-ruzinov|predaj'."""
    return f"{slug}|{deal}"


# ---------------------------------------------------------------------------
# Blob path conventions
# ---------------------------------------------------------------------------
#
#   bronze/type=byty/deal=<deal>/date=<YYYY-MM-DD>/<slug>.parquet   (one slice per unit/day)
#   silver/listings.parquet                                          (current, rebuilt)
#   gold/segment_weekly.parquet                                      (city + okres grains)
#   gold/yield_segment.parquet
#   meta/ingest_ledger.json                                          (date -> unit -> status)
#   meta/ingest_runs.parquet                                         (one row per tick)
#   meta/quality_overrides.parquet                                   (manual flag & skip)
#   agent/agent_log.parquet                                          (one row per daily brief)
#   agent/briefs/<YYYY-MM-DD>.json                                   (full memo + selections)

def bronze_path(deal: str, date: str, slug: str) -> str:
    """Hive-partitioned bronze slice for one (locality, deal) on one day.

    `date` is an ISO string 'YYYY-MM-DD'. A re-run of the same unit overwrites this
    blob (idempotent); bronze stays one snapshot per unit per day.
    """
    return f"bronze/type={TYPE}/deal={deal}/date={date}/{slug}.parquet"


BRONZE_PREFIX = f"bronze/type={TYPE}/"  # scan-all prefix for the gold build (list + concat)

SILVER_PATH = "silver/listings.parquet"
# Gold is split by geography (city vs okres) into separate files so a query can't
# accidentally sum both grains together. City grain feeds the choropleth; okres is coarse.
GOLD_SEGMENT_WEEKLY_CITY_PATH = "gold/segment_weekly_city.parquet"
GOLD_SEGMENT_WEEKLY_OKRES_PATH = "gold/segment_weekly_okres.parquet"
GOLD_YIELD_SEGMENT_CITY_PATH = "gold/yield_segment_city.parquet"
GOLD_YIELD_SEGMENT_OKRES_PATH = "gold/yield_segment_okres.parquet"

LEDGER_PATH = "meta/ingest_ledger.json"
INGEST_RUNS_PATH = "meta/ingest_runs.parquet"
QUALITY_OVERRIDES_PATH = "meta/quality_overrides.parquet"

AGENT_LOG_PATH = "agent/agent_log.parquet"


def brief_path(date: str) -> str:
    return f"agent/briefs/{date}.json"


# ---------------------------------------------------------------------------
# BRONZE — one listing-snapshot (1 row = one listing as seen in one sweep of one unit).
# Grain/key: (detail_id, scraped_date, source_slug). Bronze dedups only WITHIN a unit
# (across its pages); the same detail_id may appear in two slices on one day if two
# locality queries surface it. GLOBAL dedup by detail_id happens at the silver rebuild
# (deterministic tie-break), because ticks 20 min apart can't see each other at write.
# Immutable. ALL data is UTC: `scraped_at` is the UTC instant, `scraped_date` is the UTC
# date. (The scrape window is daytime Bratislava ≈ 04:00–21:00 UTC, which never crosses
# UTC midnight, so the UTC date == the Bratislava date throughout a sweep — no split.)
# The front-end renders timestamps in Bratislava time; storage stays UTC.
# Description prose is NEVER stored (copyright); `street` holds only the parsed token.
# ---------------------------------------------------------------------------
BRONZE_SCHEMA: dict[str, pl.DataType] = {
    "scraped_at":       pl.Datetime(time_unit="us", time_zone="UTC"),  # run timestamp
    "scraped_date":     pl.Date,            # partition
    "type":             pl.String,          # 'byty' (partition)
    "deal":             pl.String,          # 'predaj' / 'prenajom' (partition)
    "source_slug":      pl.String,          # query locality that returned this row (provenance)
    "detail_id":        pl.String,          # stable join key
    "title":            pl.String,
    "category":         pl.String,          # e.g. '3 izbový byt', 'Garsónka', 'Mezonet'
    "rooms":            pl.Int8,            # derived from category; null for Mezonet/unknown
    "area_m2":          pl.Float64,
    "price_eur":        pl.Float64,         # null/0 -> see price_on_request
    "price_per_m2":     pl.Float64,         # price_eur / area_m2
    "price_on_request": pl.Boolean,         # true when price missing/0
    "region":           pl.String,          # kraj (from list location)
    "district":         pl.String,          # okres, e.g. 'okres Bratislava II'
    "city":             pl.String,          # mestská časť / borough, e.g. 'Bratislava-Ružinov'
    "street":           pl.String,          # parsed token only, nullable (best-effort)
    "valid_from":       pl.Date,            # JSON-LD validFrom, nullable
    "detail_url":       pl.String,
}

# ---------------------------------------------------------------------------
# SILVER — one row per listing (detail_id): latest attributes + lifecycle + flags.
# Rebuilt each run from full bronze history. Lifecycle is intentionally minimal in v1
# (first/last seen + active); valid_to / days-on-market / price history are deferred.
# ---------------------------------------------------------------------------
SILVER_SCHEMA: dict[str, pl.DataType] = {
    # --- identity + latest attributes (from the most recent snapshot of this id) ---
    "detail_id":        pl.String,          # key
    "type":             pl.String,
    "deal":             pl.String,
    "title":            pl.String,
    "category":         pl.String,
    "rooms":            pl.Int8,
    "area_m2":          pl.Float64,
    "price_eur":        pl.Float64,
    "price_per_m2":     pl.Float64,
    "price_on_request": pl.Boolean,
    "region":           pl.String,
    "district":         pl.String,
    "city":             pl.String,
    "street":           pl.String,
    "valid_from":       pl.Date,
    "detail_url":       pl.String,
    # --- lifecycle (from snapshot diff) ---
    "first_seen_date":  pl.Date,
    "last_seen_date":   pl.Date,
    "is_active":        pl.Boolean,         # present in the latest sweep
    # --- quality (free parse-time flags + manual overrides) ---
    "quality_flags":    pl.List(pl.String),  # e.g. ['price_on_request', 'area_missing']
    "is_excluded":      pl.Boolean,         # any blocking flag -> filtered from gold
}

# Free parse-time flags (no detection logic — they fall out of the data).
FREE_FLAGS = ("price_on_request", "area_missing", "street_unparsed")
# Which flags are blocking (set is_excluded). Free flags are informational by default;
# exclusion in v1 comes from manual quality_overrides.
BLOCKING_FLAGS: tuple[str, ...] = ()

# ---------------------------------------------------------------------------
# GOLD — segment_weekly. Split into two files by geography; the geo key column is
# typed per file: `city` (borough/corridor → choropleth) or `district` (okres → coarse).
# Excludes is_excluded rows; segments below min sample size flagged low_confidence.
# Per-category only (no 'all categories' rollup) — callers pick a category.
# ---------------------------------------------------------------------------
def _segment_weekly_schema(geo_col: str) -> dict[str, pl.DataType]:
    return {
        "week":           pl.Date,        # Monday of the ISO week (of the UTC scraped_date)
        geo_col:          pl.String,      # 'city' or 'district' label per file
        "type":           pl.String,
        "deal":           pl.String,
        "category":       pl.String,
        "median_ppm2":    pl.Float64,
        "p25_ppm2":       pl.Float64,
        "p75_ppm2":       pl.Float64,
        "listing_count":  pl.Int32,       # distinct ACTIVE listings observed in the week
        "new_count":      pl.Int32,
        "removed_count":  pl.Int32,
        "median_price":   pl.Float64,
        "median_area":    pl.Float64,
        "ppm2_wow_pct":   pl.Float64,     # null in the first week (no prior) — never 0
        "ppm2_mom_pct":   pl.Float64,     # null until 4 weeks of history exist
        "low_confidence": pl.Boolean,     # listing_count < min sample threshold
    }


SEGMENT_WEEKLY_CITY_SCHEMA: dict[str, pl.DataType] = _segment_weekly_schema("city")
SEGMENT_WEEKLY_OKRES_SCHEMA: dict[str, pl.DataType] = _segment_weekly_schema("district")

# ---------------------------------------------------------------------------
# GOLD — yield_segment. Same two-file split + typed geo column. Combines buy (predaj)
# ppm² with rent (prenajom) monthly ppm² for the same area×category; deal is NOT a
# dimension. gross_yield_pct = rent_median_ppm2_monthly * 12 / buy_median_ppm2 * 100.
# A ratio of two independent medians from different populations — low_confidence is
# load-bearing here, not optional.
# ---------------------------------------------------------------------------
def _yield_segment_schema(geo_col: str) -> dict[str, pl.DataType]:
    return {
        "week":                     pl.Date,
        geo_col:                    pl.String,
        "type":                     pl.String,
        "category":                 pl.String,
        "buy_median_ppm2":          pl.Float64,
        "rent_median_ppm2_monthly": pl.Float64,
        "gross_yield_pct":          pl.Float64,
        "buy_sample":               pl.Int32,   # n sale listings behind buy_median_ppm2
        "rent_sample":              pl.Int32,   # n rent listings behind rent_median_ppm2
        "low_confidence":           pl.Boolean,  # either sample below threshold
    }


YIELD_SEGMENT_CITY_SCHEMA: dict[str, pl.DataType] = _yield_segment_schema("city")
YIELD_SEGMENT_OKRES_SCHEMA: dict[str, pl.DataType] = _yield_segment_schema("district")

# ---------------------------------------------------------------------------
# META — quality_overrides (manual flag & skip; honored immediately, agent cannot write)
# ---------------------------------------------------------------------------
QUALITY_OVERRIDES_SCHEMA: dict[str, pl.DataType] = {
    "detail_id": pl.String,
    "flag":      pl.String,
    "reason":    pl.String,
    "added_by":  pl.String,
    "added_at":  pl.Datetime(time_unit="us", time_zone="UTC"),
}

# ---------------------------------------------------------------------------
# META — ingest_runs (one row per 20-min tick: the per-unit ingestion log).
# n_new / n_removed are NOT here — they're computed at the daily silver/gold diff.
# ---------------------------------------------------------------------------
INGEST_RUNS_SCHEMA: dict[str, pl.DataType] = {
    "run_id":       pl.String,
    "scraped_date": pl.Date,
    "started_at":   pl.Datetime(time_unit="us", time_zone="UTC"),
    "finished_at":  pl.Datetime(time_unit="us", time_zone="UTC"),
    "source_slug":  pl.String,
    "deal":         pl.String,
    "n_pages":      pl.Int32,
    "n_rows":       pl.Int32,
    "cap_hit":      pl.Boolean,     # hit the page-33 / ~990 pagination cap
    "status":       pl.String,      # 'done' | 'blocked' | 'error'
    "error":        pl.String,      # nullable
}

# ---------------------------------------------------------------------------
# AGENT — agent_log (one row per daily brief). Adapted from the portfolio loop:
# no trades; records which segments were deep-dived plus tokens/cost/memo.
# ---------------------------------------------------------------------------
AGENT_LOG_SCHEMA: dict[str, pl.DataType] = {
    "run_date":                 pl.Date,
    "screening_input_tokens":   pl.Int64,
    "screening_output_tokens":  pl.Int64,
    "deepdive_input_tokens":    pl.Int64,
    "deepdive_output_tokens":   pl.Int64,
    "total_tokens":             pl.Int64,
    "estimated_cost_usd":       pl.Float64,
    "selected_segments":        pl.List(pl.String),  # segments the brief deep-dived
    "status":                   pl.String,           # 'ok' | 'blocked' | 'disabled'
    "memo":                     pl.String,
}


# ---------------------------------------------------------------------------
# Ledger JSON shape (meta/ingest_ledger.json) — documented, not a Parquet schema.
#
#   {
#     "2026-06-04": {
#       "bratislava-ruzinov|predaj":  {"status": "done", "scraped_at": "...Z",
#                                       "n_listings": 812, "n_pages": 28, "cap_hit": false},
#       "bratislava-ruzinov|prenajom": {"status": "pending"},
#       ...   # 42 unit keys
#     }
#   }
#
# status ∈ {'pending', 'done', 'error'}. The top-level date key is the UTC date. No tick
# fires between 22:00 and 06:00 Bratislava (spanning both midnights), so the next active
# window simply opens on a fresh UTC date key = the daily reset. Failed/pending units
# retry next tick; coverage_pct for a day = (#done) / 42.
# NOTE: the timer SCHEDULE fires on the Bratislava daytime window (politeness); the
# day-KEY stored here is UTC. The two never conflict because the window excludes midnight.
# ---------------------------------------------------------------------------
LEDGER_UNIT_STATUSES = ("pending", "done", "error")


def empty_df(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """An empty Polars frame with the given schema — for initializing blobs."""
    return pl.DataFrame(schema=schema)
