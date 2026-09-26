"""Historical reconciliation of orphaned OPEN Context positions (scripts/reconcile_context_orphans.py).

Replays only the bars the runner already passed (fill .. last_m5) through the runner's own exit
rule, so orphans get their true historical exit; never guesses missing identity; idempotent.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from scripts.reconcile_context_orphans import fetch_m5, orphans, reconcile  # noqa: E402
from test_context_runner_position_lifecycle import (FILL, TARGET_R, bar, position,  # noqa: E402
                                                    state_with_filled_setup)


class FakeReadClient:
    """Stands in for Mt5ReadClient.rates_range (read-only)."""

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def rates_range(self, symbol, timeframe, start, end, page_size=500, completed_only=True):
        self.calls.append((symbol, timeframe, start, end))
        rows = [b for b in self.bars if start <= b["time"] < end]
        return {"rates": rows[:page_size]}


def orphaned_state(last_m5):
    state = state_with_filled_setup()
    state["setups"]["SETUP1"]["status"] = "INVALIDATED_NO_REENTRY"   # orphaned by the old runner
    state["symbols"] = {"EURUSDm": {"last_m5": last_m5}}
    return state


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, state):
        self.events.append(event)


class ReconcileTests(unittest.TestCase):
    def test_orphan_closes_at_its_true_historical_bar(self):
        bars = [bar(FILL + 300 * i, 1.1001, 1.1012, 1.1005) for i in range(1, 5)]
        bars.append(bar(FILL + 300 * 5, 1.1002, 1.1033, 1.1030))           # target bar
        bars.append(bar(FILL + 300 * 6, 1.0900, 1.1003, 1.0950))           # later stop (must not matter)
        state = orphaned_state(last_m5=FILL + 300 * 6)
        rec = Recorder()
        report = reconcile(state, FakeReadClient(bars), record_event=rec)
        pos = state["setups"]["SETUP1"]["opportunities"][0]
        self.assertEqual((pos["status"], pos["exit_timestamp"], pos["realized_R"]), ("TARGET_HIT", FILL + 1500, TARGET_R))
        self.assertEqual([e["type"] for e in rec.events], ["TARGET_HIT"])
        self.assertEqual(rec.events[0]["source"], "HISTORICAL_RECONCILIATION")
        self.assertEqual(len(report["closed"]), 1)

    def test_only_bars_up_to_the_runner_cursor_are_replayed(self):
        bars = [bar(FILL + 300, 1.1001, 1.1012, 1.1005), bar(FILL + 600, 1.0900, 1.1003, 1.0950)]
        state = orphaned_state(last_m5=FILL + 300)        # the stop bar is after the cursor: runner's job
        client = FakeReadClient(bars)
        report = reconcile(state, client, record_event=Recorder())
        self.assertEqual(state["setups"]["SETUP1"]["opportunities"][0]["status"], "OPEN")
        self.assertEqual(report["still_open"][0]["bars_replayed"], 1)
        self.assertEqual(client.calls[0][2:], (FILL + 1, FILL + 301))

    def test_filled_setups_and_legacy_positions_are_not_guessed(self):
        state = state_with_filled_setup()                  # setup still FILLED: runner evaluates it itself
        state["symbols"] = {"EURUSDm": {"last_m5": FILL + 3000}}
        state["positions"]["LEGACY"] = position("LEGACY", with_identity=False)
        report = reconcile(state, FakeReadClient([bar(FILL + 300, 1.0, 2.0, 1.5)]), record_event=Recorder())
        self.assertEqual(report["closed"], [])
        self.assertEqual([x["position"] for x in report["insufficient_state"]], ["LEGACY"])
        self.assertEqual(state["setups"]["SETUP1"]["opportunities"][0]["status"], "OPEN")
        self.assertEqual(state["positions"]["LEGACY"]["status"], "OPEN")

    def test_dry_run_on_a_copy_leaves_the_original_untouched_and_rerun_is_a_no_op(self):
        bars = [bar(FILL + 300, 1.0990, 1.1003, 1.0992)]  # stop
        live = orphaned_state(last_m5=FILL + 300)
        dry = copy.deepcopy(live)
        reconcile(dry, FakeReadClient(bars), record_event=Recorder())
        self.assertEqual(live["setups"]["SETUP1"]["opportunities"][0]["status"], "OPEN")
        self.assertEqual(dry["setups"]["SETUP1"]["opportunities"][0]["status"], "STOPPED")
        rec = Recorder()
        again = reconcile(dry, FakeReadClient(bars), record_event=rec)
        self.assertEqual((again["closed"], rec.events), ([], []))

    def test_paging_collects_all_bars(self):
        bars = [bar(FILL + 300 * i, 1.1001, 1.1012, 1.1005) for i in range(1, 1201)]
        got = fetch_m5(FakeReadClient(bars), "EURUSDm", FILL, FILL + 300 * 1200)
        self.assertEqual(len(got), 1200)

    def test_orphan_discovery_matches_the_runner(self):
        state = orphaned_state(last_m5=FILL)
        self.assertEqual([(s, p["economic_position_id"]) for s, _, p in orphans(state)], [("EURUSDm", "POS1")])


class OnceMarkerTests(unittest.TestCase):
    def test_apply_with_existing_marker_is_skipped_without_touching_state(self):
        import tempfile
        from unittest.mock import patch
        import scripts.reconcile_context_orphans as mod
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "done"
            marker.write_text("{}")
            with patch.object(mod.fwd, "load_state", side_effect=AssertionError("must not load state")):
                self.assertEqual(mod.main(["--mcp-url", "http://unused", "--apply", "--once-marker", str(marker)]), 0)


if __name__ == "__main__":
    unittest.main()
