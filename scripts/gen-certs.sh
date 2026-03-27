#!/usr/bin/env bash
# Generate mTLS certificates for Vaire remote access.
#
# Usage:
#   scripts/gen-certs.sh [output_dir]
#
# Default output: ~/.vaire-dev/certs/ (dev) or pass a custom directory.
# Generates: CA cert, server cert+key, client cert+key.
set -euo pipefail

OUT="${1:-$HOME/.vaire-dev/certs}"
mkdir -p "$OUT"

CA_DAYS=3650      # 10 years
CERT_DAYS=365     # 1 year
KEY_SIZE=4096

echo "=== Vaire mTLS Certificate Generator ==="
echo "Output directory: $OUT"
echo ""

# ── CA ──────────────────────────────────────────────────────────────────
if [ ! -f "$OUT/ca.key" ]; then
    echo "[1/3] Generating CA key + certificate..."
    openssl genrsa -out "$OUT/ca.key" "$KEY_SIZE" 2>/dev/null
    openssl req -new -x509 -key "$OUT/ca.key" -out "$OUT/ca.crt" \
        -days "$CA_DAYS" -subj "/CN=Vaire CA/O=Ilmarin" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
    chmod 600 "$OUT/ca.key"
    echo "  CA cert: $OUT/ca.crt"
else
    echo "[1/3] CA already exists, skipping."
fi

# ── Server cert ─────────────────────────────────────────────────────────
if [ ! -f "$OUT/server.key" ]; then
    echo "[2/3] Generating server key + certificate..."
    openssl genrsa -out "$OUT/server.key" "$KEY_SIZE" 2>/dev/null

    # Create a config with SAN for localhost + mesh IPs
    cat > "$OUT/server.cnf" <<SERVERCNF
[req]
default_bits = $KEY_SIZE
prompt = no
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = vaire-server
O = Ilmarin

[v3_req]
subjectAltName = @alt_names
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
DNS.1 = localhost
DNS.2 = vaire
DNS.3 = vaire-dev
IP.1 = 127.0.0.1
IP.2 = 0.0.0.0
SERVERCNF

    openssl req -new -key "$OUT/server.key" -out "$OUT/server.csr" \
        -config "$OUT/server.cnf"
    openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
        -CAcreateserial -out "$OUT/server.crt" -days "$CERT_DAYS" \
        -extfile "$OUT/server.cnf" -extensions v3_req 2>/dev/null
    chmod 600 "$OUT/server.key"
    rm -f "$OUT/server.csr" "$OUT/server.cnf"
    echo "  Server cert: $OUT/server.crt"
else
    echo "[2/3] Server cert already exists, skipping."
fi

# ── Client cert ─────────────────────────────────────────────────────────
CLIENT_CN="${2:-test-client}"
CLIENT_PREFIX="client"
if [ ! -f "$OUT/${CLIENT_PREFIX}.key" ]; then
    echo "[3/3] Generating client key + certificate (CN=$CLIENT_CN)..."
    openssl genrsa -out "$OUT/${CLIENT_PREFIX}.key" "$KEY_SIZE" 2>/dev/null

    cat > "$OUT/${CLIENT_PREFIX}.cnf" <<CLIENTCNF
[req]
default_bits = $KEY_SIZE
prompt = no
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = $CLIENT_CN
O = Ilmarin

[v3_req]
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
CLIENTCNF

    openssl req -new -key "$OUT/${CLIENT_PREFIX}.key" -out "$OUT/${CLIENT_PREFIX}.csr" \
        -config "$OUT/${CLIENT_PREFIX}.cnf"
    openssl x509 -req -in "$OUT/${CLIENT_PREFIX}.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
        -CAcreateserial -out "$OUT/${CLIENT_PREFIX}.crt" -days "$CERT_DAYS" \
        -extfile "$OUT/${CLIENT_PREFIX}.cnf" -extensions v3_req 2>/dev/null
    chmod 600 "$OUT/${CLIENT_PREFIX}.key"
    rm -f "$OUT/${CLIENT_PREFIX}.csr" "$OUT/${CLIENT_PREFIX}.cnf"
    echo "  Client cert: $OUT/${CLIENT_PREFIX}.crt (CN=$CLIENT_CN)"
else
    echo "[3/3] Client cert already exists, skipping."
fi

rm -f "$OUT/ca.srl"

echo ""
echo "=== Done ==="
echo "CA:     $OUT/ca.crt"
echo "Server: $OUT/server.crt + $OUT/server.key"
echo "Client: $OUT/${CLIENT_PREFIX}.crt + $OUT/${CLIENT_PREFIX}.key"
echo ""
echo "Test with:"
echo "  curl --cert $OUT/${CLIENT_PREFIX}.crt --key $OUT/${CLIENT_PREFIX}.key \\"
echo "       --cacert $OUT/ca.crt https://127.0.0.1:8744/health"
