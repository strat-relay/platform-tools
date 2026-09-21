# Broker reconciliation boundary

Phase 1 defines read-only reconciliation interfaces for account snapshots,
symbol metadata, quotes, open positions, and pending orders. The default dry
run adapter returns no invented positions or orders and never calls broker
write primitives.

Future broker identifiers should be linked as:

```text
signal_id -> execution_intent_id -> broker_order_id -> broker_position_id
```

The future broker comment/magic mapping must be deterministic and include the
execution-intent identity. It is not enabled or emitted by Phase 1.
