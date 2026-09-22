# 07 - `postgres/migrations/011_p2_a1_signal_contract.sql` review

## 1. Content

Three additive changes: `strategy.entry_signals` (new table, see `02`), `platform.signal_ingest_quarantine` (new table), and two `ALTER TABLE` statements on existing tables (`platform.outbox_events` gains `lease_owner`/`leased_until`; `platform.reconciliation_findings`'s `status` CHECK constraint is widened to include the three new `ReconciliationStatus` values).

## 2. Forward-migration semantics and idempotency

Applied through `postgres/db.py::apply_migrations`, which is checksum-verified (`platform.schema_migrations`, `pg_advisory_xact_lock`-serialised, re-applying a changed file raises `RuntimeError: Migration checksum changed`) - unchanged mechanism, same guarantee as `010`. The SQL itself uses `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, and `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT` (`V30`) - the same idempotent style as `010`; a second raw execution of the file's statements (outside `apply_migrations`, e.g. manually) would not error, though the constraint drop/recreate is not a no-op (harmless churn, consistent with the existing repository convention).

## 3. Constraints and uniqueness

| Constraint | Purpose |
|---|---|
| `entry_signals.signal_id PRIMARY KEY` | one row per legacy signal id |
| `entry_signals.evaluation_id ... REFERENCES strategy.evaluations` | every entry signal has a real, persisted evaluation |
| `entry_signals.entry_signal_hash UNIQUE` | catches a true content duplicate arriving under an unexpected different `signal_id` |
| `entry_signals_canonical_result_uq` on `(strategy_id, strategy_version, COALESCE(parameter_set_ref,''), instrument, decision_time, candidate_id)` | the canonical-result uniqueness A7 required (`R22`, A1-11) - present and correct (`V27`) |
| `signal_ingest_quarantine` `UNIQUE (source_id, source_offset, raw_sha256)` | idempotent quarantine on redelivery/replay of the same malformed byte range |
| `reconciliation_findings_status_check` (widened) | keeps the DB in sync with the Python `ReconciliationStatus` enum, including the two currently-dead values (`EXPECTED_LAG`, `KNOWN_LEGACY_ANOMALY`, `05`) |

No `UNIQUE`/`CHECK` constraint was removed or narrowed; `010`'s `lifecycle_state` CHECK on `strategy.candidates`/`strategy.signals` is untouched.

## 4. Compatibility with migration `010` and existing P2 data

* `010` and `011` touch disjoint objects except the shared `platform.reconciliation_findings` CHECK (which `011` only widens). No conflict.
* Rows written by the **pre-A1** code (`8f49aef`, i.e. `strategy.signals`/`strategy.candidates`/`strategy.evaluations` with the old, path/line-dependent evaluation hash and no `entry_signals` counterpart) remain **fully inspectable** - nothing in `011` rewrites, renames, or deletes any existing row or column, and no historical identity (`signal_id`, `candidate_id`, old `evaluation_id`) is altered.
* **No silent rewriting of historical identities**: confirmed by inspection - `011` contains zero `UPDATE`/`DELETE` statements.

## 5. Migration risk: no backfill for pre-existing rows (identified, not present in the migration)

`011` does not populate `strategy.entry_signals` for rows already ingested under the pre-A1 schema, and **no backfill tool exists anywhere in the diff** (`V29`, confirmed by `git grep -i backfill` across `migration/` and `postgres/migrations/`). Two consequences, both worth recording before any K8s shadow deployment:

1. **Any signals already shadow-ingested by the pre-A1 code (`8f49aef`) at a real or test deployment have no `entry_signals` row** and are therefore invisible to P4.2's ManagedTrade creation and to `reconcile_legacy_signals()`'s database side, until/unless a deliberate backfill runs.
2. **If a backfill is attempted by simply resetting the tailer checkpoint and re-reading from offset 0**, it would **not** cleanly reconcile: `canonical_signal()` under the new code computes a **different** `evaluation.evaluation_hash` than the original ingestion did (the pre-A1 hash included `legacy_source_reference`/`legacy_source_hash`/`as_of` in the hashed provenance, which the new code excludes - `02`), so re-ingesting the same raw line inserts a **new** `strategy.evaluations` row under a new `evaluation_id`, while `strategy.signals.evaluation_id` (protected by `ON CONFLICT (signal_id) DO NOTHING`) keeps pointing at the **old** evaluation row. The new `entry_signals` row would reference the *new* evaluation, while `strategy.signals` for the same `signal_id` still references the *old* one - a real inconsistency between two evaluation references for one signal, not a rewrite of either, but a divergence that any backfill procedure must account for (e.g. by explicitly repointing `strategy.signals.evaluation_id`, or by treating pre-A1 rows as a distinct, documented cohort rather than backfilling them at all).

`MIGRATION_011_RISKS`: (a) no backfill tool exists for already-ingested pre-A1 rows; (b) a naive checkpoint-reset backfill would create a `strategy.signals.evaluation_id` / `strategy.entry_signals.evaluation_id` divergence for those rows, because the evaluation hash algorithm changed between the two code versions. Neither risk affects **new** signals ingested only under `011`+`3a39fd7`, which is the only path relevant to a fresh P2.1 K8s shadow deployment (no pre-A1 shadow data exists there per the preflight evidence in `09`).

## 6. Does the schema support A7's ManagedTrade prerequisites?

Yes, for the fields P2-A1 was responsible for (`02` section 4); the one gap (`reference_entry_semantics` not materialised) is derivable, not missing, and is P4.1/P4.2's responsibility to compute, not P2-A1's to store.

**`MIGRATION_011_PASS = true`**, subject to the two documented, non-blocking risks above being read before any live backfill is attempted (none is needed for a fresh deployment).
