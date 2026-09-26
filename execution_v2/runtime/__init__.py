"""Production runtime wiring for execution_v2 (mission section 15): a single supervised process
(`python -m execution_v2.runtime`) that consumes canonical EntrySignals and drives
`execution_v2.worker.ExecutionWorker`.

Deployed with `EXECUTION_AUTHORITY_MODE=DISABLED` by default (`migration.flags.
execution_authority_mode_from_env()`, the same production invariant reader P4.2's runtime already
uses) - `RuntimeConfig.from_env()` reads that mode explicitly and it is the ONLY thing that can
ever make `execution_authority_enabled=True` reach `ExecutionWorker.process_signal`; nothing here
infers permission to trade from an EntrySignal's existence, a PostgreSQL lease, or bridge
reachability (mission section 2). Even with the mode ENABLED, the shipped
`orchestration/config/v2_execution_risk_policy.json` defaults to `enabled: false`, so every signal
is independently blocked at the risk layer until an operator supplies an approved configuration -
two separate fail-closed gates, not one.

This module has never been deployed/activated; see docs/v2_execution/README.md.
"""
