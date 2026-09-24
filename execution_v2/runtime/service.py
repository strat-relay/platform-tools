"""Wires the execution_v2 runtime into ONE supervised process, matching `trade_management/
runtime/service.py`'s single-connection-per-process convention: one PostgreSQL connection, one
NATS/JetStream connection, the durable entry-signal consumer, and the health server.

`main()` fails closed (`RuntimeConfigError`) before connecting to anything if configuration is
missing or unsafe. The ONLY bridge fence implementation ever constructed here is
`HttpBridgeFenceClient`, targeting `config.bridge_fence_url` - a real HTTP client with no local
fallback (mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION` section 2).
`execution_v2.bridge_fence_sim.BridgeFenceSimulator` is never imported by this module or by
anything it imports (proven statically by
`tests/test_execution_v2_isolation.py::test_no_production_module_imports_the_test_only_simulator`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import urllib.request
from typing import Any

from postgres.db import connect, transaction

from ..fence import FenceAuthority
from ..risk import RiskPolicy
from ..risk_policy_store import read_effective_policy
from ..authority_store import read_authority
from ..worker import ExecutionWorker
from ..symbols import resolve_broker_symbol
from .bridge_client import HttpBridgeFenceClient
from .config import CONSUMER_NAME, STREAM, SUBJECT, RuntimeConfig
from .consumer import ExecutionSignalConsumer
from .health import HealthState, start_health_server

log = logging.getLogger("execution_v2.runtime")


class RuntimeContext:
    """Everything `run()` needs, split out so tests can construct it with fakes without going
    through `main()`'s real NATS/PostgreSQL connections (matches trade_management's own
    RuntimeContext split)."""

    def __init__(self, *, conn: Any, js: Any, config: RuntimeConfig, health: HealthState,
                consumer: ExecutionSignalConsumer, status_metadata: dict[str, Any] | None = None) -> None:
        self.conn = conn
        self.js = js
        self.config = config
        self.health = health
        self.consumer = consumer
        self.status_metadata = status_metadata or {}


def register_runtime_instance(conn: Any, *, instance_id: str, component: str = "execution_v2",
                              metadata: dict[str, Any] | None = None) -> None:
    """platform.ownership_leases.holder_instance_id is a real FK to platform.runtime_instances
    (discovered only against real PostgreSQL - fakes.py does not model this FK). A worker must
    register itself before it can ever acquire a fence generation."""
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.runtime_instances (instance_id, component, status, metadata)
                          VALUES (%s, %s, 'RUNNING', %s::jsonb)
                          ON CONFLICT (instance_id) DO UPDATE SET status='RUNNING',
                            last_heartbeat_at=now(), metadata=EXCLUDED.metadata""",
                       (instance_id, component, json.dumps(metadata or {})))


async def ensure_execution_stream(js: Any) -> None:
    from infrastructure.messaging.jetstream import JetStreamTopology
    await JetStreamTopology.v1().ensure(js)  # idempotent; EXECUTION already includes execution.*


async def bootstrap_consumer(js: Any, *, consumer_name: str) -> bool:
    """Idempotent: creates the durable consumer only if it does not already exist. Unlike P4's
    open consumer, this one uses DeliverPolicy.ALL (not NEW) - there is no activation-boundary
    concept for personal execution in this slice; every canonical EntrySignal is eligible for
    evaluation (risk/eligibility still independently blocks anything not explicitly approved)."""
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
    import uuid

    try:
        await js.consumer_info(STREAM, consumer_name)
        return True
    except Exception:
        config = ConsumerConfig(durable_name=consumer_name, ack_policy=AckPolicy.EXPLICIT,
                                deliver_policy=DeliverPolicy.ALL, filter_subject=SUBJECT,
                                deliver_subject=f"_INBOX.{consumer_name}.{uuid.uuid4().hex}",
                                ack_wait=30, max_deliver=-1)
        await js.add_consumer(STREAM, config)
        return False


async def subscribe_consumer(js: Any, consumer: ExecutionSignalConsumer, *, consumer_name: str) -> Any:
    # EntrySignalRecordMissing naks (retry-with-backoff, matching P4's open consumer); every
    # other exception also naks - this consumer never acks a message whose outcome it could not
    # safely persist.
    async def _on_message(msg: Any) -> None:
        from ..intent import EntrySignalRecordMissing
        from ..worker import ExecutionAuthorityDisabled
        try:
            consumer.handle_payload(msg.data)
            await msg.ack()
        except ExecutionAuthorityDisabled:
            # Disabled production infrastructure must consume the durable signal without
            # redelivery churn.  No intent, fence, bridge call, or broker effect is created.
            await msg.ack()
        except EntrySignalRecordMissing:
            await msg.nak()
        except Exception:
            log.exception("execution_v2 entry-signal handling failed for delivery")
            await msg.nak()

    return await js.subscribe(SUBJECT, stream=STREAM, durable=consumer_name, manual_ack=True, cb=_on_message)


async def run(ctx: RuntimeContext, stop: asyncio.Event) -> None:
    register_runtime_instance(ctx.conn, instance_id=ctx.config.holder_instance_id,
                              metadata=ctx.status_metadata)
    await ensure_execution_stream(ctx.js)
    await bootstrap_consumer(ctx.js, consumer_name=CONSUMER_NAME)
    ctx.health.mark_ready("nats")

    await subscribe_consumer(ctx.js, ctx.consumer, consumer_name=CONSUMER_NAME)
    ctx.health.mark_ready("entry_signal_consumer")

    await stop.wait()


async def main_async() -> None:
    config = RuntimeConfig.from_env()
    authority_provider = lambda: read_authority().get("state", "DISABLED")
    health = HealthState(execution_authority_mode=config.execution_authority_mode.value,
                         account_id=config.account_id, authority_provider=authority_provider)
    start_health_server(health, port=config.health_port)

    conn = connect(config.postgres)
    health.mark_ready("postgres")

    import nats
    nc = await nats.connect(config.nats_url, user=config.nats_user, password=config.nats_password,
                            name="execution-v2-runtime", max_reconnect_attempts=-1, reconnect_time_wait=2)
    js = nc.jetstream()

    fence_authority = FenceAuthority(keys={config.fence_key_id: config.fence_signing_key}, active_key_id=config.fence_key_id)
    bridge = HttpBridgeFenceClient(base_url=config.bridge_fence_url,
                                   read_base_url=config.read_bridge_url,
                                   execution_mode=f"{config.bridge_mode.upper()}_EXECUTION")
    # PostgreSQL is the sole runtime policy authority. The file path remains a legacy/bootstrap
    # reference for migration tooling, but is never consulted by the running evaluator.
    policy_connect = lambda *, readonly=False: connect(config.postgres, readonly=readonly)
    risk_policy, policy_source = read_effective_policy(policy_connect)
    def risk_policy_provider() -> RiskPolicy:
        policy, _source = read_effective_policy(policy_connect)
        return policy
    def risk_context_provider(record: dict[str, Any]) -> dict[str, Any]:
        broker_symbol = resolve_broker_symbol(record["instrument"], account_id=config.account_id,
                                              mode=config.bridge_mode)
        return bridge.read_risk_context(broker_symbol=broker_symbol)
    worker = ExecutionWorker(conn, fence_authority=fence_authority, bridge=bridge,
                             holder_instance_id=config.holder_instance_id, account_id=config.account_id,
                             mode=config.bridge_mode, risk_policy=risk_policy,
                             risk_context_provider=risk_context_provider,
                             risk_policy_provider=risk_policy_provider,
                             authority_provider=authority_provider)
    consumer = ExecutionSignalConsumer(worker, execution_authority_mode=config.execution_authority_mode,
                                       authority_provider=authority_provider)

    bridge_status = "UNKNOWN"
    try:
        health_url = config.bridge_fence_url.rsplit("/mcp", 1)[0] + "/health"
        request = urllib.request.Request(health_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            health_payload = json.loads(response.read().decode("utf-8"))
        bridge_status = "HEALTHY" if health_payload.get("ok") is True else "DEGRADED"
    except (OSError, ValueError, json.JSONDecodeError):
        bridge_status = "DEGRADED"

    ctx = RuntimeContext(conn=conn, js=js, config=config, health=health, consumer=consumer,
                         status_metadata={
                             "execution_authority_mode": authority_provider(),
                             "account_id": config.account_id,
                             "canary_key": worker.resource,
                             "risk_policy": {
                                 "enabled": risk_policy.enabled,
                                 "version": risk_policy.version,
                                 "risk_per_trade": risk_policy.risk_per_trade,
                                 "max_volume": risk_policy.max_volume,
                                 "max_signal_age_seconds": risk_policy.max_signal_age_seconds,
                                 "max_daily_loss": risk_policy.max_daily_loss,
                                 "max_concurrent_positions": risk_policy.max_concurrent_positions,
                                 "max_concurrent_orders": risk_policy.max_concurrent_orders,
                                 "allowed_accounts": list(risk_policy.allowed_accounts),
                                 "allowed_strategies": list(risk_policy.allowed_strategies),
                                 "allowed_symbols": list(risk_policy.allowed_symbols or ()),
                                 "canary_max_new_executions": risk_policy.canary_max_new_executions,
                                 "source": policy_source,
                             },
                             "execution_bridge": {"status": bridge_status},
                             "broker_account": {
                                 "status": "CONNECTED" if bridge_status == "HEALTHY" else "UNAVAILABLE",
                                 "account": "*" * max(0, len(config.account_id) - 4) + config.account_id[-4:],
                                 "currency": None,
                             },
                         })

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    log.warning("execution_v2 runtime starting: execution_authority_mode=%s bridge_mode=%s account_id=%s "
               "bridge_fence_url=%s", config.execution_authority_mode.value, config.bridge_mode,
               config.account_id, config.bridge_fence_url)
    try:
        await run(ctx, stop)
    finally:
        await nc.drain()
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async())
