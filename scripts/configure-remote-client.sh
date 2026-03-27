#!/usr/bin/env bash
# ============================================================================
# Vaire Migration: Configure Remote MCP Client
# ============================================================================
#
# After Vaire is running on the remote server, run this on the local
# workstation to switch Claude Code from local socket to remote mTLS HTTPS.
#
# Usage:
#   scripts/configure-remote-client.sh [--keep-local]
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

REMOTE_IP="${VAIRE_DEPLOY_IP}"
HTTPS_PORT="${VAIRE_DEPLOY_HTTPS_PORT:-8743}"
CERT_DIR="${HOME}/.vaire/certs"
CLIENT_CN="${VAIRE_DEPLOY_CLIENT_CN:-$(hostname)}"
KEEP_LOCAL=false

for arg in "$@"; do
    case $arg in
        --keep-local) KEEP_LOCAL=true ;;
        -h|--help)
            echo "Usage: $0 [--keep-local]"
            echo "  --keep-local  Don't stop the local Vaire container"
            exit 0
            ;;
    esac
done

echo "============================================================"
echo " Vaire Remote Client Configuration"
echo "============================================================"
echo " Target:  https://${REMOTE_IP}:${HTTPS_PORT}"
echo " Certs:   ${CERT_DIR}/"
echo "============================================================"
echo ""

# ── Step 1: Verify certs exist ───────────────────────────────────────────
echo "=== Step 1: Verify certificates ==="
for f in ca.crt client.crt client.key; do
    if [ ! -f "${CERT_DIR}/${f}" ]; then
        echo "ERROR: Missing ${CERT_DIR}/${f}"
        echo "  Run deploy script first to generate and copy certs"
        exit 1
    fi
    echo "  ${f}: OK"
done
echo ""

# ── Step 2: Test mTLS connectivity ───────────────────────────────────────
echo "=== Step 2: Test mTLS connectivity ==="
HEALTH=$(curl -sf \
    --cert "${CERT_DIR}/client.crt" \
    --key "${CERT_DIR}/client.key" \
    --cacert "${CERT_DIR}/ca.crt" \
    --max-time 10 \
    "https://${REMOTE_IP}:${HTTPS_PORT}/health" 2>&1) || {
    echo "ERROR: Cannot connect to Vaire on remote server"
    echo "  curl output: ${HEALTH}"
    echo ""
    echo "  Troubleshooting:"
    echo "    1. Is Vaire running? ssh ${VAIRE_DEPLOY_HOST} 'docker ps | grep vaire'"
    echo "    2. Is HTTPS enabled? Check VAIRE_HTTPS_BIND in docker-compose.yml"
    echo "    3. Is the port exposed? Check ports: section in docker-compose.yml"
    echo "    4. Is the network up? ping ${REMOTE_IP}"
    exit 1
}
echo "  Health response: ${HEALTH}"
echo ""

# ── Step 3: Update Claude Code MCP registration ──────────────────────────
echo "=== Step 3: Update Claude Code MCP registration ==="

echo "  Removing old local Vaire MCP server (if registered)..."
claude mcp remove vaire 2>/dev/null || true

echo "  Registering remote Vaire MCP server..."
claude mcp add -s user vaire -- \
    npx -y @anthropic-ai/mcp-remote \
    "https://${REMOTE_IP}:${HTTPS_PORT}/mcp" \
    --header "X-Vaire-CN: ${CLIENT_CN}" \
    --client-cert "${CERT_DIR}/client.crt" \
    --client-key "${CERT_DIR}/client.key" \
    --ca-cert "${CERT_DIR}/ca.crt"

echo "  MCP server registered: vaire → https://${REMOTE_IP}:${HTTPS_PORT}"
echo ""

# ── Step 4: Stop local Vaire (optional) ──────────────────────────────────
if [ "$KEEP_LOCAL" = false ]; then
    echo "=== Step 4: Stop local Vaire container ==="
    docker compose -f "${REPO_DIR}/docker-compose.yml" stop 2>/dev/null || true
    echo "  Local Vaire stopped (DB preserved at ~/.vaire/memory.db for rollback)"
    echo ""
else
    echo "=== Step 4: SKIPPED (--keep-local) ==="
    echo ""
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo "============================================================"
echo " Configuration complete!"
echo "============================================================"
echo ""
echo " Vaire MCP now points to ${REMOTE_IP}:${HTTPS_PORT}"
echo ""
echo " NOTE: Remote writes are auto-tagged 'unprocessed'."
echo " Run the groomer locally on the server for untagged writes."
echo ""
echo " Rollback:"
echo "   claude mcp remove vaire"
echo "   claude mcp add -s user vaire -- python -m vaire client"
echo "   docker compose -f ${REPO_DIR}/docker-compose.yml up -d"
echo "============================================================"
