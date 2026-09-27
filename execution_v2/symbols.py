"""Explicit canonical-to-broker symbol resolution at the execution boundary.

The execution database stores canonical instruments.  The final MT5 request must use the
broker/account symbol selected by configuration; no suffix convention is inferred here.

Resolution order: the account's explicit V2_BROKER_SYMBOL_MAP_JSON entry, then (when a catalog
lookup is supplied) the canonical ACTIVE MT5 provider mapping in platform.instrument_provider_mapping
(migration 027, probed from the broker). The account map still has to exist - it binds the
execution account - and any symbol it names always wins over the catalog.
"""
from __future__ import annotations

import json
import hashlib
import os
from typing import Any, Callable


class SymbolMappingError(RuntimeError):
    """The configured account has no explicit mapping for a canonical instrument."""


def _mapping_for(*, account_id: str, mode: str) -> dict[str, str]:
    raw = os.getenv("V2_BROKER_SYMBOL_MAP_JSON", "")
    if not raw.strip():
        raise SymbolMappingError("V2_BROKER_SYMBOL_MAP_JSON is required")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SymbolMappingError("V2_BROKER_SYMBOL_MAP_JSON must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise SymbolMappingError("V2_BROKER_SYMBOL_MAP_JSON must be an object")
    selected = payload.get(f"{mode}:{account_id}") or payload.get(account_id)
    if selected is None:
        selected = payload.get("default")
    if not isinstance(selected, dict) or not selected:
        raise SymbolMappingError(f"no broker symbol map configured for {mode}:{account_id}")
    if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in selected.items()):
        raise SymbolMappingError("broker symbol mappings must be non-empty string pairs")
    return dict(selected)


CatalogLookup = Callable[[str], "str | None"]


def resolve_broker_symbol(canonical_symbol: str, *, account_id: str, mode: str,
                          catalog_lookup: CatalogLookup | None = None) -> str:
    mapping = _mapping_for(account_id=account_id, mode=mode)
    if canonical_symbol in mapping:
        return mapping[canonical_symbol]
    provider_symbol = catalog_lookup(canonical_symbol) if catalog_lookup is not None else None
    if isinstance(provider_symbol, str) and provider_symbol:
        return provider_symbol
    raise SymbolMappingError(
        f"no broker symbol mapping for canonical instrument {canonical_symbol!r} "
        f"on {mode}:{account_id}")


def catalog_symbol_lookup(connect_fn: Callable[..., Any]) -> CatalogLookup:
    """Read the ACTIVE MT5 provider symbol for a canonical instrument (read-only, per call).
    A read failure resolves to None, which blocks the execution as an unmapped symbol."""
    def lookup(canonical_symbol: str) -> str | None:
        try:
            with connect_fn(readonly=True) as conn, conn.cursor() as cur:
                cur.execute("""SELECT provider_symbol FROM platform.instrument_provider_mapping
                               WHERE provider = 'MT5' AND canonical_instrument = %s AND state = 'ACTIVE'""",
                            (canonical_symbol,))
                row = cur.fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001 - fail closed: unmapped
            return None
    return lookup


def correlation_comment(attempt_id: str) -> str:
    """Stable MT5-visible token; attempt ids are bounded by the platform id format."""
    token = f"SRV2:{attempt_id}"
    return token[:64]


def canonical_request_text(args: dict[str, Any]) -> str:
    return (f"schema_version={int(args['schema_version'])};action={int(args['action'])};magic={int(args['magic'])};"
            f"symbol={args['symbol']};volume={float(args['volume']):.10f};"
            f"price={float(args['price']):.10f};sl={float(args['sl']):.10f};"
            f"tp={float(args['tp']):.10f};deviation={int(args['deviation'])};"
            f"type={int(args['type'])};type_filling={int(args['type_filling'])};"
            f"type_time={int(args['type_time'])};expiration={int(args['expiration'])};"
            f"comment={args['comment']}")


def canonical_request_fingerprint(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request_text(args).encode("ascii")).hexdigest()
