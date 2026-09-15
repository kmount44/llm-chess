"""MCP arbiter server.

Each player runs its own instance of this process, launched with the identity it
speaks for::

    chess-arbiter --client hermes     # Hermes' tool surface
    chess-arbiter --client claude     # Claude's tool surface

Colour is *not* baked in at launch — it comes from the game record, so the same
registered server can play either side and colours can be swapped between games.

The transport is stdio by default, which is what both Claude Code and Hermes
speak when the player runs on the same machine as the arbiter. Add `--serve` and
the same server speaks streamable HTTP instead, so a player on another machine
can connect to it over the network. Either way the arbiter and its store stay on
one host: that is what keeps a single source of truth.

The server itself is stateless between calls: the store holds the game, so a
client that dies mid-game loses nothing but its own reasoning context.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from typing import Any, Callable

from . import arbiter

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

INSTRUCTIONS = """\
You are playing a game of chess through this tool surface. The server is the
arbiter: it owns the board, the clocks and the rules. You cannot cheat, and you
cannot accidentally desync the position.

How to play:
1. Call `join_game` once to learn your colour and the current position.
2. Call `get_board` before every move — you must move from the position you can
   actually see, and the opponent may have moved since you last looked.
3. Call `get_legal_moves` if you want the full list of candidate moves.
4. Call `make_move` with your chosen move. SAN ("Nf3") and UCI ("g1f3") are both
   accepted. If the move is rejected you get an explicit error explaining why —
   fix it and call `make_move` again. The position is unchanged by a rejection.
5. Call `get_status` to check whose turn it is, the clocks, and whether the game
   is over. Always check before assuming it is your move.

Rules of engagement:
- You may only move on your turn, and only for the colour you were assigned.
- The game ends on checkmate, stalemate, a claimed draw, or resignation —
  `resign_game` is available and using it is not a failure.
- Think for yourself. No engine advice is provided; a separate `get_evaluation`
  tool gives you only material balance and mobility, never a recommended move.
"""


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _guard(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Turn arbiter rule violations into a recoverable payload for the agent.

    A rejected move must not look like a broken tool: the agent needs to read the
    reason and try again, so errors come back as data with ``ok: false`` rather
    than as a protocol-level failure.

    The wrapper returns the same type the tool declares (a dict) and carries the
    real signature through ``functools.wraps``, because the server derives both
    the tool's JSON schema and its result validation from that signature.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = fn(*args, **kwargs)
        except (arbiter.ArbiterError, LookupError) as exc:
            return {"ok": False, "error": str(exc)}
        if isinstance(payload, dict) and "ok" not in payload:
            return {"ok": True, **payload}
        return payload

    return wrapper


def build_server(client: str) -> Any:
    server = _Server(
        name=f"llm-chess-arbiter:{client}",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    @server.tool(description="Join the current game: your colour, the opponent, and the position.")
    @_guard
    def join_game(game_id: str | None = None) -> dict[str, Any]:
        return arbiter.join(client, game_id)

    @server.tool(description="Read the current position: FEN, board diagram, side to move, check state, clocks, material, and move history.")
    @_guard
    def get_board(game_id: str | None = None) -> dict[str, Any]:
        return arbiter.board(client, game_id)

    @server.tool(description="List every legal move in the current position, as SAN and UCI.")
    @_guard
    def get_legal_moves(game_id: str | None = None, verbose: bool = False) -> dict[str, Any]:
        return arbiter.legal_moves(client, game_id, verbose=verbose)

    @server.tool(description="Play a move. Accepts SAN ('Nf3') or UCI ('g1f3'). Rejected moves leave the position untouched.")
    @_guard
    def make_move(move: str, game_id: str | None = None, comment: str | None = None) -> dict[str, Any]:
        return arbiter.move(client, move, game_id, comment=comment)

    @server.tool(description="Game status: whose turn it is, clocks, result, and whether the game is over.")
    @_guard
    def get_status(game_id: str | None = None) -> dict[str, Any]:
        return arbiter.status(client, game_id)

    @server.tool(description="Block until it is your turn, the game ends, or the wait times out. This is how you play without polling: call it, then move if it says it is your turn.")
    @_guard
    def wait_for_turn(timeout: float = 240.0, game_id: str | None = None) -> dict[str, Any]:
        return arbiter.wait_for_turn(client, game_id, timeout=timeout)

    @server.tool(description="SAN move list so far, in order.")
    @_guard
    def get_move_history(game_id: str | None = None) -> dict[str, Any]:
        from . import store

        game = store.resolve_game(game_id, clients=[client])
        return {
            "game_id": game["id"],
            "plies": game["ply"],
            "moves": [
                {"ply": m["ply"], "san": m["san"], "by": m["client"], "color": m["color"]}
                for m in store.get_moves(game["id"])
            ],
        }

    @server.tool(description="Resign the game. Not a failure — the honest end to a lost position.")
    @_guard
    def resign_game(reason: str | None = None, game_id: str | None = None) -> dict[str, Any]:
        return arbiter.resign(client, game_id, reason)

    @server.tool(description="Offer a draw to your opponent. They must call accept_draw.")
    @_guard
    def offer_draw(game_id: str | None = None) -> dict[str, Any]:
        return arbiter.offer_draw(client, game_id)

    @server.tool(description="Accept your opponent's outstanding draw offer.")
    @_guard
    def accept_draw(game_id: str | None = None) -> dict[str, Any]:
        return arbiter.accept_draw(client, game_id)

    @server.tool(description="Claim a draw you are entitled to by rule (threefold repetition or the fifty-move rule).")
    @_guard
    def claim_draw(rule: str = "threefold repetition", game_id: str | None = None) -> dict[str, Any]:
        return arbiter.claim_draw(client, game_id, rule)

    @server.tool(description="Objective position facts only — no engine, no advice: material balance, legal move count, check state.")
    @_guard
    def get_evaluation(game_id: str | None = None) -> dict[str, Any]:
        from . import rules, store

        game = store.resolve_game(game_id, clients=[client])
        info = rules.describe(game["fen"])
        return {
            "game_id": game["id"],
            "material": rules.material_balance(game["fen"]),
            "legal_move_count": info["legal_move_count"],
            "is_check": info["is_check"],
            "in_check_from": None,
            "note": "material and mobility only — this is not an engine score",
        }

    return server


class _BearerAuth:
    """Refuse HTTP requests that do not carry the expected bearer token.

    Identity is already bound to the port a client connects to, so this is a
    lock on the door rather than the security model itself. It is worth having
    anyway: it stops a stray process on the network from playing someone else's
    moves.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        if headers.get("authorization") != f"Bearer {self.token}":
            from starlette.responses import Response

            await Response("unauthorized\n", status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def serve(
    *,
    client: str,
    host: str,
    port: int,
    path: str,
    token: str | None = None,
    extra_hosts: list[str] | None = None,
) -> int:
    """Run the arbiter over streamable HTTP instead of stdio.

    This is what lets the two players live on different machines. The arbiter
    and its store stay on one host — that is the single source of truth — while
    a client elsewhere connects to it over the network. One process per
    identity, exactly as with stdio, so neither player can claim the other's
    colour just by asking.
    """
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_server(client)

    # Reaching the server by its tailnet address means the Host header is not
    # localhost, which the default DNS-rebinding guard rejects. Allow exactly
    # the addresses we intend to be reachable at.
    allowed = [
        "localhost", "127.0.0.1", "[::1]",
        host, f"{host}:{port}",
        "host.docker.internal",
        *(extra_hosts or []),
    ]
    # Deduplicate while keeping order stable and readable.
    allowed = list(dict.fromkeys(a for a in allowed if a))

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=[f"http://{h}" for h in allowed] + [f"https://{h}" for h in allowed],
    )

    app = server.streamable_http_app(
        streamable_http_path=path, host=host, transport_security=security
    )
    if token:
        app = _BearerAuth(app, token)

    scheme = "http"
    print(f"llm-chess arbiter ({client}) → {scheme}://{host}:{port}{path}", flush=True)
    print(f"  reachable hosts: {', '.join(allowed)}", flush=True)
    print(f"  auth: {'bearer token required' if token else 'none (tailnet only)'}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chess-arbiter",
        description="MCP chess arbiter, over stdio or streamable HTTP.",
    )
    parser.add_argument(
        "--client",
        default=os.environ.get("LLM_CHESS_CLIENT", "hermes"),
        help="Identity this server speaks for (must match a player name in the game record).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve over streamable HTTP instead of stdio (for players on other machines).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address when serving.")
    parser.add_argument("--port", type=int, default=8788, help="Bind port when serving.")
    parser.add_argument("--path", default="/mcp", help="HTTP path for the MCP endpoint.")
    parser.add_argument(
        "--token",
        default=os.environ.get("LLM_CHESS_TOKEN"),
        help="Require this bearer token on HTTP requests.",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Extra Host header to accept (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.serve:
        return serve(
            client=args.client,
            host=args.host,
            port=args.port,
            path=args.path,
            token=args.token,
            extra_hosts=args.allow_host,
        )

    build_server(args.client).run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())