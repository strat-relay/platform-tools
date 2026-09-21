# 13 - Rollback matrix

Classes: **REVERSIBLE** (return to the previous state with no data loss and no operator data repair), **CONDITIONALLY_REVERSIBLE** (reversible while stated conditions hold), **ONE_WAY_WITH_MIGRATION** (going back requires an explicit export/migration; a plain restore is forbidden).

Rollback never restores an older file *generation* or *snapshot version* (`08` §5). Per-artifact class is in `04` (`data/migration_matrix.csv`, column `rollback_class`); this document defines the class by **domain and mode**.

## By mode transition

| Transition | Class | Rollback action | Condition / data at risk |
|---|---|---|---|
| `LEGACY_FILE → DB_SHADOW` | REVERSIBLE | stop shadow writers; drop or keep shadow schema | none - files remained authority |
| `DB_SHADOW → outbox/projector (reconciled)` | REVERSIBLE | disable projector; legacy writers unchanged | none |
| `DB_SHADOW → DB_PRIMARY` (signal, sizing, route) | CONDITIONALLY_REVERSIBLE | re-point readers to the projected files | only while the projector is on, reconciliation clean and file versions/ids continuous; **after** projector removal → one-way |
| `DB_SHADOW → DB_PRIMARY` (broker state) | CONDITIONALLY_REVERSIBLE | readers use projected `broker_state.json` | `snapshot_version` continuity; freshness during the switch back < 120 s or authorisations fail closed |
| `DB_SHADOW → DB_PRIMARY` (ownership rows) | ONE_WAY_WITH_MIGRATION once management acts on DB | export ledger back to JSONL (append-only, ordered by generation) | ownership written under DB authority exists only in DB |
| `DB_SHADOW → DB_PRIMARY` (execution, `T_cut`) | REVERSIBLE **before** the first DB-authorised send; ONE_WAY_WITH_MIGRATION after | export intents/attempts/generation to files with generation +1 (`08` §5) | in-flight `SENDING`/`UNCERTAIN` must be reconciled first |
| `JETSTREAM_SHADOW → JETSTREAM_PRIMARY` | CONDITIONALLY_REVERSIBLE | switch acting consumer back to the file/DB path | requires `DB_PRIMARY` true; the file transport must still exist (before `LEGACY_WRITE_RETIRE`) or the DB path acts as recovery source; events already acted on are not undone |
| `LEGACY_READ_DISABLED` | REVERSIBLE | re-enable reader | projector still on |
| `LEGACY_WRITE_DISABLED` | ONE_WAY_WITH_MIGRATION | re-enable projector and regenerate files from DB (full re-projection) | all state is in DB; old files are stale by design |
| `RETIRED` (files deleted) | ONE_WAY | regenerate from DB export | archived manifest retained |

## By domain, the "point of no return"

| Domain | Point of no return | What "rollback" means afterwards |
|---|---|---|
| Signal | projector removal (`LEGACY_WRITE_DISABLED`) | regenerate `signals.jsonl` from DB; frozen runner files were never touched |
| Sizing/route | same | same |
| Broker state | projector removal | regenerate `broker_state.json` from latest snapshot with **greater** `snapshot_version` |
| Ownership ledger | first management action recorded only in DB | export ledger to JSONL |
| Management (proposals/intents) | first DB-authorised management execution | export pending/terminal states; drain first |
| Execution | first DB-authorised broker send | quiesce → reconcile → export with generation +1; consumer guard refuses older file |
| Observation | disabling the file publisher | run file publisher from stream replay (bounded by retention) |
| Checkpoints | deleting checkpoint files | none needed (no correctness dependency) |

## Rules

1. Rollback is a **procedure with a drill**, not an intention: each domain rehearses it in a test environment before `DB_AUTHORITY_READY` (`12`).
2. Rollback always begins by **quiescing** (disarm/lease release for the resource) and a final reconciliation.
3. A rollback that discovers `UNCERTAIN` attempts does not proceed until they are resolved; an unresolvable one keeps the account halted (fail closed).
4. Rolling back to a file mode **never** means starting the legacy code against the pre-cutover files; it means exporting from PostgreSQL. Legacy start-up refuses stale generations (`08`).
5. Data-loss statement for each transition must be written in the change record (which rows/events exist only on one side).
6. Frozen strategy runners are never rolled forward or back; they are independent of every transition here.
