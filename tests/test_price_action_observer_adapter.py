from datetime import datetime, timezone
from unittest.mock import MagicMock

from orchestration.adapters.price_action_observer import PriceActionObserverAdapter


def test_only_observed_research_entries_are_promoted():
    event = {
        "candidate_id": "cand-1",
        "provenance": {
            "rejection_reason": "ENTRY_OBSERVED_RESEARCH_ONLY",
            "entry_cancelled": False, "entry_open": 100.0,
            "stop": 99.0, "target": 103.0, "direction": "LONG",
            "information_cutoff": 1792028700, "parameter_hash": "p",
            "data_snapshot_id": "d",
        },
    }
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(
        "evt-1", "XAUUSD", {"event": event}, datetime.now(timezone.utc)
    )]
    signals = PriceActionObserverAdapter(conn).discover_new_signals(set())
    assert len(signals) == 1
    assert signals[0].strategy_id == "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST"
    assert signals[0].strategy_metadata["research_only"] is True
    assert signals[0].provenance["source_read_health"] is True


def test_seen_signal_is_not_republished():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    assert PriceActionObserverAdapter(conn).discover_new_signals({"SIG_existing"}) == []
