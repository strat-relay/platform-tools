# Platform post-extraction test baseline

The applicable offline platform suite is green. Bridge implementation
characterization remains in `mt5-native-bridge`.

## Boundary exclusions

- EA source assertions in `test_execution_consumer.py` are skipped here;
  they belong to the bridge repository.
- `mt5_read_once.py` and Wine/EA path assertions are not platform tests;
  Stage 0 checks now validate the `contracts/mt5_bridge` client boundary.
- Research tests requiring `external_cases.jsonl` or `/tmp` market snapshots
  remain skipped when those external fixtures are absent.

These are extraction test assumptions or environment dependencies, not
production regressions. The platform tests use temporary synthetic runtime
state and synthetic account context where runtime state is required.

## Verification

- Platform boundary and offline PostgreSQL checks: 14 passed.
- Applicable platform/orchestration/Trade Manager suite: 204 passed, 5
  bridge-owned tests skipped.
- Strategy and research suite: 177 passed, 4 boundary/fixture tests skipped.
- Bridge characterization suite: 71 passed in `mt5-native-bridge`.
- A3 architecture map generator: passed with the audited classification
  manifest; no Strategy Studio implementation was added.
