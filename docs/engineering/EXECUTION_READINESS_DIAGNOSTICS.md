# Execution readiness diagnostics

Run `python3 scripts/audit_execution_readiness.py` from an environment with its
existing approved database configuration. The command enforces a read-only
transaction, prints no DSN/credential values and issues no broker or NATS calls.
Do not copy runtime credentials into new workloads to run this tool.

The output separates known V2 blockers (non-LIVE mode, explicit executor
research-only strategy, offline/disabled configuration) from legacy routing
warnings. Portfolio checks consider every enabled portfolio with an enabled
attached account, not a hardcoded portfolio name. Legacy route/portfolio flags
do not themselves grant V2 authority. Empty blocker lists still require the
normal authority/risk/broker preflight and valid immutable signal provenance.

The signals API now explains intentional research/SHADOW/OFF exclusions from the
immutable signal facts when no execution intent exists. It returns SKIPPED with
evidenceSource=IMMUTABLE_SIGNAL_PROVENANCE; this is a read projection, not a
fabricated execution audit row. Actual intent/risk/broker records take precedence.
Unexplained LIVE signals retain NOT_EVALUATED/NO_EXECUTION_INTENT. Historical
signals with insufficient evidence retain HISTORICAL_AUDIT_UNAVAILABLE.

No migration, execution promotion, allow-list change or research exclusion
removal is included. In particular, old SHADOW/OFF/reconstructed signals never
become executable because a current configuration later changes.
