# Shared theoretical outcome resolution v1

This is an additive, research-only resolution of the 469 accepted next-open
observations from `outcome_audit_v1`. It does not modify the original dataset,
protocol, evaluator outputs, episode ledger, or production resolver callers.

Exact command:

```text
PYTHONPATH=. python3 research/resolve_price_action_outcomes.py
PYTHONPATH=. pytest -q tests/test_research_outcomes.py tests/test_price_action_outcome_audit.py tests/test_price_action_historical_replay.py tests/test_price_action_engulfing_continuation.py tests/test_price_action_sr_retest.py tests/test_evaluation_foundation.py tests/test_kojo_wedge.py tests/test_strategy_backtest_core.py
```

Policy: use the accepted next-open reference price and frozen stop/target;
apply open-price barrier precedence; resolve a single observed barrier at its
barrier price; mark both intrabar barriers ambiguous; classify missing paths
without bridging them; use an observed reopening open when it already crosses
a barrier, but mark the valuation uncertain; and leave trades open at dataset
end because V1 defines no maximum hold. All results are gross reference-price
results without bid/ask, commission, or slippage.

The resolver version is `shared-research-outcome-resolver-v1` and the immutable
policy is `price-action-research-outcome-policy-v1`. It has no operational
database, outbox, broker, or signal-publication dependency.

