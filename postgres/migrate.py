"""Apply versioned PostgreSQL migrations only; this never imports runtime data."""
from __future__ import annotations

import json

from .config import PostgresConfig
from .db import apply_migrations, connect


def main() -> None:
    with connect(PostgresConfig.from_env()) as conn:
        print(json.dumps({"migrations": apply_migrations(conn)}))


if __name__ == "__main__":
    main()
