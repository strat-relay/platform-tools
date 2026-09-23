"""Production entrypoint: `python -m execution_v2.runtime`.

Fails closed before connecting to anything if required configuration is missing or unsafe
(`RuntimeConfig.from_env()`, called first inside `service.main_async()`). Starts exactly one
component: the durable entry-signal consumer driving `ExecutionWorker`. Its bridge dependency is
always the signed HTTP `/mcp` client; the test-only simulator is never production-wired.
"""
from __future__ import annotations

from .service import main

if __name__ == "__main__":
    main()
