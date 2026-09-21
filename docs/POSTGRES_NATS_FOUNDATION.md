# PostgreSQL / NATS foundation

V1.2 establishes dedicated StratRelay infrastructure definitions only. The
existing shared InfluenceLnk PostgreSQL and NATS resources are not used or
modified. Production remains on its current file-backed authority until a
later controlled migration.

## Canonical roles

- PostgreSQL is the authoritative store for trading-platform state,
  immutable evaluations/traces, ownership generations, runtime instances,
  execution idempotency, and outbox/inbox records.
- NATS JetStream is the at-least-once operational event transport.
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
Subjects are explicit and versioned. The initial topology has two file-backed
streams:

- `TRADING_CORE`: strategy and signal subjects.
- `EXECUTION`: execution, broker-state, and ownership subjects.

The adapter does not expose NATS client objects to domain code. NATS transport
is at-least-once; transactional inbox plus idempotent transitions provide the
effectively-once domain effect needed by later migration phases.

Local Compose adds a dedicated NATS 2.10 JetStream service with a persistent
`/data` volume and loopback-only ports. No deployment is applied by this
change.
