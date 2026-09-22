"""Latency instrumentation shared by the DB-first and NATS-first hot paths.

Timestamps (perf_counter seconds; monotonic, process-local - fine for a same-process
architectural comparison, not a claim about cross-host clock-synchronized production latency):

    T0  strategy decision complete           (decision_time / signal_emitted_at instant)
    T1  orchestrator accepts signal           (canonicalization begins)
    T2  bus publish initiated                 (JetStream publish call made, or DB tx begin
                                                for the DB-first path's equivalent step)
    T3  bus PUBACK / durable acceptance        (JetStream ack, or DB-first outbox-relay's
                                                own JetStream PUBACK after its DB commit)
    T4  distribution consumer receives signal  (independent of PostgreSQL)
    T5  PostgreSQL projector begins            (async, NATS-first only; DB-first has no
                                                separate "begins" instant - T1 already is it)
    T6  PostgreSQL projection committed

Named metrics (ms) computed from the above; every metric is optional (`None` if either
timestamp is missing), so the SAME dataclass instruments both paths without one being forced
to fill in fields it does not have (DB-first has no T5; NATS-first's "outbox availability" has
no equivalent, etc - see docs/nats_first_data_plane/00_README.md section 3 for the mapping).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class LatencyTimestamps:
    path: str  # "DB_FIRST" | "NATS_FIRST"
    t0_decision: float | None = None
    t1_orchestrator_accept: float | None = None
    t2_publish_initiated: float | None = None
    t3_puback: float | None = None
    t4_distribution_receive: float | None = None
    t5_projector_begin: float | None = None
    t6_projector_commit: float | None = None

    def metrics_ms(self) -> dict[str, float]:
        def delta(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else (b - a) * 1000.0
        out = {
            "strategy_to_bus_ms": delta(self.t0_decision, self.t2_publish_initiated),
            "orchestrator_to_bus_ms": delta(self.t1_orchestrator_accept, self.t2_publish_initiated),
            "bus_publish_ack_ms": delta(self.t2_publish_initiated, self.t3_puback),
            "bus_to_distribution_ms": delta(self.t3_puback, self.t4_distribution_receive),
            "bus_to_db_projection_ms": delta(self.t3_puback, self.t6_projector_commit),
            "projector_processing_ms": delta(self.t5_projector_begin, self.t6_projector_commit),
            "end_to_end_projection_ms": delta(self.t0_decision, self.t6_projector_commit),
            "end_to_end_acceptance_ms": delta(self.t0_decision, self.t3_puback),
        }
        return {k: v for k, v in out.items() if v is not None}


@dataclass
class LatencyRecorder:
    """In-memory distribution accumulator; used by tests and the benchmark script."""
    samples: dict[str, list[float]] = field(default_factory=dict)

    def record(self, timestamps: LatencyTimestamps) -> dict[str, float]:
        metrics = timestamps.metrics_ms()
        for name, value in metrics.items():
            self.samples.setdefault(f"{timestamps.path}:{name}", []).append(value)
        return metrics

    def distribution(self, key: str) -> dict[str, float] | None:
        values = sorted(self.samples.get(key, []))
        if not values:
            return None
        return {
            "n": len(values),
            "min": values[0],
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
            "max": values[-1],
            "mean": statistics.fmean(values),
        }

    def report(self) -> dict[str, dict[str, float]]:
        return {key: self.distribution(key) for key in sorted(self.samples) if self.distribution(key)}


def _percentile(sorted_values: list[float], p: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = p * (len(sorted_values) - 1)
    lower, upper = int(rank), min(int(rank) + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
