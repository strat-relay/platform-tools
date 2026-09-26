# ADR: Authority model for the real-time signal data plane

Status: proposed (prototype, not adopted). Applies only within the scope of this branch's
architectural evaluation.

## Context

The mission that produced this prototype asks explicitly: don't force one datastore to be
authoritative for everything. Today's DB-first path implicitly treats PostgreSQL as
authoritative for almost everything downstream of a strategy decision, because JetStream
publication only ever happens *after* a PostgreSQL commit (via the transactional outbox). That
conflates two different questions: "has this signal been durably decided and recorded for
reporting/audit" and "has this signal been durably and irrevocably accepted for real-time
delivery." NATS-first separates them. This ADR makes the separation explicit so neither path
accidentally claims authority it doesn't actually hold.

## Decision

Authority is assigned per class of information, not per system:

| Class | Authoritative system | Why |
|---|---|---|
| Control/business state (StrategyDefinition, ParameterSet, strategy lifecycle, users, subscriptions, entitlements, pricing, configuration) | **PostgreSQL** | Unchanged by this prototype. Low-frequency, relational-integrity-critical, no latency pressure. |
| Real-time event acceptance (has this `EntrySignal`/`ManagementSignal`/future `ExecutionIntent` been durably accepted onto the backbone that downstream consumers read from) | **JetStream durable acceptance (PUBACK)**, only in `NATS_FIRST` mode. In `DB_FIRST` mode this class doesn't exist independently - PostgreSQL commit is the acceptance event, by construction. | A PUBACK is JetStream's own durability contract; requiring it (and only reporting success once received - see `dataplane/realtime_publisher.py`) is what makes "accepted" mean something. PostgreSQL is not on the critical path for this class in `NATS_FIRST` mode. |
| Query/reporting history (what has this strategy signaled, when, with what evaluation trace) | **PostgreSQL, via the projector** | Reused schema; relational, queryable, joins against control-plane tables. JetStream is not a query engine and is not meant to become one. |
| Actual broker positions/orders | **The broker** | Unaffected by this prototype. Neither path claims to know a broker's state better than the broker. |
| Execution permission (may this process currently place/modify/cancel an order) | **Broker-held, authenticated, expiring fencing capability (P5 / OD-06)** | Explicitly *not* solved by JetStream durability or PostgreSQL locking. See `05_V2_EXECUTION_COMPATIBILITY.md`. Neither JetStream sequence numbers nor a PostgreSQL row lock is a substitute for a broker-verified fence. |

## Consequences

- A signal can be **durably accepted for real-time delivery** (`NATS_FIRST` mode, PUBACK
  received) while its PostgreSQL projection is still pending. This is intended: distribution and
  Trade Manager intake read from JetStream, not from the projector's output, so they are not
  blocked by projection lag (failure scenario G).
- A signal can be **durably recorded in PostgreSQL** (`DB_FIRST` mode, transaction committed)
  while its JetStream publication (via the outbox relay) is still pending, or has failed and is
  queued for retry. This is the existing, accepted behavior of the outbox pattern and is
  unchanged.
- Neither mode makes JetStream authoritative for reporting/audit history, and neither mode makes
  PostgreSQL authoritative for real-time acceptance (in `NATS_FIRST` mode). A query against
  "signals reported so far" always means the PostgreSQL projection, in both modes; the two modes
  differ only in *when* that projection is guaranteed to be complete relative to acceptance.
- Execution permission is never derived from either the message bus or the relational database
  in this architecture, in either mode. It comes from the broker-held fence, independent of and
  outside the scope of this prototype.

## Alternatives considered and rejected

- **JetStream sequence number as an execution fence.** Rejected: a monotonic bus sequence
  proves ordering and durability of *messages*, not permission to act on a *broker account*. Two
  processes could both hold valid, ordered JetStream messages and still race for the same
  broker-side fence. This is explicitly called out as a non-solution to OD-06 in the mission and
  in `05_V2_EXECUTION_COMPATIBILITY.md`.
- **Single-authority PostgreSQL for everything (status quo, extended).** Rejected as the
  *sole* answer because it makes real-time distribution and Trade Manager intake latency-coupled
  to PostgreSQL health and write latency (see benchmark, `postgres_slowdown`/`postgres_contention`
  scenarios), which is precisely the coupling this evaluation exists to measure.
- **Single-authority JetStream for everything, including reporting.** Rejected: JetStream is not
  a relational store; ad hoc reporting/joins/aggregate queries belong in PostgreSQL, and the
  reused schema already exists and is not being thrown away (reuse mandate, mission section 2).
