#!/usr/bin/env bash
# Run this from a checkout containing the audit. Only local report files are written.
set -euo pipefail
TASK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPORT_DIR="$HOME/Downloads/donchian-replay-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$REPORT_DIR"
KUBE=(kubectl --kubeconfig "$HOME/Downloads/local.yaml" --context local -n trading)
"${KUBE[@]}" get deployments \
  -o 'custom-columns=NAME:.metadata.name,IMAGES:.spec.template.spec.containers[*].image' \
  > "$REPORT_DIR/workload-images.txt" 2> "$REPORT_DIR/workload-images.stderr.txt"
printf 'Report folder: %s\n' "$REPORT_DIR"
if "${KUBE[@]}" exec -i deploy/unified-outcome-resolver -c resolver \
  -- env PYTHONPATH=/app python - < "$TASK_DIR/audit_donchian_replay.py" \
  > "$REPORT_DIR/report.json" 2> "$REPORT_DIR/stderr.txt"; then
  printf 'Read-only audit completed: %s/report.json\n' "$REPORT_DIR"
else
  printf 'Audit failed. Share report.json and stderr.txt from %s\n' "$REPORT_DIR"
  exit 1
fi
