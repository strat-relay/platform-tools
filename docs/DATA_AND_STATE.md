# Data and state

## Active V1 files

| File | Writer | Reader | Mutable? | Delete? | Recovery role |
|---|---|---|---|---|---|
| `context_structure_retrace_forward_state.json` | V1 runner | runner/status/report | yes, atomic | **NO** | Primary cursor, setup and position recovery |
| `context_structure_retrace_forward.jsonl` | V1 runner | report/audit | append-only | **NO** | Causal event history |
| `context_structure_retrace_forward_summary.md` | clean shutdown | human | replaceable | no, but preserve backup | Last summary |
| `context_structure_retrace_forward.pid` | runner lock | start/stop/operator | volatile | only after process-gone check | Duplicate-run protection |
| `context_structure_retrace_forward.heartbeat.json` | runner | health/watch | atomic | no while active | Liveness and last cursor |
| `context_structure_retrace_forward_manifest.json` | freeze | audit/report | immutable | **NO** | Frozen identity/configuration |

The prospective boundary is the manifest freeze timestamp. Pre-boundary records
are exposed development data; post-boundary records are prospective paper data.
Do not merge them in performance reports.

Historical research artifacts are the root `*_results.json`, `*_trades.csv`,
`*_summary.md`, phase ledgers and `research/` packages. They are evidence, not
runtime state. Phase 4/5 pre-correction target metrics are superseded by the
geometry bug and must not be presented as current validation.

## Recovery rules

State and ledger are the minimum prospective evidence pair. Back up both with
the manifest, heartbeat, PID metadata and runner source before manual repair.
Never reset cursors or rewrite JSONL to make a report look clean.
