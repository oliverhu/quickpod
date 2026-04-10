#!/usr/bin/env bash
# Generate a small CA plus server + client certs for quickpod resources.mtls (see README).
# Usage: ./scripts/gen_mtls_certs.sh [output_dir]
set -euo pipefail
DIR="${1:-./mtls-certs}"
mkdir -p "$DIR"
cd "$DIR"

openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -out ca.pem -subj "/CN=quickpod-mtls-ca"
chmod 600 ca.key
chmod 644 ca.pem

openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 825 -sha256
chmod 600 server.key
chmod 644 server.pem
rm -f server.csr

openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=quickpod-client"
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out client.crt -days 825 -sha256
chmod 600 client.key
chmod 644 client.crt
rm -f client.csr

echo "Wrote CA, server (server.pem + server.key), client (client.crt + client.key) under $(pwd)"
echo "Add to .gitignore: *.key ca.key"
