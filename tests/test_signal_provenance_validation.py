"""Tests for _validate_signal_provenance (Phase 2B, B-1).

Verifies that the orchestrator fails closed in REAL_EXECUTION when
source_read_health is absent from a signal's provenance, and warns
(without blocking) in non-REAL modes.
"""
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.models import StrategySignal
import signal_orchestrator as so


def _make_signal(
    provenance: dict,
    decision_time: Optional[str] = "2026-10-10T11:00:00Z",
    signal_emitted_at: Optional[str] = "2026-10-10T12:00:00Z",
) -> StrategySignal:
    return StrategySignal(
        signal_id="SIG_test",
        schema_version="strategy-signal-v1",
        strategy_id="KOJO_STRUCTURE_RECLAIM_V3",
        strategy_version="V3",
        strategy_instance_id="kojo-v3-inst1",
        source_event_id="event-1",
        market_event_id=None,
        setup_id=None,
        entry_opportunity_id=None,
        economic_position_id=None,
        created_at="2026-10-10T12:00:00Z",
        signal_timestamp="2026-10-10T11:00:00Z",
        symbol="XAUUSDm",
        canonical_symbol="XAUUSD",
        broker_symbol_hint="XAUUSDm",
        direction="LONG",
        entry_type="MARKET",
        entry_price=1900.0,
        stop_price=1895.0,
        target_price=1910.0,
        risk_distance=5.0,
        target_distance=10.0,
        target_r=2.0,
        timeframe="H1",
        lower_timeframe="M15",
        higher_timeframes=("H4",),
        entry_mechanism=("STRUCTURE_RECLAIM",),
        provenance=provenance,
        decision_time=decision_time,
        signal_emitted_at=signal_emitted_at,
    )


# ---------------------------------------------------------------------------
# Absent source_read_health
# ---------------------------------------------------------------------------

def test_fails_closed_in_real_execution_when_provenance_absent():
    signal = _make_signal({})
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_fails_closed_in_real_execution_when_source_read_health_is_none():
    signal = _make_signal({"source_read_health": None})
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


# ---------------------------------------------------------------------------
# Non-REAL modes: warn but allow
# ---------------------------------------------------------------------------

def test_allows_in_shadow_mode_with_audit_when_provenance_absent():
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    signal = _make_signal({})
    with patch.object(so, "audit", side_effect=fake_audit):
        result = so._validate_signal_provenance(signal, "SHADOW")

    assert result is True
    assert len(audit_events) == 1
    name, kwargs = audit_events[0]
    assert name == "signal_provenance_incomplete"
    assert kwargs["orchestration_mode"] == "SHADOW"
    assert "source_read_health" in kwargs["missing_fields"]


def test_allows_in_primary_mode_with_audit_when_provenance_absent():
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    signal = _make_signal({})
    with patch.object(so, "audit", side_effect=fake_audit):
        result = so._validate_signal_provenance(signal, "PRIMARY")

    assert result is True
    assert len(audit_events) == 1


# ---------------------------------------------------------------------------
# Valid provenance passes all modes
# ---------------------------------------------------------------------------

def test_passes_real_execution_when_source_read_health_is_true():
    signal = _make_signal({"source_read_health": True})
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is True


def test_passes_real_execution_when_source_read_health_is_false():
    # False = health known but unhealthy; downstream handles it.
    # The orchestrator only blocks absent (None/missing) health.
    signal = _make_signal({"source_read_health": False})
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is True


def test_passes_shadow_when_source_read_health_is_true():
    audit_events: list[tuple] = []
    signal = _make_signal({"source_read_health": True})
    with patch.object(so, "audit", side_effect=lambda *a, **k: audit_events.append(a)):
        result = so._validate_signal_provenance(signal, "SHADOW")
    assert result is True
    assert audit_events == []


def test_does_not_audit_when_provenance_is_present():
    audit_events: list[tuple] = []
    signal = _make_signal({"source_read_health": True, "source_market_data_timestamp": "2026-10-10T11:00:00Z"})
    with patch.object(so, "audit", side_effect=lambda *a, **k: audit_events.append(a)):
        so._validate_signal_provenance(signal, "SHADOW")
    assert audit_events == []


# ---------------------------------------------------------------------------
# Timestamp fields (A-11)
# ---------------------------------------------------------------------------

def test_fails_closed_in_real_execution_when_decision_time_absent():
    signal = _make_signal({"source_read_health": True}, decision_time=None)
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_fails_closed_in_real_execution_when_signal_emitted_at_absent():
    signal = _make_signal({"source_read_health": True}, signal_emitted_at=None)
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_warns_and_allows_in_shadow_when_timestamps_absent():
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    signal = _make_signal({"source_read_health": True}, decision_time=None, signal_emitted_at=None)
    with patch.object(so, "audit", side_effect=fake_audit):
        result = so._validate_signal_provenance(signal, "SHADOW")

    assert result is True
    assert len(audit_events) == 1
    name, kwargs = audit_events[0]
    assert name == "signal_provenance_incomplete"
    assert "decision_time" in kwargs["missing_fields"]
    assert "signal_emitted_at" in kwargs["missing_fields"]


def test_passes_real_execution_when_all_fields_present():
    signal = _make_signal({"source_read_health": True})
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is True


def test_all_three_missing_fields_reported_in_one_audit_event():
    """A single audit event names all three absent fields together."""
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    signal = _make_signal({}, decision_time=None, signal_emitted_at=None)
    with patch.object(so, "audit", side_effect=fake_audit):
        so._validate_signal_provenance(signal, "PRIMARY")

    assert len(audit_events) == 1
    _, kwargs = audit_events[0]
    missing = kwargs["missing_fields"]
    assert "source_read_health" in missing
    assert "decision_time" in missing
    assert "signal_emitted_at" in missing
