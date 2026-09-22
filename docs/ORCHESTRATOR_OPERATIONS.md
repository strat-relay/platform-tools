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

## Authoritative signals with execution disabled

`PRIMARY` is an authoritative signal-orchestration mode, not a synonym for
`REAL_EXECUTION`. It requires all of the following independent settings:

```sh
ORCHESTRATOR_MODE=PRIMARY
SIGNAL_AUTHORITY_MODE=DB_PRIMARY
SIGNAL_DB_PRIMARY_ENABLED=true
SIGNAL_JETSTREAM_PRIMARY_ENABLED=true
EXECUTION_AUTHORITY_MODE=DISABLED
```

It also requires an explicit canonical PostgreSQL target, `NATS_URL`, and the
persisted `SIGNAL_CUTOFF_ID` / `SIGNAL_CUTOFF_UTC`. Startup verifies the
PostgreSQL schema and publisher configuration before polling. `PRIMARY` uses
the canonical publisher and DB_FIRST outbox path; it does not instantiate the
MT5 read provider, read or write `signals.jsonl`, authorize Trade Manager
proposals, or create actionable execution routes. Per-account execution
routes are recorded as `DISABLED` dispositions. Customer distribution and the
Trade Manager are not activated by this mode.

The prepared canonical runtime ConfigMap contains these values for a future
authorized cutover only; this code change does not apply it to Kubernetes.
To start or stop PRIMARY after the later cutover procedure:

```sh
python3 signal_orchestrator.py startup-audit --mode PRIMARY
python3 signal_orchestrator.py primary-start --interval 15
python3 signal_orchestrator.py primary-stop
```

The old `REAL_EXECUTION` startup remains a distinct legacy/future,
execution-capable orchestration mode. It still requires explicit
`EXECUTION_AUTHORITY_MODE=ENABLED`, an armed real consumer, a verified
dedicated execution transport, and its existing live/resume gates. It is not
renamed to PRIMARY. It performs broker account/symbol/quote reads and an
`order_check`, records `REAL_EXECUTION_DISPOSITION` routes, can enqueue
post-live signals for the separate real execution consumer, and authorizes
pending Trade Manager proposals. The orchestrator itself still does not
submit broker orders; the separately managed consumer is the write boundary.
P5 OD-06 fencing remains a prerequisite before execution authority can be
enabled.

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
