# V2 execution compatibility (design-only; no execution code in this branch)

`BROKER_WRITES=0`. This branch implements **no** broker execution, **no** risk service, **no**
fencing mechanism. This document states how a future V2 execution path would sit on top of this
architecture, and - the important part - what this architecture explicitly does **not** provide
for that path.

## Target shape (not built here)

```
EntrySignal / execution trigger
        -> JetStream (durable acceptance, same pattern as dataplane/realtime_publisher.py)
        -> execution/risk service
        -> broker-held authenticated expiring fence validation   <-- NOT provided by this architecture
        -> MT5 bridge
        -> broker
        -> ExecutionResult
        -> JetStream
        -> PostgreSQL projection/audit (same pattern as dataplane/signal_projector.py)
```

The `EntrySignal`-acceptance half of this (trigger -> durable JetStream acceptance -> PostgreSQL
audit projection) is architecturally the same shape already prototyped and tested here:
`RealtimeSignalPublisher` and `SignalPersistenceProjector` demonstrate the pattern generically
enough that a future `ExecutionIntentPublisher`/`ExecutionResultProjector` could reuse it. That
reuse is why this prototype is described as "compatible with," not "in place of," V2 execution
work - **none of that work is done here.**

## What JetStream durability explicitly does NOT provide

- **Not a fence.** A JetStream PUBACK proves a message was durably accepted by the message bus.
  It says nothing about whether the process that published it currently holds authority to act
  on a broker account, and nothing about whether another process might also believe it holds
  that authority. Two processes can both hold valid, durably-published, correctly-ordered
  JetStream messages targeting the same broker position and still race at the broker.
- **Not a substitute for OD-06.** OD-06 (broker-held authenticated expiring fencing) is required
  precisely because *no* message-bus or database mechanism - JetStream sequence numbers,
  PostgreSQL row locks, `SELECT ... FOR UPDATE`, JetStream duplicate-window dedup - can prove
  which process is currently authorized to write to a specific broker account/position. That
  proof can only come from the broker itself (or a broker-trusted intermediary), because only
  the broker knows its own authoritative state. This is true regardless of which signal data
  plane (`DB_FIRST` or `NATS_FIRST`) produced the triggering event.
- **JetStream dedup (`Nats-Msg-Id`) is not a fencing token.** It prevents the *bus* from
  redelivering the identical message to a consumer as if it were new; it does nothing to prevent
  two *different* messages (e.g. a stale retry and a fresh decision) from both reaching an
  execution service that then both attempt to act, if the execution service itself has no fence.

## What remains mandatory, unchanged by this branch

- P5 broker-held authenticated expiring fencing (OD-06) is **still required** before any V2
  execution path may write to a broker, in both `DB_FIRST` and `NATS_FIRST` signal data-plane
  modes. `AUTHORITY_MODEL.md`'s "execution permission" row states this as an authority-model
  decision, not just an implementation detail: execution permission's authoritative source is
  the broker-held fence, full stop - never JetStream, never PostgreSQL.
- This branch does not implement, stub, weaken, or design around that requirement. No fencing
  token is generated, validated, or referenced anywhere in `dataplane/`.
- The MT5 bridge, broker connectivity, and `contracts.mt5_bridge`/`live_execution_consumer`/
  `trade_manager` modules are not imported anywhere in `dataplane/` (enforced by AST-based
  import checks in `tests/test_dataplane_projector.py` and
  `tests/test_dataplane_distribution_and_trade_manager.py`).

## Recommendation for later integration

When V2 execution work begins, the execution/risk service sitting between JetStream and the MT5
bridge is the correct - and only - place to implement OD-06 fence acquisition/validation, using
whatever broker-held mechanism P5 specifies. The data-plane mode
(`DB_FIRST`/`NATS_FIRST`) that produced the triggering `EntrySignal`/execution intent is
irrelevant to that fence: it must be validated identically regardless of which mode is active.
