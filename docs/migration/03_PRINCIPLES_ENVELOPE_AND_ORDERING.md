# 03 - Migration principles, event envelope, topology, ordering, effective-once

## 1. Principles

1. **No flag day.** Each artifact moves on its own schedule with its own gate and rollback class (`04`, `12`, `13`).
2. **One writer per fact at every instant.** A migration may have two *readers* of a fact; it may never have two *authorities*. Where two copies exist, one is generated from the other by a single writer (the projector).
3. **Transaction + outbox + projector over application dual-write.** Section 3.
4. **PostgreSQL is never a queue.** Consumers do not poll domain tables for work. The outbox is a bounded relay buffer drained by the publisher only.
5. **NATS is never the system of record.** JetStream retention is an operational convenience; any stream can be rebuilt from PostgreSQL. Decisions are made from rows.
6. **Fail closed.** In `DB_PRIMARY`, database or fence failure stops authority-bearing actions (arming, intent creation, sending, authorization). There is **no fall back to JSONL**.
7. **Frozen means frozen.** No strategy, Trade Manager policy or execution policy changes. The canonical-order-send timeout anomaly is frozen: it is *representable* (an uncertain attempt), not redesigned.
8. **Measurable gates, deterministic reconciliation** (`11`, `12`): stable IDs, canonical hashes, aggregate versions, ownership generations and terminal states - not row counts.
9. **Most conservative last.** Ownership and execution move after everything they depend on has been running in shadow (`16`).

## 2. Choosing the strategy per artifact

| Property of the artifact | Strategy | Why |
|---|---|---|
| Written by frozen legacy code | `TAILER_INGEST` | cannot add a transaction to code that must not change; ingest is idempotent by stable ID |
| Written by platform code, record + notification | `OUTBOX_PROJECTOR` | one domain tx; file (if still needed) is generated |
| Authority whose double existence is dangerous (execution, ownership fence, arming, resume generation) | `DIRECT_CUTOVER` | dual authority is the failure to avoid; single instant + fence (`08`) |
| Pure queue/transport between platform services | `REPLACE_BY_EVENTS` | subject + durable consumer + inbox |
| Progress marker whose correctness can be enforced by constraints | `DB_ONLY` / retire | e.g. `processed_signal_ids` becomes a unique index |
| Derived, never authoritative | `REBUILD` | regenerate; nothing to migrate |
| No live reader | `RETIRE` | after a proof-of-no-reader window |
| Config, research, debug | `KEEP` | out of scope of `PRODUCTION_FILE_IPC=0` |

The nine-step ladder from the task is retained as **vocabulary for states a domain can be in** (see runtime modes in `14`), not as a sequence each artifact must traverse: e.g. `RECONCILED_DUAL_WRITE` is realised as *outbox + projector + reconciliation* and never as two application writes; `DB_AUTHORITY_FILE_MIRROR` is the projector running; `LEGACY_READ_DISABLED`/`LEGACY_WRITE_DISABLED` are gates `LEGACY_READ_RETIRE_READY`/`LEGACY_WRITE_RETIRE_READY`.

## 3. Why not independent dual-write, and where the outbox pattern cannot apply

Independent application dual-write (`write file; write DB`) fails on every crash between the two statements, cannot be made atomic across a filesystem and a database, and produces divergence that only reconciliation can find. The outbox pattern makes the database commit the single decision and turns "publish" and "write legacy file" into replayable, idempotent consequences of it.

It cannot apply to:

* **Frozen runners** - the write is not ours to change. We *observe* the file, canonicalise, and ingest with a unique constraint on the stable ID. The runner file remains the source of truth for the strategy's own evidence until a declarative runtime replaces it; the platform's copy is authoritative for *platform* decisions (routing, execution).
* **The broker send** - an external side effect. Handled by: durable `SENDING` attempt committed **before** the call, an idempotency key the bridge honours, a representable `UNCERTAIN` outcome and reconciliation before any resend (`06`).
* **Fencing** - it must be one decision (`08`).

## 4. Event envelope (proposed contract; Codex V1.2 to compare with its schema)

```json
{
  "event_id":        "uuid (also Nats-Msg-Id; equals outbox.id)",
  "event_type":      "signal.entry.created",
  "schema_version":  1,
  "occurred_at":     "domain time (UTC)",
  "committed_at":    "DB commit time (UTC)",
  "aggregate":       {"type": "signal", "id": "SIG-…", "version": 1},
  "ordering_key":    "signal:SIG-…",
  "producer":        {"service": "orchestrator", "runtime_instance_id": "…", "fence": {"resource": "…", "generation": 7}},
  "causation_id":    "event_id or command id that caused this",
  "correlation_id":  "signal_id or managed_trade_id",
  "idempotency_key": "only for commands that cause external effects",
  "payload":         {"…small, versioned…": true},
  "payload_ref":     {"table": "evaluation", "key": "…", "hash": "sha256:…"},
  "payload_hash":    "sha256:… over canonical bytes of payload"
}
```

Rules: payloads are **small facts + stable references**. The V1.1 `Evaluation` / `DecisionTrace` / `StageResult` / reason codes are referenced by `evaluation_hash` and `trace_hash` (both sha256 over `canonical_bytes`) and a `payload_ref`; the trace itself is **never** embedded (it can be large and is evidence, not a signal). Consumers verify `payload_hash`; unknown `schema_version` goes to a parked state with an alert, not a silent drop.

## 5. Stream and subject topology (proposed)

| Stream | Subjects (prefix `tp.<env>.`) | Retention | Notes |
|---|---|---|---|
| `SIGNAL` | `signal.entry.created`, `signal.evaluation.recorded`, `signal.route.decided`, `signal.sizing.decided` | limits, ≥ 7 d | small facts; evaluation by reference |
| `EXECUTION` | `exec.intent.created.<account_context_id>`, `exec.attempt.updated.<…>`, `exec.result.recorded.<…>` | limits, ≥ 30 d (audit copy is PostgreSQL) | **subject partitioned by account** so one consumer per account preserves order |
| `MANAGEMENT` | `mgmt.proposal.created.<managed_trade_id>`, `mgmt.intent.authorized.<…>`, `mgmt.result.recorded.<…>` | limits | ordered per managed trade |
| `BROKER_STATE` | `broker.state.updated.<account_context_id>`, `broker.position.changed.<account_context_id>` | small, `max_msgs_per_subject` low | latest is in PostgreSQL; stream only wakes consumers |
| `OBSERVATION` | `obs.<instrument>.<kind>` | bounded by size/age, discard-old | the *only* high-volume stream; separate account/limits so it cannot starve the others; no PostgreSQL copy of ticks |
| control | none on JetStream | - | heartbeats and leases are PostgreSQL rows |

Consumers: durable, explicit ack, `max_deliver` bounded with backoff, `ack_wait` longer than the worst handler (execution's handler never blocks on the network *inside* an open DB transaction), dead-letter subject `dlq.<stream>` written together with an inbox `PARKED` row.

**Dedicated-infrastructure assumption.** This design assumes the trading platform has its **own** PostgreSQL database/credentials and its **own** JetStream account/streams, sized and backed up independently of commerce/other workloads (ADR-0001 keeps trading and commerce databases separate). If the existing shared instances are reused, Codex V1.2 must state the isolation (role, schema, account, quotas) explicitly; this document does not change any shared infrastructure.

## 6. Ordering keys

| Domain object | Key | Enforced by |
|---|---|---|
| signal | `signal_id` | unique constraint; events carry `aggregate.version` |
| managed trade | `managed_trade_id` | aggregate version (optimistic); consumer refuses gaps |
| execution intent | `execution_intent_id` | state machine with allowed transitions; single-consumer per account subject |
| account | `account_context_id` | subject partition + `max_ack_pending=1` for the execution consumer |
| position | `position_id` (broker ticket + account) | broker-state `snapshot_version`; ownership row unique |
| ownership resource | `resource` (e.g. `execution:real:<account_context_id>`) | lease `generation` (monotonic, DB-enforced) |

Global ordering is neither provided nor needed. Correctness never depends on JetStream delivery order: a handler that receives an event whose `aggregate.version` is not `current+1` re-reads the aggregate; if still ahead it parks (redelivery with backoff), if behind it is a duplicate (inbox hit).

## 7. Effective-once, layer by layer

| Layer | Prevents |
|---|---|
| `event_id` = outbox id = `Nats-Msg-Id` | duplicate publish within the JetStream dedupe window |
| inbox `(consumer, event_id)` unique, written in the **same tx** as the handler's effects | duplicate handling after redelivery, beyond the dedupe window |
| domain unique constraints (`signal_id`, `execution_intent_id`, `idempotency_key`, `(position, source)` ownership) | duplicate business objects from *different* events |
| aggregate version (CAS `UPDATE … WHERE version = v`) | lost updates, stale authorisations (precedent: `snapshot_version`) |
| fencing token (lease generation) on every authority write | zombie writers after pause/partition |
| broker idempotency key + reconciliation | duplicate broker operation after an uncertain send |

Ack timing: **commit, then ack.** A crash between them yields a redelivery that hits the inbox and is acked without effect.
