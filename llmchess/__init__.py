"""LLM-vs-LLM chess over MCP.

Two autonomous agents (Hermes, Claude) play each other through a single
arbitrated MCP server. The arbiter — not the models — owns the board: turn
order, colour binding and move legality are enforced outside both agents, so
neither can move twice, move out of turn, or desync the position.
"""

__version__ = "0.1.0"