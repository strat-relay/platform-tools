from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_control_api_image_contains_observation_repository_dependencies():
    dockerfile = (ROOT / "deploy" / "platform_api" / "Dockerfile").read_text()

    # strategy_observation.py imports strategy_backtest.library while resolving
    # managed evaluator keys. This is a runtime-image contract, not a test-only
    # dependency: without it every observation request returns SOURCE_UNAVAILABLE.
    assert "COPY strategy_backtest ./strategy_backtest" in dockerfile
