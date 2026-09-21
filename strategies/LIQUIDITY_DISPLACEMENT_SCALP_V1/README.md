# LIQUIDITY_DISPLACEMENT_SCALP_V1

Status: frozen extended forward paper testing. No live order execution.

## What it does

The strategy looks for a sequence on completed candles:

`meaningful liquidity sweep -> reclaim -> displacement -> micro-structure shift -> retracement entry`

The primary V1 configuration is M5 execution with M15 context, M1 disabled,
five completed M5 candles allowed for retracement, a structural stop with ATR,
spread, and broker-distance safety padding, 1.25R target, and a 120-minute
maximum holding time. The frozen source strategy SHA is:

`4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea`

Do not edit parameters or the manifest during a forward test.

## Start and inspect the paper runner

From the repository root:

```sh
cd /Users/caleb/mt5-native-bridge
python3 liquidity_displacement_forward.py start --interval 15
python3 liquidity_displacement_forward.py status
python3 liquidity_displacement_forward.py health
python3 liquidity_displacement_forward.py watch
python3 liquidity_displacement_forward.py trades
python3 liquidity_displacement_forward.py trades --limit 20
python3 liquidity_displacement_forward.py trades --date YYYY-MM-DD
python3 liquidity_displacement_forward.py checkpoint
python3 liquidity_displacement_forward.py daily
python3 liquidity_displacement_forward.py stop
```

`watch` is read-only observability and does not run signal evaluation. The
runner is the only owner of forward processing. MT5 and the read-only bridge
must be available for live collection; temporary read failures are recorded.

## Frozen shallow-entry variants

These are separate persistence namespaces and must be run with the wrapper:

```sh
python3 liquidity_displacement_entry_forward.py usdjpy25 start --interval 15
python3 liquidity_displacement_entry_forward.py xau33 start --interval 15
python3 liquidity_displacement_entry_forward.py btc25 start --interval 15
python3 liquidity_displacement_entry_forward.py ustec_x100m start --interval 15
python3 liquidity_displacement_entry_forward.py ustecm start --interval 15
```

Use the same `status`, `health`, `watch`, `trades`, `checkpoint`, and `stop`
subcommands after the variant name. Never use a variant command to inspect a
different symbol's files; each variant has its own state/event prefix.

## Files

The canonical runner writes `liquidity_displacement_forward_state.json`,
`liquidity_displacement_forward.jsonl`, heartbeat/PID files, daily summaries,
manifest, and summary files. Variant runners use corresponding prefixed files.
The [output README](output/README.md) maps these to historical and forward
artifacts.

## Constraints and known limitations

- Paper/read-only only; no automatic order submission.
- Completed candles only; no chasing unfilled retracements.
- Forward results are not profitability claims and require 25/50/100-fill checkpoints.
- Historical validation and forward paper results must remain separate.
- A restart or gap recovery must be tagged recovered rather than silently mixed with live-observed data.
- Minimum lot feasibility is an account constraint, not a reason to widen risk.

## Future plans

Only after sufficient forward-paper evidence: audit fill realism, spread and
slippage drift, recovered-vs-live samples, and cross-instrument behavior. No
automatic optimization or live execution is planned in this runbook.
