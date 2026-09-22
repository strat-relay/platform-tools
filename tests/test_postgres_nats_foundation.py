from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import unittest

from core.strategies.evaluation import Decision, DecisionTrace, Evaluation, TraceFidelity
from infrastructure.messaging.contracts import EventEnvelope, STREAMS, SUBJECTS, validate_subject
from infrastructure.messaging.jetstream import JetStreamPublisher, JetStreamTopology
from infrastructure.messaging.testing import InMemoryJetStream


ROOT = Path(__file__).resolve().parents[1]


class FoundationTests(unittest.TestCase):
    def test_migration_is_forward_and_contains_required_relations(self):
        migration = ROOT / "postgres/migrations/008_stratrelay_foundation.sql"
        self.assertTrue(migration.exists())
        text = migration.read_text()
        for relation in ("strategy.evaluations", "strategy.decision_traces", "strategy.stage_results",
                         "platform.outbox_events", "platform.inbox_events", "platform.ownership_leases",
                         "platform.runtime_instances", "execution.intents", "execution.broker_state_current"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {relation}", text)
        self.assertIn("STALE_FENCING_GENERATION", text)

    def test_domain_has_no_database_or_nats_driver_dependency(self):
        forbidden = {"psycopg", "nats", "asyncio_nats"}
        for path in (ROOT / "core").rglob("*.py"):
            tree = ast.parse(path.read_text())
            imports = {node.module for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module}
            imports |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                        for alias in node.names}
            self.assertTrue(forbidden.isdisjoint(imports), path)

    def test_envelope_subjects_are_versioned_and_deterministic(self):
        envelope = EventEnvelope("event-1", "signal.entry.created.v1", "signal", "sig-1", 1,
                                 "2026-09-21T00:00:00Z", {"b": 2, "a": 1})
        same = EventEnvelope("event-1", "signal.entry.created.v1", "signal", "sig-1", 1,
                             "2026-09-21T00:00:00Z", {"a": 1, "b": 2})
        self.assertEqual(envelope.canonical_bytes(), same.canonical_bytes())
        self.assertIn("signal.entry.created.v1", SUBJECTS)
        with self.assertRaises(ValueError):
            validate_subject("signal.entry.created")

    def test_topology_has_separate_core_and_execution_streams(self):
        # TRADING_OBSERVATION (architecture/p4-2-managed-trade) is a third, deliberately
        # isolated stream for the (not activated) trade-observation hot path; additive, does
        # not change TRADING_CORE/EXECUTION's own subject sets below.
        self.assertEqual(set(STREAMS), {"TRADING_CORE", "EXECUTION", "TRADING_OBSERVATION"})
        self.assertTrue(all(x.endswith(".v1") for x in STREAMS["TRADING_CORE"]["subjects"]))
        self.assertTrue(all(x.endswith(".v1") for x in STREAMS["EXECUTION"]["subjects"]))
        self.assertTrue(all(x.endswith(".v1") for x in STREAMS["TRADING_OBSERVATION"]["subjects"]))
        self.assertEqual(set(JetStreamTopology.v1().streams), set(STREAMS))

    def test_topology_creation_is_idempotent(self):
        class Manager:
            def __init__(self):
                self.created = {}
                self.calls = []

            async def stream_info(self, name):
                if name not in self.created:
                    raise RuntimeError("missing")
                return self.created[name]

            async def add_stream(self, **config):
                self.calls.append(config)
                self.created[config["name"]] = config

        async def run():
            manager = Manager()
            topology = JetStreamTopology.v1()
            await topology.ensure(manager)
            await topology.ensure(manager)
            self.assertEqual(len(manager.calls), 3)
            self.assertEqual({x["name"] for x in manager.calls}, {"TRADING_CORE", "EXECUTION", "TRADING_OBSERVATION"})
        asyncio.run(run())

    def test_publish_and_redelivery_harness(self):
        async def run():
            broker = InMemoryJetStream()
            publisher = JetStreamPublisher(broker)
            event = EventEnvelope("event-1", "signal.entry.created.v1", "signal", "sig-1", 1,
                                  "2026-09-21T00:00:00Z", {"signal_id": "sig-1"})
            await publisher.publish(event)
            first = await broker.next(event.event_type)
            redelivered = await broker.redeliver(event.event_type)
            self.assertEqual(redelivered.deliveries, 2)
            self.assertEqual(first.payload, redelivered.payload)
            await broker.ack(redelivered)
            self.assertEqual(len(broker.messages[event.event_type]), 0)
        asyncio.run(run())

    def test_evaluation_payload_is_ready_for_round_trip(self):
        trace = DecisionTrace((), Decision.NO_CANDIDATE, fidelity=TraceFidelity.L0)
        evaluation = Evaluation("S", "XAUUSDm", "2026-09-21T00:00:00Z", Decision.NO_CANDIDATE,
                                trace, trace_fidelity=TraceFidelity.L0)
        payload = evaluation.to_dict()
        self.assertEqual(evaluation.evaluation_hash, Evaluation(**{
            "strategy_id": payload["strategy_id"], "instrument": payload["instrument"],
            "decision_time": payload["decision_time"], "decision": Decision(payload["decision"]),
            "trace": trace, "trace_fidelity": TraceFidelity(payload["trace_fidelity"]),
            "strategy_version": payload["strategy_version"], "parameter_set_id": payload["parameter_set_id"],
            "candidate_id": payload["candidate_id"], "reason_codes": (),
            "runtime_version": payload["runtime_version"], "evaluator_version": payload["evaluator_version"],
            "provenance": payload["provenance"], "schema_version": payload["schema_version"]}).evaluation_hash)


if __name__ == "__main__":
    unittest.main()
