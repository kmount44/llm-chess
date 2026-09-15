"""End-to-end MCP test: drive the arbiter over real stdio as a real client would.

This is the test that says "Hermes and Claude can actually play through this".
It speaks the MCP wire protocol to a subprocess — no shortcuts through Python
imports — so it exercises the transport, tool discovery and error payloads the
agents will depend on.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


def _run(client: str, body):
    """Open a real stdio MCP session against the arbiter and run ``body`` in it."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "llmchess.mcp_server", "--client", client],
        env={**os.environ},
    )

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await body(session)

    return anyio.run(main)


def _payload(result):
    """MCP tool results are text content; our tools emit JSON."""
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


def test_client_discovers_the_full_tool_surface():
    async def body(session):
        listed = await session.list_tools()
        return sorted(t.name for t in listed.tools)

    names = _run("hermes", body)
    assert names == sorted([
        "join_game", "get_board", "get_legal_moves", "make_move", "get_status",
        "wait_for_turn", "get_move_history", "resign_game", "offer_draw",
        "accept_draw", "claim_draw", "get_evaluation",
    ])


def test_two_clients_can_play_a_real_line_over_stdio(tmp_path):
    """Hermes plays white, Claude plays black, both over genuine MCP stdio."""
    from llmchess import store

    gid = store.create_game("hermes", "claude")

    def one_move(client: str, move: str):
        async def body(session):
            return _payload(await session.call_tool("make_move", {"move": move}))

        return _run(client, body)

    for client, mv in [("hermes", "e4"), ("claude", "e5"), ("hermes", "Bc4"), ("claude", "Nc6")]:
        out = one_move(client, mv)
        assert out["ok"] is True, out
        assert out["played"] == mv

    game = store.get_game(gid)
    assert game["ply"] == 4
    assert [m["san"] for m in store.get_moves(gid)] == ["e4", "e5", "Bc4", "Nc6"]


def test_illegal_move_comes_back_as_recoverable_error(tmp_path):
    from llmchess import store

    gid = store.create_game("hermes", "claude")

    async def body(session):
        return _payload(await session.call_tool("make_move", {"move": "e5"}))

    out = _run("hermes", body)
    assert out["ok"] is False
    assert "not a legal move" in out["error"]
    # And the position is untouched.
    assert store.get_game(gid)["fen"].startswith("rnbqkbnr/pppppppp")


def test_joining_reports_colour_and_position(tmp_path):
    from llmchess import store

    store.create_game("hermes", "claude")

    async def body(session):
        return _payload(await session.call_tool("join_game", {}))

    out = _run("hermes", body)
    assert out["ok"] is True
    assert out["your_color"] == "white"
    assert out["opponent"] == "claude"
    assert out["turn"] == "white"
    assert out["legal_move_count"] == 20


def test_black_client_cannot_move_first(tmp_path):
    from llmchess import store

    store.create_game("hermes", "claude")

    async def body(session):
        return _payload(await session.call_tool("make_move", {"move": "e5"}))

    out = _run("claude", body)
    assert out["ok"] is False
    assert "it is white's turn" in out["error"]


def test_get_board_reflects_a_move_made_by_the_other_client(tmp_path):
    """The two agents share one board: what one plays, the other sees."""
    from llmchess import store

    store.create_game("hermes", "claude")

    async def play(session):
        return _payload(await session.call_tool("make_move", {"move": "e4"}))

    _run("hermes", play)

    async def look(session):
        return _payload(await session.call_tool("get_board", {}))

    view = _run("claude", look)
    assert view["turn"] == "black"
    assert view["your_turn"] is True
    assert view["last_move"]["san"] == "e4"
    assert "e4" in view["history_san"]