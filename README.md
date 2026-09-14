# llm-chess

Two autonomous LLM agents play chess against each other. Not a chess engine with
an AI bolted on — an **arbitrated MCP server** that both agents connect to as
players, plus a live spectator GUI.

Built to answer a specific question: *can Hermes beat Claude at chess when
neither of them gets to cheat, and neither of them can see the other's
reasoning?*

## How it works

```
┌──────────────┐        MCP (stdio)        ┌─────────────────────┐
│  Hermes      │──────────────────────────▶│                     │
│  (player)    │◀──────────────────────────│   chess-arbiter     │
└──────────────┘                           │   ── the rules ──   │
                                           │   turn enforcement  │
┌──────────────┐        MCP (stdio)        │   legality checks   │
│  Claude      │──────────────────────────▶│   clocks            │
│  (player)    │◀──────────────────────────│   move log / PGN    │
└──────────────┘                           └──────────┬──────────┘
                                                      │
                                            ┌─────────▼─────────┐
                                            │  live spectator   │
                                            │  GUI (WebSocket)  │
                                            └───────────────────┘
```

The arbiter is the single source of truth. Turn order, colour binding, move
legality and the clocks are enforced **outside both models**, so a player cannot
move twice, move out of turn, play an illegal move, or desync the position. A
rejected move leaves the board untouched and returns an error the agent can read
and recover from.

Both agents connect to the *same* arbiter through MCP tools. They see only the
position — never each other's reasoning — which makes this a real test of chess
ability rather than a test of who can talk the other into a loss.

## Requirements

- macOS, Linux, or Windows (developed and tested on macOS)
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or plain `pip`
- Two players:
  - **Hermes** — `hermes` CLI, with this arbiter registered as an MCP server
  - **Claude** — `claude` CLI (Claude Code), with this arbiter registered as an
    MCP server

## Install

```bash
git clone https://github.com/kmount44/llm-chess.git
cd llm-chess
./scripts/install-mac.sh
```

The installer creates a virtualenv, installs the package, and prints the exact
MCP registration commands for both agents.

Manual install:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

## Registering the arbiter with each player

Each player runs its own arbiter process with its own identity. Colour is read
from the game record, so the same registration works for either side.

**Hermes** — add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  chess:
    command: "/ABSOLUTE/PATH/llm-chess/.venv/bin/chess-arbiter"
    args: ["--client", "hermes"]
    timeout: 60
```

Restart Hermes afterwards — MCP servers are discovered at startup.

**Claude Code**:

```bash
claude mcp add chess -- /ABSOLUTE/PATH/llm-chess/.venv/bin/chess-arbiter --client claude
```

Verify with `claude mcp list` and `hermes mcp list`.

## Playing a game

```bash
# start the spectator GUI and a game, then let the agents play
chess-play --white hermes --black claude --gui
```

Open the URL it prints (default <http://127.0.0.1:8787>) to watch the game live:
board, move list, clocks, captured material, and each agent's status and
transcript as it thinks.

Useful flags:

```
--white {hermes,claude,scripted}   which agent plays white
--black {hermes,claude,scripted}   which agent plays black
--moves N                 stop after N plies (default: until the game ends)
--per-move-seconds N      time budget per move before the agent forfeits the turn
--tc MINUTES+INCREMENT    e.g. --tc 10+5. Omit for untimed play.
--script "e4 e5 ..."      the SAN line a `scripted` player follows
--gui / --no-gui          start the spectator server (default: on)
--port PORT               spectator GUI port
--dry-run                 exercise the whole pipeline with scripted moves, no LLM calls
```

`--dry-run` is the fastest way to confirm the wiring before spending tokens.
`scripted` is also usable as a real opponent without `--dry-run`, which is how
you smoke-test one live agent against a deterministic line:

```bash
chess-play --white hermes --black scripted --moves 6 --no-gui -v
```

## Verified

These are real runs, not claims:

- A live Hermes agent played `e4`, `Nf3`, `Bc4` through the MCP arbiter against a
  scripted opponent, with commentary, and carried one session across all three
  of its turns.
- Two MCP clients playing one board over real stdio, with a rejected illegal move
  leaving the position untouched.
- The spectator page rendering a position square-for-square against its FEN, and
  receiving a move played by a separate process without a reload.
- A two-process race for the same ply, where exactly one writer wins.

| Suite | Covers |
|---|---|
| `tests/test_arbiter.py` | Rule enforcement: turn order, colour binding, legality, clocks, race safety |
| `tests/test_mcp_stdio.py` | Real MCP over stdio: discovery, two clients, recoverable errors |
| `tests/test_driver.py` | Agent adapters, prompts, forfeit policy, artifacts, "agent lied about moving" |
| `tests/test_gui.py` | The spectator API against the arbiter's store |
| `tests/test_ui_browser.py` | The page in a real browser (Playwright) |

## Gotchas worth knowing

- **`hermes mcp add` is interactive.** It prompts for tool selection and cancels
  on a non-TTY. Pipe the answer: `printf 'y\n' | hermes mcp add chess ...`.
- **`LLM_CHESS_HOME` does not reach agent MCP servers.** Hermes spawns MCP
  subprocesses with a filtered environment, so a custom store path must be
  re-declared on the MCP entry (`hermes mcp add ... --env LLM_CHESS_HOME=...`).
  The driver warns when it sees the mismatch.
- **`--max-turns`, `-Q` and `--yolo` are `hermes chat` flags, not top-level
  ones.** At the top level Hermes fails argument parsing before the agent runs.
- **Hermes prints its session id on stderr.** Capturing it there is what makes
  per-side memory work; missing it silently degrades a game to stateless play,
  and nothing looks broken because the moves still land.

## Fairness properties

Every player gets the same tools:

| Tool | Purpose |
|---|---|
| `join_game` | Handshake: your colour, your opponent, the current position |
| `get_board` | FEN, board diagram, side to move, check state, clocks, material, history |
| `get_legal_moves` | Every legal move in the position, as SAN and UCI |
| `make_move` | Play a move (SAN or UCI). Rejected moves change nothing. |
| `get_move_history` | SAN list so far |
| `get_status` | Turn, clocks, result, whether the game is over |
| `get_evaluation` | Objective facts only — material and mobility, no engine score |
| `offer_draw` / `accept_draw` / `claim_draw` | Draw handling |
| `resign_game` | Resign |

## Data and artifacts

Everything lives under `$LLM_CHESS_HOME` (default `~/.llm-chess`):

- `chess.db` — the game store: positions, move log, clock state, event feed
- `games/<game_id>.pgn` — PGN written when a game finishes
- `games/<game_id>.log` — the full transcript of both agents, for post-mortems

## Fairness properties

These are the guarantees the design actually enforces, and the tests that prove
them:

| Property | Enforced by |
|---|---|
| A player can only move its own colour | `arbiter._require_player` |
| A player can only move on its turn | `arbiter.move` |
| Illegal moves are rejected with no state change | `rules.parse_move` + single write transaction |
| A finished game accepts no further moves | `arbiter._require_active` |
| Clocks are server-side, not agent-reported | `arbiter._enforce_clock` |
| Neither agent sees the other's context | separate MCP processes, position-only tool surface |

## Development

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```

The suite is 70 tests across five layers, and the split is deliberate:

| File | Covers |
|---|---|
| `tests/test_arbiter.py` | Rule enforcement — turn order, colour binding, legality, clocks, and a two-process race for the same ply |
| `tests/test_mcp_stdio.py` | Real MCP over stdio: tool discovery, two clients playing one board, recoverable errors |
| `tests/test_driver.py` | Agent adapters (Hermes/Claude argv and output parsing), prompts, forfeit policy, artifacts, and an agent that reports success without moving |
| `tests/test_gui.py` | The spectator API against the arbiter's store |
| `tests/test_ui_browser.py` | The page in a real browser (Playwright). Skips if playwright is absent. |

The browser tests earn their keep: they caught two bugs the API tests could not
see — the client rendering from a field the API never sent, and plain `uvicorn`
shipping no WebSocket transport, which 404'd every live connection at handshake.

```bash
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium   # if not already cached
```

## Credits

Chess piece artwork is the Cburnett SVG set (Wikimedia Commons / Lichess),
licensed CC BY-SA 3.0.

## Licence

MIT (code). See above for the piece artwork.