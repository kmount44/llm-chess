"""Chess rules layer.

Thin, deliberate wrapper around ``python-chess`` so the rest of the codebase
never touches the library's API directly. Everything here is pure: no store, no
I/O, no clocks. The arbiter owns sequencing; this module only answers "is this
legal, and what is the position now?".
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import chess
import chess.pgn

START_FEN = chess.STARTING_FEN

# Terminal outcomes that are not wins for a side.
DRAW_REASONS = {
    chess.Termination.STALEMATE: "stalemate",
    chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material",
    chess.Termination.SEVENTYFIVE_MOVES: "seventy-five move rule",
    chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition",
    chess.Termination.FIFTY_MOVES: "fifty move rule",
    chess.Termination.THREEFOLD_REPETITION: "threefold repetition",
}

WIN_REASONS = {
    chess.Termination.CHECKMATE: "checkmate",
}


def Board_from_fen(fen: str) -> chess.Board:  # noqa: N802 - reads as a constructor
    board = chess.Board(fen)
    return board


@dataclass
class AppliedMove:
    uci: str
    san: str
    fen_after: str
    is_check: bool
    is_checkmate: bool


def describe(fen: str) -> dict[str, Any]:
    """Everything a player is allowed to know about the position."""
    board = chess.Board(fen)
    legal = list(board.legal_moves)
    outcome = board.outcome(claim_draw=True)
    return {
        "fen": board.fen(),
        "turn": "white" if board.turn else "black",
        "fullmove": board.fullmove_number,
        "halfmove_clock": board.halfmove_clock,
        "legal_move_count": len(legal),
        "is_check": board.is_check(),
        "is_game_over": board.is_game_over(claim_draw=True),
        "outcome": _outcome_payload(outcome),
        "can_claim_draw": _claimable_draw(board),
        "castling": {
            "white": board.has_kingside_castling_rights(chess.WHITE),
            "white_queenside": board.has_queenside_castling_rights(chess.WHITE),
            "black": board.has_kingside_castling_rights(chess.BLACK),
            "black_queenside": board.has_queenside_castling_rights(chess.BLACK),
        },
        "ascii": str(board),
    }


def legal_moves(fen: str, *, verbose: bool = False) -> list[Any]:
    board = chess.Board(fen)
    if not verbose:
        return [_move_payload(board, m) for m in board.legal_moves]
    return [_move_payload(board, m, with_targets=True) for m in board.legal_moves]


def _move_payload(board: chess.Board, move: chess.Move, *, with_targets: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "uci": move.uci(),
        "san": board.san(move),
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
    }
    if move.promotion:
        payload["promotion"] = chess.piece_symbol(move.promotion)
    if with_targets:
        payload["capture"] = board.is_capture(move)
        payload["check"] = board.gives_check(move)
    return payload


def _claimable_draw(board: chess.Board) -> list[str]:
    claims = []
    if board.can_claim_threefold_repetition():
        claims.append("threefold repetition")
    if board.can_claim_fifty_moves():
        claims.append("fifty move rule")
    return claims


def _outcome_payload(outcome: chess.Outcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    if outcome.winner is None:
        return {"result": "1/2-1/2", "reason": DRAW_REASONS.get(outcome.termination, str(outcome.termination))}
    winner = "white" if outcome.winner == chess.WHITE else "black"
    return {
        "result": "1-0" if winner == "white" else "0-1",
        "winner": winner,
        "reason": WIN_REASONS.get(outcome.termination, str(outcome.termination)),
    }


def parse_move(fen: str, text: str) -> chess.Move | None:
    """Accept SAN ('Nf3'), UCI ('g1f3'), or a hyphenated coordinate ('g1-f3').

    Returns ``None`` when the text is not a legal move in this position. The
    arbiter turns that into an explicit error the agent can recover from rather
    than a crash.
    """
    if not text:
        return None
    board = chess.Board(fen)
    raw = text.strip()
    for candidate in (raw, raw.replace("-", "").replace(" ", "")):
        with_san = candidate
        # Tolerate trailing + / # / !? annotations that models like to add.
        stripped = with_san.rstrip("+#!?")
        for form in {with_san, stripped}:
            try:
                move = board.parse_san(form)
                if move in board.legal_moves:
                    return move
            except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
                pass
        try:
            move = chess.Move.from_uci(candidate)
            if move in board.legal_moves:
                return move
        except (ValueError, chess.InvalidMoveError):
            pass
        # Castling by hand: models sometimes emit O-O / 0-0.
        normalised = candidate.replace("0", "O").upper()
        if normalised.startswith("O-O"):
            side = chess.QUEENSIDE if normalised.startswith("O-O-O") else chess.KINGSIDE
            is_white = board.turn == chess.WHITE
            move = board.parse_san("O-O-O" if side == chess.QUEENSIDE else "O-O") if _can_castle(board, is_white, side) else None
            if move and move in board.legal_moves:
                return move
    return None


def _can_castle(board: chess.Board, is_white: bool, side: int) -> bool:
    if side == chess.KINGSIDE:
        return board.has_kingside_castling_rights(is_white)
    return board.has_queenside_castling_rights(is_white)


def apply(fen: str, move: chess.Move) -> AppliedMove:
    board = chess.Board(fen)
    san = board.san(move)
    board.push(move)
    return AppliedMove(
        uci=move.uci(),
        san=san,
        fen_after=board.fen(),
        is_check=board.is_check(),
        is_checkmate=board.is_checkmate(),
    )


def material_balance(fen: str) -> dict[str, int]:
    board = chess.Board(fen)
    values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    out = {"white": 0, "black": 0}
    for piece_type, value in values.items():
        out["white"] += len(board.pieces(piece_type, chess.WHITE)) * value
        out["black"] += len(board.pieces(piece_type, chess.BLACK)) * value
    return out


def to_pgn(game: dict[str, Any], moves: list[dict[str, Any]]) -> str:
    """Reconstruct a PGN from the recorded move list.

    Replays from the stored start position so an illegal or missing row shows up
    as a PGN that stops early rather than a silently wrong game.
    """
    board = chess.Board(game.get("start_fen") or START_FEN)
    pgn = chess.pgn.Game()
    pgn.headers["Event"] = "LLM Chess (MCP arbiter)"
    pgn.headers["Site"] = "llm-chess"
    pgn.headers["Date"] = _pgn_date(game.get("created"))
    pgn.headers["White"] = f"{game.get('white_client', '?')} (LLM)"
    pgn.headers["Black"] = f"{game.get('black_client', '?')} (LLM)"
    pgn.headers["Result"] = game.get("result") or "*"
    if game.get("time_control"):
        pgn.headers["TimeControl"] = game["time_control"]
    if game.get("start_fen") and game["start_fen"] != START_FEN:
        pgn.headers["SetUp"] = "1"
        pgn.headers["FEN"] = game["start_fen"]
    pgn.headers["Annotator"] = "llm-chess arbiter"
    if game.get("result_reason"):
        pgn.headers["Termination"] = game["result_reason"]

    node = pgn
    for row in moves:
        try:
            move = board.parse_san(row["san"])
        except ValueError:
            break
        node = node.add_variation(move)
        if row.get("comment"):
            node.comment = row["comment"]
        board.push(move)

    exporter = io.StringIO()
    print(pgn, file=exporter, end="\n\n")
    return exporter.getvalue()


def _pgn_date(created: float | None) -> str:
    import time as _time

    tm = _time.localtime(created or _time.time())
    return f"{tm.tm_year:04d}.{tm.tm_mon:02d}.{tm.tm_mday:02d}"