"""Import smoke test for every Python module used by production runtime pods."""
from __future__ import annotations

import importlib


ENTRYPOINT_MODULES = (
    "liquidity_live_service",
    "signal_orchestrator",
    "scripts.outcome_resolver_service",
    "scripts.signal_outbox_relay",
    "trade_management.runtime",
    "execution_v2.runtime",
    "market_data_cache.service",
    "broker_view.service",
    "context_structure_retrace_forward",
    "context_structure_retrace_v2_forward",
)


def main() -> None:
    for module_name in ENTRYPOINT_MODULES:
        importlib.import_module(module_name)
        print(f"runtime entrypoint import ok: {module_name}")


if __name__ == "__main__":
    main()
