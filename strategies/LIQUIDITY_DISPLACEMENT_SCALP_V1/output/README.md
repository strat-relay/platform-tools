# LIQUIDITY_DISPLACEMENT_SCALP_V1 outputs

This directory is the documentation landing point for V1 outputs. The current
runner still writes compatibility files at the repository root.

## Historical research

- `backtest_results.json`, `backtest_trades.csv`, `backtest_summary.md`
- `liquidity_displacement_results.json`, `liquidity_displacement_trades.csv`, `liquidity_displacement_summary.md`
- `liquidity_displacement_oos_results.json` and `liquidity_displacement_oos_summary.md`
- `liquidity_displacement_multi_results*.json` and matching summaries for other symbols
- `liquidity_displacement_entry_depth_results.json` and summary for 25/33/50% entry-depth research

## Forward paper

- `liquidity_displacement_forward.jsonl` — append-only lifecycle events
- `liquidity_displacement_forward_state.json` — resumable paper state
- `liquidity_displacement_forward_manifest.json` — frozen configuration/hash
- `liquidity_displacement_forward_summary.md` — human summary
- `liquidity_displacement_daily.jsonl` — idempotent daily health records
- `liquidity_displacement_forward.heartbeat.json` and `.pid` — runner health/ownership

Variants use prefixes such as `liquidity_displacement_usdjpy25_*`,
`xau33_*`, `btc25_*`, `ustec_x100m_*`, and `ustecm_*`.

Do not edit state, event logs, or manifests manually while a runner is active.
