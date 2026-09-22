"""Smoke test only: proves the benchmark harness runs end-to-end and produces distributions for
both paths in every scenario. Not an assertion about actual latency values - see
dataplane/benchmark.py's module docstring and docs/nats_first_data_plane/04_BENCHMARK_RESULTS.md
for why these numbers are an architectural-comparison proxy, not a production claim."""
from __future__ import annotations

import unittest

from dataplane.benchmark import SCENARIOS, ScenarioConfig, run_scenario


class BenchmarkSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_scenario_produces_distributions_for_both_paths_with_a_small_iteration_count(self):
        for cfg in SCENARIOS:
            fast_cfg = ScenarioConfig(cfg.name, cfg.description, iterations=6,
                                      pg_stmt_latency_ms=0.05, pg_spike_probability=cfg.pg_spike_probability,
                                      pg_spike_ms=cfg.pg_spike_ms, js_publish_latency_ms=0.05,
                                      jetstream_outage_for_first_fraction=cfg.jetstream_outage_for_first_fraction,
                                      backlog=cfg.backlog)
            result = await run_scenario(fast_cfg)
            self.assertEqual(result["scenario"], cfg.name)
            keys = result["distributions"].keys()
            self.assertTrue(any(k.startswith("DB_FIRST:") for k in keys), f"{cfg.name}: no DB_FIRST metrics")
            self.assertTrue(any(k.startswith("NATS_FIRST:") for k in keys), f"{cfg.name}: no NATS_FIRST metrics")
            # Every metric that was recorded must have a full, valid distribution shape.
            for dist in result["distributions"].values():
                self.assertGreater(dist["n"], 0)
                self.assertLessEqual(dist["min"], dist["p50"])
                self.assertLessEqual(dist["p50"], dist["p95"])
                self.assertLessEqual(dist["p95"], dist["p99"])
                self.assertLessEqual(dist["p99"], dist["max"])

    async def test_nats_restart_recovery_scenario_shows_db_first_commits_survive_the_outage(self):
        cfg = ScenarioConfig("nats_restart_recovery_fast", "smoke", iterations=6,
                             pg_stmt_latency_ms=0.05, js_publish_latency_ms=0.05,
                             jetstream_outage_for_first_fraction=0.5)
        result = await run_scenario(cfg)
        self.assertGreater(result["db_first_relay_failures_during_outage"], 0)
        self.assertGreater(result["nats_first_rejections_during_outage"], 0)
        # Despite the outage, every DB-first iteration's PostgreSQL commit eventually gets
        # relayed (all rows converge to PUBLISHED after recovery) and every NATS-first
        # rejection is safely retried with the same deterministic identity.
        db_first_ack = result["distributions"].get("DB_FIRST:bus_publish_ack_ms")
        nats_first_ack = result["distributions"].get("NATS_FIRST:bus_publish_ack_ms")
        self.assertIsNotNone(db_first_ack)
        self.assertIsNotNone(nats_first_ack)
        self.assertEqual(db_first_ack["n"], cfg.iterations)
        self.assertEqual(nats_first_ack["n"], cfg.iterations)


if __name__ == "__main__":
    unittest.main()
