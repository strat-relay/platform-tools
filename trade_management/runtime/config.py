"""Runtime configuration: sourced only from the environment, fails closed on anything missing
or unsafe. No default silently substitutes for a required production value except the 22347
bridge URL and the observation interval, both of which already have a safe, documented,
repository-wide convention to fall back to (matching `orchestration/config.py`,
`paper_runner.py`, etc.).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from migration.flags import ExecutionAuthorityMode, execution_authority_mode_from_env
from postgres.config import PostgresConfig

DEFAULT_BRIDGE_MCP_URL = "http://127.0.0.1:22347/mcp"
DEFAULT_OBSERVATION_INTERVAL_SECONDS = 30.0  # SHIP-FIRST value; see docs/engineering/OPTIMIZATION_REGISTER.md

OPEN_CONSUMER_NAME = "trade-mgmt-open"
DECISION_CONSUMER_NAME = "trade-manager-shadow"


class RuntimeConfigError(RuntimeError):
    """Configuration is missing or unsafe; the runtime must not start."""


@dataclass(frozen=True)
class RuntimeConfig:
    postgres: PostgresConfig
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    bridge_mcp_url: str
    observation_interval_seconds: float
    health_port: int
    pod_name: str | None

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        mode = execution_authority_mode_from_env()
        if mode is not ExecutionAuthorityMode.DISABLED:
            # Defense in depth (mission section 10 "hard safety wall"): this runtime never
            # touches execution, but it must refuse to even start if something upstream has
            # already flipped execution on - a P4 shadow deployment is not the place to find
            # that out.
            raise RuntimeConfigError("P4 runtime requires EXECUTION_AUTHORITY_MODE=DISABLED")

        pg = PostgresConfig.from_env()
        pg.require_explicit_target()

        nats_url = os.getenv("NATS_URL")
        if not nats_url or not nats_url.strip():
            raise RuntimeConfigError("NATS_URL is required")

        bridge_url = os.getenv("P4_BRIDGE_MCP_URL", DEFAULT_BRIDGE_MCP_URL).strip()
        if not bridge_url:
            raise RuntimeConfigError("P4_BRIDGE_MCP_URL, if set, must not be empty")
        if ":22348" in bridge_url:
            raise RuntimeConfigError("P4_BRIDGE_MCP_URL must not target the execution port (22348)")

        interval_raw = os.getenv("P4_OBSERVATION_INTERVAL_SECONDS")
        interval = DEFAULT_OBSERVATION_INTERVAL_SECONDS
        if interval_raw is not None and interval_raw.strip():
            try:
                interval = float(interval_raw)
            except ValueError as exc:
                raise RuntimeConfigError("P4_OBSERVATION_INTERVAL_SECONDS must be numeric") from exc
        if interval <= 0:
            raise RuntimeConfigError("P4_OBSERVATION_INTERVAL_SECONDS must be positive")

        health_port_raw = os.getenv("P4_HEALTH_PORT", "8080")
        try:
            health_port = int(health_port_raw)
        except ValueError as exc:
            raise RuntimeConfigError("P4_HEALTH_PORT must be an integer") from exc

        return cls(postgres=pg, nats_url=nats_url.strip(),
                   nats_user=os.getenv("P4_NATS_USER") or os.getenv("P2_NATS_USER"),
                   nats_password=os.getenv("P4_NATS_PASSWORD") or os.getenv("P2_NATS_PASSWORD"),
                   bridge_mcp_url=bridge_url, observation_interval_seconds=interval,
                   health_port=health_port, pod_name=os.getenv("POD_NAME"))
