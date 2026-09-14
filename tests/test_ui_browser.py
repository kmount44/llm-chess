"""Real-browser tests for the spectator UI.

The API tests in ``test_gui.py`` prove the server is correct; these prove the
*page* is. They exist because a real browser caught two bugs no API test could:
the client rendered from a field the API never sent (empty board), and plain
``uvicorn`` had no WebSocket transport, so every live connection 404'd at
handshake.

Skipped automatically when playwright or its browser is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

import uvicorn  # noqa: E402

from llmchess import arbiter, store  # noqa: E402
from llmchess.web.app import app  # noqa: E402

LINE = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6",
        "Be3", "e5", "Nf3", "Be7"]

FILES = "abcdefgh"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


@pytest.fixture
def live_server():
    """A real uvicorn server on a free port, not Starlette's in-process client."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import urllib.error
    import urllib.request

    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/active", timeout=1)
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    else:
        pytest.fail("uvicorn never came up")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def game():
    gid = store.create_game("hermes", "claude")
    for i, san in enumerate(LINE):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid)
    return gid


def _fen_pieces(fen: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for r, row in enumerate(fen.split()[0].split("/")):
        rank, f = 8 - r, 0
        for ch in row:
            if ch.isdigit():
                f += int(ch)
            else:
                out[f"{FILES[f]}{rank}"] = ch
                f += 1
    return out


def _browser():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def test_board_renders_every_piece_the_fen_describes(live_server, game):
    """The board must show the arbiter's position exactly, square for square."""
    expected = _fen_pieces(store.get_game(game)["fen"])
    with _browser() as p:
        page = p.chromium.launch().new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live_server, wait_until="networkidle")
        page.wait_for_selector("#board .piece", timeout=15000)
        page.wait_for_timeout(800)

        dom = page.evaluate(
            "() => Object.fromEntries(Array.from(document.querySelectorAll('#board .sq'))"
            ".map(s => [s.dataset.square, s.dataset.piece || '']))"
        )
        assert errors == [], errors
        assert len(dom) == 64
        assert {sq: v for sq, v in dom.items() if v} == expected

        # Both square colours must be present, or the board is a flat block.
        classes = page.evaluate(
            "() => { const c = {}; for (const s of document.querySelectorAll('#board .sq')) "
            "c[s.className] = (c[s.className]||0)+1; return c; }"
        )
        assert any("light" in k for k in classes)
        assert any("dark" in k for k in classes)


@pytest.fixture
def mated_game():
    gid = store.create_game("hermes", "claude")
    for i, san in enumerate(["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid)
    return gid


def test_page_reports_the_real_result_of_a_finished_game(live_server, mated_game):
    store.set_agent_status(mated_game, "claude", "thinking",
                           session_id="sess-abc", model="claude-sonnet-4-5")
    with _browser() as p:
        page = p.chromium.launch().new_page()
        page.goto(f"{live_server}", wait_until="networkidle")
        page.wait_for_selector("#board .piece", timeout=15000)
        page.wait_for_timeout(1000)
        status = page.text_content("#status-pill")
        rows = page.evaluate("() => document.querySelectorAll('#movelist li').length")
        agents = page.inner_text("#agents")
        assert "finished" in status.lower() and "1-0" in status and "checkmate" in status
        assert rows == 4
        assert "claude" in agents and "hermes" in agents
        assert "sess-abc" in agents


def test_page_shows_an_unfinished_game_as_in_play(live_server, game):
    with _browser() as p:
        page = p.chromium.launch().new_page()
        page.goto(f"{live_server}", wait_until="networkidle")
        page.wait_for_selector("#board .piece", timeout=15000)
        page.wait_for_timeout(800)
        assert "in play" in page.text_content("#status-pill")


def test_open_page_receives_a_move_made_by_another_process(live_server, game):
    """The live view is the whole point: a move must appear without a reload."""
    with _browser() as p:
        page = p.chromium.launch().new_page()
        page.goto(live_server, wait_until="networkidle")
        page.wait_for_selector("#board .piece", timeout=15000)
        page.wait_for_timeout(800)
        before = page.evaluate("() => state.game.ply")

        # Play through the arbiter in a separate process, as an agent would.
        subprocess.run(
            [sys.executable, "-c",
             "from llmchess import store, arbiter; g=store.resolve_game(); arbiter.move('hermes','h3',g['id'])"],
            check=True, capture_output=True, cwd=str(Path(__file__).resolve().parents[1]),
        )

        deadline = time.time() + 15
        while time.time() < deadline:
            if page.evaluate("() => state.game.ply") > before:
                break
            time.sleep(0.25)
        else:
            pytest.fail("the open page never received the pushed move")

        page.wait_for_timeout(600)
        assert page.evaluate(
            "() => document.querySelector('#board .sq[data-square=\"h3\"]').dataset.piece"
        ) == "P"
        assert page.evaluate(
            "() => Array.from(document.querySelectorAll('#board .sq.last')).map(s=>s.dataset.square)"
        ) == ["h3", "h2"]