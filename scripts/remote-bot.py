#!/usr/bin/env python3
"""A deterministic MCP client that plays a game over the network.

Two uses:

  * as a stand-in opponent, so one live agent can be tested end to end without
    spending a second model's tokens;
  * as a connectivity self-test for a player on another machine — if this runs
    from the Mac against the VM's arbiter, the network path works.

It talks the real MCP wire protocol over streamable HTTP, so it exercises the
same transport a model's client would.

    ./remote-bot.py --url http://100.118.227.23:8790/mcp --moves e5 Nc6 Nf6

Moves are preferred in the order given and skipped when illegal, so a line that
the live opponent has knocked the game out of still plays on without erroring.
"""

from __future__ import annotations

import argparse
import json
import sys

import anyio


async def play(url: str, preferred: list[str], timeout: float, max_plies: int) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    played = 0
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, args: dict | None = None) -> dict:
                result = await session.call_tool(tool, args or {})
                if not result.content:
                    return {}
                return json.loads(result.content[0].text)

            joined = await call("join_game")
            if not joined.get("ok", True):
                print(f"join failed: {joined.get('error')}", file=sys.stderr)
                return 1
            print(f"playing as {joined.get('your_color')} in {joined.get('game_id')}",
                  flush=True)

            while played < max_plies:
                status = await call("wait_for_turn", {"timeout": timeout})
                released = status.get("released")

                if released == "game over":
                    print(f"game over: {status.get('result')} — {status.get('result_reason')}",
                          flush=True)
                    return 0
                if released == "timeout":
                    print("still not our turn; waiting again", flush=True)
                    continue

                legal = await call("get_legal_moves")
                options = legal.get("moves") or []
                sans = {m["san"] for m in options} if options and isinstance(options[0], dict) \
                    else set(options)

                choice = next((m for m in preferred if m in sans), None)
                if choice is None:
                    if not options:
                        print("no legal moves", flush=True)
                        continue
                    choice = (options[0]["san"] if isinstance(options[0], dict) else options[0])

                out = await call("make_move", {"move": choice})
                if not out.get("ok"):
                    print(f"move rejected: {out.get('error')}", file=sys.stderr)
                    return 1

                played += 1
                print(f"{played:>3}. {out.get('played')} (bot)", flush=True)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Play a game against an MCP chess arbiter.")
    ap.add_argument("--url", required=True, help="MCP endpoint, e.g. http://host:port/mcp")
    ap.add_argument("--moves", nargs="*", default=[], help="Preferred moves in SAN, in order.")
    ap.add_argument("--timeout", type=float, default=240.0, help="Per-wait timeout in seconds.")
    ap.add_argument("--max-plies", type=int, default=200, help="Safety cap on moves played.")
    args = ap.parse_args()
    return anyio.run(play, args.url, args.moves, args.timeout, args.max_plies)


if __name__ == "__main__":
    sys.exit(main())