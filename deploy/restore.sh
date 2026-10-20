#!/usr/bin/env bash
# Restore Postgres from its backups (Phase 9, deploy/base/backup.yaml) into a second cluster,
# `postgres-restore`, beside the live one: the latest base backup, then the archived WAL up to
# TARGET_TIME (an ISO timestamp, e.g. "2026-10-20 09:30:00+00"), or all of it without one.
#
#   deploy/restore.sh ["TARGET_TIME"]
#
# It checks the copy and prints how to switch the app over. Switching stays a decision for a
# person: it's an outage either way. Only the kind cluster: production would point at its own.
set -euo pipefail
CONTEXT=kind-chatbot
NS=chatbot
TARGET=${1:-}
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }

k delete cluster.postgresql.cnpg.io postgres-restore --ignore-not-found --wait >/dev/null
recovery_target=""
if [ -n "$TARGET" ]; then
  recovery_target="recoveryTarget: {targetTime: \"$TARGET\"}"
fi
image=$(k get cluster.postgresql.cnpg.io postgres -o jsonpath='{.spec.imageName}')
k apply -f - <<YAML
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: postgres-restore
spec:
  instances: 1
  imageName: $image
  storage:
    size: 2Gi
  bootstrap:
    recovery:
      source: postgres
      database: chatbot
      owner: chatbot
      $recovery_target
  # The live cluster's backups, read only: this copy archives nothing.
  externalClusters:
    - name: postgres
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: backups
          serverName: postgres
YAML
echo "restoring${TARGET:+ to $TARGET}…"
k wait cluster.postgresql.cnpg.io/postgres-restore --for=condition=Ready --timeout=15m >/dev/null

# SQL on a cluster's primary, as the superuser.
sql() {
  local pod
  pod=$(k get pod -l "cnpg.io/cluster=$1,cnpg.io/instanceRole=primary" -o name)
  k exec "$pod" -c postgres -- psql -U postgres -d chatbot -Atc "$2"
}
echo "restored. live vs copy:"
for query in \
  "SELECT count(*) FROM users" \
  "SELECT count(*) FROM threads" \
  "SELECT count(DISTINCT thread_id) FROM checkpoints" \
  "SELECT version_num FROM alembic_version" \
  "SELECT extversion FROM pg_extension WHERE extname = 'vector'"; do
  printf '  %-58s %8s %8s\n' "$query" "$(sql postgres "$query")" "$(sql postgres-restore "$query")"
done
cat <<'TEXT'

To switch the app to the copy: scale api and worker to 0, point DATABASE_URL at the copy's
Secret (postgres-restore-app, key uri) in deploy/base/app.yaml, apply, and scale back up.
To discard it: kubectl --context kind-chatbot -n chatbot delete cluster postgres-restore
TEXT
