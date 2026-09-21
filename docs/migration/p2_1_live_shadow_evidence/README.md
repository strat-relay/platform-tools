# P2.1 live-shadow evidence package

Status: **STOPPED BEFORE ATTACHMENT**.

The read-only preflight passed for the authoritative K8s signal runtime, but
the safe P2 shadow target was not available. The `trading` namespace contains
the existing `context-paper` and `orchestrator-shadow` containers only; it has
no dedicated PostgreSQL or NATS/JetStream service and no deployed P2 shadow
component. The only database/NATS instances available were the prior isolated
local test containers, which are not a live K8s shadow target.

No deployment, restart, configuration change, file write, signal injection,
or authority change was performed. The legacy K8s signal path remained the
sole authority throughout.

## Verified preflight

See `preflight.json` and `legacy_signal_snapshot.json` for raw, machine-readable
facts. At collection time:

- K8s context `local`, namespace `trading`, was reachable.
- `context-paper` and `orchestrator-shadow` were both running in the existing
  `mt5-native-bridge-main-runtime` pod.
- The legacy signal file was copied read-only for hashing/counting only.
- 22347 returned a read-only health response with `pending=0`.
- 22348 returned connection refused.
- The orchestrator manifest remained `SHADOW` with
  `live_execution_enabled=false`.
- No execution consumer, Trade Manager, or Phase 7 deployment was present.

## Why attachment was not attempted

Attaching `LegacySignalTailer` requires an explicitly isolated PostgreSQL
shadow target and a dedicated NATS/JetStream shadow target with credentials and
network reachability. None is deployed in the live namespace. Reusing the
local Colima test services would not prove live K8s shadow behavior and could
mix environments, so the task stopped as required.

## Minimum required deployment

Deploy only the following later, after review:

1. A dedicated P2 shadow PostgreSQL role/database with migrations through 010.
2. A dedicated NATS 2.10 JetStream account/stream `TRADING_CORE`, isolated from
   shared infrastructure.
3. A disabled-by-default P2 shadow worker mounting the authoritative signal
   output read-only, using `LegacySignalTailer`, `migration.signal.ingest_signal`,
   and the P2 shadow consumer. It must not expose execution subjects or call
   the execution consumer.
4. Explicit removal/rollback manifests and credentials supplied through
   Kubernetes secrets, never committed here.

P2.1 live observation, reconciliation, restart evidence, and gate approval
remain pending until that deployment exists.

Synthetic partial-line, malformed-line, rotation, canonicalization, duplicate,
and reconciliation tests remain covered by the offline P2 test suite; they are
not represented as live evidence.
