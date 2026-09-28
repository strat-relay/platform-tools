"""Runtime configuration: sourced only from the environment, fails closed on anything missing or
unsafe - mirrors `trade_management/runtime/config.py`'s own conventions, but for the opposite
authority direction: P4's runtime *requires* EXECUTION_AUTHORITY_MODE=DISABLED to even start,
while this one is the one component allowed to observe ENABLED - and even then, ENABLED alone
never makes it place an order (see execution_v2/runtime/__init__.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from migration.flags import ExecutionAuthorityMode, execution_authority_mode_from_env
from postgres.config import PostgresConfig

CONSUMER_NAME = "execution-v2-entry-signal"
SUBJECT = "signal.entry.created.v1"
STREAM = "TRADING_CORE"

DEFAULT_HEALTH_PORT = 8081


class RuntimeConfigError(RuntimeError):
    """Configuration is missing or unsafe; the runtime must not start."""


@dataclass(frozen=True)
class RuntimeConfig:
    postgres: PostgresConfig
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    execution_authority_mode: ExecutionAuthorityMode
    account_id: str
    bridge_mode: str  # "demo" | "real" - the resource namespace prefix, never a live-vs-sim switch by itself
    bridge_fence_url: str  # the REAL bridge's fence-validation endpoint; no default, ever (see from_env)
    read_bridge_url: str
    fence_signing_key: bytes
    fence_key_id: str
    risk_policy_path: str
    health_port: int
    holder_instance_id: str
    # BRIDGE (default, unchanged production behaviour): synchronous read-bridge risk context.
    # REDIS: cached RiskSnapshot + atomic reservation (execution_v2/risk_state); requires RISK_REDIS_URL.
    risk_context_source: str = "BRIDGE"
    risk_redis_url: str | None = None
    # V2_RISK_SIZING_BASIS: EQUITY (default) or FREE_MARGIN - the capital risk_per_trade applies to
    # on the bridge risk path.
    risk_sizing_basis: str = "EQUITY"

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        # The single hard gate (mission section 2): read once, explicitly, and pass the resulting
        # enum through unchanged - process_signal() itself decides nothing about default-enabling.
        mode = execution_authority_mode_from_env()

        pg = PostgresConfig.from_env()
        pg.require_explicit_target()

        nats_url = os.getenv("NATS_URL")
        if not nats_url or not nats_url.strip():
            raise RuntimeConfigError("NATS_URL is required")

        account_id = os.getenv("V2_EXECUTION_ACCOUNT_ID")
        if not account_id or not account_id.strip():
            raise RuntimeConfigError("V2_EXECUTION_ACCOUNT_ID is required")

        bridge_mode = os.getenv("V2_EXECUTION_BRIDGE_MODE", "demo").strip().lower()
        if bridge_mode not in ("demo", "real"):
            raise RuntimeConfigError("V2_EXECUTION_BRIDGE_MODE must be 'demo' or 'real'")

        # The REAL bridge fence endpoint (mission CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION
        # section 2): no default of any kind. A deployment with this unset FAILS CLOSED here,
        # before execution_v2/runtime/service.py ever constructs anything - there is no fallback
        # to a simulator, mock, or local success adapter anywhere in this codebase's production
        # wiring (execution_v2/bridge_fence_sim.py is never imported by any file under
        # execution_v2/runtime/).
        bridge_fence_url = os.getenv("V2_BRIDGE_FENCE_URL")
        if not bridge_fence_url or not bridge_fence_url.strip():
            raise RuntimeConfigError("V2_BRIDGE_FENCE_URL is required - no real bridge is configured")
        bridge_fence_url = bridge_fence_url.strip()
        if not bridge_fence_url.endswith("/mcp"):
            raise RuntimeConfigError("V2_BRIDGE_FENCE_URL must target the real bridge /mcp endpoint")
        read_bridge_url = os.getenv("V2_READ_BRIDGE_URL", bridge_fence_url).strip()
        if not read_bridge_url.endswith("/mcp"):
            raise RuntimeConfigError("V2_READ_BRIDGE_URL must target a bridge /mcp endpoint")

        # FenceAuthority.from_env() already fails closed on a missing/short key; re-validate here
        # too so RuntimeConfig.from_env() alone (without constructing a FenceAuthority) is enough
        # to prove the process cannot start unfenced.
        raw_key = os.getenv("V2_FENCE_SIGNING_KEY")
        if not raw_key or not raw_key.strip():
            raise RuntimeConfigError("V2_FENCE_SIGNING_KEY is required")
        key_bytes = raw_key.strip().encode("utf-8")
        if len(key_bytes) < 32:
            raise RuntimeConfigError("V2_FENCE_SIGNING_KEY must be at least 32 bytes")
        key_id = os.getenv("V2_FENCE_KEY_ID", "v2-fence-key-1").strip()
        if not key_id:
            raise RuntimeConfigError("V2_FENCE_KEY_ID, if set, must not be empty")

        risk_policy_path = os.getenv("V2_EXECUTION_RISK_POLICY_PATH")
        if not risk_policy_path or not risk_policy_path.strip():
            from ..risk import DEFAULT_RISK_POLICY_PATH
            risk_policy_path = str(DEFAULT_RISK_POLICY_PATH)  # the shipped enabled:false config

        health_port_raw = os.getenv("V2_EXECUTION_HEALTH_PORT", str(DEFAULT_HEALTH_PORT))
        try:
            health_port = int(health_port_raw)
        except ValueError as exc:
            raise RuntimeConfigError("V2_EXECUTION_HEALTH_PORT must be an integer") from exc

        holder_instance_id = os.getenv("POD_NAME") or os.getenv("HOSTNAME")
        if not holder_instance_id or not holder_instance_id.strip():
            raise RuntimeConfigError("POD_NAME (or HOSTNAME) is required - ownership fencing needs a stable holder identity")

        risk_context_source = os.getenv("RISK_CONTEXT_SOURCE", "BRIDGE").strip().upper() or "BRIDGE"
        if risk_context_source not in ("BRIDGE", "REDIS"):
            raise RuntimeConfigError("RISK_CONTEXT_SOURCE must be BRIDGE or REDIS")
        risk_redis_url = (os.getenv("RISK_REDIS_URL") or "").strip() or None
        if risk_context_source == "REDIS" and risk_redis_url is None:
            raise RuntimeConfigError("RISK_CONTEXT_SOURCE=REDIS requires RISK_REDIS_URL")
        risk_sizing_basis = os.getenv("V2_RISK_SIZING_BASIS", "EQUITY").strip().upper() or "EQUITY"
        if risk_sizing_basis not in ("EQUITY", "FREE_MARGIN"):
            raise RuntimeConfigError("V2_RISK_SIZING_BASIS must be EQUITY or FREE_MARGIN")

        return cls(postgres=pg, nats_url=nats_url.strip(),
                   nats_user=os.getenv("V2_NATS_USER") or os.getenv("P2_NATS_USER"),
                   nats_password=os.getenv("V2_NATS_PASSWORD") or os.getenv("P2_NATS_PASSWORD"),
                   execution_authority_mode=mode, account_id=account_id.strip(), bridge_mode=bridge_mode,
                   bridge_fence_url=bridge_fence_url, read_bridge_url=read_bridge_url,
                   fence_signing_key=key_bytes, fence_key_id=key_id, risk_policy_path=risk_policy_path,
                   health_port=health_port, holder_instance_id=holder_instance_id.strip(),
                   risk_context_source=risk_context_source, risk_redis_url=risk_redis_url,
                   risk_sizing_basis=risk_sizing_basis)
