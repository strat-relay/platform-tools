# Platform Signals and Control API

This Platform-owned, read-only API workload serves canonical EntrySignal routes and the migrated Control API surface. It does not perform trading execution or serve broker facts from PostgreSQL.

## Contract

- `GET /api/v1/signals` reads `strategy.entry_signals` and relational `strategy.entry_signal_mechanisms` from `trading-postgres`.
- `GET /api/v1/signals/{signal_id}` performs the same canonical read for one ID and returns HTTP 404 when absent.
- `limit` defaults to 100 and is bounded to 1–500; `offset` defaults to 0. Ordering is `decision_time DESC, signal_id ASC`.
- Every request validates applied schema `012` and starts a PostgreSQL read-only transaction. A missing database/schema produces HTTP 503; there is no file fallback.
- The envelope declares `source=canonical_postgres` and `schema_version=012`. Canonical columns are returned with exact Console aliases for symbol, signal timestamp, and geometry.
- Other `/api/v1/*` routes are dispatched to `PlatformControlApi`: system/safety use authority configuration and PostgreSQL, strategies use the mounted active platform config, events use `platform.outbox_events`, execution records use canonical execution tables, and routes without canonical authority return explicit unavailable/inactive semantics. No legacy filesystem fallback is present.
- The router centralizes credentialed CORS for the exact `https://console.stratrelay.app` origin; the API does not reflect arbitrary origins. `PLATFORM_API_CORS_ORIGINS` is the API's non-credentialed standalone allowlist.

## Image and workload

The image is built from `deploy/platform_api/Dockerfile` and deployed by immutable digest from the local registry.

The workload identity remains `platform-signals-api`, port `22350`; a second `platform-control-api` Service alias selects the same pod, so no additional pod is required. The pod receives the PostgreSQL DSN and authority modes plus a read-only mount of the active strategy ConfigMap. It runs non-root with a read-only root filesystem and restricted network egress, without legacy runtime-state mounts. The single-replica update strategy uses `maxSurge: 0` and `maxUnavailable: 1`; a rollout can briefly interrupt API availability.

The current namespace quota is full; consolidation and no-surge rollouts avoid adding a pod.
