"""Parity tests: the LangGraph port must behave identically to the hand loop.

A benchmark between two loops only means something if they are the same agent
under the same conditions. These tests drive BOTH implementations through the
same scripted API responses and assert they agree on termination mode, API call
count, iteration count and tool dispatch. A divergence here would silently turn
the comparison into a measurement of two different systems.

No API key and no network: the client is a stub that replays a fixed script.
That also makes the four termination paths -- answer, tool-then-answer, the
ceiling synthesis, and repeated truncation -- reachable without contriving
prompts long enough to trigger them for real.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain
import graph_loop
import tools

# --- stubs -----------------------------------------------------------------


class Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input or {}
        self.id = id


class Response:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = None


def text_response(text="done"):
    return Response("end_turn", [Block("text", text=text)])


def tool_response(name="list_topics", call_id="tu_1"):
    return Response("tool_use", [
        Block("tool_use", name=name, input={}, id=call_id)
    ])


def truncated_response():
    return Response("max_tokens", [Block("text", text="cut")])


class ScriptedClient:
    """Replays a list of responses; repeats the last one once exhausted."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        async def create(self, **kwargs):
            o = self._outer
            o.calls.append(kwargs)
            idx = min(len(o.calls) - 1, len(o.script) - 1)
            return o.script[idx]


@pytest.fixture
def harness(monkeypatch):
    """Neutralise everything except control flow, for both loops at once."""
    executed = []

    async def fake_execute_tool(name, tool_input, chat_id, job_queue):
        executed.append(name)
        return f"ran {name}", False

    monkeypatch.setattr(tools, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(tools, "TOOL_SCHEMAS_CACHED", [])
    monkeypatch.setattr(
        brain, "build_system_blocks", lambda chat_id, extra=None: [
            {"type": "text", "text": "sys" + (extra or "")}
        ]
    )
    # conversation_id is None throughout, so database.record_* is never
    # reached; nothing else in either loop touches SQLite.
    return executed


def drive(loop_module, script, monkeypatch, ceiling=10):
    client = ScriptedClient(script)
    monkeypatch.setattr(brain, "_get_client", lambda: client)
    monkeypatch.setattr(brain, "MAX_TOOL_ITERATIONS", ceiling)
    history: list = []

    import asyncio
    reply = asyncio.run(
        loop_module.get_response(1, "hello", history, None)
    )
    return reply, client, history


LOOPS = [brain, graph_loop]
IDS = ["native", "langgraph"]


# --- per-path behaviour ----------------------------------------------------


@pytest.mark.parametrize("loop", LOOPS, ids=IDS)
def test_plain_answer(loop, harness, monkeypatch):
    reply, client, _history = drive(loop, [text_response("hi")], monkeypatch)
    assert reply == "hi"
    assert len(client.calls) == 1
    assert harness == []


@pytest.mark.parametrize("loop", LOOPS, ids=IDS)
def test_tool_then_answer(loop, harness, monkeypatch):
    reply, client, _ = drive(
        loop, [tool_response(), text_response("after tool")], monkeypatch
    )
    assert reply == "after tool"
    assert harness == ["list_topics"]
    assert len(client.calls) == 2


@pytest.mark.parametrize("loop", LOOPS, ids=IDS)
def test_ceiling_forces_synthesis(loop, harness, monkeypatch):
    """At the ceiling the final round's tools still run, then ONE tool-free
    call synthesises an answer. Ceiling 2 => 2 tool rounds + 1 synthesis."""
    reply, client, _ = drive(
        loop, [tool_response(), tool_response(), tool_response()],
        monkeypatch, ceiling=2,
    )
    assert len(client.calls) == 3
    assert harness == ["list_topics", "list_topics"]
    # The synthesis call is the one made with no tools available.
    assert not client.calls[-1].get("tools")
    assert "out of tool calls" in client.calls[-1]["system"][0]["text"]
    assert reply


@pytest.mark.parametrize("loop", LOOPS, ids=IDS)
def test_repeated_truncation_gives_up(loop, harness, monkeypatch):
    reply, client, _ = drive(loop, [truncated_response()], monkeypatch)
    assert reply == brain._OVER_BUDGET_REPLY
    assert len(client.calls) == brain.MAX_TRUNCATION_RETRIES + 1


# --- cross-implementation parity -------------------------------------------


SCRIPTS = {
    "answer": [text_response("x")],
    "tool_then_answer": [tool_response(), text_response("y")],
    "ceiling": [tool_response(), tool_response(), tool_response()],
    "truncated": [truncated_response()],
}


@pytest.mark.parametrize("script_name", list(SCRIPTS))
def test_both_loops_agree(script_name, harness, monkeypatch):
    ceiling = 2 if script_name == "ceiling" else 10
    script = SCRIPTS[script_name]

    harness.clear()
    native_reply, native_client, native_history = drive(
        brain, script, monkeypatch, ceiling
    )
    native_tools = list(harness)

    harness.clear()
    graph_reply, graph_client, graph_history = drive(
        graph_loop, script, monkeypatch, ceiling
    )
    graph_tools = list(harness)

    assert native_reply == graph_reply
    assert len(native_client.calls) == len(graph_client.calls)
    assert native_tools == graph_tools
    # History shape drives the next turn's prompt cache; a divergence here
    # would show up as a cache-hit-rate difference wrongly attributed to the
    # framework.
    assert len(native_history) == len(graph_history)
    assert [e["role"] for e in native_history] == [
        e["role"] for e in graph_history
    ]
