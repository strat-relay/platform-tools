# Strategy registry

| Strategy | Status | Scope | Runner |
|---|---|---|---|
| `CONTEXT_STRUCTURE_RETRACE_V1` | frozen prospective paper | M15 execution, M5 lower, H1/H4 higher; XAUUSDm/BTCUSDm/USDJPYm/EURUSDm | `context_structure_retrace_forward.py` |
| `LIQUIDITY_DISPLACEMENT_SCALP_V1` | frozen extended paper variants | symbol-specific cohorts are separate namespaces; M5/M15 | `liquidity_displacement_forward.py` and wrapper |
| `MICRO_SCALP` | archived historical research | historical only | `micro_scalp*.py` |
| `SIMPLE_SR_CANDLE_V1` | historical research | historical only | `simple_sr_candle*.py` |
| `BASELINE` | legacy reference | historical/reference | `strategies/BASELINE` |

## Context V1 paths

- source: `context_structure_retrace_forward.py`, `context_structure_retrace/`
- manifest: `context_structure_retrace_forward_manifest.json`
- state/ledger/heartbeat/PID: corresponding root-prefixed files
- report: `python3 context_structure_retrace_forward.py report`
- freeze: `CONTEXT_STRUCTURE_RETRACE_V1`

## Liquidity-displacement paths

The canonical and variant state/ledger/manifest files use
`liquidity_displacement_*` names. They are independent of Context V1 and must
not be combined. See `strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/README.md`.

The historical evidence includes positive and negative regimes, but no result
is a live-profitability claim. Minimum-lot constraints, overlap normalization,
spread and forward sample size remain limitations.
