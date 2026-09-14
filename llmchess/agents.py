"""Agent adapters — how each player is actually driven.

Every player is a real CLI agent with the arbiter registered as an MCP server,
so the moves come from tool calls the arbiter validated, never from text we
scrape out of a model's reply.

Sessions persist across a player's own turns: each side keeps one continuous
conversation for the whole game, which is what makes them play like opponents
with a plan rather than stateless position-solvers. Neither side ever sees the
other's session.
"""

from __future__ import annotations

import abc
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_ID_RE = re.compile(r"\b(\d{8}_\d{6}_[0-9a-f]{6})\b")


@dataclass
class TurnResult:
    ok: bool
    reply: str = ""
    session_id: str | None = None
    error: str | None = None
    duration_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class Agent(abc.ABC):
    """One player's brain."""

    name: str = "agent"

    def __init__(self, *, workdir: Path, per_move_seconds: int = 300, extra: list[str] | None = None):
        self.workdir = workdir
        self.per_move_seconds = per_move_seconds
        self.extra = extra or []
        self.session_id: str | None = None
        self.last_error: str | None = None
        self.game_id: str | None = None
        self.client: str | None = None

    def bind_game(self, game_id: str, client: str) -> None:
        """Told which game and identity it is playing.

        CLI agents do not need this — they discover the game through the MCP
        tools, exactly as an outside client would. In-process agents (the
        dry-run script) use it to talk to the arbiter directly.
        """
        self.game_id = game_id
        self.client = client

    @abc.abstractmethod
    def _argv(self, prompt: str) -> list[str]:
        """Build the command that takes one turn."""

    def _parse(self, stdout: str, stderr: str) -> TurnResult:
        return TurnResult(ok=True, reply=stdout.strip(), session_id=self.session_id)

    def available(self) -> tuple[bool, str]:
        exe = self._argv("x")[0]
        if shutil.which(exe) is None:
            return False, f"'{exe}' not found on PATH"
        return True, ""

    def take_turn(self, prompt: str) -> TurnResult:
        """Run one turn to completion. Never raises: the driver decides policy."""
        argv = self._argv(prompt)
        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                timeout=self.per_move_seconds,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired as exc:
            self.last_error = f"timed out after {self.per_move_seconds}s"
            return TurnResult(
                ok=False,
                error=self.last_error,
                duration_s=time.time() - started,
                reply=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            )
        except OSError as exc:
            self.last_error = str(exc)
            return TurnResult(ok=False, error=str(exc), duration_s=time.time() - started)

        result = self._parse(proc.stdout, proc.stderr)
        result.duration_s = time.time() - started
        if proc.returncode != 0 and not result.reply:
            result.ok = False
            result.error = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:2000]
            self.last_error = result.error
        if result.session_id:
            self.session_id = result.session_id
        return result


class HermesAgent(Agent):
    """Hermes, driven headlessly with a persistent session.

    Turn one starts a fresh session; the session id is recovered by diffing the
    session list before and after, which is more reliable than assuming the
    newest session is ours. Every later turn resumes that exact session.
    """

    name = "hermes"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self._known_sessions: set[str] = set()

    def _hermes(self) -> str:
        return os.environ.get("LLM_CHESS_HERMES_BIN", "hermes")

    def _argv(self, prompt: str) -> list[str]:
        argv = [self._hermes()]
        if self.session_id:
            argv += ["--resume", self.session_id]
        argv += ["-z", prompt, "--max-turns", "14", "-Q"]
        argv += self.extra
        return argv

    def _list_sessions(self) -> set[str]:
        try:
            proc = subprocess.run(
                [self._hermes(), "sessions", "list", "--source", "cli", "--limit", "25"],
                capture_output=True, text=True, timeout=60, env={**os.environ},
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        return set(SESSION_ID_RE.findall(proc.stdout))

    def take_turn(self, prompt: str) -> TurnResult:
        before = self._list_sessions() if not self.session_id else set()
        result = super().take_turn(prompt)
        if not self.session_id:
            after = self._list_sessions()
            new = after - before
            if new:
                self.session_id = sorted(new)[-1]
                result.session_id = self.session_id
            elif match := SESSION_ID_RE.search(result.reply):
                self.session_id = match.group(1)
                result.session_id = self.session_id
        return result

    def _parse(self, stdout: str, stderr: str) -> TurnResult:
        reply = stdout.strip()
        if not reply and stderr.strip():
            # On a non-TTY the banner/status noise goes to stderr; the answer is
            # what landed on stdout. Only fall back when stdout is empty.
            reply = stderr.strip()
        return TurnResult(ok=bool(reply), reply=reply, session_id=self.session_id,
                          error=None if reply else "empty response")


class ClaudeAgent(Agent):
    """Claude Code, driven headlessly with a resumable session.

    ``--output-format json`` returns the session id so the next turn can resume
    it; the flag set is deliberately narrow so an unattended game cannot wander
    off doing tool calls we did not ask for.
    """

    name = "claude"

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.model: str | None = None

    def _claude(self) -> str:
        return os.environ.get("LLM_CHESS_CLAUDE_BIN", "claude")

    def _argv(self, prompt: str) -> list[str]:
        argv = [self._claude(), "-p", prompt, "--output-format", "json"]
        if self.session_id:
            argv += ["--resume", self.session_id]
        # `--allowedTools` prefix-matches: mcp__chess covers every arbiter tool.
        argv += ["--allowedTools", "mcp__chess"]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra
        return argv

    def _parse(self, stdout: str, stderr: str) -> TurnResult:
        text = stdout.strip()
        if not text:
            return TurnResult(ok=False, error=(stderr or "empty response").strip()[:2000])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # Not JSON (older CLI, or a warning line before it). Treat the stdout
            # as the reply and keep whatever session we already had.
            return TurnResult(ok=True, reply=text, session_id=self.session_id)

        reply = payload.get("result") or payload.get("text") or ""
        session_id = payload.get("session_id") or self.session_id
        if payload.get("is_error"):
            return TurnResult(ok=False, reply=reply, session_id=session_id,
                              error=str(reply)[:2000], raw=payload)
        return TurnResult(ok=True, reply=str(reply), session_id=session_id, raw=payload)


class ScriptedAgent(Agent):
    """A deterministic opponent for ``--dry-run``.

    Plays from its own move list, so the whole pipeline — driver, arbiter, GUI,
    PGN — can be exercised without spending a token. The driver hands each side
    only the moves for its own colour.
    """

    name = "scripted"

    def __init__(self, moves: list[str], **kw: Any):
        super().__init__(**kw)
        self.moves = list(moves)
        self.i = 0

    def _argv(self, prompt: str) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def take_turn(self, prompt: str) -> TurnResult:
        from . import arbiter

        move = self.moves[self.i] if self.i < len(self.moves) else None
        self.i += 1
        if move is None:
            return TurnResult(ok=False, session_id="scripted", error="out of scripted moves")
        try:
            out = arbiter.move(self.client, move, self.game_id)
        except arbiter.ArbiterError as exc:
            return TurnResult(ok=False, session_id="scripted", error=str(exc))
        return TurnResult(ok=True, reply=f"(scripted) {out['played']}", session_id="scripted")


def build_agent(
    name: str,
    *,
    workdir: Path,
    per_move_seconds: int,
    dry_run: bool = False,
    script: list[str] | None = None,
    color: str | None = None,
) -> Agent:
    if dry_run:
        # A single SAN line describes the whole game; each side gets its half.
        line = list(script or [])
        mine = line[0::2] if color == "white" else line[1::2]
        return ScriptedAgent(mine, workdir=workdir, per_move_seconds=per_move_seconds)
    if name == "hermes":
        return HermesAgent(workdir=workdir, per_move_seconds=per_move_seconds)
    if name == "claude":
        return ClaudeAgent(workdir=workdir, per_move_seconds=per_move_seconds)
    raise ValueError(f"unknown agent '{name}' (expected 'hermes' or 'claude')")


KNOWN_AGENTS = ("hermes", "claude")