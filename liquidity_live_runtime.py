"""Non-broker Liquidity live-evaluator runtime boundary.

The production workload can use this module later.  It reads canonical instance
membership and provider mappings, consumes only the read-only 22347 market-data
client, and persists accepted signals through the existing CanonicalSignalPublisher.
It never imports an execution client and never reads paper/forward state files.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from orchestration.canonical_signal_publisher import CanonicalSignalPublisher
from orchestration.liquidity_live import STRATEGY_ID, PARAMETER_SETS, LiquidityLiveEvaluator
from liquidity_market_data import LiveMarketSnapshot
from liquidity_lifecycle import settle_filled_entry
from liquidity_outcomes import project_liquidity_outcome
from postgres.db import connect
from observability.strategy_audit import audit
from market_data_cache.reader import MarketDataUnavailable


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


class PostgresLiquiditySetupStore:
    def __init__(self, conn: Any):
        self.conn = conn

    def load(self, instance_id: str, canonical_instrument: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT setup_id, state, payload
                           FROM strategy.liquidity_setup_state
                           WHERE instance_id=%s AND canonical_instrument=%s""",
                        (instance_id, canonical_instrument))
            return [{**(payload or {}), "setup_id": setup_id, "state": state}
                    for setup_id, state, payload in cur.fetchall()]

    def save(self, state: dict[str, Any]) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.liquidity_setup_state
                    (setup_id, strategy_id, strategy_version, instance_id,
                     canonical_instrument, state, payload)
                    VALUES (%s,'LIQUIDITY_DISPLACEMENT_SCALP_V1','V1',%s,%s,%s,%s::jsonb)
                    ON CONFLICT (setup_id) DO UPDATE SET state=EXCLUDED.state,
                    payload=EXCLUDED.payload, updated_at=now()""",
                        (state["setup_id"], state["instance_id"], state["canonical_instrument"],
                         state["state"], json.dumps(state)))


def open_liquidity_entries(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT s.signal_id, s.strategy_instance_id, s.instrument,
                              p.provider_symbol, s.direction, s.entry_price,
                              s.stop_price, s.target_price, s.decision_time
                       FROM strategy.entry_signals s
                       JOIN strategy.entry_signal_outcomes o ON o.signal_id = s.signal_id
                       JOIN platform.instrument_provider_mapping p
                         ON p.provider = 'MT5' AND p.canonical_instrument = s.instrument
                        AND p.state = 'ACTIVE'
                       WHERE s.strategy_id = 'LIQUIDITY_DISPLACEMENT_SCALP_V1'
                         AND o.status = 'OPEN'""")
        return [{"signal_id": sid, "instance_id": iid, "canonical_instrument": instrument,
                 "provider_symbol": symbol, "direction": direction, "entry": float(entry),
                 "stop": float(stop), "target": float(target), "fill_timestamp": decision_time}
                for sid, iid, instrument, symbol, direction, entry, stop, target, decision_time in cur.fetchall()]


def monitor_open_liquidity_entries(conn: Any, snapshot_reader: Any) -> list[str]:
    """Monitor already-entered trades without consulting membership state."""
    terminal: list[str] = []
    for row in open_liquidity_entries(conn):
        try:
            snapshot = snapshot_reader(row["canonical_instrument"], row["provider_symbol"])
        except MarketDataUnavailable as exc:
            audit("open_entry_data_unavailable", runner="liquidity-live", signal_id=row["signal_id"],
                  canonical_instrument=row["canonical_instrument"], provider_symbol=row["provider_symbol"],
                  reason="MARKET_DATA_STALE" if "stale" in str(exc).lower() else "MARKET_DATA_UNAVAILABLE")
            continue
        if not isinstance(snapshot, LiveMarketSnapshot):
            raise RuntimeError("snapshot reader did not return a validated LiveMarketSnapshot")
        outcome = settle_filled_entry(direction=row["direction"], entry=row["entry"], stop=row["stop"],
                                      target=row["target"], fill_timestamp=row["fill_timestamp"],
                                      bars=list(snapshot.M5), max_hold_minutes=120)
        if outcome is None:
            continue
        if project_liquidity_outcome(row["signal_id"], status=outcome.status,
                                     realized_r=outcome.realized_r, exit_timestamp=outcome.exit_timestamp,
                                     connect_fn=lambda: conn):
            terminal.append(row["signal_id"])
    return terminal


class LiquidityLiveRuntime:
    def __init__(self, *, conn: Any, snapshot_reader: Callable[..., dict[str, Any]],
                 publisher: CanonicalSignalPublisher, runtime_instance_id: str = "liquidity-live-runtime"):
        self.conn = conn
        self.snapshot_reader = snapshot_reader
        self.publisher = publisher
        self.runtime_instance_id = runtime_instance_id
        self.evaluators: dict[str, LiquidityLiveEvaluator] = {}
        self._restored: set[str] = set()
        self.membership_consecutive_failures: dict[str, int] = {}
        self.runner_consecutive_failures = 0
        self.setup_store = PostgresLiquiditySetupStore(conn)

    def heartbeat(self, *, status: str = "RUNNING") -> None:
        """Persist workload liveness in the canonical runtime registry."""
        audit("runner_heartbeat", runner="liquidity-live", status=status,
              runtime_instance_id=self.runtime_instance_id, broker_writes=0)
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.runtime_instances
                    (instance_id, component, process_id, status, metadata)
                    VALUES (%s, 'liquidity-live-evaluator', NULL, %s, %s)
                    ON CONFLICT (instance_id) DO UPDATE SET
                    component = EXCLUDED.component, status = EXCLUDED.status,
                    last_heartbeat_at = now(), metadata = EXCLUDED.metadata,
                    stopped_at = NULL""", (self.runtime_instance_id, status,
                                             json.dumps({"broker_writes": 0, "source": "LIVE_MARKET"})))

    def tick(self, *, evaluation_time: str | None = None) -> dict[str, Any]:
        evaluation_time = evaluation_time or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        published: list[str] = []
        membership_statuses: dict[str, dict[str, Any]] = {}
        audit("runner_cycle_started", runner="liquidity-live", evaluation_time=evaluation_time)
        self.heartbeat()
        terminal = monitor_open_liquidity_entries(self.conn, self.snapshot_reader)
        audit("open_entries_monitored", runner="liquidity-live", terminal_outcomes=terminal,
              terminal_count=len(terminal))
        memberships = active_instance_memberships(self.conn)
        audit("memberships_loaded", runner="liquidity-live", membership_count=len(memberships),
              memberships=[{"instance_id": row["instance_id"],
                            "canonical_instrument": row["canonical_instrument"],
                            "provider_symbol": row["provider_symbol"]} for row in memberships])
        for row in memberships:
            instance_id = row["instance_id"]
            membership_key = f"{instance_id}:{row['canonical_instrument']}"
            parameter_set = PARAMETER_SETS.get(instance_id)
            if parameter_set is None or row["canonical_instrument"] != parameter_set.canonical_instrument:
                membership_statuses[membership_key] = {"status": "SKIPPED", "reason": "PARAMETER_SET_MISMATCH"}
                continue
            try:
                evaluator = self.evaluators.setdefault(instance_id, LiquidityLiveEvaluator(parameter_set))
                if instance_id not in self._restored:
                    evaluator.restore(self.setup_store.load(instance_id, row["canonical_instrument"]))
                    self._restored.add(instance_id)
                snapshot = self.snapshot_reader(row["canonical_instrument"], row["provider_symbol"])
                if not isinstance(snapshot, LiveMarketSnapshot):
                    audit("snapshot_rejected", runner="liquidity-live", instance_id=instance_id,
                          canonical_instrument=row["canonical_instrument"],
                          provider_symbol=row["provider_symbol"], reason="INVALID_LIVE_SNAPSHOT")
                    raise MarketDataUnavailable("invalid live market snapshot")
                signal = evaluator.evaluate(snapshot, evaluation_time=evaluation_time)
                self.membership_consecutive_failures[membership_key] = 0
                membership_statuses[membership_key] = {"status": "HEALTHY",
                                                        "decision": "SIGNAL" if signal else "NO_SIGNAL"}
                audit("strategy_decision", runner="liquidity-live", strategy_id=STRATEGY_ID,
                      instance_id=instance_id, canonical_instrument=row["canonical_instrument"],
                      provider_symbol=row["provider_symbol"],
                      decision=("SIGNAL" if signal else "NO_SIGNAL"),
                      signal_id=signal.signal_id if signal else None,
                      setup_id=signal.setup_id if signal else None)
                for state in evaluator.export_state():
                    self.setup_store.save(state)
                if signal is None:
                    continue
                _, inserted = self.publisher.publish(signal)
                audit("signal_persisted", runner="liquidity-live", strategy_id=signal.strategy_id,
                      instance_id=signal.strategy_instance_id, signal_id=signal.signal_id,
                      canonical_instrument=signal.canonical_symbol,
                      provider_symbol=signal.broker_symbol_hint, inserted=inserted)
                if inserted:
                    published.append(signal.signal_id)
            except MarketDataUnavailable as exc:
                failures = self.membership_consecutive_failures.get(membership_key, 0) + 1
                self.membership_consecutive_failures[membership_key] = failures
                reason = "MARKET_DATA_STALE" if "stale" in str(exc).lower() else "MARKET_DATA_UNAVAILABLE"
                membership_statuses[membership_key] = {"status": "DATA_UNAVAILABLE", "reason": reason,
                                                        "consecutive_failures": failures}
                audit("membership_data_unavailable", runner="liquidity-live", strategy_id=STRATEGY_ID,
                      instance_id=instance_id, canonical_instrument=row["canonical_instrument"],
                      provider_symbol=row["provider_symbol"], reason=reason,
                      error_type=type(exc).__name__, error=str(exc)[:300],
                      consecutive_failures=failures)
                audit("strategy_decision", runner="liquidity-live", strategy_id=STRATEGY_ID,
                      instance_id=instance_id, canonical_instrument=row["canonical_instrument"],
                      provider_symbol=row["provider_symbol"], decision="DATA_UNAVAILABLE",
                      terminal_reason=reason, signal_id=None, setup_id=None)
                continue
        self.conn.commit()
        self.runner_consecutive_failures = 0
        cycle_status = "DEGRADED" if any(v.get("status") == "DATA_UNAVAILABLE" for v in membership_statuses.values()) else "HEALTHY"
        self.heartbeat(status=cycle_status)
        self.conn.commit()
        audit("runner_cycle_completed", runner="liquidity-live", memberships=len(memberships),
              status=cycle_status, membership_statuses=membership_statuses,
              runner_consecutive_failures=self.runner_consecutive_failures,
              membership_consecutive_failures=dict(self.membership_consecutive_failures),
              market_data_consecutive_failures=sum(self.membership_consecutive_failures.values()),
              published=published, terminal_outcomes=terminal, production_broker_writes=0)
        return {"memberships": len(memberships), "published": published, "terminal_outcomes": terminal,
                "status": cycle_status, "membership_statuses": membership_statuses,
                "runner_consecutive_failures": self.runner_consecutive_failures,
                "membership_consecutive_failures": dict(self.membership_consecutive_failures),
                "market_data_consecutive_failures": sum(self.membership_consecutive_failures.values()),
                "production_broker_writes": 0}


def build_runtime(*, conn: Any, cutoff_id: str, cutoff_utc: str,
                  snapshot_reader: Callable[..., dict[str, Any]]) -> LiquidityLiveRuntime:
    return LiquidityLiveRuntime(
        conn=conn,
        snapshot_reader=snapshot_reader,
        publisher=CanonicalSignalPublisher(conn, cutoff_id=cutoff_id, cutoff_utc=cutoff_utc,
                                            source_id="liquidity-live-evaluator"),
    )
