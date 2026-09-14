"""The arbiter — the only component allowed to advance a game.

Every rule that matters is enforced here, outside both models:

* a client may only move the colour the game assigned it,
* a client may only move on its own turn,
* a move must be legal in the current position,
* a client may not move after the game has ended,
* clocks (when a time control is set) are the arbiter's, not the agent's.

Agents get back a structured error they can recover from; they never get a
half-applied position. The GUI and the PGN exporter read the same rows this
module writes.
"""

from __future__ import annotations

import time
from typing import Any

from . import rules, store


class ArbiterError(Exception):
    """A rule violation or bad request, phrased for an LLM to act on."""


TERMINAL = "finished"


def _require_active(game: dict[str, Any]) -> None:
    if game["status"] == TERMINAL:
        raise ArbiterError(
            f"game {game['id']} is over ({game.get('result') or '*'} — "
            f"{game.get('result_reason') or 'no reason recorded'}). "
            "No further moves are accepted; start a new game instead."
        )


def _require_player(game: dict[str, Any], client: str) -> str:
    color = store.color_for_client(game, client)
    if color is None:
        raise ArbiterError(
            f"'{client}' is a spectator in game {game['id']} "
            f"({game['white_client']} vs {game['black_client']}); it cannot move."
        )
    return color


def _turn(game: dict[str, Any]) -> str:
    import chess

    board = chess.Board(game["fen"])
    return "white" if board.turn else "black"


def start_game(game_id: str | None = None) -> dict[str, Any]:
    """Stamp the moment play actually begins.

    The clocks must run from here, not from when the record was created — a game
    can sit pending for a long time while the driver gets its agents ready.
    """
    game = store.resolve_game(game_id)
    if game["status"] == TERMINAL:
        raise ArbiterError(f"game {game['id']} is already over")
    now = time.time()
    conn = store.connect()
    try:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE games SET status = 'active', started = ?, last_move_ts = ?,"
                " version = version + 1 WHERE id = ?",
                (now, now, game["id"]),
            )
            conn.execute(
                "INSERT INTO events (game_id, ts, kind, text) VALUES (?, ?, 'started', ?)",
                (game["id"], now, f"{game['white_client']} vs {game['black_client']} — play begins"),
            )
    finally:
        conn.close()
    return store.get_game(game["id"]) or game


def join(client: str, game_id: str | None = None) -> dict[str, Any]:
    """Handshake: who am I, what am I playing, and where do we stand?"""
    game = store.resolve_game(game_id, clients=[client])
    color = store.color_for_client(game, client)
    payload: dict[str, Any] = {
        "game_id": game["id"],
        "you": client,
        "your_color": color,
        "opponent": game["black_client"] if color == "white" else game["white_client"],
        "status": game["status"],
        "ply": game["ply"],
        "result": game.get("result"),
        "result_reason": game.get("result_reason"),
        "time_control": game.get("time_control"),
    }
    if color is None:
        payload["note"] = "you are a spectator in this game; use the read-only tools"
    else:
        payload.update(_board_payload(game, color))
    return payload


def _board_payload(game: dict[str, Any], color: str | None) -> dict[str, Any]:
    info = rules.describe(game["fen"])
    turn = info["turn"]
    payload = {
        "fen": info["fen"],
        "turn": turn,
        "board_ascii": info["ascii"],
        "is_check": info["is_check"],
        "is_game_over": info["is_game_over"],
        "outcome": info["outcome"],
        "fullmove": info["fullmove"],
        "halfmove_clock": info["halfmove_clock"],
        "legal_move_count": info["legal_move_count"],
        "claimable_draws": info["can_claim_draw"],
        "material": rules.material_balance(game["fen"]),
        "your_turn": (turn == color) if color else None,
    }
    if game.get("initial_ms"):
        payload["clocks"] = _clocks(game)
    return payload


def _clocks(game: dict[str, Any]) -> dict[str, Any]:
    """Remaining time per side, accounting for time burned since the last move."""
    remaining = {"white": game.get("white_ms"), "black": game.get("black_ms")}
    last = game.get("last_move_ts") or game.get("started") or game.get("created")
    if game["status"] != TERMINAL and last and game.get("initial_ms"):
        burning = _turn(game)
        elapsed = int((time.time() - last) * 1000)
        if remaining.get(burning) is not None:
            remaining[burning] = max(0, remaining[burning] - elapsed)
    return {
        "white_ms": remaining.get("white"),
        "black_ms": remaining.get("black"),
        "increment_ms": game.get("increment_ms") or 0,
    }


def board(client: str, game_id: str | None = None) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    color = store.color_for_client(game, client)
    payload = {
        "game_id": game["id"],
        "status": game["status"],
        "you": client,
        "your_color": color,
        "ply": game["ply"],
        "history_san": [m["san"] for m in store.get_moves(game["id"])],
        **_board_payload(game, color),
    }
    if game["ply"]:
        last = store.get_moves(game["id"])[-1]
        payload["last_move"] = {"san": last["san"], "by": last["client"], "color": last["color"]}
    return payload


def legal_moves(client: str, game_id: str | None = None, *, verbose: bool = False) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    color = store.color_for_client(game, client)
    moves = rules.legal_moves(game["fen"], verbose=verbose)
    return {
        "game_id": game["id"],
        "fen": game["fen"],
        "turn": _turn(game),
        "your_color": color,
        "your_turn": _turn(game) == color,
        "count": len(moves),
        "moves": moves,
    }


def move(
    client: str,
    move_text: str,
    game_id: str | None = None,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    """Validate and commit one move. Atomic: a rejected move changes nothing."""
    game = store.resolve_game(game_id, clients=[client])
    _require_active(game)
    color = _require_player(game, client)

    turn = _turn(game)
    if turn != color:
        raise ArbiterError(
            f"it is {turn}'s turn, not {color}'s. You are playing {color}. "
            "Call get_board to see the current position before moving again."
        )

    _enforce_clock(game)

    if game.get("start_fen") and game["ply"] == 0 and game["start_fen"] != rules.START_FEN:
        pass

    parsed = rules.parse_move(game["fen"], move_text)
    if parsed is None:
        sample = ", ".join(m["san"] for m in rules.legal_moves(game["fen"])[:12])
        raise ArbiterError(
            f"'{move_text}' is not a legal move in this position. "
            f"Legal moves include: {sample}… — call get_legal_moves for the full list."
        )

    applied = rules.apply(game["fen"], parsed)
    now = time.time()

    conn = store.connect()
    try:
        with store.write_tx(conn):
            fresh = conn.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
            if fresh is None:
                raise ArbiterError(f"game {game['id']} vanished")
            fresh = dict(fresh)
            if fresh["fen"] != game["fen"] or fresh["ply"] != game["ply"]:
                raise ArbiterError(
                    "the position changed while you were thinking (another writer won the race) — "
                    "call get_board and move again."
                )
            if fresh["status"] == TERMINAL:
                raise ArbiterError("the game ended while you were thinking")

            think_ms = None
            if fresh.get("last_move_ts"):
                think_ms = int((now - fresh["last_move_ts"]) * 1000)

            conn.execute(
                """
                INSERT INTO moves (game_id, ply, client, color, san, uci, fen_after, ts, think_ms, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fresh["id"], fresh["ply"] + 1, client, color, applied.san,
                 applied.uci, applied.fen_after, now, think_ms, comment),
            )

            white_ms, black_ms = fresh.get("white_ms"), fresh.get("black_ms")
            if fresh.get("initial_ms"):
                spent = int((now - (fresh.get("last_move_ts") or fresh.get("started") or fresh["created"])) * 1000)
                budget = white_ms if color == "white" else black_ms
                budget = max(0, (budget or 0) - spent) + (fresh.get("increment_ms") or 0)
                if color == "white":
                    white_ms = budget
                else:
                    black_ms = budget

            conn.execute(
                """
                UPDATE games SET fen = ?, ply = ?, status = 'active',
                    started = COALESCE(started, ?), last_move_ts = ?,
                    white_ms = ?, black_ms = ?, version = version + 1
                WHERE id = ?
                """,
                (applied.fen_after, fresh["ply"] + 1, now, now, white_ms, black_ms, fresh["id"]),
            )
            conn.execute(
                "INSERT INTO events (game_id, ts, kind, client, text) VALUES (?, ?, ?, ?, ?)",
                (fresh["id"], now, "move", client,
                 f"{applied.san}" + (" #" if applied.is_checkmate else " +" if applied.is_check else "")),
            )
    finally:
        conn.close()

    result = _maybe_finish(game["id"])
    return {
        "game_id": game["id"],
        "accepted": True,
        "played": applied.san,
        "played_uci": applied.uci,
        "by": client,
        "color": color,
        "is_check": applied.is_check,
        "is_checkmate": applied.is_checkmate,
        "fen": applied.fen_after,
        "board_ascii": str(__import__("chess").Board(applied.fen_after)),
        **(result or {}),
        "next_to_move": _turn(store.get_game(game["id"]) or game),
    }


def _enforce_clock(game: dict[str, Any]) -> None:
    if not game.get("initial_ms") or game["status"] == TERMINAL:
        return
    clocks = _clocks(game)
    turn = _turn(game)
    remaining = clocks["white_ms"] if turn == "white" else clocks["black_ms"]
    if remaining is not None and remaining <= 0:
        loser = turn
        winner = "black" if loser == "white" else "white"
        _finish(
            game["id"],
            result="1-0" if winner == "white" else "0-1",
            reason=f"{loser} lost on time",
        )
        raise ArbiterError(
            f"{loser} ran out of time — the game is over and "
            f"{winner} wins. result={('1-0' if winner == 'white' else '0-1')}"
        )


def _maybe_finish(game_id: str) -> dict[str, Any] | None:
    """Close the game out when the position is terminal."""
    game = store.get_game(game_id)
    if not game or game["status"] == TERMINAL:
        return None
    info = rules.describe(game["fen"])
    outcome = info.get("outcome")
    if not outcome:
        return None
    _finish(game_id, result=outcome["result"], reason=outcome.get("reason", "game over"))
    return {
        "game_over": True,
        "result": outcome["result"],
        "result_reason": outcome.get("reason"),
        "winner": outcome.get("winner"),
    }


def _finish(game_id: str, *, result: str, reason: str) -> None:
    conn = store.connect()
    try:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE games SET status = 'finished', finished = ?, result = ?,"
                " result_reason = ?, version = version + 1 WHERE id = ? AND status != 'finished'",
                (time.time(), result, reason, game_id),
            )
            conn.execute(
                "INSERT INTO events (game_id, ts, kind, text) VALUES (?, ?, 'game_over', ?)",
                (game_id, time.time(), f"{result} — {reason}"),
            )
    finally:
        conn.close()


def adjudicate(game_id: str, result: str, reason: str) -> dict[str, Any]:
    """End a game without a player action (timeout, no-show, house rule).

    Kept separate from ``resign``/``claim_draw`` because the driver sometimes has
    to close a game out that no agent asked to end — and the reason should read
    as a ruling, not as one player's decision.
    """
    game = store.get_game(game_id)
    if not game:
        raise ArbiterError(f"no such game: {game_id}")
    if game["status"] == TERMINAL:
        return {"game_id": game_id, "result": game["result"], "already_over": True}
    _finish(game_id, result=result, reason=reason)
    store.add_event(game_id, "adjudicated", text=f"{result} — {reason}")
    return {"game_id": game_id, "result": result, "result_reason": reason}


def resign(client: str, game_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    _require_active(game)
    color = _require_player(game, client)
    winner = "black" if color == "white" else "white"
    result = "0-1" if color == "white" else "1-0"
    _finish(game["id"], result=result, reason=f"{color} resigned" + (f" ({reason})" if reason else ""))
    store.add_event(game["id"], "resign", client=client, text=reason or f"{color} resigned")
    return {"game_id": game["id"], "resigned": color, "result": result, "winner": winner}


def claim_draw(client: str, game_id: str | None = None, rule: str = "threefold repetition") -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    _require_active(game)
    _require_player(game, client)
    import chess

    board = chess.Board(game["fen"])
    ok = (rule.startswith("threefold") and board.can_claim_threefold_repetition()) or (
        rule.startswith("fifty") and board.can_claim_fifty_moves()
    )
    if not ok:
        raise ArbiterError(
            f"cannot claim a draw by {rule} in this position. "
            f"Claimable right now: {rules.describe(game['fen'])['can_claim_draw'] or 'none'}"
        )
    _finish(game["id"], result="1/2-1/2", reason=f"draw claimed by {client} ({rule})")
    store.add_event(game["id"], "draw_claimed", client=client, text=rule)
    return {"game_id": game["id"], "result": "1/2-1/2", "reason": rule}


def offer_draw(client: str, game_id: str | None = None) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    _require_active(game)
    _require_player(game, client)
    store.add_event(game["id"], "draw_offered", client=client, text=f"{client} offers a draw")
    return {"game_id": game["id"], "offered_by": client, "note": "waiting for the opponent to accept_draw"}


def accept_draw(client: str, game_id: str | None = None) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    _require_active(game)
    _require_player(game, client)
    offers = [e for e in store.events_since(game["id"]) if e["kind"] == "draw_offered"]
    if not offers or offers[-1]["client"] == client:
        raise ArbiterError(
            "there is no outstanding draw offer from your opponent to accept"
        )
    _finish(game["id"], result="1/2-1/2", reason="draw agreed")
    store.add_event(game["id"], "draw_agreed", client=client, text="draw agreed")
    return {"game_id": game["id"], "result": "1/2-1/2", "reason": "draw agreed"}


def status(client: str, game_id: str | None = None) -> dict[str, Any]:
    game = store.resolve_game(game_id, clients=[client])
    color = store.color_for_client(game, client)
    return {
        "game_id": game["id"],
        "status": game["status"],
        "result": game.get("result"),
        "result_reason": game.get("result_reason"),
        "ply": game["ply"],
        "turn": _turn(game),
        "your_color": color,
        "your_turn": _turn(game) == color,
        "fen": game["fen"],
        "opponent": game["black_client"] if color == "white" else game["white_client"],
        "clocks": _clocks(game) if game.get("initial_ms") else None,
    }


def pgn(game_id: str | None = None) -> str:
    game = store.get_game(game_id)
    if not game:
        raise ArbiterError("no game to export")
    return rules.to_pgn(game, store.get_moves(game["id"]))