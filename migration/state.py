from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class MigrationState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    SHADOW_WRITE = "SHADOW_WRITE"
    RECONCILING = "RECONCILING"
    DB_PRIMARY = "DB_PRIMARY"
    EVENT_SHADOW = "EVENT_SHADOW"
    EVENT_PRIMARY = "EVENT_PRIMARY"
    LEGACY_READ_DISABLED = "LEGACY_READ_DISABLED"
    LEGACY_WRITE_DISABLED = "LEGACY_WRITE_DISABLED"
    RETIRED = "RETIRED"


_TRANSITIONS = {
    MigrationState.NOT_STARTED: {MigrationState.SHADOW_WRITE, MigrationState.RECONCILING},
    MigrationState.SHADOW_WRITE: {MigrationState.RECONCILING, MigrationState.NOT_STARTED},
    MigrationState.RECONCILING: {MigrationState.DB_PRIMARY, MigrationState.EVENT_SHADOW, MigrationState.SHADOW_WRITE},
    MigrationState.DB_PRIMARY: {MigrationState.EVENT_SHADOW, MigrationState.LEGACY_READ_DISABLED},
    MigrationState.EVENT_SHADOW: {MigrationState.EVENT_PRIMARY, MigrationState.DB_PRIMARY},
    MigrationState.EVENT_PRIMARY: {MigrationState.LEGACY_READ_DISABLED},
    MigrationState.LEGACY_READ_DISABLED: {MigrationState.LEGACY_WRITE_DISABLED},
    MigrationState.LEGACY_WRITE_DISABLED: {MigrationState.RETIRED},
    MigrationState.RETIRED: set(),
}


def transition(current: MigrationState, target: MigrationState) -> MigrationState:
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"invalid migration transition: {current.value} -> {target.value}")
    return target


@dataclass
class MigrationStateStore:
    """Test/dormant state store; production authority belongs in PostgreSQL."""

    path: Path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, domain: str, state: MigrationState, evidence: dict[str, Any]) -> None:
        payload = self.read()
        previous = MigrationState(payload.get(domain, MigrationState.NOT_STARTED.value))
        transition(previous, state) if previous != state else state
        payload[domain] = {"state": state.value, "evidence": evidence}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":")); fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
