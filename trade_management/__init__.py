"""P4.2: ManagedTrade, the broker-independent Trade Observation Service, and TM-NONE.

Scope (architecture/p4-2-managed-trade): consume the existing canonical EntrySignal
(P2-A1, `strategy.entry_signals`, subject `signal.entry.created.v1`) to create a
`ManagedTrade`; observe open ManagedTrades through a `MarketDataProvider` only (no broker
position, order, execution result, or Phase 7 dependency); run the first Trade Manager
version, `TM-NONE`, which persists an auditable `HOLD` for every eligible observation and
takes no action.

Nothing in this package is wired into a production entrypoint. `open_consumer.py`,
`observation.py`'s service loop, and `tm_none.py`'s consumer are durable-consumer
*implementations*, not activated processes - starting them is a separate, later,
explicitly-authorized step (see docs/p4_2_managed_trade/README.md).

This package imports nothing from `execution`, `live_execution_consumer`, `trade_manager`
(the legacy package), `contracts.mt5_bridge`, `orchestration`, `control_api`, or
`context_structure_retrace_phase7_observer` - enforced by
tests/test_trade_management_isolation.py. BROKER_WRITES=0.
"""
