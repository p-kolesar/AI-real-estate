"""Self-healing ingestion ledger + the per-tick runner.

The ledger (`meta/ingest_ledger.json`) is a date → unit → status map. One UTC date
key holds all 42 `(locality, deal)` units; a unit is 'pending' until a sweep marks it
'done' (or 'error'). The active scrape window (06:00–22:00 Bratislava) never crosses
UTC midnight, so a fresh UTC date key simply appears each morning = the daily reset.

`run_tick()` is what the 20-min timer calls: ensure today's units exist → pick the
next pending one → sweep it → write its bronze slice (idempotent overwrite) → append
an `ingest_runs` row → mark the unit. A failed/missed unit stays 'pending' and is
retried next tick. No-op once the day is complete.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import polars as pl

from realestate import scraper
from realestate.schemas import (
    CONTAINER,
    INGEST_RUNS_PATH,
    INGEST_RUNS_SCHEMA,
    LEDGER_PATH,
    bronze_path,
    scrape_units,
    unit_key,
)
from storage.blobs import append_parquet, read_json, write_json, write_parquet


# ---------------------------------------------------------------------------
# Ledger state machine (pure-ish: read/write via blobs)
# ---------------------------------------------------------------------------

def load_ledger() -> dict:
    return read_json(CONTAINER, LEDGER_PATH, default={}) or {}


def save_ledger(ledger: dict) -> None:
    write_json(CONTAINER, LEDGER_PATH, ledger)


def ensure_day(ledger: dict, date: str) -> dict:
    """Initialize all 42 units 'pending' for `date` if the day key is absent."""
    if date not in ledger:
        ledger[date] = {unit_key(slug, deal): {"status": "pending"} for slug, deal in scrape_units()}
    return ledger[date]


def next_pending(ledger: dict, date: str) -> tuple[str, str] | None:
    """First (slug, deal) still pending today, in deterministic unit order."""
    day = ledger.get(date, {})
    for slug, deal in scrape_units():
        if day.get(unit_key(slug, deal), {}).get("status") == "pending":
            return slug, deal
    return None


def mark(ledger: dict, date: str, slug: str, deal: str, status: str, **meta) -> None:
    ledger.setdefault(date, {})[unit_key(slug, deal)] = {"status": status, **meta}


def coverage_pct(ledger: dict, date: str) -> float:
    day = ledger.get(date, {})
    if not day:
        return 0.0
    done = sum(1 for u in day.values() if u.get("status") == "done")
    return round(100.0 * done / len(day), 1)


# ---------------------------------------------------------------------------
# Bronze write + run log
# ---------------------------------------------------------------------------

def _write_bronze(deal: str, date: str, slug: str, df: pl.DataFrame) -> None:
    """Idempotent: overwrites this unit's slice for the day (one snapshot per unit/day)."""
    write_parquet(CONTAINER, bronze_path(deal, date, slug), df)


def _log_run(run_id: str, date: str, started: datetime, finished: datetime,
             slug: str, deal: str, result: dict) -> None:
    row = pl.DataFrame(
        {
            "run_id": [run_id],
            "scraped_date": [datetime.fromisoformat(date).date()],
            "started_at": [started],
            "finished_at": [finished],
            "source_slug": [slug],
            "deal": [deal],
            "n_pages": [int(result.get("n_pages", 0))],
            "n_rows": [int(result.get("n_rows", 0))],
            "cap_hit": [bool(result.get("cap_hit", False))],
            "status": [result.get("status", "error")],
            "error": [result.get("error")],
        },
        schema=INGEST_RUNS_SCHEMA,
    )
    append_parquet(CONTAINER, INGEST_RUNS_PATH, row)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape_one(slug: str, deal: str, *, write_bronze: bool = True,
               update_ledger: bool = False, now: datetime | None = None) -> dict:
    """Sweep a single unit end-to-end: scrape → (write bronze) → log run.

    Called by `run_tick` (update_ledger=True, via the ledger); the update_ledger=False
    variant is kept for eval/spot-checks. Returns a JSON-friendly summary (no DataFrame).
    """
    now = now or datetime.now(timezone.utc)
    date = now.date().isoformat()
    run_id = uuid.uuid4().hex
    started = now

    result = scraper.sweep_unit(slug, deal, scraped_at=now)
    finished = datetime.now(timezone.utc)

    df = result["df"]
    if write_bronze and result["status"] == "done":
        _write_bronze(deal, date, slug, df)
    _log_run(run_id, date, started, finished, slug, deal, result)

    if update_ledger:
        ledger = load_ledger()
        ensure_day(ledger, date)
        ledger_status = "done" if result["status"] == "done" else "pending"  # block/error -> retry
        mark(ledger, date, slug, deal, ledger_status,
             scraped_at=finished.isoformat(), n_listings=result["n_rows"],
             n_pages=result["n_pages"], cap_hit=result["cap_hit"],
             sweep_status=result["status"], error=result["error"])
        save_ledger(ledger)

    return {
        "run_id": run_id, "date": date, "slug": slug, "deal": deal,
        "status": result["status"], "n_rows": result["n_rows"],
        "n_pages": result["n_pages"], "cap_hit": result["cap_hit"],
        "error": result["error"], "wrote_bronze": write_bronze and result["status"] == "done",
    }


def run_tick(now: datetime | None = None) -> dict:
    """One 20-min timer tick: pick the next pending unit and sweep it. No-op when done."""
    now = now or datetime.now(timezone.utc)
    date = now.date().isoformat()

    ledger = load_ledger()
    ensure_day(ledger, date)
    save_ledger(ledger)  # persist the daily reset even if nothing is pending

    nxt = next_pending(ledger, date)
    if nxt is None:
        return {"status": "idle", "date": date, "coverage_pct": coverage_pct(ledger, date),
                "reason": "day complete — all 42 units done"}

    slug, deal = nxt
    summary = scrape_one(slug, deal, write_bronze=True, update_ledger=True, now=now)
    summary["coverage_pct"] = coverage_pct(load_ledger(), date)
    return summary
