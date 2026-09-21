# Backup and disaster recovery

The minimum V1 evidence set is:

```text
context_structure_retrace_forward.py
context_structure_retrace/
context_structure_retrace_forward_state.json
context_structure_retrace_forward.jsonl
context_structure_retrace_forward_manifest.json
context_structure_retrace_forward.heartbeat.json
context_structure_retrace_forward.pid
context_structure_retrace_forward_summary.md
docs/
artifacts/test-results/
```

Run the additive, read-only backup helper:

```sh
cd /Users/caleb/mt5-native-bridge
scripts/backup_runtime.sh
```

It creates a timestamped directory outside the repository and copies files;
it does not stop the runner, rewrite state, or include credentials. Verify the
backup contents and hashes. Keep at least one backup separate from the Mac.

Do not commit secrets, MT5 profiles, account credentials, `.env` files,
volatile PIDs/heartbeats, or unreviewed prospective data. Prospective ledgers
are evidence and must be backed up even if they are excluded from normal source
control.

## GitHub backup status

On 2026-09-16, the source checkpoint was initialized locally and pushed to
the private repository `blissmen/mt5-strategy-research` on branch `main`.
The checkpoint excludes active runtime state/ledgers, volatile files, secrets,
and the large evidence artifacts listed in
`docs/LARGE_EVIDENCE_ARTIFACTS.md`.

A successful GitHub push is not full disaster recovery. The excluded runtime
state/ledger and large evidence artifacts still require a separately verified
backup. Do not claim full recoverability until those copies exist and their
checksums have been verified.
