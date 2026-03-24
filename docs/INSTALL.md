# Installation Guide

Vaire has two deployment modes: **Docker** (recommended) and **standalone**. Docker bundles the ML models into the image so startup is instant after the first build. Standalone runs directly on your machine.

## Prerequisites

- Python 3.11+
- Docker and Docker Compose (for Docker mode)
- ~1.5 GB disk for ML models (downloaded once)

## Option A: Docker (recommended)

Docker is the recommended way to run Vaire. The image bundles all three ML models so they never re-download on startup:

- **all-MiniLM-L6-v2** (~90 MB) — sentence embeddings
- **gte-reranker-modernbert-base** (~570 MB) — cross-encoder reranker
- **doc2query/msmarco-t5-small-v1** (~80 MB) — synthetic query generation

### 1. Clone and build

```bash
git clone https://github.com/lonewolf896/Vaire.git
cd Vaire
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

The first build takes a few minutes (model downloads). Subsequent rebuilds reuse cached layers and take seconds.

### 2. Install the thin client

The MCP client is a lightweight proxy that forwards tool calls to the Docker container over a Unix socket. Install it on the host:

```bash
pip install -e .
```

### 3. Register with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "vaire": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "vaire", "client"]
    }
  }
}
```

### 4. Verify

```bash
# Container running?
docker compose ps

# Server healthy?
python -m vaire health

# Socket exists?
ls ~/.vaire/vaire.sock
```

### Data locations

| Path | Contents |
|---|---|
| `~/.vaire/memory.db` | SQLite database (all memories) |
| `~/.vaire/vaire.sock` | Unix domain socket |
| `~/.vaire/vaire.pid` | Server PID file |
| `~/.vaire/replicas/` | Litestream WAL backups |

### Managing the server

```bash
# Start
UID=$(id -u) GID=$(id -g) docker compose up -d

# Rebuild after code changes
UID=$(id -u) GID=$(id -g) docker compose up -d --build

# Stop
docker compose down

# View logs
docker compose logs -f vaire
```

### Mounting directories for ingestion

Vaire can bulk-ingest markdown files, but the Docker container can only read mounted directories. Edit `docker-compose.yml` to add volumes:

```yaml
volumes:
  - ${HOME}/.vaire:/data
  - /path/to/your/project:/workspace/your-project:ro
```

If you mount host directories at a different container path, set the path remap so memories store the correct host-side paths:

```yaml
environment:
  - VAIRE_PATH_REMAP=/workspace:/home/youruser/workspace
```

Restart after editing:

```bash
UID=$(id -u) GID=$(id -g) docker compose up -d
```

---

## Option B: Standalone (no Docker)

For single-user setups where you don't want Docker overhead.

### 1. Install

```bash
pip install vaire
```

Or from source:

```bash
git clone https://github.com/lonewolf896/Vaire.git
cd Vaire
pip install -e .
```

### 2. Register with Claude Code

For stdio mode (starts and stops with each Claude Code session):

```json
{
  "mcpServers": {
    "vaire": {
      "command": "vaire"
    }
  }
}
```

For SSE mode (persistent background server):

```bash
vaire --transport sse --port 8742
```

```json
{
  "mcpServers": {
    "vaire": {
      "type": "sse",
      "url": "http://127.0.0.1:8742/sse"
    }
  }
}
```

### 3. Verify

```bash
python -m vaire health
```

Note: In standalone mode, ML models are downloaded on first use and cached for the process lifetime. The first `remember` or `recall` call will be slower while models load.

---

## Enabling hooks (optional but recommended)

Hooks give Vaire automatic context injection and capture. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python -m vaire context \"$PWD\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python -m vaire capture --from-stdin --directory \"$PWD\""
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python -m vaire drain \"$PWD\""
          }
        ]
      }
    ],
    "PostCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python -m vaire restore \"$PWD\""
          }
        ]
      }
    ]
  }
}
```

What each hook does:

| Hook | Fires when | Effect |
|---|---|---|
| `UserPromptSubmit` | You send a message | Injects project context into the session |
| `PostToolUse` | Claude uses any tool | Captures the action into an audit log |
| `PreCompact` | Context window is about to compress | Drains working state into a checkpoint |
| `PostCompact` | Context window was compressed | Reconstructs context from checkpoint + hot memories |

Or let Vaire install them for you via the MCP tool:

```
Tool: install_hooks
  project_directory: "/path/to/your/project"
```

---

## Teaching Claude to use Vaire

Add this to your global `~/.claude/CLAUDE.md`:

```markdown
## Memory
- On every new session, call `recall` with the current project name
- Before starting any task, call `get_project_context` for the current directory
- After completing significant work, call `remember` to store decisions and outcomes
- When the user switches to a new subject mid-session, call `restore` before responding
```

Or call `sync_instructions` once and Vaire will manage these instructions automatically.

---

## Configuration

All settings use the `VAIRE_` environment variable prefix. Set them in `docker-compose.yml` (Docker mode) or export them in your shell (standalone).

| Variable | Default | What it controls |
|---|---|---|
| `VAIRE_PORT` | `8742` | Server port (SSE mode) |
| `VAIRE_DB_PATH` | `~/.vaire/memory.db` | Database location |
| `VAIRE_SOCKET_PATH` | `~/.vaire/vaire.sock` | Unix socket path (Docker mode) |
| `VAIRE_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |
| `VAIRE_DECAY_FACTOR` | `0.95` | Heat decay per hour |
| `VAIRE_WRITE_GATE_THRESHOLD` | `0.55` | Minimum surprisal to store a memory |
| `VAIRE_COGNITIVE_LOAD_LIMIT` | `8` | Active context chunk limit |
| `VAIRE_WRRF_FTS_WEIGHT` | `0.3` | Weight of keyword signal in retrieval |

Full list in `vaire/config.py`.

---

## Upgrading

```bash
git pull
UID=$(id -u) GID=$(id -g) docker compose up -d --build
pip install -e .  # update the thin client
```

The database schema auto-migrates on startup. No manual migration steps needed.

## Uninstalling

```bash
docker compose down
pip uninstall vaire
rm -rf ~/.vaire  # removes database, socket, backups
```
