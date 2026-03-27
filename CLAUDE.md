# Vaire — Developer Guide for Claude Code

## Role: vaire-dev (auto-load)

On session start, load the Vaire-Dev role: `recall("role:vaire-dev")`. This role includes Vaire-specific context queries, key file locations, testing commands, and cross-domain security awareness. Also check for open tasks: `recall("task-repo role:dev status:open")`.

## Project overview

Vaire is a persistent memory engine for Claude Code. The source lives under `vaire/` and is installed as a Python package. It runs inside Docker and exposes a Unix domain socket for local agents and an optional mTLS HTTPS endpoint for remote agents.

## Running locally

```bash
# Start the production server (Unix socket only)
UID=$(id -u) GID=$(id -g) docker compose up -d --build

# Start the dev server (Unix socket + mTLS HTTPS on port 8744)
UID=$(id -u) GID=$(id -g) docker compose -f docker-compose.dev.yml up -d --build

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
Local agents:
  Claude Code → python -m vaire client (MCP thin client)
    → ~/.vaire/vaire.sock (Unix Domain Socket)
    → Vaire Server (Docker)
    → ~/.vaire/memory.db (SQLite + FTS5 + vec0)
    → Litestream → ~/.vaire/replicas/

Remote agents (mTLS):
  Remote Claude Code → HTTPS POST (client cert required)
    → MTLSMiddleware (sets transport_ctx, extracts X-Vaire-CN)
    → FastMCP Starlette app (same MCP tools)
    → Vaire Server (Docker)
    → auto-tags all writes with "unprocessed"
```

## Remote access (mTLS)

Vaire supports authenticated remote access over HTTPS with mutual TLS. Disabled by default.

### Certificate setup

```bash
# Generate CA + server + client certs
scripts/gen-certs.sh ~/.vaire/certs

# Or for dev environment
scripts/gen-certs.sh ~/.vaire-dev/certs test-client

# Generate additional client certs (one per remote host)
scripts/gen-certs.sh ~/.vaire/certs my-remote-host
```

### Server configuration

Set these environment variables (or in `docker-compose.yml`):

```bash
VAIRE_TLS_CERT=/certs/server.crt    # Server certificate
VAIRE_TLS_KEY=/certs/server.key     # Server private key
VAIRE_TLS_CA=/certs/ca.crt          # CA cert (verifies client certs)
VAIRE_HTTPS_BIND=0.0.0.0:8743       # Inside Docker; host port mapping controls exposure
```

All four must be set together. The HTTPS server runs alongside the Unix socket server.

### Remote client setup

On the remote client (the machine connecting to Vaire):

```bash
# Copy client cert + key + CA cert to the client machine
scp ~/.vaire/certs/client.crt ~/.vaire/certs/client.key ~/.vaire/certs/ca.crt remote-host:~/.vaire/certs/

# Register Vaire as an MCP server in Claude Code
claude mcp add -s user vaire -- \
  npx @anthropic-ai/mcp-remote https://<SERVER_IP>:8743/mcp \
  --header "X-Vaire-CN: $(hostname)" \
  --tls-cert ~/.vaire/certs/client.crt \
  --tls-key ~/.vaire/certs/client.key \
  --tls-ca ~/.vaire/certs/ca.crt

# Test connectivity
curl --cert ~/.vaire/certs/client.crt --key ~/.vaire/certs/client.key \
     --cacert ~/.vaire/certs/ca.crt https://<SERVER_IP>:8743/health
```

For automated setup, use `scripts/configure-remote-client.sh` with `deploy.env`.

### Remote access security model

Remote agents have restricted permissions compared to local agents:

| Allowed remotely | Blocked remotely |
|---|---|
| `remember` (auto-tagged "unprocessed") | `forget` (no evidence deletion) |
| `recall`, `recall_hierarchical` | `rate_memory` (no metadata inflation) |
| `get_project_context` | `add_rule` (no global rule injection) |
| `memory_stats`, `validate_memory` | `create_trigger` (no trigger injection) |
| `anchor` (auto-tagged "unprocessed") | `ingest_file`, `ingest_directory`, `ingest_preview` |
| `checkpoint`, `restore` | `install_hooks`, `sync_instructions` |
| `get_project_story`, `get_rules` | `seed_project` |
| `navigate_memory`, `get_causal_chain` | |
| `assess_coverage`, `detect_gaps` | |
| `consolidate_now`, `drill_down` | |

All remote `remember()` and `anchor()` calls are automatically:
- Tagged with `"unprocessed"` for groomer review
- Stamped with `provenance_agent = "remote:{CN}"` from the X-Vaire-CN header
- Scanned for prompt injection patterns (flagged with `"_injection_warning"` if detected)
- Excluded from decision auto-protection and prospective trigger creation

### Groomer sanitization

The groomer agent (Vale) processes the "unprocessed" queue:

```
groom_sanitize(limit=50, auto_approve=False)  # review mode
groom_sanitize(limit=50, auto_approve=True)   # auto-process
```

Validation per memory: content length, injection pattern scan, tag validation, suspicious encoding detection. Results: clean → approved, suspicious → quarantined, rejected → heat zeroed.

## Security hardening

| Control | Setting | Default |
|---|---|---|
| Max content length | `VAIRE_MAX_CONTENT_LENGTH` | 50,000 chars |
| Max tags per memory | `VAIRE_MAX_TAG_COUNT` | 50 |
| Max tag length | `VAIRE_MAX_TAG_LENGTH` | 100 chars |
| Rate limit | `VAIRE_RATE_LIMIT_PER_MINUTE` | 120 req/min |
| Rate limit burst | `VAIRE_RATE_LIMIT_BURST` | 20 |
| Ingest allowed dirs | `VAIRE_INGEST_ALLOWED_DIRS` | "" (unrestricted for local) |
| Regex match cap | `VAIRE_REGEX_TIMEOUT_MATCHES` | 100 per scan |
| Injection detection | `VAIRE_PROMPT_INJECTION_DETECTION` | true |

Groomer role requires an explicit allowlist in `~/.vaire/vaire.ini`:
```ini
[groomer]
approved = vale-groomer
```

## Memory tools (MCP)

| Tool | When to use |
|---|---|
| `remember` | Store decisions, patterns, bugs fixed, constraints |
| `recall` | Retrieve relevant past context before starting work |
| `forget` | Remove stale or incorrect memories |
| `get_project_context` | Hot memories for a directory |
| `consolidate_now` | Force a full consolidation cycle (rarely needed — 3-tier daemon handles this) |
| `memory_stats` | System health check |

## Consolidation tiers

The astrocyte daemon runs continuously on three schedules:

| Tier | Interval | Phases |
|---|---|---|
| Light | 60s (`ACTION_LOG_INTERVAL`) | Heat decay + action log → outcome extraction |
| Medium | 15min (`MEDIUM_CYCLE_INTERVAL`) | Entity extraction + duplicate merge |
| Full | 5min idle (`IDLE_THRESHOLD_SECONDS`) | Causal discovery + memify + CLS + compression |
| Sleep | 6h gap (`SLEEP_CYCLE_MIN_GAP_HOURS`) | Dream replay + community detection |

Action log entries are grouped into 30-min time windows. Only complete windows are processed.
Outcome extraction transforms raw tool calls into narratives (files edited, git ops, errors).
The write gate filters redundant outcomes. Entity specificity filter rejects generic words.
