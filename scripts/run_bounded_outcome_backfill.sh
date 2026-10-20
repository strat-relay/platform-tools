#!/usr/bin/env bash
# Run only the audited 2026-10-20 recovery; save results on the operator's Mac.
set -euo pipefail
source_ref=${1:?Pass the immutable application commit SHA}
[[ "$source_ref" =~ ^[0-9a-f]{40}$ ]] || { echo 'An immutable commit SHA is required' >&2; exit 1; }
report_dir="$HOME/Downloads/stratrelay-backfill-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$report_dir"
gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/strat-relay/platform-tools/contents/scripts/bounded_outcome_backfill.py?ref=$source_ref" \
  > "$report_dir/backfill.py"
gh api -H 'Accept: application/vnd.github.raw+json' \
  "repos/strat-relay/platform-tools/contents/docs/research/bounded_backfill_20261020/plan.json?ref=$source_ref" \
  > "$report_dir/plan.json"
python3 - "$report_dir" <<'PY'
import json, pathlib, subprocess, sys
directory = pathlib.Path(sys.argv[1])
code = (directory/'backfill.py').read_text()
plan = (directory/'plan.json').read_text()
expected = '2b179f39f5721d4f2835cb6587d5f5bf80f5af8c2d2e9aaf43db894dfab7301f'
if json.loads(plan)['plan_sha256'] != expected:
    raise SystemExit('Unexpected recovery plan')
args = ['bounded-backfill','--expected-plan-sha',expected,'--apply']
payload = ('import io, sys\nsys.argv = '+repr(args)+'\n'
           'sys.stdin = io.StringIO('+repr(plan)+')\n'
           'exec(compile('+repr(code)+", '<bounded-backfill>', 'exec'), {'__name__':'__main__'})\n")
command = ['kubectl','--kubeconfig',str(pathlib.Path.home()/'Downloads/local.yaml'),
           '--context','local','-n','trading','exec','-i',
           'deploy/unified-outcome-resolver','-c','resolver','--','env',
           'PYTHONPATH=/app','MARKET_DATA_BRIDGE_URL=http://10.10.10.100:22347/mcp',
           'python','-']
with (directory/'report.json').open('w') as report, (directory/'progress.log').open('w') as progress:
    result = subprocess.run(command,input=payload,text=True,stdout=report,stderr=progress)
print('Report:',directory/'report.json')
print('Progress:',directory/'progress.log')
if result.returncode:
    print('Backfill did not report success. Inspect both files before retrying.',file=sys.stderr)
raise SystemExit(result.returncode)
PY
