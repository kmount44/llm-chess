"""Live spectator GUI.

A local-only web app that reads the same store the arbiter writes and pushes
changes to the browser over a WebSocket. It is deliberately read-mostly: the
only thing it can change is creating a new game, because the arbiter must stay
the only component that can touch a position in progress.

The very first connected browser tab is *the* spectator view; nothing here is
required for a game to run, so the GUI can be closed and reopened mid-game.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import arbiter, rules, store

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="llm-chess spectator", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# --------------------------------------------------------------------------- #
# read API
# --------------------------------------------------------------------------- #


def _snapshot(game_id: str) -> dict[str, Any]:
    game = store.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail=f"no such game: {game_id}")

    moves = store.get_moves(game_id)
    info = rules.describe(game["fen"])
    import chess

    board = chess.Board(game["fen"])

    captured = {"white": [], "black": []}
    replay = chess.Board(game["start_fen"])
    for mv in moves:
        try:
            m = replay.parse_san(mv["san"])
        except ValueError:
            break
        if replay.is_capture(m):
            victim = replay.piece_at(m.to_square)
            if victim is not None:
                captured["white" if victim.color == chess.BLACK else "black"].append(
                    chess.piece_symbol(victim.piece_type)
                )
        replay.push(m)

    return {
        "game": {
            "id": game["id"],
            "status": game["status"],
            "result": game.get("result"),
            "result_reason": game.get("result_reason"),
            "ply": game["ply"],
            "fen": game["fen"],
            "start_fen": game["start_fen"],
            "created": game["created"],
            "started": game.get("started"),
            "finished": game.get("finished"),
            "time_control": game.get("time_control"),
            "version": game["version"],
            "white_client": game["white_client"],
            "black_client": game["black_client"],
            "notes": game.get("notes"),
        },
        "position": {
            "fen": game["fen"],
            "turn": "white" if board.turn else "black",
            "is_check": board.is_check(),
            "is_game_over": board.is_game_over(claim_draw=True),
            "fullmove": board.fullmove_number,
            "halfmove_clock": board.halfmove_clock,
            "legal_move_count": board.legal_moves.count(),
            "last_move": _last_move_uci(board, moves),
            "captured": captured,
        },
        "moves": [
            {
                "ply": m["ply"],
                "san": m["san"],
                "uci": m["uci"],
                "color": m["color"],
                "client": m["client"],
                "ts": m["ts"],
                "think_ms": m.get("think_ms"),
                "comment": m.get("comment"),
            }
            for m in moves
        ],
        "agents": store.get_agents(game_id),
        "clock": _clock_view(game),
        "events": store.events_since(game_id)[-200:],
    }


def _last_move_uci(board: chess.Board, moves: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not moves:
        return None
    last = moves[-1]
    return {"from": last["uci"][:2], "to": last["uci"][2:4], "san": last["san"]}


def _clock_view(game: dict[str, Any]) -> dict[str, Any] | None:
    if not game.get("initial_ms"):
        return None
    live = arbiter._clocks(game)  # the arbiter owns clock arithmetic
    return {
        "white_ms": live["white_ms"],
        "black_ms": live["black_ms"],
        "increment_ms": live["increment_ms"],
        "initial_ms": game["initial_ms"],
        "running": game["status"] == "active",
        "at": time.time(),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/games")
def api_games() -> JSONResponse:
    return JSONResponse({"games": store.list_games()})


@app.get("/api/game/{game_id}")
def api_game(game_id: str) -> JSONResponse:
    return JSONResponse(_snapshot(game_id))


@app.get("/api/active")
def api_active() -> JSONResponse:
    gid = store.active_game_id()
    if not gid or not store.get_game(gid):
        return JSONResponse({"game": None})
    return JSONResponse(_snapshot(gid))


@app.get("/api/game/{game_id}/pgn")
def api_pgn(game_id: str) -> PlainTextResponse:
    game = store.get_game(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="no such game")
    return PlainTextResponse(rules.to_pgn(game, store.get_moves(game_id)))


class NewGame(BaseModel):
    white: str = "hermes"
    black: str = "claude"
    initial_ms: int | None = None
    increment_ms: int = 0
    time_control: str | None = None
    start_fen: str | None = None
    autostart: bool = True
    notes: str | None = None


@app.post("/api/game")
def api_new_game(req: NewGame) -> JSONResponse:
    """Create a game. Optionally kick off the driver so the match plays itself."""
    try:
        gid = store.create_game(
            req.white,
            req.black,
            start_fen=req.start_fen,
            time_control=req.time_control,
            initial_ms=req.initial_ms,
            increment_ms=req.increment_ms,
            notes=req.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad start position: {exc}") from exc

    started = False
    if req.autostart and not _driver_running():
        _spawn_driver(gid)
        started = True
    return JSONResponse({"game_id": gid, "driver_started": started})


@app.get("/api/driver")
def api_driver() -> JSONResponse:
    proc = _DRIVER.get("proc")
    return JSONResponse(
        {
            "running": _driver_running(),
            "game_id": _DRIVER.get("game_id"),
            "pid": proc.pid if proc else None,
        }
    )


# --------------------------------------------------------------------------- #
# driver process management (the GUI can start a match; it never runs one)
# --------------------------------------------------------------------------- #

_DRIVER: dict[str, Any] = {"proc": None, "game_id": None}


def _driver_running() -> bool:
    proc = _DRIVER.get("proc")
    return bool(proc and proc.poll() is None)


def _spawn_driver(game_id: str, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, "-m", "llmchess.driver", "--game-id", game_id, "--no-gui"]
    if extra:
        cmd.extend(extra)
    env = {**os.environ}
    log = store.home() / "games"
    log.mkdir(parents=True, exist_ok=True)
    handle = open(log / f"{game_id}.driver.log", "a", buffering=1)  # noqa: SIM115
    _DRIVER["proc"] = subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT)
    _DRIVER["game_id"] = game_id
    store.add_event(game_id, "driver_started", text=f"driver pid {_DRIVER['proc'].pid}")


# --------------------------------------------------------------------------- #
# live socket
# --------------------------------------------------------------------------- #


@app.websocket("/ws/game/{game_id}")
async def ws_game(ws: WebSocket, game_id: str) -> None:
    await ws.accept()
    last_version = -1
    last_event = 0
    try:
        while True:
            try:
                snap = _snapshot(game_id)
            except HTTPException:
                await ws.send_text(json.dumps({"type": "gone", "game_id": game_id}))
                await asyncio.sleep(0.5)
                continue

            version = snap["game"]["version"]
            new_events = [e for e in snap["events"] if e["id"] > last_event]
            if version != last_version or new_events:
                last_version = version
                if new_events:
                    last_event = max(e["id"] for e in new_events)
                await ws.send_text(json.dumps({
                    "type": "state",
                    "state": snap,
                    "new_events": new_events,
                }, default=str))
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


@app.websocket("/ws/active")
async def ws_active(ws: WebSocket) -> None:
    """Follow whichever game is active — used by the GUI's default view."""
    await ws.accept()
    try:
        while True:
            gid = store.active_game_id()
            if gid and store.get_game(gid):
                await ws.send_text(json.dumps({"type": "active", "game_id": gid}))
                await ws.close()
                return
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, RuntimeError):
        return


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chess-gui", description="llm-chess spectator GUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    print(f"llm-chess spectator → http://{args.host}:{args.port}", flush=True)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())