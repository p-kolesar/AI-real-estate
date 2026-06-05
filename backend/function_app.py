"""Azure Functions app: read-only HTTP API + two timer triggers.

Routes are served under /api (host prefix). All read routes serve precomputed
Parquet/JSON from Blob as bounded JSON and stay 200 even before any data exists.

Timers (NCRONTAB, 6-field, evaluated in WEBSITE_TIME_ZONE = Bratislava):
  * scrape_next_area — every 20 min, 06:00–21:40 (the 06:00–22:00 window, 48 slots);
    one (locality, deal) unit per tick via the self-healing ledger.
  * build_and_brief  — daily 22:30; rebuild silver/gold then run the daily brief.

The agent is read-only; the only write paths are ingestion (bronze) and the daily
silver/gold/brief rebuild — never triggered by a public read route.
"""

import json
import logging
import time
from datetime import datetime, timezone

import polars as pl
import requests
import azure.functions as func

from storage.blobs import write_parquet, read_parquet
from agent.loop import run_agent
from realestate.scraper import (
    BASE,
    UA,
    CONTAINER as RE_CONTAINER,
    _robots_allows,
    _parse,
    _write_csv,
    _coverage,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json(obj, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(obj, ensure_ascii=False, default=str),
        mimetype="application/json",
        status_code=status,
    )


def _err(message: str, status: int = 400) -> func.HttpResponse:
    return _json({"error": message}, status)


# ===========================================================================
# Health
# ===========================================================================

@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok"})


# ===========================================================================
# Read API (realestate/*)
# ===========================================================================

@app.route(route="realestate/bootstrap", methods=["GET"])
def re_bootstrap(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    return _json(data.bootstrap())


@app.route(route="realestate/segments", methods=["GET"])
def re_segments(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    p = req.params
    return _json(data.segments(
        grain=p.get("grain", "city"), area=p.get("area"), category=p.get("category"),
        deal=p.get("deal"), week=p.get("week"),
    ))


@app.route(route="realestate/yield", methods=["GET"])
def re_yield(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    p = req.params
    return _json(data.yield_segments(
        grain=p.get("grain", "city"), area=p.get("area"), category=p.get("category"),
        week=p.get("week"),
    ))


@app.route(route="realestate/trend", methods=["GET"])
def re_trend(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    p = req.params
    return _json(data.trend(
        grain=p.get("grain", "city"), area=p.get("area"), category=p.get("category"),
        deal=p.get("deal"), metric=p.get("metric", "median_ppm2"),
    ))


@app.route(route="realestate/geo", methods=["GET"])
def re_geo(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    p = req.params
    return _json(data.geo(
        metric=p.get("metric", "median_ppm2"), deal=p.get("deal", "predaj"),
        category=p.get("category"), week=p.get("week"),
    ))


@app.route(route="realestate/brief", methods=["GET"])
def re_brief(req: func.HttpRequest) -> func.HttpResponse:
    from realestate import data
    brief = data.get_brief(req.params.get("date"))
    return _json(brief) if brief else _json({"error": "no brief yet"}, 404)


# ===========================================================================
# Manual spot-check + recon (Task 0)
# ===========================================================================


# ---- Real estate scraper ----


@app.route(route="scrape-realestate", methods=["GET"])
def scrape_realestate(req: func.HttpRequest) -> func.HttpResponse:
    """Scrape real-estate listings into a CSV blob.

    Query params: locality (comma-separated, default "bratislava-ruzinov"),
    deal ("predaj"), type ("byty"), pages (1-10). Stops at HTTP_BUDGET_S to stay
    under the Functions request timeout; `complete=false` flags an early stop.
    """
    t0 = time.time()
    localities = [s.strip() for s in req.params.get("locality", "bratislava-ruzinov").split(",") if s.strip()]
    deal = req.params.get("deal", "predaj")
    ptype = req.params.get("type", "byty")
    try:
        pages = max(1, min(int(req.params.get("pages", "1")), 10))
    except ValueError:
        pages = 1

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = requests.Session()
    rows, complete = [], True
    try:
        for loc in localities:
            base_url = f"{BASE}/vysledky/{ptype}/{loc}/{deal}"
            if not _robots_allows(base_url):
                continue
            for n in range(1, pages + 1):
                if time.time() - t0 > HTTP_BUDGET_S:  # the timing guarantee
                    complete = False
                    break
                url = base_url if n == 1 else f"{base_url}?page={n}"
                try:
                    r = session.get(
                        url,
                        headers={"User-Agent": UA, "Accept-Language": "sk,en;q=0.8"},
                        timeout=15,
                    )
                    r.raise_for_status()
                except Exception as e:
                    logging.error("fetch %s: %s", url, e)
                    break
                for rec in _parse(r.text, base_url):
                    rec.update({"scraped_at": ts, "locality": loc, "deal": deal})
                    rows.append(rec)
                time.sleep(1.0)  # politeness between pages
            if not complete:
                break

        blob = _write_csv(rows, ts) if rows else None
        body = {
            "ok": bool(rows),
            "complete": complete,
            "blob": blob,
            "container": RE_CONTAINER,
            "coverage": _coverage(rows),
            "elapsed_s": round(time.time() - t0, 1),
        }
        return func.HttpResponse(
            json.dumps(body, ensure_ascii=False),
            mimetype="application/json",
            status_code=200 if rows else 502,
        )
    except Exception as e:
        logging.exception("scrape-realestate failed")
        return func.HttpResponse(
            json.dumps({"ok": False, "error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )



# ===========================================================================
# Admin / manual triggers (help the deploy → test-run loop; POST only)
# ===========================================================================



@app.route(route="admin/scrape-tick", methods=["POST"])
def admin_scrape_tick(req: func.HttpRequest) -> func.HttpResponse:
    """Fire one ledger-driven ingestion tick on demand (same as the 20-min timer)."""
    from realestate import ledger
    try:
        return _json(ledger.run_tick())
    except Exception as e:
        logging.exception("admin scrape-tick failed")
        return _err(str(e), 500)


@app.route(route="admin/rebuild", methods=["POST"])
def admin_rebuild(req: func.HttpRequest) -> func.HttpResponse:
    """Rebuild silver + gold on demand (no brief)."""
    from realestate import build
    try:
        return _json(build.rebuild_all())
    except Exception as e:
        logging.exception("admin rebuild failed")
        return _err(str(e), 500)


@app.route(route="admin/brief", methods=["POST"])
def admin_brief(req: func.HttpRequest) -> func.HttpResponse:
    """Run the daily brief on demand (against current gold)."""
    from agent.loop import run_agent
    try:
        return _json(run_agent())
    except Exception as e:
        logging.exception("admin brief failed")
        return _err(str(e), 500)


# ===========================================================================
# Timer triggers
# ===========================================================================

@app.timer_trigger(schedule="0 */20 6-21 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def scrape_next_area(timer: func.TimerRequest) -> None:
    """Every 20 min within the Bratislava daytime window: sweep one pending unit."""
    from realestate import ledger
    try:
        summary = ledger.run_tick()
        logging.info("scrape_next_area: %s", json.dumps(summary, default=str))
    except Exception:
        logging.exception("scrape_next_area tick failed")


@app.timer_trigger(schedule="0 30 22 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def build_and_brief(timer: func.TimerRequest) -> None:
    """Daily ~22:30 Bratislava: rebuild silver/gold, then run the daily brief."""
    from agent.loop import run_agent
    from realestate import build
    try:
        rebuild = build.rebuild_all()
        logging.info("build_and_brief rebuild: %s", json.dumps(rebuild, default=str))
        brief = run_agent()
        logging.info("build_and_brief brief: status=%s", brief.get("status"))
    except Exception:
        logging.exception("build_and_brief failed")
