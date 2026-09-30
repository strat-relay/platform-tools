"""Selection and preflight for the explicitly bounded CURRENT-WORKING-SET.

This module intentionally reads only the compact operational projection.  It
does not load the legacy full state, historical event ledger, or Phase 2
observation archives.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

TERMINAL_POSITION_STATUSES = {"TARGET_HIT", "STOPPED", "AMBIGUOUS_INTRABAR", "CLOSED", "EXPIRED"}
TERMINAL_SETUP_STATUSES = {"INVALIDATED_NO_REENTRY", "NO_RETRACE", "EXPIRED", "CLOSED"}


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class WorkingSetBoundary:
    boundary_id: str
    captured_at: str
    event_cutoff: str
    source_files: dict[str, str]
    source_hashes: dict[str, str]
    symbol_cursors: dict[str, Any]
    archive_refs: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "WORKING_SET",
            "boundary_id": self.boundary_id,
            "captured_at": self.captured_at,
            "event_cutoff": self.event_cutoff,
            "source_files": self.source_files,
            "source_hashes": self.source_hashes,
            "symbol_cursors": self.symbol_cursors,
            "archive_refs": self.archive_refs,
        }


def build_working_set(compact: dict[str, Any]) -> dict[str, Any]:
    """Select entities capable of affecting continuation after the boundary."""
    setups: dict[str, dict[str, Any]] = {}
    opportunities: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    context_refs: list[dict[str, Any]] = []
    for setup_id, raw in (compact.get("setups") or {}).items():
        setup = dict(raw)
        setup["setup_id"] = setup.get("setup_id") or setup_id
        setup_opportunities = [dict(item, setup_id=setup_id) for item in setup.get("opportunities", [])]
        active_opportunities = [item for item in setup_opportunities
                                if item.get("status") not in TERMINAL_POSITION_STATUSES]
        # RETURN_AFTER_SETUP_TARGET_COMPLETED remains in the set because it is
        # an explicit continuation state; terminal invalidation/no-retrace does
        # not have future strategy work.
        if setup.get("status") in TERMINAL_SETUP_STATUSES and not active_opportunities:
            continue
        if setup.get("status") not in TERMINAL_SETUP_STATUSES or active_opportunities:
            setups[setup_id] = setup
            opportunities.extend(active_opportunities)
            positions.extend(item for item in active_opportunities if item.get("economic_position_id"))
            if setup.get("context_snapshot"):
                context_refs.append({"setup_id": setup_id, "context_snapshot": setup["context_snapshot"]})
    symbols = {symbol: dict(value) for symbol, value in (compact.get("symbols") or {}).items()}
    return {"setups": setups, "opportunities": opportunities, "positions": positions,
            "symbols": symbols, "context_references": context_refs,
            "runner": {key: compact.get(key) for key in
                        ("schema", "strategy_version", "last_successful_read_at", "prospective_boundary", "runner_status", "kill_switch")}}


def validate_working_set(compact: dict[str, Any], events: Iterable[dict[str, Any]], *, cutoff: str) -> dict[str, Any]:
    """Strictly validate active entities; archived terminal drift is reported."""
    boundary_time = _time(cutoff)
    selected = build_working_set(compact)
    issues: list[dict[str, Any]] = []
    archived_inconsistencies: list[dict[str, Any]] = []
    active_positions = {item.get("economic_position_id"): item for item in selected["positions"]}
    latest: dict[str, dict[str, Any]] = {}
    events_at_cutoff = 0
    for event in events:
        event_time = _time(event.get("event_time"))
        if event_time and boundary_time and event_time > boundary_time:
            continue
        events_at_cutoff += 1
        entity = event.get("economic_position_id") or event.get("setup_id")
        if entity:
            latest[str(entity)] = event
    for position_id, position in active_positions.items():
        prior = latest.get(str(position_id))
        if prior and prior.get("type") in TERMINAL_POSITION_STATUSES:
            issues.append({"kind": "ACTIVE_POSITION_HAS_TERMINAL_EVENT", "id": position_id,
                           "event": prior.get("type")})
    for entity, event in latest.items():
        if entity not in active_positions and event.get("type") in TERMINAL_POSITION_STATUSES:
            archived_inconsistencies.append({"id": entity, "event": event.get("type")})
    return {"mode": "WORKING_SET", "safe_to_import": not issues, "cutoff": cutoff,
            "events_at_or_before_cutoff": events_at_cutoff, "issues": issues,
            "archived_historical_inconsistencies": archived_inconsistencies,
            "counts": {"setups": len(selected["setups"]), "opportunities": len(selected["opportunities"]),
                       "positions": len(selected["positions"]), "symbol_progress": len(selected["symbols"]),
                       "context_references": len(selected["context_references"])}}


def make_boundary(compact: dict[str, Any], *, captured_at: str, event_cutoff: str,
                  source_files: dict[str, str], source_hashes: dict[str, str],
                  archive_refs: dict[str, str]) -> WorkingSetBoundary:
    payload = {"captured_at": captured_at, "event_cutoff": event_cutoff,
               "source_hashes": source_hashes, "symbol_cursors": compact.get("symbols", {})}
    boundary_id = "ws-" + hashlib.sha256(_canonical(payload).encode()).hexdigest()[:24]
    return WorkingSetBoundary(boundary_id, captured_at, event_cutoff, source_files, source_hashes,
                              compact.get("symbols", {}), archive_refs)
