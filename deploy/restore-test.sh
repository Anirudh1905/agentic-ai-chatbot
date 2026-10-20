#!/usr/bin/env bash
# make restore-test (Phase 9): proves the backups restore to a point in time, not just that
# they exist. Writes a marker, notes the time T, writes a second marker, waits until the WAL
# holding both is archived, restores to T (deploy/restore.sh), and checks that the copy has
# the first marker and not the second. Then removes the copy and the markers.
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT=kind-chatbot
NS=chatbot
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
sql() {
  local pod
  pod=$(k get pod -l "cnpg.io/cluster=$1,cnpg.io/instanceRole=primary" -o name)
  k exec "$pod" -c postgres -- psql -U postgres -d chatbot -v ON_ERROR_STOP=1 -Atc "$2"
}

# A completed base backup to restore from (the ScheduledBackup takes one when it's created).
for _ in $(seq 60); do
  completed=$(k get backups.postgresql.cnpg.io -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' | grep -c '^completed$' || true)
  [ "$completed" -gt 0 ] && break
  sleep 5
done
[ "$completed" -gt 0 ] || { echo "FAIL: no completed base backup" >&2; exit 1; }

run=$(date +%s)
sql postgres "SET client_min_messages = warning;
  CREATE TABLE IF NOT EXISTS restore_test (run bigint, mark text, at timestamptz DEFAULT now())" >/dev/null
sql postgres "INSERT INTO restore_test (run, mark) VALUES ($run, 'before')" >/dev/null
sleep 2
target=$(sql postgres "SELECT now()")
sleep 2
sql postgres "INSERT INTO restore_test (run, mark) VALUES ($run, 'after')" >/dev/null
wal=$(sql postgres "SELECT pg_walfile_name(pg_current_wal_lsn())")
sql postgres "SELECT pg_switch_wal()" >/dev/null
echo "markers written; restore target $target; waiting for $wal to be archived"
for _ in $(seq 60); do
  archived=$(sql postgres "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")
  [[ "$archived" > "$wal" || "$archived" == "$wal" ]] && break
  sleep 5
done
[[ "$archived" > "$wal" || "$archived" == "$wal" ]] || { echo "FAIL: $wal never archived" >&2; exit 1; }

./deploy/restore.sh "$target"
marks=$(sql postgres-restore "SELECT string_agg(mark, ',' ORDER BY at) FROM restore_test WHERE run = $run")
k delete cluster.postgresql.cnpg.io postgres-restore --wait >/dev/null
sql postgres "DELETE FROM restore_test WHERE run = $run" >/dev/null
[ "$marks" = "before" ] || { echo "FAIL: the copy has markers '$marks', want 'before'" >&2; exit 1; }
echo "ok: restored to $target: the marker before it, not the one after"
