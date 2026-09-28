# Trade Manager LIVE: personal management execution

**Status:** implemented. Nothing reaches a broker until an operator sets the Trade Manager mode to
`LIVE` *and* execution authority is `ENABLED`. Migration 036 leaves the mode at its current value.

## Flow

```
Trade Manager (decision_engine)          -- never talks to a broker --
  trade.decision.made.v1 (outbox -> TRADING_CORE)
      |
execution-v2-runtime: durable consumer "execution-v2-management" (DeliverPolicy.NEW)
  execution_v2.management.ManagementWorker.process_decision
      | authorize (below) -> execution_v2.management_intent row
      | shared ownership lease + generation (resource execution:real:<account>)
      | REDUCE_ONLY WriteAuthorization bound to the exact request fingerprint
      v
22348 execution bridge: durable fence (tool + REDUCE_ONLY scope + fingerprint), attempt ledger
      v
EA: mt5_position_modify (SL/TP) | mt5_close_position
```

## Authorization (legacy `trade_manager/central.authorize()` rules, now in PostgreSQL)

| Rule | Otherwise |
|---|---|
| action is MOVE_TO_BREAKEVEN / TRAIL_STOP / MOVE_STOP / MOVE_TARGET / EXIT | ignored (no row) |
| Trade Manager mode is `LIVE` | ignored |
| the trade is linked to a platform position (managed_trade -> execution_intent -> FILLED result -> ticket) | ignored: paper signals and manual positions are never touched |
| execution authority `ENABLED` (checked again right before the write) | `REJECTED` / `FENCED` |
| decision is at most 120 s old | `REJECTED STALE_DECISION` |
| the position is at the broker now, in the trade's direction | `REJECTED POSITION_NOT_FOUND` / `POSITION_DIRECTION_MISMATCH` |
| the resulting stop is set and never looser than the broker's stop | `REJECTED RISK_INCREASING_STOP_CHANGE` |
| the request differs from the broker's SL/TP | `NO_CHANGE` (no write) |
| the action key (ticket, action, stop, target) is new | `REJECTED DUPLICATE_MANAGEMENT_ACTION` |

The EA independently refuses to remove or widen a stop and levels inside the broker stop distance.

## Outcomes

`APPLIED` (EA confirmed, or goal state reached), `BROKER_REJECTED` (EA/broker refusal, with reason),
`FENCED` (lease, authority or bridge fence refused before dispatch), `UNKNOWN_RECONCILIATION_REQUIRED`
(transport lost and the position does not show the requested state - never retried automatically).
An ambiguous outcome is reconciled by goal state: the position's SL/TP equal the request, or the
ticket is gone after a close.

## Rollback

Set the Trade Manager mode to `SHADOW` (Console or `POST /api/v1/trade-manager/mode`). Decisions keep
being recorded; nothing is sent. Disabling execution authority also stops all writes.
