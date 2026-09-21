# CONTEXT_STRUCTURE_RETRACE_V1 outputs

Current root-level Phase 1 artifacts:

- `context_structure_retrace_sample_snapshot.json`
- `context_structure_retrace_sample_event.json`
- `context_structure_retrace_provenance_report.json`

Phase 2 artifacts, when generated, are named:

- `context_structure_retrace_phase2_ledger.jsonl`
- `context_structure_retrace_phase2_ledger.csv`
- `context_structure_retrace_phase2_snapshots.jsonl`
- `context_structure_retrace_phase2_results.json`
- `context_structure_retrace_phase2_summary.md`

The active prospective files are at the repository root because the runner is
still using its frozen paths: `context_structure_retrace_forward_state.json`,
`context_structure_retrace_forward.jsonl`, heartbeat, PID, manifest and
summary. Do not move them while PID 49978 is active. These are paper records,
never broker orders. See `docs/DATA_AND_STATE.md`.
