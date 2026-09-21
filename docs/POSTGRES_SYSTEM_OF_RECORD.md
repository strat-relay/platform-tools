# PostgreSQL system-of-record foundation

This branch adds an inactive PostgreSQL representation for the Phase 6
`CONTEXT_STRUCTURE_RETRACE_V1` state. The existing runner, compact JSON sidecar,
execution consumer, bridge, broker, and Trade Manager remain unchanged.

## Deployment decision

PostgreSQL will run in Docker, and the eventual platform will be managed through
Docker Compose. The normal deployment model is documented in
`docs/DOCKER_DEPLOYMENT.md`; native-host PostgreSQL is not part of the plan.
The first Compose foundation is `compose.yaml`, with PostgreSQL active and
migration/integration-test services available as explicit operational commands.

Run with an explicitly supplied `TRADING_POSTGRES_DSN` (or standard `PG*`
variables), then apply migrations before importing. The importer is idempotent:
source hashes are recorded in `platform.migration_batches`, mutable state is
upserted by stable IDs, lifecycle rows are append-only, and research/context
observations are deduplicated by canonical content hash.

The database contract separates `research`, `strategy`, `orchestration`,
`execution`, `trade_management`, `audit`, `telemetry`, and `platform` namespaces.
Application and read-only credentials must be provisioned outside these
migrations; passwords are never stored in the repository.

The normalized identity model is `platform.strategy_versions`,
`platform.configuration_versions`, `platform.freeze_manifests`, and bounded
`strategy.runner_state`. Setups, opportunities, economic positions, and
lifecycle events are persisted row-by-row. Events with both setup and position
identities are preserved in the canonical lifecycle table and in both typed
projections. Unattached events are retained in telemetry/audit storage rather
than being discarded.

Imports require an explicit `--event-cutoff`. Preflight rejects event rows after
that cutoff, terminal-state conflicts, orphan links, duplicate identities, and
incompatible snapshot boundaries before opening the write transaction.

Mutable ownership is intentionally singular: setup owns retrace/invalidation,
target-consumed setup state, and reentry eligibility; entry opportunity owns
attempt identity and proposed entry/stop/target; economic position owns live
status, current stop, realized result, and position-level target/reentry state;
runner owns only process/cursor metadata. Lifecycle rows are historical evidence
and do not become a second mutable authority. Derived reports and MFE/MAE
summaries are not restart authority.

## Safety boundary

Import and validation are offline operations. They do not invoke MT5, `OrderSend`,
broker APIs, live consumers, or service restarts. No cutover is performed by
this branch. A future cutover requires an explicit runbook covering dual-write,
backfill parity, replay/continuation parity, read-only shadow period, rollback,
and operator approval for each execution boundary.

## Commands

```sh
docker compose up -d postgres
docker compose run --rm migrate
docker compose run --rm postgres-tests

# Offline/native Python tooling remains available for development diagnostics:
python3 -m postgres.migrate
python3 -m postgres.import_phase6 --mode working-set \
  --event-cutoff 2026-09-17T16:54:41Z \
  --captured-at 2026-09-17T16:54:41Z
python3 -m postgres.validate_phase6
```

`WORKING_SET` imports only continuation-capable entities from one explicit
compact-state boundary. The full state, lifecycle ledger, and Phase 2 archives
remain read-only references and are excluded. `--mode historical` remains
available as a separate audit importer and is not the migration path.

The Compose migration command only applies versioned migrations. It does not
import current live Phase 6 state. The Phase 6 importer remains a separate,
explicit operation and is not part of normal container startup.

Both Python commands require `psycopg` and an explicit database connection
environment. See `docs/DOCKER_DEPLOYMENT.md` for backup/restore and networking.
