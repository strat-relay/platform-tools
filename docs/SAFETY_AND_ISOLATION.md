# Safety and isolation

Current status: PAPER / READ-ONLY ONLY.

`CONTEXT_STRUCTURE_RETRACE_V1` can call only:

- `mt5_symbol_info`
- `mt5_quote`
- `mt5_rates`

The bridge and EA also expose live-capable primitives including
`mt5_market_order`, `mt5_pending_order`, `mt5_cancel_pending_order`,
`mt5_close_position`, and `mt5_trailing_stop`. Those tools are not reachable
through the V1 runner's `bridge_read` allow-list. The order-isolation audit and
tests verify this boundary statically.

This is distinct from MT5's technical capability: MT5 and the EA can submit
orders when explicitly configured, but V1 does not. A future local execution
service would require a separate frozen-intent, risk, authorization,
submission and reconciliation design. It is not implemented here.

Never treat paper fills as broker positions. Never place `confirm=true` live
requests from this project during V1 research.
