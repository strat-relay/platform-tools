"""Deterministically classify open EntrySignals from canonical market candles.

Default mode is a read-only coverage report. ``--apply`` is an explicit
operator action and writes only provable strategy outcomes through the shared
Outcome Resolver writer; ambiguous or insufficient candle coverage remains OPEN.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from outcome_resolver import Candle, EvaluationContract, persist_outcome_row, resolve_candle_path


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _signals(conn, strategy_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.signal_id, s.instrument, s.direction, s.entry_price,
                      s.stop_price, s.target_price, s.decision_time,
                      s.entry_type, s.strategy_metadata
                 FROM strategy.entry_signals s
                 LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
                WHERE s.strategy_id = %s AND (o.status = 'OPEN' OR o.signal_id IS NULL)
                ORDER BY s.decision_time, s.signal_id""",
            (strategy_id,),
        )
        return [
            {"signal_id": sid, "instrument": instrument, "direction": direction,
             "entry": float(entry), "stop": float(stop), "target": float(target),
             "decision_time": _utc(decision_time), "entry_type": entry_type,
             "strategy_metadata": metadata}
            for sid, instrument, direction, entry, stop, target, decision_time, entry_type, metadata in cur.fetchall()
        ]


def _candles(redis_client, instrument: str, timeframe_minutes: int = 15) -> list[Candle]:
    raw = json.loads(redis_client.get(f"md:bars:{instrument}:M{timeframe_minutes}") or "[]")
    result = []
    for row in raw:
        opened = datetime.fromtimestamp(int(row["time"]), timezone.utc)
        result.append(Candle(opened, opened + timedelta(minutes=timeframe_minutes),
                             float(row["high"]), float(row["low"]),
                             float(row["close"]) if row.get("close") is not None else None))
    return result


def replay(*, conn, redis_client, strategy_id: str, apply: bool) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {"strategy_id": strategy_id, "apply": apply, "signals": [],
                              "counts": {"resolved": 0, "ambiguous": 0, "insufficient": 0}}
    for signal in _signals(conn, strategy_id):
        signal["strategy_id"] = strategy_id
        contract = EvaluationContract.from_signal(signal)
        result = resolve_candle_path(
            direction=signal["direction"], entry=signal["entry"], stop=signal["stop"],
            target=signal["target"], entry_timestamp=signal["decision_time"],
            candles=_candles(redis_client, signal["instrument"], contract.timeframe_minutes),
            max_hold_minutes=contract.max_hold_minutes,
            timeframe_minutes=contract.timeframe_minutes,
            expiration_minutes=contract.expiration_minutes,
            activation=contract.activation,
            time_exit_price=contract.time_exit_price,
        )
        report["counts"]["resolved" if result.resolution_state == "RESOLVED" else
                          "ambiguous" if result.resolution_state == "AMBIGUOUS_INTRABAR" else
                          "insufficient"] += 1
        report["signals"].append({"signal_id": signal["signal_id"],
                                  "status": result.status,
                                  "resolution_state": result.resolution_state,
                                  "resolution_method": result.resolution_method,
                                  "exit_timestamp": result.exit_timestamp,
                                  "evidence": result.evidence})
        if apply and result.resolution_state == "RESOLVED":
            with conn.cursor() as cur:
                persist_outcome_row(
                    cur, signal_id=signal["signal_id"], outcome_type="ENTRY_ONLY",
                    status=result.status, realized_r=result.realized_r,
                    exit_timestamp=result.exit_timestamp, source=strategy_id,
                    updated_at=now,
                )
    if apply:
        conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-id", default="KOJO_STRUCTURE_RECLAIM_V3")
    parser.add_argument("--apply", action="store_true", help="explicitly persist only provable resolutions")
    args = parser.parse_args(argv)
    from postgres.db import connect
    import redis

    conn = connect(readonly=not args.apply)
    redis_client = redis.from_url(
        os.environ["MARKET_DATA_REDIS_URL"], password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
    )
    try:
        print(json.dumps(replay(conn=conn, redis_client=redis_client,
                                strategy_id=args.strategy_id, apply=args.apply),
                         indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
