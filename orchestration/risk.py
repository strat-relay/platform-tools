from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from orchestration.models import SizingDecision, StrategySignal, stable_id


class RiskSizingEngine:
    def size(self, signal: StrategySignal, portfolio_id: str, account_id: str, snapshot: dict[str, Any],
             metadata: dict[str, Any], risk_fraction: float, sizing_policy_id: str = "equity-fractional-v1") -> SizingDecision:
        created = datetime.now(timezone.utc).isoformat()
        sid = stable_id("SIZE", {"signal_id": signal.signal_id, "account_id": account_id, "risk": risk_fraction,
                                  "snapshot_id": snapshot.get("snapshot_id") or snapshot.get("timestamp")})
        equity = snapshot.get("equity")
        entry, stop, risk = signal.entry_price, signal.stop_price, signal.risk_distance
        min_v, max_v, step = metadata.get("volume_min"), metadata.get("volume_max"), metadata.get("volume_step")
        tick_size, tick_value = metadata.get("tick_size"), metadata.get("tick_value")
        reason, decision = "EXECUTABLE", "EXECUTABLE"
        desired = float(equity) * risk_fraction if equity is not None else None
        raw = None; rounded = None; loss = None; margin_required = None
        if equity is None or desired is None:
            decision, reason = "REJECTED", "MISSING_ACCOUNT_DATA"
        elif risk <= 0 or (signal.direction == "LONG" and stop >= entry) or (signal.direction == "SHORT" and stop <= entry):
            decision, reason = "REJECTED", "INVALID_STOP_GEOMETRY"
        elif not all(x is not None and float(x) > 0 for x in (min_v, max_v, step, tick_size, tick_value)):
            decision, reason = "REJECTED", "MISSING_SYMBOL_METADATA"
        else:
            loss_per_lot = risk / float(tick_size) * float(tick_value)
            raw = desired / loss_per_lot if loss_per_lot > 0 else 0
            rounded = math.floor(raw / float(step)) * float(step)
            loss = rounded * loss_per_lot
            if rounded < float(min_v):
                decision, reason = "REJECTED", "BELOW_MINIMUM_VOLUME"
                rounded = 0.0
            elif rounded > float(max_v):
                decision, reason = "REJECTED", "ABOVE_MAXIMUM_VOLUME"
                rounded = float(max_v)
            if snapshot.get("free_margin") is not None and margin_required is not None and margin_required > float(snapshot["free_margin"]):
                decision, reason = "REJECTED", "INSUFFICIENT_FREE_MARGIN"
        return SizingDecision(sizing_decision_id=sid, signal_id=signal.signal_id, portfolio_id=portfolio_id,
            account_id=account_id, account_snapshot_id=snapshot.get("snapshot_id") or snapshot.get("timestamp"),
            sizing_policy_id=sizing_policy_id, strategy_id=signal.strategy_id, strategy_version=signal.strategy_version,
            desired_risk_fraction=risk_fraction, desired_risk_amount=desired, entry=entry, stop=stop, target=signal.target_price,
            raw_volume=raw, rounded_volume=rounded, volume_min=min_v, volume_max=max_v, volume_step=step,
            estimated_loss_at_stop=loss, estimated_margin_required=margin_required, decision=decision, reason=reason, created_at=created)
