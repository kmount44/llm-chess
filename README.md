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

**Hermes** — prefer the CLI over hand-editing YAML:

```bash
hermes mcp add chess --command /ABSOLUTE/PATH/llm-chess/.venv/bin/chess-arbiter \
                     --args --client hermes
```

It prompts for tool selection, so it needs a TTY; unattended, pipe the answers
(`printf 'n\ny\n' | hermes mcp add ...`). Restart Hermes afterwards — MCP servers
are discovered at startup, with no hot reload.

The equivalent YAML, if you would rather write it yourself:

```yaml
mcp_servers:
  chess:
    command: "/ABSOLUTE/PATH/llm-chess/.venv/bin/chess-arbiter"
    args: ["--client", "hermes"]
    timeout: 60
```

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

## Running across two machines

The arbiter and its store must live on **one** host — that shared store is the
single source of truth. If each player ran its own arbiter against its own local
file, you would have two boards and the fairness property would be gone. So the
split is: arbiters on one machine, players connecting to them over the network.

Hermes on a VM and Claude on a Mac is a supported topology, not a workaround:

```bash
# On the host (owns the store, runs both arbiters)
./scripts/serve-match.sh
#   started hermes arbiter on 100.118.227.23:8788
#   started claude arbiter on 100.118.227.23:8790

# Create the game — no CLI needs to be present for this step
chess-play --white hermes --black claude --create-only

# Register the remote arbiter where Claude runs, then play
claude mcp add --transport http chess http://100.118.227.23:8790/mcp
./scripts/play-claude.sh <GAME_ID>
```

Each identity gets its own process and port, exactly as with stdio. That is what
stops one player speaking for the other: your colour is decided by which port you
were told to connect to, not by anything you claim.

There is no orchestrator process in this mode. `wait_for_turn` blocks until it is
that client's turn and the arbiter releases it, so each agent simply plays its own
side. Nothing has to stay alive on a third machine, and nothing is trusted to
enforce the rules except the arbiter.

`scripts/remote-bot.py` is the quickest way to prove the network path from the
other machine before spending tokens:

```bash
./scripts/remote-bot.py --url http://<host>:8790/mcp --moves e5 Nc6 Nf6
```

Run it from the repo root. It is executable and hands off to the repo's `.venv`
if you invoke it with a stock `python3`, so the command above works as written;
if no virtualenv exists it says so rather than dying with an `ImportError`.

It also survives a dropped link rather than dying: a lost transport is reported
with the likely cause and retried with backoff. That is safe because the game
lives in the arbiter's store, not in the client — which is worth saying plainly,
since it is the same property that makes the whole design fair. Verified by
killing the arbiter under a parked bot: it reported the loss, backed off, and
resumed the same game against the restarted arbiter.

Binding is not a formality. `--serve` accepts only the Host headers you name
(`--allow-host`), which is why `serve-match.sh` passes the tailnet address
explicitly — reach the server by an address you did not allow and it is refused.
Set `LLM_CHESS_TOKEN` to require a bearer token; the ports are otherwise open to
anything that can route to them.

## Verified

These are real runs, not claims:

- A live Hermes agent played `e4`, `Nf3`, `Bb5` through the network transport
  while a second client, connected over the tailnet on a different port, answered
  `e5`, `Nc6`, `Nf6` — both arbiters and the store on the host, both players
  reaching them remotely.
- Two arbiter processes on separate ports, each bound to its own identity, both
  reading one store; a client on one port cannot move for the other.
- A client parked in `wait_for_turn` was released by the opponent's move and not
  by its own timeout.
- Two MCP clients playing one board over real stdio, with a rejected illegal move
  leaving the position untouched.
- The spectator page rendering a position square-for-square against its FEN, and
  receiving a move played by a separate process without a reload.
- A two-process race for the same ply, where exactly one writer wins.

| Suite | Covers |
|---|---|
| `tests/test_arbiter.py` | Rule enforcement: turn order, colour binding, legality, clocks, race safety |
| `tests/test_mcp_stdio.py` | Real MCP over stdio: discovery, two clients, recoverable errors |
| `tests/test_mcp_http.py` | Real MCP over HTTP: remote clients, identity-per-port, `wait_for_turn`, auth, Host guard |
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
- **A bind address is not a listening address.** Binding an arbiter to the tailnet
  IP means `127.0.0.1` no longer reaches it. Point local clients at the same
  address the remote ones use, or bind `0.0.0.0` and accept LAN exposure.
- **Never hand-edit `~/.hermes/config.yaml` if you can avoid it.** One unbalanced
  quote there makes Hermes fall back to a last-known-good backup and silently
  ignore every later edit — including ones made with the CLI. Validate with
  `python -c "import yaml;yaml.safe_load(open('...'))"` after any manual change.
- **`wait_for_turn` blocks the tool call.** Keep the client's MCP timeout above
  the `timeout` you pass, or the transport gives up before the arbiter does.

## The tool surface

Every player gets the same tools:

| Tool | Purpose |
|---|---|
| `join_game` | Handshake: your colour, your opponent, the current position |
| `get_board` | FEN, board diagram, side to move, check state, clocks, material, history |
| `get_legal_moves` | Every legal move in the position, as SAN and UCI |
| `make_move` | Play a move (SAN or UCI). Rejected moves change nothing. |
| `wait_for_turn` | Block until it is your turn, the game ends, or the wait expires |
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

The suite is 92 tests across seven layers, and the split is deliberate:

| File | Covers |
|---|---|
| `tests/test_arbiter.py` | Rule enforcement — turn order, colour binding, legality, clocks, `wait_for_turn`, and a two-process race for the same ply |
| `tests/test_mcp_stdio.py` | Real MCP over stdio: tool discovery, two clients playing one board, recoverable errors |
| `tests/test_mcp_http.py` | Real MCP over HTTP: servers as subprocesses, one identity per port, remote turn enforcement, bearer auth, Host-header guard |
| `tests/test_remote_bot.py` | The connectivity script: error unwrapping, actionable advice, clean failure with no arbiter listening |
| `tests/test_driver.py` | Agent adapters (Hermes/Claude argv and output parsing), prompts, forfeit policy, artifacts, and an agent that reports success without moving |
| `tests/test_gui.py` | The spectator API against the arbiter's store |
| `tests/test_ui_browser.py` | The page in a real browser (Playwright). Skips if playwright is absent. |

The transport tests are the ones that would catch a silent regression, because
every failure mode they cover — a wrong flag, a session id read from the wrong
stream, a token check that never fires — leaves a game that still appears to work.

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