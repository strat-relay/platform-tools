import json
import logging
import unittest

from observability.strategy_audit import LOGGER, audit


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class StrategyAuditTests(unittest.TestCase):
    def test_audit_event_is_one_structured_json_line(self):
        handler = _Capture()
        old_level = LOGGER.level
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        try:
            audit("signal_received", runner="signal-orchestrator", signal_id="SIG-1",
                  strategy_id="CONTEXT_STRUCTURE_RETRACE_V1", entry=100.0)
        finally:
            LOGGER.removeHandler(handler)
            LOGGER.setLevel(old_level)

        self.assertEqual(len(handler.messages), 1)
        event = json.loads(handler.messages[0])
        self.assertEqual(event["audit"], "strategy")
        self.assertEqual(event["event"], "signal_received")
        self.assertEqual(event["runner"], "signal-orchestrator")
        self.assertEqual(event["signal_id"], "SIG-1")
        self.assertIn("timestamp", event)


if __name__ == "__main__":
    unittest.main()
