#!/usr/bin/env bash
# Lets the cluster reach Langfuse, which runs in Docker Compose (Phase 9): attaches its web
# container to kind's Docker network and points the `langfuse-web` Service (deploy/local, a
# Service with a manual EndpointSlice) at its address there. Pods can't resolve Docker's
# container names themselves. Run again whenever the container is recreated (Tilt does).
set -euo pipefail
CONTAINER=$(docker ps --filter "label=com.docker.compose.service=langfuse-web" --format '{{.Names}}' | head -1)
[ -n "$CONTAINER" ] || { echo "langfuse-web isn't running (make langfuse)"; exit 1; }
docker network connect kind "$CONTAINER" 2>/dev/null || true
IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$CONTAINER")
kubectl --context kind-chatbot apply -f - <<YAML
apiVersion: v1
kind: Service
metadata: {name: langfuse-web, namespace: chatbot}
spec:
  ports: [{name: http, port: 3000, targetPort: 3000}]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: langfuse-web
  namespace: chatbot
  labels: {kubernetes.io/service-name: langfuse-web}
addressType: IPv4
ports: [{name: http, port: 3000}]
endpoints: [{addresses: ["$IP"]}]
YAML
echo "langfuse-web -> $IP"
