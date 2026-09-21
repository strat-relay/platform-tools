"""Platform-side client for the versioned MT5 bridge protocol.

This package contains transport and protocol contracts only.  Trading policy,
risk, ownership, and execution authorization remain in platform callers.
"""

from .client import BridgeEndpoint, CallContext, Mt5ExecutionClient, Mt5ReadClient
from .errors import (BridgeAckUncertain, BridgePermissionError, BridgeToolError,
                     BridgeTransportError, BridgeReadTimeout)

__all__ = [
    "BridgeEndpoint", "CallContext", "Mt5ReadClient", "Mt5ExecutionClient",
    "BridgeAckUncertain", "BridgePermissionError", "BridgeToolError",
    "BridgeTransportError", "BridgeReadTimeout",
]
