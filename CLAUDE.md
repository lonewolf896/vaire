# Vaire — Developer Guide for Claude Code

## Project overview

Vaire is a persistent memory engine for Claude Code. The source lives under `vaire/` and is installed as a Python package. It runs inside Docker and exposes a Unix domain socket that a thin MCP client proxies to Claude Code.

## Running locally

```bash
# Start the server
UID=$(id -u) GID=$(id -g) docker compose up -d --build

# Check health
python -m vaire health

# Run tests
python -m pytest vaire/tests/ -x -q --ignore=vaire/tests/test_stress.py --ignore=vaire/tests/test_live_system.py
```

## Key conventions

- All configuration uses the `VAIRE_` environment variable prefix (see `vaire/config.py`)
- `StorageEngine` is the only class that touches the SQLite connection directly
- Schema migrations use the `schema_migrations` table — never modify an existing migration, append a new one
- Single-item lookups return `dict | None`; multi-item queries return `list[dict]`; inserts return `int`
- Embeddings are stored as `bytes` (numpy float32 `.tobytes()`)

## Architecture

```
Claude Code → python -m vaire client (MCP thin client)
  → ~/.vaire/vaire.sock (Unix Domain Socket)
  → Vaire Server (Docker)
  → ~/.vaire/memory.db (SQLite + FTS5 + vec0)
  → Litestream → ~/.vaire/replicas/
```

## Memory tools (MCP)

| Tool | When to use |
|---|---|
| `remember` | Store decisions, patterns, bugs fixed, constraints |
| `recall` | Retrieve relevant past context before starting work |
| `forget` | Remove stale or incorrect memories |
| `get_project_context` | Hot memories for a directory |
| `consolidate_now` | Force consolidation after a heavy session |
| `memory_stats` | System health check |
