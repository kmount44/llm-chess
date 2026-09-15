"""Persistent game store — the single source of truth for a running game.

Everything the arbiter, the driver and the spectator GUI need lives in one
SQLite database under ``$LLM_CHESS_HOME`` (default ``~/.llm-chess``). Multiple
processes (two MCP servers, the driver, the GUI) open it concurrently, so every
mutation happens inside a ``BEGIN IMMEDIATE`` transaction with a busy timeout;
SQLite serialises writers for us.

Rows are never rewritten destructively: the move list and the event feed are
append-only, which is what makes the GUI's live view and the PGN export
reproducible after the fact.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

DEFAULT_HOME = "~/.llm-chess"


def home() -> Path:
    """Root directory for stores. Honour ``LLM_CHESS_HOME`` for test isolation."""
    raw = os.environ.get("LLM_CHESS_HOME") or DEFAULT_HOME
    p = Path(raw).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    p = home() / "chess.db"
    return p


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the store. WAL + a generous busy timeout keep concurrent writers safe."""
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS games (
            id            TEXT PRIMARY KEY,
            created       REAL NOT NULL,
            started       REAL,
            finished      REAL,
            status        TEXT NOT NULL DEFAULT 'pending',   -- pending|active|finished
            white_client  TEXT NOT NULL,
            black_client  TEXT NOT NULL,
            start_fen     TEXT NOT NULL,
            fen           TEXT NOT NULL,
            ply           INTEGER NOT NULL DEFAULT 0,
            version       INTEGER NOT NULL DEFAULT 0,
            result        TEXT,
            result_reason TEXT,
            time_control  TEXT,
            initial_ms    INTEGER,
            increment_ms  INTEGER DEFAULT 0,
            white_ms      INTEGER,
            black_ms      INTEGER,
            last_move_ts  REAL,
            notes         TEXT
        );

        CREATE TABLE IF NOT EXISTS moves (
            game_id    TEXT NOT NULL,
            ply        INTEGER NOT NULL,
            client     TEXT NOT NULL,
            color      TEXT NOT NULL,
            san        TEXT NOT NULL,
            uci        TEXT NOT NULL,
            fen_after  TEXT NOT NULL,
            ts         REAL NOT NULL,
            think_ms   INTEGER,
            comment    TEXT,
            PRIMARY KEY (game_id, ply),
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id  TEXT NOT NULL,
            ts       REAL NOT NULL,
            kind     TEXT NOT NULL,
            client   TEXT,
            text     TEXT,
            data     TEXT
        );

        CREATE TABLE IF NOT EXISTS agents (
            game_id     TEXT NOT NULL,
            client      TEXT NOT NULL,
            color       TEXT,
            status      TEXT NOT NULL DEFAULT 'idle',  -- idle|waiting|thinking|moved|error|stopped
            session_id  TEXT,
            model       TEXT,
            last_seen   REAL,
            last_error  TEXT,
            PRIMARY KEY (game_id, client)
        );

        CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, id);
        CREATE INDEX IF NOT EXISTS idx_moves_game ON moves(game_id, ply);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


@contextlib.contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialise a mutation against every other process touching this store."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _bump(conn: sqlite3.Connection, game_id: str) -> int:
    conn.execute(
        "UPDATE games SET version = version + 1 WHERE id = ?", (game_id,)
    )
    row = conn.execute("SELECT version FROM games WHERE id = ?", (game_id,)).fetchone()
    return int(row["version"]) if row else 0


# --------------------------------------------------------------------------- #
# games
# --------------------------------------------------------------------------- #


def create_game(
    white_client: str,
    black_client: str,
    *,
    start_fen: str | None = None,
    time_control: str | None = None,
    initial_ms: int | None = None,
    increment_ms: int = 0,
    notes: str | None = None,
) -> str:
    from . import rules

    fen = start_fen or rules.START_FEN
    rules.Board_from_fen(fen)  # validate before we persist anything

    game_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = time.time()
    conn = connect()
    try:
        with write_tx(conn):
            conn.execute(
                """
                INSERT INTO games (id, created, status, white_client, black_client,
                                   start_fen, fen, ply, version, time_control,
                                   initial_ms, increment_ms, white_ms, black_ms, notes)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
                """,
                (game_id, now, white_client, black_client, fen, fen,
                 time_control, initial_ms, increment_ms, initial_ms, initial_ms, notes),
            )
            for client, color in ((white_client, "white"), (black_client, "black")):
                conn.execute(
                    "INSERT OR REPLACE INTO agents (game_id, client, color, status, last_seen)"
                    " VALUES (?, ?, ?, 'idle', ?)",
                    (game_id, client, color, now),
                )
    finally:
        conn.close()
    set_active_game(game_id)
    add_event(game_id, "game_created", text=f"{white_client} (white) vs {black_client} (black)")
    return game_id


def get_game(game_id: str | None = None) -> dict[str, Any] | None:
    conn = connect()
    try:
        gid = game_id or active_game_id()
        if not gid:
            return None
        row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_games(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, created, status, white_client, black_client, ply, result,"
            " result_reason FROM games ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_active_game(game_id: str) -> None:
    conn = connect()
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('active_game', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (game_id,),
            )
    finally:
        conn.close()


def active_game_id() -> str | None:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'active_game'").fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def resolve_game(game_id: str | None = None, *, clients: list[str] | None = None) -> dict[str, Any]:
    """Find the game an MCP client means.

    Explicit id wins; otherwise the active game; otherwise the newest
    unfinished game. Raises ``LookupError`` when nothing is playable, so the
    arbiter can return a clear message instead of mutating the wrong game.
    """
    if game_id:
        g = get_game(game_id)
        if g:
            return g
        # The GUI shows only the tail of an id, and people read ids to agents
        # that way, so accept an unambiguous fragment. LIKE would treat the
        # underscores in an id as wildcards, hence the explicit escape.
        frag = game_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id FROM games WHERE id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\'"
                " ORDER BY created DESC",
                (f"%{frag}", f"{frag}%"),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) == 1:
            g = get_game(rows[0]["id"])
            if g:
                return g
        if not rows:
            raise LookupError(f"no such game: {game_id}")
        options = ", ".join(r["id"] for r in rows[:6])
        raise LookupError(
            f"'{game_id}' matches {len(rows)} games. Use a longer fragment or the"
            f" full id — candidates: {options}"
        )

    gid = active_game_id()
    if gid:
        g = get_game(gid)
        if g:
            return g

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM games WHERE status != 'finished' ORDER BY created DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError(
            "no active game — start one with `chess-play` (or the GUI's New Game button)"
        )
    g = get_game(row["id"])
    assert g is not None
    return g


def color_for_client(game: dict[str, Any], client: str) -> str | None:
    if game.get("white_client") == client:
        return "white"
    if game.get("black_client") == client:
        return "black"
    return None


# --------------------------------------------------------------------------- #
# events / agents
# --------------------------------------------------------------------------- #


def add_event(
    game_id: str,
    kind: str,
    *,
    client: str | None = None,
    text: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    conn = connect()
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO events (game_id, ts, kind, client, text, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, time.time(), kind, client, text,
                 json.dumps(data) if data else None),
            )
            _bump(conn, game_id)
    finally:
        conn.close()


def events_since(game_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE game_id = ? AND id > ? ORDER BY id LIMIT ?",
            (game_id, after_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                with contextlib.suppress(json.JSONDecodeError):
                    d["data"] = json.loads(d["data"])
            out.append(d)
        return out
    finally:
        conn.close()


def set_agent_status(
    game_id: str,
    client: str,
    status: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    conn = connect()
    try:
        with write_tx(conn):
            conn.execute(
                """
                INSERT INTO agents (game_id, client, status, session_id, model, last_seen, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, client) DO UPDATE SET
                    status      = excluded.status,
                    session_id  = COALESCE(excluded.session_id, agents.session_id),
                    model       = COALESCE(excluded.model, agents.model),
                    last_seen   = excluded.last_seen,
                    last_error  = excluded.last_error
                """,
                (game_id, client, status, session_id, model, time.time(), error),
            )
            _bump(conn, game_id)
    finally:
        conn.close()


def get_agents(game_id: str) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agents WHERE game_id = ? ORDER BY color", (game_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def agent_session(game_id: str, client: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT session_id FROM agents WHERE game_id = ? AND client = ?",
            (game_id, client),
        ).fetchone()
        return row["session_id"] if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# moves
# --------------------------------------------------------------------------- #


def get_moves(game_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        sql = "SELECT * FROM moves WHERE game_id = ? ORDER BY ply"
        args: tuple[Any, ...] = (game_id,)
        if limit:
            sql += " DESC LIMIT ?"
        rows = conn.execute(sql, (game_id, limit) if limit else args).fetchall()
        out = [dict(r) for r in rows]
        return list(reversed(out)) if limit else out
    finally:
        conn.close()