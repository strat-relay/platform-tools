# P0/P1 substrate status

This document records the dormant migration substrate added after the V1.2.1
handoff. It does not authorize a domain cutover. Legacy files remain the
production authority and `PRODUCTION_FILE_IPC=0` remains false.

## Provenance

- A4 migration map and tools: `trading-platform-claude-a4`, commit
  `89ba8b2e051453c0ed9e24c8c923b6b9dcd0456e`.
- A3 Strategy Studio source: `mt5-native-bridge-claude-a3`, commit
  `26d4a404e5ce06392650e53c28a4833a8833b4cc`.

Both are imported verbatim into the authoritative platform history.

## Implemented substrate

- `docs/migration/tools/audit_file_ipc.py` is deterministic and emits the
  machine-readable unresolved-site report. Its explicit allowlist preserves
  configuration, research/offline, and durability-only file operations.
- `migration.modes` models state authority, event transport, and legacy
  projection as independent axes and refuses illegal or unavailable modes.
- `migration.state` models the finite migration state machine. The JSON store
  is test/dormant tooling only; runtime authority is represented by migration
  tables in PostgreSQL migration 009.
- `migration.projector` provides an idempotent, compatibility-only output.
- `migration.tailer` handles durable offsets, partial lines, malformed rows,
  source provenance, canonical record identity, and truncation/rotation.
- `migration.reconcile` emits concrete machine-readable findings and summary
  counts rather than count-only comparisons.
- `migration.gates` requires evidence references and rejects positive blocking
  measures without silently approving proposed thresholds.
- `migration.observability` emits a read-only status shape for later Ops API
  exposure.

## Findings retained for later phases

The A4 findings are not opportunistically fixed here: undeclared
`tradeability_decisions`, absent durable pre-send execution record, broker-side
fencing, checkpoint-before-processing fanout, scan-then-append ownership,
incomplete runtime backup, cross-repository lifecycle reads, and the Phase 7
retired full-state dependency. They remain mapped to their A4 future phases.

Open decisions OD-01 through OD-09 remain recorded in the imported A4 docs.
OD-09 is resolved as dedicated StratRelay PostgreSQL/JetStream. OD-06 remains
a P5 blocker; PostgreSQL stale-generation rejection is not broker-side fencing.

## V1.2.1 discovery failures

The baseline repository-wide discovery result was 301 tests: 296 passed, 3
failures, and 2 errors. The five cases were classified as pre-existing
fixture/provenance/environment issues: a missing research snapshot
`/tmp/fx_universe_snapshot_20260917.json`, a missing compact-state fixture
created by the provenance adapter test, and three phase-6 provenance
expectation failures. No production semantics were changed to mask them.
