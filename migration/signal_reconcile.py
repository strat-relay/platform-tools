from __future__ import annotations

import json
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any

from .reconcile import ReconciliationStatus, reconcile
from .signal import canonical_signal


def reconcile_legacy_signals(conn: Any, source_path: Path, *, run_id: str | None = None, source_id: str | None = None) -> dict[str, Any]:
    """Read a legacy JSONL source and compare canonical rows by signal identity."""
    run_id = run_id or f"signal-reconcile-{uuid.uuid4()}"
    legacy, malformed = [], []
    for offset, raw_line in _lines(source_path):
        try:
            raw = json.loads(raw_line)
            signal = canonical_signal(raw, source_reference={"source_id": source_id or str(source_path), "source_offset": offset})
            legacy.append({**signal.fields, "id": signal.signal_id, "hash": signal.entry_signal_hash, "version": signal.fields["strategy_version"]})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            malformed.append({"identity": f"{source_id or source_path}:{offset}", "status": ReconciliationStatus.MALFORMED_LEGACY_LINE.value, "detail": str(exc)})
    with conn.cursor() as cur:
        cur.execute("""SELECT e.signal_id,e.entry_signal_hash,e.strategy_id,e.strategy_version,e.strategy_ref,e.parameter_set_ref,
            e.strategy_instance_id,e.instrument,e.direction,e.decision_time,e.signal_emitted_at,e.entry_type,e.entry_price,
            e.stop_price,e.target_price,e.risk_distance,e.target_distance,e.target_r,e.economic_position_id,
            e.entry_opportunity_id,e.setup_id,e.source_event_id,e.terminal_state,o.publish_status,i.status,
            co.publish_status,ci.status
            FROM strategy.entry_signals e
            LEFT JOIN platform.outbox_events o ON o.event_id=e.signal_id || ':entry.created'
            LEFT JOIN platform.inbox_events i ON i.event_id=o.event_id AND i.consumer_name='p2-signal-shadow'
            LEFT JOIN platform.outbox_events co ON co.event_id=e.candidate_id || ':candidate.detected'
            LEFT JOIN platform.inbox_events ci ON ci.event_id=co.event_id AND ci.consumer_name='p2-signal-shadow'
            """)
        database = [{"id": r[0], "hash": r[1], "strategy_id": r[2], "version": r[3], "strategy_ref": r[4], "parameter_set_ref": r[5], "strategy_instance_id": r[6], "instrument": r[7], "direction": r[8], "decision_time": r[9].astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), "signal_emitted_at": r[10].astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") if r[10] else None, "entry_type": r[11], "entry_price": float(r[12]) if r[12] is not None else None, "stop_price": float(r[13]) if r[13] is not None else None, "target_price": float(r[14]) if r[14] is not None else None, "risk_distance": float(r[15]) if r[15] is not None else None, "target_distance": float(r[16]) if r[16] is not None else None, "target_r": float(r[17]) if r[17] is not None else None, "economic_position_id": r[18], "entry_opportunity_id": r[19], "setup_id": r[20], "source_event_id": r[21], "terminal_state": r[22], "_outbox_status": r[23], "_inbox_status": r[24], "_candidate_outbox_status": r[25], "_candidate_inbox_status": r[26]} for r in cur.fetchall()]
    result = reconcile(legacy, database, key="id")
    finding_by_id = {item["identity"]: item for item in result["findings"]}
    for row in database:
        missing = []
        if row["_outbox_status"] != "PUBLISHED": missing.append("outbox event not published")
        if row["_inbox_status"] != "PROCESSED": missing.append("durable JetStream consumer has no processed inbox effect")
        if row["_candidate_outbox_status"] != "PUBLISHED": missing.append("candidate outbox event not published")
        if row["_candidate_inbox_status"] != "PROCESSED": missing.append("candidate event has no processed durable inbox effect")
        if missing:
            finding = finding_by_id.get(row["id"])
            detail = "; ".join(missing)
            if finding:
                if finding["status"] == ReconciliationStatus.MATCH.value:
                    result["summary"][ReconciliationStatus.MATCH.value] = max(0, result["summary"].get(ReconciliationStatus.MATCH.value, 0) - 1)
                    finding["status"] = ReconciliationStatus.UNRESOLVED.value
                    result["summary"][ReconciliationStatus.UNRESOLVED.value] = result["summary"].get(ReconciliationStatus.UNRESOLVED.value, 0) + 1
                finding["detail"] = "; ".join(filter(None, (finding.get("detail", ""), detail)))
            else:
                finding = {"identity": row["id"], "status": ReconciliationStatus.UNRESOLVED.value, "detail": detail}
                result["findings"].append(finding); finding_by_id[row["id"]] = finding
                result["summary"][ReconciliationStatus.UNRESOLVED.value] = result["summary"].get(ReconciliationStatus.UNRESOLVED.value, 0) + 1
    result["findings"].extend(malformed); result["summary"][ReconciliationStatus.MALFORMED_LEGACY_LINE.value] = len(malformed)
    result["clean"] = all(item["status"] == ReconciliationStatus.MATCH.value for item in result["findings"])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO platform.reconciliation_runs(run_id,domain,status,summary,completed_at) VALUES (%s,'signals','COMPLETED',%s::jsonb,now())", (run_id, json.dumps(result["summary"], sort_keys=True)))
        for finding in result["findings"]:
            cur.execute("INSERT INTO platform.reconciliation_findings(run_id,identity,status,detail) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (run_id, finding["identity"], finding["status"], finding.get("detail", "")))
    conn.commit()
    result["run_id"] = run_id
    return result


def _lines(path: Path):
    offset = 0
    with path.open("rb") as fh:
        for line in fh:
            raw = line.rstrip(b"\r\n")
            yield offset, raw
            offset += len(line)
