"""Non-broker Liquidity live-evaluator runtime boundary.

The production workload can use this module later.  It reads canonical instance
membership and provider mappings, consumes only the read-only 22347 market-data
client, and persists accepted signals through the existing CanonicalSignalPublisher.
It never imports an execution client and never reads paper/forward state files.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from orchestration.canonical_signal_publisher import CanonicalSignalPublisher
from orchestration.liquidity_live import PARAMETER_SETS, LiquidityLiveEvaluator
from postgres.db import connect


def active_instance_memberships(conn: Any) -> list[dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT i.instance_id, m.canonical_instrument, p.provider_symbol
                      FROM platform.strategy_definition d
                      JOIN platform.strategy_instance i ON i.strategy_id = d.strategy_id
                      JOIN strategy.instrument_membership m
                        ON m.strategy_id = i.strategy_id AND m.strategy_instance_id = i.instance_id
                       AND m.state = 'ACTIVE'
                      JOIN platform.instrument_provider_mapping p
                        ON p.provider = 'MT5' AND p.canonical_instrument = m.canonical_instrument
                       AND p.state = 'ACTIVE'
                      WHERE d.strategy_id = 'LIQUIDITY_DISPLACEMENT_SCALP_V1'
                        AND d.enabled = TRUE AND i.enabled = TRUE
                      ORDER BY i.instance_id, m.canonical_instrument""")
        return [{"instance_id": iid, "canonical_instrument": canonical, "provider_symbol": symbol}
                for iid, canonical, symbol in cur.fetchall()]


class LiquidityLiveRuntime:
    def __init__(self, *, conn: Any, snapshot_reader: Callable[..., dict[str, Any]],
                 publisher: CanonicalSignalPublisher, runtime_instance_id: str = "liquidity-live-runtime"):
        self.conn = conn
        self.snapshot_reader = snapshot_reader
        self.publisher = publisher
        self.runtime_instance_id = runtime_instance_id
        self.evaluators: dict[str, LiquidityLiveEvaluator] = {}

    def heartbeat(self, *, status: str = "RUNNING") -> None:
        """Persist workload liveness in the canonical runtime registry."""
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.runtime_instances
                    (instance_id, component, process_id, status, metadata)
                    VALUES (%s, 'liquidity-live-evaluator', NULL, %s, %s)
                    ON CONFLICT (instance_id) DO UPDATE SET
                    component = EXCLUDED.component, status = EXCLUDED.status,
                    last_heartbeat_at = now(), metadata = EXCLUDED.metadata,
                    stopped_at = NULL""", (self.runtime_instance_id, status,
                                             {"broker_writes": 0, "source": "LIVE_MARKET"}))

    def tick(self, *, evaluation_time: str | None = None) -> dict[str, Any]:
        evaluation_time = evaluation_time or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        published: list[str] = []
        self.heartbeat()
        memberships = active_instance_memberships(self.conn)
        for row in memberships:
            instance_id = row["instance_id"]
            parameter_set = PARAMETER_SETS.get(instance_id)
            if parameter_set is None or row["canonical_instrument"] != parameter_set.canonical_instrument:
                continue
            evaluator = self.evaluators.setdefault(instance_id, LiquidityLiveEvaluator(parameter_set))
            snapshot = self.snapshot_reader(row["canonical_instrument"], row["provider_symbol"])
            # The provider symbol comes from the mapping query; the evaluator never
            # derives it from a canonical symbol suffix.
            snapshot = {**snapshot, "provider_symbol": row["provider_symbol"], "source_kind": "LIVE_MARKET"}
            signal = evaluator.evaluate(snapshot, evaluation_time=evaluation_time)
            if signal is None:
                continue
            _, inserted = self.publisher.publish(signal)
            if inserted:
                published.append(signal.signal_id)
        self.conn.commit()
        return {"memberships": len(memberships), "published": published, "production_broker_writes": 0}


def build_runtime(*, conn: Any, cutoff_id: str, cutoff_utc: str,
                  snapshot_reader: Callable[..., dict[str, Any]]) -> LiquidityLiveRuntime:
    return LiquidityLiveRuntime(
        conn=conn,
        snapshot_reader=snapshot_reader,
        publisher=CanonicalSignalPublisher(conn, cutoff_id=cutoff_id, cutoff_utc=cutoff_utc,
                                            source_id="liquidity-live-evaluator"),
    )
