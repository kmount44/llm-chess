"""Tests for scripts/play-claude.sh.

This script drives Claude Code as one side of a networked match, so it is
driven here with a stub `claude` on PATH that emits the same JSON shape the
real CLI does. That keeps the loop's own logic — session resumption, turn
counting, game-over handling — under test without needing a model or a live
arbiter.

The bash 3.2 test at the bottom is not decoration. macOS ships bash 3.2 as
/bin/bash, and there `"${arr[@]}"` on an *empty* array under `set -u` is fatal
("unbound variable"); bash 4.4+ made it legal, so a Linux box cannot reproduce
the failure by running the script normally. It is run inside a bash:3.2
container instead, which is the only way this stays caught.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "play-claude.sh"


def _write_stub(tmp_path: Path, replies: list[dict], argv_log: Path) -> Path:
    """A fake `claude` that logs its argv and emits the given replies in order.

    The real CLI is invoked once per turn and prints a JSON object with a
    session_id and a result; the stub does the same, so the script's parsing and
    resume behaviour are exercised for real.
    """
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"LOG = {str(argv_log)!r}\n"
        "with open(LOG, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "n = sum(1 for _ in open(LOG))\n"
        f"REPLIES = {replies!r}\n"
        "print(json.dumps(REPLIES[min(n - 1, len(REPLIES) - 1)]))\n"
    )
    stub.chmod(0o755)
    return stub


def _run(stub: Path, game_id: str, max_moves: str, **extra_env):
    env = {
        **os.environ,
        "LLM_CHESS_CLAUDE_BIN": str(stub),
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), game_id, max_moves],
        capture_output=True, text=True, env=env, timeout=180,
    )


def test_first_turn_does_not_resume_and_later_turns_do(tmp_path):
    """A session id only exists after the first turn, so turn one has no --resume.

    That empty first-turn argv is exactly the case that killed the script on
    macOS, so it is asserted directly rather than left implicit.
    """
    argv_log = tmp_path / "argv.log"
    stub = _write_stub(
        tmp_path,
        [
            {"session_id": "sess-abc", "result": "played e5"},
            {"session_id": "sess-abc", "result": "played Nc6"},
        ],
        argv_log,
    )

    proc = _run(stub, "GAME1", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert len(calls) == 2, calls
    assert "--resume" not in calls[0], "turn 1 must not resume a session that does not exist"
    assert "--resume" in calls[1] and "sess-abc" in calls[1], (
        "turn 2 must carry the session id so the model keeps its memory of the game"
    )
    assert "claude session: sess-abc" in proc.stdout


def test_game_over_ends_the_loop(tmp_path):
    argv_log = tmp_path / "argv.log"
    stub = _write_stub(
        tmp_path,
        [{"session_id": "s1", "result": "GAME OVER white — checkmate"}],
        argv_log,
    )

    proc = _run(stub, "GAME2", "50")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "finished:" in proc.stdout
    # It must stop on the arbiter's verdict rather than burning the whole budget.
    assert len(argv_log.read_text().splitlines()) == 1


def test_script_passes_the_game_id_to_claude(tmp_path):
    argv_log = tmp_path / "argv.log"
    stub = _write_stub(tmp_path, [{"session_id": "s1", "result": "ok"}], argv_log)

    proc = _run(stub, "20260915_093311_0055be", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    (call,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert "20260915_093311_0055be" in " ".join(call), "the prompt must name the game"


def test_no_tool_whitelist_by_default(tmp_path):
    """No --allowedTools unless asked for.

    Claude Code treats it as a whitelist and hides tools that do not match, so
    the model reports the chess tools as missing. Passing a guessed pattern
    blind is worse than passing none.
    """
    argv_log = tmp_path / "argv.log"
    stub = _write_stub(tmp_path, [{"session_id": "s1", "result": "ok"}], argv_log)

    proc = _run(stub, "GAME3", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    (call,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert "--allowedTools" not in call, call


def test_allowlist_is_passed_through_when_set(tmp_path):
    argv_log = tmp_path / "argv.log"
    stub = _write_stub(tmp_path, [{"session_id": "s1", "result": "ok"}], argv_log)

    proc = _run(stub, "GAME4", "1", LLM_CHESS_ALLOWED_TOOLS="mcp__chess__*")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    (call,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert "--allowedTools" in call and "mcp__chess__*" in call, call


# --------------------------------------------------------------------------- #
# the macOS interpreter
# --------------------------------------------------------------------------- #

def _have_docker() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.skipif(not _have_docker(), reason="docker not available")
def test_runs_under_macos_bash_3_2(tmp_path):
    """Run the real script on bash 3.2, the shell macOS ships.

    Unguarded `"${empty[@]}"` under `set -u` aborts here with
    'resume_args[@]: unbound variable' on the first turn — the reported failure.
    """
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        "echo '{\"session_id\":\"s1\",\"result\":\"played e5\"}'\n"
    )
    stub.chmod(0o755)

    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{REPO}:/repo:ro",
            "-v", f"{tmp_path}:/stub:ro",
            "-e", "LLM_CHESS_CLAUDE_BIN=/stub/claude",
            "bash:3.2", "bash", "/repo/scripts/play-claude.sh", "TESTGAME", "1",
        ],
        capture_output=True, text=True, timeout=300,
    )
    combined = proc.stdout + proc.stderr
    assert "unbound variable" not in combined, combined
    assert proc.returncode == 0, combined
    assert "stopped after 1 turns" in combined, combined


@pytest.mark.skipif(not _have_docker(), reason="docker not available")
def test_serve_match_script_parses_under_bash_3_2():
    """serve-match.sh should stay runnable on bash 3.2 even though it targets Linux."""
    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{REPO}:/repo:ro",
         "bash:3.2", "bash", "-n", "/repo/scripts/serve-match.sh"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr