# Price Action historical replay freeze

This is retrospective research evidence for the frozen, `RESEARCH`-status
evaluators. It is not a validation, graduation, deployment, execution, or
broker-realized outcome result.

The full native MT5 export was inventoried and hash-pinned. The replay uses the
common 30-calendar-day tail, 2026-08-19 21:00 UTC through 2026-09-18 21:00 UTC,
with 120 H1 bars and 6 M15 bars of causal warm-up. The 30-day selection was
declared before replay because the current evaluator implementation is
computationally expensive; broader history is present in the inventory but was
not replayed. The period is retrospective because prior exposure is unknown.

Source records are completed native H1 and M15 candles. `time` is the UTC
candle-open epoch; close is derived only as open plus the timeframe duration.
No H1 aggregation, gap fill, availability timestamp, or revision timestamp was
used. Weekend/market-closure gaps are retained and classified from the source
gap ledgers; unexpected gaps, invalid OHLC, duplicates, and ordering failures
are readiness blockers.

Exact commands:

```text
PYTHONPATH=. python3 research/price_action_historical_replay.py
PYTHONPATH=. pytest -q tests/test_price_action_historical_replay.py tests/test_price_action_engulfing_continuation.py tests/test_price_action_sr_retest.py tests/test_evaluation_foundation.py
```

The replay calls `EngulfingContinuationEvaluator` and
`SupportResistanceRetestEvaluator` directly. Their emitted proposals and
next-open observations are reported separately. No `EntrySignal` is produced,
so no outcome resolver, cost model, expectancy, profit factor, or drawdown
claim is valid. Outcome status is explicitly `BLOCKED` in the report.

