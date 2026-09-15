#!/usr/bin/env bash
# Host side: run the arbiters that both players connect to.
#
# Run this on the machine that owns the game store. Both arbiter processes and
# the database live here — that single store is what keeps one source of truth.
# A player on another machine connects to these ports over the network.
#
#   ./scripts/serve-match.sh          # start (prints the other machine's command)
#   ./scripts/serve-match.sh stop     # stop
#
# Each identity gets its own process and port, exactly as with stdio. That is
# what stops one player from speaking for the other: your colour is decided by
# which port you were told to connect to.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
ARBITER="$VENV/bin/chess-arbiter"
RUN_DIR="${LLM_CHESS_RUN:-/tmp/llm-chess}"
PORT_HERMES="${PORT_HERMES:-8788}"
PORT_CLAUDE="${PORT_CLAUDE:-8790}"

[ -x "$ARBITER" ] || { echo "error: $ARBITER missing — run scripts/install-mac.sh first" >&2; exit 1; }
mkdir -p "$RUN_DIR"

stop_all() {
  for f in "$RUN_DIR"/arbiter-*.pid; do
    [ -e "$f" ] || continue
    pid="$(cat "$f")"
    if kill "$pid" 2>/dev/null; then echo "stopped $(basename "$f" .pid) (pid $pid)"; fi
    rm -f "$f"
  done
}

if [ "${1:-}" = "stop" ]; then
  stop_all
  exit 0
fi

# Prefer the tailnet address so the other machine can reach us; fall back to
# loopback for a single-machine game.
BIND="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "$BIND" ]; then
  BIND="127.0.0.1"
  echo "note: no tailnet address found — binding to loopback (same-machine play only)"
fi

TOKEN_ARGS=()
if [ -n "${LLM_CHESS_TOKEN:-}" ]; then
  TOKEN_ARGS=(--token "$LLM_CHESS_TOKEN")
fi

start_one() {
  local client="$1" port="$2"
  # TOKEN_ARGS is empty unless a token was set, and bash 3.2 (macOS) aborts on
  # an empty array expansion under `set -u`, so it stays guarded here too even
  # though this script normally runs on the Linux host.
  "$ARBITER" --client "$client" --serve \
    --host "$BIND" --port "$port" \
    --allow-host "$BIND" --allow-host "$BIND:$port" \
    ${TOKEN_ARGS[@]+"${TOKEN_ARGS[@]}"} \
    >"$RUN_DIR/arbiter-$client.log" 2>&1 &
  echo $! >"$RUN_DIR/arbiter-$client.pid"
  echo "started $client arbiter on $BIND:$port (pid $(cat "$RUN_DIR/arbiter-$client.pid"))"
}

stop_all
start_one hermes "$PORT_HERMES"
start_one claude "$PORT_CLAUDE"

sleep 2
for c in hermes claude; do
  if ! kill -0 "$(cat "$RUN_DIR/arbiter-$c.pid")" 2>/dev/null; then
    echo "error: $c arbiter died on startup:" >&2
    cat "$RUN_DIR/arbiter-$c.log" >&2
    exit 1
  fi
done

cat <<EOF

Both arbiters are up. Logs: $RUN_DIR/arbiter-*.log

On the machine running Claude Code:

  claude mcp add --transport http chess http://$BIND:$PORT_CLAUDE/mcp
  ./scripts/play-claude.sh <GAME_ID>

Create the game from here first:

  $VENV/bin/chess-play --white hermes --black claude --create-only

Watch it live from any machine on the tailnet:

  $VENV/bin/chess-gui --host 0.0.0.0 --port 8792   # then open http://$BIND:8792
EOF