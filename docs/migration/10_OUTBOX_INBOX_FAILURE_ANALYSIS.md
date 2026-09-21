# 10 - Outbox / inbox failure analysis

JetStream is **at-least-once**. Nothing in this document claims exactly-once delivery; it shows how *effects* become once.

## 1. Model

**Outbox** (written in the domain transaction): `outbox(id = event_id, aggregate_type, aggregate_id, aggregate_version, ordering_key, subject, envelope jsonb, created_at, published_at NULL, publish_attempts, partition)`. Relay: leases a partition (`relay:outbox:<p>`), selects unpublished rows **in `ordering_key` order** (`FOR UPDATE SKIP LOCKED` per key group, never reordering within a key), publishes with `Nats-Msg-Id = id`, waits for the JetStream publish-ack, then sets `published_at`. Bounded retention: published rows are archived/dropped after N days; the table is a **relay buffer, not a queue consumers read**.

**Inbox** (written in the consumer's transaction): `inbox(consumer, event_id, handled_at, outcome)` `PRIMARY KEY (consumer, event_id)`. A handler: `BEGIN; INSERT inbox ON CONFLICT DO NOTHING (0 rows ⇒ duplicate ⇒ skip); apply effects (with fence/CAS); INSERT outbox…; COMMIT; ACK`.

## 2. Failure modes

| # | Failure | Consequence | Defence | Residual |
|---|---|---|---|---|
| 1 | crash **before** domain commit | nothing happened | tx atomicity | none |
| 2 | crash **after** commit, before publish | event unpublished | outbox row persists; relay publishes on restart | latency spike; alerted by `outbox_oldest_unpublished_age` |
| 3 | publish succeeded, `published_at` not set (crash) | republish | `Nats-Msg-Id` dedupes inside window; inbox beyond | duplicates delivered outside the window are absorbed by inbox |
| 4 | relay publishes out of order across instances | reordering within a key | lease per partition + per-key ordered selection | two relays during failover: `aggregate.version` check at consumer |
| 5 | JetStream unavailable | outbox grows | alert on age/size; producers keep committing (state stays true) | **execution**: intents expire by freshness (fail-safe); backlog cap alarms; never fall back to files |
| 6 | JetStream stream loss/corruption | events missing | rebuild = republish from outbox/DB (`published_at` reset for a range); state is unaffected | consumers must tolerate replays (they do: inbox) |
| 7 | dedupe window (2 min default) exceeded | duplicate accepted by JetStream | inbox | inbox retention ≥ max redelivery horizon |
| 8 | consumer crash before inbox commit | tx rolled back | redelivery | none |
| 9 | consumer crash after commit, before ack | redelivery | inbox hit → ack | none |
| 10 | handler slower than `ack_wait` | redelivery while running | idempotent handler; heartbeat `InProgress` ack; execution handler splits into short txs and does the network call between them | double *processing* possible, never double *effect* (CAS/inbox) |
| 11 | poison message (schema/logic) | infinite redelivery | `max_deliver`, backoff, `PARKED` inbox row + `dlq.*` + alert | operator resolves; state machine unaffected |
| 12 | event overtakes its own state (consumer reads DB before replica applied) | reads stale | read primary / require `version ≥ aggregate.version` | park with backoff |
| 13 | **PITR / DB restore to earlier time** | events published for state that no longer exists | consumers verify the aggregate exists at `≥ version` before acting; execution re-derives from rows; operator runbook: after restore, rebuild streams from outbox | phantom events are ignored, not executed |
| 14 | clock skew between hosts | freshness misjudged | use DB time for freshness/lease; `occurred_at` informational | NTP alert |
| 15 | unbounded inbox/outbox | storage growth | retention jobs; partitioned tables; gauge + alert | must be sized (open decision OD-08) |
| 16 | consumer restarted with wrong start position | missed or replayed history | position derived from DB high-water mark; shadow gate compares | reconciliation detects |
| 17 | two consumers for one durable name | competing handlers | single instance guarded by lease where order matters (execution per account) | none |
| 18 | schema evolution | unknown `schema_version` | park, never drop; expand/contract versions | dual-version window documented |
| 19 | outbox write omitted by a code path | **silent lost event** | lint/test: every domain command that changes a state listed in `03` must insert an outbox row; property test compares state transitions to emitted events; reconciliation rule "state without event" | review item |
| 20 | someone reads the DB to find work (DB-as-queue) | polling coupling, ordering loss | architectural rule + review check; consumers act on events and re-read *by key* | review item |

## 3. Domain-specific notes

* **Execution** treats #5, #10, #13 as first-order: it never sends on the basis of a received event alone; it re-reads the intent by key, checks state and freshness, and takes the CAS.
* **Ownership/authority** events are *not* the authority: a consumer ignoring or replaying `authority.changed` cannot alter authority; only `runtime_lease`/`authority` rows can.
* **Observation** uses the same envelope but weaker guarantees are acceptable (advisory), so no inbox table is required; a bounded in-memory dedupe suffices.

## 4. Inbox idempotency model (summary)

| Effect | Idempotency mechanism |
|---|---|
| create business object | unique natural key (`signal_id`, `execution_intent_id`, `action_key`) |
| state transition | CAS on `state`/`version`, allowed-transition table |
| external broker write | idempotency key + pre-send attempt + reconciliation |
| emit downstream event | outbox row keyed by deterministic id derived from `(causation_id, event_type)` so a replayed handler regenerates the *same* `event_id` |
| projector file write | projector idempotent: writes by deterministic content; compares canonical hash before rewriting |

Deterministic downstream `event_id` (last row) matters: if a redelivered handler minted a random new id, duplicates would leak past `Nats-Msg-Id` and the inbox of the next consumer.
