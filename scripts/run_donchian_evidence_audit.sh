#!/usr/bin/env bash
# Only local report files are written. Compressed transport avoids large exec output.
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
  -- env PYTHONPATH=/app DONCHIAN_AUDIT_GZIP=1 python - < "$TASK_DIR/audit_donchian_replay.py" \
  > "$REPORT_DIR/report.json.gz.part" 2> "$REPORT_DIR/stderr.txt"; then
  python3 - "$REPORT_DIR" <<'PY'
import gzip, json, pathlib, sys
folder = pathlib.Path(sys.argv[1])
# A truncated transfer cannot become a successful report. gzip validates its CRC.
with gzip.open(folder/'report.json.gz.part', 'rt') as stream:
    report = json.load(stream)
if report.get('status') == 'BLOCKED':
    print(json.dumps(report))
    raise SystemExit(1)
if not isinstance(report.get('signals'), list) or 'summary' not in report:
    raise SystemExit('Incomplete audit report; partial transfer retained')
temporary = folder/'report.json.part'
temporary.write_text(json.dumps(report, indent=2))
temporary.replace(folder/'report.json')
(folder/'report.json.gz.part').replace(folder/'report.json.gz')
print('Validated report:', folder/'report.json')
print('Summary:', json.dumps(report['summary']))
PY
else
  printf 'Audit transfer failed. Partial output and stderr retained in %s\n' "$REPORT_DIR"
  exit 1
fi
