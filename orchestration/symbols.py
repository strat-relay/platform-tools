"""Canonical ↔ broker symbol translation.

All symbol mapping between the platform's canonical representation and
broker-specific suffixes lives here.  Adapters must not inline their own
suffix logic — add the mapping here instead so that a broker migration or
new suffix convention only requires a single-file change.

Current broker (Exness): canonical symbol + "m" suffix (e.g. XAUUSD → XAUUSDm).
Exceptions are recorded in SUFFIX_OVERRIDES.
"""
from __future__ import annotations

# Per-instrument overrides for the canonical-to-broker mapping.
# Key: canonical symbol (uppercase).  Value: exact broker symbol string.
# Example: {"BTCUSD": "BTCUSDm", "XAUUSD": "XAUUSDm"}
SUFFIX_OVERRIDES: dict[str, str] = {}

# The broker suffix applied when no override exists.
_DEFAULT_SUFFIX = "m"


def canonical_to_broker_hint(canonical: str, suffix: str = _DEFAULT_SUFFIX) -> str:
    """Return the broker symbol hint for a canonical instrument name.

    Checks SUFFIX_OVERRIDES first; falls back to appending `suffix`.
    The result is a hint, not a guarantee — the execution boundary
    resolves the authoritative mapping from broker metadata.
    """
    if not isinstance(canonical, str):
        return canonical
    return SUFFIX_OVERRIDES.get(canonical.upper(), canonical + suffix)


def broker_to_canonical(broker_symbol: str, suffix: str = _DEFAULT_SUFFIX) -> str:
    """Strip the broker suffix to recover the canonical symbol name.

    Checks SUFFIX_OVERRIDES (reversed) first; falls back to stripping `suffix`.
    """
    if not isinstance(broker_symbol, str):
        return broker_symbol
    # Reverse-lookup: find a canonical key whose override matches.
    for canonical, override in SUFFIX_OVERRIDES.items():
        if override == broker_symbol:
            return canonical
    if broker_symbol.endswith(suffix):
        return broker_symbol[: -len(suffix)]
    return broker_symbol
