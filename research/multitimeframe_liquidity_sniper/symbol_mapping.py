"""Research-only canonical to broker-symbol resolution.

Strategy code uses canonical symbols (for example ``USDJPY``).  The connected
research account may expose either the canonical name or a broker suffix such
as ``USDJPYm``.  Resolution is performed from read-only ``mt5_symbol_info``
responses and the selected broker name is kept at the data boundary.
"""
from __future__ import annotations

from collections.abc import Mapping


def candidate_symbols(canonical: str) -> tuple[str, ...]:
    """Return deterministic candidates, preferring the unsuffixed identity."""
    base = canonical[:-1] if canonical.endswith("m") else canonical
    return (base, f"{base}m")


def is_available(info: Mapping[str, object] | None, candidate: str) -> bool:
    """Accept only an explicit symbol-info response without an error."""
    if not isinstance(info, Mapping) or info.get("error"):
        return False
    return info.get("symbol") == candidate


def resolve_from_symbol_info(
    canonical: str, responses: Mapping[str, Mapping[str, object] | None]
) -> tuple[str, Mapping[str, object]]:
    """Resolve a canonical symbol from already-fetched read-only responses."""
    for candidate in candidate_symbols(canonical):
        info = responses.get(candidate)
        if is_available(info, candidate):
            return candidate, info  # type: ignore[return-value]
    tried = ", ".join(candidate_symbols(canonical))
    raise LookupError(f"no research broker symbol available for {canonical}; tried {tried}")


def resolve_from_read_probes(
    canonical: str,
    symbol_info: Mapping[str, Mapping[str, object] | None],
    quotes: Mapping[str, Mapping[str, object] | None],
) -> tuple[str, Mapping[str, object]]:
    """Prefer an alias with both metadata and a usable read-only quote."""
    for candidate in candidate_symbols(canonical):
        info = symbol_info.get(candidate)
        quote = quotes.get(candidate)
        if isinstance(quote, Mapping) and not quote.get("error"):
            if is_available(info, candidate):
                return candidate, info  # type: ignore[return-value]
            # Some MT5 builds expose a live quote while SymbolSelect-backed
            # metadata is unavailable. Keep the broker identity and the
            # successful quote probe rather than choosing a dead alias.
            return candidate, {"symbol": candidate, "quote_probe": "ok", "metadata_probe": info}
    return resolve_from_symbol_info(canonical, symbol_info)
