from __future__ import annotations

from typing import Any, Protocol


class BrokerReadAdapter(Protocol):
    def account_snapshot(self, account_id: str) -> dict[str, Any]: ...
    def symbol_metadata(self, symbol: str) -> dict[str, Any]: ...
    def quote(self, symbol: str) -> dict[str, Any]: ...
    def open_positions(self, account_id: str) -> list[dict[str, Any]]: ...
    def pending_orders(self, account_id: str) -> list[dict[str, Any]]: ...


class BrokerExecutionAdapter(Protocol):
    def submit_order(self, *args, **kwargs): ...
    def cancel_order(self, *args, **kwargs): ...
    def modify_order(self, *args, **kwargs): ...
    def close_position(self, *args, **kwargs): ...


class DryRunReadAdapter:
    """Default adapter: no broker writes and no invented market/account data."""
    def __init__(self, provider=None): self.provider = provider

    def account_snapshot(self, account_id):
        if not self.provider: raise RuntimeError("account read adapter unavailable")
        return self.provider.account_snapshot(account_id)

    def symbol_metadata(self, symbol):
        if not self.provider: raise RuntimeError("symbol read adapter unavailable")
        return self.provider.symbol_metadata(symbol)

    def quote(self, symbol):
        if not self.provider: raise RuntimeError("quote read adapter unavailable")
        return self.provider._read("mt5_quote", {"symbol": symbol})

    def open_positions(self, account_id):
        return []

    def pending_orders(self, account_id):
        return []
