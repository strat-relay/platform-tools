from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .config import PostgresConfig
from .db import apply_migrations, connect, transaction
from .phase6 import Phase6Store, canonical_hash
from .preflight import build_boundary, event_id, validate_state
from .working_set import build_working_set, make_boundary, validate_working_set

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def import_phase6(conn, *, compact_path: Path, manifest_path: Path, events_path: Path,
                  ledger_path: Path | None = None, snapshots_path: Path | None = None,
                  batch_id: str | None = None, event_cutoff: str | None = None,
                  mode: str = "historical", boundary_id: str | None = None,
                  captured_at: str | None = None) -> dict[str, object]:
    if mode not in {"historical", "working-set"}:
        raise ValueError("mode must be historical or working-set")
    compact = read_json(compact_path)
    manifest = read_json(manifest_path)
    hashes = {"state": file_hash(compact_path), "manifest": file_hash(manifest_path), "events": file_hash(events_path)}
    research_paths = [p for p in (ledger_path, snapshots_path) if p] if mode == "historical" else []
    if research_paths:
        hashes["research"] = hashlib.sha256("".join(file_hash(p) for p in research_paths).encode()).hexdigest()
    batch_id = batch_id or f"phase6-{hashes['state'][:16]}"
    events = []
    with events_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    boundary = build_boundary(compact, manifest, cutoff=event_cutoff,
                              source_files={"compact": str(compact_path), "manifest": str(manifest_path), "events": str(events_path)},
                              source_hashes=hashes)
    if not event_cutoff:
        raise ValueError("Explicit --event-cutoff is required for every import mode")
    if mode == "working-set":
        validation = validate_working_set(compact, events, cutoff=event_cutoff)
        selected = build_working_set(compact)
        boundary = make_boundary(compact, captured_at=captured_at or compact.get("last_successful_read_at") or event_cutoff,
                                 event_cutoff=event_cutoff,
                                 source_files={"compact": str(compact_path), "manifest": str(manifest_path), "events": str(events_path)},
                                 source_hashes=hashes,
                                 archive_refs={"full_state": "preserved-read-only", "phase2_ledger": "excluded",
                                               "phase2_snapshots": "excluded"})
        if boundary_id and boundary_id != boundary.boundary_id:
            raise ValueError("boundary_id does not match the captured source hashes/cursors")
    else:
        boundary = build_boundary(compact, manifest, cutoff=event_cutoff,
                                  source_files={"compact": str(compact_path), "manifest": str(manifest_path), "events": str(events_path)},
                                  source_hashes=hashes)
        validation = validate_state(compact, events, cutoff=event_cutoff)
        selected = {"setups": compact.get("setups") or {}, "symbols": compact.get("symbols") or {}}
    if not validation["safe_to_import"]:
        raise ValueError(json.dumps({"safe_to_import": False, "validation": validation}, sort_keys=True))
    store = Phase6Store(conn)
    strategy_id = str(compact.get("strategy_version") or manifest.get("strategy_version") or "CONTEXT_STRUCTURE_RETRACE_V1")
    counts = {"setups": 0, "opportunities": 0, "positions": 0, "events": 0, "observations": 0, "links": 0, "unattached_events": 0}
    with transaction(conn):
        store.batch(batch_id, hashes, {"schema": compact.get("schema"), "strategy_id": strategy_id})
        ids = store.identity(compact, manifest, hashes)
        if mode == "working-set":
            store.working_set_boundary(boundary.as_dict(), validation)
        store.record_import_batch(boundary.as_dict(), batch_id=batch_id, strategy_version_id=ids["strategy_version_id"],
                                  configuration_version_id=ids["configuration_version_id"], freeze_manifest_id=ids["freeze_manifest_id"], validation=validation)
        store.normalized_runner(compact, ids, batch_id=batch_id)
        store.runner(compact, manifest, hashes)
        for setup_id, setup in selected["setups"].items():
            setup = dict(setup, _identity=dict(ids, batch_id=batch_id))
            store.setup(setup, strategy_id)
            counts["setups"] += 1
            snapshot = setup.get("context_snapshot")
            if snapshot:
                oid = store.observation(snapshot, kind="COMPACT_CONTEXT_SNAPSHOT", source_file=str(compact_path), line=0)
                store.link_observation(setup["setup_id"], oid, "CONTEXT_SNAPSHOT")
                counts["observations"] += 1; counts["links"] += 1
            opportunity_source = setup.get("opportunities", [])
            if mode == "working-set":
                opportunity_source = [item for item in opportunity_source
                                      if item.get("status") not in {"TARGET_HIT", "STOPPED", "CLOSED", "EXPIRED"}]
            for opportunity in opportunity_source:
                opportunity = dict(opportunity, setup_id=setup_id, symbol=setup.get("symbol"), direction=setup.get("direction"), pattern=setup.get("pattern"), _identity=dict(ids, batch_id=batch_id))
                store.opportunity(opportunity, legacy_phase6=(mode == "historical"))
                counts["opportunities"] += 1
                if opportunity.get("economic_position_id"):
                    counts["positions"] += 1
        for line_number, event in enumerate(events, 1) if mode == "historical" else []:
            eid = event_id(event)
            store.lifecycle(event, eid, batch_id=batch_id)
            if not event.get("setup_id") and not event.get("economic_position_id"):
                store.unattached_event(event, eid, source_file=str(events_path), line=line_number, batch_id=batch_id)
                counts["unattached_events"] += 1
            counts["events"] += 1
        for path, kind in ((ledger_path, "PHASE2_LEDGER"), (snapshots_path, "PHASE2_SNAPSHOT")) if mode == "historical" else []:
            if not path:
                continue
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip(): continue
                    observation = json.loads(line)
                    store.observation(observation, kind=kind, source_file=str(path), line=line_number,
                                      observation_id=observation.get("event_id") or observation.get("snapshot_id"))
                    counts["observations"] += 1
        store.complete_import_batch(batch_id, attempted=counts, inserted=counts, deduplicated={}, rejected={}, validation=validation)
    return {"batch_id": batch_id, "strategy_id": strategy_id, "mode": mode.upper().replace("-", "_"),
            "boundary": boundary.as_dict(), "validation": validation, "hashes": hashes, "counts": counts}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an explicitly bounded Phase 6 state into PostgreSQL")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--compact", type=Path, default=ROOT / "context_structure_retrace_forward_state_compact.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "context_structure_retrace_forward_manifest.json")
    parser.add_argument("--events", type=Path, default=ROOT / "context_structure_retrace_forward.jsonl")
    parser.add_argument("--ledger", type=Path, default=ROOT / "context_structure_retrace_phase2_ledger.jsonl")
    parser.add_argument("--snapshots", type=Path, default=ROOT / "context_structure_retrace_phase2_snapshots.jsonl")
    parser.add_argument("--mode", choices=("working-set", "historical"), default="working-set")
    parser.add_argument("--boundary-id", help="Expected working-set boundary id from the captured artifacts")
    parser.add_argument("--captured-at", help="UTC timestamp of the read-only artifact capture")
    parser.add_argument("--event-cutoff", required=True, help="Explicit UTC event boundary; import rejects unsafe temporal merges")
    args = parser.parse_args()
    with connect(PostgresConfig.from_env()) as conn:
        if args.migrate: print(json.dumps({"migrations": apply_migrations(conn)}))
        print(json.dumps(import_phase6(conn, compact_path=args.compact, manifest_path=args.manifest, events_path=args.events, ledger_path=args.ledger, snapshots_path=args.snapshots, event_cutoff=args.event_cutoff, mode=args.mode, boundary_id=args.boundary_id, captured_at=args.captured_at), indent=2))


if __name__ == "__main__": main()
