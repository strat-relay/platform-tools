"""Production entrypoint: `python -m trade_management.runtime`.

Fails closed before connecting to anything if required configuration is missing or unsafe
(`RuntimeConfig.from_env()`, called first inside `service.main_async()`). Starts exactly the
three P4.2 components this vertical slice needs - the ManagedTrade opening consumer, the
observation scheduler loop, and the TM-NONE decision consumer - in one process. Nothing here
starts an execution consumer, REAL_EXECUTION, Phase 7, or any broker-write path.
"""
from __future__ import annotations

from .service import main

if __name__ == "__main__":
    main()
