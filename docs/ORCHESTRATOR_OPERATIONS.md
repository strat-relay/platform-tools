# Orchestrator Operations

The orchestrator owns only `runtime/orchestration/`. It does not write Phase
6 or Phase 7 files, and it never submits broker orders. The historical
`SIGNAL_ORCHESTRATOR_SHADOW_V1` version name remains in the ledger schema for
compatibility; startup mode is now explicit.

The normal startup is:

```sh
python3 signal_orchestrator.py audit-order-isolation
python3 signal_orchestrator.py shadow-start --interval 15
python3 signal_orchestrator.py health
python3 signal_orchestrator.py report
```

Stop only the orchestrator with `shadow-stop`; do not stop the Phase 6 or
Phase 7 processes as part of this operation.

For the REAL execution architecture, the orchestrator still performs
canonical signal creation, routing, account reads, and sizing disposition.
The separate `live_execution_consumer.py` owns execution intents and the
broker execution boundary. Use the explicit REAL gate only after the account,
live-output, consumer-arm, and resume-cutoff authorities agree:

```sh
python3 signal_orchestrator.py startup-audit --mode REAL_EXECUTION
python3 signal_orchestrator.py real-start --interval 15
python3 signal_orchestrator.py health
python3 signal_orchestrator.py report
```

`real-start` refuses SHADOW configuration and contradictory runtime state.
Use `real-stop` to stop only the REAL orchestrator. The consumer's resume
cutoff remains the authority that excludes pre-resume and gap-recovery signals.
