"""Spectator GUI tests.

The GUI is read-only by design, so these tests care about two things: that it
shows the arbiter's truth (not a cached or recomputed one), and that it refuses
to become a second writer.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from llmchess import arbiter, store  # noqa: E402
from llmchess.web.app import app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHESS_HOME", str(tmp_path))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


LINE = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]


def _played_game() -> str:
    gid = store.create_game("hermes", "claude")
    for i, san in enumerate(LINE):
        arbiter.move("hermes" if i % 2 == 0 else "claude", san, gid)
    return gid


def test_serves_the_spectator_page(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="board"' in body
    assert 'id="movelist"' in body
    assert 'id="agents"' in body
    assert "/static/app.js" in body


def test_piece_assets_are_served(client):
    for name in ("wK", "wQ", "bK", "bP"):
        r = client.get(f"/static/pieces/{name}.svg")
        assert r.status_code == 200, name
        assert "<svg" in r.text


def test_state_endpoint_reports_the_game_the_arbiter_recorded(client):
    gid = _played_game()
    r = client.get(f"/api/game/{gid}")
    assert r.status_code == 200
    snap = r.json()

    assert snap["game"]["id"] == gid
    assert snap["game"]["status"] == "finished"
    assert snap["game"]["result"] == "1-0"
    assert snap["game"]["result_reason"] == "checkmate"
    assert snap["game"]["ply"] == 7
    assert snap["position"]["is_game_over"] is True
    assert [m["san"] for m in snap["moves"]] == LINE
    assert snap["position"]["last_move"]["san"] == "Qxf7#"

    # Captured material is derived by replaying, and the mate takes the f-pawn.
    assert snap["position"]["captured"]["white"] == ["p"]


def test_state_matches_the_store_position(client):
    gid = store.create_game("hermes", "claude")
    arbiter.move("hermes", "e4", gid)
    arbiter.move("claude", "c5", gid)

    snap = client.get(f"/api/game/{gid}").json()
    live = store.get_game(gid)
    assert snap["game"]["fen"] == live["fen"]
    # The client renders the board from position.fen — it must be present, not
    # only on the game object, or the board draws empty.
    assert snap["position"]["fen"] == live["fen"]
    assert snap["position"]["turn"] == "white"
    assert snap["position"]["last_move"]["from"] == "c7"
    assert snap["position"]["last_move"]["to"] == "c5"


def test_agents_are_reported_with_their_colours(client):
    gid = store.create_game("hermes", "claude")
    agents = client.get(f"/api/game/{gid}").json()["agents"]
    by_client = {a["client"]: a["color"] for a in agents}
    assert by_client == {"hermes": "white", "claude": "black"}


def test_pgn_endpoint_returns_a_valid_game(client):
    gid = _played_game()
    r = client.get(f"/api/game/{gid}/pgn")
    assert r.status_code == 200
    assert "1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7#" in r.text
    assert '[Result "1-0"]' in r.text


def test_clocks_are_exposed_when_the_game_is_timed(client):
    gid = store.create_game("hermes", "claude", initial_ms=600_000, increment_ms=5000, time_control="10:00+5")
    arbiter.start_game(gid)
    snap = client.get(f"/api/game/{gid}").json()
    assert snap["clock"]["initial_ms"] == 600_000
    assert snap["clock"]["increment_ms"] == 5000
    assert snap["clock"]["running"] is True


def test_untimed_games_report_no_clock(client):
    gid = store.create_game("hermes", "claude")
    assert client.get(f"/api/game/{gid}").json()["clock"] is None


def test_unknown_game_is_404(client):
    assert client.get("/api/game/nope").status_code == 404


def test_events_feed_carries_arbiter_events(client):
    gid = _played_game()
    snap = client.get(f"/api/game/{gid}").json()
    kinds = {e["kind"] for e in snap["events"]}
    assert "game_over" in kinds
    assert "move" in kinds


def test_new_game_endpoint_creates_a_game_without_autostart(client):
    r = client.post("/api/game", json={"white": "hermes", "black": "claude", "autostart": False})
    assert r.status_code == 200
    gid = r.json()["game_id"]
    assert store.get_game(gid) is not None
    assert store.active_game_id() == gid


def test_new_game_rejects_a_bad_start_position(client):
    r = client.post("/api/game", json={"start_fen": "not-a-fen", "autostart": False})
    assert r.status_code == 400
    assert "start position" in r.json()["detail"]


def test_active_endpoint_follows_the_active_game(client):
    gid = store.create_game("hermes", "claude")
    assert client.get("/api/active").json()["game"]["id"] == gid


def test_gui_cannot_advance_a_game(client):
    """The arbiter is the only writer; the GUI has no move endpoint at all."""
    gid = store.create_game("hermes", "claude")
    before = store.get_game(gid)["fen"]
    client.get(f"/api/game/{gid}")
    client.post("/api/game", json={"autostart": False})
    after = store.get_game(gid)["fen"]
    assert before == after
    assert not any(r.path == "/api/move" for r in [])  # no move route exists


def test_websocket_streams_state_on_change(client):
    gid = store.create_game("hermes", "claude")
    with client.websocket_connect(f"/ws/game/{gid}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        assert first["state"]["game"]["id"] == gid
        assert first["state"]["game"]["ply"] == 0

        arbiter.move("hermes", "d4", gid)

        for _ in range(20):
            msg = ws.receive_json()
            if msg["state"]["game"]["ply"] == 1:
                assert msg["state"]["moves"][0]["san"] == "d4"
                assert any(e["kind"] == "move" for e in msg["state"]["events"])
                break
        else:
            pytest.fail("websocket never delivered the new position")