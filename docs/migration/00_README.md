# State and event migration: PostgreSQL + JetStream (design only)

Agent CLAUDE-A4-STATE-EVENT-MIGRATION. Baseline `64cb03345ff386cd560fa904deb457802a2edc85` in an isolated worktree (`claude-a4/state-event-migration`). Codex's worktree, all production code, runtime, brokers, MT5, Kubernetes, PostgreSQL and NATS were untouched. This directory is **documentation and read-only analysis tooling only**; it performs no cutover and defines no DB/NATS code.

> **PostgreSQL tells us what is true. JetStream tells services what happened. The trading platform decides what should happen. The MT5 bridge tells MetaTrader what to do and reports what MT5 says.**

## Documents

| # | Document | Task deliverable |
|---|---|---|
| 00 | this file (overview, findings, open decisions) | - |
| 01 | [`01_FILE_IPC_INVENTORY.md`](01_FILE_IPC_INVENTORY.md) *(generated)* | 1 file-IPC inventory |
| 02 | [`02_CURRENT_AND_TARGET_GRAPHS.md`](02_CURRENT_AND_TARGET_GRAPHS.md) | 2 current graph, 3 target graph |
| 03 | [`03_PRINCIPLES_ENVELOPE_AND_ORDERING.md`](03_PRINCIPLES_ENVELOPE_AND_ORDERING.md) | strategy selection, envelope, topology, ordering, effective-once |
| 04 | [`04_ARTIFACT_MIGRATION_MATRIX.md`](04_ARTIFACT_MIGRATION_MATRIX.md) *(generated)* | 4 artifact-by-artifact matrix |
| 05 | [`05_SIGNAL_MIGRATION.md`](05_SIGNAL_MIGRATION.md) | 5 signal migration |
| 06 | [`06_EXECUTION_INTENT_MIGRATION.md`](06_EXECUTION_INTENT_MIGRATION.md) | 6 execution-intent migration |
| 07 | [`07_BROKER_STATE_AND_TRADE_MANAGER_MIGRATION.md`](07_BROKER_STATE_AND_TRADE_MANAGER_MIGRATION.md) | 7 broker state, 8 Trade Manager |
| 08 | [`08_OWNERSHIP_AND_FENCING_CUTOVER.md`](08_OWNERSHIP_AND_FENCING_CUTOVER.md) | 9 ownership/fencing cutover |
| 09 | [`09_CHECKPOINT_AND_OBSERVATION_MIGRATION.md`](09_CHECKPOINT_AND_OBSERVATION_MIGRATION.md) | 10 checkpoints, 11 observation fan-out |
| 10 | [`10_OUTBOX_INBOX_FAILURE_ANALYSIS.md`](10_OUTBOX_INBOX_FAILURE_ANALYSIS.md) | 12 outbox/inbox failures |
| 11 | [`11_RECONCILIATION_PROTOCOL.md`](11_RECONCILIATION_PROTOCOL.md) | 13 reconciliation |
| 12 | [`12_CUTOVER_GATES.md`](12_CUTOVER_GATES.md) | 14 cutover gates |
| 13 | [`13_ROLLBACK_MATRIX.md`](13_ROLLBACK_MATRIX.md) | 15 rollback matrix |
| 14 | [`14_RUNTIME_MODES.md`](14_RUNTIME_MODES.md) | 16 startup/runtime modes |
| 15 | [`15_OBSERVABILITY_REQUIREMENTS.md`](15_OBSERVABILITY_REQUIREMENTS.md) | 17 observability |
| 16 | [`16_DEPLOYMENT_SEQUENCE.md`](16_DEPLOYMENT_SEQUENCE.md) | 18 deployment sequence |
| 17 | [`17_FILE_IPC_ZERO_DEFINITION.md`](17_FILE_IPC_ZERO_DEFINITION.md) | 19 `PRODUCTION_FILE_IPC=0` + audit |
| 18 | [`18_CODEX_V1_2_REVIEW_CHECKLIST.md`](18_CODEX_V1_2_REVIEW_CHECKLIST.md) | 20 Codex V1.2 review checklist |
| - | `data/`, `tools/` | matrix CSV, call-site CSV, audit baseline; `render_matrix.py`, `audit_file_ipc.py` |

## Headline findings

1. **53 artifact groups; 23 authoritative, 3 transport, 6 checkpoints** (rest projection/config/research/debug). Files that play two roles (record *and* queue) are the migration's real difficulty; each is flagged as architectural debt in `04`.
2. **Not one ladder.** Eight per-artifact strategies (`03` §2). Execution, ownership fence, arming and resume generation use a **single fenced cutover**; frozen strategy state is **tailed**, never dual-written; checkpoints are **removed by making reprocessing harmless**; placeholders are retired.
3. **The execution path has no durable pre-send record.** The decision row is appended *after* the broker call; the only pre-send journal is in the smoke path. The target `SENDING`/`UNCERTAIN` attempt state machine closes this without redesigning the frozen order-send timeout anomaly.
4. **"Generation" is an operator epoch, not a fencing token; singletons are PID files** (check-then-write). Fencing is a real gap that the database can close for platform writes; closing it for the *broker* call needs a bridge-side rule (OD-06).
5. **Trade Manager fan-out is at-most-once** (checkpoint before processing) and stores all published ids forever; `append_unique` is scan-then-append.
6. **Portability and coupling holes**: cwd-relative defaults (`fanout`, `stream_consumer`), the consumer's cross-repository read of the bridge's `request_lifecycle.jsonl`, and a Phase 7 reader of a retired full-state file.
7. **Latent defect**: `route_signal` appends to `tradeability_decisions`, a stream absent from `OrchestrationStore.paths` (verified by reading; present in the original repository's HEAD as well). Runtime behaviour unknown (OD-01).
8. **DR gap**: `scripts/backup_runtime.sh` backs up only Context runner files and docs, not the runtime authority artifacts.
9. **Baseline for the audit**: 254 file call sites in 72 production modules, **174 IPC candidates in 15 modules** (`data/io_audit_baseline.json`).

## Open decisions

| ID | Decision / question | Recommendation | Blocks |
|---|---|---|---|
| **OD-01** | Does the running system degrade on the undeclared `tradeability_decisions` stream (`route_signal` KeyError)? | verify read-only against the live runtime (look for `ROUTE_DEGRADED` in `delivery_status`) before migrating that stream | P2 for that stream |
| **OD-02** | Does `runtime/execution/real_execution_resume_generations/` exist and who writes it? (no source references it at baseline) | inspect the live dir read-only; import as history if present | P5 |
| **OD-03** | Is the Phase 7 observer live? It reads the retired full-state file | if live, repoint to compact state; else retire | P6 |
| **OD-04** | Is demo mode (`demo_*`) still used? | retire if not | P6 |
| **OD-05** | Frozen runners: keep the runner→adapter *file* hop as a documented exception, port persistence (fingerprint scope permits), or run the adapter in-process? | tailer first; decide before `LEGACY_WRITE_RETIRE_READY`; in-process adapter is the lowest-risk route to literal zero | P6 |
| **OD-06** | Bridge-side fencing: may the bridge reject a lower fence token / honour the idempotency key with a durable lookup? | yes; required for full duplicate-send closure | P5 |
| **OD-07** | Owner sign-off on gate thresholds (`12`) and soak durations | adopt as proposed, adjust with data from P1-P2 | each gate |
| **OD-08** | Sizing and retention for outbox/inbox/streams/history; JetStream dedupe window; account/quotas | size after P1 measurements; document in Codex handoff | P2 |
| **OD-09** | Dedicated PostgreSQL/JetStream instances (assumed) vs shared | dedicated | P1 |

## Tooling

```sh
python3 docs/migration/tools/render_matrix.py            # regenerates data/migration_matrix.csv, 01, 04
python3 docs/migration/tools/render_matrix.py --check    # validates categories/strategies/rollback classes
python3 docs/migration/tools/audit_file_ipc.py inventory # data/io_call_sites.csv, data/io_audit_baseline.json
python3 docs/migration/tools/audit_file_ipc.py audit     # exit 1 while IPC candidates exist
```

Both tools parse or write only under `docs/migration/`; they never import repository modules and touch no runtime, broker, MT5, PostgreSQL, NATS or Kubernetes.

## Limits of this analysis

* Derived from reading source at the baseline commit; **no live runtime directory, database or stream was inspected**. Items depending on live state are open decisions, not findings.
* Gate thresholds and retention numbers are proposals.
* `trade_manager/{state,storage,prospective,observation_storage}.py` were classified from their storage role; their internal call sites appear in `data/io_call_sites.csv` but were not traced end to end.
