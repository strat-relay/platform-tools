"""NATS/JetStream-first real-time signal data plane (prototype).

This package is entirely additive to the existing DB-first path
(`orchestration/canonical_signal_publisher.py`, `infrastructure/messaging/outbox_relay.py`).
It reuses `migration.signal.canonical_signal`/`ingest_signal`, the V1.1 `Evaluation` model, and
the shared `infrastructure.messaging` envelope/publisher/consumer primitives wherever possible.

Nothing here is wired into `signal_orchestrator.py` or any other live production entry point.
`SIGNAL_DATA_PLANE_MODE` (see `dataplane.mode`) defaults to `DB_FIRST`; the code in this package
only runs when a caller explicitly constructs and drives it (tests, the benchmark script, or a
future, separately-approved integration).
"""
