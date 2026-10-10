# Unified Outcome Resolver signal contract

This is the platform handoff contract for strategy engines. A strategy emits a
canonical signal and setup state; it does not monitor candles, calculate an
outcome, call execution services, or write `strategy.entry_signal_outcomes`.

## Required identity and provenance

`signal_id` is the canonical immutable identity. `historical_signal_ids` may
list aliases used by replay/import, but aliases never create another outcome
row. The signal carries `strategy_id`, `strategy_version`, `instrument`,
`direction`, `decision_time`, `signal_emitted_at`, `entry_type`,
`entry_price`, `stop_price`, `target_price`, and source provenance. If supplied,
`economic_position_id`, `entry_opportunity_id`, and `setup_id` are reporting
keys only; each signal remains an individual record.

## Versioned `outcome_contract`

The nested contract is versioned independently from the strategy:

```json
{
  "version": "entry-outcome.v2",
  "timeframe_minutes": 15,
  "activation": "SIGNAL_TIMESTAMP",
  "max_hold_minutes": 120,
  "expiration_minutes": null,
  "time_exit_price": "CLOSE",
  "price_basis": "THEORETICAL_TOUCH"
}
```

`THEORETICAL_TOUCH` evaluates the recorded candle OHLC and reports a strategy
result. It must not be described as a broker fill. `EXECUTABLE_BID_ASK` requires
the relevant persisted BID/ASK extrema and close. A spread, midpoint, or
synthetic quote is not sufficient. Missing quote evidence produces
`OPEN` + `INSUFFICIENT_DATA`.

The resolver activates entries causally, evaluates completed candles in time
order, preserves same-candle ambiguity as `AMBIGUOUS_INTRABAR`, and does not
invent terminal outcomes across a candle gap or missing coverage.

## Outcome and execution boundary

Canonical strategy outcomes use the status taxonomy `OPEN`, `TARGET_HIT`,
`STOPPED`, `TIME_EXIT`, `EXPIRED`, `PROFIT_EXIT`, and `INVALIDATED`, with a
resolution state of `RESOLVED`, `INSUFFICIENT_DATA`, `AMBIGUOUS_INTRABAR`, or
`REJECTED`. The only canonical writer is the platform resolver under the
`RESOLVER_PRIMARY` fence. Broker fill, broker exit, realized broker R, and
execution reason are separate attribution facts and never replace the
theoretical strategy outcome.

Paper, shadow, and live signals retain their evidence mode and record mode in
provenance. Those modes affect publication and broker authorization, not the
identity or replay semantics of the theoretical outcome.
