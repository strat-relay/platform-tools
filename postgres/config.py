from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PostgresConfig:
    """Connection settings sourced only from environment variables."""

    dsn: str | None = None
    readonly_dsn: str | None = None
    host: str | None = None
    port: str | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        return cls(
            dsn=os.getenv("TRADING_POSTGRES_DSN"),
            readonly_dsn=os.getenv("TRADING_POSTGRES_READONLY_DSN"),
            host=os.getenv("PGHOST"), port=os.getenv("PGPORT"),
            database=os.getenv("PGDATABASE"), user=os.getenv("PGUSER"),
            password=os.getenv("PGPASSWORD"),
        )

    def connect_kwargs(self) -> dict[str, str]:
        if self.dsn:
            return {"conninfo": self.dsn}
        return {key: value for key, value in {
            "host": self.host, "port": self.port, "dbname": self.database,
            "user": self.user, "password": self.password,
        }.items() if value}

    def require_explicit_target(self) -> None:
        """Reject libpq ambient defaults when starting an authoritative writer."""
        if self.dsn and self.dsn.strip():
            return
        missing = [name for name, value in (
            ("PGHOST", self.host), ("PGPORT", self.port), ("PGDATABASE", self.database),
            ("PGUSER", self.user), ("PGPASSWORD", self.password),
        ) if not value]
        if missing:
            raise ValueError("explicit PostgreSQL target required; set TRADING_POSTGRES_DSN or all PG* values (missing "
                             + ", ".join(missing) + ")")

    def redacted(self) -> str:
        if self.dsn:
            return "<TRADING_POSTGRES_DSN>"
        fields = [f"{key}={value}" for key, value in self.connect_kwargs().items() if key != "password"]
        return " ".join(fields) or "<environment connection settings unset>"
