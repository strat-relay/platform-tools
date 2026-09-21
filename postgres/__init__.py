"""PostgreSQL system-of-record foundation for the trading platform.

This package is intentionally inactive until an operator runs its migration or
import commands.  The live Phase 6 runner continues to use its existing files.
"""

from .config import PostgresConfig
from .db import apply_migrations, connect, health_check
from .phase6 import Phase6Store

__all__ = ["PostgresConfig", "Phase6Store", "apply_migrations", "connect", "health_check"]
