"""Autonomous portfolio agent: 2-level loop with token logging and caps.

Level 1 (screening): the loop pre-fetches quote + analyst recommendation for the
whole watchlist (cheap Finnhub calls, ~no model tokens) and Claude ranks them in a
single call -> picks 2-3 to deep-dive and may add/remove watchlist symbols.
Level 2 (deep dive, tool use, max_tokens 2048): full signals -> trades + memo.

Guardrails: daily token budget (8000) blocks the deep dive; cumulative spend cap
($5) disables the agent. The watchlist is agent-managed (size-capped)."""

import json
import os
import re
from datetime import datetime

import anthropic
import polars as pl

from market.finnhub import FinnhubClient
from storage.blobs import read_parquet, write_parquet
from trading import apply_trade
from agent.prompts import MANDATE, screening_user_prompt, deepdive_user_prompt
from agent.tools import DEEPDIVE_TOOLS, run_tool

CONTAINER = "papertrading"
MODEL = "claude-sonnet-4-6"
SCREENING_MAX_TOKENS = 1024
DEEPDIVE_MAX_TOKENS = 4096
# Screening-runaway guard (blocks the deep dive if screening alone exceeds it).
# Sized for the full-watchlist scan; the real cost backstop is SPEND_CAP_USD.
DAILY_TOKEN_CAP = 15000
SPEND_CAP_USD = 5.0
INPUT_COST_PER_1M = 3.00
OUTPUT_COST_PER_1M = 15.00
MAX_TOOL_ROUNDS = 5
MAX_WATCHLIST = 30

# Sector-diversified seed; the agent grows/prunes it from here.
DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",  # tech / comms
    "JPM", "BRK.B",                            # financials
    "UNH", "LLY",                              # healthcare
    "XOM",                                     # energy
    "CAT",                                     # industrials
    "PG",                                      # staples
    "SPY", "QQQ",                              # ETFs
]


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * INPUT_COST_PER_1M + output_tokens * OUTPUT_COST_PER_1M) / 1_000_000


def _extract_json(text: str):
    """Pull the JSON object out of the model's final text (tolerates ```json fences)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _memo_after_json(text: str) -> str:
    """Strip the leading ```json {...}``` trades block; return the prose memo that follows."""
    return re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text, count=1, flags=re.DOTALL).strip()


def _watchlist() -> list[str]:
    try:
        wl = read_parquet(CONTAINER, "watchlist.parquet")["symbol"].to_list()
    except Exception:
        wl = []
    return wl or DEFAULT_WATCHLIST


def _prefetch_screen_data(fc, universe: list[str]) -> list[dict]:
    """Fetch quote + analyst recommendation for every watchlist symbol."""
    rows = []
    for sym in universe:
        try:
            quote = fc.get_quote(sym)
            rec = fc.get_analyst_recommendation(sym)
            rows.append({"symbol": sym, "price": quote.get("price"), "recommendation": rec})
        except Exception as e:
            rows.append({"symbol": sym, "price": None, "recommendation": {}, "error": str(e)})
    return rows


def _symbol_of(item) -> str:
    return (item.get("symbol", "") if isinstance(item, dict) else str(item)).upper()


def _apply_watchlist_changes(fc, watchlist: list[str], add, remove, held: list[str]):
    """Apply agent add/remove: validate adds via a live quote, enforce size cap,
    never remove a symbol with an open position. Returns (new_watchlist, changed)."""
    wl = list(watchlist)
    changed = False

    for item in remove or []:
        sym = _symbol_of(item)
        if sym in wl and sym not in held:
            wl.remove(sym)
            changed = True

    for item in add or []:
        if len(wl) >= MAX_WATCHLIST:
            break
        sym = _symbol_of(item)
        if not sym or sym in wl:
            continue
        try:
            if fc.get_quote(sym).get("price") is None:  # reject symbols Finnhub can't price
                continue
        except Exception:
            continue
        wl.append(sym)
        changed = True

    if changed:
        write_parquet(CONTAINER, "watchlist.parquet", pl.DataFrame({"symbol": wl}))
    return wl, changed


def _complete(client, system, user, max_tokens):
    """Single model call, no tools. Returns (text, in_tokens, out_tokens)."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _converse(client, fc, system, user, tools, max_tokens):
    """Run a tool-use conversation to completion. Returns (final_text, in_tokens, out_tokens)."""
    messages = [{"role": "user", "content": user}]
    in_tok = out_tok = 0
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return text, in_tok, out_tok
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                try:
                    out = run_tool(fc, block.name, block.input)
                except Exception as e:
                    out = {"error": str(e)}
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, default=str)}
                )
        messages.append({"role": "user", "content": results})
    return "", in_tok, out_tok


def _log_run(level1, level2, memo: str) -> None:
    total = sum(level1) + sum(level2)
    cost = _cost(level1[0] + level2[0], level1[1] + level2[1])
    row = pl.DataFrame(
        {
            "run_date": [datetime.now().date()],
            "level1_input_tokens": [level1[0]],
            "level1_output_tokens": [level1[1]],
            "level2_input_tokens": [level2[0]],
            "level2_output_tokens": [level2[1]],
            "total_tokens": [total],
            "estimated_cost_usd": [round(cost, 6)],
            "memo": [memo],
        }
    )
    log = read_parquet(CONTAINER, "agent_log.parquet")
    write_parquet(CONTAINER, "agent_log.parquet", pl.concat([log, row], how="diagonal_relaxed"))


def run_agent() -> dict:
    """Execute one daily agent run. Returns a summary dict."""
    log = read_parquet(CONTAINER, "agent_log.parquet")

    # Cumulative spend cap — disables the agent entirely.
    spent = float(log["estimated_cost_usd"].sum()) if len(log) > 0 else 0.0
    if spent >= SPEND_CAP_USD:
        return {"status": "disabled", "reason": f"cumulative spend cap ${SPEND_CAP_USD} reached (${spent:.2f})"}

    watchlist = _watchlist()
    fc = FinnhubClient()
    client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))

    portfolio_df = read_parquet(CONTAINER, "portfolio.parquet")
    positions = portfolio_df.to_dicts()
    held = [p["symbol"] for p in positions]
    cash_ledger = read_parquet(CONTAINER, "cash_ledger.parquet")
    cash = float(cash_ledger.row(-1, named=True)["amount"]) if len(cash_ledger) > 0 else 0.0

    # Level 1 — pre-fetch the universe, then one ranking call.
    rows = _prefetch_screen_data(fc, watchlist)
    screen_text, l1_in, l1_out = _complete(
        client, MANDATE, screening_user_prompt(rows, positions), SCREENING_MAX_TOKENS
    )
    screen = _extract_json(screen_text) or {}
    selected = [s.upper() for s in screen.get("selected", []) if s.upper() in watchlist]
    new_wl, wl_changed = _apply_watchlist_changes(
        fc, watchlist, screen.get("add", []), screen.get("remove", []), held
    )

    # Daily token budget — block the deep dive if screening already exhausted it.
    if l1_in + l1_out >= DAILY_TOKEN_CAP:
        memo = f"BLOCKED: daily token cap reached during screening. {screen.get('rationale', '')}"
        _log_run((l1_in, l1_out), (0, 0), memo)
        return {"status": "blocked", "reason": "daily token cap", "scanned": len(watchlist)}

    if not selected:
        memo = f"Žiadne symboly nevybrané na deep dive. {screen.get('rationale', '')}"
        _log_run((l1_in, l1_out), (0, 0), memo)
        return {"status": "ok", "scanned": len(watchlist), "selected": [], "watchlist_changed": wl_changed}

    # Level 2 — deep dive (tool use) on the selected names.
    dive_text, l2_in, l2_out = _converse(
        client, fc, MANDATE, deepdive_user_prompt(selected, positions, cash), DEEPDIVE_TOOLS, DEEPDIVE_MAX_TOKENS
    )
    decision = _extract_json(dive_text) or {}
    trades = decision.get("trades", [])
    memo = _memo_after_json(dive_text) or dive_text or "(no memo produced)"

    # Execute trades deterministically; price comes from a live quote, not the model.
    executed, skipped = [], []
    for t in trades:
        try:
            price = fc.get_quote(str(t.get("symbol", "")).upper()).get("price")
            result = apply_trade(t.get("symbol"), t.get("shares"), price, t.get("side"))
            result["reasoning"] = t.get("reasoning")
            executed.append(result)
        except Exception as e:
            skipped.append({"trade": t, "error": str(e)})

    _log_run((l1_in, l1_out), (l2_in, l2_out), memo)
    return {
        "status": "ok",
        "scanned": len(watchlist),
        "selected": selected,
        "executed": executed,
        "skipped": skipped,
        "watchlist_changed": wl_changed,
        "watchlist_size": len(new_wl),
        "tokens": l1_in + l1_out + l2_in + l2_out,
    }
