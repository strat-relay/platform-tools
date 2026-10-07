from __future__ import annotations

import unittest
import math
from datetime import datetime, timezone
from decimal import Decimal

from context_structure_retrace_outcome_projector import project_entry_only_outcomes


CUTOFF = "post-t0-cutoff"
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
DECISION = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.rows = []
        if "FROM platform.schema_migrations" in normalized:
            if params == ("015",):
                self.rows = [("015",)]
        elif normalized.startswith("SELECT value FROM platform.system_metadata"):
            value = self.db.metadata.get(params[0])
            self.rows = [(value,)] if value is not None else []
        elif normalized.startswith("INSERT INTO platform.system_metadata"):
            key, cutoff, cutoff_id, strategy_id, outcome_type, _updated_at = params
            self.db.metadata.setdefault(key, {"cutoff_utc": cutoff, "signal_cutoff_id": cutoff_id,
                                              "strategy_id": strategy_id, "outcome_type": outcome_type})
        elif "FROM strategy.entry_signals" in normalized:
            strategy_id, cutoff_id = params
            self.rows = [(row[0], row[2], row[3], row[5] if len(row) > 5 else None,
                          row[6] if len(row) > 6 else DECISION) for row in self.db.signals
                         if row[1] == strategy_id and row[4] == cutoff_id]
        elif normalized.startswith("INSERT INTO strategy.entry_signal_outcomes"):
            signal_id, outcome_type, status, realized_r, exit_at, source, updated_at = params
            existing = self.db.outcomes.get(signal_id)
            proposed = (outcome_type, status, realized_r, exit_at, source)
            if existing is None:
                self.db.outcomes[signal_id] = proposed
                self.rows = [(signal_id,)]
            elif existing != proposed:
                same_numeric = ((existing[2] is None and proposed[2] is None) or
                                (existing[2] is not None and proposed[2] is not None and
                                 math.isclose(float(existing[2]), float(proposed[2]),
                                              rel_tol=1e-12, abs_tol=1e-12)))
                same_projection = (existing[0] == proposed[0] and existing[1] == proposed[1]
                                   and same_numeric and existing[3] == proposed[3]
                                   and existing[4] == proposed[4])
                if same_projection:
                    return
                self.db.outcomes[signal_id] = proposed
                self.rows = [(signal_id,)]
        elif normalized.startswith("SELECT status FROM strategy.entry_signal_outcomes"):
            existing = self.db.outcomes.get(params[0])
            self.rows = [(existing[1],)] if existing else []
        elif normalized.startswith("SELECT outcome_type, status, realized_r"):
            existing = self.db.outcomes.get(params[0])
            self.rows = [existing] if existing else []
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return Cursor(self.db)

    def commit(self):
        self.db.commits += 1


class FakeDB:
    def __init__(self):
        self.signals = [
            ("SIG-TARGET", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-TARGET", "OP-TARGET", CUTOFF, None, DECISION),
            ("SIG-STOP", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-STOP", "OP-STOP", CUTOFF, None, DECISION),
            ("SIG-OPEN", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-OPEN", "OP-OPEN", CUTOFF, None, DECISION),
            ("SIG-INVALIDATED", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-INVALIDATED", "OP-INVALIDATED", CUTOFF, None, DECISION),
            # A canonical row from a different cutoff must not enter this projection.
            ("SIG-OLD", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-OLD", "OP-OLD", "other-cutoff", None, NOW),
        ]
        self.metadata = {}
        self.outcomes = {}
        self.commits = 0

    def connect(self, **_kwargs):
        return Connection(self)


def runner_state():
    return {"positions": {
        "POS-TARGET": {"economic_position_id": "POS-TARGET", "entry_opportunity_id": "OP-TARGET",
                       "status": "TARGET_HIT", "realized_R": 0.7428190594695953,
                       "exit_timestamp": 1790058900},
        "POS-STOP": {"economic_position_id": "POS-STOP", "entry_opportunity_id": "OP-STOP",
                     "status": "STOPPED", "realized_R": -1.0, "exit_timestamp": 1790059200},
        "POS-OPEN": {"economic_position_id": "POS-OPEN", "entry_opportunity_id": "OP-OPEN",
                     "status": "OPEN", "realized_R": None, "exit_timestamp": None},
        "POS-INVALIDATED": {"economic_position_id": "POS-INVALIDATED", "entry_opportunity_id": "OP-INVALIDATED",
                             "status": "INVALIDATED", "realized_R": None, "exit_timestamp": 1790059500},
        "POS-OLD": {"economic_position_id": "POS-OLD", "entry_opportunity_id": "OP-OLD",
                    "status": "TARGET_HIT", "realized_R": 99.0, "exit_timestamp": 1790058900},
    }}


class EntryOnlyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.kwargs = {"connect_fn": self.db.connect,
                       "environ": {"ENTRY_OUTCOME_SIGNAL_CUTOFF_ID": CUTOFF},
                       "clock": lambda: NOW}

    def test_projects_target_stop_open_and_invalidated_without_importing_other_cutoffs(self):
        result = project_entry_only_outcomes(runner_state(), **self.kwargs)
        self.assertEqual(result, {"matched": 4, "projected": 4, "unchanged": 0, "unmatched": 0})
        target = self.db.outcomes["SIG-TARGET"]
        self.assertEqual(target[:3], ("ENTRY_ONLY", "TARGET_HIT", 0.7428190594695953))
        self.assertEqual(target[3], datetime.fromtimestamp(1790058900, timezone.utc))
        self.assertEqual(self.db.outcomes["SIG-STOP"][1:3], ("STOPPED", -1.0))
        self.assertEqual(self.db.outcomes["SIG-OPEN"][1:4], ("OPEN", None, None))
        self.assertEqual(self.db.outcomes["SIG-INVALIDATED"][1:4],
                         ("INVALIDATED", None, datetime.fromtimestamp(1790059500, timezone.utc)))
        self.assertNotIn("SIG-OLD", self.db.outcomes)
        self.assertEqual(self.db.metadata["context.entry_only_outcome_cutoff"]["signal_cutoff_id"], CUTOFF)

    def test_repeated_projection_is_idempotent(self):
        first = project_entry_only_outcomes(runner_state(), **self.kwargs)
        second = project_entry_only_outcomes(runner_state(), **self.kwargs)
        self.assertEqual(first["projected"], 4)
        self.assertEqual(second, {"matched": 4, "projected": 0, "unchanged": 4, "unmatched": 0})
        self.assertEqual(len(self.db.outcomes), 4)

    def test_numeric_round_trip_does_not_conflict_on_float_representation(self):
        project_entry_only_outcomes(runner_state(), **self.kwargs)
        stored = self.db.outcomes["SIG-TARGET"]
        # PostgreSQL NUMERIC retains the decimal representation produced by
        # the driver; reading it back as float can differ by a few ULPs.
        self.db.outcomes["SIG-TARGET"] = (*stored[:2], Decimal("0.742819059469595"), *stored[3:])

        result = project_entry_only_outcomes(runner_state(), **self.kwargs)

        self.assertEqual(result, {"matched": 4, "projected": 0, "unchanged": 4, "unmatched": 0})
        self.assertEqual(self.db.outcomes["SIG-TARGET"][2], Decimal("0.742819059469595"))

    def test_runner_state_overwrites_a_stale_terminal_database_outcome(self):
        state = {"positions": {"POS-STALE": {
            "economic_position_id": "POS-STALE",
            "entry_opportunity_id": "OPP-STALE",
            "status": "OPEN",
        }}}
        self.db.signals.append(("SIG-STALE", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-STALE", "OPP-STALE", CUTOFF, None, NOW))
        self.db.outcomes["SIG-STALE"] = (
            "ENTRY_ONLY", "INVALIDATED", None,
            datetime.fromtimestamp(1790059500, timezone.utc),
            "CONTEXT_STRUCTURE_RETRACE_V1",
        )

        result = project_entry_only_outcomes(state, **self.kwargs)

        self.assertEqual(result, {"matched": 1, "projected": 1, "unchanged": 0, "unmatched": 4})
        self.assertEqual(self.db.outcomes["SIG-STALE"][1], "OPEN")

    def test_time_exit_is_not_overwritten_by_runner_open_state(self):
        self.db.signals.append(("SIG-TIME", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-TIME",
                                "OP-TIME", CUTOFF, None, NOW))
        self.db.outcomes["SIG-TIME"] = (
            "ENTRY_ONLY", "TIME_EXIT", -0.25,
            datetime.fromtimestamp(1790059500, timezone.utc),
            "CONTEXT_STRUCTURE_RETRACE_V1",
        )
        state = {"positions": {"POS-TIME": {
            "economic_position_id": "POS-TIME", "entry_opportunity_id": "OP-TIME",
            "status": "OPEN", "realized_R": None, "exit_timestamp": None,
        }}}

        result = project_entry_only_outcomes(state, **self.kwargs)

        self.assertEqual(result, {"matched": 1, "projected": 0, "unchanged": 1, "unmatched": 4})
        self.assertEqual(self.db.outcomes["SIG-TIME"][1], "TIME_EXIT")

    def test_entry_opportunity_mismatch_is_not_projected(self):
        state = runner_state()
        state["positions"]["POS-TARGET"]["entry_opportunity_id"] = "DIFFERENT"
        result = project_entry_only_outcomes(state, **self.kwargs)
        self.assertEqual(result["unmatched"], 1)
        self.assertNotIn("SIG-TARGET", self.db.outcomes)

    def test_entry_only_outcome_uses_decision_time_not_later_emission_time(self):
        self.db.signals.append((
            "SIG-LATE-EMISSION", "CONTEXT_STRUCTURE_RETRACE_V1", "POS-LATE",
            "OP-LATE", CUTOFF,
            datetime(2026, 9, 24, 20, 40, 26, 108663, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 20, 35, tzinfo=timezone.utc),
        ))
        state = {"positions": {"POS-LATE": {
            "economic_position_id": "POS-LATE",
            "entry_opportunity_id": "OP-LATE",
            "status": "TARGET_HIT",
            "realized_R": 1.0,
            "exit_timestamp": datetime(2026, 9, 24, 20, 40, tzinfo=timezone.utc).timestamp(),
        }}}

        result = project_entry_only_outcomes(state, **self.kwargs)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(self.db.outcomes["SIG-LATE-EMISSION"][1], "TARGET_HIT")


if __name__ == "__main__":
    unittest.main()
