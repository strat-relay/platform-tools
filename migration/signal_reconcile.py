from __future__ import annotations

import json
import uuid
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
        cur.execute("SELECT signal_id,entry_signal_hash,strategy_id,strategy_version,instrument,direction,decision_time,terminal_state FROM strategy.entry_signals")
        database = [{"id": r[0], "hash": r[1], "strategy_id": r[2], "version": r[3], "instrument": r[4], "direction": r[5], "decision_time": str(r[6]), "terminal_state": r[7]} for r in cur.fetchall()]
    result = reconcile(legacy, database, key="id")
    result["findings"].extend(malformed); result["summary"][ReconciliationStatus.MALFORMED_LEGACY_LINE.value] = len(malformed)
    result["clean"] = result["clean"] and not malformed
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
            yield offset, raw.decode("utf-8")
            offset += len(line)
