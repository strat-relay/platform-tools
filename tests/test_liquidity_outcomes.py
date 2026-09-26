import unittest

from liquidity_outcomes import project_liquidity_outcome


class Cursor:
    def __init__(self, signal_strategy="LIQUIDITY_DISPLACEMENT_SCALP_V1"):
        self.signal_strategy = signal_strategy
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchone(self):
        return (self.signal_strategy,) if len(self.calls) == 1 else ("signal",)


class Conn:
    def __init__(self, strategy="LIQUIDITY_DISPLACEMENT_SCALP_V1"):
        self.cursor_obj = Cursor(strategy)
        self.committed = False
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self.cursor_obj
    def commit(self): self.committed = True


class LiquidityOutcomeTests(unittest.TestCase):
    def test_open_has_no_exit_or_realized_value(self):
        conn = Conn()
        self.assertTrue(project_liquidity_outcome("sig", status="OPEN", realized_r=None,
                                                  exit_timestamp=None, connect_fn=lambda: conn))
        self.assertTrue(conn.committed)

    def test_terminal_price_outcome_requires_both_values(self):
        with self.assertRaises(ValueError):
            project_liquidity_outcome("sig", status="TARGET_HIT", realized_r=None,
                                      exit_timestamp="2026-09-26T12:00:00Z", connect_fn=lambda: Conn())

    def test_non_liquidity_signal_is_rejected_before_insert(self):
        with self.assertRaises(ValueError):
            project_liquidity_outcome("sig", status="OPEN", realized_r=None,
                                      exit_timestamp=None,
                                      connect_fn=lambda: Conn("CONTEXT_STRUCTURE_RETRACE_V1"))


if __name__ == "__main__":
    unittest.main()
