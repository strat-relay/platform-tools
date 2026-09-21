import json
import tempfile
import unittest
from pathlib import Path

from trade_manager.fanout import FanoutConsumer, SharedObservationPublisher, market_envelope, position_event
from trade_manager.stream_consumer import SharedStreamTradeManager


def pos(pid="p1", created="2026-09-17T12:00:00+00:00", symbol="XAUUSDm"):
    return {"economic_position_id": pid, "setup_id": "s-" + pid, "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
            "symbol": symbol, "direction": "SHORT", "entry": 100.0, "entry_timestamp": created,
            "original_stop": 105.0, "current_stop": 105.0, "target": 90.0, "size": 1.0, "status": "OPEN"}


def market(pid="p1", ts="2026-09-17T12:01:00+00:00"):
    return market_envelope("XAUUSDm", observed_at=ts, source_timestamp=ts, bid=99.0, ask=99.2,
                           spread=.2, m1=None, m5=None, context={}, economic_position_id=pid,
                           setup_id="s-" + pid, strategy_id="CONTEXT_STRUCTURE_RETRACE_V1")


class FanoutTests(unittest.TestCase):
    def test_independent_consumers_and_duplicate_suppression(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            e = market()
            self.assertTrue(p.publish(e)); self.assertTrue(p.publish(e))
            a, b = FanoutConsumer("a", d), FanoutConsumer("b", d)
            self.assertEqual(len(a.read()), 1)
            self.assertEqual(len(b.read()), 1)
            self.assertEqual(a.checkpoint, b.checkpoint)
            self.assertNotEqual(a.checkpoint_path, b.checkpoint_path)

    def test_checkpoint_restart_and_gap_detection(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            p.publish(market(ts="2026-09-17T12:01:00+00:00"))
            c = FanoutConsumer("collector", d)
            self.assertEqual(len(c.read()), 1)
            self.assertEqual(FanoutConsumer("collector", d).read(), [])
            # A missing sequence is observable, never silently treated as history.
            path = Path(d) / "events.jsonl"
            row = market(pid="p2", ts="2026-09-17T12:02:00+00:00")
            row["source"]["sequence"] = 3
            path.write_text(path.read_text() + json.dumps(row) + "\n")
            c2 = FanoutConsumer("gap", d)
            c2.read()
            self.assertTrue(c2.gaps)

    def test_lifecycle_identity_supports_same_symbol_positions(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            p.publish(position_event("POSITION_OPENED", pos("a"), observed_at="2026-09-17T12:00:00Z"))
            p.publish(position_event("POSITION_OPENED", pos("b"), observed_at="2026-09-17T12:00:00Z"))
            rows = FanoutConsumer("x", d).read()
            self.assertEqual({r["economic_position_id"] for r in rows}, {"a", "b"})

    def test_phase7_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            p.events_path = Path(d) / "missing" / "events.jsonl"
            self.assertFalse(p.publish(market()))

    def test_actual_collection_start_excludes_prestart_positions(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            p.publish(position_event("POSITION_OPENED", pos("old", "2026-09-17T11:00:00Z"), observed_at="2026-09-17T12:00:00Z"))
            p.publish(market("old", "2026-09-17T12:00:01Z"))
            manager = SharedStreamTradeManager({"trade_manager_activation_cutoff": "2026-09-17T11:05:34Z"}, d)
            manager.start("2026-09-17T12:00:00Z")
            result = manager.process_once()
            self.assertEqual(result["processed_observations"], 0)
            self.assertEqual(manager.started_at, "2026-09-17T12:00:00Z")

    def test_inactive_collector_does_not_consume(self):
        with tempfile.TemporaryDirectory() as d:
            SharedObservationPublisher(d).publish(position_event("POSITION_OPENED", pos(), observed_at="2026-09-17T12:00:00Z"))
            manager = SharedStreamTradeManager({}, d)
            result = manager.process_once()
            self.assertTrue(result["inactive"])
            self.assertEqual(manager.consumer.checkpoint, 0)

    def test_zero_broker_writes(self):
        with tempfile.TemporaryDirectory() as d:
            p = SharedObservationPublisher(d)
            p.publish(market())
            self.assertEqual(FanoutConsumer("a", d).health()["broker_writes"], 0)


if __name__ == "__main__":
    unittest.main()
