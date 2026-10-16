# Validation record

## Scope

44 new offline research compositions; six preserved existing strategy bindings;
50 definitions across ten families. No registration/deployment/execution changes.
The branch additionally carries the earlier Price Action reversal timeout/coverage
fix so its existing-strategy regression tests run against the corrected code.

## Executed checks

```bash
PYTHONPATH=. python -m pytest -q \
 tests/test_strategy_library.py \
 tests/test_price_action_reversal_v1.py \
 tests/test_price_action_sr_retest.py \
 tests/test_price_action_engulfing_continuation.py \
 tests/test_price_action_historical_replay.py \
 tests/test_research_outcomes.py \
 tests/test_evaluation_foundation.py \
 tests/test_strategy_backtest_core.py
```

Result: **253 passed**. This is a focused/regression run, not the full repository
suite. New checks cover all 44 definitions' LONG/SHORT full lifecycles, as-of data
bounds, prefix determinism, entry independence from subsequent candle H/L/C,
restart equivalence, parameter/data/artifact binding, canonical provenance,
input atomicity, duplicate/revision handling, session/DST and range boundaries,
indicator numerical vectors, known-at pivots/FVG, and shared TP/SL resolution.

```bash
python -m compileall -q strategy_backtest/library research/strategy_library.py research/strategy_library_fixtures.py tests/test_strategy_library.py
git diff --check
PYTHONPATH=. python -m research.strategy_library catalog --output docs/strategy_library/catalog.json
PYTHONPATH=. python -m research.strategy_library synthetic --output docs/strategy_library/synthetic_report.json
```

All passed. Generated JSON is serialized with nonfinite values rejected.
The synthetic replay has 88 setup-to-entry scenarios, all counts reconciled,
all with accepted reconstructed entries. Each includes separate constructed TP
and SL barrier cases through the existing shared resolver. These are fixtures,
not a sample whose win rate or expectancy has financial meaning.

## Data readiness and limits

Historical market replay for the new definitions is NOT RUN. No common audited
H1/M15/H4/D1 source dataset is bundled/available in this checkout for these
experiments. Existing historical evidence and legacy IDs remain separate.

Needed per experiment: immutable raw completed OHLC, canonical/broker mappings,
positive tick size, source and timestamp semantics, warm-up, coverage/gap audit,
volume semantics for VWAP hypotheses, and availability/revision evidence where
available. Audited aggregation from lower timeframes is possible but is not
silently performed by the strategy runtime.

A complete-week policy without a pinned closure calendar fails closed on missing
bars, including normal FX weekend gaps. Any calendar-aware extension requires a
new explicit policy/version, not a hidden gap waiver. Session proxies and ICT/SMC
models are the documented mechanical interpretations, not universal definitions.

No automatic qualification, net profitability, broker-fill fidelity, account
performance, production readiness, or execution eligibility is claimed.
