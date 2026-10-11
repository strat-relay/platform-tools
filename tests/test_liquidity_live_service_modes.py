"""Tests for standalone vs orchestrator activation mode separation.

Verifies that LIQUIDITY_LIVE_ORCHESTRATOR_MODE and LIQUIDITY_LIVE_RUNTIME_ENABLED
activate the correct paths and that simultaneous publication is made impossible
rather than merely discouraged.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import liquidity_live_service as svc


# ---------------------------------------------------------------------------
# Standalone service: LIQUIDITY_LIVE_ORCHESTRATOR_MODE blocks startup
# ---------------------------------------------------------------------------

def test_standalone_refuses_when_orchestrator_mode_enabled():
    """check_configuration() must raise when LIQUIDITY_LIVE_ORCHESTRATOR_MODE=true,
    even if LIQUIDITY_LIVE_RUNTIME_ENABLED=true."""
    env = {
        "LIQUIDITY_LIVE_RUNTIME_ENABLED": "true",
        "LIQUIDITY_LIVE_ORCHESTRATOR_MODE": "true",
        "SIGNAL_CUTOFF_ID": "cutoff-1",
        "SIGNAL_CUTOFF_UTC": "2026-01-01T00:00:00Z",
        "MARKET_DATA_SOURCE": "REDIS",
    }
    with patch.dict(os.environ, env, clear=True):
        try:
            svc.check_configuration()
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            assert "LIQUIDITY_LIVE_ORCHESTRATOR_MODE" in str(exc)
            assert "standalone" in str(exc).lower() or "orchestrator" in str(exc).lower()


def test_standalone_runs_when_only_runtime_enabled():
    """Existing production config (LIQUIDITY_LIVE_RUNTIME_ENABLED=true only) must still work."""
    env = {
        "LIQUIDITY_LIVE_RUNTIME_ENABLED": "true",
        "LIQUIDITY_LIVE_ORCHESTRATOR_MODE": "false",
        "SIGNAL_CUTOFF_ID": "cutoff-1",
        "SIGNAL_CUTOFF_UTC": "2026-01-01T00:00:00Z",
        "MARKET_DATA_SOURCE": "REDIS",
    }
    with patch.dict(os.environ, env, clear=True):
        # Should not raise (past the orchestrator mode check).
        svc.check_configuration()


def test_standalone_disabled_when_runtime_not_enabled():
    env = {
        "LIQUIDITY_LIVE_RUNTIME_ENABLED": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        try:
            svc.check_configuration()
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            assert "disabled" in str(exc).lower()


def test_orchestrator_mode_case_insensitive():
    """LIQUIDITY_LIVE_ORCHESTRATOR_MODE=TRUE (uppercase) must also block."""
    env = {
        "LIQUIDITY_LIVE_RUNTIME_ENABLED": "true",
        "LIQUIDITY_LIVE_ORCHESTRATOR_MODE": "TRUE",
        "SIGNAL_CUTOFF_ID": "cutoff-1",
        "SIGNAL_CUTOFF_UTC": "2026-01-01T00:00:00Z",
        "MARKET_DATA_SOURCE": "REDIS",
    }
    with patch.dict(os.environ, env, clear=True):
        try:
            svc.check_configuration()
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            assert "LIQUIDITY_LIVE_ORCHESTRATOR_MODE" in str(exc)


# ---------------------------------------------------------------------------
# Orchestrator: adapter registers only on LIQUIDITY_LIVE_ORCHESTRATOR_MODE
# ---------------------------------------------------------------------------

def _minimal_config() -> dict:
    """Minimal config that passes StrategyRegistry without touching a DB."""
    return {
        "enabled": True,
        "instances": [],
        "strategies": [
            {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "enabled": True},
            {"strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "enabled": True},
        ],
        "portfolio": {"max_concurrent_signals": 10, "instruments": []},
    }


def test_orchestrator_adapter_not_registered_without_orchestrator_mode():
    """LIQUIDITY_LIVE_RUNTIME_ENABLED=true alone must NOT register the adapter
    in the orchestrator — that var now controls only the standalone service."""
    import signal_orchestrator as so
    env = {
        "LIQUIDITY_LIVE_RUNTIME_ENABLED": "true",
        "LIQUIDITY_LIVE_ORCHESTRATOR_MODE": "false",
    }
    conn = object()
    with patch.dict(os.environ, env, clear=True):
        with patch("signal_orchestrator.ContextStructureRetraceAdapter"), \
             patch("signal_orchestrator.LiquidityDisplacementAdapter"), \
             patch("signal_orchestrator.LiquidityInstanceAdapter"), \
             patch("signal_orchestrator.audit"), \
             patch("signal_orchestrator.StrategyRegistry") as mock_reg:
            mock_reg.return_value.enabled.return_value = []
            adapters = so.load_adapters(_minimal_config(), "2026-01-01T00:00:00Z", conn=conn)
    from orchestration.adapters.liquidity_live import LiquidityLiveAdapter
    assert not any(isinstance(a, LiquidityLiveAdapter) for a in adapters)


def test_adapter_registered_when_orchestrator_mode_true():
    """LIQUIDITY_LIVE_ORCHESTRATOR_MODE=true must register the adapter."""
    import signal_orchestrator as so
    env = {"LIQUIDITY_LIVE_ORCHESTRATOR_MODE": "true"}
    conn = object()
    with patch.dict(os.environ, env, clear=True):
        with patch("signal_orchestrator.ContextStructureRetraceAdapter"), \
             patch("signal_orchestrator.LiquidityDisplacementAdapter"), \
             patch("signal_orchestrator.LiquidityInstanceAdapter"), \
             patch("signal_orchestrator.audit"), \
             patch("signal_orchestrator.StrategyRegistry") as mock_reg, \
             patch("orchestration.adapters.liquidity_live.LiquidityLiveAdapter") as mock_adapter_cls, \
             patch("liquidity_live_runtime.LiquidityLiveRuntime"), \
             patch("liquidity_live_runtime.register_orchestrator_publication_mode"), \
             patch("liquidity_market_data.build_liquidity_market_data"):
            mock_reg.return_value.enabled.return_value = [
                {"strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "enabled": True}
            ]
            mock_adapter_cls.return_value = object()
            adapters = so.load_adapters(_minimal_config(), "2026-01-01T00:00:00Z", conn=conn)
    mock_adapter_cls.assert_called_once()
