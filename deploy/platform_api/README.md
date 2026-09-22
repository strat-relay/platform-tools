# Platform Signals API

This workload is a narrow Platform-owned read boundary for canonical EntrySignal data. It does not replace the bridge-owned Control API and does not expose broker endpoints.

## Contract

- `GET /api/v1/signals` reads `strategy.entry_signals` and relational `strategy.entry_signal_mechanisms` from `trading-postgres`.
- `GET /api/v1/signals/{signal_id}` performs the same canonical read for one ID and returns HTTP 404 when absent.
- `limit` defaults to 100 and is bounded to 1–500; `offset` defaults to 0. Ordering is `decision_time DESC, signal_id ASC`.
- Every request validates applied schema `012` and starts a PostgreSQL read-only transaction. A missing database/schema produces HTTP 503; there is no file fallback.
- The envelope declares `source=canonical_postgres` and `schema_version=012`. Canonical columns are returned with exact Console aliases for symbol, signal timestamp, and geometry.

## Image and workload

The image is built from `deploy/platform_api/Dockerfile`, pushed from the host as `host.docker.internal:5001/trading-platform-signals-api:20260922-canonical-signals-v2`, and referenced in Kubernetes by its immutable digest via `localhost:5001`.

The workload/service identity is `platform-signals-api`, port `22350`; it is separate from `control-api` port `22349`. The pod only receives the PostgreSQL DSN, runs non-root with a read-only root filesystem and restricted network egress, and does not mount runtime state files.

The existing Console hostname is still routed wholesale to the legacy Control API through a token-managed Cloudflare Tunnel. This workload must not be substituted as the host's origin. To route only signal paths, configure an explicit edge path rule for `/api/v1/signals` and `/api/v1/signals/*` to `platform-signals-api:22350`, preserving the current fallback origin for every other route. No such rule is installed by these manifests.
