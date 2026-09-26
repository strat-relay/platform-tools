"""SIGNAL_DATA_PLANE_MODE: which hot path accepts an orchestrator signal.

This is a SEPARATE axis from `migration.flags.SignalAuthorityMode` (which governs whether
PostgreSQL or the legacy file is authoritative for signal STATE/query history). This flag
governs which path accepts a signal onto the real-time backbone:

    DB_FIRST    (default) - unchanged: orchestrator -> PostgreSQL transaction (ingest_signal)
                 -> transactional outbox -> OutboxRelay -> JetStream. Acceptance = PG commit.
    NATS_FIRST            - orchestrator -> JetStream direct publish -> PUBACK is acceptance;
                 PostgreSQL is populated asynchronously by SignalPersistenceProjector.

The two modes are mutually exclusive PRODUCERS for one signal (never dual-write the same
signal through both hot paths at once); see docs/nats_first_data_plane/03_MIGRATION_PATH.md
for the transition design. Same fail-closed style as `SignalAuthorityFlags`: explicit
true/false only, default DB_FIRST, NATS_FIRST requires the caller to confirm NATS
availability before selecting it - this module never silently falls back.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class SignalDataPlaneMode(str, Enum):
    DB_FIRST = "DB_FIRST"
    NATS_FIRST = "NATS_FIRST"


def _bool(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be explicitly true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class SignalDataPlaneFlags:
    mode: SignalDataPlaneMode = SignalDataPlaneMode.DB_FIRST
    nats_first_enabled: bool = False

    @classmethod
    def from_env(cls) -> "SignalDataPlaneFlags":
        configured = os.getenv("SIGNAL_DATA_PLANE_MODE")
        nats_first_enabled = _bool("SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED")
        if configured is None or not configured.strip():
            mode = SignalDataPlaneMode.NATS_FIRST if nats_first_enabled else SignalDataPlaneMode.DB_FIRST
        else:
            try:
                mode = SignalDataPlaneMode(configured.strip().upper())
            except ValueError as exc:
                raise ValueError("SIGNAL_DATA_PLANE_MODE must be DB_FIRST or NATS_FIRST") from exc
        if mode is SignalDataPlaneMode.NATS_FIRST and not nats_first_enabled:
            raise ValueError("NATS_FIRST requires SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED=true (explicit double confirmation)")
        if mode is SignalDataPlaneMode.DB_FIRST and nats_first_enabled:
            raise ValueError("SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED=true requires SIGNAL_DATA_PLANE_MODE=NATS_FIRST (or unset)")
        return cls(mode, nats_first_enabled)

    def validate(self, *, nats_available: bool = False) -> None:
        """Fail closed: NATS_FIRST may only be selected when the caller has
        independently confirmed transport availability (mirrors
        SignalAuthorityFlags.validate). A transient NATS outage while already
        running NATS_FIRST is a publish-time failure (RealtimePublicationFailed),
        not silently downgraded to DB_FIRST here or anywhere in this package.
        """
        if self.mode is SignalDataPlaneMode.NATS_FIRST and not nats_available:
            raise RuntimeError("SIGNAL_DATA_PLANE_MODE=NATS_FIRST requested without explicit NATS availability")
