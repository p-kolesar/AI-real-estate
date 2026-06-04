"""Claude tool definitions backed by FinnhubClient, plus a dispatcher.

Tools return compact structured market data (not text). Trades are NOT a tool:
the loop executes them deterministically from the agent's final decision."""


def _symbol_tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. AAPL"}},
            "required": ["symbol"],
        },
    }


# Level 1 (screening) pre-fetches quote + recommendation in the loop, so it needs
# no Claude tools. Level 2 — deep dive: full signal set.
DEEPDIVE_TOOLS = [
    _symbol_tool("get_quote", "Live price (price/open/high/low) for a symbol."),
    _symbol_tool("get_fundamentals", "P/E, EPS, ROE, dividend yield."),
    _symbol_tool("get_news", "Recent headlines (last 7 days)."),
    _symbol_tool("get_insider_sentiment", "Insider sentiment (MSPR) signal."),
    _symbol_tool("get_analyst_recommendation", "Analyst consensus buy/hold/sell counts."),
    _symbol_tool("get_price_target", "Analyst price target (high/low/mean/median)."),
    _symbol_tool("get_earnings", "Next upcoming earnings date."),
]


def run_tool(fc, name: str, tool_input: dict):
    """Dispatch a tool call to the FinnhubClient. Returns a JSON-serializable dict/list."""
    symbol = (tool_input.get("symbol") or "").upper()
    method = getattr(fc, name, None)
    if method is None:
        return {"error": f"unknown tool {name}"}
    return method(symbol)
