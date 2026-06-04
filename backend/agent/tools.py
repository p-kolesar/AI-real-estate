"""The agent's 4 read-only tools, backed by realestate/data.py.

The agent is fully read-only: no trades, no watchlist, no writes. Every tool just
returns precomputed Parquet slices as compact JSON. All arithmetic lives in
build.py / data.py (Polars), never in the model.
"""

from realestate import data

_GRAIN = {"type": "string", "enum": ["city", "okres"], "description": "city = mestská časť (choropleth), okres = coarse district"}
_DEAL = {"type": "string", "enum": ["predaj", "prenajom"]}

ANALYST_TOOLS = [
    {
        "name": "segment_stats",
        "description": "Weekly segment_weekly stats (median/p25/p75 €/m², counts, WoW/MoM) "
                       "for an area × category × deal. Filter args are optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "grain": _GRAIN,
                "area": {"type": "string", "description": "City/borough or district label, e.g. 'Bratislava-Ružinov'"},
                "category": {"type": "string", "description": "e.g. '3 izbový byt'"},
                "deal": _DEAL,
                "week": {"type": "string", "description": "ISO Monday 'YYYY-MM-DD'; omit for all weeks"},
            },
        },
    },
    {
        "name": "trend_series",
        "description": "Time-series of one metric for a segment (area × category × deal).",
        "input_schema": {
            "type": "object",
            "properties": {
                "grain": _GRAIN,
                "area": {"type": "string"},
                "category": {"type": "string"},
                "deal": _DEAL,
                "metric": {"type": "string", "enum": ["median_ppm2", "listing_count", "median_price", "median_area"],
                           "description": "default median_ppm2"},
            },
        },
    },
    {
        "name": "yield_analysis",
        "description": "Gross-yield rows (buy €/m² vs monthly rent €/m², gross_yield_pct) "
                       "for an area × category. Deal is not a dimension here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "grain": _GRAIN,
                "area": {"type": "string"},
                "category": {"type": "string"},
                "week": {"type": "string"},
            },
        },
    },
    {
        "name": "query_listings",
        "description": "Sample of individual active listings (max 150) for context/drill-down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "grain": _GRAIN,
                "area": {"type": "string"},
                "category": {"type": "string"},
                "deal": _DEAL,
                "limit": {"type": "integer", "description": "default 50, hard cap 150"},
            },
        },
    },
]


def run_tool(name: str, tool_input: dict):
    """Dispatch a read-only tool call to data.py. Returns JSON-serializable output."""
    grain = tool_input.get("grain", "city")
    if name == "segment_stats":
        return data.segment_stats(grain=grain, area=tool_input.get("area"),
                                  category=tool_input.get("category"), deal=tool_input.get("deal"),
                                  week=tool_input.get("week"))
    if name == "trend_series":
        return data.trend(grain=grain, area=tool_input.get("area"), category=tool_input.get("category"),
                          deal=tool_input.get("deal"), metric=tool_input.get("metric", "median_ppm2"))
    if name == "yield_analysis":
        return data.yield_segments(grain=grain, area=tool_input.get("area"),
                                   category=tool_input.get("category"), week=tool_input.get("week"))
    if name == "query_listings":
        return data.query_listings(grain=grain, area=tool_input.get("area"),
                                   category=tool_input.get("category"), deal=tool_input.get("deal"),
                                   limit=int(tool_input.get("limit", 50)))
    return {"error": f"unknown tool {name}"}
