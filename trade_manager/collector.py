"""Provider-agnostic causal observation collector.

Providers are injected by a future read-only adapter. This module itself has
no MT5, bridge, or broker dependency and cannot perform broker writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .observation import CausalObserver
from .observation_storage import ObservationStore


class CausalObservationCollector:
    def __init__(self, position_source: Callable[[], Iterable[dict[str, Any]]],
                 market_source: Callable[[dict[str, Any]], dict[str, Any]],
                 store: ObservationStore | None = None,
                 observer: CausalObserver | None = None):
        self.position_source = position_source
        self.market_source = market_source
        self.store = store or ObservationStore()
        self.observer = observer or CausalObserver()
        self.observer.observations.update(self.store.last_observations())
        self.mode = "ADVISORY_SHADOW"

    def collect_once(self, as_of: str | int | float | datetime | None = None) -> dict[str, int]:
        when = as_of or datetime.now(timezone.utc)
        totals = {"positions": 0, "observations": 0, "events": 0, "decisions": 0}
        for position in self.position_source():
            if position.get("status", "OPEN") != "OPEN":
                continue
            market = self.market_source(position)
            bundle = self.observer.observe(position, market["quote"], market.get("m1_rates", []),
                                           market.get("m5_rates", []), when,
                                           ema_tolerance=float(market.get("ema_tolerance", 0.0)),
                                           structure_tolerance=float(market.get("structure_tolerance", 0.0)))
            counts = self.store.append_bundle(bundle)
            totals["positions"] += 1
            totals["observations"] += counts["observation"]
            totals["events"] += counts["events"]
            totals["decisions"] += counts["decision"]
        return totals
