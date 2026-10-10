"""Unit tests for LiquidityLiveAdapter (Phase 1B, A-4).

Covers:
  - StrategyAdapter protocol compliance (strategy_id attr, discover_new_signals callable)
  - discover_new_signals delegates to collect_signals without publishing
  - after_publish_hook calls ensure_open_liquidity_outcome on success
  - after_publish_hook swallows errors and emits an audit event on failure
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.adapters.liquidity_live import LiquidityLiveAdapter, STRATEGY_ID


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

def test_strategy_id_class_attribute():
    assert LiquidityLiveAdapter.strategy_id == STRATEGY_ID


def test_strategy_id_instance_attribute():
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    assert adapter.strategy_id == STRATEGY_ID


def test_has_discover_new_signals():
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    assert callable(getattr(adapter, "discover_new_signals", None))


def test_has_after_publish_hook():
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    assert callable(getattr(adapter, "after_publish_hook", None))


# ---------------------------------------------------------------------------
# discover_new_signals
# ---------------------------------------------------------------------------

def test_discover_new_signals_delegates_to_collect_signals():
    runtime = MagicMock()
    fake_signal = MagicMock(signal_id="SIG_abc123", strategy_id=STRATEGY_ID)
    runtime.collect_signals.return_value = [fake_signal]

    adapter = LiquidityLiveAdapter(runtime)
    seen = {"SIG_old"}
    result = adapter.discover_new_signals(seen)

    runtime.collect_signals.assert_called_once_with(seen)
    assert result == [fake_signal]


def test_discover_new_signals_returns_empty_when_runtime_returns_none_signals():
    runtime = MagicMock()
    runtime.collect_signals.return_value = []

    adapter = LiquidityLiveAdapter(runtime)
    result = adapter.discover_new_signals(set())

    assert result == []


def test_discover_new_signals_does_not_call_publish():
    """collect_signals is pure; no publisher attribute should be touched."""
    runtime = MagicMock(spec=["collect_signals"])
    runtime.collect_signals.return_value = []
    adapter = LiquidityLiveAdapter(runtime)
    adapter.discover_new_signals(set())
    # Only collect_signals should have been called on the runtime mock.
    assert runtime.method_calls == [call.collect_signals(set())]


# ---------------------------------------------------------------------------
# after_publish_hook — success path
# ---------------------------------------------------------------------------

def test_after_publish_hook_calls_ensure_open_liquidity_outcome():
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    fake_conn = MagicMock()

    with patch("liquidity_live_runtime.ensure_open_liquidity_outcome") as mock_ensure:
        adapter.after_publish_hook("SIG_xyz", fake_conn)

    mock_ensure.assert_called_once_with(fake_conn, "SIG_xyz")


# ---------------------------------------------------------------------------
# after_publish_hook — failure path
# ---------------------------------------------------------------------------

def test_after_publish_hook_swallows_exception_and_audits():
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    fake_conn = MagicMock()

    audit_events: list[tuple] = []

    def fake_ensure(conn, signal_id):
        raise RuntimeError("db write failed")

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    with patch("liquidity_live_runtime.ensure_open_liquidity_outcome", side_effect=fake_ensure):
        with patch("observability.strategy_audit.audit", side_effect=fake_audit):
            # Must NOT raise.
            adapter.after_publish_hook("SIG_fail", fake_conn)

    assert len(audit_events) == 1
    name, kwargs = audit_events[0]
    assert name == "after_publish_hook_failed"
    assert kwargs["strategy_id"] == STRATEGY_ID
    assert kwargs["signal_id"] == "SIG_fail"
    assert kwargs["hook"] == "ensure_open_liquidity_outcome"
    assert "db write failed" in kwargs["error"]


def test_after_publish_hook_does_not_raise_on_audit_failure():
    """Double-fault: ensure_open throws AND audit import fails — adapter stays silent."""
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)

    with patch("liquidity_live_runtime.ensure_open_liquidity_outcome", side_effect=RuntimeError("x")):
        with patch.dict("sys.modules", {"observability.strategy_audit": None}):
            # Should not propagate either error.
            try:
                adapter.after_publish_hook("SIG_double_fault", MagicMock())
            except Exception as exc:
                assert False, f"after_publish_hook raised unexpectedly: {exc}"
