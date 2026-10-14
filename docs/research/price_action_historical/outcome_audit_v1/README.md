# Additive Price Action outcome audit

This revision preserves the baseline dataset, protocol, replay report, and
strategy rules. It adds episode reconciliation, cancellation diagnostics, and
an in-memory research-entry adapter.

Run from the repository root:

```text
PYTHONPATH=. python3 research/price_action_outcome_audit.py
PYTHONPATH=. pytest -q tests/test_price_action_outcome_audit.py tests/test_price_action_historical_replay.py tests/test_price_action_engulfing_continuation.py tests/test_price_action_sr_retest.py tests/test_evaluation_foundation.py tests/test_kojo_wedge.py tests/test_strategy_backtest_core.py
```

The accepted observation is not an accepted broker fill. It means the next
M15 open passed the evaluator's gap, stop/target geometry, trend-invalidation,
and actual-entry-R checks. The adapter preserves episode identity, strategy
version, parameter hash, primitive dependency hash, dataset manifest hash,
snapshot identity, entry geometry, and outcome-policy hash in
`accepted_research_entries.jsonl`.

The existing `strategy_backtest.execution.SimulatedExecution` contract was not
invoked. It is incompatible with this frozen protocol because it uses
conservative stop-first ordering for same-candle stop/target touches, has no
explicit data-gap classification, requires an invented expiry for the
unspecified maximum hold, and cannot model unavailable bid/ask, commission, or
slippage evidence. Therefore all 469 accepted research entries remain
unresolved rather than being assigned theoretical outcomes.

