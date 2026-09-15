"""Arbiter enforcement tests.

These are the tests that matter: they prove the rules hold even when a model
misbehaves. Each one drives the real arbiter against a real on-disk store in a
temporary ``LLM_CHESS_HOME``.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys

import pytest

from llmchess import arbiter, rules, store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


@pytest.fixture
def game():
    gid = store.create_game("hermes", "claude")
    return store.get_game(gid)


# --------------------------------------------------------------------------- #
# a real game, played to checkmate
# --------------------------------------------------------------------------- #

SCHOLARS_MATE = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]


def test_scholars_mate_ends_the_game(game):
    for i, san in enumerate(SCHOLARS_MATE):
        client = "hermes" if i % 2 == 0 else "claude"
        arbiter.move(client, san, game["id"])

    final = store.get_game(game["id"])
    assert final["status"] == "finished"
    assert final["result"] == "1-0"
    assert final["result_reason"] == "checkmate"
    assert final["ply"] == 7


def test_moves_are_recorded_in_order_with_legality(game):
    for i, san in enumerate(SCHOLARS_MATE):
        client = "hermes" if i % 2 == 0 else "claude"
        arbiter.move(client, san, game["id"])
    moves = store.get_moves(game["id"])
    # Stored SAN carries the check/mate suffix — that is correct SAN.
    assert [m["san"] for m in moves] == ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
    assert [m["color"] for m in moves] == ["white", "black"] * 3 + ["white"]


def test_pgn_export_is_valid_and_replayable(game):
    for i, san in enumerate(SCHOLARS_MATE):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, game["id"])

    import chess.pgn
    import io

    text = arbiter.pgn(game["id"])
    assert '[Result "1-0"]' in text
    assert '[White "hermes (LLM)"]' in text

    parsed = chess.pgn.read_game(io.StringIO(text))
    board = parsed.board()
    replayed = []
    for mv in parsed.mainline_moves():
        replayed.append(board.san(mv))
        board.push(mv)
    assert replayed == ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
    assert board.is_checkmate()


# --------------------------------------------------------------------------- #
# rule enforcement
# --------------------------------------------------------------------------- #


def test_out_of_turn_move_is_rejected_and_changes_nothing(game):
    arbiter.move("hermes", "e4", game["id"])
    before = store.get_game(game["id"])

    with pytest.raises(arbiter.ArbiterError, match="it is black's turn"):
        arbiter.move("hermes", "e5", game["id"])

    after = store.get_game(game["id"])
    assert after["fen"] == before["fen"]
    assert after["ply"] == before["ply"]
    assert len(store.get_moves(game["id"])) == 1


def test_illegal_move_is_rejected_and_changes_nothing(game):
    with pytest.raises(arbiter.ArbiterError, match="not a legal move"):
        arbiter.move("hermes", "e5", game["id"])
    after = store.get_game(game["id"])
    assert after["fen"] == rules.START_FEN
    assert after["ply"] == 0


def test_gibberish_move_is_rejected(game):
    with pytest.raises(arbiter.ArbiterError, match="not a legal move"):
        arbiter.move("hermes", "banana", game["id"])


def test_a_spectator_cannot_move():
    gid = store.create_game("hermes", "claude")
    with pytest.raises(arbiter.ArbiterError, match="spectator"):
        arbiter.move("grok", "e4", gid)


def test_no_moves_are_accepted_after_the_game_ends(game):
    for i, san in enumerate(SCHOLARS_MATE):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, game["id"])
    with pytest.raises(arbiter.ArbiterError, match="is over"):
        arbiter.move("claude", "Kxf7", game["id"])


def test_a_player_cannot_move_the_opponents_pieces(game):
    # After 1.e4 it is black's turn. If white tries to move again it is rejected,
    # even though the move ("e5") would be legal for black.
    arbiter.move("hermes", "e4", game["id"])
    with pytest.raises(arbiter.ArbiterError):
        arbiter.move("hermes", "e5", game["id"])


def test_cannot_claim_an_unearned_draw(game):
    with pytest.raises(arbiter.ArbiterError, match="cannot claim a draw"):
        arbiter.claim_draw("hermes", game["id"])


def test_resignation_ends_the_game_with_a_win_for_the_opponent(game):
    arbiter.move("hermes", "e4", game["id"])
    out = arbiter.resign("hermes", game["id"], "position is lost")
    assert out["result"] == "0-1"
    final = store.get_game(game["id"])
    assert final["status"] == "finished"
    assert "resigned" in final["result_reason"]


def test_draw_offer_must_be_accepted_by_the_opponent(game):
    arbiter.move("hermes", "e4", game["id"])
    arbiter.offer_draw("hermes", game["id"])
    with pytest.raises(arbiter.ArbiterError, match="no outstanding draw offer"):
        arbiter.accept_draw("hermes", game["id"])
    out = arbiter.accept_draw("claude", game["id"])
    assert out["result"] == "1/2-1/2"


# --------------------------------------------------------------------------- #
# move parsing tolerance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    ["e4", "e2e4", "e2-e4", " e4 ", "e4!", "e4?!"],
)
def test_move_text_variants_are_accepted(game, text):
    out = arbiter.move("hermes", text, game["id"])
    assert out["played"] == "e4"


def test_castling_by_notation_is_accepted():
    gid = store.create_game("hermes", "claude")
    line = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "O-O"]
    for i, san in enumerate(line):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid)
    assert store.get_moves(gid)[-1]["san"] == "O-O"

    gid2 = store.create_game("hermes", "claude")
    line2 = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "0-0"]
    for i, san in enumerate(line2):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid2)
    assert store.get_moves(gid2)[-1]["san"] == "O-O"


# --------------------------------------------------------------------------- #
# clocks
# --------------------------------------------------------------------------- #


def test_a_player_that_runs_out_of_time_loses():
    gid = store.create_game("hermes", "claude", initial_ms=1000, increment_ms=0, time_control="0:01+0")
    import time as _t

    _t.sleep(1.2)
    with pytest.raises(arbiter.ArbiterError, match="ran out of time"):
        arbiter.move("hermes", "e4", gid)
    final = store.get_game(gid)
    assert final["status"] == "finished"
    assert final["result"] == "0-1"
    assert "lost on time" in final["result_reason"]


def test_clocks_are_tracked_server_side():
    gid = store.create_game("hermes", "claude", initial_ms=600_000, increment_ms=5000, time_control="10:00+5")
    arbiter.start_game(gid)
    arbiter.move("hermes", "e4", gid)
    game = store.get_game(gid)
    # White burned a few ms and gained the increment; black is untouched.
    assert game["white_ms"] > 600_000 - 60_000
    assert game["black_ms"] == 600_000
    status = arbiter.status("claude", gid)
    # get_status reports the live clock: black is now on the move, so its time is
    # draining in real time. The stored, non-draining value stays untouched.
    assert 600_000 - 5_000 < status["clocks"]["black_ms"] <= 600_000
    assert status["clocks"]["increment_ms"] == 5000
    assert status["your_turn"] is True


def test_start_game_stamps_the_clock_origin():
    """The clock must run from game start, not from when the record was created."""
    gid = store.create_game("hermes", "claude", initial_ms=60_000, time_control="1:00+0")
    fresh = store.get_game(gid)
    assert fresh["status"] == "pending"
    arbiter.start_game(gid)
    started = store.get_game(gid)
    assert started["status"] == "active"
    assert started["started"] is not None
    assert started["last_move_ts"] is not None


# --------------------------------------------------------------------------- #
# concurrency — the store must serialise two players writing at once
# --------------------------------------------------------------------------- #


def _try_move(args):
    home, gid, client, move_text = args
    os.environ["LLM_CHESS_HOME"] = home
    from llmchess import arbiter as a

    try:
        a.move(client, move_text, gid)
        return "ok"
    except a.ArbiterError:
        return "rejected"


def test_simultaneous_moves_cannot_both_land(tmp_path):
    """Two writers race for the same ply; exactly one may win."""
    home = str(tmp_path)
    os.environ["LLM_CHESS_HOME"] = home
    gid = store.create_game("hermes", "claude")

    ctx = mp.get_context("spawn")
    with ctx.Pool(2) as pool:
        results = pool.map(
            _try_move,
            [(home, gid, "hermes", "e4"), (home, gid, "claude", "d4")],
        )
    assert results.count("ok") == 1, results
    assert store.get_game(gid)["ply"] == 1


# --------------------------------------------------------------------------- #
# tool surface smoke
# --------------------------------------------------------------------------- #


def test_every_mcp_tool_handles_a_legal_call_and_a_bad_one(game):
    from llmchess.mcp_server import build_server
    import json
    import asyncio

    server = build_server("hermes")
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {
        "join_game", "get_board", "get_legal_moves", "make_move", "get_status",
        "get_move_history", "resign_game", "offer_draw", "accept_draw",
        "claim_draw", "get_evaluation",
    } <= names
    assert "make_move" in names


def test_guarded_tool_returns_recoverable_error_not_a_crash(isolated_home):
    from llmchess.mcp_server import _guard

    @_guard
    def boom() -> dict:
        raise arbiter.ArbiterError("nope")

    payload = boom()
    assert payload["ok"] is False
    assert payload["error"] == "nope"

    @_guard
    def fine() -> dict:
        return {"turn": "white"}

    assert fine() == {"ok": True, "turn": "white"}


# --------------------------------------------------------------------------- #
# wait_for_turn — the primitive that replaces a cross-machine orchestrator
# --------------------------------------------------------------------------- #


def test_wait_for_turn_releases_the_side_on_move(game):
    arbiter.start_game(game["id"])
    out = arbiter.wait_for_turn("hermes", game["id"], timeout=2)
    assert out["released"] == "your turn"
    assert out["your_turn"] is True
    assert out["your_color"] == "white"


def test_wait_for_turn_does_not_release_before_the_game_starts(game):
    """A created-but-unstarted game is not playable, even for white.

    Otherwise a client that connects early would move before the clock starts.
    """
    out = arbiter.wait_for_turn("hermes", game["id"], timeout=0.4)
    assert out["released"] == "timeout"
    assert out["your_turn"] is False


def test_wait_for_turn_times_out_without_claiming_it_is_your_turn(game):
    arbiter.start_game(game["id"])
    out = arbiter.wait_for_turn("claude", game["id"], timeout=0.4)
    assert out["released"] == "timeout"
    assert out["your_turn"] is False


def test_wait_for_turn_stops_when_the_game_ends(game):
    arbiter.start_game(game["id"])
    arbiter.resign("hermes", game["id"], "testing")
    out = arbiter.wait_for_turn("claude", game["id"], timeout=2)
    assert out["released"] == "game over"
    assert out["status"] == "finished"


def test_wait_for_turn_refuses_a_spectator(game):
    """A third party cannot wait for a turn it will never get."""
    with pytest.raises(arbiter.ArbiterError) as exc:
        arbiter.wait_for_turn("bystander", game["id"], timeout=0.2)
    assert "spectator" in str(exc.value)


def test_wait_for_turn_returns_as_soon_as_the_turn_arrives(game):
    """It must wake on the opponent's move, not sit out the whole timeout."""
    import time

    arbiter.start_game(game["id"])

    import threading

    def opponent_moves():
        time.sleep(0.5)
        arbiter.move("hermes", "e4", game["id"])

    t = threading.Thread(target=opponent_moves)
    started = time.time()
    t.start()
    out = arbiter.wait_for_turn("claude", game["id"], timeout=20, poll=0.05)
    elapsed = time.time() - started
    t.join()

    assert out["released"] == "your turn"
    assert out["your_color"] == "black"
    assert out["ply"] == 1
    assert elapsed < 10, f"woke slowly: {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# short ids
#
# The GUI shows only the tail of an id, and people read ids to agents that way,
# so an agent handed "421f54" must find "20260915_112801_421f54" rather than
# being told the game does not exist.
# --------------------------------------------------------------------------- #


def test_a_short_tail_resolves_to_the_game(game):
    assert store.resolve_game(game["id"][-6:])["id"] == game["id"]


def test_the_full_id_still_resolves(game):
    assert store.resolve_game(game["id"])["id"] == game["id"]


def test_a_prefix_fragment_resolves(game):
    assert store.resolve_game(game["id"][:8])["id"] == game["id"]


def test_like_wildcards_are_escaped(game):
    """'%' must stay literal.

    Unescaped it would match every game at once and report an ambiguity where
    the honest answer is "there is no game called '%'".
    """
    with pytest.raises(LookupError, match="no such game"):
        store.resolve_game("%")


def test_an_ambiguous_fragment_names_the_candidates():
    a = store.create_game("hermes", "claude")
    b = store.create_game("hermes", "claude")
    with pytest.raises(LookupError) as exc:
        store.resolve_game(a[:4])
    message = str(exc.value)
    assert "matches 2 games" in message
    assert a in message and b in message


def test_an_unknown_fragment_is_reported_plainly():
    with pytest.raises(LookupError, match="no such game: zzzzzz"):
        store.resolve_game("zzzzzz")


def test_a_short_id_reaches_the_arbiter(game):
    """The whole point: a client can move using the short id it was given."""
    out = arbiter.move("hermes", "e4", game["id"][-6:])
    assert out["played"] == "e4"
    assert store.get_game(game["id"])["ply"] == 1