"""Disabled, explicit plumbing for the four Liquidity strategy instances.

This module contains identity, timestamp, startup-baseline, and signal-shape
logic only.  It does not start a publisher, append to the production signal
ledger, call a bridge, or enable a route.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestration.models import StrategySignal, stable_id
from orchestration.replay_guard import EPOCH_PATH, eligibility, load_epoch, records_by_strategy


@dataclass(frozen=True)
class LiquidityInstanceDefinition:
    strategy_id: str
    instance_id: str
    publisher_id: str
    parent_strategy_id: str
    symbol: str
    entry_fraction: float
    state_path: str
    source_namespace: str
    dedupe_namespace: str
    target_r: float = 1.25
    max_retrace_candles: int = 3
    max_hold_minutes: int = 120
    stop_semantics: str = "sweep extreme plus max(0.10 ATR, 1.25x spread, broker stop minimum)"
    entry_semantics: str = "V1 sweep -> reclaim -> displacement -> micro structure shift -> frozen displacement retracement"
    broker_symbol: str | None = None
    execution_account: str = "exness-shadow-1"
    enabled: bool = False
    real_execution_enabled: bool = False

    def behavior_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "parent_strategy_id": self.parent_strategy_id,
            "symbol": self.symbol,
            "entry_fraction": self.entry_fraction,
            "target_r": self.target_r,
            "max_retrace_candles": self.max_retrace_candles,
            "max_hold_minutes": self.max_hold_minutes,
            "stop_semantics": self.stop_semantics,
            "entry_semantics": self.entry_semantics,
        }

    def behavior_hash(self) -> str:
        raw = json.dumps(self.behavior_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def source_event_id(self, setup_id: str, event_time: str) -> str:
        return stable_id(self.source_namespace, {
            "strategy_id": self.strategy_id,
            "instance_id": self.instance_id,
            "symbol": self.symbol,
            "setup_id": setup_id,
            "event_time": event_time,
        })

    def dedupe_key(self, setup_id: str, event_time: str) -> str:
        return stable_id(self.dedupe_namespace, {
            "strategy_id": self.strategy_id,
            "instance_id": self.instance_id,
            "setup_id": setup_id,
            "event_time": event_time,
        })


LIQUIDITY_INSTANCE_DEFINITIONS: tuple[LiquidityInstanceDefinition, ...] = (
    LiquidityInstanceDefinition(
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        instance_id="liquidity-xau-base",
        publisher_id="liquidity-xau-base-publisher",
        parent_strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        symbol="XAUUSDm", entry_fraction=0.50,
        state_path="liquidity_displacement_forward_state.json",
        source_namespace="LIQUIDITY_BASE_EVENT",
        dedupe_namespace="LIQUIDITY_BASE_DEDUPE",
        max_retrace_candles=3, broker_symbol="XAUUSDm",
    ),
    LiquidityInstanceDefinition(
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1",
        instance_id="liquidity-xau33",
        publisher_id="liquidity-xau33-publisher",
        parent_strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        symbol="XAUUSDm", entry_fraction=1.0 / 3.0,
        state_path="liquidity_displacement_xau33_state.json",
        source_namespace="LIQUIDITY_XAU33_EVENT",
        dedupe_namespace="LIQUIDITY_XAU33_DEDUPE",
        max_retrace_candles=5, broker_symbol="XAUUSDm",
    ),
    LiquidityInstanceDefinition(
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1",
        instance_id="liquidity-btc25",
        publisher_id="liquidity-btc25-publisher",
        parent_strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        symbol="BTCUSDm", entry_fraction=0.25,
        state_path="liquidity_displacement_btc25_state.json",
        source_namespace="LIQUIDITY_BTC25_EVENT",
        dedupe_namespace="LIQUIDITY_BTC25_DEDUPE",
        max_retrace_candles=5, broker_symbol="BTCUSDm",
    ),
    LiquidityInstanceDefinition(
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1",
        instance_id="liquidity-usdjpy25",
        publisher_id="liquidity-usdjpy25-publisher",
        parent_strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        symbol="USDJPYm", entry_fraction=0.25,
        state_path="liquidity_displacement_usdjpy25_state.json",
        source_namespace="LIQUIDITY_USDJPY25_EVENT",
        dedupe_namespace="LIQUIDITY_USDJPY25_DEDUPE",
        max_retrace_candles=5, broker_symbol="USDJPYm",
    ),
)

DEFINITIONS_BY_ID = {x.strategy_id: x for x in LIQUIDITY_INSTANCE_DEFINITIONS}


def liquidity_definition(strategy_id: str) -> LiquidityInstanceDefinition:
    try:
        return DEFINITIONS_BY_ID[strategy_id]
    except KeyError as exc:
        raise ValueError(f"unknown Liquidity strategy instance: {strategy_id}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiquidityInstanceAdapter:
    """Read-only adapter for one explicitly isolated runner state file."""

    def __init__(self, root: Path, definition: LiquidityInstanceDefinition):
        self.root = Path(root)
        self.definition = definition
        self.state_path = self.root / definition.state_path
        self.baseline_path = self.root / "runtime" / "orchestration" / f"{definition.instance_id}-startup-baseline.json"

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"invalid state object: {self.state_path}")
        return raw

    def startup_baseline(self) -> dict[str, Any]:
        """Return a durable-baseline payload without writing it."""
        state = self.load_state()
        rows = list((state.get("signals") or {}).values())
        ids = sorted(str(x.get("setup_id")) for x in rows if x.get("setup_id"))
        latest = max((str(x.get("simulated_fill_timestamp") or x.get("detected_at") or "") for x in rows), default=None)
        return {"instance_id": self.definition.instance_id, "setup_ids": ids, "latest_event_time": latest}

    def _live_baseline(self) -> dict[str, Any]:
        if not self.baseline_path.exists():
            return {}
        raw = json.loads(self.baseline_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def adapt_record(self, record: dict[str, Any]) -> dict[str, Any]:
        setup_id = str(record.get("setup_id") or "")
        event_time = str(record.get("simulated_fill_timestamp") or record.get("fill_timestamp") or record.get("detected_at") or "")
        decision_time = record.get("decision_time") or record.get("actionable_at") or record.get("detected_at")
        return {
            "strategy_id": self.definition.strategy_id,
            "instance_id": self.definition.instance_id,
            "publisher_id": self.definition.publisher_id,
            "symbol": self.definition.symbol,
            "setup_id": setup_id,
            "direction": str(record.get("direction") or ""),
            "event_time": event_time,
            "decision_time": decision_time,
            "entry": record.get("entry_realistic", record.get("entry_theoretical")),
            "stop": record.get("stop_loss"),
            "target": record.get("target_theoretical"),
            "source_health": bool(record.get("source_read_health", record.get("source_health", True))),
            "gap_recovery": bool(record.get("gap_recovery", False)),
            "signal_eligibility": {
                "historical_paper_row": True,
                "execution_eligible_without_live_publication": False,
                "requires_explicit_signal_emitted_at": True,
            },
            "source_event_id": self.definition.source_event_id(setup_id, event_time),
            "dedupe_key": self.definition.dedupe_key(setup_id, event_time),
        }

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        """Discover only explicitly published, post-baseline rows.

        Existing paper rows do not contain ``signal_emitted_at`` and are
        therefore never promoted by this adapter.  A future live publisher
        must write that field explicitly into its own state namespace.
        """
        state = self.load_state()
        baseline_file = self._live_baseline()
        baseline = set((state.get("publisher_startup_baseline") or {}).get("setup_ids", []))
        if baseline_file.get("baseline_setup_id"):
            baseline.add(str(baseline_file["baseline_setup_id"]))
        baseline_event = str(baseline_file.get("baseline_event_timestamp") or "")
        publisher = LiquidityLivePublisher(
            replace(self.definition, real_execution_enabled=True),
            mode="REAL_ELIGIBLE", baseline_setup_ids=baseline,
        )
        result: list[StrategySignal] = []
        for record in (state.get("signals") or {}).values():
            emitted_at = record.get("signal_emitted_at")
            # The runner's paper rows do not carry a live emission timestamp.
            # For a post-baseline state transition, this adapter is the live
            # publication boundary and stamps the publication at discovery.
            # Existing rows never qualify because their event is at/before the
            # persisted baseline.
            event_time = str(record.get("simulated_fill_timestamp") or record.get("fill_timestamp") or "")
            setup_id = str(record.get("setup_id") or "")
            if not event_time or setup_id in baseline or (baseline_event and event_time <= baseline_event):
                continue
            if state.get("last_read_error") or state.get("last_gap"):
                continue
            emitted_at = str(emitted_at or utc_now())
            adapted = self.adapt_record(record)
            # Decision time is causal market time, not the wall-clock time at
            # which a restarted publisher rediscovered the row.
            adapted["decision_time"] = event_time
            signal = publisher.publish(adapted, emitted_at=emitted_at)
            epoch = load_epoch(EPOCH_PATH)
            watermark = records_by_strategy(epoch or {}).get(self.definition.strategy_id)
            if signal is not None and watermark and not eligibility(signal.to_dict(), watermark)[0]:
                signal = None
            if signal is not None and signal.signal_id not in seen_signal_ids:
                result.append(signal)
        return result


class LiquidityLivePublisher:
    """Disabled publisher contract; returns objects, never writes production state."""

    def __init__(self, definition: LiquidityInstanceDefinition, mode: str = "PAPER_ONLY",
                 baseline_setup_ids: Iterable[str] = ()):
        if mode not in {"PAPER_ONLY", "SHADOW", "REAL_ELIGIBLE"}:
            raise ValueError("unsupported publisher mode")
        self.definition = definition
        self.mode = mode
        self.baseline_setup_ids = set(baseline_setup_ids)
        self.published_dedupe_keys: set[str] = set()

    def publish(self, adapted: dict[str, Any], *, emitted_at: str | None = None) -> StrategySignal | None:
        if self.mode != "REAL_ELIGIBLE" or not self.definition.real_execution_enabled:
            return None
        setup_id = str(adapted.get("setup_id") or "")
        if not setup_id or setup_id in self.baseline_setup_ids:
            return None
        if not adapted.get("source_health", False) or adapted.get("gap_recovery", False):
            return None
        event_time = str(adapted.get("event_time") or "")
        if not emitted_at:
            raise ValueError("explicit signal_emitted_at is required")
        key = self.definition.dedupe_key(setup_id, event_time)
        if key in self.published_dedupe_keys:
            return None
        self.published_dedupe_keys.add(key)
        entry = float(adapted["entry"]); stop = float(adapted["stop"]); target = float(adapted["target"])
        risk_distance = abs(entry - stop); target_distance = abs(target - entry)
        signal_id = stable_id("SIG", {"strategy_id": self.definition.strategy_id, "instance_id": self.definition.instance_id, "source_event_id": adapted["source_event_id"]})
        return StrategySignal(
            signal_id=signal_id, schema_version="strategy-signal-v1",
            strategy_id=self.definition.strategy_id, strategy_version="V1",
            strategy_instance_id=self.definition.instance_id,
            source_event_id=adapted["source_event_id"], market_event_id=None,
            setup_id=setup_id, entry_opportunity_id=setup_id, economic_position_id=None,
            created_at=emitted_at, signal_timestamp=event_time,
            symbol=self.definition.symbol, canonical_symbol=self.definition.symbol.rstrip("m"),
            broker_symbol_hint=self.definition.broker_symbol or self.definition.symbol,
            direction=str(adapted["direction"]), entry_type="MARKET",
            entry_price=entry, stop_price=stop, target_price=target,
            risk_distance=risk_distance, target_distance=target_distance,
            target_r=(target_distance / risk_distance) if risk_distance else None,
            timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
            entry_mechanism=("LIQUIDITY_RECLAIM",), strategy_metadata={"entry_fraction": self.definition.entry_fraction},
            provenance={"source_state_reference": str(self.definition.state_path), "source_read_health": adapted["source_health"], "gap_recovery": adapted["gap_recovery"], "publisher_id": self.definition.publisher_id},
            decision_time=str(adapted.get("decision_time") or event_time), signal_emitted_at=emitted_at,
        )
