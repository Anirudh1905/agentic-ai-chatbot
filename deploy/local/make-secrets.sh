#!/usr/bin/env bash
# make secrets: deploy/local/secrets.env (gitignored) from .env, for the kind overlay's Secret,
# and deploy/local/ca-bundle.pem, the trust store its pods use.
set -euo pipefail
cd "$(dirname "$0")/../.."
KEYS="OPENAI_API_KEY OPENAI_BASE_URL JWT_SECRET LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY"
out=deploy/local/secrets.env
: > "$out"
chmod 600 "$out"
for key in $KEYS; do
  value=$(grep -sE "^${key}=" .env | tail -1 | cut -d= -f2- || true)
  if [ -n "$value" ]; then
    echo "${key}=${value}" >> "$out"
  fi
done
echo "wrote $out ($(wc -l < "$out" | tr -d ' ') keys)"

# The backup store's keys (deploy/base/backup.yaml), made up once: the backups in MinIO are
# only readable with the keys they were written with.
store=deploy/local/backup-store.env
if [ ! -s "$store" ]; then
  (umask 077; printf 'ACCESS_KEY_ID=backups\nACCESS_SECRET_KEY=%s\n' "$(openssl rand -hex 24)" > "$store")
  echo "wrote $store"
fi

# The cluster's trust store: this machine's roots, including any a corporate TLS-inspecting proxy
# installs (the app trusts the OS store, through truststore; a container's doesn't have them).
# Without a macOS keychain (Linux, CI), the system's bundle, else certifi's.
ca=deploy/local/ca-bundle.pem
if command -v security >/dev/null 2>&1; then
  security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > "$ca"
  security find-certificate -a -p /Library/Keychains/System.keychain >> "$ca"
elif [ -f /etc/ssl/certs/ca-certificates.crt ]; then
  cp /etc/ssl/certs/ca-certificates.crt "$ca"
else
  cp "$(uv run python -m certifi)" "$ca"
fi
echo "wrote $ca ($(grep -c 'BEGIN CERTIFICATE' "$ca") certificates)"
