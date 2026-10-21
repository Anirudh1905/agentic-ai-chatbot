#!/usr/bin/env bash
# The deployed app, checked from outside through the gateway (Phase 9; CI runs it after deploying
# to kind): ready, /metrics not public, register and log in, Grafana and Alertmanager behind the
# gateway, Prometheus scraping both API pods and the worker with the alert rules loaded, and the
# worker reading the queue.
set -euo pipefail
BASE=${BASE:-http://localhost:8088}

# Waits up to $2 seconds for the command in $1 to succeed.
retry() {
  local deadline=$((SECONDS + $2))
  until eval "$1" >/dev/null 2>&1; do
    if [ $SECONDS -ge $deadline ]; then echo "FAIL: $1" >&2; return 1; fi
    sleep 5
  done
}

# The number of series a Prometheus query returns.
series() {
  curl -fsS "$BASE/prometheus/api/v1/query" --data-urlencode "query=$1" \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]["result"]))'
}

retry "curl -fsS $BASE/api/readyz" 180
echo "ok: ready"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/metrics")
[ "$code" = 404 ] || { echo "FAIL: /api/metrics answered $code, want 404" >&2; exit 1; }
echo "ok: /metrics not public"

email="smoke-$(date +%s)@example.com"
password="smoke-$RANDOM-$RANDOM-password"
curl -fsS "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\": \"$email\", \"password\": \"$password\"}" >/dev/null
token=$(curl -fsS "$BASE/api/auth/login" --data-urlencode "username=$email" \
  --data-urlencode "password=$password" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -fsS "$BASE/api/me/memory" -H "Authorization: Bearer $token" >/dev/null
echo "ok: register, log in, an authenticated call"

# Their APIs, not just a 200: an unrouted path falls through to the UI, which answers 200 too.
curl -fsS "$BASE/grafana/api/health" | grep -q '"database": *"ok"' \
  || { echo "FAIL: Grafana isn't behind /grafana" >&2; exit 1; }
curl -fsS "$BASE/alertmanager/api/v2/status" | grep -q '"cluster"' \
  || { echo "FAIL: Alertmanager isn't behind /alertmanager" >&2; exit 1; }
echo "ok: Grafana and Alertmanager"

retry '[ "$(series "up{namespace=\"chatbot\"} == 1")" -ge 3 ]' 180
echo "ok: Prometheus scrapes at least 3 chatbot targets"

rules=$(curl -fsS "$BASE/prometheus/api/v1/rules" \
  | python3 -c 'import json,sys; print(sum(len(g["rules"]) for g in json.load(sys.stdin)["data"]["groups"] if g["name"] == "chatbot"))')
[ "$rules" -ge 10 ] || { echo "FAIL: $rules chatbot alert rules loaded, want 10" >&2; exit 1; }
echo "ok: $rules alert rules loaded"

# The worker reports the queue's depth only after it has read it from ElasticMQ.
retry '[ "$(series "chatbot_queue_messages")" -ge 3 ]' 120
echo "ok: the worker reads the queue"
