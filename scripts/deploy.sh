#!/usr/bin/env bash
# Vaire deployment script — test, build, swap, verify, or rollback.
#
# Usage:
#   ./scripts/deploy.sh              # Full deploy: test → build → swap → verify
#   ./scripts/deploy.sh --skip-tests # Build → swap → verify (skip test suite)
#   ./scripts/deploy.sh --rollback   # Rollback to previous image
#   ./scripts/deploy.sh --smoke-only # Just run smoke tests against running container
#
# The script automatically creates a backup tag of the current image before
# swapping, so rollback is always available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV="${PROJECT_DIR}/.venv/bin/python"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
QA_COMPOSE_FILE="${PROJECT_DIR}/docker-compose.qa.yml"
IMAGE_NAME="vaire-vaire"
BACKUP_TAG="${IMAGE_NAME}:rollback"
CONTAINER_NAME="vaire"
SOCKET_PATH="${HOME}/.vaire/vaire.sock"
QA_SOCKET_PATH="${HOME}/.vaire-qa/vaire.sock"
HEALTH_TIMEOUT=60
SMOKE_TIMEOUT=120

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[DEPLOY]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ── Parse arguments ──────────────────────────────────────────────────

SKIP_TESTS=false
ROLLBACK=false
SMOKE_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=true ;;
        --rollback)   ROLLBACK=true ;;
        --smoke-only) SMOKE_ONLY=true ;;
        --help|-h)
            echo "Usage: $0 [--skip-tests] [--rollback] [--smoke-only]"
            exit 0
            ;;
        *) fail "Unknown argument: $arg" ;;
    esac
done

cd "$PROJECT_DIR"

# ── Rollback ─────────────────────────────────────────────────────────

if $ROLLBACK; then
    log "Rolling back to previous image..."
    if ! docker image inspect "$BACKUP_TAG" &>/dev/null; then
        fail "No rollback image found. Tag '${BACKUP_TAG}' does not exist."
    fi

    docker tag "$BACKUP_TAG" "${IMAGE_NAME}:latest"
    rm -f "${HOME}/.vaire/vaire.pid"
    GID=$(id -g) docker compose down 2>/dev/null || true
    GID=$(id -g) docker compose up -d

    log "Waiting for health..."
    for i in $(seq 1 $((HEALTH_TIMEOUT / 2))); do
        status=$(docker inspect "$CONTAINER_NAME" --format='{{.State.Health.Status}}' 2>/dev/null || echo "missing")
        if [ "$status" = "healthy" ]; then
            log "Rollback complete. Container healthy."
            exit 0
        fi
        sleep 2
    done
    fail "Rollback container did not become healthy within ${HEALTH_TIMEOUT}s"
fi

# ── Smoke test only ──────────────────────────────────────────────────

smoke_test() {
    local socket="$1"
    local label="$2"
    log "Running smoke tests against ${label}..."

    VAIRE_SOCKET_PATH="$socket" "$VENV" -c "
import asyncio, sys, time, random
sys.path.insert(0, '.')
from vaire.socket_client import VaireClient

async def smoke():
    c = VaireClient('${socket}', call_timeout=${SMOKE_TIMEOUT}.0)
    errors = []

    # 1. memory_stats
    try:
        r = await c.call('memory_stats', {})
        assert 'total_memories' in r, 'missing total_memories'
        print(f'  memory_stats: OK ({r[\"total_memories\"]} memories)')
    except Exception as e:
        errors.append(f'memory_stats: {e}')

    # 2. remember with force
    mid = None
    try:
        token = random.randint(100000, 999999)
        r = await c.call('remember', {
            'force': True,
            'content': f'Deploy smoke test {token}',
            'context': '/tmp/deploy-smoke',
            'tags': ['deploy-smoke'],
        })
        mid = r.get('id') or r.get('memory_id')
        assert mid is not None, f'no id: {r}'
        print(f'  remember(force=True): OK (id={mid})')
    except Exception as e:
        errors.append(f'remember: {e}')

    # 3. recall
    try:
        r = await c.call('recall', {'query': f'smoke test {token}', 'max_results': 5})
        memories = r.get('result', r) if isinstance(r, dict) else r
        assert isinstance(memories, list), f'not a list: {type(memories)}'
        print(f'  recall: OK ({len([m for m in memories if not m.get(\"_budget_meta\")])} results)')
    except Exception as e:
        errors.append(f'recall: {e}')

    # 4. forget (cleanup)
    if mid:
        try:
            await c.call('forget', {'memory_id': mid})
            print(f'  forget: OK')
        except Exception as e:
            errors.append(f'forget: {e}')

    await c.disconnect()

    if errors:
        print(f'  FAILURES: {len(errors)}')
        for e in errors:
            print(f'    - {e}')
        sys.exit(1)
    else:
        print('  All smoke tests passed.')

asyncio.run(smoke())
" || return 1
}

if $SMOKE_ONLY; then
    if [ ! -S "$SOCKET_PATH" ]; then
        fail "Socket not found at ${SOCKET_PATH}. Is the container running?"
    fi
    smoke_test "$SOCKET_PATH" "production"
    exit 0
fi

# ── Step 1: Run test suite ───────────────────────────────────────────

if ! $SKIP_TESTS; then
    log "Step 1/5: Running unit tests..."
    "$VENV" -m pytest vaire/tests/ -x -q \
        --ignore=vaire/tests/test_stress.py \
        --ignore=vaire/tests/test_live_system.py \
        --ignore=vaire/tests/test_mcp_integration.py \
        --ignore=vaire/tests/test_mcp_throughput.py \
        -k "not test_recall_completes_under" \
        || fail "Unit tests failed. Aborting deploy."
    log "Unit tests passed."

    log "Running MCP integration tests (QA container)..."
    "$VENV" -m pytest vaire/tests/test_mcp_integration.py -x -q \
        || fail "MCP integration tests failed. Aborting deploy."
    log "MCP integration tests passed."
else
    warn "Skipping tests (--skip-tests)"
fi

# ── Step 2: Backup current image ─────────────────────────────────────

log "Step 2/5: Backing up current image..."
if docker image inspect "${IMAGE_NAME}:latest" &>/dev/null; then
    docker tag "${IMAGE_NAME}:latest" "$BACKUP_TAG"
    log "Current image backed up as ${BACKUP_TAG}"
else
    warn "No current image to backup (first deploy?)"
fi

# ── Step 3: Build new image ──────────────────────────────────────────

log "Step 3/5: Building new image..."
GID=$(id -g) docker compose build \
    || fail "Image build failed. Aborting deploy."
log "Image built successfully."

# ── Step 4: Swap container ───────────────────────────────────────────

log "Step 4/5: Swapping container..."
rm -f "${HOME}/.vaire/vaire.pid"
GID=$(id -g) docker compose down 2>/dev/null || true
GID=$(id -g) docker compose up -d \
    || fail "Container start failed. Run '$0 --rollback' to restore."

# Wait for healthy
log "Waiting for health check..."
for i in $(seq 1 $((HEALTH_TIMEOUT / 2))); do
    status=$(docker inspect "$CONTAINER_NAME" --format='{{.State.Health.Status}}' 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ]; then
        log "Container healthy after $((i * 2))s"
        break
    fi
    if [ "$status" = "unhealthy" ]; then
        warn "Container unhealthy — checking logs..."
        docker logs "$CONTAINER_NAME" --tail 10 2>&1
        # Try removing stale PID and restarting
        rm -f "${HOME}/.vaire/vaire.pid"
        docker restart "$CONTAINER_NAME" 2>/dev/null || true
        sleep 5
    fi
    sleep 2
done

status=$(docker inspect "$CONTAINER_NAME" --format='{{.State.Health.Status}}' 2>/dev/null || echo "missing")
if [ "$status" != "healthy" ]; then
    warn "Container not healthy. Rolling back..."
    GID=$(id -g) docker compose down 2>/dev/null || true
    if docker image inspect "$BACKUP_TAG" &>/dev/null; then
        docker tag "$BACKUP_TAG" "${IMAGE_NAME}:latest"
        rm -f "${HOME}/.vaire/vaire.pid"
        GID=$(id -g) docker compose up -d
        sleep 10
        fail "Deploy failed — rolled back to previous image."
    else
        fail "Deploy failed and no rollback image available."
    fi
fi

# ── Step 5: Smoke test ───────────────────────────────────────────────

log "Step 5/5: Running smoke tests..."
if ! smoke_test "$SOCKET_PATH" "production"; then
    warn "Smoke tests failed. Rolling back..."
    GID=$(id -g) docker compose down 2>/dev/null || true
    if docker image inspect "$BACKUP_TAG" &>/dev/null; then
        docker tag "$BACKUP_TAG" "${IMAGE_NAME}:latest"
        rm -f "${HOME}/.vaire/vaire.pid"
        GID=$(id -g) docker compose up -d
        sleep 10
        fail "Smoke tests failed — rolled back to previous image."
    else
        fail "Smoke tests failed and no rollback image available."
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────

log "Deploy complete."
docker logs "$CONTAINER_NAME" 2>&1 | grep -i "warm\|loaded\|version" | head -5
echo ""
log "Commands:"
log "  ./scripts/deploy.sh --smoke-only   # Re-run smoke tests"
log "  ./scripts/deploy.sh --rollback     # Rollback to previous image"
