"""Match driver — runs a game between two agents, turn by turn.

The driver never touches a position. It decides *whose turn it is*, hands that
player a prompt, and then waits for the arbiter's store to record a move. If the
player does not produce one, the driver nudges, then adjudicates. That separation
is what makes the game trustworthy: a move exists only because the arbiter
accepted it, not because the driver believed what a model said.

    chess-play --white hermes --black claude --gui
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import chess

from . import agents as agents_mod
from . import arbiter, rules, store

MAX_NUDGES = 3


# --------------------------------------------------------------------------- #
# artefacts
# --------------------------------------------------------------------------- #


def games_dir() -> Path:
    d = store.home() / "games"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifacts(game_id: str) -> dict[str, str]:
    """PGN and a full transcript, written when a game ends."""
    game = store.get_game(game_id)
    if not game:
        return {}
    out: dict[str, str] = {}

    pgn_path = games_dir() / f"{game_id}.pgn"
    pgn_path.write_text(rules.to_pgn(game, store.get_moves(game_id)), encoding="utf-8")
    out["pgn"] = str(pgn_path)

    lines = [
        f"# llm-chess transcript — {game_id}",
        f"white: {game['white_client']}",
        f"black: {game['black_client']}",
        f"result: {game.get('result') or '*'} — {game.get('result_reason') or 'unfinished'}",
        f"plies: {game['ply']}",
        "",
        "## moves",
    ]
    for m in store.get_moves(game_id):
        lines.append(f"{m['ply']:>3}. {m['san']:<8} {m['client']} ({m['color']})"
                     + (f"  [{m['think_ms']} ms]" if m.get("think_ms") else ""))
    lines.append("")
    lines.append("## agent commentary")
    for e in store.events_since(game_id):
        if e["kind"] == "agent_message":
            lines.append("")
            lines.append(f"--- {e['client']} ({e.get('ts')}) ---")
            lines.append(str(e.get("text") or ""))
    log_path = games_dir() / f"{game_id}.log"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    out["log"] = str(log_path)
    return out


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #


def build_prompt(game: dict[str, Any], client: str, color: str, *, first: bool, legal_count: int) -> str:
    opponent = game["black_client"] if color == "white" else game["white_client"]
    history = [m["san"] for m in store.get_moves(game["id"])]
    last = history[-1] if history else None
    moves_so_far = " ".join(
        f"{i // 2 + 1}{'.' if i % 2 == 0 else '...'}{san}" for i, san in enumerate(history)
    ) or "(none)"

    return f"""\
You are playing a game of chess as {color}, against {opponent}. Game id: {game['id']}.

{"This is the opening move of the game." if first else f"Moves so far: {moves_so_far}"}
{f"Your opponent's last move: {last}" if last else ""}
Current position (FEN): {game['fen']}
Side to move: {color}. Legal moves available: {legal_count}.

It is your turn. Play your move now, through the chess MCP tools. Pass
game_id="{game['id']}" on every call:

  1. get_board  — confirm the position matches the FEN above
  2. get_legal_moves — if you want the candidate list
  3. make_move  — with game_id="{game['id']}" and your move as SAN (e.g. "Nf3") or UCI (e.g. "g1f3")

If make_move returns ok=false, read the error and call make_move again with a
legal move. Do not give up after one rejection.

When your move has been accepted, reply with at most 20 words explaining it.
No analysis, no move list, no markdown — just the sentence.
"""


def build_nudge(game: dict[str, Any], color: str) -> str:
    return f"""\
No move has been registered for game {game['id']} yet, and it is still {color} to move.

Call make_move now with game_id="{game['id']}" and a legal move. Nothing else
matters — the game does not advance until the arbiter accepts a move from you.
"""


# --------------------------------------------------------------------------- #
# match loop
# --------------------------------------------------------------------------- #


class Driver:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.players: dict[str, Any] = {}
        self.stopped = False

    # -- setup -------------------------------------------------------------

    def prepare(self) -> str:
        a = self.args
        if a.game_id:
            game = store.get_game(a.game_id)
            if not game:
                raise SystemExit(f"no such game: {a.game_id}")
            store.set_active_game(game["id"])
            self.game_id = game["id"]
        else:
            initial_ms, increment_ms, tc = _parse_tc(a.tc)
            self.game_id = store.create_game(
                a.white, a.black,
                time_control=tc, initial_ms=initial_ms, increment_ms=increment_ms,
            )

        self.players = {}
        for client, color in ((a.white, "white"), (a.black, "black")):
            player = agents_mod.build_agent(
                client,
                workdir=Path.cwd(),
                per_move_seconds=a.per_move_seconds,
                dry_run=a.dry_run,
                script=a.script,
                color=color,
            )
            ok, why = player.available()
            if not ok:
                raise SystemExit(f"player '{client}' is not runnable: {why}")
            player.bind_game(self.game_id, client)
            self.players[client] = player

        game = store.get_game(self.game_id)
        assert game is not None
        store.add_event(self.game_id, "match_setup",
                        text=f"white={a.white} black={a.black} dry_run={a.dry_run}")
        return self.game_id

    # -- per turn ----------------------------------------------------------

    def play(self) -> None:
        a = self.args
        plies = 0
        while True:
            if self.stopped:
                break
            game = store.get_game(self.game_id)
            if not game:
                break
            if game["status"] == "finished":
                break
            if a.moves and game["ply"] >= a.moves:
                arbiter.adjudicate(self.game_id, "*", f"stopped at {a.moves} plies (--moves)")
                break

            board = chess.Board(game["fen"])
            color = "white" if board.turn else "black"
            client = game["white_client"] if color == "white" else game["black_client"]
            player = self.players[client]
            plies = game["ply"]

            if plies == 0:
                arbiter.start_game(self.game_id)
                game = store.get_game(self.game_id) or game

            if self._adjudicate_passive_draw(game):
                break

            store.set_agent_status(self.game_id, client, "thinking",
                                   session_id=player.session_id,
                                   model=getattr(player, "model", None))
            accepted = False
            for attempt in range(MAX_NUDGES + 1):
                prompt = (build_prompt(game, client, color,
                                       first=(game["ply"] == 0 and attempt == 0),
                                       legal_count=board.legal_moves.count())
                          if attempt == 0 else build_nudge(game, color))
                result = player.take_turn(prompt)

                store.add_event(
                    self.game_id, "agent_message", client=client,
                    text=result.reply or result.error or "(no response)",
                    data={"move": f"attempt {attempt + 1}", "wall_s": round(result.duration_s, 1)},
                )
                if result.session_id:
                    store.set_agent_status(self.game_id, client, "thinking",
                                           session_id=result.session_id)

                after = store.get_game(self.game_id)
                if after is None:
                    break
                if after["status"] == "finished":
                    store.set_agent_status(self.game_id, client, "moved",
                                           session_id=player.session_id)
                    accepted = True
                    break
                if after["ply"] > plies:
                    store.set_agent_status(self.game_id, client, "moved",
                                           session_id=player.session_id)
                    accepted = True
                    break

                store.add_event(self.game_id, "nudge", client=client,
                                text=f"no move after attempt {attempt + 1}: "
                                     f"{(result.error or 'agent replied without moving')[:300]}")
                store.set_agent_status(self.game_id, client, "waiting",
                                       session_id=player.session_id,
                                       error=(result.error or "replied without making a move")[:500])
                if a.verbose:
                    print(f"  ! {client} did not move (attempt {attempt + 1})", flush=True)

            if not accepted:
                loser = client
                result = "1-0" if loser == "black" else "0-1"
                arbiter.adjudicate(
                    self.game_id, result,
                    f"{loser} failed to move after {MAX_NUDGES + 1} attempts (forfeit)",
                )
                store.set_agent_status(self.game_id, client, "error",
                                       error="failed to move — forfeited")
                break

            if a.verbose:
                moves = store.get_moves(self.game_id)
                if moves:
                    print(f"  {moves[-1]['ply']:>3}. {moves[-1]['san']:<8} {client}", flush=True)

    def _adjudicate_passive_draw(self, game: dict[str, Any]) -> bool:
        """Close out positions that are drawn but never claimed by a player."""
        if not self.args.auto_draw:
            return False
        board = chess.Board(game["fen"])
        if board.is_insufficient_material():
            arbiter.adjudicate(self.game_id, "1/2-1/2", "insufficient material")
            return True
        if board.can_claim_threefold_repetition():
            arbiter.adjudicate(self.game_id, "1/2-1/2", "threefold repetition")
            return True
        if board.can_claim_fifty_moves():
            arbiter.adjudicate(self.game_id, "1/2-1/2", "fifty move rule")
            return True
        return False

    # -- GUI ---------------------------------------------------------------

    def start_gui(self) -> str | None:
        a = self.args
        if not a.gui:
            return None
        cmd = [sys.executable, "-m", "llmchess.web.app", "--port", str(a.port)]
        log = games_dir() / f"{self.game_id}.gui.log"
        handle = open(log, "w", buffering=1)  # noqa: SIM115
        proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{a.port}"
        for _ in range(60):
            if proc.poll() is not None:
                return None
            if _http_ok(f"{url}/api/active"):
                return url
            time.sleep(0.25)
        return url


def _http_ok(url: str) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _parse_tc(tc: str | None) -> tuple[int | None, int, str | None]:
    if not tc:
        return None, 0, None
    try:
        minutes, _, inc = tc.partition("+")
        return int(float(minutes) * 60_000), int(float(inc or 0) * 1000), tc
    except ValueError as exc:
        raise SystemExit(f"bad --tc '{tc}' (expected e.g. 10+5)") from exc


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chess-play", description="Run an LLM-vs-LLM chess match over MCP.")
    p.add_argument("--white", default="hermes", choices=list(agents_mod.KNOWN_AGENTS))
    p.add_argument("--black", default="claude", choices=list(agents_mod.KNOWN_AGENTS))
    p.add_argument("--game-id", help="continue an existing game instead of creating one")
    p.add_argument("--moves", type=int, default=0, help="stop after this many plies (0 = play to the end)")
    p.add_argument("--per-move-seconds", type=int, default=300)
    p.add_argument("--tc", help="time control, e.g. 10+5 (omit for untimed)")
    p.add_argument("--gui", dest="gui", action="store_true", default=True)
    p.add_argument("--no-gui", dest="gui", action="store_false")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--dry-run", action="store_true", help="no LLM calls; play a scripted line")
    p.add_argument("--create-only", action="store_true",
                   help="create and start the game, print its id, and exit. Use this "
                        "when the players connect themselves (e.g. over the network) "
                        "instead of being driven from here.")
    p.add_argument("--script", default="e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#",
                   help="space-separated SAN moves for --dry-run")
    p.add_argument("--auto-draw", action="store_true", default=True,
                   help="adjudicate threefold/fifty-move/insufficient-material as draws")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # --script is a single SAN line covering both sides; normalise it once,
    # whoever is playing it (the default string is a game, not a move).
    if isinstance(args.script, str):
        args.script = args.script.split()
    if args.white == args.black:
        raise SystemExit("--white and --black must be different agents")

    # A network match is created here and then played by the agents themselves,
    # so neither CLI needs to be present on this machine for this to work.
    if args.create_only:
        initial_ms, increment_ms, tc = _parse_tc(args.tc)
        game_id = store.create_game(
            args.white, args.black,
            time_control=tc, initial_ms=initial_ms, increment_ms=increment_ms,
        )
        arbiter.start_game(game_id)
        print(game_id)
        return 0

    # Agents reach the game through their own MCP server process, and Hermes
    # spawns MCP subprocesses with a filtered environment — LLM_CHESS_HOME does
    # not reach them. Warn rather than silently playing against an empty store.
    custom_home = os.environ.get("LLM_CHESS_HOME")
    if custom_home:
        print(
            f"note: LLM_CHESS_HOME={custom_home} is set. Agent MCP servers will not\n"
            "      inherit it (Hermes filters the environment for MCP subprocesses),\n"
            "      so agents may look at a different store. Re-declare it on the MCP\n"
            "      server entry with `hermes mcp add ... --env LLM_CHESS_HOME=...`,\n"
            "      or unset it and use the default ~/.llm-chess.",
            file=sys.stderr,
        )

    driver = Driver(args)
    game_id = driver.prepare()

    url = driver.start_gui()
    if url:
        print(f"spectator GUI → {url}", flush=True)
    print(f"match {game_id}: {args.white} (white) vs {args.black} (black)", flush=True)

    try:
        driver.play()
    except KeyboardInterrupt:
        print("\ninterrupted — adjudicating as unfinished", flush=True)
        arbiter.adjudicate(game_id, "*", "match interrupted")

    game = store.get_game(game_id)
    artifacts = write_artifacts(game_id)
    assert game is not None
    print(f"\nresult: {game.get('result') or '*'} — {game.get('result_reason') or 'unfinished'}"
          f" after {game['ply']} plies", flush=True)
    for kind, path in artifacts.items():
        print(f"{kind}: {path}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())