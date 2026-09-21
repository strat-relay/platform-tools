"""Prospective multi-policy collector behind injected read-only providers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .experiments import MultiPolicyExperimentRunner
from .observation import CausalObserver
from .observation_storage import ObservationStore


class ProspectiveActivation:
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)

    def activate(self, *, strategies: list[str], instruments: list[str]) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        row = {"trade_manager_activation_cutoff": datetime.now(timezone.utc).isoformat(),
               "strategies": strategies, "instruments": instruments,
               "prospective_collection_enabled": True, "collector_started": False,
               "mode": "ADVISORY_SHADOW"}
        self.path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
        return row

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))


class ProspectiveExperimentCollector:
    def __init__(self, position_source: Callable[[], Iterable[dict[str, Any]]],
                 market_source: Callable[[dict[str, Any]], dict[str, Any]], activation: dict[str, Any],
                 store: ObservationStore | None = None, runner: MultiPolicyExperimentRunner | None = None):
        self.position_source, self.market_source, self.activation = position_source, market_source, activation
        self.observer, self.store, self.runner = CausalObserver(), store or ObservationStore(), runner or MultiPolicyExperimentRunner()

    def _eligible(self, position: dict[str, Any]) -> bool:
        created = position.get("created_at") or position.get("fill_timestamp_iso") or position.get("entry_time")
        if not created:
            return False
        cutoff = datetime.fromisoformat(self.activation["trade_manager_activation_cutoff"].replace("Z", "+00:00"))
        return datetime.fromisoformat(str(created).replace("Z", "+00:00")) > cutoff and position.get("strategy_id") in self.activation["strategies"] and position.get("symbol") in self.activation["instruments"]

    def collect_once(self, as_of: str | None = None) -> dict[str, Any]:
        totals = {"eligible_positions": 0, "observations": 0, "policy_rows": 0}
        for position in self.position_source():
            if position.get("status", "OPEN") != "OPEN" or not self._eligible(position):
                continue
            market = self.market_source(position); when = as_of or datetime.now(timezone.utc).isoformat()
            self.runner.restore(position, self.store.experiments.records())
            bundle = self.observer.observe(position, market["quote"], market.get("m1_rates", []), market.get("m5_rates", []), when)
            self.store.append_bundle(bundle)
            rows = self.runner.observe(position, bundle["observation"])
            self.store.append_experiment_results(rows)
            totals["eligible_positions"] += 1; totals["observations"] += 1; totals["policy_rows"] += len(rows)
        return totals

    def write_dashboard(self, path: str | Path) -> dict[str, Any]:
        dashboard = {**self.runner.dashboard(), "activation": self.activation,
                     "collector_started": False, "market_source": "INJECTED_READ_ONLY_PROVIDER",
                     "position_source": "INJECTED_READ_ONLY_PROVIDER"}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(dashboard, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return dashboard
