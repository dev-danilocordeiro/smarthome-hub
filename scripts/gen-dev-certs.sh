#!/usr/bin/env bash
# Development PKI for the MQTT broker: a throwaway CA and a server certificate valid for
# the hostnames devices use (localhost on the host, `mqtt` inside compose).
# Output goes to infra/mqtt/certs/ (gitignored), or to $CERT_DIR. Never use these in production.
#
#   scripts/gen-dev-certs.sh [--force]
set -euo pipefail

DIR="${CERT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra/mqtt/certs}"
DAYS=825  # under the 825-day ceiling Apple and others enforce for TLS server certs

if [[ -f "$DIR/server.crt" && "${1:-}" != "--force" ]]; then
  echo "dev certificates already exist in $DIR (use --force to regenerate)"
  exit 0
fi

mkdir -p "$DIR"
cd "$DIR"
rm -f ./*.crt ./*.key ./*.csr ./*.srl ./*.ext

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout ca.key -out ca.crt -days "$DAYS" \
  -subj "/O=Smart Home Hub (dev)/CN=Smart Home Hub Dev CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout server.key -out server.csr \
  -subj "/O=Smart Home Hub (dev)/CN=mqtt" 2>/dev/null

cat > server.ext <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,DNS:mqtt,IP:127.0.0.1
EXT
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days "$DAYS" -extfile server.ext 2>/dev/null

rm -f server.csr server.ext ca.srl
# The broker runs as uid 1883 inside its container and must read its key.
chmod 644 ca.crt server.crt server.key
chmod 600 ca.key
echo "wrote dev CA and server certificate to $DIR"
