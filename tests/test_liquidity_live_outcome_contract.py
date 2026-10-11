"""Integration tests for the Unified Outcome Resolver signal contract.

Covers:
- outcome_contract fields present and correctly formed on every Liquidity Live signal
- provider_symbol present in provenance
- Publishing via orchestrator adapter path creates NO outcome row (resolver owns that)
- DB-level publication exclusivity: tick() blocks when orchestrator lock is active
- Standalone and orchestrator modes cannot both publish canonical signals
"""
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.liquidity_live import (
    LiquidityLiveEvaluator, LiquidityParameterSet, _snapshot_health, STRATEGY_VERSION,
)
from liquidity_market_data import LiveMarketSnapshot
import liquidity_live_runtime as lrt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PARAM_SET = LiquidityParameterSet(
    parameter_set_id="test-ps-1",
    instance_id="test-instance-1",
    canonical_instrument="XAUUSD",
    broker_symbol="XAUUSDm",
    entry_fraction=0.50,
    max_retrace_candles=3,
    target_r=1.25,
    max_hold_minutes=120,
)


def _make_signal(provenance: Optional[dict[str, Any]] = None) -> Any:
    """Create a minimal StrategySignal with the given provenance."""
    from orchestration.models import StrategySignal
    prov = provenance if provenance is not None else {
        "source_read_health": True,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": False,
        "provider_symbol": "XAUUSDm",
        "outcome_contract_version": "entry-outcome.v2",
    }
    return StrategySignal(
        signal_id="SIG_contract_test",
        schema_version="strategy-signal-v1",
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        strategy_version="V1",
        strategy_instance_id="test-instance-1",
        source_event_id="evt-1",
        market_event_id=None, setup_id="setup-1",
        entry_opportunity_id="evt-1", economic_position_id=None,
        created_at="2026-10-10T12:00:00Z",
        signal_timestamp="2026-10-10T11:00:00Z",
        symbol="XAUUSDm", canonical_symbol="XAUUSD", broker_symbol_hint="XAUUSDm",
        direction="LONG", entry_type="MARKET",
        entry_price=1900.0, stop_price=1895.0, target_price=1910.0,
        risk_distance=5.0, target_distance=10.0, target_r=1.25,
        timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
        entry_mechanism=("LIQUIDITY_SWEEP",),
        strategy_metadata={"outcome_contract": {
            "version": "entry-outcome.v2",
            "timeframe_minutes": 5,
            "activation": "SIGNAL_TIMESTAMP",
            "max_hold_minutes": 120,
            "expiration_minutes": None,
            "time_exit_price": "CLOSE",
            "price_basis": "THEORETICAL_TOUCH",
            "same_candle_priority": "STOP_FIRST",
            "time_exit_priority": "BEFORE_PRICE",
        }},
        provenance=prov,
        decision_time="2026-10-10T11:00:00Z",
        signal_emitted_at="2026-10-10T12:00:00Z",
    )


def _make_evaluator_signal() -> Any:
    """Produce a real signal from LiquidityLiveEvaluator using a minimal setup."""
    evaluator = LiquidityLiveEvaluator(_PARAM_SET)
    # Inject a pre-entered setup so _signal_for_fill() triggers deterministically.
    from orchestration.models import stable_id
    setup_id = stable_id("LQSETUP", {
        "instance": "test-instance-1",
        "parameter_set": "test-ps-1",
        "sweep_time": 1_000_000,
        "direction": "LONG",
    })
    bar = {"time": 1_000_000, "open": 1905.0, "high": 1908.0, "low": 1899.0, "close": 1902.0}
    m5 = [bar] * 50
    m15 = [bar] * 20
    evaluator._setups[setup_id] = {
        "setup_id": setup_id,
        "instance_id": "test-instance-1",
        "parameter_set_id": "test-ps-1",
        "canonical_instrument": "XAUUSD",
        "direction": "LONG",
        "state": "PENDING_RETRACE",
        "sweep_time": 1_000_000,
        "sweep_index": 40,
        "displacement_index": 42,
        "displacement_timestamp": 1_000_000,
        "entry": 1901.5,
        "stop": 1895.0,
        "target": 1910.0,
        "risk": 6.5,
        "scan_index": 40,
        "scan_timestamp": 1_000_000,
        "entry_signal_id": None,
        "lifecycle": ["SWEEP"],
    }
    health = {
        "source_read_health": True,
        "source_market_data_timestamp": "2026-10-10T11:55:00Z",
        "gap_recovery": False,
    }
    retrace_bar = {"time": 1_000_300, "open": 1902.0, "high": 1905.0, "low": 1899.0, "close": 1902.5}
    return evaluator._signal_for_fill(
        evaluator._setups[setup_id],
        bar=retrace_bar,
        evaluation_time="2026-10-10T12:00:00Z",
        broker_symbol="XAUUSDm",
        validated_by="mt5-read-22347",
        snapshot_health=health,
    )


# ---------------------------------------------------------------------------
# outcome_contract fields
# ---------------------------------------------------------------------------

def test_signal_carries_outcome_contract():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata.get("outcome_contract")
    assert oc is not None, "strategy_metadata.outcome_contract must be present"


def test_outcome_contract_version():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["version"] == "entry-outcome.v2"


def test_outcome_contract_timeframe_is_m5():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["timeframe_minutes"] == 5


def test_outcome_contract_activation():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["activation"] == "SIGNAL_TIMESTAMP"


def test_outcome_contract_price_basis():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["price_basis"] == "THEORETICAL_TOUCH"


def test_outcome_contract_stop_first_collision():
    """Liquidity V1 frozen rule: stop takes priority on same candle."""
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["same_candle_priority"] == "STOP_FIRST"


def test_outcome_contract_before_price_time_exit():
    """Liquidity V1 frozen rule: time exit evaluated before price exit."""
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["time_exit_priority"] == "BEFORE_PRICE"


def test_outcome_contract_max_hold_minutes_from_parameter_set():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["max_hold_minutes"] == _PARAM_SET.max_hold_minutes


def test_outcome_contract_time_exit_price():
    signal = _make_evaluator_signal()
    oc = signal.strategy_metadata["outcome_contract"]
    assert oc["time_exit_price"] == "CLOSE"


# ---------------------------------------------------------------------------
# provider_symbol in provenance
# ---------------------------------------------------------------------------

def test_provenance_carries_provider_symbol():
    signal = _make_evaluator_signal()
    assert "provider_symbol" in signal.provenance
    assert signal.provenance["provider_symbol"] == "XAUUSDm"


def test_provider_symbol_matches_broker_symbol_hint():
    signal = _make_evaluator_signal()
    assert signal.provenance["provider_symbol"] == signal.broker_symbol_hint


# ---------------------------------------------------------------------------
# No outcome row created on orchestrator publish path (item 9)
# ---------------------------------------------------------------------------

def test_after_publish_hook_does_not_write_outcome_row():
    """The orchestrator publish path must NOT write to strategy.entry_signal_outcomes.

    Before this fix, after_publish_hook called ensure_open_liquidity_outcome().
    After the fix, it must only emit an audit event.  The resolver creates the OPEN row.
    """
    from orchestration.adapters.liquidity_live import LiquidityLiveAdapter

    conn = MagicMock()
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)

    executed_sqls: list[str] = []
    mock_cursor = MagicMock()

    def capture_execute(sql, *args, **kwargs):
        executed_sqls.append(str(sql).strip())

    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute.side_effect = capture_execute
    conn.cursor.return_value = mock_cursor

    with patch("observability.strategy_audit.audit"):
        adapter.after_publish_hook("SIG_test", conn)

    # No SQL touching entry_signal_outcomes should have been executed.
    outcome_writes = [s for s in executed_sqls if "entry_signal_outcomes" in s.lower()]
    assert outcome_writes == [], (
        f"after_publish_hook must not write to entry_signal_outcomes; "
        f"found: {outcome_writes}"
    )


def test_after_publish_hook_emits_audit_event():
    """Hook must emit exactly one observability event for tracing."""
    from orchestration.adapters.liquidity_live import LiquidityLiveAdapter
    from unittest.mock import patch

    conn = MagicMock()
    adapter = LiquidityLiveAdapter(MagicMock())

    audit_calls: list[tuple] = []
    with patch("observability.strategy_audit.audit",
               side_effect=lambda name, **kw: audit_calls.append((name, kw))):
        adapter.after_publish_hook("SIG_audit_test", conn)

    assert len(audit_calls) == 1
    name, kwargs = audit_calls[0]
    assert name == "signal_published_to_orchestrator"
    assert kwargs["signal_id"] == "SIG_audit_test"
    assert kwargs["outcome_writer"] == "UNIFIED_OUTCOME_RESOLVER"


def test_collect_signals_does_not_call_outcome_writers():
    """collect_signals() must never call ensure_open_liquidity_outcome,
    ensure_open_liquidity_outcomes, or monitor_open_liquidity_entries."""
    import re
    import inspect
    forbidden = [
        "ensure_open_liquidity_outcome",
        "ensure_open_liquidity_outcomes",
        "monitor_open_liquidity_entries",
    ]
    source = inspect.getsource(lrt.LiquidityLiveRuntime.collect_signals)
    # Match actual calls: function_name(  — not occurrences inside comment/docstring lines.
    for fn_name in forbidden:
        # Lines starting with # or inside triple-quoted strings are excluded via the call pattern.
        calls_found = re.findall(rf"\b{re.escape(fn_name)}\s*\(", source)
        assert not calls_found, (
            f"collect_signals() must not call {fn_name}(); "
            "outcome writes belong to the Unified Outcome Resolver."
        )


# ---------------------------------------------------------------------------
# DB-level publication exclusivity (item 8)
# ---------------------------------------------------------------------------

def test_tick_blocked_when_orchestrator_lock_active():
    """tick() must block publication when the orchestrator holds the DB lock."""
    conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    # Simulate orchestrator lock found in DB
    mock_cursor.fetchone.return_value = (1,)
    conn.cursor.return_value = mock_cursor

    publisher = MagicMock()
    runtime = lrt.LiquidityLiveRuntime(
        conn=conn,
        snapshot_reader=MagicMock(side_effect=lrt.MarketDataUnavailable("no data")),
        publisher=publisher,
    )
    try:
        runtime.tick()
        assert False, "Expected RuntimeError from exclusivity check"
    except RuntimeError as exc:
        assert "orchestrator" in str(exc).lower()
    # The publisher must never have been called — publication was blocked at the lock.
    publisher.existing_signal_ids.assert_not_called()
    publisher.publish.assert_not_called()


def test_tick_proceeds_when_no_orchestrator_lock():
    """tick() must proceed normally when the orchestrator lock is absent."""
    conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    # No lock record
    mock_cursor.fetchone.return_value = None
    conn.cursor.return_value = mock_cursor

    snapshot_reader = MagicMock(side_effect=lrt.MarketDataUnavailable("no data"))
    publisher = MagicMock()
    runtime = lrt.LiquidityLiveRuntime(
        conn=conn, snapshot_reader=snapshot_reader, publisher=publisher,
    )
    # tick() will fail later (DB queries for memberships etc.), but must not
    # fail at the exclusivity check.
    try:
        runtime.tick()
    except RuntimeError as exc:
        assert "orchestrator" not in str(exc).lower(), (
            f"Unexpected orchestrator-lock error when lock is absent: {exc}"
        )
    except Exception:
        pass  # other DB failures expected in unit tests


def test_check_publication_exclusivity_raises_on_db_error():
    """A DB error during the lock check must fail closed, not allow publication."""
    conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute.side_effect = Exception("connection lost")
    conn.cursor.return_value = mock_cursor

    try:
        lrt.check_publication_exclusivity(conn)
        assert False, "Expected RuntimeError"
    except RuntimeError as exc:
        assert "exclusivity check failed" in str(exc).lower() or "publication" in str(exc).lower()


def test_register_orchestrator_publication_mode_upserts_record():
    """register_orchestrator_publication_mode() must write to platform.runtime_instances."""
    conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = mock_cursor

    lrt.register_orchestrator_publication_mode(conn)

    assert mock_cursor.execute.called
    sql = mock_cursor.execute.call_args[0][0].lower()
    assert "platform.runtime_instances" in sql
    assert "insert" in sql


def test_simultaneous_modes_blocked_at_db_level():
    """End-to-end: if orchestrator registers the lock and standalone calls tick(),
    tick() must block even when env vars are not correctly set on the standalone pod.

    This verifies that DB-level exclusivity goes beyond environment-variable separation:
    even a misconfigured standalone pod (e.g., LIQUIDITY_LIVE_ORCHESTRATOR_MODE absent)
    cannot publish when the orchestrator's DB lock is active.
    """
    # 1. Orchestrator registers the lock
    orch_conn = MagicMock()
    orch_cursor = MagicMock()
    orch_cursor.__enter__ = MagicMock(return_value=orch_cursor)
    orch_cursor.__exit__ = MagicMock(return_value=False)
    orch_conn.cursor.return_value = orch_cursor
    lrt.register_orchestrator_publication_mode(orch_conn)

    # 2. Standalone calls tick() — but its DB connection shows the lock active
    standalone_conn = MagicMock()
    standalone_cursor = MagicMock()
    standalone_cursor.__enter__ = MagicMock(return_value=standalone_cursor)
    standalone_cursor.__exit__ = MagicMock(return_value=False)
    # Simulate DB returning the active lock record
    standalone_cursor.fetchone.return_value = (1,)
    standalone_conn.cursor.return_value = standalone_cursor

    publisher = MagicMock()
    standalone_runtime = lrt.LiquidityLiveRuntime(
        conn=standalone_conn,
        snapshot_reader=MagicMock(),
        publisher=publisher,
    )
    try:
        standalone_runtime.tick()
        assert False, "Standalone should be blocked by DB lock"
    except RuntimeError as exc:
        # Must specifically mention the orchestrator lock, not just publisher guard
        assert "orchestrator" in str(exc).lower()

    # Critical: publish was never invoked on the standalone path
    publisher.publish.assert_not_called()


def test_stale_orchestrator_heartbeat_remains_fail_closed():
    """A 300s-old heartbeat must not silently authorize a bypass publisher."""
    stale_conn = MagicMock()
    stale_cursor = MagicMock()
    stale_cursor.__enter__ = MagicMock(return_value=stale_cursor)
    stale_cursor.__exit__ = MagicMock(return_value=False)
    stale_cursor.fetchone.return_value = ("RUNNING",)
    stale_conn.cursor.return_value = stale_cursor

    try:
        lrt.check_publication_exclusivity(stale_conn)
        assert False, "A stale RUNNING fence must remain blocked"
    except RuntimeError as exc:
        assert "stale heartbeat" in str(exc).lower()


def test_publication_fence_has_explicit_clean_stop_release():
    """Only an explicit STOPPED transition releases the orchestrator fence."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    lrt.release_orchestrator_publication_mode(conn)

    sql = cursor.execute.call_args[0][0].lower()
    assert "status = 'stopped'" in sql
    assert "liquidity-live-orchestrator" in str(cursor.execute.call_args[0][1])
