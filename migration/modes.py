from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class StateAuthorityMode(str, Enum):
    LEGACY_FILE = "LEGACY_FILE"
    DB_SHADOW = "DB_SHADOW"
    DB_PRIMARY = "DB_PRIMARY"


class EventTransportMode(str, Enum):
    LEGACY_FILE = "LEGACY_FILE"
    JETSTREAM_SHADOW = "JETSTREAM_SHADOW"
    JETSTREAM_PRIMARY = "JETSTREAM_PRIMARY"


class LegacyProjectionMode(str, Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class RuntimeModes:
    state_authority: StateAuthorityMode
    event_transport: EventTransportMode
    legacy_projection: LegacyProjectionMode = LegacyProjectionMode.ENABLED

    def validate(self) -> None:
        if self.state_authority == StateAuthorityMode.LEGACY_FILE and self.event_transport != EventTransportMode.LEGACY_FILE:
            raise ValueError("LEGACY_FILE authority requires LEGACY_FILE event transport")
        if self.state_authority == StateAuthorityMode.DB_SHADOW and self.event_transport == EventTransportMode.JETSTREAM_PRIMARY:
            raise ValueError("DB_SHADOW cannot use JETSTREAM_PRIMARY")
        if self.state_authority == StateAuthorityMode.DB_PRIMARY and self.event_transport == EventTransportMode.LEGACY_FILE and self.legacy_projection == LegacyProjectionMode.DISABLED:
            raise ValueError("DB_PRIMARY with disabled projection cannot use legacy file transport")

    def require_dependencies(self, *, database_available: Callable[[], bool] | None = None,
                             jetstream_available: Callable[[], bool] | None = None) -> None:
        self.validate()
        if self.state_authority == StateAuthorityMode.DB_PRIMARY and database_available and not database_available():
            raise RuntimeError("DB_PRIMARY requires PostgreSQL; refusing file fallback")
        if self.event_transport == EventTransportMode.JETSTREAM_PRIMARY and jetstream_available and not jetstream_available():
            raise RuntimeError("JETSTREAM_PRIMARY requires JetStream; refusing file fallback")

