"""Production runtime wiring for P4.2 (architecture/p4-2-runtime).

`trade_management/` (the parent package) is pure domain logic and imports nothing broker- or
transport-related - that invariant is unchanged and still enforced by
`tests/test_trade_management_isolation.py`, which only scans `trade_management/*.py`. This
subpackage is the one place allowed to hold the production adapters that logic needs to actually
run: a real JetStream client (`nats`), a real read-only market-data client
(`contracts.mt5_bridge.Mt5ReadClient`, restricted to `READ_TOOLS` at the transport layer, pointed
at the existing 22347 research listener - never 22348), and a real PostgreSQL connection
(`postgres.db.connect`). It still imports nothing from `execution`, `live_execution_consumer`,
`trade_manager`, `orchestration`, `control_api`, or `context_structure_retrace_phase7_observer`
(enforced by `tests/test_trade_management_runtime_isolation.py`).

Entry point: `python -m trade_management.runtime` (`__main__.py`). Nothing in this package runs
on import; `main()` must be invoked explicitly. See `docs/p4_2_managed_trade/03_RUNTIME.md`.
"""
