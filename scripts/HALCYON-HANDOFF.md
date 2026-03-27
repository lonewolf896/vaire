# Vaire Remote Server Configuration Handoff

This document is for the Claude Code session running ON THE REMOTE SERVER to complete Vaire configuration after the deployment script has run.

## Context

Vaire is being migrated from a workstation to an always-on server. The `deploy-halcyon.sh` script has already:
- Cloned/pulled the source via git
- Copied `memory.db` to `~/.vaire/`
- Generated TLS certificates in `~/.vaire/certs/`
- Built the Docker image

Your job is to configure docker-compose.yml with mTLS, verify the deployment, and set up the groomer.

## Step 1: Update docker-compose.yml

Edit `~/workspace/vaire/docker-compose.yml` to add mTLS configuration:

```yaml
services:
  vaire:
    build: .
    container_name: vaire
    user: "${UID}:${GID}"
    volumes:
      - ${HOME}/.vaire:/data
      - ${HOME}/workspace:/workspace:ro
      - ${HOME}/.vaire/certs:/certs:ro       # ADD THIS
    environment:
      - VAIRE_PATH_REMAP=/workspace:${HOME}/workspace
      - VAIRE_COGNITIVE_LOAD_LIMIT=8
      # mTLS remote access                    # ADD THESE
      - VAIRE_TLS_CERT=/certs/server.crt
      - VAIRE_TLS_KEY=/certs/server.key
      - VAIRE_TLS_CA=/certs/ca.crt
      - VAIRE_HTTPS_BIND=0.0.0.0:8743        # Docker-internal; ports: controls exposure
      - VAIRE_INGEST_ALLOWED_DIRS=/workspace
      # Existing settings (keep as-is)
      - VAIRE_NLI_RERANKING_ENABLED=false
      - VAIRE_COMET_ENRICHMENT_ENABLED=false
      - VAIRE_CONCEPTNET_ENRICHMENT_ENABLED=false
      - VAIRE_WRRF_FTS_WEIGHT=0.3
      - VAIRE_CANDIDATE_POOL_MULTIPLIER=5
      - VAIRE_WRRF_CANDIDATE_MULTIPLIER=5
      - VAIRE_WRITE_GATE_THRESHOLD=0.55
    ports:                                    # ADD THIS
      - "<MESH_IP>:8743:8743"                 # Replace <MESH_IP> with server's mesh/LAN IP
    healthcheck:
      test: ["CMD", "python", "-m", "vaire", "health"]
      interval: 60s
      timeout: 30s
      retries: 5
      start_period: 60s
    restart: unless-stopped
```

Key points:
- Replace `<MESH_IP>` with this server's mesh/LAN IP
- `PATH_REMAP` uses THIS server's home dir
- Port 8743 exposed only on the specified interface

## Step 2: Configure groomer allowlist

Create `~/.vaire/vaire.ini` with the approved groomer agent IDs:

```ini
[groomer]
approved = groomer-vale
```

Required because groomer role assignment needs an explicit allowlist (security hardening R10).

## Step 3: Restart with new config

```bash
cd ~/workspace/vaire
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

## Step 4: Verify deployment

```bash
# 1. Container health
docker ps | grep vaire
docker logs vaire --tail 20

# 2. Internal health check
docker exec vaire python -m vaire health

# 3. Database integrity
docker exec vaire python -c "
from vaire.storage import StorageEngine
s = StorageEngine('/data/memory.db')
stats = s.get_memory_stats()
print(f'Memories: {stats[\"total_memories\"]}')
print(f'Active: {stats[\"active_memories\"]}')
"

# 4. mTLS endpoint (local test)
curl --cert ~/.vaire/certs/client.crt \
     --key ~/.vaire/certs/client.key \
     --cacert ~/.vaire/certs/ca.crt \
     https://127.0.0.1:8743/health

# 5. Socket server (local)
python -m vaire health
```

## Step 5: Register local MCP client

For the groomer (Vale) running locally on this server:

```bash
claude mcp add -s user vaire -- python -m vaire client
```

Test: `recall("test")` should return results from the migrated DB.

## Step 6: Verify remote access

On the workstation, the operator runs `scripts/configure-remote-client.sh`. Then test:

```bash
# From the workstation
curl --cert ~/.vaire/certs/client.crt \
     --key ~/.vaire/certs/client.key \
     --cacert ~/.vaire/certs/ca.crt \
     https://<SERVER_MESH_IP>:8743/health
```

## Step 7: Set up Litestream NFS replication (optional)

If NFS is mounted, update `litestream.yml`:

```yaml
dbs:
  - path: /data/memory.db
    replicas:
      - path: /data/replicas/memory.db
        retention: 24h
        retention-check-interval: 1h
      # NFS backup replica
      - path: /mnt/nfs/backups/vaire/memory.db
        retention: 72h
        retention-check-interval: 6h
```

## Acceptance Criteria

- [ ] Vaire container running with `restart: unless-stopped`
- [ ] `docker exec vaire python -m vaire health` returns OK
- [ ] Memory count matches source DB
- [ ] mTLS health check from workstation succeeds
- [ ] Local socket works for groomer
- [ ] Litestream replicating (check `ls -la ~/.vaire/replicas/`)

## Rollback

1. Stop remote Vaire: `docker compose down`
2. On workstation: `docker compose up -d` (DB still there)
3. On workstation: `claude mcp remove vaire && claude mcp add -s user vaire -- python -m vaire client`

## Architecture After Migration

```
Workstation:
  Claude Code → mcp-remote → HTTPS (mTLS) → server:8743

Server:
  Vaire Docker container
    ├─ Unix socket (local: ~/.vaire/vaire.sock) ← groomer
    ├─ HTTPS :8743 (mTLS) ← remote agents
    ├─ SQLite + FTS5 + vec0 (memory.db)
    ├─ Litestream → replicas + NFS
    └─ 3 ML models (MiniLM, GTE-reranker, doc2query T5)
```
