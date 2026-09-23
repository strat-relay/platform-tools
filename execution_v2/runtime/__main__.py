"""Production entrypoint: `python -m execution_v2.runtime`.

Fails closed before connecting to anything if required configuration is missing or unsafe
(`RuntimeConfig.from_env()`, called first inside `service.main_async()`). Starts exactly one
component: the durable entry-signal consumer driving `ExecutionWorker`. The bridge it talks to is
always the in-process simulator (`BridgeFenceSimulator`) - never a real MT5 transport - regardless
of `EXECUTION_AUTHORITY_MODE`, until a future, separate mission wires the real bridge (see
docs/v2_execution/README.md "Before activation").
"""
from __future__ import annotations

from .service import main

if __name__ == "__main__":
    main()
