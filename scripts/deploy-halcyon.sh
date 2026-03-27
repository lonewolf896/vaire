#!/usr/bin/env bash
# ============================================================================
# Vaire Migration: Deploy to Remote Server
# ============================================================================
#
# Deploys Vaire from the local workstation to a remote always-on server.
# Run this script from the workstation — it SSHes to the remote for setup.
#
# Prerequisites:
#   - Copy deploy.env.example to deploy.env and fill in your values
#   - SSH access to the remote server
#   - Docker + Docker Compose installed on the remote
#   - Git access from the remote to the Vaire repo
#
# Usage:
#   scripts/deploy-halcyon.sh [--dry-run] [--skip-build] [--skip-db]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# ── Load configuration from deploy.env ────────────────────────────────────
if [ ! -f "${REPO_DIR}/deploy.env" ]; then
    echo "ERROR: deploy.env not found. Copy deploy.env.example and fill in your values."
    exit 1
fi
# shellcheck source=/dev/null
source "${REPO_DIR}/deploy.env"

# Validate required vars
for var in VAIRE_DEPLOY_HOST VAIRE_DEPLOY_IP VAIRE_DEPLOY_USER \
           VAIRE_DEPLOY_REPO_DIR VAIRE_DEPLOY_DATA_DIR VAIRE_DEPLOY_GIT_REMOTE; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: ${var} not set in deploy.env"
        exit 1
    fi
done

REMOTE_HOST="${VAIRE_DEPLOY_HOST}"
REMOTE_IP="${VAIRE_DEPLOY_IP}"
REMOTE_USER="${VAIRE_DEPLOY_USER}"
REMOTE_DATA="${VAIRE_DEPLOY_DATA_DIR}"
REMOTE_REPO="${VAIRE_DEPLOY_REPO_DIR}"
REMOTE_WORKSPACE="${VAIRE_DEPLOY_WORKSPACE:-$(dirname "$REMOTE_REPO")}"
GIT_REMOTE="${VAIRE_DEPLOY_GIT_REMOTE}"
GIT_BRANCH="${VAIRE_DEPLOY_GIT_BRANCH:-main}"
HTTPS_PORT="${VAIRE_DEPLOY_HTTPS_PORT:-8743}"

LOCAL_DATA="${HOME}/.vaire"
LOCAL_REPO="${REPO_DIR}"

DRY_RUN=false
SKIP_BUILD=false
SKIP_DB=false

# ── Parse args ────────────────────────────────────────────────────────────
for arg in "$@"; do
    case $arg in
        --dry-run)    DRY_RUN=true ;;
        --skip-build) SKIP_BUILD=true ;;
        --skip-db)    SKIP_DB=true ;;
        -h|--help)
            echo "Usage: $0 [--dry-run] [--skip-build] [--skip-db]"
            exit 0
            ;;
    esac
done

run() {
    echo "  [RUN] $*"
    if [ "$DRY_RUN" = false ]; then "$@"; fi
}

ssh_run() {
    echo "  [SSH:${REMOTE_HOST}] $*"
    if [ "$DRY_RUN" = false ]; then ssh "${REMOTE_HOST}" "$@"; fi
}

echo "============================================================"
echo " Vaire Migration to ${REMOTE_HOST}"
echo "============================================================"
echo " Source:  $(hostname) (local)"
echo " Target:  ${REMOTE_HOST} (${REMOTE_IP})"
echo " Repo:    ${GIT_REMOTE} @ ${GIT_BRANCH}"
echo " Dry run: ${DRY_RUN}"
echo "============================================================"
echo ""

# ── Step 1: Pre-flight checks ────────────────────────────────────────────
echo "=== Step 1: Pre-flight checks ==="

echo "  Checking SSH access to ${REMOTE_HOST}..."
if ! ssh -o ConnectTimeout=5 "${REMOTE_HOST}" "echo ok" &>/dev/null; then
    echo "ERROR: Cannot SSH to ${REMOTE_HOST}. Check SSH config."
    exit 1
fi
echo "  SSH: OK"

echo "  Checking Docker on ${REMOTE_HOST}..."
if ! ssh_run "docker info" &>/dev/null; then
    echo "ERROR: Docker not available on ${REMOTE_HOST}."
    exit 1
fi
echo "  Docker: OK"

echo "  Checking local Vaire health..."
if python -m vaire health &>/dev/null; then
    echo "  Local Vaire: running"
else
    echo "  Local Vaire: not running (OK — will copy DB directly)"
fi

echo "  Checking local DB exists..."
if [ ! -f "${LOCAL_DATA}/memory.db" ]; then
    echo "ERROR: No database at ${LOCAL_DATA}/memory.db"
    exit 1
fi
DB_SIZE=$(du -sh "${LOCAL_DATA}/memory.db" | cut -f1)
echo "  DB size: ${DB_SIZE}"

echo "  Checking local git is pushed..."
LOCAL_SHA=$(git -C "${LOCAL_REPO}" rev-parse --short HEAD)
LOCAL_AHEAD=$(git -C "${LOCAL_REPO}" rev-list --count @{u}..HEAD 2>/dev/null || echo "?")
if [ "$LOCAL_AHEAD" != "0" ] && [ "$LOCAL_AHEAD" != "?" ]; then
    echo "  WARNING: ${LOCAL_AHEAD} unpushed commits. Run 'git push' first."
    if [ "$DRY_RUN" = false ]; then
        read -p "  Continue anyway? [y/N] " -n 1 -r
        echo
        [[ $REPLY =~ ^[Yy]$ ]] || exit 1
    fi
fi
echo ""

# ── Step 2: Git clone/pull on remote ─────────────────────────────────────
echo "=== Step 2: Sync Vaire source code via git ==="

ssh_run "mkdir -p ${REMOTE_WORKSPACE}"

if ssh "${REMOTE_HOST}" "test -d ${REMOTE_REPO}/.git" 2>/dev/null; then
    echo "  Repo exists — pulling latest from ${GIT_BRANCH}..."
    ssh_run "cd ${REMOTE_REPO} && git fetch origin && git reset --hard origin/${GIT_BRANCH}"
else
    echo "  Cloning ${GIT_REMOTE} → ${REMOTE_REPO}..."
    ssh_run "git clone --branch ${GIT_BRANCH} ${GIT_REMOTE} ${REMOTE_REPO}"
fi

DEPLOYED_SHA=$(ssh "${REMOTE_HOST}" "cd ${REMOTE_REPO} && git rev-parse --short HEAD" 2>/dev/null)
DEPLOYED_MSG=$(ssh "${REMOTE_HOST}" "cd ${REMOTE_REPO} && git log --oneline -1" 2>/dev/null)
echo "  Deployed: ${DEPLOYED_MSG}"

if [ "$DEPLOYED_SHA" != "$LOCAL_SHA" ]; then
    echo "  WARNING: remote (${DEPLOYED_SHA}) != local (${LOCAL_SHA})"
fi
echo ""

# ── Step 3: Create directory structure on remote ──────────────────────────
echo "=== Step 3: Create directory structure ==="
ssh_run "mkdir -p ${REMOTE_DATA}/{certs,replicas}"
echo ""

# ── Step 4: Generate TLS certificates ────────────────────────────────────
echo "=== Step 4: Generate TLS certificates ==="

ssh_run "cd ${REMOTE_REPO} && bash scripts/gen-certs.sh ${REMOTE_DATA}/certs server"

# Generate a client cert for the local workstation
CLIENT_CN="${VAIRE_DEPLOY_CLIENT_CN:-$(hostname)}"
ssh_run "cd ${REMOTE_REPO} && bash scripts/gen-certs.sh ${REMOTE_DATA}/certs ${CLIENT_CN}"

echo "  Copying client certs to local machine..."
run mkdir -p "${LOCAL_DATA}/certs"
run scp "${REMOTE_HOST}:${REMOTE_DATA}/certs/ca.crt" "${LOCAL_DATA}/certs/"
run scp "${REMOTE_HOST}:${REMOTE_DATA}/certs/client.crt" "${LOCAL_DATA}/certs/"
run scp "${REMOTE_HOST}:${REMOTE_DATA}/certs/client.key" "${LOCAL_DATA}/certs/"
echo ""

# ── Step 5: Copy database ────────────────────────────────────────────────
if [ "$SKIP_DB" = false ]; then
    echo "=== Step 5: Copy database to remote ==="

    echo "  Stopping local Vaire (if running)..."
    run docker compose -f "${LOCAL_REPO}/docker-compose.yml" stop 2>/dev/null || true
    sleep 2

    echo "  Copying memory.db..."
    run rsync -avz "${LOCAL_DATA}/memory.db" "${REMOTE_HOST}:${REMOTE_DATA}/memory.db"
    for f in memory.db-wal memory.db-shm; do
        if [ -f "${LOCAL_DATA}/${f}" ]; then
            run rsync -avz "${LOCAL_DATA}/${f}" "${REMOTE_HOST}:${REMOTE_DATA}/${f}"
        fi
    done
    echo ""
else
    echo "=== Step 5: SKIPPED (--skip-db) ==="
    echo ""
fi

# ── Step 6: Build and start Vaire ─────────────────────────────────────────
if [ "$SKIP_BUILD" = false ]; then
    echo "=== Step 6: Build and start Vaire ==="
    ssh_run "cd ${REMOTE_REPO} && UID=\$(id -u) GID=\$(id -g) docker compose up -d --build"
    echo ""
else
    echo "=== Step 6: SKIPPED (--skip-build) ==="
    echo ""
fi

# ── Step 7: Wait for health check ────────────────────────────────────────
echo "=== Step 7: Health check ==="
echo "  Waiting for Vaire to start..."
for i in $(seq 1 30); do
    if ssh_run "docker exec vaire python -m vaire health" &>/dev/null; then
        echo "  Vaire healthy after ${i}s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Vaire failed to start within 30s"
        echo "  Check: ssh ${REMOTE_HOST} 'docker logs vaire'"
        exit 1
    fi
    sleep 1
done
echo ""

# ── Step 8: Test mTLS ────────────────────────────────────────────────────
echo "=== Step 8: Test mTLS connectivity ==="
if curl -sf \
    --cert "${LOCAL_DATA}/certs/client.crt" \
    --key "${LOCAL_DATA}/certs/client.key" \
    --cacert "${LOCAL_DATA}/certs/ca.crt" \
    "https://${REMOTE_IP}:${HTTPS_PORT}/health" 2>/dev/null; then
    echo "  mTLS connection: OK"
else
    echo "  WARNING: mTLS test failed — HTTPS may not be configured yet"
    echo "  See scripts/HALCYON-HANDOFF.md for docker-compose.yml setup"
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────────────
echo "============================================================"
echo " Deployment complete!"
echo "============================================================"
echo ""
echo " Next steps:"
echo "  1. Configure docker-compose.yml on remote (see HALCYON-HANDOFF.md)"
echo "  2. Run: scripts/configure-remote-client.sh"
echo "  3. Verify MCP tools work via HTTPS"
echo ""
echo " Rollback:"
echo "  - Restart local: cd ${LOCAL_REPO} && docker compose up -d"
echo "  - DB preserved at ${LOCAL_DATA}/memory.db"
echo "============================================================"
