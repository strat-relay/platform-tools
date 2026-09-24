"""Resolve which TradeManagerVersion a new ManagedTrade binds to, exactly once, at creation.

The resolver is consulted in the ManagedTrade creation transaction and never again (A6 06
section 5, A7 04 section 5): the result is written into immutable columns on `managed_trade`.
A later change to `legacy_stream_binding` affects only trades opened afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .ids import binding_id as _binding_id


class TmVersionUnavailable(RuntimeError):
    """TM-NONE-1 (or whatever the default resolves to) is not registered/FROZEN. Fail closed:
    no ManagedTrade may be created (A6 07 section 4; A7 04 section 4 row "TM-NONE-1 not
    registered")."""


@dataclass(frozen=True)
class BindingResolution:
    tm_version_id: str
    binding_id: str
    binding_hash: str
    resolution: str  # LEGACY_STATIC | DEFAULT_TM_NONE


class StreamBindingResolver(Protocol):
    def resolve(self, conn: Any, *, strategy_id: str, strategy_instance_id: str | None,
               instrument: str, decision_time: str) -> BindingResolution | None:
        """Return a resolution, or None to let the caller fall back to the next resolver."""
        ...


def _load_frozen_version(conn: Any, tm_version_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trade_management.trade_manager_version WHERE tm_version_id=%s",
                    (tm_version_id,))
        row = cur.fetchone()
    if row is None or row[0] != "FROZEN":
        raise TmVersionUnavailable(f"{tm_version_id} is not a registered FROZEN TradeManagerVersion")


class LegacyStaticResolver:
    """Reads `trade_management.legacy_stream_binding` for the row valid at `decision_time`,
    most-specific-first (instance+instrument > instrument > strategy-wide), most recent
    `valid_from` not after `decision_time`. Returns None (never raises) when no row matches, so
    the caller can fall back to `DefaultTmNoneResolver` - "binding table empty/no row ->
    DEFAULT_TM_NONE binding; creation never blocks on missing bindings" (A7 10)."""

    def resolve(self, conn: Any, *, strategy_id: str, strategy_instance_id: str | None,
               instrument: str, decision_time: str) -> BindingResolution | None:
        with conn.cursor() as cur:
            cur.execute("""SELECT binding_id, tm_version_id, binding_hash, strategy_instance_id, instrument
                          FROM trade_management.legacy_stream_binding
                          WHERE strategy_id=%s AND valid_from <= %s
                            AND (strategy_instance_id IS NULL OR strategy_instance_id=%s)
                            AND (instrument IS NULL OR instrument=%s)
                          ORDER BY valid_from DESC""",
                        (strategy_id, decision_time, strategy_instance_id, instrument))
            rows = cur.fetchall()
        if not rows:
            return None
        # Most specific match: (instance AND instrument) > instrument-only > instance-only > strategy-wide.
        def specificity(row: Any) -> int:
            _, _, _, row_instance, row_instrument = row
            return (row_instance is not None) + (row_instrument is not None)
        best = max(rows, key=specificity)
        binding_id, tm_version_id, binding_hash, _, _ = best
        _load_frozen_version(conn, tm_version_id)
        return BindingResolution(tm_version_id=tm_version_id, binding_id=binding_id,
                                 binding_hash=binding_hash, resolution="LEGACY_STATIC")


class DefaultTmNoneResolver:
    """Fallback: always TM-NONE-1. Fail closed (`TmVersionUnavailable`) if TM-NONE-1 is not
    registered/FROZEN - creation never proceeds by guessing a version."""

    def __init__(self, *, tm_version_id: str) -> None:
        self.tm_version_id = tm_version_id

    def resolve(self, conn: Any, *, strategy_id: str, strategy_instance_id: str | None,
               instrument: str, decision_time: str) -> BindingResolution:
        _load_frozen_version(conn, self.tm_version_id)
        binding_id = _binding_id(strategy_id=strategy_id, strategy_instance_id=strategy_instance_id,
                                 instrument=instrument, tm_version_id=self.tm_version_id,
                                 resolution="DEFAULT_TM_NONE", valid_from=decision_time)
        binding_hash = binding_id  # synthetic binding: the id already is a content hash
        return BindingResolution(tm_version_id=self.tm_version_id, binding_id=binding_id,
                                 binding_hash=binding_hash, resolution="DEFAULT_TM_NONE")


class ChainedResolver:
    """Tries each resolver in order; the first non-None result wins. The conventional
    composition is `ChainedResolver([LegacyStaticResolver(), DefaultTmNoneResolver(...)])`."""

    def __init__(self, resolvers: list[StreamBindingResolver]) -> None:
        if not resolvers:
            raise ValueError("ChainedResolver requires at least one resolver")
        self.resolvers = resolvers

    def resolve(self, conn: Any, *, strategy_id: str, strategy_instance_id: str | None,
               instrument: str, decision_time: str) -> BindingResolution:
        for resolver in self.resolvers:
            result = resolver.resolve(conn, strategy_id=strategy_id, strategy_instance_id=strategy_instance_id,
                                      instrument=instrument, decision_time=decision_time)
            if result is not None:
                return result
        raise TmVersionUnavailable("no resolver in the chain produced a binding")
