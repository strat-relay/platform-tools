from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class SignalAuthorityMode(str, Enum):
    LEGACY_FILE = "LEGACY_FILE"
    DB_SHADOW = "DB_SHADOW"
    DB_PRIMARY = "DB_PRIMARY"


def _bool(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be explicitly true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class SignalAuthorityFlags:
    db_primary_enabled: bool = False
    jetstream_primary_enabled: bool = False

    @classmethod
    def from_env(cls) -> "SignalAuthorityFlags":
        flags = cls(_bool("SIGNAL_DB_PRIMARY_ENABLED"), _bool("SIGNAL_JETSTREAM_PRIMARY_ENABLED"))
        if flags.jetstream_primary_enabled and not flags.db_primary_enabled:
            raise ValueError("SIGNAL_JETSTREAM_PRIMARY_ENABLED requires SIGNAL_DB_PRIMARY_ENABLED")
        return flags

    @classmethod
    def mode_from_env(cls) -> SignalAuthorityMode:
        flags = cls.from_env()
        configured = os.getenv("SIGNAL_AUTHORITY_MODE")
        if configured is None or not configured.strip():
            if flags.db_primary_enabled:
                if not flags.jetstream_primary_enabled:
                    raise ValueError("DB_PRIMARY requires SIGNAL_JETSTREAM_PRIMARY_ENABLED=true")
                return SignalAuthorityMode.DB_PRIMARY
            return SignalAuthorityMode.LEGACY_FILE
        try:
            mode = SignalAuthorityMode(configured.strip().upper())
        except ValueError as exc:
            raise ValueError("SIGNAL_AUTHORITY_MODE must be LEGACY_FILE, DB_SHADOW, or DB_PRIMARY") from exc
        if mode in {SignalAuthorityMode.LEGACY_FILE, SignalAuthorityMode.DB_SHADOW} and (flags.db_primary_enabled or flags.jetstream_primary_enabled):
            raise ValueError(f"{mode.value} requires both canonical-primary flags to be false")
        if mode is SignalAuthorityMode.DB_PRIMARY and not (flags.db_primary_enabled and flags.jetstream_primary_enabled):
            raise ValueError("DB_PRIMARY requires SIGNAL_DB_PRIMARY_ENABLED and SIGNAL_JETSTREAM_PRIMARY_ENABLED")
        return mode

    def validate(self, *, db_available: bool = False, nats_available: bool = False) -> None:
        """Validate authority prerequisites without coupling DB commits to NATS uptime."""
        if self.db_primary_enabled and not db_available:
            raise RuntimeError("signal DB primary requested without explicit DB availability")
        # A temporary NATS outage must not veto a committed DB authority
        # transition. The transactional outbox remains pending for relay retry.
        # Keep the parameter for callers that report transport health, but do
        # not turn transient transport availability into a persistence gate.
