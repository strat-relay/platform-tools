# Testing

Run from the repository root:

```sh
python3 -m unittest discover -v
```

The complete suite was rerun on 2026-09-16 and passed 96 tests with 0
failures. Evidence is stored under `artifacts/test-results/`.

Coverage groups include:

- **UNIT:** indicators, schemas, patterns and geometry helpers.
- **CAUSALITY / NO-LOOKAHEAD:** completed/forming HTF bars, replay windows,
  causal pattern and S/R availability.
- **GEOMETRY:** signed target direction, opposing structure, no `abs()` reward
  masking and invalid-target handling.
- **LIFECYCLE / REENTRY:** invalidation, hovering, departure/return and target
  completion semantics.
- **ECONOMIC NORMALIZATION:** one economic position per opportunity and
  overlap/re-entry accounting.
- **TWO-LEG ACCOUNTING:** total 1R split into 0.5R + 0.5R and BE transition.
- **STATE / RESTART / GAP RECOVERY:** atomic persistence, locks, heartbeat and
  recovery tags.
- **CROSS-INSTRUMENT / TIMEFRAME:** supplied symbols and configurable context.
- **REPORTING:** read-only report/trades/watch behavior.
- **PAPER/LIVE ISOLATION:** allow-list and order-path audit.
- **REGRESSION:** frozen hashes and historical edge cases.

Use `python3 context_structure_retrace_forward.py audit-order-isolation` before
any prospective start. Never interpret a passing historical test as proof of
live profitability.
