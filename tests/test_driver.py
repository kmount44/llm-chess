"""Driver and agent-adapter tests.

These cover the glue that decides whose turn it is and whether a move actually
landed. The driver trusts only the arbiter's store, so the important assertions
here are about *not* believing an agent that says it moved.
"""

from __future__ import annotations

import pytest

from llmchess import agents as agents_mod
from llmchess import arbiter, driver, store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# scripted opponent
# --------------------------------------------------------------------------- #

LINE = "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#"


def test_scripted_sides_split_one_line_by_colour():
    white = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=LINE, color="white")
    black = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=LINE, color="black")
    assert white.moves == ["e4", "Bc4", "Qh5", "Qxf7#"]
    assert black.moves == ["e5", "Nc6", "Nf6"]


def test_scripted_accepts_a_raw_string_without_splitting_into_characters():
    """A raw string must not be iterated character-by-character."""
    white = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=LINE, color="white")
    assert white.moves[0] == "e4"
    assert all(len(m) >= 2 and " " not in m for m in white.moves)


def test_scripted_agent_plays_through_the_arbiter():
    gid = store.create_game("humanish", "scripted")
    white = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=LINE, color="white")
    black = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=LINE, color="black")
    white.bind_game(gid, "humanish")
    black.bind_game(gid, "scripted")

    for _ in range(7):
        game = store.get_game(gid)
        if game["status"] == "finished":
            break
        mover = white if game["ply"] % 2 == 0 else black
        result = mover.take_turn("ignored")
        assert result.ok, result.error

    final = store.get_game(gid)
    assert final["result"] == "1-0"
    assert final["result_reason"] == "checkmate"


def test_scripted_agent_reports_when_it_runs_out_of_moves():
    gid = store.create_game("humanish", "scripted")
    white = agents_mod.build_agent("scripted", workdir=".", per_move_seconds=10,
                                   script=["e4"], color="white")
    white.bind_game(gid, "humanish")
    assert white.take_turn("x").ok is True
    second = white.take_turn("x")
    assert second.ok is False
    assert "out of scripted moves" in second.error


# --------------------------------------------------------------------------- #
# the driver only believes the store
# --------------------------------------------------------------------------- #


class Liar(agents_mod.Agent):
    """Claims success, never touches the arbiter."""

    name = "liar"

    def _argv(self, prompt: str) -> list[str]:
        return ["true"]

    def take_turn(self, prompt: str) -> agents_mod.TurnResult:
        return agents_mod.TurnResult(ok=True, reply="I definitely moved.")


def test_driver_rejects_an_agent_that_claims_a_move_it_never_made(monkeypatch):
    """A player saying "done" is not a move. Only the arbiter can confirm one."""
    args = driver.build_parser().parse_args(["--white", "hermes", "--black", "claude", "--no-gui"])
    d = driver.Driver(args)
    gid = store.create_game("hermes", "claude")
    d.game_id = gid
    d.players = {
        "hermes": Liar(workdir=".", per_move_seconds=1),
        "claude": Liar(workdir=".", per_move_seconds=1),
    }
    d.play()

    final = store.get_game(gid)
    assert final["ply"] == 0
    assert final["status"] == "finished"
    assert "failed to move" in final["result_reason"]
    assert final["result"] == "0-1"  # white forfeited


def test_driver_stop_flag_ends_the_loop():
    args = driver.build_parser().parse_args(["--white", "hermes", "--black", "claude", "--no-gui"])
    d = driver.Driver(args)
    gid = store.create_game("hermes", "claude")
    d.game_id = gid
    d.players = {"hermes": Liar(workdir=".", per_move_seconds=1)}
    d.stopped = True
    d.play()  # returns immediately rather than spinning
    assert store.get_game(gid)["status"] != "finished"


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #


def test_prompt_tells_the_agent_its_colour_game_id_and_tools():
    gid = store.create_game("hermes", "claude")
    game = store.get_game(gid)
    prompt = driver.build_prompt(game, "hermes", "white", first=True, legal_count=20)
    assert gid in prompt
    assert "as white" in prompt
    assert "against claude" in prompt
    assert "make_move" in prompt
    assert "get_board" in prompt
    assert "This is the opening move" in prompt


def test_follow_up_prompt_carries_the_move_history():
    gid = store.create_game("hermes", "claude")
    arbiter.move("hermes", "e4", gid)
    arbiter.move("claude", "e5", gid)
    game = store.get_game(gid)
    prompt = driver.build_prompt(game, "hermes", "white", first=False, legal_count=30)
    assert "1.e4" in prompt
    assert "1...e5" in prompt
    assert "e5" in prompt
    assert "opening move" not in prompt


def test_nudge_prompt_is_explicit_about_not_having_moved():
    gid = store.create_game("hermes", "claude")
    game = store.get_game(gid)
    prompt = driver.build_nudge(game, "white")
    assert gid in prompt
    assert "No move has been registered" in prompt
    assert "make_move" in prompt


# --------------------------------------------------------------------------- #
# time control parsing
# --------------------------------------------------------------------------- #


def test_time_control_parsing():
    assert driver._parse_tc(None) == (None, 0, None)
    assert driver._parse_tc("10+5") == (600_000, 5000, "10+5")
    assert driver._parse_tc("1+0") == (60_000, 0, "1+0")


def test_bad_time_control_is_rejected():
    with pytest.raises(SystemExit):
        driver._parse_tc("ten minutes")


# --------------------------------------------------------------------------- #
# agent adapters
# --------------------------------------------------------------------------- #


def test_hermes_session_id_is_read_from_stderr():
    """`hermes chat -Q` prints the session id on stderr, the answer on stdout.

    Missing this silently downgraded every game to stateless play: the moves
    still landed, so nothing looked broken.
    """
    a = agents_mod.HermesAgent(workdir=".", per_move_seconds=10)
    result = a._parse(
        "Played 1.e4 as white in game 20260914_012206_7f90d6; it's black's turn.\n",
        "\nsession_id: 20260914_012215_484abd\n",
    )
    assert result.ok is True
    assert result.session_id == "20260914_012215_484abd"
    # The game id in the reply must not be mistaken for the session id.
    assert "20260914_012206_7f90d6" not in (result.session_id or "")


def test_hermes_resume_flag_is_only_added_once_a_session_exists():
    a = agents_mod.HermesAgent(workdir=".", per_move_seconds=10)
    assert "--resume" not in a._argv("hello")
    a.session_id = "20260914_012215_484abd"
    argv = a._argv("hello")
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "20260914_012215_484abd"


def test_hermes_argv_uses_the_chat_subcommand_for_its_flags():
    """-Q/--max-turns are chat flags; at the top level they fail argument parsing."""
    a = agents_mod.HermesAgent(workdir=".", per_move_seconds=30)
    argv = a._argv("hello")
    assert argv[1] == "chat"
    assert "-q" in argv and "-Q" in argv
    assert "--max-turns" in argv
    # The prompt must never be swallowed as a flag value.
    assert argv[argv.index("-q") + 1] == "hello"


def test_hermes_usage_errors_are_surfaced_not_treated_as_a_silent_no_show():
    a = agents_mod.HermesAgent(workdir=".", per_move_seconds=10)
    result = a._parse("", "usage: hermes [-h]\nhermes: error: argument command: invalid choice: '14'\n")
    assert result.ok is False
    assert result.error and "invalid choice" in result.error


def test_claude_session_id_is_read_from_json_output():
    a = agents_mod.ClaudeAgent(workdir=".", per_move_seconds=10)
    result = a._parse('{"result": "Played e4.", "session_id": "abc-123"}', "")
    assert result.ok is True
    assert result.session_id == "abc-123"
    assert result.reply == "Played e4."


def test_claude_json_error_payload_is_not_reported_as_success():
    a = agents_mod.ClaudeAgent(workdir=".", per_move_seconds=10)
    result = a._parse('{"result": "rate limited", "is_error": true, "session_id": "abc"}', "")
    assert result.ok is False
    assert "rate limited" in (result.error or "")


def test_claude_non_json_output_does_not_crash_the_parser():
    a = agents_mod.ClaudeAgent(workdir=".", per_move_seconds=10)
    result = a._parse("Played e4.", "")
    assert result.ok is True
    assert "e4" in result.reply


def test_agent_build_rejects_an_unknown_player():
    with pytest.raises(ValueError):
        agents_mod.build_agent("grok", workdir=".", per_move_seconds=10)


# --------------------------------------------------------------------------- #
# artefacts
# --------------------------------------------------------------------------- #


def test_artifacts_are_written_with_the_real_result():
    gid = store.create_game("hermes", "claude")
    for i, san in enumerate(["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid)
    store.add_event(gid, "agent_message", client="hermes", text="A clean finish.")

    out = driver.write_artifacts(gid)
    assert "pgn" in out and "log" in out

    pgn = (open(out["pgn"])).read()
    assert "Qxf7#" in pgn and '[Result "1-0"]' in pgn

    log = open(out["log"]).read()
    assert "A clean finish." in log
    assert "checkmate" in log