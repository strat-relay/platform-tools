"""Canonical instrument catalogue and instance membership boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

MEMBERSHIP_SCHEMA_VERSION = "025"
MAPPING_SCHEMA_VERSION = "027"
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
    asset_class: str = "OTHER"


DEFAULT_PROVIDER = "MT5"
ASSET_CLASSES = ("FX", "CRYPTO", "METAL", "INDEX", "COMMODITY", "EQUITY", "OTHER")


class InstrumentMembershipRepository:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._read_versioned(MEMBERSHIP_SCHEMA_VERSION, sql, params)

    def _read_versioned(self, version: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (version,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable(f"canonical PostgreSQL schema {version} is required")
                    cur.execute(sql, params)
                    return [_row_dict(cur, row) for row in cur.fetchall()]
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable("canonical PostgreSQL instrument source unavailable") from exc

    def list_catalog(self, provider: str = DEFAULT_PROVIDER) -> list[CatalogInstrument]:
        """The instrument catalog: canonical instruments with an ACTIVE mapping for `provider`
        (platform.instrument_provider_mapping, migration 027). The database is the only source;
        nothing is guessed from provider symbol conventions."""
        rows = self._read_versioned(MAPPING_SCHEMA_VERSION, """SELECT canonical_instrument, provider,
            provider_symbol, coalesce(display_name, canonical_instrument) AS display_name, asset_class
            FROM platform.instrument_provider_mapping
            WHERE provider = %s AND state = 'ACTIVE'
            ORDER BY asset_class, canonical_instrument""", (provider,))
        return [CatalogInstrument(r["canonical_instrument"], r["provider"], r["provider_symbol"],
                                  r["display_name"], r["asset_class"]) for r in rows]

    def save_mapping(self, canonical: str, provider_symbol: str, asset_class: str, *,
                     display_name: str | None = None, state: str = "ACTIVE",
                     provider: str = DEFAULT_PROVIDER, actor: str) -> dict[str, Any]:
        """Idempotent upsert of one provider mapping; the revision moves only on a real change."""
        canonical, provider_symbol = canonical.strip().upper(), provider_symbol.strip()
        asset_class, state = asset_class.strip().upper(), state.strip().upper()
        if not canonical or not provider_symbol or asset_class not in ASSET_CLASSES or state not in {"ACTIVE", "DISABLED"}:
            raise ValueError("canonical, provider_symbol, a known asset_class and ACTIVE|DISABLED state are required")
        try:
            with self._connect(readonly=False) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (MAPPING_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 027 is required")
                    cur.execute("""INSERT INTO platform.instrument_provider_mapping
                        (provider, canonical_instrument, provider_symbol, asset_class, display_name, state, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (provider, canonical_instrument) DO UPDATE SET
                            provider_symbol = EXCLUDED.provider_symbol, asset_class = EXCLUDED.asset_class,
                            display_name = EXCLUDED.display_name, state = EXCLUDED.state,
                            revision = platform.instrument_provider_mapping.revision + 1,
                            updated_at = now(), updated_by = EXCLUDED.updated_by
                        WHERE (platform.instrument_provider_mapping.provider_symbol, platform.instrument_provider_mapping.asset_class,
                               platform.instrument_provider_mapping.display_name, platform.instrument_provider_mapping.state)
                              IS DISTINCT FROM (EXCLUDED.provider_symbol, EXCLUDED.asset_class,
                                                EXCLUDED.display_name, EXCLUDED.state)
                        RETURNING canonical_instrument, provider, provider_symbol, asset_class, display_name,
                                  state, revision""",
                                (provider, canonical, provider_symbol, asset_class, display_name, state, actor))
                    row = cur.fetchone()
                    changed = row is not None
                    if row is None:
                        cur.execute("""SELECT canonical_instrument, provider, provider_symbol, asset_class, display_name,
                                              state, revision FROM platform.instrument_provider_mapping
                                       WHERE provider = %s AND canonical_instrument = %s""", (provider, canonical))
                        row = cur.fetchone()
                    result = {**_row_dict(cur, row), "changed": changed}
                conn.commit()
                return result
        except (CanonicalSourceUnavailable, ValueError):
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable(f"canonical PostgreSQL instrument mapping write unavailable: {exc}") from exc

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
