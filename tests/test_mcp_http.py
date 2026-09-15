"""End-to-end MCP test over streamable HTTP.

This is the transport that matters when the two players are not on the same
machine: the arbiter and its store stay on one host, and a player elsewhere
connects to it over the network. The tests below stand up real servers as
subprocesses and drive them with real MCP clients, because the things most
likely to break here — the Host-header guard, session handshake, turn
enforcement across two connections — do not exist at the Python-import level.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    """Block until the server accepts connections, or fail with its output."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"arbiter exited early (rc={proc.returncode}):\n{out}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.15)
    raise AssertionError(f"arbiter never listened on port {port}")


class Arbiter:
    """A live arbiter server on its own port and identity."""

    def __init__(self, client: str, token: str | None = None, extra: list[str] | None = None):
        self.client = client
        self.port = _free_port()
        self.token = token
        cmd = [
            sys.executable, "-m", "llmchess.mcp_server",
            "--client", client,
            "--serve", "--host", "127.0.0.1", "--port", str(self.port),
        ]
        if token:
            cmd += ["--token", token]
        cmd += extra or []
        self.proc = subprocess.Popen(
            cmd, env={**os.environ},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        _wait_for_port(self.port, self.proc)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()


@pytest.fixture
def arbiter_pair():
    """Two arbiter processes — one per player identity — sharing one store."""
    a, b = Arbiter("hermes"), Arbiter("claude")
    try:
        yield a, b
    finally:
        a.stop()
        b.stop()


def _call(server: Arbiter, tool: str, args: dict | None = None):
    """One tool call over a fresh HTTP MCP session."""
    async def main():
        async with streamable_http_client(server.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args or {})

    result = anyio.run(main)
    assert result.content, f"{tool} returned no content"
    return json.loads(result.content[0].text)


def _tools(server: Arbiter) -> list[str]:
    async def main():
        async with streamable_http_client(server.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return sorted(t.name for t in listed.tools)

    return anyio.run(main)


def test_http_client_discovers_the_full_tool_surface(arbiter_pair):
    server, _ = arbiter_pair
    assert _tools(server) == sorted([
        "join_game", "get_board", "get_legal_moves", "make_move", "get_status",
        "wait_for_turn", "get_move_history", "resign_game", "offer_draw",
        "accept_draw", "claim_draw", "get_evaluation",
    ])


def test_two_remote_clients_play_a_real_line(arbiter_pair):
    """Hermes and Claude each on their own connection, one shared board."""
    from llmchess import store

    white, black = arbiter_pair
    gid = store.create_game("hermes", "claude")

    for server, mv in [(white, "e4"), (black, "e5"), (white, "Bc4"), (black, "Nc6")]:
        out = _call(server, "make_move", {"move": mv})
        assert out["ok"] is True, out
        assert out["played"] == mv

    game = store.get_game(gid)
    assert game["ply"] == 4
    assert [m["san"] for m in store.get_moves(gid)] == ["e4", "e5", "Bc4", "Nc6"]


def test_turn_order_is_enforced_across_separate_connections(arbiter_pair):
    """A remote client cannot move out of turn just because it has its own server."""
    from llmchess import store

    white, black = arbiter_pair
    store.create_game("hermes", "claude")

    out = _call(black, "make_move", {"move": "e5"})
    assert out["ok"] is False
    assert "it is white's turn" in out["error"]

    _call(white, "make_move", {"move": "e4"})
    out = _call(black, "make_move", {"move": "e5"})
    assert out["ok"] is True


def test_each_server_is_bound_to_its_own_identity(arbiter_pair):
    """The claude server speaks for claude and cannot claim white."""
    from llmchess import store

    white, black = arbiter_pair
    store.create_game("hermes", "claude")

    assert _call(white, "join_game")["your_color"] == "white"
    assert _call(black, "join_game")["your_color"] == "black"


def test_wait_for_turn_reports_a_timeout_without_lying(arbiter_pair):
    from llmchess import store, arbiter

    white, black = arbiter_pair
    gid = store.create_game("hermes", "claude")
    arbiter.start_game(gid)

    # White is on move, so black waits and gives up.
    out = _call(black, "wait_for_turn", {"timeout": 2})
    assert out["released"] == "timeout"
    assert out["your_turn"] is False

    # White is on move immediately.
    out = _call(white, "wait_for_turn", {"timeout": 2})
    assert out["released"] == "your turn"
    assert out["your_turn"] is True


def test_wait_for_turn_releases_in_the_moment_the_turn_arrives(arbiter_pair):
    """The real cross-machine case: one side parks while the other moves.

    This is what removes the need for a driver process, so it is worth proving
    rather than assuming.
    """
    from llmchess import store, arbiter

    white, black = arbiter_pair
    gid = store.create_game("hermes", "claude")
    arbiter.start_game(gid)

    async def main():
        async with streamable_http_client(black.url) as (rw, ww):
            async with ClientSession(rw, ww) as black_session:
                await black_session.initialize()

                async def white_moves_later():
                    await anyio.sleep(1.5)
                    async with streamable_http_client(white.url) as (rw2, ww2):
                        async with ClientSession(rw2, ww2) as white_session:
                            await white_session.initialize()
                            await white_session.call_tool("make_move", {"move": "e4"})

                async with anyio.create_task_group() as tg:
                    tg.start_soon(white_moves_later)
                    result = await black_session.call_tool("wait_for_turn", {"timeout": 30})
                return json.loads(result.content[0].text)

    out = anyio.run(main)
    assert out["released"] == "your turn", out
    assert out["your_turn"] is True
    # Released *because white actually moved*, not because the wait expired.
    assert out["ply"] == 1
    assert out["turn"] == "black"


def test_wait_for_turn_stops_when_the_game_ends(arbiter_pair):
    from llmchess import store, arbiter

    white, black = arbiter_pair
    gid = store.create_game("hermes", "claude")
    arbiter.start_game(gid)
    arbiter.resign("hermes")

    out = _call(black, "wait_for_turn", {"timeout": 3})
    assert out["released"] == "game over"
    assert out["status"] == "finished"


def test_bearer_token_is_required_when_configured():
    server = Arbiter("hermes", token="s3cret")
    try:
        import httpx2

        # Without the token the guard refuses before MCP is reached.
        r = httpx2.post(server.url, json={})
        assert r.status_code == 401

        r = httpx2.post(server.url, json={},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        # With the right token the handshake gets through to the MCP layer.
        async def main():
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer s3cret"}
            ) as http:
                async with streamable_http_client(server.url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        return sorted(t.name for t in listed.tools)

        assert "make_move" in anyio.run(main)
    finally:
        server.stop()


def test_server_refuses_an_unexpected_host_header():
    """DNS-rebinding protection: only the addresses we named are accepted."""
    server = Arbiter("hermes")
    try:
        import httpx2

        r = httpx2.post(server.url, json={},
                        headers={"Host": "evil.example.com",
                                 "Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream"})
        assert r.status_code in (400, 421)
    finally:
        server.stop()


def test_arbiter_reports_its_endpoint_on_startup(arbiter_pair):
    """The startup line is what a human copies into the MCP client config."""
    server, _ = arbiter_pair
    server.stop()
    out = server.proc.stdout.read() if server.proc.stdout else ""
    assert "/mcp" in out