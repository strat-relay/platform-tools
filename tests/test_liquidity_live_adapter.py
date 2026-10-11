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

def test_after_publish_hook_does_not_call_ensure_open_liquidity_outcome():
    """Hook must NOT write to strategy.entry_signal_outcomes — resolver owns that."""
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    fake_conn = MagicMock()

    with patch("liquidity_live_runtime.ensure_open_liquidity_outcome") as mock_ensure, \
         patch("observability.strategy_audit.audit"):
        adapter.after_publish_hook("SIG_xyz", fake_conn)

    mock_ensure.assert_not_called()


def test_after_publish_hook_emits_publication_audit_event():
    """Hook must emit exactly one audit event confirming publication."""
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)
    fake_conn = MagicMock()
    audit_events: list[tuple] = []

    def fake_audit(event_name, **kwargs):
        audit_events.append((event_name, kwargs))

    with patch("observability.strategy_audit.audit", side_effect=fake_audit):
        adapter.after_publish_hook("SIG_audit", fake_conn)

    assert len(audit_events) == 1
    name, kwargs = audit_events[0]
    assert name == "signal_published_to_orchestrator"
    assert kwargs["signal_id"] == "SIG_audit"
    assert kwargs["outcome_writer"] == "UNIFIED_OUTCOME_RESOLVER"


def test_after_publish_hook_does_not_raise_on_audit_failure():
    """Hook stays silent when the audit import fails — never propagates."""
    runtime = MagicMock()
    adapter = LiquidityLiveAdapter(runtime)

    with patch.dict("sys.modules", {"observability.strategy_audit": None}):
        try:
            adapter.after_publish_hook("SIG_double_fault", MagicMock())
        except Exception as exc:
            assert False, f"after_publish_hook raised unexpectedly: {exc}"
