# Cached execution risk state

**Status:** implemented behind `RISK_CONTEXT_SOURCE` (default `BRIDGE`, unchanged production behaviour).

## Ownership

| Concern | Owner |
|---|---|
| Broker observation (account, positions, orders, deal history, symbol metadata) | **Risk-state collector** (`execution_v2/risk_state/collector.py`), the only component that polls the read bridge (22347) for risk |
| Hot risk snapshot + execution risk reservations | **Redis** (`execution_v2/risk_state/store.py`) |
| Durable audit/history (intents, attempts, results, risk evidence) | **PostgreSQL**, unchanged |
| Risk decision on a signal | **Execution** (`CachedRiskGate`): reads Redis only; never the bridge |
| Actual positions and orders | **The broker**, always |

**Invariant: the cache is never more authoritative than the broker.** Every snapshot value is a
timestamped broker observation, refreshed on a fixed cadence and immediately after known broker
changes. Periodic collection is the reconciliation that corrects cache drift. When the cache can't
prove it's fresh and trustworthy, execution fails closed; it never falls back to reading MT5
synchronously.

## Flow

```
Broker/MT5 -> read bridge (22347) -> collector -> normalize/validate -> Redis RiskSnapshot
EntrySignal -> read Redis snapshot -> freshness + health -> evaluate_candidate (unchanged)
            -> atomic reservation (Lua) -> existing authority / fence / idempotency -> 22348
```

## RiskSnapshot (`risk-snapshot.v1`)

`account_ref` (a non-reversible hash, never the raw account id), `provider`, `generation`,
`observed_at`, `balance`, `equity`, per-component timestamps (`equity_observed_at`,
`positions_observed_at`, `orders_observed_at`, `history_observed_at`), `open_positions`
(ticket, provider symbol, canonical instrument, direction, volume, open price, stop loss, take
profit), `pending_orders`, `trading_day`, `daily_realized_pnl`, `daily_loss`, `source_health`,
`source_errors`.

Missing critical values are never treated as zero. A malformed broker row fails that collection
and keeps the previous valid snapshot. `daily_loss` keeps the bridge path's exact semantics: the
sum of today's (UTC) losing deal results; gains never offset losses.

## Collection cadence

| Class | Contents | Default | Max age used by execution |
|---|---|---|---|
| fast | equity/balance, positions, pending orders (+ metadata for any new position symbol) | `RISK_FAST_REFRESH_SECONDS=10` | positions/orders/equity: 30 s |
| history | today's deals -> daily loss | `RISK_HISTORY_REFRESH_SECONDS=30` | 90 s, and it must be for today's UTC trading day |
| reference | symbol sizing metadata for allowed symbols and position symbols | `RISK_REFERENCE_REFRESH_SECONDS=300` | 900 s |

History at 30 s means realized loss can lag the broker by at most about 90 s before execution
refuses. Losses that realize inside that window are bounded by the stops of positions the snapshot
already shows, and by `max_concurrent_positions`.

## Freshness and health gate (execution)

The snapshot is **missing**, the **fast/history source is not `healthy`** (its latest collection
failed), or a critical value is absent → `RISK_STATE_UNAVAILABLE`. A component is older than its
max age, or history belongs to a previous day → `RISK_STATE_STALE`. Every rejection records
`risk_context_failure {stage, code, retryable}` in `execution_risk_evidence.diagnostics`.

Policy choice (test E): a snapshot that is still young but whose latest collection failed is
**not** used. One failed fast collection blocks execution until the next successful one.

## Positions are normal risk input

Open positions no longer abort risk construction (the bridge path raised
`open-position exposure cannot be calculated safely`, which became `RISK_STATE_UNAVAILABLE`).
They count toward `max_concurrent_positions`, so with a limit of 1 an occupied slot is
`MAX_CONCURRENT_POSITIONS_EXCEEDED`. They also feed `account_exposure`.

**`account_exposure` definition (new; confirm before cutover):** the open stop-risk in account
currency, meaning the sum over positions of `volume * max(0, distance from open to stop) / tick_size
* tick_value`, plus active reservations' reserved risk. A position without a stop is unbounded
(`MAX_ACCOUNT_EXPOSURE_EXCEEDED`). A position whose symbol metadata can't be obtained fails closed.
Before this change the value was always a hard-coded `0.0`, so `max_account_exposure` has never
actually been enforced.

## Atomic reservation

`RESERVE_LUA` evaluates, in one Redis script: broker snapshot (generation-checked) + active
reservations against daily loss, positions, orders and exposure, then inserts the reservation.
Two signals can't both take the last slot, even if both local reads saw capacity.

Lifecycle:

```
RESERVED --submit--> SUBMITTED --confirm--> CONFIRMED --(snapshot newer than confirmation)--> RETIRED
   |                     |  \--ambiguous--> UNKNOWN --(reconciliation from PostgreSQL only)--> CONFIRMED | RELEASED
   |                     \--broker rejection / never dispatched--> RELEASED
   |--definite pre-submit rejection--> RELEASED
   \--TTL (RISK_RESERVATION_TTL_SECONDS=60), only while RESERVED--> EXPIRED
```

- The attempt reaches SENDING only after a successful `RESERVED -> SUBMITTED`. An expired or
  released reservation ends the attempt `NOT_SENT`.
- `SUBMITTED` and `UNKNOWN` never expire, and worker paths can't release `UNKNOWN`. The collector
  releases or confirms them only when `execution_v2.execution_attempt` shows a definite outcome
  (`CONFIRMED` / `REJECTED`, `FENCED`, `NOT_SENT`, `CANCELLED`, `FAILED`). `UNCERTAIN`/`SENDING`
  keep holding capacity: UNKNOWN_RECONCILIATION_REQUIRED semantics are unchanged.
- A fill or broker rejection requests an immediate collector refresh (`risk:refresh:<ref>`).
  Execution never waits for it.

## Cutover plan (separate, operator-approved)

1. Deploy a dedicated Redis in the `trading` namespace (do not share another application's Redis).
2. Deploy the collector (`python -m execution_v2.risk_state.collector_service`) with read-only
   bridge and read-only PostgreSQL. Leave the execution runtime on `RISK_CONTEXT_SOURCE=BRIDGE`.
3. Observe for at least a trading session: collector health `healthy`, component ages within
   limits, snapshot positions/orders matching the broker, `daily_loss` matching the bridge path.
4. Confirm the `account_exposure` definition and the `max_account_exposure` value against it.
5. Set `RISK_CONTEXT_SOURCE=REDIS` and `RISK_REDIS_URL` on the execution runtime. This does not
   change execution authority. Rollback: set it back to `BRIDGE`.
