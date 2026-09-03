"""A LangGraph port of brain.get_response's dispatch loop.

Exists to be BENCHMARKED against the hand-written loop, not to replace it.
bot.py never imports this module; only eval/run.py does, via
`--loop langgraph`. Shipping it to production would be claiming a migration
that hasn't been justified yet.

What is deliberately shared rather than reimplemented
-----------------------------------------------------
Everything except the control flow. The same `tools.TOOL_SCHEMAS_CACHED` and
`tools.execute_tool` registry, the same `brain.build_system_blocks`,
`_cache_messages`, `_blocks_to_dicts`, `_trim_history`, and the same
`database.record_*` instrumentation. If this module rebuilt any of those, the
comparison would be measuring two different agents and the benchmark would be
worthless.

Three couplings matter and are easy to get wrong:

1. The client is fetched through `brain._get_client()` on every call, never
   cached locally. eval/run.py swaps that function to install the response
   cache; a local reference captured at import would bypass it and turn every
   replay into a live API call.
2. The ceiling is read from `brain.MAX_TOOL_ITERATIONS` at call time, not
   bound to a module constant at import. run.py sets that attribute on the
   module directly, after import, precisely because reading it once at import
   is how a ceiling run silently executed at the default 10 while claiming
   otherwise.
3. System blocks come from `brain.build_system_blocks`, so run.py's
   `freeze_clock` (which patches `brain.datetime`) reaches this loop too.

Why the ceiling is a node and not `recursion_limit`
---------------------------------------------------
LangGraph's own bound raises GraphRecursionError at the graph boundary. That
is a hard stop: the exception surfaces with no opportunity to do anything with
the tool results already gathered, which is the opposite of what the hand loop
does -- it spends one more call, with tools withheld, forcing a real answer
out of whatever came back before the budget ran out.

So the ceiling is carried as a counter in graph state and enforced by a
conditional edge into an explicit `synthesize` node. `recursion_limit` is
still set, generously, as a runaway guard: reaching it means the routing is
wrong, not that the turn ran long.
"""

import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import brain
import database
import tools

logger = logging.getLogger(__name__)

# Graph steps per tool round: call_model + execute_tools. Plus the synthesis
# node, the terminal node, and headroom for truncation retries. A ceiling of N
# should never need more than this, so tripping it is a routing bug.
_RECURSION_HEADROOM = 10


def _ceiling() -> int:
    """Read the ceiling live. See coupling (2) in the module docstring."""
    return brain.MAX_TOOL_ITERATIONS


class GraphState(TypedDict, total=False):
    # --- per-turn context, set once at invoke and never mutated ---
    chat_id: int
    job_queue: Any
    conversation_id: str | None
    turn_index: int
    turn_start: int
    system_blocks: list

    # --- evolving conversation ---
    messages: list
    stop_reason: str | None
    response_content: Any

    # --- accounting, mirrored from the hand loop so report.py can read both ---
    iterations: int
    calls: int
    api_ms: float
    usage: dict
    truncations: int

    # --- outcome ---
    final_text: str
    terminated_by: str


def _account(state: GraphState, response) -> None:
    u = getattr(response, "usage", None)
    if u is None:
        return
    usage = state["usage"]
    usage["read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    usage["write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    usage["fresh"] += getattr(u, "input_tokens", 0) or 0


def _extract_text(content) -> str:
    return next(
        (b.text for b in content if b.type == "text"), ""
    ) or "Sorry, I couldn't come up with a response for that -- try rephrasing."


# --- nodes -----------------------------------------------------------------


async def call_model(state: GraphState) -> dict:
    """One tool-enabled API round. Mirrors the body of the hand loop's `for`."""
    messages = state["messages"]
    started = time.perf_counter()
    response = await brain._get_client().messages.create(
        model=brain.MODEL,
        max_tokens=brain.MAX_TOKENS,
        system=state["system_blocks"],
        tools=tools.TOOL_SCHEMAS_CACHED,
        messages=brain._cache_messages(messages, state["turn_start"]),
    )
    api_ms = state["api_ms"] + (time.perf_counter() - started) * 1000
    _account(state, response)

    update: dict = {
        "iterations": state["iterations"] + 1,
        "calls": state["calls"] + 1,
        "api_ms": api_ms,
        "stop_reason": response.stop_reason,
        "response_content": response.content,
    }

    if response.stop_reason == "max_tokens":
        # The trailing tool_use block has truncated JSON and must not run, and
        # the turn can't be appended either -- an unanswered tool_use poisons
        # every later request. Drop it whole and nudge for smaller batches.
        truncations = state["truncations"] + 1
        update["truncations"] = truncations
        logger.warning(
            "Response truncated at max_tokens for chat %s (attempt %d)",
            state["chat_id"], truncations,
        )
        if truncations > brain.MAX_TRUNCATION_RETRIES:
            update["final_text"] = brain._OVER_BUDGET_REPLY
            update["terminated_by"] = "truncated"
            return update
        update["messages"] = messages + [
            {"role": "assistant",
             "content": [{"type": "text", "text": "(reply cut off)"}]},
            {"role": "user", "content": brain._TRUNCATION_NUDGE},
        ]
        return update

    # An empty content list is rejected by the API on the next request.
    blocks = brain._blocks_to_dicts(response.content) or [
        {"type": "text", "text": "(no content)"}
    ]
    update["messages"] = messages + [{"role": "assistant", "content": blocks}]

    if response.stop_reason != "tool_use":
        update["final_text"] = _extract_text(response.content)
        update["terminated_by"] = "answer"
    return update


async def execute_tools(state: GraphState) -> dict:
    """Run every tool_use block in the last response, timing each one."""
    conversation_id = state["conversation_id"]
    tool_results = []
    call_index = 0
    for block in state["response_content"]:
        if block.type != "tool_use":
            continue
        # Timed around execute_tool only: handlers are synchronous SQLite (plus
        # Playwright for image tools), so this is execution time, not the API
        # round trip. Kept apart from api_ms because most handlers are
        # sub-millisecond and would vanish under ~2s of network otherwise.
        started = time.perf_counter()
        result_text, is_error = await tools.execute_tool(
            block.name, block.input, state["chat_id"], state["job_queue"]
        )
        if conversation_id is not None:
            database.record_tool_call(
                conversation_id,
                state["turn_index"],
                block.name,
                state["iterations"],
                call_index,
                (time.perf_counter() - started) * 1000,
                is_error,
            )
        call_index += 1
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_text,
            "is_error": is_error,
        })
    return {"messages": state["messages"] + [
        {"role": "user", "content": tool_results}
    ]}


async def synthesize(state: GraphState) -> dict:
    """The ceiling path: one more call with NO tools available.

    This node is the whole reason the ceiling isn't `recursion_limit`. Rather
    than discarding a turn that mostly succeeded, Claude is made to answer from
    the tool results already in `messages`.
    """
    logger.warning(
        "Hit MAX_TOOL_ITERATIONS for chat %s -- forcing a synthesis-only reply",
        state["chat_id"],
    )
    started = time.perf_counter()
    response = await brain._get_client().messages.create(
        model=brain.MODEL,
        max_tokens=brain.MAX_TOKENS,
        system=brain.build_system_blocks(
            state["chat_id"],
            extra=(
                "You are out of tool calls for this turn. Answer using "
                "ONLY the tool results already gathered above -- do not "
                "claim to call any more tools. If something is still "
                "missing, say so briefly rather than guessing."
            ),
        ),
        messages=brain._cache_messages(state["messages"], state["turn_start"]),
    )
    _account(state, response)
    return {
        "api_ms": state["api_ms"] + (time.perf_counter() - started) * 1000,
        "calls": state["calls"] + 1,
        "final_text": _extract_text(response.content),
        "terminated_by": "ceiling",
    }


# --- routing ---------------------------------------------------------------


def route_after_model(state: GraphState) -> str:
    if state.get("terminated_by") == "truncated":
        return "done"
    if state["stop_reason"] == "max_tokens":
        return "retry"
    if state["stop_reason"] == "tool_use":
        return "tools"
    return "done"


def route_after_tools(state: GraphState) -> str:
    """The ceiling check, placed AFTER tool execution to match the hand loop.

    There, the final round's tools still run before the `for` falls through to
    synthesis. Checking before execution instead would silently drop one round
    of work and make the two loops incomparable.
    """
    if state["iterations"] >= _ceiling():
        return "synthesize"
    return "call_model"


def build_graph():
    g = StateGraph(GraphState)
    g.add_node("call_model", call_model)
    g.add_node("execute_tools", execute_tools)
    g.add_node("synthesize", synthesize)
    g.add_edge(START, "call_model")
    g.add_conditional_edges(
        "call_model",
        route_after_model,
        {"tools": "execute_tools", "retry": "call_model", "done": END},
    )
    g.add_conditional_edges(
        "execute_tools",
        route_after_tools,
        {"call_model": "call_model", "synthesize": "synthesize"},
    )
    g.add_edge("synthesize", END)
    return g.compile()


_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# --- public entry point ----------------------------------------------------


async def get_response(
    chat_id: int,
    user_text: str,
    history: list,
    job_queue,
    conversation_id: str | None = None,
    turn_index: int = 0,
) -> str:
    """Signature-identical to brain.get_response, so eval/run.py can swap the
    two modules without knowing which it holds.
    """
    messages = history + [{"role": "user", "content": user_text}]
    turn_start = len(history)
    turn_started = time.perf_counter()

    if conversation_id is not None:
        database.record_turn_start(
            conversation_id, turn_index, chat_id, len(user_text), user_text
        )

    initial: GraphState = {
        "chat_id": chat_id,
        "job_queue": job_queue,
        "conversation_id": conversation_id,
        "turn_index": turn_index,
        "turn_start": turn_start,
        "system_blocks": brain.build_system_blocks(chat_id),
        "messages": messages,
        "stop_reason": None,
        "response_content": None,
        "iterations": 0,
        "calls": 0,
        "api_ms": 0.0,
        "usage": {"read": 0, "write": 0, "fresh": 0},
        "truncations": 0,
    }

    final = await get_graph().ainvoke(
        initial,
        config={"recursion_limit": _ceiling() * 2 + _RECURSION_HEADROOM},
    )

    final_text = final.get("final_text") or _extract_text([])
    terminated_by = final.get("terminated_by", "answer")

    # --- commit, mirroring brain._commit ---
    if terminated_by == "truncated":
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": final_text})
    else:
        history.extend(final["messages"][len(history):])
        # Every stored turn must end on an assistant reply. It won't when the
        # ceiling path ran, whose synthesis response is deliberately never
        # appended to `messages` (a one-off call made with no tools).
        if not history or history[-1].get("role") != "assistant":
            history.append({"role": "assistant", "content": final_text})
    brain._trim_history(history)
    brain._log_cache_usage(chat_id, final["calls"], final["usage"])

    if conversation_id is not None:
        database.finalize_turn(
            conversation_id,
            turn_index,
            terminated_by,
            iterations=final["iterations"],
            api_calls=final["calls"],
            api_ms=final["api_ms"],
            total_ms=(time.perf_counter() - turn_started) * 1000,
            reply_text=final_text,
        )
    return final_text
