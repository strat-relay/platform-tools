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
  "price_basis": "THEORETICAL_TOUCH",
  "same_candle_priority": "AMBIGUOUS_INTRABAR",
  "time_exit_priority": "AFTER_PRICE"
}
```

`THEORETICAL_TOUCH` evaluates the recorded candle OHLC and reports a strategy
result. It must not be described as a broker fill. `EXECUTABLE_BID_ASK` requires
the relevant persisted BID/ASK extrema and close. A spread, midpoint, or
synthetic quote is not sufficient. Missing quote evidence produces
`OPEN` + `INSUFFICIENT_DATA`.

`same_candle_priority` must explicitly be `AMBIGUOUS_INTRABAR`, `STOP_FIRST`,
or `TARGET_FIRST`. `time_exit_priority` must explicitly be `AFTER_PRICE` or
`BEFORE_PRICE`. Liquidity V1's frozen legacy behavior is represented by
`STOP_FIRST` and `BEFORE_PRICE`; new Liquidity signals must emit those fields
rather than relying on strategy-name inference.

The resolver activates entries causally, evaluates completed candles in time
order, preserves same-candle ambiguity as `AMBIGUOUS_INTRABAR`, and does not
invent terminal outcomes across a candle gap or missing coverage.

The resolver is also the sole owner of the canonical outcome row. On first
discovery it idempotently creates or adopts the signal's `OPEN` row, then may
advance that row to a terminal status in the same transaction. Signal
publishers and strategy-specific `after_publish` hooks must not create or
update `strategy.entry_signal_outcomes`; any compatibility hook must be
disabled before the `RESOLVER_PRIMARY` fence is enabled. Existing `OPEN` rows
are adopted without resetting terminal facts, and are replayed from the
durable signal cursor (or from the signal decision time when no cursor exists).

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

## Liquidity handoff checklist

Before Liquidity Live can replace its standalone monitor, each published
signal must include:

- `strategy_metadata.outcome_contract` with `entry-outcome.v2`, `M5`,
  `SIGNAL_TIMESTAMP`, the configured hold/expiration values, `CLOSE`,
  `THEORETICAL_TOUCH`, `STOP_FIRST`, and `BEFORE_PRICE`.
- `provenance.provider_symbol` (for example `EURUSDm`) so the resolver can
  read the same broker-symbol candle archive after a restart.
- `decision_time` as the causal event time, and `signal_emitted_at` as the
  publication wall-clock time; replay uses the former.
- `entry_opportunity_id` for opportunity grouping and, when available,
  `economic_position_id` for independent-signal reporting. Neither replaces
  `signal_id` as the outcome key.

The orchestrator persists the canonical signal and outbox event only. The
resolver discovers that signal, creates/adopts its `OPEN` row, replays
completed M5 candles, and owns terminal settlement. Broker execution remains
an attribution input and cannot create or replace the theoretical outcome.
