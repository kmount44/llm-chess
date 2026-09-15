"""Tests for the remote bot — the script used to prove a network path works.

It is the first thing anyone runs from the far machine, so a raw traceback when
the arbiter is not up yet is a bad failure mode: it looks like a broken script
rather than "the server is not running". These tests pin the behaviour that
matters there.

The bot is a script, not a package module, and its filename has a hyphen, so it
is loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "remote-bot.py"


@pytest.fixture(scope="module")
def bot():
    spec = importlib.util.spec_from_file_location("remote_bot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_root_cause_unwraps_an_exception_group(bot):
    """anyio buries the useful message inside a group; we want the leaf."""
    inner = ConnectionRefusedError("connection refused")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    text = bot._root_cause(group)
    assert "connection refused" in text
    assert "ExceptionGroup" not in text


def test_root_cause_survives_deeply_nested_groups(bot):
    exc: BaseException = ValueError("buried")
    for _ in range(6):
        exc = ExceptionGroup("wrap", [exc])
    assert "buried" in bot._root_cause(exc)


def test_connect_failures_get_actionable_advice(bot):
    text = bot._advice(ConnectionRefusedError("All connection attempts failed"))
    assert "cannot reach the arbiter" in text
    # It should point at the two things that are actually wrong most often.
    assert "serve-match.sh" in text


def test_auth_failures_suggest_the_token(bot):
    text = bot._advice(RuntimeError("HTTP 401 unauthorized"))
    assert "bearer token" in text


def test_unrecognised_failures_are_passed_through_unchanged(bot):
    text = bot._advice(RuntimeError("something odd happened"))
    assert "something odd happened" in text


def test_bot_fails_cleanly_when_nothing_is_listening():
    """No traceback, a clear message, and a non-zero exit code."""
    port = _unused_port()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--url", f"http://127.0.0.1:{port}/mcp",
         "--moves", "e5", "--retries", "0"],
        capture_output=True, text=True, timeout=180,
        env={**os.environ},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "cannot reach the arbiter" in combined
    # A wall of traceback is the thing we are trying to avoid.
    assert "Traceback (most recent call last)" not in combined
    assert "ExceptionGroup" not in combined