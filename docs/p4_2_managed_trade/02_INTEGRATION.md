# P4.2 integration onto the current StratRelay production lineage

Branch: `integration/p4-2-managed-trade` · Worktree: `trading-platform-p4-2-integration`

- P4 source: `architecture/p4-2-managed-trade` @ `89a4d3382a18c789710c803f7056eef9608e59cc`
- Target: `main` @ `ede48f401f64a370ebb7350cf0a7cd4db03bca05` (Codex's Platform Signal API work)

## 1. Bringing P4.2 onto current lineage

`git diff --stat d3edcbb..ede48f4` (P4.2's base -> the integration target) showed **zero file
overlap** with P4.2's own changes: Codex's Platform Signal API work
(`platform_api/`, `deploy/platform_api/`, `docs/control_api_authority_audit.md`,
`tests/test_platform_signal_api.py`) is entirely new files, touching neither
`infrastructure/messaging/contracts.py` nor any `postgres/migrations/*.sql` nor
`postgres/foundation.py`. `git cherry-pick 89a4d33` onto a fresh branch at `ede48f4` applied
**with no conflicts** - nothing to resolve, both sets of subjects/streams and both packages
coexist untouched. Migration numbering was inspected before assuming this: Codex's work added no
migration file, so `013` (P4.2's number) remained free, and `DATABASE_SCHEMA_VERSION` needed no
reconciliation beyond P4.2's own bump.

One soft interaction, checked and confirmed harmless: `platform_api/signals.py` has its own
`SCHEMA_VERSION = "012"` constant, checked against `platform.schema_migrations` as `SELECT
version FROM platform.schema_migrations WHERE version = %s`. This is a **floor check** ("has
migration 012 been applied"), not an exact-version gate - `apply_migrations()` inserts one row
per migration file, keyed by its own filename prefix, so the "012" row remains present after 013
and 014 are also applied. P4.2's migrations do not break this check, and no coordination with
Codex's work was required.

## 2. Real PostgreSQL proof (this integration step's own work)

No PostgreSQL was reachable in earlier P4.2 sessions' sandboxes; this integration environment
has Docker available. Following the repository's own existing convention
(`compose.yaml`'s `postgres`/`migrate`/`postgres-tests` services, `Dockerfile.postgres-tools`,
`test_postgres_integration.py`'s `@unittest.skipUnless(_database_available(), ...)` pattern), an
**isolated, ephemeral** `postgres:16-alpine` container (no named volume, no shared network with
anything else, destroyed after the run) was used - never production PostgreSQL, never the
long-lived `compose.yaml` `postgres_data` volume.

**Two genuine bugs were found and fixed by this step** - exactly what the real-PostgreSQL proof
is for; neither was visible to the in-process `FakeConnection` substrate used by
`tests/test_trade_management_*.py`:

1. **Invalid SQL**: `legacy_stream_binding`'s uniqueness rule was written as a table-level
   `UNIQUE (strategy_id, COALESCE(strategy_instance_id, ''), COALESCE(instrument, ''), valid_from)`
   constraint. PostgreSQL's table-level `UNIQUE(...)` constraint syntax does not accept
   expressions - only column names. Fixed by moving it to a `CREATE UNIQUE INDEX
   legacy_stream_binding_uq ON ... (...)` statement, matching the exact pattern
   `011_p2_a1_signal_contract.sql` already uses for `entry_signals_canonical_result_uq`. The
   fake substrate never parses SQL, so this was invisible until migration `013` was actually
   applied to real PostgreSQL, where it failed immediately with `SyntaxError: syntax error at or
   near "("`.
2. **Ambiguous parameterised `unnest`**: `SELECT to_regclass(x) FROM unnest(%s) AS x` with a
   Python list bound as an untyped parameter is ambiguous to PostgreSQL's function resolver
   (`AmbiguousFunction`). Fixed with an explicit cast: `unnest(%s::text[])`.

A third issue was a bug in the **test**, not the product: an immutability-trigger test attempted
to "change" `tm_version_id` to the value it already held, which the trigger correctly treats as
a no-op (not a mutation) - fixed to update to a genuinely different value. A fourth, defensive
fix: added `setUp(self): self.conn.rollback()` to `TradeManagementRealPostgresTests` so a query
error in one test can never cascade into `InFailedSqlTransaction` errors on every subsequent
test sharing the same class-level connection (directly motivated by watching exactly that
cascade happen before the `unnest` fix was applied).

**Proven, against the isolated real instance** (`test_postgres_integration.py`,
`TradeManagementRealPostgresTests`, 18 tests, all passing - see section 5 for how to reproduce):

- Migrations `001` through `014` apply cleanly in one pass (`python -m postgres.migrate`).
- All ten new `trade_management.*` tables and the four named indexes exist
  (`to_regclass`/`pg_indexes`).
- `managed_trade`'s binding/geometry columns reject `UPDATE` (immutability trigger); mutable
  bookkeeping columns (`last_observation_seq`) remain writable.
- `trade_manager_version` rejects any manifest mutation and any `DELETE`; the one permitted
  transition (`FROZEN` -> `RETIRED`) succeeds.
- `legacy_stream_binding` rejects both `UPDATE` and `DELETE` (append-only).
- `trade_manager_decision` rejects both `UPDATE` and `DELETE` (immutable).
- `trade_observation` has **no** explicit immutability trigger (by design - A6/A7 rely on the
  content-addressed `observation_id` primary key with no `ON CONFLICT` clause on the real insert
  path); this step explicitly proved that protection holds at the database level too: a second
  `INSERT` of the same `observation_id` is rejected by the unique-constraint violation, not
  silently accepted as an overwrite.
- P2's schema is untouched: `strategy.entry_signals`, `strategy.entry_signal_mechanisms`,
  `strategy.evaluations`, `platform.outbox_events`, `platform.inbox_events` all exist with their
  expected shape.
- No forbidden column name (`account`, `ticket`, `lot`, `broker_position`, `execution_*`,
  `entitle`, `subscription`, `customer`, `published_*`) exists anywhere in the `trade_management`
  schema (`information_schema.columns`, real database, not just the migration file's text).
- **Migration reapplication is a safe no-op**: `apply_migrations()` called a second time against
  the already-migrated instance returns `[]` (nothing re-executed) - proven both via the
  in-test call and via a separate `python -m postgres.migrate` process invocation against the
  same instance.

## 3. TM-NONE-1 production seed (`postgres/migrations/014_tm_none_1_seed.sql`)

The smallest reviewed seed: registers exactly `TM-NONE-1`, nothing else. **`TM-LEGACY-0` is
deliberately not seeded** (mission section 3; not required by the first activation path).

The manifest/`manifest_hash`/`tm_version_id` embedded as SQL literals are computed **once**,
directly from `trade_management/versions.py:TM_NONE_1_MANIFEST` (the same object
`tests/test_trade_management_versions_and_ids.py::test_golden_vector_for_tm_none_1` already
pins), so the seed can never silently drift from what the application code itself would compute:

```
tm_version_id = TMV_ecaca5f080f9f79bb18cc936
manifest_hash = ecaca5f080f9f79bb18cc936fb3867420c416eb372f998f3ec7c175369b5ac0d
status        = FROZEN
```

Idempotent by construction: `INSERT ... ON CONFLICT (tm_version_id) DO NOTHING` on the version
row, and a `WHERE NOT EXISTS (...)` guard on its `tm_version_promotion` row. Proven idempotent
two ways against the isolated instance: (a) the normal migration-runner path (checksum-gated,
runs once), and (b) re-running the seed file's own statements **directly**, bypassing the
migration runner entirely, to prove the SQL itself - not just the runner's bookkeeping - is safe
to repeat (`test_tm_none_1_seed_is_idempotent_on_reapplication`).

**Semantics, as seeded and as implemented in `trade_management/tm_none.py`/`publication_gate.py`
(unchanged by this integration step):** every observation TM-NONE-1 evaluates yields
`action = HOLD` only; no stop movement, no partial profit, no exit, no broker action of any kind;
`trade_management.tm_version_promotion.publication_eligibility = SHADOW_ONLY` (permanent for
this version, since it never produces an actionable decision); every decision's
`publication_decision.outcome = WITHHELD` with `reason = NOT_ACTIONABLE_HOLD`. No claim about
strategy performance is made or implied by registering this version - it decides nothing about
management (A6 07 section 5).

## 4. Scope reductions carried forward unchanged

Everything listed in `00_README.md`'s "Scope reductions vs the full A6/A7 design" section still
applies at this integration step, exactly as documented there: no `CHALLENGER` evaluation
tracks, no `managed_trade_state_history`, no anti-join reconciliation report, no
`GAP_RECOVERY`/`PRE_ORCHESTRATOR_REFERENCE` eligibility taxonomy, no `ManagementSignal`
distribution, no broker execution, no execution intents, no OD-06 fencing, no NATS-first, no
Console/Platform-API ManagedTrade endpoints. Ship-first: this integration step adds only what
was required to prove the first safe shadow vertical slice (real-database proof + the minimal
production seed), nothing more.

## 5. Reproducing the real-PostgreSQL proof

```sh
docker network create p42-proof-net
docker run -d --name p42-proof-postgres --network p42-proof-net \
  -e POSTGRES_DB=trading_platform -e POSTGRES_USER=trading_app \
  -e POSTGRES_PASSWORD=<ephemeral-only> postgres:16-alpine
# wait for pg_isready, then:
docker build -f Dockerfile.postgres-tools -t p42-proof-tools:latest .
docker run --rm --network p42-proof-net \
  -e PGHOST=p42-proof-postgres -e PGPORT=5432 -e PGDATABASE=trading_platform \
  -e PGUSER=trading_app -e PGPASSWORD=<ephemeral-only> \
  p42-proof-tools:latest python -m postgres.migrate
docker run --rm --network p42-proof-net \
  -e PGHOST=p42-proof-postgres -e PGPORT=5432 -e PGDATABASE=trading_platform \
  -e PGUSER=trading_app -e PGPASSWORD=<ephemeral-only> \
  p42-proof-tools:latest python -m unittest -v test_postgres_integration
docker rm -f p42-proof-postgres; docker network rm p42-proof-net
```

This is the same substrate the repository's `compose.yaml` `postgres`/`postgres-tests` services
already use in spirit; this step used a throwaway network/container instead of the shared
`postgres_data` volume specifically to keep the proof isolated, per the mission's explicit
instruction never to test schema mutation against a persistent/production-like instance.

## 6. Regression summary

Two independent test surfaces, both clean:

- **`tests/` discovery suite** (fake-substrate, no real database): 227 tests (178 pre-existing +
  49 from the P4.2 cherry-pick) -> `failures=3, errors=5, skipped=1`, verified byte-identical to
  the 178 pre-existing tests' own baseline at the same signature via a detached worktree at the
  target commit. Zero new regressions.
- **`test_postgres_integration.py`** (repo root, real-database surface, not part of `tests/`
  discovery): 18 tests, all skipped in this sandbox (no local `psycopg`/reachable PostgreSQL) but
  all 18 independently proven passing against the isolated Docker instance above (section 2).
