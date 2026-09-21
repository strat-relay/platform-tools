# Live Execution Consumer — Phase 1

Phase 1 is a production-shaped boundary locked to `DRY_RUN`. It reads only
prospective canonical signals and `EXECUTABLE` sizing decisions from the
Signal Orchestrator. Historical `PRE_ORCHESTRATOR_REFERENCE` records and
non-executable sizing decisions cannot create execution intents.

```text
StrategySignal -> sizing decision -> ExecutionIntent -> validation -> DRY_RUN decision
       |                                      |
       +-> independent distribution          +-> BrokerReadAdapter only
```

The broker execution adapter is schema-only. No submit, cancel, close, or
protection mutation is reachable in this phase. Zero equity is observed and
rejected as `REJECTED_ZERO_EQUITY`; it is never replaced with a fixture value.

Commands:

```text
python3 live_execution_consumer.py start --interval 15
python3 live_execution_consumer.py status
python3 live_execution_consumer.py health
python3 live_execution_consumer.py report
python3 live_execution_consumer.py decisions
python3 live_execution_consumer.py audit-order-isolation
```

Runtime files are isolated under `runtime/execution/`. The consumer does not
write Phase 6, Phase 7, or orchestrator ledgers.
