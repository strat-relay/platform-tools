"""Canonical instrument catalogue and instance membership boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

MEMBERSHIP_SCHEMA_VERSION = "025"
DEFAULT_INSTANCE_BY_STRATEGY = {"CONTEXT_STRUCTURE_RETRACE_V1": "phase6"}


class MembershipConflict(Exception):
    pass


class UnsupportedInstrument(Exception):
    pass


@dataclass(frozen=True)
class CatalogInstrument:
    canonical_instrument: str
    provider: str
    provider_symbol: str
    display_name: str


def catalog_from_config(config: dict[str, Any]) -> list[CatalogInstrument]:
    """Return only explicitly mapped canonical identities.

    Unknown provider symbols are intentionally not guessed into canonical
    identities. A later provider adapter can supply the same shape without
    changing StrategyInstance membership.
    """
    mappings = config.get("symbol_mappings") or {}
    if not isinstance(mappings, dict):
        return []
    result = []
    for canonical, provider_symbol in sorted(mappings.items()):
        if isinstance(canonical, str) and isinstance(provider_symbol, str) and provider_symbol:
            result.append(CatalogInstrument(canonical.upper(), "MT5", provider_symbol,
                                            {"XAUUSD": "Gold", "BTCUSD": "Bitcoin",
                                             "ETHUSD": "Ethereum", "NAS100": "Nasdaq 100"}.get(canonical.upper(), canonical.upper())))
    return result


class InstrumentMembershipRepository:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (MEMBERSHIP_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 025 is required")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL instrument membership source unavailable") from exc

    def list_membership(self, strategy_id: str, instance_id: str) -> list[dict[str, Any]]:
        return self._read("""SELECT strategy_instance_id, strategy_id, canonical_instrument,
            state, revision, created_at, updated_at, updated_by
            FROM strategy.instrument_membership
            WHERE strategy_id = %s AND strategy_instance_id = %s
            ORDER BY canonical_instrument""", (strategy_id, instance_id))

    def save_membership(self, strategy_id: str, instance_id: str, canonical: str,
                        state: str, expected_revision: int | None, actor: str) -> dict[str, Any]:
        canonical = canonical.strip().upper()
        state = state.upper()
        if not canonical or state not in {"ACTIVE", "DISABLED"}:
            raise ValueError("canonicalInstrument and state ACTIVE|DISABLED are required")
        try:
            with self._connect(readonly=False) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (MEMBERSHIP_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 025 is required")
                    cur.execute("""SELECT strategy_instance_id, strategy_id, canonical_instrument,
                        state, revision, created_at, updated_at, updated_by
                        FROM strategy.instrument_membership
                        WHERE strategy_instance_id = %s AND canonical_instrument = %s
                        FOR UPDATE""", (instance_id, canonical))
                    row = cur.fetchone()
                    if row is None:
                        if expected_revision not in (None, 0):
                            raise MembershipConflict("membership does not exist")
                        cur.execute("""INSERT INTO strategy.instrument_membership
                            (strategy_instance_id, strategy_id, canonical_instrument, state, updated_by)
                            VALUES (%s,%s,%s,%s,%s)
                            RETURNING strategy_instance_id, strategy_id, canonical_instrument, state,
                                      revision, created_at, updated_at, updated_by""",
                                    (instance_id, strategy_id, canonical, state, actor))
                    else:
                        current = _row_dict(cur, row)
                        if expected_revision is not None and int(current["revision"]) != expected_revision:
                            raise MembershipConflict(f"revision conflict: expected {expected_revision}, current {current['revision']}")
                        cur.execute("""UPDATE strategy.instrument_membership
                            SET state = %s, revision = revision + 1, updated_at = now(), updated_by = %s
                            WHERE strategy_instance_id = %s AND canonical_instrument = %s
                            RETURNING strategy_instance_id, strategy_id, canonical_instrument, state,
                                      revision, created_at, updated_at, updated_by""",
                                    (state, actor, instance_id, canonical))
                    result = _row_dict(cur, cur.fetchone())
                conn.commit()
                return result
        except (MembershipConflict, UnsupportedInstrument, CanonicalSourceUnavailable):
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL instrument membership write unavailable") from exc
