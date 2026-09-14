#!/usr/bin/env bash
# Install llm-chess on macOS and print the MCP registration commands.
#
# Safe to re-run: it reuses the venv if one is already present.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }

# --- python -----------------------------------------------------------------
PY=""
for cand in python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "error: Python 3.10+ is required and was not found on PATH." >&2
  echo "Install it with: brew install python@3.12" >&2
  exit 1
fi
say "using $($PY --version) at $(command -v "$PY")"

# --- venv + deps ------------------------------------------------------------
VENV="$ROOT/.venv"
if [ -d "$VENV" ]; then
  say "reusing existing venv at $VENV"
else
  if command -v uv >/dev/null 2>&1; then
    say "creating venv with uv"
    uv venv "$VENV" --python "$PY"
  else
    say "creating venv with venv module (uv not found)"
    "$PY" -m venv "$VENV"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  say "installing llm-chess and its dependencies"
  uv pip install --python "$VENV/bin/python" -e "$ROOT"
else
  say "installing llm-chess and its dependencies"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install -e "$ROOT"
fi

ARBITER="$VENV/bin/chess-arbiter"
[ -x "$ARBITER" ] || { echo "error: chess-arbiter was not installed at $ARBITER" >&2; exit 1; }

# --- players ----------------------------------------------------------------
say "checking for the two players"
HERMES_BIN="$(command -v hermes || true)"
CLAUDE_BIN="$(command -v claude || true)"
[ -n "$HERMES_BIN" ] && echo "  hermes: $HERMES_BIN" || warn "hermes CLI not on PATH — Hermes cannot play yet"
[ -n "$CLAUDE_BIN" ] && echo "  claude: $CLAUDE_BIN" || warn "claude CLI not on PATH — Claude cannot play yet"

say "smoke test: can the arbiter start?"
"$VENV/bin/python" -m llmchess.mcp_server --help >/dev/null
echo "  arbiter ok"

# --- next steps -------------------------------------------------------------
cat <<EOF

$(say "installed. Now register the arbiter with each player:")

1) Claude Code — run this once:

   claude mcp add chess -- $ARBITER --client claude

   Verify with: claude mcp list

2) Hermes — add this under mcp_servers in ~/.hermes/config.yaml, then restart
   Hermes (MCP servers are discovered at startup; there is no hot reload):

   mcp_servers:
     chess:
       command: "$ARBITER"
       args: ["--client", "hermes"]
       timeout: 60

   Verify with: hermes mcp list

3) Check both clients really see the tools, then play:

   $VENV/bin/chess-play --dry-run --no-gui     # no tokens, proves the wiring
   $VENV/bin/chess-play --white hermes --black claude --gui

The spectator GUI prints a localhost URL when the match starts.

Note: agents need to be able to use MCP tools without an interactive prompt,
since the match runs unattended.
EOF