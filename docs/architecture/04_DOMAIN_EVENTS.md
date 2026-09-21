# Domain events and boundary contracts

Design only.  No stream, subject, consumer or schema described here has been created.
NATS JetStream is **not enabled** today (verified in the Postgres/NATS audit: the live
server runs core NATS with `-m 8222` only, `jetstream.disabled = true`, and is shared
with another product — see `05_DATA_OWNERSHIP.md` §6).

## 1. Principles

1. **PostgreSQL is authoritative; NATS JetStream is the durable operational transport.**
   An event is published from a **transactional outbox** written in the same DB
   transaction as the state change, so state and event cannot diverge.
2. **At-least-once delivery, idempotent consumers.**  Every consumer keeps an **inbox**
   (`event_id` processed table) and treats `event_id` as its idempotency key.  Event ids
   are deterministic where the fact is deterministic (existing `stable_id`), so
   re-derivation after a crash yields the same id and is deduplicated.
3. **Ordering is per key, not global:** `stream_id` for signals, `managed_trade_id` for
   management, `subscription_id` for commerce.  JetStream subject partitioning and
   consumer `MaxAckPending=1` per key where order matters.
4. **Only three kinds of thing cross a domain boundary:** events, versioned read APIs,
   and ports.  Never a table, never a file, never an import.
5. **Privacy direction rules** (enforced by schema, reviewed in CI):
   * events **into** Commerce carry no account, broker, size, ticket or internal
     strategy identifiers;
   * events **into** the Trading Core carry no customer PII (Commerce sends none in V1);
   * `execution.*` and `broker.*` events are **never** visible to Commerce credentials.
6. **Production JSON/JSONL IPC is transitional.**  Each current file stream has a named
   successor event (§5); no new production JSONL channel may be introduced.

## 2. Envelope (all events)

```jsonc
{
  "event_id": "sig_pub_9f2c…",            // deterministic where possible, else UUIDv7
  "event_type": "signal.published",
  "schema_version": "1",
  "occurred_at": "2026-09-21T14:05:00Z",   // domain time
  "produced_at": "2026-09-21T14:05:00.412Z",
  "producer": "trading-core/signals",
  "correlation_id": "sig_entry_1a…",       // root EntrySignal id
  "causation_id": "sig_entry_1a…",
  "key": {"stream_id": "LD:XAUUSD", "managed_trade_id": null},
  "sequence": 1287,                        // per ordering key, gap-detectable
  "payload": { }
}
```

`sequence` is gap-detectable per key (generalising the existing `FanoutConsumer` gap
detection in `trade_manager/fanout.py`).

## 3. Catalogue

### 3.1 Cross-domain events

| Event | Producer → consumers | Key | Payload essentials (customer-safe where marked ◆) | Guarantee |
|---|---|---|---|---|
| `signal.entry.created` | Signals → Personal Execution, Trade Management (opens ManagedTrade), Performance | `stream_id` | full EntrySignal (internal ids, provenance) | at-least-once, idempotent on `signal_id` |
| `signal.published` ◆ | Signals → Distribution, read-model builder | `stream_id` | `signal_id, stream_id, instrument, direction, entry{type,level,valid_until}, stop, target(s), timeframe, published_at, sequence, strategy/version display, disclaimer_ref` | ordered per stream |
| `signal.withheld` | Signals → Ops audit | `stream_id` | `signal_id, reason` | audit only |
| `signal.retracted` ◆ | Signals → Distribution, read models | `stream_id` | `signal_id, reason_customer, retracted_at` | ordered |
| `trade.opened` | Trade Management → Performance, Ops | `managed_trade_id` | `managed_trade_id, signal_id, published_signal_id \| null, reference_fill, tm_version` | ordered per trade |
| `trade.decision.made` | Trade Management → Personal Execution, Performance (audit) | `managed_trade_id` | full TradeManagerDecision incl. reason codes, evidence ref, `tm_version` | ordered per trade |
| `signal.management.published` ◆ | Signals(gate) → Distribution, read models | `managed_trade_id` | `managed_trade_id, sequence, action, parameters, reason_customer, published_at` | ordered per trade |
| `trade.closed` | Trade Management → Performance, Ops | `managed_trade_id` | both outcomes `entry_only{…}` and `managed{…}`, `closed_at` | ordered per trade |
| `signal.outcome.published` ◆ | Signals(gate) → Distribution, read models | `managed_trade_id` | final outcome of a **published** trade (`entry_only` for all subscribers; `managed` detail only rendered for TM-entitled) | ordered per trade |
| `live.trade.closed` | Personal Execution → Performance | `stream_id` | broker-reconciled result attributable to a stream/trade (no account id) | at-least-once |
| `performance.snapshot.computed` | Performance → Commerce (claims review) | `series_key` | `snapshot_id, series_key, provenance, n, period, metrics, methodology_version, evidence_hash` | ordered per series |
| `stream.state.changed` ◆ | Strategy → Commerce, read models | `stream_id` | `state, effective_at` | ordered |
| `stream.catalog.updated` ◆ | Strategy → Commerce | `stream_id` | strategy/instrument display metadata, current version label | last-write-wins by `sequence` |
| `instrument.catalog.updated` ◆ | Market → Commerce | `instrument` | code, display name, asset class | LWW |

### 3.2 Commerce-internal events (never leave `stratrelay-platform`)

`subscription.activated | changed | cancelled`, `entitlement.granted | revoked`,
`price.quoted`, `payment.succeeded | failed` (from provider webhooks),
`delivery.requested | attempted | delivered | failed | suppressed`,
`endpoint.verified`, `testimonial.submitted | approved | removed`.

### 3.3 Personal-Execution-internal events (never leave `trading-platform`)

`execution.intent.created`, `execution.result.recorded`, `broker.state.reconciled`,
`position.owned | released`, `execution.kill_switch.changed`,
`execution.lease.acquired | lost`.

### 3.4 Commerce → Trading Core

**None in V1.**  The Trading Core must be able to run, publish and measure with zero
subscribers.  Operator actions (publish/pause a stream, approve a claim) go through the
Ops API, not through events.

## 4. Proposed JetStream layout (design only — not created)

| Stream | Subjects | Retention | Consumers (durable) | Access |
|---|---|---|---|---|
| `TRADING_SIGNALS` | `signal.>` , `trade.>` | limits + age (replay window; DB is the record) | `personal-exec-entry`, `distribution-published`, `readmodel-builder`, `performance-ingest`, `trade-mgmt-open` | trading-core publish; commerce subscribe **only** to `signal.published`, `signal.retracted`, `signal.management.published`, `signal.outcome.published` (internal `signal.entry.created` and `trade.*` are never exposed to commerce credentials) |
| `TRADING_EVIDENCE` | `performance.>` , `live.>` | long | `claims-review` | performance publish |
| `STREAM_CATALOG` | `stream.>`, `instrument.>` | last-per-subject | `commerce-catalog` | trading publish |
| `EXECUTION_PRIVATE` | `execution.>`, `broker.>`, `position.>` | limits | personal-exec only | **no commerce credentials** |
| `COMMERCE_DISTRIBUTION` | `delivery.>` | work-queue | channel workers | commerce only |

Subject convention `<event_type>.<stream_id>` (`signal.published.LD.XAUUSD`) so consumers
can filter by stream.  Per-account NATS credentials enforce the privacy rules of §1.5;
`EXECUTION_PRIVATE` is a separate NATS account.

## 5. Transitional mapping: files → events

| Today (filesystem IPC) | Successor |
|---|---|
| strategy state/ledger JSON → `poll_once` adapters | strategy runner writes opportunity/position facts to Postgres; Signals emits `signal.entry.created` via outbox |
| `runtime/orchestration/signals.jsonl` | `signal.entry.created` (+ `entry_signal` table) |
| `route_decisions.jsonl`, `sizing_decisions.jsonl`, `tradeability_decisions.jsonl` | Personal-Execution tables + `execution.*` |
| `distribution_queue.jsonl`, `delivery_status.jsonl` | **removed**: replaced by publication gate + `signal.published`; delivery state lives in Commerce |
| `runtime/trade_manager/observation_stream/events.jsonl` + `checkpoint.*.json` | `trade.decision.made` + JetStream consumer checkpoints |
| `management_proposals/intents/decisions.jsonl` | Personal-Execution translator tables + `execution.intent.created` |
| `ownership_registry.jsonl`, `broker_state.json` | Personal-Execution tables (`ownership`, `broker_state`) |
| `runtime/execution/*.jsonl`, `real_state.json`, `real_execution_resume*.json` | Personal-Execution tables; `resume_generation` becomes a fenced lease |

Migration technique (later, not now): a **strangler gateway** tails a JSONL stream and
publishes the equivalent event with the same deterministic id, while the consumer runs
in shadow beside the file-based path and parity is verified before cutover.  JSONL/Parquet
remain valid *export* formats for research and backtests only.

## 6. Contract governance

* Schemas live in a dedicated **`stratrelay-contracts`** repository (JSON Schema +
  generated Python/TypeScript types), semver'd; `v1` immutable once tagged, additive
  changes only within a major.
* Producers run **contract tests** against fixtures; consumers run consumer-driven tests.
* The MT5 **bridge protocol** stays in `mt5-native-bridge` (`docs/extraction/02_…`); it
  is a technology adapter contract, not a domain event.
