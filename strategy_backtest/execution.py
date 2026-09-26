from __future__ import annotations

from dataclasses import dataclass, field

from .models import CostModel, EntrySignal, EntrySignalOutcome, MarketEvent


SAME_BAR_POLICY = "CONSERVATIVE_STOP_FIRST"


@dataclass
class _OpenTrade:
    signal: EntrySignal
    entry_timestamp: int
    entry_price: float


@dataclass
class SimulatedExecution:
    cost_model: CostModel
    outcomes: list[EntrySignalOutcome] = field(default_factory=list)
    _open: dict[str, _OpenTrade] = field(default_factory=dict)
    _pending: list[EntrySignal] = field(default_factory=list)

    def submit(self, signal: EntrySignal) -> None:
        self._pending.append(signal)

    def _fill_price(self, signal: EntrySignal, event: MarketEvent) -> float | None:
        if event.open_timestamp < signal.decision_timestamp:
            return None
        if signal.order_type == "MARKET":
            return event.open
        if signal.order_type == "LIMIT":
            touched = event.low <= signal.entry_price <= event.high
            return signal.entry_price if touched else None
        if signal.direction == "LONG" and event.high >= signal.entry_price:
            return signal.entry_price
        if signal.direction == "SHORT" and event.low <= signal.entry_price:
            return signal.entry_price
        return None

    def _close(self, trade: _OpenTrade, event: MarketEvent, price: float, status: str, reason: str) -> None:
        signal = trade.signal
        risk = abs(signal.entry_price - signal.stop_price)
        signed_move = price - trade.entry_price if signal.direction == "LONG" else trade.entry_price - price
        realized_r = signed_move / risk - self.cost_model.spread_price / risk - self.cost_model.commission_r
        self.outcomes.append(EntrySignalOutcome(signal.signal_id, status, event.close_timestamp, price, realized_r, reason, {"same_bar_policy": SAME_BAR_POLICY, "cost_model": self.cost_model.model_id, "entry_timestamp": trade.entry_timestamp}))
        self._open.pop(signal.signal_id, None)

    def consume(self, event: MarketEvent) -> None:
        for signal in tuple(self._pending):
            price = self._fill_price(signal, event)
            if price is not None:
                self._open[signal.signal_id] = _OpenTrade(signal, event.open_timestamp, price)
                self._pending.remove(signal)
            elif signal.expiry_timestamp is not None and event.close_timestamp >= signal.expiry_timestamp:
                self.outcomes.append(EntrySignalOutcome(signal.signal_id, "EXPIRED", event.close_timestamp, event.close, 0.0, "ENTRY_EXPIRY", {"cost_model": self.cost_model.model_id}))
                self._pending.remove(signal)

        for trade in tuple(self._open.values()):
            signal = trade.signal
            stop_hit = event.low <= signal.stop_price if signal.direction == "LONG" else event.high >= signal.stop_price
            target_hit = event.high >= signal.target_price if signal.direction == "LONG" else event.low <= signal.target_price
            if stop_hit:
                self._close(trade, event, signal.stop_price, "STOPPED", "STOP_HIT" if not target_hit else "SAME_BAR_STOP_AND_TARGET")
            elif target_hit:
                self._close(trade, event, signal.target_price, "TARGET_HIT", "TARGET_HIT")
            elif signal.expiry_timestamp is not None and event.close_timestamp >= signal.expiry_timestamp:
                self._close(trade, event, event.close, "TIME_EXIT", "TIME_EXIT")

    def finalize(self, event: MarketEvent | None) -> None:
        if event is None:
            return
        for trade in tuple(self._open.values()):
            self._close(trade, event, event.close, "TIME_EXIT", "END_OF_DATA")
