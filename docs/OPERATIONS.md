# Operations manual

All commands below run from the repository root:

```sh
cd /Users/caleb/mt5-native-bridge
```

## MT5 and bridge

Start the local bridge in a separate terminal:

```sh
python3 bridge.py
curl -fsS http://127.0.0.1:22347/health
```

MT5 must be open, connected to Exness, and have `MT5TradingBridge.mq5`
compiled/attached with WebRequest permission for `http://127.0.0.1:22347`.

## V1 commands

```sh
python3 context_structure_retrace_forward.py status
python3 context_structure_retrace_forward.py health
python3 context_structure_retrace_forward.py report
python3 context_structure_retrace_forward.py report --symbol XAUUSDm
python3 context_structure_retrace_forward.py report --recent 20
python3 context_structure_retrace_forward.py audit-order-isolation
python3 context_structure_retrace_forward.py stop
```

The current frozen runner was started with:

```sh
python3 context_structure_retrace_forward.py start --interval 15 --symbols XAUUSDm BTCUSDm USDJPYm EURUSDm
```

Do not run a second `start`; the PID lock prevents duplicate runners. `stop`
requests a clean shutdown and does not send broker commands.

## Health interpretation

- `ACTIVE`: runner is collecting; verify heartbeat age and bridge health.
- `STALE`: heartbeat or market data is older than its freshness threshold.
- `STOPPED`: no collection is occurring.
- `DATA_GAP_DETECTED`: a completed-candle interval was missed; preserve the
  event and inspect recovery before interpreting sample counts.
- `WAITING_FOR_RETRACE`: setup exists but no paper entry opportunity yet.
- `SETUP_INVALIDATED_BEFORE_ENTRY`: thesis extreme was crossed before entry.
- `ENTRY_OPPORTUNITY`: one normalized price interaction became eligible.
- `OPEN_POSITION`, `TARGET_HIT`, `STOP_HIT`, `BREAKEVEN`: simulated lifecycle
  states only; no broker position is implied.

Never delete state to clear an error. Back it up and diagnose first.

## Other strategy research

The liquidity-displacement wrapper has separate commands documented in
`strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/README.md`. Do not mix its state,
ledger or symbols with V1.

## Phase 7 observer

Phase 7 is optional and passive:

```sh
python3 context_structure_retrace_phase7_observer.py health
python3 context_structure_retrace_phase7_observer.py report
python3 context_structure_retrace_phase7_observer.py audit-order-isolation
```

Start it only after its manifest exists, using `start --interval 15`. It has
separate runtime files documented in `docs/PHASE7_POSITION_MANAGEMENT.md`.
Its report is observation-only; no shadow hypothesis is a V1 decision.

## Signal orchestrator

The independent shadow platform uses:

```sh
python3 signal_orchestrator.py audit-order-isolation
python3 signal_orchestrator.py shadow-start --interval 15
python3 signal_orchestrator.py health
python3 signal_orchestrator.py report
python3 signal_orchestrator.py signals
python3 signal_orchestrator.py decisions
python3 signal_orchestrator.py distribution
python3 signal_orchestrator.py shadow-stop
```

It owns only `runtime/orchestration/`. `MISSING_ACCOUNT_DATA` affects sizing,
not canonicalization or distribution. Live execution is disabled.
# Phase 1 execution consumer (dry run only)

```text
python3 live_execution_consumer.py status
python3 live_execution_consumer.py health
python3 live_execution_consumer.py report
python3 live_execution_consumer.py audit-order-isolation
```

Do not set live mode or add broker-write calls. Runtime state is isolated in
`runtime/execution/`.
