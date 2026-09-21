"""Pure, read-only import boundary and terminal-state validation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

IMPORTER_VERSION = "phase6-normalized-import-v2"
TERMINAL_POSITION_EVENTS = {"TARGET_HIT", "STOPPED"}
TERMINAL_SETUP_EVENTS = {"INVALIDATED_NO_REENTRY", "SETUP_INVALIDATED_BEFORE_ENTRY"}


@dataclass(frozen=True)
class ImportBoundary:
    cutoff: str
    source_snapshot_times: dict[str, Any]
    source_files: dict[str, str]
    source_hashes: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"cutoff": self.cutoff, "source_snapshot_times": self.source_snapshot_times,
                "source_files": self.source_files, "source_hashes": self.source_hashes}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def event_id(event: dict[str, Any]) -> str:
    return event.get("event_id") or "event-" + hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def build_boundary(compact: dict[str, Any], manifest: dict[str, Any], *, cutoff: str | None,
                   source_files: dict[str, str], source_hashes: dict[str, str]) -> ImportBoundary:
    if not cutoff:
        raise ValueError("Explicit --event-cutoff is required; implicit temporal merging is unsafe")
    if manifest.get("strategy_version") and compact.get("strategy_version") != manifest.get("strategy_version"):
        raise ValueError("strategy version differs between compact state and freeze manifest")
    cutoff_dt = _parse(cutoff)
    compact_time = compact.get("last_successful_read_at")
    if compact_time and _parse(compact_time) < cutoff_dt:
        raise ValueError("event cutoff is newer than compact snapshot boundary")
    return ImportBoundary(cutoff=cutoff, source_snapshot_times={
        "compact_last_successful_read_at": compact_time,
        "compact_last_poll_at": compact.get("last_poll_at"),
        "manifest_freeze_timestamp": manifest.get("freeze_timestamp"),
    }, source_files=source_files, source_hashes=source_hashes)


def validate_state(compact: dict[str, Any], events: Iterable[dict[str, Any]], *, cutoff: str) -> dict[str, Any]:
    cutoff_dt = _parse(cutoff)
    issues: list[dict[str, Any]] = []
    setup_ids = set(compact.get("setups", {}))
    position_ids = set(compact.get("positions", {}))
    opportunity_ids = set()
    for setup_id, setup in (compact.get("setups") or {}).items():
        for opportunity in setup.get("opportunities", []):
            oid = opportunity.get("entry_opportunity_id")
            if oid in opportunity_ids:
                issues.append({"kind": "DUPLICATE_OPPORTUNITY_ID", "id": oid})
            if oid:
                opportunity_ids.add(oid)
            if opportunity.get("setup_id") and opportunity["setup_id"] != setup_id:
                issues.append({"kind": "OPPORTUNITY_SETUP_MISMATCH", "id": oid})
            if opportunity.get("economic_position_id") and opportunity["economic_position_id"] not in position_ids:
                issues.append({"kind": "ORPHAN_POSITION_REFERENCE", "id": opportunity["economic_position_id"]})
    latest_setup: dict[str, dict[str, Any]] = {}
    latest_position: dict[str, dict[str, Any]] = {}
    event_count = 0
    for event in events:
        timestamp = event.get("event_time")
        if timestamp and _parse(timestamp) > cutoff_dt:
            issues.append({"kind": "EVENT_AFTER_CUTOFF", "event_id": event_id(event), "event_time": timestamp})
            continue
        event_count += 1
        if event.get("setup_id"):
            latest_setup[event["setup_id"]] = event
        if event.get("economic_position_id"):
            latest_position[event["economic_position_id"]] = event
    for pid, position in (compact.get("positions") or {}).items():
        event = latest_position.get(pid)
        if not event:
            continue
        if event.get("type") in TERMINAL_POSITION_EVENTS and position.get("status") == "OPEN":
            issues.append({"kind": "POSITION_TERMINALITY_MISMATCH", "id": pid, "event": event.get("type")})
        if event.get("type") not in TERMINAL_POSITION_EVENTS and position.get("status") in TERMINAL_POSITION_EVENTS:
            issues.append({"kind": "POSITION_TERMINAL_EVENT_MISSING", "id": pid})
    for sid, setup in (compact.get("setups") or {}).items():
        event = latest_setup.get(sid)
        if not event:
            continue
        if event.get("type") in TERMINAL_SETUP_EVENTS and setup.get("status") not in TERMINAL_SETUP_EVENTS:
            issues.append({"kind": "SETUP_TERMINALITY_MISMATCH", "id": sid, "event": event.get("type")})
        if event.get("type") == "RETURN_AFTER_SETUP_TARGET_COMPLETED" and not setup.get("target_completed"):
            issues.append({"kind": "TARGET_CONSUMED_MISMATCH", "id": sid})
    return {"safe_to_import": not issues, "cutoff": cutoff, "events_at_or_before_cutoff": event_count,
            "issues": issues, "latest_setup_event_ids": {k: event_id(v) for k, v in latest_setup.items()},
            "latest_position_event_ids": {k: event_id(v) for k, v in latest_position.items()}}
