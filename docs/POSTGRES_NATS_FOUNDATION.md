# PostgreSQL / NATS foundation

V1.2 established the persistence and messaging contracts. The dedicated
canonical platform PostgreSQL and NATS JetStream services are provisioned by
`deploy/canonical_platform/`; they are separate from the migration-only
`p2-signal-shadow-*` resources. Provisioning the services does not change the
live signal authority, which remains file-backed/SHADOW until a separately
authorized T0 cutover.

## Canonical roles

- PostgreSQL is the authoritative store for trading-platform state,
  immutable evaluations/traces, ownership generations, runtime instances,
  execution idempotency, and outbox/inbox records.
- NATS JetStream is the durable operational event backbone and at-least-once
  transport; it is not merely a PostgreSQL implementation detail.
- JSONL remains a transitional production transport during migration.
- Parquet remains the preferred store for large research candle/tick/feature
  data; manifests/configuration remain JSON.

## Migration matrix

| Current artifact | Producer | Consumer | Current classification | Target PostgreSQL | Target subject | Phase |
|---|---|---|---|---|---|---|
| `runtime/orchestration/signals.jsonl` | orchestrator | execution/ops readers | AUTHORITATIVE_STATE (legacy) | `strategy.signals` | `signal.entry.created.v1` | dual-read design |
| `runtime/orchestration/events.jsonl` | orchestrator | ops/research | EVENT_TRANSPORT | outbox + audit projection | versioned event subject | dual-write later |
| `execution/execution_intents.jsonl` | execution workflow | execution consumer | AUTHORITATIVE_STATE (legacy) | `execution.intents` | `execution.intent.created.v1` | not started |
| `execution/events.jsonl` | execution consumer | ops/research | EVENT_TRANSPORT | `execution.results` + outbox | `execution.result.recorded.v1` | not started |
| `trade_manager/management_intents.jsonl` | Trade Manager | execution consumer | AUTHORITATIVE_STATE (legacy) | future execution intent projection | future execution subject | not started |
| `trade_manager/management_proposals.jsonl` | Trade Manager | Trade Manager/execution | AUTHORITATIVE_STATE | future TM tables | none yet | not started |
| `trade_manager/observation_stream/events.jsonl` | observation fanout | TM/research | EVENT_TRANSPORT | future observation tables | future TM subject | not started |
| `trade_manager/observation_stream/checkpoint.*.json` | fanout consumer | fanout restart | REBUILDABLE_CACHE | consumer checkpoint/inbox | JetStream durable consumer | not started |
| `trade_manager/ownership_registry.jsonl` | ownership registry | execution/TM | AUTHORITATIVE_STATE | `platform.ownership_leases` | `ownership.changed.v1` | foundation only |
| `trade_manager/broker_state.json` | broker-state stream | TM/execution/ops | REBUILDABLE_CACHE + latest projection | `execution.broker_state_current` and history | `broker.state.updated.v1` | foundation only |
| strategy state JSON/JSONL | legacy runners | legacy reports | AUTHORITATIVE_STATE (legacy) | strategy tables by migration | candidate/signal subjects | not started |
| research JSONL/CSV/Parquet | research jobs | research reports | RESEARCH_ARTIFACT | not generally migrated | none | preserve |
| manifests/config JSON | runners/deploy | startup | CONFIGURATION | version tables where authoritative | none | preserve/introduce deliberately |
| summaries/diagnostic JSONL | runners/ops | humans/tools | DEBUG_ARTIFACT | none | none | preserve or expire |

No file artifact is silently retired in V1.2. `PRODUCTION_FILE_IPC=0` is not
claimed.

## Database foundation

Migration `008_stratrelay_foundation.sql` adds canonical evaluation and trace
tables, candidates/signals, runtime instances, durable ownership leases with
monotonic generations, transactional outbox/inbox, execution idempotency, and
current/history broker-state tables. State plus outbox is committed by the
same caller transaction. Inbox claim is the duplicate-delivery boundary.

The ownership function rejects a stale generation in the database. A process
restart cannot reset a lease generation.

## JetStream foundation

`infrastructure/messaging/` owns the event envelope and JetStream adapter.
Subjects are explicit and versioned. The code contract defines two file-backed
streams:

- `TRADING_CORE`: strategy and signal subjects.
- `EXECUTION`: execution, broker-state, and ownership subjects. This stream is
  not provisioned by the current canonical infrastructure task; no V2
  execution stream is being introduced or activated here.

The current infrastructure provisions only `TRADING_CORE`, with the exact
strategy and signal subjects in `infrastructure/messaging/contracts.py`.

For V1/P2 canonical EntrySignal creation, PostgreSQL is the authoritative
domain transaction and its outbox publishes to JetStream:

```text
Orchestrator → PostgreSQL transaction + outbox → JetStream
```

For latency-sensitive broker execution workflows, the V2 direction is
JetStream-first:

```text
JetStream → execution/risk consumer → authenticated broker-held fence
validation → MT5 bridge → broker → ExecutionResult via JetStream
→ PostgreSQL projection/audit
```

This V2 direction is architectural guidance only; it is not implemented by
this infrastructure provisioning. JetStream durability and idempotency do not
replace broker-held fencing. The broker remains authoritative for actual
broker positions and orders, and P5 OD-06 remains a required blocker before
broker-writing execution can be enabled. The adapter does not expose NATS
client objects to domain code; transport remains at-least-once, with inbox and
idempotent transitions supplying effectively-once domain effects.

Local Compose adds a dedicated NATS 2.10 JetStream service with a persistent
`/data` volume and loopback-only ports.

## Canonical Kubernetes runtime status

The canonical namespace deployment is described by
`deploy/canonical_platform/runtime.yaml`:

- `trading-postgres` is a ClusterIP-only PostgreSQL 16.4 StatefulSet using a
  5Gi `local-path` PVC, explicit Secret-backed administrator/bootstrap and
  non-superuser application roles, and database `trading_platform`.
- `trading-nats` is a ClusterIP-only NATS 2.10 StatefulSet with JetStream file
  storage on a 2Gi `local-path` PVC and Secret-backed client authentication.
- `TRADING_CORE` is the only provisioned stream. Its subjects are the exact
  strategy/signal subject set in `infrastructure/messaging/contracts.py`.
  The broader `EXECUTION` code contract is not provisioned by this deployment.
- Migrations 001–012 are applied to the initially empty canonical database.
  There are no imported runtime rows. Migration 003 contributes two static
  `audit.execution_safety_invariants` definition rows.
- `signal-outbox-relay` is prepared at zero replicas with a runtime-image
  placeholder. The DB_PRIMARY flags are stored in a separate prepared
  ConfigMap, not attached to the live SHADOW orchestrator. No T0 cutoff has
  been established.

The local-path claims survive pod restarts but are tied to the single cluster
node. They do not provide node-loss redundancy; off-node backup/restore remains
necessary before relying on them as a production disaster-recovery boundary.
