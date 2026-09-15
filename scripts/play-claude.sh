#!/usr/bin/env bash
# Player side: drive Claude Code as one side of a networked match.
#
# Run this on the machine that hosts Claude Code, after registering the remote
# arbiter with it (see scripts/serve-match.sh for the exact command). Claude
# keeps one session across its turns, so it remembers the game it is playing.
#
#   ./scripts/play-claude.sh <GAME_ID> [MAX_MOVES]
#
# The arbiter enforces everything: turn order, legality, clocks. This loop only
# asks Claude to take its turn, and stops when the arbiter says the game ended.
set -euo pipefail

GAME_ID="${1:?usage: play-claude.sh GAME_ID [MAX_MOVES]}"
MAX_MOVES="${2:-80}"
CLAUDE_BIN="${LLM_CHESS_CLAUDE_BIN:-claude}"
SERVER="${LLM_CHESS_MCP_NAME:-chess}"
# Claude Code matches this against tool names. Some builds want the bare server
# prefix and others need the glob, so it stays overridable rather than being
# guessed at here.
ALLOWED_TOOLS="${LLM_CHESS_ALLOWED_TOOLS:-mcp__${SERVER}}"

PROMPT="You are playing a chess game (game_id ${GAME_ID}) through the '${SERVER}' MCP tools. \
Do this, in order, every turn:
1. Call wait_for_turn with game_id=\"${GAME_ID}\" and timeout=240.
2. If it returns released=\"game over\", reply with exactly: GAME OVER <result> — <result_reason>, and stop.
3. If it returns released=\"timeout\", call wait_for_turn again.
4. If it returns released=\"your turn\", call get_board to read the position.
5. Call make_move with a move you have chosen yourself — no engine advice is available.
   If the move is rejected, read the error and try another; the position is unchanged.
Do not play out of turn. Do not guess the move before reading the board."

# Claude's session id, kept across turns so it remembers the game. Empty until
# the first turn completes, which is why the resume flag is built conditionally.
session_id=""
played=0

while [ "$played" -lt "$MAX_MOVES" ]; do
  resume_args=()
  if [ -n "$session_id" ]; then
    resume_args=(--resume "$session_id")
  fi

  # ${arr[@]+"${arr[@]}"} and not plain "${arr[@]}": macOS ships bash 3.2, and
  # there expanding an empty array under `set -u` aborts with "unbound
  # variable". The guard expands to nothing when the array is empty, which is
  # the first turn of every game. Bash 4.4+ made the bare form legal, so this
  # only bites on the Mac.
  out="$("$CLAUDE_BIN" -p "$PROMPT" --output-format json \
        --allowedTools "$ALLOWED_TOOLS" ${resume_args[@]+"${resume_args[@]}"} 2>/dev/null || true)"

  if [ -z "$out" ]; then
    echo "no output from claude; retrying in 5s" >&2
    sleep 5
    continue
  fi

  reply="$(printf '%s' "$out" | python3 -c \
    'import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print(d.get("result") or d.get("text") or "")' 2>/dev/null || true)"

  sid="$(printf '%s' "$out" | python3 -c \
    'import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
print(d.get("session_id") or "")' 2>/dev/null || true)"

  # Keep one session for the whole game so Claude remembers its own reasoning.
  if [ -n "$sid" ] && [ -z "$session_id" ]; then
    session_id="$sid"
    echo "claude session: $sid"
  fi

  case "$reply" in
    *"GAME OVER"*)
      echo "finished: $reply"
      exit 0
      ;;
  esac

  played=$((played + 1))
  echo "turn $played done: ${reply:0:160}"
done

echo "stopped after $MAX_MOVES turns"