"""Read-only audit for Context outcome attribution.

The query intentionally returns evidence and IDs only.  It never updates
strategy.entry_signal_outcomes or broker state.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from postgres.db import connect


AUDIT_SQL = """
WITH facts AS (
    SELECT s.signal_id, s.decision_time, s.signal_emitted_at,
           s.entry_price, s.stop_price, s.target_price,
           o.status, o.realized_r, o.exit_timestamp,
           o.strategy_outcome, o.strategy_realized_r,
           o.execution_outcome, o.broker_realized_r,
           o.broker_fill_timestamp, o.broker_exit_timestamp,
           o.broker_exit_reason, o.attribution_status,
           r.outcome AS execution_result_outcome,
           c.broker_deal_id, c.broker_position_id,
           c.broker_exit_timestamp AS close_fact_exit_timestamp,
           c.broker_exit_reason AS close_fact_reason,
           c.broker_realized_r AS close_fact_realized_r
    FROM strategy.entry_signals s
    LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
    LEFT JOIN execution_v2.execution_intent i ON i.entry_signal_id = s.signal_id
    LEFT JOIN execution_v2.execution_result r ON r.execution_intent_id = i.execution_intent_id
    LEFT JOIN execution_v2.broker_close_fact c ON c.entry_signal_id = s.signal_id
    WHERE s.strategy_id = 'CONTEXT_STRUCTURE_RETRACE_V1'
)
SELECT * FROM facts ORDER BY decision_time, signal_id
"""


def _row_dict(cur: Any, row: Any) -> dict[str, Any]:
    names = [column.name if hasattr(column, "name") else column[0] for column in cur.description]
    return dict(zip(names, row, strict=True))


def audit(connect_fn=connect) -> dict[str, Any]:
    with connect_fn(readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(AUDIT_SQL)
            rows = [_row_dict(cur, row) for row in cur.fetchall()]

    def before(left: Any, right: Any) -> bool:
        return left is not None and right is not None and left < right

    categories = {key: [] for key in ("A", "B", "C", "D", "E", "F")}
    for row in rows:
        sid = row["signal_id"]
        if row["status"] == "TARGET_HIT" and before(row["exit_timestamp"], row["signal_emitted_at"]): categories["A"].append(sid)
        if row["status"] == "STOPPED" and before(row["exit_timestamp"], row["signal_emitted_at"]): categories["B"].append(sid)
        if row["exit_timestamp"] is not None and row["decision_time"] == row["exit_timestamp"] and before(row["decision_time"], row["signal_emitted_at"]): categories["C"].append(sid)
        if row["attribution_status"] == "AMBIGUOUS_INTRABAR" or row["status"] == "AMBIGUOUS_INTRABAR": categories["D"].append(sid)
        broker_outcome = {"STOP_LOSS": "STOPPED", "SL": "STOPPED", "TAKE_PROFIT": "TARGET_HIT", "TP": "TARGET_HIT"}.get(str(row["close_fact_reason"] or "").upper(), row["execution_outcome"])
        if broker_outcome and row["status"] and broker_outcome != row["status"]: categories["E"].append(sid)
        if row["execution_result_outcome"] == "FILLED" and not row["close_fact_exit_timestamp"]: categories["F"].append(sid)
    return {"total_signals": len(rows), "counts": {key: len(value) for key, value in categories.items()},
            "affected_signal_ids": categories}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Context outcome attribution audit")
    parser.parse_args()
    print(json.dumps(audit(), default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
