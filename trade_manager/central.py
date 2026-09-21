"""Central, broker-free Trade Manager handoff.

Trade Manager evaluates policy and publishes immutable proposals only.  This
module contains the durable ownership/state contracts used by the orchestrator
and REAL consumer; it deliberately has no MT5 client and no write operation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from platform_runtime import trading_platform_runtime_dir

ROOT = Path(__file__).resolve().parents[1]
CENTRAL = trading_platform_runtime_dir(root=ROOT) / "management"
OWNERSHIP_PATH = CENTRAL / "ownership_registry.jsonl"
BROKER_STATE_PATH = CENTRAL / "broker_state.json"
PROPOSALS_PATH = CENTRAL / "management_proposals.jsonl"
INTENTS_PATH = CENTRAL / "management_intents.jsonl"
DECISIONS_PATH = CENTRAL / "management_decisions.jsonl"

ALLOWED_ACTIONS = frozenset({"MOVE_TO_BREAKEVEN", "TRAIL_STOP", "PARTIAL_CLOSE", "CLOSE_POSITION"})
TERMINAL_ACTIONS = frozenset({"PARTIAL_CLOSE", "CLOSE_POSITION"})


def normalize_account_margin_mode(raw: Any, symbolic: str | None = None) -> tuple[str, str]:
    """Map native MT5 margin-mode metadata without using trade-mode/type."""
    name = str(symbolic or "").upper()
    if name in {"HEDGING", "NETTING"}:
        # Permit already-canonical state snapshots to round-trip without
        # treating them as native enum names.  Live MT5 input still follows
        # the symbolic enum mapping below.
        return str(raw), name
    if name == "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING":
        return str(raw), "HEDGING"
    if name in {"ACCOUNT_MARGIN_MODE_RETAIL_NETTING", "ACCOUNT_MARGIN_MODE_EXCHANGE"}:
        return str(raw), "NETTING"
    return str(raw), "UNKNOWN"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def append_unique(path: Path, row: dict[str, Any], key: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("unique_key") == key:
                    return False
            except json.JSONDecodeError:
                continue
    value = {**row, "unique_key": key}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n")
    return True


def rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


class OwnershipRegistry:
    """Append-only ownership ledger; only successful REAL execution may add rows."""

    def __init__(self, path: Path = OWNERSHIP_PATH):
        self.path = path

    def current(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows(self.path):
            # Multiple REAL executions can contribute to one NETTING position;
            # never collapse those rows by broker_position_id.
            key = row.get("ownership_id") or row.get("broker_deal_id") or row.get("broker_order_id") or row.get("broker_position_id")
            if key:
                result[str(key)] = row
        return result

    def record_real_execution(self, intent: dict[str, Any], broker_result: dict[str, Any], *, position_id: str | None = None) -> dict[str, Any]:
        """Record provenance after a successful broker result, never before."""
        position_id = str(position_id or broker_result.get("position_id") or broker_result.get("ticket") or "")
        if not position_id:
            raise ValueError("successful execution did not provide a broker position identity")
        row = {
            "schema": "position-ownership-v1", "ownership_id": stable_id("OWN", {"intent_id": intent.get("execution_intent_id"), "position": position_id}),
            "strategy_id": intent.get("strategy_id"), "instance_id": intent.get("strategy_instance_id"),
            "source_event_id": intent.get("source_event_id") or intent.get("signal_id"), "signal_id": intent.get("signal_id"),
            "intent_id": intent.get("execution_intent_id"), "broker_symbol": intent.get("broker_symbol"),
            "broker_order_id": broker_result.get("order_id"), "broker_deal_id": broker_result.get("deal_id"),
            "broker_position_id": position_id, "open_time": utc_now(),
            "initial_volume": intent.get("approved_volume"), "current_known_volume": intent.get("approved_volume"),
            "ownership_state": "OWNED", "account_position_mode": broker_result.get("account_position_mode"),
            "source": "REAL_EXECUTION_CONSUMER", "updated_at": utc_now(),
        }
        append_unique(self.path, row, row["ownership_id"])
        return row

    def prove(self, position: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        current = self.current()
        key = str(position.get("ticket") or position.get("position_id") or position.get("broker_position_id") or "")
        candidates = [row for row in current.values() if str(row.get("broker_position_id")) == key or str(row.get("broker_symbol")) == str(position.get("symbol"))]
        if not key:
            return False, "UNOWNED_POSITION", None
        exact = [row for row in candidates if str(row.get("broker_position_id")) == key and row.get("ownership_state") == "OWNED"]
        if not exact:
            return False, "UNOWNED_POSITION", None
        if str(position.get("account_position_mode", "")).upper() == "NETTING":
            same_symbol = [row for row in current.values() if row.get("broker_symbol") == position.get("symbol") and row.get("ownership_state") == "OWNED"]
            if len({str(row.get("strategy_id")) + "|" + str(row.get("instance_id")) for row in same_symbol}) > 1:
                return False, "AMBIGUOUS_NET_POSITION_OWNERSHIP", None
        return True, "POSITION_OWNERSHIP_PROVEN", exact[0]

    def record_management_result(self, ownership_id: str, *, state: str, current_volume: float | None = None) -> None:
        current = self.current()
        row = next((value for value in current.values() if value.get("ownership_id") == ownership_id), None)
        if not row:
            return
        updated = {**row, "ownership_state": state, "current_known_volume": current_volume if current_volume is not None else row.get("current_known_volume"), "updated_at": utc_now()}
        append_unique(self.path, updated, stable_id("OWNSTATE", {"ownership": ownership_id, "state": state, "volume": updated.get("current_known_volume")}))


class BrokerStateStream:
    def __init__(self, path: Path = BROKER_STATE_PATH):
        self.path = path

    def save(self, *, account: dict[str, Any], positions: list[dict[str, Any]], pending_orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        previous = self.load() or {}
        version = int(previous.get("snapshot_version", 0)) + 1
        symbolic_mode = account.get("account_margin_mode") or account.get("position_mode") or account.get("account_position_mode")
        raw_mode, mode = normalize_account_margin_mode(account.get("account_margin_mode_raw"), symbolic_mode)
        value = {"schema": "central-broker-state-v1", "snapshot_version": version, "captured_at": utc_now(),
                 "account_margin_mode_raw": raw_mode, "account_margin_mode": account.get("account_margin_mode", "UNKNOWN"),
                 "account_position_mode": mode, "account": account,
                 "positions": positions, "pending_orders": pending_orders or [], "read_only_source": "REAL_EXECUTION_CONSUMER"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"); tmp.replace(self.path)
        return value

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def healthy(self, max_age_seconds: int = 120) -> bool:
        state = self.load()
        if not state or not state.get("snapshot_version") or not state.get("captured_at"):
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["captured_at"].replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            return False
        return age <= max_age_seconds

    def refresh_from_provider(self, provider: Any, account_id: str) -> dict[str, Any]:
        """Refresh the normalized read-only state from the REAL consumer."""
        account = provider.account_snapshot(account_id)
        positions = provider.open_positions(account_id)
        pending = provider.pending_orders(account_id)
        raw = account.get("raw", {}) if isinstance(account, dict) else {}
        account = {**account, "account_margin_mode_raw": raw.get("account_margin_mode_raw"),
                   "account_margin_mode": raw.get("account_margin_mode"),
                   "position_mode": raw.get("position_mode")}
        return self.save(account=account, positions=positions, pending_orders=pending)


@dataclass(frozen=True)
class ManagementProposal:
    management_proposal_id: str
    proposal_version: str
    strategy_id: str
    instance_id: str
    position_identity: str
    broker_symbol: str
    source_intent_id: str | None
    source_event_id: str | None
    action: str
    current_position_snapshot_version: int
    requested_stop: float | None
    requested_target: float | None
    requested_close_volume: float | None
    reason_code: str
    policy_id: str
    policy_version: str
    decision_time: str
    proposal_emitted_at: str
    source_state_hash: str

    @classmethod
    def from_decision(cls, decision: dict[str, Any], position: dict[str, Any], snapshot_version: int) -> "ManagementProposal":
        action_map = {"MOVE_BREAKEVEN": "MOVE_TO_BREAKEVEN", "TRAIL_STOP": "TRAIL_STOP", "REDUCE_POSITION": "PARTIAL_CLOSE", "CLOSE_POSITION": "CLOSE_POSITION"}
        action = action_map.get(str(decision.get("action")), "HOLD")
        identity = str(position.get("ticket") or position.get("broker_position_id") or position.get("economic_position_id"))
        body = {"position": identity, "action": action, "snapshot": snapshot_version, "policy": decision.get("management_policy_id") or decision.get("management_policy")}
        return cls(stable_id("MP", body), "management-proposal-v1", str(position.get("strategy_id")), str(position.get("instance_id") or position.get("strategy_instance_id") or ""), identity, str(position.get("symbol")), position.get("source_intent_id"), position.get("source_event_id"), action, snapshot_version, decision.get("proposed_price") if action in {"MOVE_TO_BREAKEVEN", "TRAIL_STOP"} else None, decision.get("proposed_target"), decision.get("proposed_size") if action == "PARTIAL_CLOSE" else None, str((decision.get("reason_codes") or ["UNSPECIFIED"])[0]), str(decision.get("management_policy_id") or decision.get("management_policy") or ""), str(decision.get("management_policy_version") or ""), str(decision.get("timestamp") or utc_now()), utc_now(), hashlib.sha256(json.dumps(position, sort_keys=True, default=str).encode()).hexdigest())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def stop_is_safe(position: dict[str, Any], requested_stop: float | None) -> bool:
    if requested_stop is None:
        return True
    current = position.get("current_stop")
    if current is None:
        return True
    return float(requested_stop) >= float(current) if str(position.get("direction", "")).upper() == "LONG" else float(requested_stop) <= float(current)


def authorize(proposal: dict[str, Any], *, state: dict[str, Any], registry: OwnershipRegistry) -> tuple[bool, str, dict[str, Any] | None]:
    if proposal.get("action") not in ALLOWED_ACTIONS:
        return False, "UNSUPPORTED_MANAGEMENT_ACTION", None
    if proposal.get("proposal_version") != "management-proposal-v1":
        return False, "INVALID_MANAGEMENT_PROPOSAL_SCHEMA", None
    if not state or int(proposal.get("current_position_snapshot_version", -1)) != int(state.get("snapshot_version", -2)):
        return False, "POSITION_STATE_CHANGED", None
    position = next((p for p in state.get("positions", []) if str(p.get("ticket") or p.get("position_id") or p.get("broker_position_id")) == str(proposal.get("position_identity"))), None)
    if position is None:
        return False, "POSITION_NOT_FOUND", None
    owned, reason, owner = registry.prove({**position, "account_position_mode": state.get("account_position_mode")})
    if not owned:
        return False, reason, None
    if owner.get("strategy_id") != proposal.get("strategy_id") or owner.get("instance_id", "") != proposal.get("instance_id", ""):
        return False, "OWNERSHIP_STRATEGY_OR_INSTANCE_MISMATCH", None
    if not stop_is_safe(position, proposal.get("requested_stop")):
        return False, "RISK_INCREASING_STOP_CHANGE", None
    if proposal.get("action") == "PARTIAL_CLOSE" and float(proposal.get("requested_close_volume") or 0) > float(owner.get("current_known_volume") or 0):
        return False, "CLOSE_VOLUME_EXCEEDS_OWNED_VOLUME", None
    action_key = stable_id("MA", {"ownership": owner.get("ownership_id"), "proposal": proposal.get("management_proposal_id"), "action": proposal.get("action"), "requested_stop": proposal.get("requested_stop"), "requested_target": proposal.get("requested_target"), "requested_close_volume": proposal.get("requested_close_volume")})
    if any(row.get("action_key") == action_key and row.get("status") == "AUTHORIZED" for row in rows(INTENTS_PATH)):
        return False, "DUPLICATE_MANAGEMENT_ACTION", None
    intent = {"schema_version": "management-execution-intent-v1", "intent_type": proposal.get("action"), "intent_id": stable_id("MINT", {"action_key": action_key}), "management_proposal_id": proposal.get("management_proposal_id"), "strategy_id": proposal.get("strategy_id"), "instance_id": proposal.get("instance_id"), "ownership_id": owner.get("ownership_id"), "broker_position_id": owner.get("broker_position_id"), "broker_symbol": proposal.get("broker_symbol"), "requested_stop": proposal.get("requested_stop"), "requested_target": proposal.get("requested_target"), "requested_close_volume": proposal.get("requested_close_volume"), "position_snapshot_version": state.get("snapshot_version"), "policy_id": proposal.get("policy_id"), "policy_version": proposal.get("policy_version"), "created_at": utc_now(), "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "action_key": action_key, "status": "AUTHORIZED", "provenance": {"source": "TRADE_MANAGER", "management_proposal_id": proposal.get("management_proposal_id")}}
    append_unique(INTENTS_PATH, intent, intent["intent_id"])
    return True, "AUTHORIZED", intent


def authorize_pending_proposals() -> dict[str, int]:
    """Orchestrator-side proposal sink; never talks to a broker."""
    state = BrokerStateStream().load() or {}
    registry = OwnershipRegistry()
    authorized = rejected = 0
    for proposal in rows(PROPOSALS_PATH):
        decision_key = stable_id("MPDEC", {"proposal": proposal.get("management_proposal_id")})
        if any(row.get("unique_key") == decision_key for row in rows(DECISIONS_PATH)):
            continue
        ok, reason, intent = authorize(proposal, state=state, registry=registry)
        row = {"schema": "management-authorization-v1", "management_proposal_id": proposal.get("management_proposal_id"), "status": "AUTHORIZED" if ok else "REJECTED", "reason": reason, "intent_id": intent.get("intent_id") if intent else None, "decision_time": utc_now()}
        append_unique(DECISIONS_PATH, row, decision_key)
        authorized += int(ok); rejected += int(not ok)
    return {"authorized": authorized, "rejected": rejected}
