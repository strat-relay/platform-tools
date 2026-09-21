from __future__ import annotations

import os
from dataclasses import dataclass


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

    def validate(self, *, db_available: bool = False, nats_available: bool = False) -> None:
        if self.db_primary_enabled and not db_available:
            raise RuntimeError("signal DB primary requested without explicit DB availability")
        if self.jetstream_primary_enabled and not nats_available:
            raise RuntimeError("signal JetStream primary requested without explicit NATS availability")
