# 09 - JetStream topology for observation and management (design; nothing created)

Baseline facts: V1.2 defines two streams (`TRADING_CORE`: `strategy.*`, `signal.*`; `EXECUTION`: `execution.*`, `broker.*`, `ownership.*`), file storage, `limits` retention, 30-day max age, and **no** `trade.*` or management subject (`M16`). A2 (`docs/architecture/04_DOMAIN_EVENTS.md`) proposed `TRADING_SIGNALS` carrying `signal.>` and `trade.>`. A4 (`03`, `09`) proposed a **separate, bounded observation stream**. Sizing/retention numbers remain **OD-08**; nothing below fixes production capacity.

## 1. Is a separate observation stream correct? - verified

Yes, with a refinement.

| Property | Observation events | Decision / trade lifecycle / management signal events |
|---|---|---|
| Volume | (open trades) x (poll rate); highest volume in the trade domain | one per evaluated observation *that is persisted*, but only actionable ones are downstream-relevant; opened/closed are rare |
| Value after processing | low - the durable record is `trade_observation` in PostgreSQL | high - domain facts consumed by Distribution, Personal Execution, Performance, Ops |
| Loss tolerance | rebuildable from PostgreSQL (`08` section 5) | must be redeliverable |
| Retention need | short replay window | longer (audit/replay of downstream consumers) |
| Failure domain | must not starve or evict domain events | - |
| Access | Trading Core internal | Commerce may subscribe **only** to `signal.management.published.v1` (A2 privacy rule) |

Refinement: A2 lumps `trade.>` into one stream. Observation and domain events have opposite profiles, and JetStream forbids overlapping subject filters between streams, so the split is by **explicit subject lists** (no `trade.>` wildcard on either side).

## 2. Streams and subjects (proposed)

| Stream | Subjects | Producers | Notes |
|---|---|---|---|
| **`TRADING_OBSERVATION`** *(new)* | `trade.observation.recorded.v1.<instrument>` | Trade Observation Service only | high volume, bounded, discard-old |
| `TRADING_CORE` *(extend V1.2's list)* | `signal.entry.created.v1` (exists), `trade.opened.v1`, `trade.decision.made.v1`, `trade.closed.v1`, `signal.management.published.v1` | Signals, Trade Management, Publication gate | domain events; A2 names + V1.2 `.v1` convention |
| `EXECUTION` *(exists)* | `execution.*`, `broker.*`, `ownership.*` | Personal Execution | **private**; separate NATS account (A2); the Personal Execution translator consumes `trade.decision.made.v1` from `TRADING_CORE` and publishes only into this stream |

**Naming reconciliation.** The task suggests `management.signal.published.v1`; A2's accepted event catalogue names it `signal.management.published` and Commerce's subscription filter is `signal.published`, `signal.retracted`, `signal.management.published`, `signal.outcome.published`. Adopting `management.signal.published.v1` would fall outside the `signal.>` family Commerce is entitled to and outside V1.2's `signal.` prefix routing. **Recommendation: `signal.management.published.v1`** (A2 name + V1.2 version suffix).

Subject token `<instrument>` (canonical, e.g. `XAUUSD`) lets consumers filter by instrument; the ordering key is `managed_trade_id` (in the payload/header), **not** a subject token, so a trade's ordering does not depend on subject sharding.

## 3. `TRADING_OBSERVATION` settings

| Setting | Recommendation | Reason |
|---|---|---|
| Storage | file | survives broker restart; memory would lose the replay window |
| Retention policy | `limits`; `discard = old` | bounded; never blocks producers |
| `max_age` | **not fixed here (OD-08)**; lower bound = staleness horizon of the strictest TM version + replay window | observations older than the stale limit are useless to the evaluator |
| `max_bytes` / `max_msgs` | **OD-08**; must be set so observation bursts cannot exhaust the account's storage | isolation from domain streams |
| `max_msgs_per_subject` | optional bound per instrument | cap memory of a runaway producer |
| Duplicates window | default (2 min) or as configured; deterministic `Nats-Msg-Id = observation_id` | absorbs producer retries; the inbox absorbs the rest (A4 failure modes 3, 7) |
| Account | prefer a separate JetStream account/quotas from `EXECUTION` | A2 privacy + starvation isolation |
| Replay source of truth | PostgreSQL `trade_observation` | stream loss is repaired by republishing from rows |

## 4. Consumers

| Consumer (durable) | Stream | Behaviour |
|---|---|---|
| `trade-manager-evaluator` | `TRADING_OBSERVATION` | pull, **explicit ack**, `ack_wait` > handler p99 (handler is DB-only, no network), `max_deliver` bounded (proposal 5) with exponential backoff, `max_ack_pending` small (per-trade order is enforced by `observation_seq`, `08`) |
| `trade-manager-shadow` (P4.4) | `TRADING_OBSERVATION` | same, results recorded with `role = SHADOW`, no outbox effects |
| `research-observer` (optional, replaces Phase 7's role) | `TRADING_OBSERVATION` | read-only; own durable; may lag freely |
| `personal-exec-translator` | `TRADING_CORE` (`trade.decision.made.v1`) | P5-blocked for real intents (`13`) |
| `distribution-published` | `TRADING_CORE` (`signal.management.published.v1`) | Commerce/Distribution side, not part of P4 |

**Poison / quarantine.** After `max_deliver` failures the handler writes a **`quarantine` row** (event id, reason, payload hash) in the same transaction as the inbox `PARKED` marker and publishes to `dlq.trade.observation`; the ManagedTrade is flagged `OBSERVATION_QUARANTINED` (no decision from the poisoned observation, evidence gap recorded). Parked items never block other trades: the durable consumer acks the poison message after quarantine.

## 5. Stale-observation policy

Two layers: (1) **stream** - `max_age` drops observations that could never be useful; (2) **evaluator** - per-version `max_market_age_ms` turns an old-but-delivered observation into `HOLD(STALE_MARKET_DATA)` (`08`). The producer also refuses to publish from a provider whose quote age exceeds its own bound, recording a producer-side gap instead.

## 6. Observability (what P4 must be able to see)

Per stream/consumer: `consumer_lag_messages`, `consumer_lag_seconds`, redeliveries, parked/quarantined count, oldest unacked age; per ManagedTrade: `last_observation_seq`, `last_applied_seq`, `observation_gap_open`, `time_since_last_observation`; producer: provider quote age, snapshot rate, outbox oldest unpublished age (A4 `15`). Alert when a trade has no observation for more than the version's stale limit (silent producer failure is the exact failure the current path hides: `MISSING_QUOTE` was only visible as an `incomplete_intervals` counter).

## 7. Not decided here

Production sizing, replica count, retention numbers, account layout (`OD-08`); whether the evaluator shards by instrument (not needed at expected scale).
