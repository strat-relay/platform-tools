from __future__ import annotations

from typing import Any, Mapping


def migration_status(*, modes: Any, phase: str, gate_status: Mapping[str, Any], reconciliation: Mapping[str, Any], unresolved_sites: int, health: Mapping[str, Any], schema_version: str | None = None) -> dict[str, Any]:
    return {
        "state_authority_mode": modes.state_authority.value,
        "event_transport_mode": modes.event_transport.value,
        "legacy_projection": modes.legacy_projection.value,
        "migration_phase": phase,
        "gate_status": dict(gate_status),
        "reconciliation_status": reconciliation.get("summary", {}),
        "mismatch_count": sum(v for k, v in reconciliation.get("summary", {}).items() if k != "MATCH"),
        "unresolved_file_sites": unresolved_sites,
        "outbox": {k: health.get(k) for k in ("outbox_unpublished", "outbox_attempts", "outbox_oldest_age_seconds")},
        "inbox": {k: health.get(k) for k in ("inbox_duplicate_hits", "inbox_failures")},
        "jetstream": {k: health.get(k) for k in ("consumer_lag", "consumer_redeliveries")},
        "postgres_schema_version": schema_version,
    }
