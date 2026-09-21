# Agent handoff status

Last checked: 2026-09-16 UTC. This file is documentation only and may become
stale; the CLI health/status commands are authoritative.

## Active paper runners

- `CONTEXT_STRUCTURE_RETRACE_V1`: ACTIVE, PID 49978 at last check, 15-second
  poll, XAUUSDm/BTCUSDm/USDJPYm/EURUSDm.
- Other liquidity-displacement runner states are historical/operational
  artifacts and must be verified individually before use.

Verify before acting because runner state changes:

```sh
python3 liquidity_displacement_forward.py status
python3 liquidity_displacement_entry_forward.py usdjpy25 status
python3 liquidity_displacement_entry_forward.py xau33 status
python3 liquidity_displacement_entry_forward.py btc25 status
python3 liquidity_displacement_entry_forward.py ustec_x100m status
python3 liquidity_displacement_entry_forward.py ustecm status
```

## Frozen constraints

- Paper/read-only only.
- No live order submission.
- Do not modify the frozen V1 manifest or source strategy SHA:
  `4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea`.
- Do not mix symbols or reuse another strategy's state/event files.
- `watch`, `trades`, `health`, `status`, `daily`, and `checkpoint` are observational commands.

## Research branches

- MICRO_SCALP: archived; do not continue optimizing.
- SIMPLE_SR_CANDLE_V1: historical research only; no runner.
- CONTEXT_STRUCTURE_RETRACE_V1: descriptive Phase 2 ledger; no trade selection.
- BASELINE: legacy reference only.

Detailed handoffs are in `strategies/*/STATUS.md`, `docs/HANDOFF.md`, and the
root recovery runbook.
