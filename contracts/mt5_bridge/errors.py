"""Typed transport errors for the MT5 bridge boundary."""
from __future__ import annotations


class BridgeTransportError(RuntimeError):
    """Connection, HTTP, or malformed-response failure."""


class BridgeReadTimeout(TimeoutError):
    """A read timed out before the bridge acknowledged the request."""

    def __init__(self, *, endpoint: str, request_id: str, operation: str,
                 symbol: str | None, elapsed_ms: float):
        self.endpoint = endpoint
        self.request_id = request_id
        self.operation = operation
        self.symbol = symbol
        self.elapsed_ms = elapsed_ms
        super().__init__(f"{operation.upper()}_TIMEOUT:endpoint={endpoint} "
                         f"request_id={request_id} elapsed_ms={elapsed_ms:.1f}"
                         + (f" symbol={symbol}" if symbol else ""))


class BridgeToolError(BridgeTransportError):
    """The bridge accepted the RPC but returned an MCP tool error."""


class BridgePermissionError(BridgeToolError):
    """The bridge rejected an authorization compatibility header."""


class BridgeAckUncertain(BridgeTransportError):
    """A write may have reached MT5 but its acknowledgement was lost."""


def classify_tool_error(detail: str) -> BridgeToolError:
    if detail.startswith(("BROKER_WRITE_REQUIRES_", "SMOKE_TEST_", "EXPLICIT_")):
        return BridgePermissionError(detail)
    return BridgeToolError(detail)
