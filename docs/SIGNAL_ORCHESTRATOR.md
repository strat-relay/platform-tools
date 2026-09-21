# Signal Orchestrator

The signal orchestrator is a separate SHADOW-only infrastructure layer. It
observes strategy-owned ledgers, translates opportunities into immutable
`StrategySignal` objects, and fans them independently into audit, shadow
account-sizing, and an internal distribution queue.

Strategies do not know account balances, lot sizes, subscribers, or delivery
channels. The first enabled adapter is `CONTEXT_STRUCTURE_RETRACE_V1`; other
strategies remain disabled until their adapters are separately audited.

The orchestrator freeze, source hash, configuration hash, and canonical schema
hash are stored in `runtime/orchestration/manifest.json`. Its prospective
boundary is independent of Phase 6 and Phase 7. Pre-freeze strategy records
are not counted as prospective orchestrator signals.

## Commands

```sh
python3 signal_orchestrator.py freeze
python3 signal_orchestrator.py shadow-start --interval 15
python3 signal_orchestrator.py health
python3 signal_orchestrator.py status
python3 signal_orchestrator.py report
python3 signal_orchestrator.py signals
python3 signal_orchestrator.py decisions
python3 signal_orchestrator.py distribution
python3 signal_orchestrator.py audit-order-isolation
python3 signal_orchestrator.py shadow-stop
```

Only read-only bridge methods are reachable: account info, symbol info,
quotes, and rates. Live execution is disabled and no broker-write route is
implemented.
