"""Read-only Trade Manager adapter for the shared Phase 7 fan-out stream."""
from __future__ import annotations

import json
import argparse
import signal
import time
from datetime import datetime, timezone
from typing import Any

from .experiments import MultiPolicyExperimentRunner
from .fanout import FanoutConsumer
from .observation import CausalObserver
from .observation_storage import ObservationStore
from .phase2 import Phase2TradeManager
from .central import (BROKER_STATE_PATH, PROPOSALS_PATH, OwnershipRegistry,
                      BrokerStateStream, ManagementProposal, append_unique)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class SharedStreamTradeManager:
    def __init__(self, activation: dict[str, Any], root: str = "runtime/trade_manager/observation_stream"):
        self.activation = activation
        self.consumer = FanoutConsumer("trade_manager", root)
        self.observer = CausalObserver()
        self.runner = MultiPolicyExperimentRunner()
        self.store = ObservationStore("runtime/trade_manager/observations")
        self.mode = str(activation.get("mode", "ADVISORY_SHADOW"))
        self.policy_manager = Phase2TradeManager()
        self.ownership = OwnershipRegistry()
        self.broker_state = BrokerStateStream()
        self.positions: dict[str, dict[str, Any]] = {}
        self.incomplete_intervals: list[dict[str, Any]] = []
        self.started_at: str | None = None
        self.state_path = self.consumer.root / "collector_state.json"
        self.health_path = self.consumer.root / "collector_health.json"
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.started_at = saved.get("collection_started_at")

    def start(self, started_at: str | None = None) -> str:
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.state_path.write_text(json.dumps({
            "collection_started_at": self.started_at,
            "original_activation_cutoff": self.activation.get("trade_manager_activation_cutoff"),
            "mode": self.mode,
        }, indent=2) + "\n", encoding="utf-8")
        self._write_health(running=True)
        return self.started_at

    def _eligible(self, event: dict[str, Any]) -> bool:
        created = event.get("entry_timestamp") or event.get("observed_at")
        if not self.started_at or not created:
            return False
        return _parse_time(created) >= _parse_time(self.started_at)

    def _write_health(self, *, running: bool = True, processed: int = 0, position_events: int = 0) -> None:
        health = self.consumer.health()
        health.update({"status": "ACTIVE" if running else "STOPPED", "timestamp": datetime.now(timezone.utc).isoformat(),
                       "collector_running": running, "collection_started_at": self.started_at,
                       "observation_count": processed, "position_event_count": position_events,
                       "incomplete_intervals": len(self.incomplete_intervals), "broker_writes": 0,
                       "mode": self.mode, "direct_broker_access": False,
                       "execution_endpoint": "NONE_BY_DESIGN",
                       "broker_state_stream_ready": self.broker_state.healthy() if self.mode == "REAL_MANAGEMENT" else True,
                       "ownership_registry_ready": self.ownership.path.parent.exists(),
                       "orchestrator_proposal_sink_ready": PROPOSALS_PATH.parent.exists()})
        self.health_path.write_text(json.dumps(health, indent=2, default=str) + "\n", encoding="utf-8")

    def process_once(self) -> dict[str, Any]:
        if not self.started_at:
            health = self.consumer.health()
            health.update({"collector_running": False, "collection_started_at": None,
                           "observation_count": 0, "position_event_count": 0, "broker_writes": 0})
            self._write_health(running=False)
            return {"processed_observations": 0, "position_events": 0, "policy_rows": 0,
                    "incomplete_intervals": len(self.incomplete_intervals), "health": health,
                    "broker_writes": 0, "inactive": True}
        rows = self.consumer.read()
        processed = 0; policy_rows = 0; position_events = 0
        # Lifecycle state is applied first so an observation and its lifecycle event
        # are safe even if a publisher poll writes them in the opposite order.
        for event in rows:
            if event["event_type"] in {"POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED"}:
                position_events += 1
                self.positions[event["economic_position_id"]] = {**self.positions.get(event["economic_position_id"], {}), **event}
                continue
            if event["event_type"] != "MARKET_OBSERVATION":
                continue
            pid = event.get("economic_position_id")
            position = self.positions.get(pid) if pid else None
            if not position or not self._eligible(position):
                continue
            if event.get("bid") is None or event.get("ask") is None:
                self.incomplete_intervals.append({"economic_position_id": pid, "timestamp": event["observed_at"], "reason": "MISSING_QUOTE"})
                continue
            market = {"quote": {"bid": event["bid"], "ask": event["ask"]},
                      "m1_rates": event.get("m1_history", []), "m5_rates": event.get("m5_history", [])}
            position = {**position, "entry": position.get("entry"), "original_stop": position.get("original_stop"),
                        "original_target": position.get("target"), "current_stop": position.get("current_stop"),
                        "size": position.get("size"), "entry_time": position.get("entry_timestamp"), "status": position.get("status", "OPEN")}
            bundle = self.observer.observe(position, market["quote"], market["m1_rates"], market["m5_rates"], event["observed_at"])
            self.store.append_bundle(bundle); results = self.runner.observe(position, bundle["observation"])
            if self.mode == "REAL_MANAGEMENT":
                decision = self.policy_manager.evaluate(position, bundle["observation"], timestamp=event["observed_at"])
                if decision.get("action") != "HOLD":
                    snapshot = self.broker_state.load() or {}
                    proposal = ManagementProposal.from_decision(decision, position, int(snapshot.get("snapshot_version", 0)))
                    append_unique(PROPOSALS_PATH, proposal.as_dict(), proposal.management_proposal_id)
            self.store.append_experiment_results(results); processed += 1; policy_rows += len(results)
        self._write_health(processed=processed, position_events=position_events)
        return {"processed_observations": processed, "position_events": position_events,
                "policy_rows": policy_rows, "incomplete_intervals": len(self.incomplete_intervals),
                "health": self.consumer.health(), "broker_writes": 0}

    def status(self) -> dict[str, Any]:
        health = self.consumer.health()
        running = bool(self.started_at and health.get("collector_running"))
        return {
            "COLLECTOR_IMPLEMENTED": True,
            "OBSERVATION_PUBLISHER_ACTIVE": bool(health.get("publisher_running", False)),
            "PROSPECTIVE_COLLECTION_ACTIVE": running,
            "PROSPECTIVE_TRADES_OBSERVED": int(health.get("observation_count", 0)),
            "collection_started_at": self.started_at,
            "trade_manager_activation_cutoff": self.activation.get("trade_manager_activation_cutoff"),
            "publisher_sequence": health.get("publisher_sequence", 0),
            "collector_checkpoint": health.get("collector_checkpoint", 0),
            "collector_lag_events": health.get("collector_lag_events", 0),
            "collector_lag_seconds": health.get("collector_lag_seconds"),
            "experiment_count": len(self.runner.experiments),
            "eligible_positions": sorted(self.positions),
            "broker_writes": 0,
            "mode": self.mode,
            "direct_broker_access": False,
            "execution_endpoint": "NONE_BY_DESIGN",
            "broker_state_stream_ready": self.broker_state.healthy() if self.mode == "REAL_MANAGEMENT" else True,
            "ownership_registry_ready": self.ownership.path.parent.exists(),
            "orchestrator_proposal_sink_ready": PROPOSALS_PATH.parent.exists(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Trade Manager shared-stream consumer")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--interval", type=float, default=1.0)
    start.add_argument("--activation", default="runtime/trade_manager/activation.json")
    start.add_argument("--mode", choices=("ADVISORY_SHADOW", "REAL_MANAGEMENT"), default=None)
    sub.add_parser("status")
    args = parser.parse_args()
    activation_path = __import__("pathlib").Path(args.activation)
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    if args.mode:
        activation["mode"] = args.mode
    manager = SharedStreamTradeManager(activation)
    if args.command == "status":
        print(json.dumps(manager.status(), indent=2, default=str))
        return
    manager.start()
    stop = {"value": False}
    def handle(_signum: int, _frame: Any) -> None:
        stop["value"] = True
    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    while not stop["value"]:
        manager.process_once()
        time.sleep(max(0.1, args.interval))
    manager._write_health(running=False)


if __name__ == "__main__":
    main()
