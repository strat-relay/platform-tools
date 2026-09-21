"""Version-1 canonical request serialization shared with the MT5 EA."""
from __future__ import annotations

import hashlib
from typing import Any

REQUIRED_FIELDS = (
    "schema_version", "action", "magic", "symbol", "volume", "price", "sl",
    "tp", "deviation", "type", "type_filling", "type_time", "expiration", "comment",
)


def canonical_request_text(request: dict[str, Any]) -> str:
    return (f"schema_version={int(request['schema_version'])};action={int(request['action'])};magic={int(request['magic'])};"
            f"symbol={request['symbol']};volume={float(request['volume']):.10f};"
            f"price={float(request['price']):.10f};sl={float(request['sl']):.10f};"
            f"tp={float(request['tp']):.10f};deviation={int(request['deviation'])};"
            f"type={int(request['type'])};type_filling={int(request['type_filling'])};"
            f"type_time={int(request['type_time'])};expiration={int(request['expiration'])};"
            f"comment={request['comment']}")


def canonical_request_fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request_text(request).encode("ascii")).hexdigest()


def wire_request(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return None
    result = dict(request)
    result.setdefault("schema_version", 1)
    return result
