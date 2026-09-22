# Ordering guarantees: what is actually required

The mission asks that global ordering not be imposed unless required. This document works
through each ordering-sensitive relationship in the target architecture and states what is
actually needed, so the subject/consumer design in `dataplane/` isn't accidentally either too
weak (silent reordering that breaks correctness) or too strong (unnecessary global serialization
that caps throughput for no reason).

## What's required, per relationship

**1. Per-signal lifecycle (a single `EntrySignal`'s own events).** Required: yes, strictly.
`entry.accepted` must be processed before any later lifecycle transition for the *same*
`signal_id` (a future `ManagementSignal` referencing it, a future `ExecutionResult`, etc). This
prototype only implements the first stage (`entry.accepted`), so there is currently only one
event per signal at this layer. The envelope already carries `aggregate_id=signal_id,
aggregate_version=1` (`dataplane/realtime_publisher.py`); future lifecycle stages should
increment `aggregate_version` per `aggregate_id` so a consumer can detect out-of-order delivery
of the *same aggregate* even under redelivery, without needing global ordering.

**2. Per-strategy ordering (two different signals from the same strategy).** Required: not for
correctness of this prototype's scope. Two `EntrySignal`s from the same `strategy_id` are
independent domain aggregates (different `signal_id`s) with no defined relationship the current
schema enforces between them. Nothing downstream (projector, distribution, Trade Manager intake
as designed here) needs to see strategy A's signal 1 before strategy A's signal 2. If a future
requirement emerges (e.g. "a strategy's signals must be evaluated by Trade Manager in emission
order because a later signal can supersede an earlier one"), that becomes a **new**, explicit
requirement on a **specific** consumer (Trade Manager), not a property the transport should be
made to guarantee for everyone. See the "if this changes" note below.

**3. Per-instrument ordering.** Required: not by this prototype's scope, for the same reason as
per-strategy: nothing here reads "all signals for XAUUSD in order" as a unit. If a future
instrument-level aggregation consumer needs it, it should key its *own* consumer/subject
partitioning on instrument, rather than the publisher imposing a global instrument-ordered
subject scheme that every other consumer would also have to pay for.

**4. Trade Manager observation ordering (EntrySignal -> Trade Manager -> TradeManagerDecision).**
Required: yes, for a *single* entry signal's own management stream, once that exists - the same
per-aggregate-version rule as (1) applies once `ManagementSignal`s exist. Not required *across*
different entry signals' management streams (managing position A's signal does not need to be
ordered relative to managing position B's signal). Trade Manager is not activated in this
prototype (`dataplane/distribution.py` only demonstrates the intake boundary), so this is
recorded as a design constraint for when it is built, not something enforced by code here.

**5. V2 execution ordering (EntrySignal/execution trigger -> execution/risk service -> broker).**
Required: yes, strictly, per economic position - two execution intents against the *same*
broker-held position must never be allowed to race or reorder at the point they reach the
broker. This is **not** solved by JetStream ordering, and this prototype does not attempt to
solve it: it is exactly the broker-held authenticated expiring fence (P5/OD-06) that remains
mandatory (`05_V2_EXECUTION_COMPATIBILITY.md`). JetStream subject/consumer ordering can help
route related execution events to the same durable consumer instance (reducing *contention*,
not providing *correctness*), but the actual correctness guarantee has to come from the broker
fence, because JetStream has no knowledge of the broker's authoritative position state.

## Design decision: no additional ordering key beyond `aggregate_id`/`aggregate_version`

Given the above, this prototype does **not** introduce per-strategy or per-instrument JetStream
subject partitioning, and does **not** rely on JetStream's stream-level FIFO-per-subject
ordering for anything beyond "redelivery of the same message is still recognizable as the same
message" (which `Nats-Msg-Id`/`event_id` dedup already gives, independent of subject ordering).
`realtime.signal.entry.accepted.v1` is a single subject on the `SIGNAL_REALTIME` stream; multiple
signals from the same or different strategies interleave freely, and every consumer
(`SignalPersistenceProjector`, `RealtimeConsumer` for distribution, `RealtimeConsumer` for Trade
Manager intake) is written to be correct regardless of delivery order across *different*
`signal_id`s - each `handle()` call is self-contained, keyed by the envelope's own `aggregate_id`
and `event_id`, with no dependency on "the previous message I saw."

**If this changes** (a future requirement genuinely needs per-strategy or per-instrument
ordering across signals, not just within one signal's lifecycle), the recommended mechanism is a
subject suffix or JetStream subject-mapping key (e.g. `realtime.signal.entry.accepted.v1.<strategy_id>`
consumed by strategy-keyed durable consumers), not a global sequence - so ordering cost is paid
only by consumers that actually need it, and unrelated strategies/instruments remain
independently, concurrently processable.

## Redelivery and idempotency are the actual correctness backstop, not ordering

Because JetStream is at-least-once, *any* ordering guarantee it does provide can still be
violated from a consumer's point of view under redelivery (a message can be reprocessed after
later messages were already seen). The correctness properties this architecture actually relies
on are the ones proven in `tests/test_dataplane_projector.py` and
`tests/test_dataplane_failure_scenarios.py`: deterministic `signal_id`, `entry_signal_hash`
content-identity checking (`CanonicalSignalIdentityConflict`/`SignalProjectionConflict`), and
idempotent writes (`ON CONFLICT DO NOTHING`, inbox claim/mark-processed) - not message order.
Ordering is a performance/design convenience where it's needed (relationship 1 and 5 above); it
is never the sole mechanism relied on for correctness anywhere in this design.
