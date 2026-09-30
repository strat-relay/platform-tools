import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from market_data_cache.bridge_guard import (BRIDGE_BACKPRESSURE, BRIDGE_TIMEOUT,
                                            BridgeCircuitBreaker, BridgeCircuitOpen,
                                            BridgeRequestGate, classify_bridge_failure)


class BridgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.breaker = BridgeCircuitBreaker(failure_threshold=3, initial_backoff=5,
                                            max_backoff=20, clock=lambda: self.now[0],
                                            random_source=__import__("random").Random(1))

    def test_global_error_classification(self):
        self.assertEqual(classify_bridge_failure(RuntimeError("EA did not respond; allow WebRequest")),
                         "EA_UNAVAILABLE")
        self.assertEqual(classify_bridge_failure(RuntimeError("bridge backpressure: queue depth 32 >= 32")),
                         BRIDGE_BACKPRESSURE)
        self.assertEqual(classify_bridge_failure(TimeoutError("read timed out")), BRIDGE_TIMEOUT)

    def test_backpressure_trips_global_circuit_without_symbol_counters(self):
        self.breaker.record_failure(RuntimeError("bridge backpressure: queue depth 32 >= 32"),
                                    "mt5_symbol_snapshot")
        self.assertEqual(self.breaker.state, "OPEN")
        self.assertEqual(self.breaker.backpressure_events, 1)
        with self.assertRaises(BridgeCircuitOpen):
            self.breaker.allow("mt5_symbol_snapshot")

    def test_open_circuit_allows_one_probe_after_backoff(self):
        for _ in range(3):
            self.breaker.record_failure(TimeoutError("EA did not respond"), "mt5_symbol_snapshot")
        self.now[0] = self.breaker.next_probe_at + 0.01
        self.breaker.allow("mt5_terminal_info")
        with self.assertRaises(BridgeCircuitOpen):
            self.breaker.allow("mt5_symbol_snapshot")
        self.breaker.record_success("mt5_terminal_info")
        self.assertEqual(self.breaker.state, "CLOSED")

    def test_half_open_failure_reopens_with_backoff(self):
        for _ in range(3):
            self.breaker.record_failure(TimeoutError("timeout"), "mt5_symbol_snapshot")
        self.now[0] = self.breaker.next_probe_at + 0.01
        self.breaker.allow("mt5_terminal_info")
        self.breaker.record_failure(TimeoutError("timeout"), "mt5_terminal_info")
        self.assertEqual(self.breaker.state, "OPEN")
        self.assertGreater(self.breaker.next_probe_at, self.now[0])

    def test_request_gate_bounds_concurrency(self):
        active = [0]
        maximum = [0]
        lock = threading.Lock()

        def slow(_tool, _args):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return {"ok": True}

        gate = BridgeRequestGate(slow, BridgeCircuitBreaker(), max_inflight=2)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: gate.call("mt5_quote", {"symbol": f"S{i}"}), range(6)))
        self.assertLessEqual(maximum[0], 2)

    def test_duplicate_requests_coalesce(self):
        started = threading.Event()
        release = threading.Event()
        calls = [0]

        def slow(_tool, _args):
            calls[0] += 1
            started.set()
            release.wait(1)
            return {"value": 1}

        gate = BridgeRequestGate(slow, BridgeCircuitBreaker(), max_inflight=4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(gate.call, "mt5_symbol_snapshot", {"symbol": "BTCUSDm", "limit": 8})
            self.assertTrue(started.wait(1))
            second = pool.submit(gate.call, "mt5_symbol_snapshot", {"symbol": "BTCUSDm", "limit": 8})
            release.set()
            self.assertEqual(first.result(), second.result())
        self.assertEqual(calls[0], 1)
        self.assertEqual(gate.requests_coalesced_total, 1)


if __name__ == "__main__":
    unittest.main()
