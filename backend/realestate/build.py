"""Silver rebuild + gold build — the daily analytics recompute.

Everything here is deterministic Polars over the immutable bronze history; it is
rebuilt from scratch each run (no incremental state), so a fixed bronze always
yields the same silver/gold. Called by the `build_and_brief` daily timer.

  silver  — one row per listing (detail_id): latest attributes (most-recent snapshot)
            + lifecycle (first/last seen, is_active) + quality flags + exclusion.
            Global dedup by detail_id happens HERE.
  gold    — segment_weekly + yield_segment, each split into city / okres files.
            Built from bronze weekly observations, excluding silver-flagged ids.

Weeks are the Monday of the ISO week of the (UTC) scraped_date. WoW/MoM deltas are
null when the comparison week is absent — never 0 (so the UI shows "unavailable").
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl

from realestate.schemas import (
    BRONZE_PREFIX,
    CONTAINER,
    GOLD_SEGMENT_WEEKLY_CITY_PATH,
    GOLD_SEGMENT_WEEKLY_OKRES_PATH,
    GOLD_YIELD_SEGMENT_CITY_PATH,
    GOLD_YIELD_SEGMENT_OKRES_PATH,
    QUALITY_OVERRIDES_PATH,
    SEGMENT_WEEKLY_CITY_SCHEMA,
    SILVER_PATH,
    SILVER_SCHEMA,
    TYPE,
    YIELD_SEGMENT_CITY_SCHEMA,
    empty_df,
)
from storage.blobs import blob_exists, read_parquet, read_parquet_dataset, write_parquet

# Minimum distinct listings before a segment/yield row is trusted (else low_confidence).
MIN_SAMPLE_SEGMENT = 5
MIN_SAMPLE_YIELD = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monday(col: str) -> pl.Expr:
    """Monday of the ISO week containing the date in `col` (weekday: Mon=1..Sun=7)."""
    return (pl.col(col) - pl.duration(days=pl.col(col).dt.weekday() - 1)).alias("week")


def _excluded_ids(silver: pl.DataFrame) -> pl.Series:
    return silver.filter(pl.col("is_excluded"))["detail_id"]


def _overrides() -> pl.DataFrame | None:
    if blob_exists(CONTAINER, QUALITY_OVERRIDES_PATH):
        try:
            return read_parquet(CONTAINER, QUALITY_OVERRIDES_PATH)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Silver
# ---------------------------------------------------------------------------

def rebuild_silver() -> dict:
    """Rebuild silver/listings.parquet from full bronze history. Returns a summary."""
    bronze = read_parquet_dataset(CONTAINER, BRONZE_PREFIX)
    if bronze is None or len(bronze) == 0:
        write_parquet(CONTAINER, SILVER_PATH, empty_df(SILVER_SCHEMA))
        return {"status": "empty", "listings": 0}

    latest_date = bronze["scraped_date"].max()

    # Latest snapshot per listing (deterministic tie-break: scraped_at, then source_slug).
    latest = (
        bronze.sort(["detail_id", "scraped_at", "source_slug"])
        .unique(subset="detail_id", keep="last")
    )

    # Lifecycle from the full history.
    lifecycle = bronze.group_by("detail_id").agg(
        pl.col("scraped_date").min().alias("first_seen_date"),
        pl.col("scraped_date").max().alias("last_seen_date"),
    )

    silver = latest.join(lifecycle, on="detail_id", how="left").with_columns(
        is_active=(pl.col("last_seen_date") == latest_date),
    )

    # Free parse-time quality flags (no detection logic — they fall out of the data).
    silver = silver.with_columns(
        pl.concat_list(
            pl.when(pl.col("price_on_request")).then(pl.lit("price_on_request")),
            pl.when(pl.col("area_m2").is_null() | (pl.col("area_m2") <= 0)).then(pl.lit("area_missing")),
            pl.when(pl.col("street").is_null()).then(pl.lit("street_unparsed")),
        ).list.drop_nulls().alias("quality_flags")
    )

    # Exclusion: manual quality_overrides (flag & skip), honored immediately.
    ov = _overrides()
    excluded = set(ov["detail_id"].to_list()) if ov is not None and len(ov) else set()
    silver = silver.with_columns(
        pl.col("detail_id").is_in(list(excluded)).alias("is_excluded")
        if excluded else pl.lit(False).alias("is_excluded")
    )

    # Project to the silver contract (drops bronze-only partition/provenance cols).
    silver = silver.select([c for c in SILVER_SCHEMA]).cast(SILVER_SCHEMA)  # type: ignore[arg-type]
    write_parquet(CONTAINER, SILVER_PATH, silver)
    return {
        "status": "ok",
        "listings": len(silver),
        "active": int(silver["is_active"].sum()),
        "excluded": int(silver["is_excluded"].sum()),
        "latest_date": str(latest_date),
    }


# ---------------------------------------------------------------------------
# Gold
# ---------------------------------------------------------------------------

def _weekly_snapshots(bronze: pl.DataFrame, silver: pl.DataFrame, geo_col: str) -> pl.DataFrame:
    """Each listing counted once per week (latest snapshot that week), bad ids excluded,
    null geo dropped. Carries price_per_m2/price/area for aggregation."""
    excluded = _excluded_ids(silver)
    return (
        bronze.with_columns(_monday("scraped_date"))
        .filter(~pl.col("detail_id").is_in(excluded))
        .filter(pl.col(geo_col).is_not_null())
        .sort(["detail_id", "week", "scraped_at", "source_slug"])
        .unique(subset=["detail_id", "week"], keep="last")
    )


def _segment_weekly(weekly: pl.DataFrame, silver: pl.DataFrame, geo_col: str) -> pl.DataFrame:
    group = ["week", geo_col, "type", "deal", "category"]

    stats = weekly.group_by(group).agg(
        pl.col("price_per_m2").median().alias("median_ppm2"),
        pl.col("price_per_m2").quantile(0.25, "linear").alias("p25_ppm2"),
        pl.col("price_per_m2").quantile(0.75, "linear").alias("p75_ppm2"),
        pl.col("detail_id").n_unique().alias("listing_count"),
        pl.col("price_eur").median().alias("median_price"),
        pl.col("area_m2").median().alias("median_area"),
    )

    # new / removed from silver lifecycle, bucketed to the week of first/last seen.
    new_counts = (
        silver.filter(pl.col(geo_col).is_not_null())
        .with_columns(_monday("first_seen_date"))
        .group_by(["week", geo_col, "type", "deal", "category"])
        .agg(pl.col("detail_id").n_unique().alias("new_count"))
    )
    removed_counts = (
        silver.filter(pl.col(geo_col).is_not_null() & ~pl.col("is_active"))
        .with_columns(_monday("last_seen_date"))
        .group_by(["week", geo_col, "type", "deal", "category"])
        .agg(pl.col("detail_id").n_unique().alias("removed_count"))
    )

    seg = (
        stats.join(new_counts, on=group, how="left")
        .join(removed_counts, on=group, how="left")
        .with_columns(
            pl.col("new_count").fill_null(0),
            pl.col("removed_count").fill_null(0),
        )
        .sort([geo_col, "type", "deal", "category", "week"])
    )

    # WoW / MoM on median_ppm2 within a segment, by actual week distance (null if the
    # comparison week is absent — never a fake 0).
    seg = seg.with_columns(
        ((pl.col("median_ppm2") / pl.col("median_ppm2").shift(1) - 1) * 100)
        .over([geo_col, "type", "deal", "category"]).alias("ppm2_wow_pct"),
        ((pl.col("median_ppm2") / pl.col("median_ppm2").shift(4) - 1) * 100)
        .over([geo_col, "type", "deal", "category"]).alias("ppm2_mom_pct"),
    )
    # Only valid when the prior/4-weeks-ago row is exactly 7/28 days earlier.
    week_gap1 = (pl.col("week") - pl.col("week").shift(1)).over([geo_col, "type", "deal", "category"])
    week_gap4 = (pl.col("week") - pl.col("week").shift(4)).over([geo_col, "type", "deal", "category"])
    seg = seg.with_columns(
        pl.when(week_gap1 == pl.duration(days=7)).then(pl.col("ppm2_wow_pct")).otherwise(None).alias("ppm2_wow_pct"),
        pl.when(week_gap4 == pl.duration(days=28)).then(pl.col("ppm2_mom_pct")).otherwise(None).alias("ppm2_mom_pct"),
    )

    seg = seg.with_columns((pl.col("listing_count") < MIN_SAMPLE_SEGMENT).alias("low_confidence"))
    schema = {**SEGMENT_WEEKLY_CITY_SCHEMA}
    if geo_col != "city":
        schema = {("district" if k == "city" else k): v for k, v in schema.items()}
    return seg.select(list(schema.keys())).cast(schema)  # type: ignore[arg-type]


def _yield_segment(weekly: pl.DataFrame, geo_col: str) -> pl.DataFrame:
    group = ["week", geo_col, "type", "category"]
    per_deal = weekly.group_by(["week", geo_col, "type", "category", "deal"]).agg(
        pl.col("price_per_m2").median().alias("median_ppm2"),
        pl.col("detail_id").n_unique().alias("sample"),
    )
    buy = per_deal.filter(pl.col("deal") == "predaj").select(
        group + [pl.col("median_ppm2").alias("buy_median_ppm2"), pl.col("sample").alias("buy_sample")]
    )
    rent = per_deal.filter(pl.col("deal") == "prenajom").select(
        group + [pl.col("median_ppm2").alias("rent_median_ppm2_monthly"), pl.col("sample").alias("rent_sample")]
    )
    y = buy.join(rent, on=group, how="inner").with_columns(
        (pl.col("rent_median_ppm2_monthly") * 12 / pl.col("buy_median_ppm2") * 100).alias("gross_yield_pct"),
    ).with_columns(
        (
            (pl.col("buy_sample") < MIN_SAMPLE_YIELD) | (pl.col("rent_sample") < MIN_SAMPLE_YIELD)
        ).alias("low_confidence")
    )
    schema = {**YIELD_SEGMENT_CITY_SCHEMA}
    if geo_col != "city":
        schema = {("district" if k == "city" else k): v for k, v in schema.items()}
    return y.select(list(schema.keys())).cast(schema)  # type: ignore[arg-type]


def rebuild_gold() -> dict:
    """Rebuild the four gold files (segment_weekly + yield_segment, city + okres)."""
    bronze = read_parquet_dataset(CONTAINER, BRONZE_PREFIX)
    silver = read_parquet(CONTAINER, SILVER_PATH) if blob_exists(CONTAINER, SILVER_PATH) else None
    if bronze is None or silver is None or len(bronze) == 0:
        for path, schema in (
            (GOLD_SEGMENT_WEEKLY_CITY_PATH, SEGMENT_WEEKLY_CITY_SCHEMA),
            (GOLD_SEGMENT_WEEKLY_OKRES_PATH, {("district" if k == "city" else k): v for k, v in SEGMENT_WEEKLY_CITY_SCHEMA.items()}),
            (GOLD_YIELD_SEGMENT_CITY_PATH, YIELD_SEGMENT_CITY_SCHEMA),
            (GOLD_YIELD_SEGMENT_OKRES_PATH, {("district" if k == "city" else k): v for k, v in YIELD_SEGMENT_CITY_SCHEMA.items()}),
        ):
            write_parquet(CONTAINER, path, empty_df(schema))
        return {"status": "empty"}

    out = {}
    for geo_col, sw_path, y_path in (
        ("city", GOLD_SEGMENT_WEEKLY_CITY_PATH, GOLD_YIELD_SEGMENT_CITY_PATH),
        ("district", GOLD_SEGMENT_WEEKLY_OKRES_PATH, GOLD_YIELD_SEGMENT_OKRES_PATH),
    ):
        weekly = _weekly_snapshots(bronze, silver, geo_col)
        sw = _segment_weekly(weekly, silver, geo_col)
        yl = _yield_segment(weekly, geo_col)
        write_parquet(CONTAINER, sw_path, sw)
        write_parquet(CONTAINER, y_path, yl)
        out[geo_col] = {"segment_weekly_rows": len(sw), "yield_rows": len(yl)}
    return {"status": "ok", **out}


def rebuild_all() -> dict:
    """Full daily recompute: silver then gold."""
    started = datetime.now(timezone.utc)
    silver = rebuild_silver()
    gold = rebuild_gold()
    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "silver": silver,
        "gold": gold,
    }
