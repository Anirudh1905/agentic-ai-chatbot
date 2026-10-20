#!/usr/bin/env bash
# make cluster: the kind cluster and the third-party services the app runs on (Phase 9).
# Safe to run again: it creates what's missing and upgrades the charts in place.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=chatbot
CONTEXT=kind-$CLUSTER

# kind's node runs kube-proxy and every pod's file watchers on the Docker VM's kernel, whose
# default inotify limits are too low: kube-proxy then fails with "too many open files" and no
# pod reaches the API server. The limits reset when the VM restarts, so after one, run this again.
docker run --rm --privileged alpine:3 \
  sysctl -qw fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288

if ! kind get clusters | grep -qx "$CLUSTER"; then
  # kind makes the new cluster your current context; put back the one you had.
  previous=$(kubectl config current-context 2>/dev/null || true)
  kind create cluster --config deploy/kind.yaml
  if [ -n "$previous" ]; then kubectl config use-context "$previous" >/dev/null; fi
fi

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add jetstack https://charts.jetstack.io >/dev/null
helm repo update cnpg prometheus-community jetstack >/dev/null

helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version 1.9.2 \
  --kube-context "$CONTEXT" --namespace envoy-gateway-system --create-namespace \
  --values deploy/helm/envoy-gateway.yaml --wait
helm upgrade --install cnpg cnpg/cloudnative-pg --version 0.29.1 \
  --kube-context "$CONTEXT" --namespace cnpg-system --create-namespace \
  --values deploy/helm/cloudnative-pg.yaml --wait
# Postgres backups (Phase 9): CNPG's Barman Cloud plugin, beside the operator; its TLS
# certificates come from cert-manager.
helm upgrade --install cert-manager jetstack/cert-manager --version v1.21.2 \
  --kube-context "$CONTEXT" --namespace cert-manager --create-namespace \
  --set crds.enabled=true --wait
helm upgrade --install barman-cloud cnpg/plugin-barman-cloud --version 0.8.1 \
  --kube-context "$CONTEXT" --namespace cnpg-system --wait
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version 92.10.1 \
  --kube-context "$CONTEXT" --namespace monitoring --create-namespace \
  --values deploy/helm/kube-prometheus-stack.yaml --wait --timeout 10m

echo "cluster $CLUSTER ready (context $CONTEXT)"
