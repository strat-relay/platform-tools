"""Durable persistence for an operator-edited V2 risk policy (mission
`CLAUDE-V2-RISK-EXECUTION-CONSOLE`). This is the ONLY write path for the policy the Console's
Risk & Execution page edits - `platform_api`'s new mutation endpoint calls
`write_policy_override` and nothing else, and every write goes through
`execution_v2.risk.parse_policy_dict` first, so a saved override can never be less validated
than the file-based baseline `execution_v2/runtime/service.py` loads at startup.

Explicitly NOT wired into the running execution_v2 worker/runtime in this mission: `read_
effective_policy` and `write_policy_override` are new, additive read/write paths that nothing
in `execution_v2/runtime/` calls. The runtime still loads
`orchestration/config/v2_execution_risk_policy.json` directly via `load_risk_policy()`, completely
unaffected by anything written here - see migration 018's own docstring for the same point.
Wiring the runtime to prefer this override is an explicit, separately-authorized future change.

Chosen over writing to the JSON file directly because `platform_api`'s deployment runs with
`readOnlyRootFilesystem: true` (the file lives inside the image), and because a write to a
Kubernetes-mounted ConfigMap from inside a running pod is not a normal or safe operation this
service is set up to do. PostgreSQL is this codebase's existing, conventional persistence layer
for every other piece of dynamic canonical state.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from postgres.db import connect, transaction

from .risk import DEFAULT_RISK_POLICY_PATH, RiskPolicy, RiskPolicyError, load_risk_policy, parse_policy_dict

SOURCE_OVERRIDE = "postgres_override"
SOURCE_FILE_BASELINE = "file_baseline"


def read_effective_policy(connect_fn: Callable[..., Any] = connect, *,
                          file_path: str = str(DEFAULT_RISK_POLICY_PATH)) -> tuple[RiskPolicy, str]:
    """Returns the policy the Console should display: the Postgres override if an operator has
    ever saved one, else the file-based baseline execution_v2 itself actually runs on today. The
    `str` return value tells the caller which one it got, so the UI can say so honestly."""
    with connect_fn(readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT policy_json FROM execution_v2.risk_policy_override WHERE id = 'current'")
            row = cur.fetchone()
    if row is not None:
        raw = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return parse_policy_dict(raw, source=SOURCE_OVERRIDE), SOURCE_OVERRIDE
    return load_risk_policy(file_path), SOURCE_FILE_BASELINE


def write_policy_override(raw_policy: Any, *, updated_by: str | None = None,
                          connect_fn: Callable[..., Any] = connect) -> RiskPolicy:
    """Validates `raw_policy` with the EXACT same rules the file-based baseline is held to
    (`parse_policy_dict` - shared with `load_risk_policy`), then persists it. Raises
    `RiskPolicyError` (never writes anything) if validation fails - the caller is responsible for
    turning that into a 400 response with the message attached, never a silent partial write."""
    policy = parse_policy_dict(raw_policy, source=SOURCE_OVERRIDE)
    # Only the validated, canonical field set is ever persisted - never the caller's raw dict
    # verbatim, so an unknown/extra key in the request body can never sneak into storage.
    canonical = {
        "version": policy.version, "enabled": policy.enabled, "max_volume": policy.max_volume,
        "allowed_symbols": list(policy.allowed_symbols or ()), "allowed_accounts": list(policy.allowed_accounts),
        "allowed_strategies": list(policy.allowed_strategies), "max_signal_age_seconds": policy.max_signal_age_seconds,
        "risk_per_trade": policy.risk_per_trade, "max_daily_loss": policy.max_daily_loss,
        "max_concurrent_positions": policy.max_concurrent_positions, "max_concurrent_orders": policy.max_concurrent_orders,
        "max_account_exposure": policy.max_account_exposure, "duplicate_position_policy": policy.duplicate_position_policy,
        "canary_max_new_executions": policy.canary_max_new_executions,
    }
    conn = connect_fn(readonly=False)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO execution_v2.risk_policy_override (id, policy_json, updated_by)
                          VALUES ('current', %s, %s)
                          ON CONFLICT (id) DO UPDATE
                            SET policy_json = EXCLUDED.policy_json, updated_by = EXCLUDED.updated_by,
                                updated_at = now()""",
                       (json.dumps(canonical), updated_by))
    return parse_policy_dict(canonical, source=SOURCE_OVERRIDE)


def read_canary_status(connect_fn: Callable[..., Any] = connect, *, canary_key: str,
                       configured_max: int) -> dict[str, int]:
    """Reads the REAL durable canary counter `execution_v2.worker.py`'s
    `_acquire_canary_slot` maintains (`execution_v2.canary_state`, migration 017) - never a
    number invented or cached by this module. No row yet (the canary has never actually been
    claimed against) is the normal, expected, healthy "nothing has happened yet" state, not an
    error: it reports the full configured capacity as remaining, matching what a first claim
    would actually see."""
    with connect_fn(readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max_new_executions, consumed FROM execution_v2.canary_state WHERE canary_key = %s",
                       (canary_key,))
            row = cur.fetchone()
    if row is None:
        return {"max_new_executions": configured_max, "consumed": 0, "remaining": max(0, configured_max)}
    max_new_executions, consumed = int(row[0]), int(row[1])
    return {"max_new_executions": max_new_executions, "consumed": consumed,
           "remaining": max(0, max_new_executions - consumed)}
