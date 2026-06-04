"""Read-only daily intelligence brief — a 2-level loop with token logging + caps.

Adapted from the portfolio agent, stripped to read-only:

  Level 1 (screening): build the compact gold segment table for the latest week
    (no model tokens to fetch — it's precomputed) and ask Claude to pick 2–4
    notable segments. One `_complete` call.
  Level 2 (deep dive, tool use): the agent pulls segment_stats / trend_series /
    yield_analysis / query_listings for the picks and writes a Slovak memo.

There are NO trades and NO watchlist — the agent only reads. Guardrails: cumulative
SPEND_CAP_USD auto-disables the agent; DAILY_TOKEN_CAP blocks the deep dive if
screening alone runs away. Exact cost is logged to agent/agent_log.parquet, and the
full memo is written to agent/briefs/<date>.json.
"""

import json
import os
import re
from datetime import datetime, timezone

import anthropic
import polars as pl

from agent.prompts import MANDATE, deepdive_user_prompt, screening_user_prompt
from agent.tools import ANALYST_TOOLS, run_tool
from realestate import data
from realestate.schemas import (
    AGENT_LOG_PATH,
    AGENT_LOG_SCHEMA,
    CONTAINER,
    brief_path,
    empty_df,
)
from storage.blobs import blob_exists, read_parquet, write_json, write_parquet

MODEL = "claude-sonnet-4-6"
SCREENING_MAX_TOKENS = 1500
DEEPDIVE_MAX_TOKENS = 3500
DAILY_TOKEN_CAP = 50000   # runaway guard (one brief ≈ 34k); real backstop is SPEND_CAP_USD
SPEND_CAP_USD = 8.0       # cumulative; auto-disables the agent (~$2 expected over 2 weeks)
INPUT_COST_PER_1M = 3.00  # claude-sonnet-4-6
OUTPUT_COST_PER_1M = 15.00
MAX_TOOL_ROUNDS = 5
GRAIN = "city"            # the brief screens the choropleth (borough) grain


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * INPUT_COST_PER_1M + output_tokens * OUTPUT_COST_PER_1M) / 1_000_000


def _extract_json(text: str):
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


def _agent_log() -> pl.DataFrame:
    if blob_exists(CONTAINER, AGENT_LOG_PATH):
        try:
            return read_parquet(CONTAINER, AGENT_LOG_PATH)
        except Exception:
            pass
    return empty_df(AGENT_LOG_SCHEMA)


def _complete(client, system, user, max_tokens):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _converse(client, system, user, tools, max_tokens):
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
                    out = run_tool(block.name, block.input)
                except Exception as e:
                    out = {"error": str(e)}
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, default=str)}
                )
        messages.append({"role": "user", "content": results})
    return "", in_tok, out_tok


def _selected_keys(selected: list[dict]) -> list[str]:
    return [f"{s.get('area')}|{s.get('category')}|{s.get('deal')}" for s in selected]


def _log_and_brief(run_date, screen, dive, selected, status, memo, week) -> dict:
    s_in, s_out = screen
    d_in, d_out = dive
    total = s_in + s_out + d_in + d_out
    cost = round(_cost(s_in + d_in, s_out + d_out), 6)
    keys = _selected_keys(selected)

    row = pl.DataFrame(
        {
            "run_date": [run_date],
            "screening_input_tokens": [s_in],
            "screening_output_tokens": [s_out],
            "deepdive_input_tokens": [d_in],
            "deepdive_output_tokens": [d_out],
            "total_tokens": [total],
            "estimated_cost_usd": [cost],
            "selected_segments": [keys],
            "status": [status],
            "memo": [memo],
        },
        schema=AGENT_LOG_SCHEMA,
    )
    log = _agent_log()
    write_parquet(CONTAINER, AGENT_LOG_PATH, pl.concat([log, row], how="diagonal_relaxed"))

    brief = {
        "run_date": run_date.isoformat(),
        "week": week,
        "grain": GRAIN,
        "status": status,
        "selected_segments": selected,
        "memo": memo,
        "tokens": {"screening_in": s_in, "screening_out": s_out,
                   "deepdive_in": d_in, "deepdive_out": d_out, "total": total},
        "estimated_cost_usd": cost,
        "model": MODEL,
    }
    write_json(CONTAINER, brief_path(run_date.isoformat()), brief)
    return brief


def run_agent() -> dict:
    """Execute one daily brief. Returns a summary dict (also written to Blob)."""
    run_date = datetime.now(timezone.utc).date()
    log = _agent_log()

    spent = float(log["estimated_cost_usd"].sum()) if len(log) else 0.0
    if spent >= SPEND_CAP_USD:
        return {"status": "disabled", "reason": f"spend cap ${SPEND_CAP_USD} reached (${spent:.2f})"}

    boot = data.bootstrap()
    week = boot.get("latest_week")
    table = data.latest_segment_table(grain=GRAIN, max_rows=60)

    # No gold yet (data still accumulating) — skip the model entirely, log a $0 run.
    if not table:
        memo = "Žiadne dáta na analýzu — dáta sa ešte len zbierajú (gold je prázdny)."
        brief = _log_and_brief(run_date, (0, 0), (0, 0), [], "ok", memo, week)
        return {"status": "ok", "reason": "no gold data", "brief": brief}

    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        return {"status": "disabled", "reason": "CLAUDE_API_KEY not set"}
    client = anthropic.Anthropic(api_key=api_key)

    # Level 1 — screening.
    screen_text, s_in, s_out = _complete(
        client, MANDATE, screening_user_prompt(table, GRAIN, week), SCREENING_MAX_TOKENS
    )
    screen = _extract_json(screen_text) or {}
    selected = [s for s in screen.get("selected", []) if isinstance(s, dict) and s.get("area")][:4]

    if s_in + s_out >= DAILY_TOKEN_CAP:
        memo = f"BLOCKED: denný tokenový limit dosiahnutý pri screeningu. {screen.get('rationale', '')}"
        brief = _log_and_brief(run_date, (s_in, s_out), (0, 0), selected, "blocked", memo, week)
        return {"status": "blocked", "reason": "daily token cap", "brief": brief}

    if not selected:
        memo = f"Žiadne segmenty nevybrané na hĺbkovú analýzu. {screen.get('rationale', '')}"
        brief = _log_and_brief(run_date, (s_in, s_out), (0, 0), [], "ok", memo, week)
        return {"status": "ok", "selected": [], "brief": brief}

    # Level 2 — deep dive (tool use) → memo.
    memo, d_in, d_out = _converse(
        client, MANDATE, deepdive_user_prompt(selected, GRAIN), ANALYST_TOOLS, DEEPDIVE_MAX_TOKENS
    )
    memo = memo.strip() or "(memo sa nevygenerovalo)"
    brief = _log_and_brief(run_date, (s_in, s_out), (d_in, d_out), selected, "ok", memo, week)
    return {
        "status": "ok",
        "week": week,
        "selected": _selected_keys(selected),
        "tokens": s_in + s_out + d_in + d_out,
        "estimated_cost_usd": brief["estimated_cost_usd"],
        "brief": brief,
    }
