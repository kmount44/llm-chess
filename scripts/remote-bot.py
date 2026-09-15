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

A dropped connection is expected on a network link — the arbiter may be
restarted, a tailnet path may flap — so a lost transport is reported as such and
retried rather than crashing. That is safe precisely because the game state
lives in the arbiter's store, not in this process: reconnecting resumes the same
game at whatever ply it has reached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _ensure_deps() -> None:
    """Hand off to the repo's virtualenv when run with a bare python3.

    This file is executable and carries a ``#!/usr/bin/env python3`` shebang, so
    ``./scripts/remote-bot.py`` runs under whatever python3 is on PATH — which
    does not have anyio. Rather than die with a ModuleNotFoundError that looks
    like a broken script, re-exec under the venv the installer created.
    """
    try:
        import anyio  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    script = Path(__file__).resolve()
    venv_dir = script.parent.parent / ".venv"

    # Are we already running under that venv? Ask sys.prefix. Comparing
    # resolved paths is tempting and wrong: a venv's bin/python is a symlink to
    # the interpreter it was built from, so resolving collapses two *different*
    # venvs onto the same base binary and the check always answers "yes,
    # already there" — which means no handoff ever happens.
    if Path(sys.prefix) == venv_dir:
        raise SystemExit(
            "error: anyio is missing from the repo virtualenv, which should not "
            "happen.\n"
            "  fix: ./scripts/install-mac.sh"
        )

    for name in ("python", "python3"):
        candidate = venv_dir / "bin" / name
        if candidate.exists():
            # execv, not a subprocess: this replaces the process, so the exit
            # code, signals and streaming output all behave as the caller expects.
            os.execv(str(candidate), [str(candidate), str(script), *sys.argv[1:]])
            return  # not reached

    raise SystemExit(
        "error: required packages are not importable and no usable virtualenv "
        "was found.\n"
        f"  looked for: {venv_dir / 'bin' / 'python'}\n"
        "  fix: run ./scripts/install-mac.sh, or invoke it explicitly:\n"
        "       .venv/bin/python scripts/remote-bot.py --url ... --moves ..."
    )


_ensure_deps()

import anyio  # noqa: E402  (must come after _ensure_deps)

DONE = "done"
LOST = "lost"


def _root_cause(exc: BaseException) -> str:
    """Unwrap anyio's ExceptionGroup down to the message that actually helps."""
    seen = 0
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and seen < 10:
        exc = exc.exceptions[0]
        seen += 1
    return f"{type(exc).__name__}: {exc}"


def _advice(exc: BaseException) -> str:
    """Turn a transport failure into something the reader can act on."""
    text = _root_cause(exc)
    low = text.lower()
    if "connect" in low or "connection" in low or "refused" in low:
        return (f"{text}\n  cannot reach the arbiter. Check that it is running "
                f"(./scripts/serve-match.sh), that the host and port are right, "
                f"and that this machine can route to it.")
    if "401" in low or "unauthorized" in low:
        return (f"{text}\n  the arbiter wants a bearer token. Configure it on this "
                f"client, or unset LLM_CHESS_TOKEN on the host.")
    return text


async def play_round(url: str, preferred: list[str], timeout: float, budget: int) -> tuple[str, int]:
    """Play until the game ends, the budget runs out, or the link drops.

    Returns ("done" | "lost", plies_played_this_round). Never raises for a
    transport problem — connecting is inside the guard too, because a reconnect
    attempt is exactly when the far end is most likely to be unreachable.
    """
    played = 0
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

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
                    return DONE, 0
                print(f"playing as {joined.get('your_color')} in "
                      f"{joined.get('game_id')}", flush=True)

                while played < budget:
                    status = await call("wait_for_turn", {"timeout": timeout})
                    released = status.get("released")

                    if released == "game over":
                        print(f"game over: {status.get('result')} — "
                              f"{status.get('result_reason')}", flush=True)
                        return DONE, played
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
                        return DONE, played

                    played += 1
                    print(f"{played:>3}. {out.get('played')} (bot)", flush=True)
    except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
        print(f"\nlink lost after {played} move(s) this round.\n  {_advice(exc)}",
              file=sys.stderr, flush=True)
        return LOST, played

    return DONE, played


async def play(url: str, preferred: list[str], timeout: float, max_plies: int,
               retries: int) -> int:
    total = 0
    attempt = 0

    while total < max_plies:
        status, played = await play_round(url, preferred, timeout, max_plies - total)
        total += played

        if status == DONE:
            print(f"finished after {total} move(s) from this side", flush=True)
            return 0

        attempt += 1
        if attempt > retries:
            print(f"\ngiving up after {retries} reconnect attempt(s); "
                  f"played {total} move(s).\nThe game is unaffected — it lives in "
                  f"the arbiter's store, not in this process.", file=sys.stderr)
            return 1

        delay = min(2 ** attempt, 15)
        print(f"reconnecting in {delay}s (attempt {attempt}/{retries})", flush=True)
        await anyio.sleep(delay)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Play a game against an MCP chess arbiter.")
    ap.add_argument("--url", required=True, help="MCP endpoint, e.g. http://host:port/mcp")
    ap.add_argument("--moves", nargs="*", default=[], help="Preferred moves in SAN, in order.")
    ap.add_argument("--timeout", type=float, default=240.0, help="Per-wait timeout in seconds.")
    ap.add_argument("--max-plies", type=int, default=200, help="Safety cap on moves played.")
    ap.add_argument("--retries", type=int, default=3,
                    help="Reconnect attempts after a dropped link (default 3).")
    args = ap.parse_args()
    return anyio.run(play, args.url, args.moves, args.timeout, args.max_plies, args.retries)


if __name__ == "__main__":
    sys.exit(main())