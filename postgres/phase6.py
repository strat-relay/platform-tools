from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


class Phase6Store:
    """DB-API adapter for the inactive Phase 6 PostgreSQL representation."""

    def __init__(self, conn):
        self.conn = conn

    def _execute(self, sql: str, params: tuple[Any, ...] = ()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def batch(self, batch_id: str, hashes: dict[str, str], metadata: dict[str, Any] | None = None) -> None:
        self._execute("""INSERT INTO platform.migration_batches
            (batch_id, source_state_sha256, source_manifest_sha256, source_events_sha256, source_research_sha256, metadata)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (batch_id) DO NOTHING""",
            (batch_id, hashes.get("state"), hashes.get("manifest"), hashes.get("events"), hashes.get("research"), _json(metadata)))

    def working_set_boundary(self, boundary: dict[str, Any], validation: dict[str, Any]) -> None:
        self._execute("""INSERT INTO platform.migration_boundaries
            (boundary_id,mode,captured_at,event_cutoff,source_files,source_hashes,symbol_cursors,archive_refs,preflight_result)
            VALUES (%s,'WORKING_SET',%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (boundary_id) DO UPDATE SET preflight_result=EXCLUDED.preflight_result""",
            (boundary["boundary_id"], boundary["captured_at"], boundary["event_cutoff"],
             _json(boundary.get("source_files")), _json(boundary.get("source_hashes")),
             _json(boundary.get("symbol_cursors")), _json(boundary.get("archive_refs")), _json(validation)))

    def record_import_batch(self, boundary: dict[str, Any], *, batch_id: str, strategy_version_id: str,
                            configuration_version_id: str, freeze_manifest_id: str, validation: dict[str, Any]) -> None:
        self._execute("""INSERT INTO platform.import_batches
            (batch_id,import_boundary,source_snapshot_times,source_files,source_hashes,strategy_version_id,
             configuration_version_id,freeze_manifest_id,validation_result,importer_version)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (batch_id) DO UPDATE SET validation_result=EXCLUDED.validation_result""",
            (batch_id, boundary["cutoff"], _json(boundary["source_snapshot_times"]), _json(boundary["source_files"]),
            _json(boundary["source_hashes"]), strategy_version_id, configuration_version_id,
             freeze_manifest_id, _json(validation), "phase6-normalized-import-v2"))

    def complete_import_batch(self, batch_id: str, *, attempted: dict[str, Any], inserted: dict[str, Any],
                              deduplicated: dict[str, Any], rejected: dict[str, Any], validation: dict[str, Any]) -> None:
        self._execute("""UPDATE platform.import_batches SET completed_at=now(), counts_attempted=%s::jsonb,
            counts_inserted=%s::jsonb, counts_deduplicated=%s::jsonb, counts_rejected=%s::jsonb,
            validation_result=%s::jsonb WHERE batch_id=%s""",
            (_json(attempted), _json(inserted), _json(deduplicated), _json(rejected), _json(validation), batch_id))

    def identity(self, state: dict[str, Any], manifest: dict[str, Any], hashes: dict[str, str]) -> dict[str, str]:
        strategy_version = str(state.get("strategy_version") or manifest.get("strategy_version"))
        schema_version = str(state.get("schema") or manifest.get("schema_version") or "unknown")
        strategy_id = strategy_version
        config_hash = str(manifest.get("configuration_hash") or "unknown")
        config_id = config_hash
        freeze_hash = hashes.get("manifest") or canonical_hash(manifest)
        freeze_id = freeze_hash
        self._execute("""INSERT INTO platform.strategy_versions(strategy_version_id,strategy_version,schema_version,source_hash)
            VALUES (%s,%s,%s,%s) ON CONFLICT (strategy_version_id) DO NOTHING""", (strategy_id, strategy_version, schema_version, hashes.get("manifest")))
        self._execute("""INSERT INTO platform.configuration_versions(configuration_version_id,configuration_hash,configuration)
            VALUES (%s,%s,%s::jsonb) ON CONFLICT (configuration_version_id) DO NOTHING""", (config_id, config_hash, _json(manifest.get("configuration"))))
        self._execute("""INSERT INTO platform.freeze_manifests(freeze_manifest_id,strategy_version_id,configuration_version_id,freeze_timestamp,manifest_hash,manifest)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (freeze_manifest_id) DO NOTHING""", (freeze_id, strategy_id, config_id, manifest.get("freeze_timestamp"), freeze_hash, _json(manifest)))
        return {"strategy_version_id": strategy_id, "configuration_version_id": config_id, "freeze_manifest_id": freeze_id}

    def normalized_runner(self, state: dict[str, Any], ids: dict[str, str], *, batch_id: str) -> None:
        runner_id = str(state.get("strategy_version") or "CONTEXT_STRUCTURE_RETRACE_V1")
        self._execute("""INSERT INTO strategy.runner_state
            (runner_id,strategy_version_id,configuration_version_id,freeze_manifest_id,status,started_at,last_processed_at,state_version,
             prospective_boundary,kill_switch,poll_interval_seconds,source_state_sha256,import_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
            ON CONFLICT (runner_id) DO UPDATE SET status=EXCLUDED.status,last_processed_at=EXCLUDED.last_processed_at,
             state_version=EXCLUDED.state_version,source_state_sha256=EXCLUDED.source_state_sha256,import_batch_id=EXCLUDED.import_batch_id,updated_at=now()""",
            (runner_id,ids["strategy_version_id"],ids["configuration_version_id"],ids["freeze_manifest_id"],state.get("runner_status"),
             state.get("created_at"),state.get("last_successful_read_at"),state.get("schema"),_json(state.get("prospective_boundary")),
             state.get("kill_switch"),state.get("poll_interval_seconds"),state.get("source_state_sha256"),batch_id))

    def runner(self, state: dict[str, Any], manifest: dict[str, Any], source_hashes: dict[str, str]) -> None:
        strategy_id = str(state.get("strategy_version") or manifest.get("strategy_version") or "CONTEXT_STRUCTURE_RETRACE_V1")
        self._execute("""INSERT INTO strategy.phase6_runners
            (strategy_id,strategy_version,schema_version,manifest_ref,freeze_timestamp,configuration_hash,phase2_representation_hash,
             runner_status,prospective_boundary,kill_switch,created_at,last_poll_at,last_successful_read_at,poll_interval_seconds,
             source_state_sha256,source_manifest_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (strategy_id) DO UPDATE SET strategy_version=EXCLUDED.strategy_version,schema_version=EXCLUDED.schema_version,
             runner_status=EXCLUDED.runner_status,prospective_boundary=EXCLUDED.prospective_boundary,kill_switch=EXCLUDED.kill_switch,
             last_poll_at=EXCLUDED.last_poll_at,last_successful_read_at=EXCLUDED.last_successful_read_at,
             source_state_sha256=EXCLUDED.source_state_sha256,source_manifest_sha256=EXCLUDED.source_manifest_sha256,updated_at=now()""",
            (strategy_id, state.get("strategy_version"), state.get("schema"), str(manifest.get("path") or "phase6-manifest"),
             manifest.get("freeze_timestamp"), manifest.get("configuration_hash"), manifest.get("phase2_representation_hash"),
             state.get("runner_status"), _json(state.get("prospective_boundary")), state.get("kill_switch"), state.get("created_at"),
             state.get("last_poll_at"), state.get("last_successful_read_at"), state.get("poll_interval_seconds"),
             source_hashes.get("state"), source_hashes.get("manifest")))
        for symbol, progress in (state.get("symbols") or {}).items():
            self._execute("""INSERT INTO strategy.phase6_symbol_progress(strategy_id,symbol,last_m5,last_m15,initialized,last_candle)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (strategy_id,symbol) DO UPDATE SET
                last_m5=EXCLUDED.last_m5,last_m15=EXCLUDED.last_m15,initialized=EXCLUDED.initialized,last_candle=EXCLUDED.last_candle,updated_at=now()""",
                (strategy_id, symbol, progress.get("last_m5"), progress.get("last_m15"), progress.get("initialized"), _json(progress.get("last_candle"))))

    def setup(self, setup: dict[str, Any], strategy_id: str) -> None:
        self._execute("""INSERT INTO strategy.phase6_setups
            (setup_id,strategy_id,market_event_id,symbol,direction,pattern,setup_timestamp,setup_timestamp_iso,qualification,qualification_flags,
             retrace_state,entry_level,theoretical_entry,spread_at_detection,target_completed,status,m5_start_index,zone_left,thesis_invalidated,
             event_bar,compact_geometry,provenance) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (setup_id) DO UPDATE SET status=EXCLUDED.status,retrace_state=EXCLUDED.retrace_state,target_completed=EXCLUDED.target_completed,
             zone_left=EXCLUDED.zone_left,thesis_invalidated=EXCLUDED.thesis_invalidated,m5_start_index=EXCLUDED.m5_start_index,
             event_bar=EXCLUDED.event_bar,compact_geometry=EXCLUDED.compact_geometry,provenance=EXCLUDED.provenance,updated_at=now()""",
            (setup.get("setup_id"), strategy_id, setup.get("market_event_id"), setup.get("symbol"), setup.get("direction"), setup.get("pattern"),
             setup.get("setup_timestamp"), setup.get("setup_timestamp_iso"), setup.get("qualification"), _json(setup.get("qualification_flags")),
             setup.get("retrace_state"), setup.get("entry_level"), setup.get("theoretical_entry"), setup.get("spread_at_detection"),
             setup.get("target_completed"), setup.get("status"), setup.get("m5_start_index"), setup.get("zone_left"), setup.get("thesis_invalidated"),
             _json(setup.get("event_bar")), _json(setup.get("geometry")), _json(setup.get("provenance"))))
        ids = setup.get("_identity") or {}
        self._execute("""UPDATE strategy.phase6_setups SET strategy_version_id=%s,configuration_version_id=%s,freeze_manifest_id=%s,
            import_batch_id=%s,reentry_state=%s,reentry_eligible=%s,entry_attempt_count=%s WHERE setup_id=%s""",
            (ids.get("strategy_version_id"),ids.get("configuration_version_id"),ids.get("freeze_manifest_id"),ids.get("batch_id"),
             setup.get("reentry_state") or setup.get("retrace_state"), setup.get("reentry_eligible"), len(setup.get("opportunities", [])), setup.get("setup_id")))

    def opportunity(self, value: dict[str, Any], *, legacy_phase6: bool = True) -> None:
        fields = ("entry_opportunity_id","setup_id","entry_attempt_id","economic_position_id","fill_timestamp","fill_timestamp_iso","fill_candle_number",
                  "entry_mechanisms","theoretical_entry","executable_paper_entry","spread_at_fill","stop","target","status","mfe_price","mae_price",
                  "reentry_type","exit_timestamp","exit_reason","realized_R","symbol","direction","pattern","provenance","geometry","leg_a","leg_b")
        vals = [value.get(f) for f in fields]
        for i in (7,23,24,25,26): vals[i] = _json(vals[i])
        self._execute("""INSERT INTO strategy.phase6_entry_opportunities
            (entry_opportunity_id,setup_id,entry_attempt_id,economic_position_id,fill_timestamp,fill_timestamp_iso,fill_candle_number,entry_mechanisms,
             theoretical_entry,executable_paper_entry,spread_at_fill,stop,target,status,mfe_price,mae_price,reentry_type,exit_timestamp,exit_reason,realized_r,
             symbol,direction,pattern,provenance,compact_geometry,leg_a,leg_b) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (entry_opportunity_id) DO UPDATE SET status=EXCLUDED.status,stop=EXCLUDED.stop,target=EXCLUDED.target,mfe_price=EXCLUDED.mfe_price,
             mae_price=EXCLUDED.mae_price,exit_timestamp=EXCLUDED.exit_timestamp,exit_reason=EXCLUDED.exit_reason,realized_r=EXCLUDED.realized_r,updated_at=now()""", tuple(vals))
        if legacy_phase6:
            self._execute("""INSERT INTO audit.phase6_economic_positions_legacy(economic_position_id,entry_opportunity_id,setup_id,status,stop,target,mfe_price,mae_price,realized_r,fill_timestamp,fill_timestamp_iso,exit_timestamp,exit_reason,reentry_type,provenance)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (economic_position_id) DO UPDATE SET status=EXCLUDED.status,stop=EXCLUDED.stop,target=EXCLUDED.target,mfe_price=EXCLUDED.mfe_price,mae_price=EXCLUDED.mae_price,realized_r=EXCLUDED.realized_r,exit_timestamp=EXCLUDED.exit_timestamp,exit_reason=EXCLUDED.exit_reason,updated_at=now()""",
                (value.get("economic_position_id"),value.get("entry_opportunity_id"),value.get("setup_id"),value.get("status"),value.get("stop"),value.get("target"),value.get("mfe_price"),value.get("mae_price"),value.get("realized_R"),value.get("fill_timestamp"),value.get("fill_timestamp_iso"),value.get("exit_timestamp"),value.get("exit_reason"),value.get("reentry_type"),_json(value.get("provenance"))))
        ids = value.get("_identity") or {}
        self._execute("""UPDATE strategy.phase6_entry_opportunities SET strategy_version_id=%s,configuration_version_id=%s,freeze_manifest_id=%s,
            import_batch_id=%s,attempt_number=%s,entry=%s,original_stop=%s,target_consumed=%s WHERE entry_opportunity_id=%s""",
            (ids.get("strategy_version_id"),ids.get("configuration_version_id"),ids.get("freeze_manifest_id"),ids.get("batch_id"),
             value.get("attempt_number"),value.get("executable_paper_entry"),value.get("stop"),value.get("status") == "TARGET_HIT",value.get("entry_opportunity_id")))
        if value.get("economic_position_id") and legacy_phase6:
            self._execute("""UPDATE audit.phase6_economic_positions_legacy SET strategy_version_id=%s,configuration_version_id=%s,freeze_manifest_id=%s,
                import_batch_id=%s,symbol=%s,direction=%s,entry=%s,original_stop=%s,current_stop=%s,size=%s,opened_at=%s,closed_at=%s,
                realized_pnl=%s,mfe_r=%s,mae_r=%s,target_consumed=%s,reentry_state=%s WHERE economic_position_id=%s""",
            (ids.get("strategy_version_id"),ids.get("configuration_version_id"),ids.get("freeze_manifest_id"),ids.get("batch_id"),
             value.get("symbol"),value.get("direction"),value.get("executable_paper_entry"),value.get("stop"),value.get("stop"),
                 value.get("size"),value.get("fill_timestamp_iso"),value.get("exit_timestamp"),value.get("realized_pnl"),value.get("mfe_R"),
                 value.get("mae_R"),value.get("status") == "TARGET_HIT",value.get("reentry_type"),value.get("economic_position_id")))

        if value.get("economic_position_id"):
            self._execute("""INSERT INTO strategy.economic_positions
                (economic_position_id,strategy_version_id,configuration_version_id,freeze_manifest_id,import_batch_id,
                 entry_opportunity_id,setup_id,symbol,direction,status,entry,original_stop,current_stop,target,mfe_price,mae_price,
                 realized_r,opened_at,closed_at,exit_reason,reentry_state,target_consumed,provenance)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (economic_position_id) DO UPDATE SET status=EXCLUDED.status,target=EXCLUDED.target,
                 current_stop=EXCLUDED.current_stop,mfe_price=EXCLUDED.mfe_price,mae_price=EXCLUDED.mae_price,
                 realized_r=EXCLUDED.realized_r,closed_at=EXCLUDED.closed_at,exit_reason=EXCLUDED.exit_reason,
                 target_consumed=EXCLUDED.target_consumed,reentry_state=EXCLUDED.reentry_state,provenance=EXCLUDED.provenance,updated_at=now()""",
                (value.get("economic_position_id"),ids.get("strategy_version_id"),ids.get("configuration_version_id"),ids.get("freeze_manifest_id"),ids.get("batch_id"),
                 value.get("entry_opportunity_id"),value.get("setup_id"),value.get("symbol"),value.get("direction"),value.get("status"),
                 value.get("executable_paper_entry"),value.get("stop"),value.get("stop"),value.get("target"),value.get("mfe_price"),value.get("mae_price"),
                 value.get("realized_R"),value.get("fill_timestamp_iso"),value.get("exit_timestamp"),value.get("exit_reason"),value.get("reentry_type"),
                 value.get("status") == "TARGET_HIT",_json(value.get("provenance"))))

    def lifecycle(self, event: dict[str, Any], event_id: str, *, batch_id: str | None = None) -> None:
        payload = _json(event)
        self._execute("""INSERT INTO strategy.phase6_lifecycle_events
            (event_id,setup_id,entry_opportunity_id,economic_position_id,event_time,event_type,source,payload,import_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (event_id) DO NOTHING""",
            (event_id,event.get("setup_id"),event.get("entry_opportunity_id"),event.get("economic_position_id"),event.get("event_time"),event.get("type"),event.get("source"),payload,batch_id))
        if event.get("economic_position_id"):
            self._execute("INSERT INTO strategy.phase6_position_lifecycle(event_id,economic_position_id,event_time,event_type,source,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING", (event_id,event.get("economic_position_id"),event.get("event_time"),event.get("type"),event.get("source"),payload))
        if event.get("setup_id"):
            self._execute("INSERT INTO strategy.phase6_setup_lifecycle(event_id,setup_id,event_time,event_type,source,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING", (event_id,event.get("setup_id"),event.get("event_time"),event.get("type"),event.get("source"),payload))

    def unattached_event(self, event: dict[str, Any], event_id: str, *, source_file: str, line: int, batch_id: str) -> None:
        event_type = str(event.get("type") or "UNKNOWN")
        classification = "TELEMETRY" if event_type in {"READ_ERROR", "DATA_GAP_DETECTED"} else "AUDIT"
        self._execute("""INSERT INTO telemetry.phase6_unattached_events
            (event_id,event_type,event_time,source,source_file,source_line,payload,linkage_reason,import_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT (event_id) DO NOTHING""",
            (event_id,event_type,event.get("event_time"),event.get("source"),source_file,line,_json(event),
             f"NO_DOMAIN_LINK:{classification}",batch_id))

    def observation(self, value: dict[str, Any], *, kind: str, source_file: str, line: int, observation_id: str | None = None) -> str:
        content_hash = canonical_hash(value)
        oid = observation_id or f"obs-{content_hash[:32]}"
        self._execute("""INSERT INTO research.phase6_observations(observation_id,content_sha256,kind,source_file,source_line,event_id,snapshot_ref,observed_at,symbol,payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (content_sha256) DO NOTHING""",
            (oid,content_hash,kind,source_file,line,value.get("event_id"),value.get("snapshot_id") or value.get("context_snapshot_ref"),value.get("event_timestamp") or value.get("timestamp"),value.get("symbol"),_json(value)))
        return oid

    def link_observation(self, setup_id: str, observation_id: str, role: str) -> None:
        self._execute("INSERT INTO strategy.phase6_setup_observation_refs(setup_id,observation_id,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (setup_id, observation_id, role))
