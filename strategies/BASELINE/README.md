# BASELINE

Status: legacy paper-engine/backtest reference. Keep separate from the named
strategy branches.

## Run / inspect

```sh
cd /Users/caleb/mt5-native-bridge
python3 paper_runner.py --once --audit paper_audit.jsonl
python3 paper_runner.py --enable-paper --interval 15 --audit paper_audit.jsonl
python3 -m unittest -v test_paper_engine.py
```

The paper engine is disabled by default and uses the read-only bridge. It does
not authorize live order submission. Historical files such as
`backtest_results.json`, `backtest_trades.csv`, and `backtest_summary.md` are
legacy reference outputs.

## Constraints

Do not use BASELINE files as state for other strategies. Keep symbol,
configuration, and output namespaces explicit to avoid cross-instrument
contamination.
