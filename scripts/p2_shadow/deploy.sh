#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-/Users/caleb/Downloads/local.yaml}"
NAMESPACE="${P2_NAMESPACE:-trading}"
CONTEXT="${P2_CONTEXT:-local}"

cd "$ROOT"
python3 -m scripts.p2_shadow.preflight
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing deployment from a dirty worktree; commit the reviewed P2.1 source first." >&2
  exit 2
fi
SOURCE_COMMIT="$(git rev-parse HEAD)"
DEPS_SHA="$(cat requirements-postgres.txt requirements-messaging.txt | shasum -a 256 | awk '{print $1}')"
PACK_DIR="${TMPDIR:-/tmp}/p2-shadow-build-${SOURCE_COMMIT}"
mkdir -p "$PACK_DIR"
SOURCE_ARCHIVE="$PACK_DIR/source.tar.gz"
# Prevent macOS tar from serializing resource forks as AppleDouble `._*`
# entries. Those sidecars can match the migration `*.sql` glob in the pod.
COPYFILE_DISABLE=1 tar -czf "$SOURCE_ARCHIVE" --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' --exclude='.git' \
  -C "$ROOT" core migration postgres infrastructure scripts/p2_shadow
SOURCE_BUNDLE_SHA="$(shasum -a 256 "$SOURCE_ARCHIVE" | awk '{print $1}')"
RUNNER_GENERATION="$(kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get deployment mt5-native-bridge-main-runtime -o jsonpath='{.metadata.generation}')"
RUNNER_IMAGE="$(kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get pod mt5-native-bridge-main-runtime-7f7bd678ff-wlv7b -o jsonpath='{.status.containerStatuses[?(@.name=="orchestrator-shadow")].imageID}')"
RUNNER_CONFIG_SHA256="$(kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get configmap mt5-native-bridge-safe-platform-config -o jsonpath='{.data.platform\.json}' | shasum -a 256 | awk '{print $1}')"

kubectl --kubeconfig "$KUBECONFIG" create configmap p2-signal-shadow-source -n "$NAMESPACE" \
  --from-file=source.tar.gz="$SOURCE_ARCHIVE" \
  --from-file=requirements-postgres.txt="$ROOT/requirements-postgres.txt" \
  --from-file=requirements-messaging.txt="$ROOT/requirements-messaging.txt" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG" apply -f -

if ! kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get secret p2-signal-shadow-db >/dev/null 2>&1; then
  python3 - <<'PY' | kubectl --kubeconfig "$KUBECONFIG" apply -f -
import base64, json, secrets
keys={"POSTGRES_USER":"p2shadow_admin", "POSTGRES_PASSWORD":secrets.token_urlsafe(36),
      "P2_APP_USER":"p2shadow_ingest", "P2_APP_PASSWORD":secrets.token_urlsafe(36),
      "P2_NATS_USER":"p2shadow_worker", "P2_NATS_PASSWORD":secrets.token_urlsafe(36)}
obj={"apiVersion":"v1","kind":"Secret","metadata":{"name":"p2-signal-shadow-db","namespace":"trading","labels":{"app.kubernetes.io/part-of":"p2-signal-shadow"}},"type":"Opaque","data":{k:base64.b64encode(v.encode()).decode() for k,v in keys.items()}}
print(json.dumps(obj))
PY
fi

P2_SOURCE_SHA="$SOURCE_COMMIT" P2_DEPS_SHA="$DEPS_SHA" P2_SOURCE_COMMIT="$SOURCE_COMMIT" \
P2_SOURCE_BUNDLE_SHA="$SOURCE_BUNDLE_SHA" P2_RUNNER_IMAGE="$RUNNER_IMAGE" \
P2_RUNNER_GENERATION="$RUNNER_GENERATION" P2_RUNNER_CONFIG_SHA256="$RUNNER_CONFIG_SHA256" \
  python3 -m scripts.p2_shadow.render_manifest "$ROOT/deploy/p2_shadow/resources.yaml" > "$PACK_DIR/resources.yaml"
kubectl --kubeconfig "$KUBECONFIG" apply -f "$PACK_DIR/resources.yaml"
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" rollout status statefulset/p2-signal-shadow-postgres --timeout=10m
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" rollout status statefulset/p2-signal-shadow-nats --timeout=10m
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" scale deployment/p2-signal-shadow-worker --replicas=1
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" rollout status deployment/p2-signal-shadow-worker --timeout=20m
kubectl --kubeconfig "$KUBECONFIG" -n "$NAMESPACE" get deploy,sts,pod,svc,pvc -l app.kubernetes.io/part-of=p2-signal-shadow -o wide
echo "P2 shadow deployment requested; inspect with scripts/p2_shadow/report.sh."
