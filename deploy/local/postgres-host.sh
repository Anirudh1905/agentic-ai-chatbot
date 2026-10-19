#!/usr/bin/env bash
# Publishes the cluster's Postgres on localhost:5433 (Phase 9), for the tests, make eval and
# database tools: a small socat container on kind's Docker network, forwarding to the
# postgres-host NodePort (postgres-host.yaml). Tilt runs it; safe to run again.
set -euo pipefail
NAME=chatbot-postgres-host
PORT=${POSTGRES_PORT:-5433}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped --network kind -p "127.0.0.1:$PORT:5432" \
  alpine/socat:1.8.0.3 tcp-listen:5432,fork,reuseaddr tcp-connect:chatbot-control-plane:30432 >/dev/null
echo "postgres on localhost:$PORT"
