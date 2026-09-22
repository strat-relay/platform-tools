#!/usr/bin/env bash
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/Users/caleb/Downloads/local.yaml}"
NAMESPACE="${P2_NAMESPACE:-trading}"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get deploy,sts,pod,svc,pvc,networkpolicy -l app.kubernetes.io/part-of=p2-signal-shadow -o wide
POD="$(kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get pod -l app=p2-signal-shadow-worker -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "P2 shadow worker is not running; durable status remains on the evidence PVC." >&2
  exit 1
fi
echo "WORKER_STATUS"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" exec "$POD" -c worker -- cat /data/status.json
echo "BOOTSTRAP_PROOF"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" exec "$POD" -c worker -- cat /data/bootstrap-proof.json
echo "EVIDENCE_WINDOW_MARKER"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" exec "$POD" -c worker -- cat /data/evidence-window.json
