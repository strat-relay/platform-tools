#!/usr/bin/env bash
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/Users/caleb/Downloads/local.yaml}"
NAMESPACE="${P2_NAMESPACE:-trading}"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" scale deployment/p2-signal-shadow-worker --replicas=0
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" scale statefulset/p2-signal-shadow-postgres --replicas=0
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" scale statefulset/p2-signal-shadow-nats --replicas=0
echo "P2 shadow path stopped. PVCs and evidence are preserved; no legacy runtime object was changed."
