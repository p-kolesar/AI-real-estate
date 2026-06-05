"""Azure Functions app: read-only HTTP API + timer triggers, split by APP_ROLE.

One codebase, two deployments. The APP_ROLE app setting decides which triggers
register at import time:

  * "api" (default) — the Flex Consumption app: read API (/health, /realestate/*),
    admin rebuild/brief, and the daily build_and_brief timer. No browser needed.
  * "scraper" — the containerized Function App on Azure Container Apps: only the
    20-min ingestion timer (scrape_next_area), which drives the headless-browser
    scraper. It lives in a container because Flex Consumption can't run Chromium
    (no custom image / no system libs). See realestate/browser_scraper.py.

Routes are served under /api (host prefix). All read routes serve precomputed
Parquet/JSON from Blob as bounded JSON and stay 200 even before any data exists.

Timers (NCRONTAB, 6-field, evaluated in WEBSITE_TIME_ZONE = Bratislava):
  * scrape_next_area — every 20 min, 06:00–21:40 (the 06:00–22:00 window, 48 slots);
    one (locality, deal) unit per tick via the self-healing ledger. [scraper role]
  * build_and_brief  — daily 22:30; rebuild silver/gold then run the daily brief.
                       [api role]

The agent is read-only; the only write paths are ingestion (bronze) and the daily
silver/gold/brief rebuild — never triggered by a public read route.
"""

import json
import logging
import os

import azure.functions as func

# NOTE (import-safety): medallion modules — agent.loop, realestate.data / build /
# ledger — are imported LAZILY inside the handlers/timers that use them, never at
# module top. That keeps the app START fast and lets /health serve regardless of
# which heavy deps a given role's image carries (the scraper image has Playwright;
# the api image has anthropic).

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Which triggers this deployment registers. The container sets APP_ROLE=scraper;
# the Flex Consumption app defaults to "api".
APP_ROLE = os.environ.get("APP_ROLE", "api").strip().lower()


def _json(obj, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(obj, ensure_ascii=False, default=str),
        mimetype="application/json",
        status_code=status,
    )


def _err(message: str, status: int = 400) -> func.HttpResponse:
    return _json({"error": message}, status)


# ===========================================================================
# Health — registered in BOTH roles (Container Apps + Flex readiness probes)
# ===========================================================================

@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok", "role": APP_ROLE})


if APP_ROLE == "scraper":
    # =======================================================================
    # Containerized ingestion (headless browser) — Azure Container Apps
    # =======================================================================

    @app.route(route="admin/scrape-tick", methods=["GET"])
    def admin_scrape_tick(req: func.HttpRequest) -> func.HttpResponse:
        """Fire one ledger-driven ingestion tick on demand (same as the timer)."""
        from realestate import ledger
        try:
            return _json(ledger.run_tick())
        except Exception as e:
            logging.exception("admin scrape-tick failed")
            return _err(str(e), 500)

    @app.timer_trigger(schedule="0 */20 6-21 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
    def scrape_next_area(timer: func.TimerRequest) -> None:
        """Every 20 min within the Bratislava daytime window: sweep one pending unit."""
        from realestate import ledger
        try:
            summary = ledger.run_tick()
            logging.info("scrape_next_area: %s", json.dumps(summary, default=str))
        except Exception:
            logging.exception("scrape_next_area tick failed")

else:
    # =======================================================================
    # Read API (realestate/*) — Flex Consumption
    # =======================================================================

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

    # =======================================================================
    # Admin / manual triggers (help the deploy → test-run loop; POST only)
    # =======================================================================

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

    # =======================================================================
    # Timer triggers
    # =======================================================================

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
