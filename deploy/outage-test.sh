#!/usr/bin/env bash
# make outage-test (Phase 12): proves a provider outage degrades to the fallback model, not
# errors, on the deployed stack. Puts a proxy (deploy/outage/) between the API and the model
# gateway that answers 503 for every gpt-5.4 chat call, then checks that chats still finish,
# gpt-5.4's circuit breaker opens, gpt-4.1 answers, and ChatbotBreakerOpen fires. Then takes the
# proxy away and checks gpt-5.4 answers again and its breaker closes. Only the kind cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT=kind-chatbot
NS=chatbot
BASE=${BASE:-http://localhost:8088}
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }

# A Prometheus query's first value, or 0.
q() {
  curl -fsS "$BASE/prometheus/api/v1/query" --data-urlencode "query=$1" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)'
}
# Waits up to $2 seconds for the condition in $1, running $3 (if given) between checks.
wait_for() {
  local deadline=$((SECONDS + $2))
  until eval "$1"; do
    [ $SECONDS -lt $deadline ] || { echo "FAIL: $1" >&2; return 1; }
    if [ -n "${3:-}" ]; then eval "$3"; else sleep 10; fi
  done
}
chats() { PYTHONPATH=tests uv run python tests/outage/chats.py "$1"; }

restored=false
restore() {
  $restored && return
  restored=true
  echo "restoring the API's model gateway"
  k set env deploy/api OPENAI_BASE_URL- >/dev/null
  k delete deploy/outage-proxy svc/outage-proxy cm/outage-proxy --ignore-not-found >/dev/null
  k rollout status deploy/api --timeout=300s >/dev/null
}
trap restore EXIT

image=$(k get deploy api -o jsonpath='{.spec.template.spec.containers[0].image}')
secrets=$(k get deploy api -o jsonpath='{.spec.template.spec.volumes[?(@.name=="secrets")].secret.secretName}')
ca_bundle=$(k get deploy api -o jsonpath='{.spec.template.spec.volumes[?(@.name=="ca-bundle")].configMap.name}')
k create configmap outage-proxy --from-file=deploy/outage/proxy.py --dry-run=client -o yaml \
  | k apply -f - >/dev/null
sed -e "s|API_IMAGE|$image|" -e "s|API_SECRETS|$secrets|" -e "s|API_CA_BUNDLE|$ca_bundle|" \
  deploy/outage/proxy.yaml | k apply -f - >/dev/null
k rollout status deploy/outage-proxy --timeout=120s >/dev/null
# The environment wins over the secret file (config.py), so this points the API at the proxy.
k set env deploy/api OPENAI_BASE_URL=http://outage-proxy:8000 >/dev/null
k rollout status deploy/api --timeout=300s >/dev/null
echo "outage on: every gpt-5.4 call answers 503"

chats 6
echo "ok: every chat finished during the outage"

wait_for '[ "$(q "max(chatbot_breaker_open{dependency=\"gpt-5.4\"})")" = 1 ]' 90
echo "ok: gpt-5.4's circuit breaker is open"
fallback=$(q 'sum(chatbot_model_call_duration_seconds_count{model="gpt-4.1"})')
[ "${fallback%.*}" -gt 0 ] || { echo "FAIL: no gpt-4.1 calls" >&2; exit 1; }
echo "ok: gpt-4.1 answered $fallback model calls"
echo "  proxy: $(k exec deploy/outage-proxy -- python -c 'import urllib.request; print(urllib.request.urlopen("http://localhost:8000/_outage").read().decode())')"

# The alert needs the breaker open for a minute; chats keep the outage visible meanwhile.
wait_for '[ "$(q "count(ALERTS{alertname=\"ChatbotBreakerOpen\",alertstate=\"firing\",dependency=\"gpt-5.4\"})")" = 1 ]' \
  300 'chats 1 >/dev/null; sleep 15'
echo "ok: ChatbotBreakerOpen is firing for gpt-5.4"

restore
echo "outage over"
# The breaker opened at most 30 s ago; once that cooldown passes, a chat's call probes gpt-5.4.
wait_for '[ "$(q "sum(chatbot_model_call_duration_seconds_count{model=\"gpt-5.4\"})")" != 0 ]' \
  300 'chats 1 >/dev/null; sleep 15'
echo "ok: gpt-5.4 answers again"
wait_for '[ "$(q "max(chatbot_breaker_open{dependency=\"gpt-5.4\"})")" = 0 ]' 120 'chats 1 >/dev/null; sleep 15'
echo "ok: its breaker closed"
