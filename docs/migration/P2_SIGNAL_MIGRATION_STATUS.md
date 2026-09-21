# P2 signal migration status

P2 is implemented as a dormant S0 shadow path. The legacy append-only signal
output remains authoritative. No signal DB or JetStream authority transition
was performed.

## OD-01

The pre-fix regression reproduced:

```text
SKIPPED / MISSING_ACCOUNT_DATA / error "'tradeability_decisions'"
```

The minimal repair declares `tradeability_decisions` in
`OrchestrationStore.paths`. The route now persists the intended row. Genuine
account snapshot failures still produce `MISSING_ACCOUNT_DATA`. REAL execution
code was not changed; A5's finding that REAL does not consume orchestrator
`sizing_decisions` remains true.

## S0 seam

`LegacySignalTailer` adapts the existing append-only output to
`canonical_signal()` and `ingest_signal()`. The canonical ingest API accepts a
record and a PostgreSQL connection; a future `StrategyHost` can call the same
API without changing persistence or event semantics. Frozen strategy files are
not imported by this adapter and were not modified.

The ingest transaction writes candidate, evaluation, signal lifecycle, and
`strategy.candidate.detected.v1` / `signal.entry.created.v1` outbox rows in one
transaction. Duplicate source records reuse stable identities and produce one
logical record and one event per subject.

## Gate evidence

| Gate | Status | Evidence | Threshold / observation |
|---|---|---|---|
| `SHADOW_WRITE_READY` | PASS | `tests/test_signal_migration.py`; isolated PostgreSQL ingest | canonical records ingested; malformed records visible |
| `DUAL_WRITE_RECONCILED` | PASS | `migration.reconcile`; isolated synthetic comparison | semantic mismatch count 0 for the fixture |
| `DB_AUTHORITY_READY` | NOT_EVALUATED | authority handoff intentionally excluded | requires separate review |
| `JETSTREAM_SHADOW_READY` | PASS | isolated `TRADING_CORE` durable consumer run | real candidate event consumed; duplicate transport yielded one logical effect |
| `JETSTREAM_PRIMARY_READY` | NOT_EVALUATED | authority handoff intentionally excluded | not inferred from shadow tests |
| `LEGACY_READ_RETIRE_READY` | NOT_EVALUATED | no runtime access audit | legacy readers remain active |
| `LEGACY_WRITE_RETIRE_READY` | NOT_EVALUATED | S0 is still active | requires S1/StrategyHost and runtime proof |

## P2-relevant unresolved file sites

The static audit still reports 66 unresolved sites overall. P2-relevant
runtime-dependent sites are retained in
`docs/migration/data/unresolved_file_sites.json`, notably:

- `signal_orchestrator.py`: source fingerprint, runtime state/manifest/resume
  reads, heartbeat/stop writes; requires deployment/runtime tracing.
- `orchestration/adapters/context_structure_retrace.py`: frozen compact-state
  read; requires a runtime open/read audit.
- `orchestration/adapters/liquidity_displacement.py`: frozen state read;
  requires per-instance runtime tracing.

The remaining unresolved execution, Trade Manager, research, and debug sites
are outside P2 scope.

## Explicit non-goals

No customer publication, execution consumer connection, MT5 access, broker
write, ownership cutover, broker-state cutover, StrategyHost, or permanent
file-IPC exception was added. `PRODUCTION_FILE_IPC=0` remains false.
