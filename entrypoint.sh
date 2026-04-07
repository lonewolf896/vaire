#!/bin/bash
# Vaire entrypoint with circuit breaker + ES crash logging.
# Wraps litestream replicate -exec to track restart failures.
# If the child process crashes 3 times within WINDOW seconds, exit cleanly
# (code 0) so Docker's restart policy stops retrying.
# Crash events are shipped to ES via Logstash UDP syslog.

set -euo pipefail

RESTART_FILE="/data/.restart_count"
WINDOW=300  # 5 minutes
MAX_RESTARTS=3
LOGSTASH_HOST="${VAIRE_LOGSTASH_HOST:-10.0.10.31}"
LOGSTASH_PORT="${VAIRE_LOGSTASH_PORT:-5514}"
HOSTNAME="vaire-halcyon"

# Ship a structured log event to ES via Logstash UDP syslog input.
# Falls back to stderr if Logstash is unreachable — never blocks startup.
log_to_es() {
    local severity="$1"
    local message="$2"
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local payload="<14>${timestamp} ${HOSTNAME} vaire-entrypoint: severity=${severity} event=container_crash message=\"${message}\""
    echo "$payload" > /dev/udp/"$LOGSTASH_HOST"/"$LOGSTASH_PORT" 2>/dev/null || true
    echo "[entrypoint] ${severity}: ${message}" >&2
}

# Read previous restart state
now=$(date +%s)
if [[ -f "$RESTART_FILE" ]]; then
    read -r count last_ts < "$RESTART_FILE" 2>/dev/null || { count=0; last_ts=0; }
    elapsed=$((now - last_ts))
    if [[ $elapsed -gt $WINDOW ]]; then
        # Outside window — reset counter
        count=0
    fi
else
    count=0
fi

# Check circuit breaker
if [[ $count -ge $MAX_RESTARTS ]]; then
    log_to_es "CRITICAL" "Circuit breaker OPEN: ${count} crashes in ${WINDOW}s window. Container halted. Manual intervention required — delete ${RESTART_FILE} and restart."
    # Exit 0 so 'restart: on-failure' does NOT restart
    exit 0
fi

# Log startup
if [[ $count -gt 0 ]]; then
    log_to_es "WARNING" "Restarting after crash (attempt $((count + 1))/${MAX_RESTARTS})"
fi

# Run litestream + vaire
litestream replicate -config /etc/litestream.yml -exec "python -m vaire server"
exit_code=$?

# If we get here, the child exited
if [[ $exit_code -ne 0 ]]; then
    # Increment crash counter
    count=$((count + 1))
    echo "$count $now" > "$RESTART_FILE"
    log_to_es "ERROR" "Vaire exited with code ${exit_code}. Crash count: ${count}/${MAX_RESTARTS} in ${WINDOW}s window."
else
    # Clean exit (e.g. SIGTERM) — reset counter
    rm -f "$RESTART_FILE"
    log_to_es "INFO" "Vaire shut down cleanly (exit 0)."
fi

# Exit with the child's code so Docker restart policy can act
exit $exit_code
