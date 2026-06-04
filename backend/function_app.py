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

import azure.functions as func

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

@app.route(route="scrape-realestate", methods=["POST", "GET"])
def scrape_realestate(req: func.HttpRequest) -> func.HttpResponse:
    """Manual sweep of one ?locality=&deal= unit.

    ?debug=raw  -> recon dump (Task 0): payload structure, NO writes.
    otherwise   -> sweep + write a bronze slice (?dry=true to skip the write).
    """
    from realestate import ledger, scraper
    p = req.params
    locality, deal = p.get("locality"), p.get("deal")
    if not locality or not deal:
        return _err("provide ?locality=<slug>&deal=predaj|prenajom")

    if p.get("debug") == "raw":
        page = int(p.get("page", 1))
        return _json(scraper.recon(locality, deal, page=page))

    write_bronze = p.get("dry", "").lower() not in ("1", "true", "yes")
    try:
        return _json(ledger.scrape_one(locality, deal, write_bronze=write_bronze, update_ledger=False))
    except Exception as e:
        logging.exception("scrape_realestate failed")
        return _err(str(e), 500)


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
