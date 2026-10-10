"""Tests for Liquidity Live provenance derivation and orchestrator gate integration.

Covers:
- _snapshot_health() derivation for bridge and cache paths
- Healthy signals pass REAL_EXECUTION gate
- Unhealthy / stale / gap-recovery signals blocked in REAL_EXECUTION
- SHADOW allows all with audit
- tick() guard on adapter-mode runtime (B-3)
"""
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.liquidity_live import _snapshot_health, LIVE_MARKET_VALIDATORS, _MAX_DATA_AGE_SECONDS
from liquidity_market_data import LiveMarketSnapshot
import signal_orchestrator as so


# ---------------------------------------------------------------------------
# Snapshot health derivation
# ---------------------------------------------------------------------------

def _bridge_snapshot(**overrides: Any) -> LiveMarketSnapshot:
    """A bridge-sourced snapshot (data_health=None)."""
    defaults = dict(
        M5=(), M15=(), quote={}, contract={},
        canonical_instrument="XAUUSD", provider_symbol="XAUUSDm",
        source_market_data_timestamp="2026-10-10T12:00:00Z",
        source_kind="LIVE_MARKET",
        validated_by="mt5-read-22347",
        data_health=None,
    )
    defaults.update(overrides)
    return LiveMarketSnapshot(**defaults)


def _cache_snapshot(data_health: dict[str, Any], **overrides: Any) -> LiveMarketSnapshot:
    """A cache-sourced snapshot (data_health dict set)."""
    defaults = dict(
        M5=(), M15=(), quote={}, contract={},
        canonical_instrument="XAUUSD", provider_symbol="XAUUSDm",
        source_market_data_timestamp="2026-10-10T12:00:00Z",
        source_kind="LIVE_MARKET",
        validated_by="market-data-cache",
        data_health=data_health,
    )
    defaults.update(overrides)
    return LiveMarketSnapshot(**defaults)


def test_bridge_path_is_always_healthy():
    snap = _bridge_snapshot()
    health = _snapshot_health(snap)
    assert health["source_read_health"] is True
    assert health["gap_recovery"] is False
    assert health["source_market_data_timestamp"] == "2026-10-10T12:00:00Z"


def test_cache_path_healthy_when_all_good():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "OK",
        "recovery_status": "NONE",
        "cache_age_seconds": 10.0,
        "source": "redis",
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is True
    assert health["gap_recovery"] is False


def test_cache_path_unhealthy_when_continuity_degraded():
    snap = _cache_snapshot({
        "continuity_status": "DEGRADED",
        "gap_status": "OK",
        "recovery_status": "NONE",
        "cache_age_seconds": 10.0,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is False


def test_cache_path_unhealthy_when_gap_detected():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "GAP_DETECTED",
        "recovery_status": "NONE",
        "cache_age_seconds": 10.0,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is False


def test_cache_path_unhealthy_and_gap_recovery_true_when_recovering():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "OK",
        "recovery_status": "RECOVERING",
        "cache_age_seconds": 10.0,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is False
    assert health["gap_recovery"] is True


def test_cache_path_unhealthy_when_too_old():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "OK",
        "recovery_status": "NONE",
        "cache_age_seconds": _MAX_DATA_AGE_SECONDS + 1,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is False


def test_cache_path_healthy_at_exact_max_age():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "OK",
        "recovery_status": "NONE",
        "cache_age_seconds": _MAX_DATA_AGE_SECONDS,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is True


def test_cache_path_unknown_continuity_is_unhealthy():
    snap = _cache_snapshot({
        "continuity_status": "UNKNOWN",
        "gap_status": "UNKNOWN",
        "recovery_status": "NONE",
        "cache_age_seconds": 5.0,
    })
    health = _snapshot_health(snap)
    assert health["source_read_health"] is False


def test_snapshot_health_carries_diagnostic_fields():
    snap = _cache_snapshot({
        "continuity_status": "HEALTHY",
        "gap_status": "OK",
        "recovery_status": "NONE",
        "cache_age_seconds": 30.0,
        "source": "redis",
    })
    health = _snapshot_health(snap)
    assert "source_continuity_status" in health
    assert "source_gap_status" in health
    assert "source_recovery_status" in health
    assert "source_data_age_seconds" in health


# ---------------------------------------------------------------------------
# Gate integration: healthy Liquidity Live signal passes REAL_EXECUTION
# ---------------------------------------------------------------------------

def _make_ll_signal(provenance: dict[str, Any],
                    decision_time: str = "2026-10-10T11:00:00Z",
                    signal_emitted_at: str = "2026-10-10T12:00:00Z") -> so.StrategySignal:
    from orchestration.models import StrategySignal
    return StrategySignal(
        signal_id="SIG_ll_test",
        schema_version="strategy-signal-v1",
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        strategy_version="V1",
        strategy_instance_id="liquidity-xau-base",
        source_event_id="event-ll-1",
        market_event_id=None, setup_id=None,
        entry_opportunity_id=None, economic_position_id=None,
        created_at="2026-10-10T12:00:00Z",
        signal_timestamp="2026-10-10T11:00:00Z",
        symbol="XAUUSDm", canonical_symbol="XAUUSD", broker_symbol_hint="XAUUSDm",
        direction="LONG", entry_type="MARKET",
        entry_price=1900.0, stop_price=1895.0, target_price=1910.0,
        risk_distance=5.0, target_distance=10.0, target_r=2.0,
        timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
        entry_mechanism=("LIQUIDITY_SWEEP",),
        provenance=provenance,
        decision_time=decision_time,
        signal_emitted_at=signal_emitted_at,
    )


def test_healthy_liquidity_signal_passes_real_execution():
    signal = _make_ll_signal({
        "source_read_health": True,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": False,
    })
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is True


def test_unhealthy_source_read_health_blocked_real_execution():
    signal = _make_ll_signal({
        "source_read_health": False,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": False,
    })
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_gap_recovery_blocked_real_execution():
    signal = _make_ll_signal({
        "source_read_health": True,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": True,
    })
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_stale_market_data_blocked_real_execution():
    # Emitted at 12:00, data timestamp at 11:00 = 3600s > 600s limit
    signal = _make_ll_signal(
        provenance={
            "source_read_health": True,
            "source_market_data_timestamp": "2026-10-10T11:00:00Z",
            "gap_recovery": False,
        },
        decision_time="2026-10-10T11:00:00Z",
        signal_emitted_at="2026-10-10T12:00:00Z",
    )
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_fresh_data_just_within_limit_passes():
    # Emitted at 12:00, data at 11:50:00 = 600s exactly = allowed
    signal = _make_ll_signal(
        provenance={
            "source_read_health": True,
            "source_market_data_timestamp": "2026-10-10T11:50:00Z",
            "gap_recovery": False,
        },
        decision_time="2026-10-10T11:50:00Z",
        signal_emitted_at="2026-10-10T12:00:00Z",
    )
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is True


def test_unparseable_timestamp_blocks_real_execution():
    signal = _make_ll_signal({
        "source_read_health": True,
        "source_market_data_timestamp": "not-a-date",
        "gap_recovery": False,
    })
    assert so._validate_signal_provenance(signal, "REAL_EXECUTION") is False


def test_unhealthy_and_gap_recovery_both_allowed_in_shadow_with_audit():
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    signal = _make_ll_signal({
        "source_read_health": False,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": True,
    })
    with patch.object(so, "audit", side_effect=fake_audit):
        result = so._validate_signal_provenance(signal, "SHADOW")
    assert result is True
    assert len(audit_events) == 1
    _, kwargs = audit_events[0]
    assert kwargs["orchestration_mode"] == "SHADOW"
    fields = kwargs["missing_fields"]
    assert any("source_read_health" in f for f in fields)
    assert any("gap_recovery" in f for f in fields)


# ---------------------------------------------------------------------------
# tick() guard: publisher=None raises clearly (B-3)
# ---------------------------------------------------------------------------

def test_tick_raises_configuration_error_when_publisher_none():
    """adapter-mode runtime (publisher=None) must fail immediately on tick()."""
    import importlib
    lrt = importlib.import_module("liquidity_live_runtime")

    runtime = lrt.LiquidityLiveRuntime(
        conn=MagicMock(),
        snapshot_reader=MagicMock(),
        publisher=None,
    )
    try:
        runtime.tick()
        assert False, "Expected RuntimeError"
    except RuntimeError as exc:
        assert "collect_signals" in str(exc).lower() or "publisher" in str(exc).lower()


def test_tick_does_not_raise_when_publisher_set():
    """tick() should not raise the guard when a publisher is provided."""
    import importlib
    lrt = importlib.import_module("liquidity_live_runtime")

    publisher = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    runtime = lrt.LiquidityLiveRuntime(
        conn=conn,
        snapshot_reader=MagicMock(side_effect=lrt.MarketDataUnavailable("no data")),
        publisher=publisher,
    )
    # tick() will fail later when it tries to query the DB, but it must NOT fail
    # at the publisher guard.
    try:
        runtime.tick()
    except RuntimeError as exc:
        # The publisher guard produces a specific message; any other RuntimeError
        # is from DB/snapshot code deeper in tick() and is acceptable here.
        assert "collect_signals" not in str(exc).lower()
    except Exception:
        pass  # DB failures are expected in unit tests
