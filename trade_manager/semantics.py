"""Canonical bid/ask semantics for managing an existing position."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceSemantics:
    direction: str
    bid: float
    ask: float

    def __post_init__(self) -> None:
        if self.direction.upper() not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if self.ask < self.bid:
            raise ValueError("ask must be >= bid")

    @property
    def direction_upper(self) -> str:
        return self.direction.upper()

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mark_price(self) -> float:
        """Neutral valuation mark; never used as a broker execution price."""
        return (self.bid + self.ask) / 2.0

    @property
    def entry_price(self) -> float:
        """Price to open: ask for LONG, bid for SHORT."""
        return self.ask if self.direction_upper == "LONG" else self.bid

    @property
    def close_price(self) -> float:
        """Price to close: bid for LONG, ask for SHORT."""
        return self.bid if self.direction_upper == "LONG" else self.ask

    @property
    def stop_cross_price(self) -> float:
        return self.close_price

    @property
    def target_cross_price(self) -> float:
        return self.close_price

    @property
    def mfe_price(self) -> float:
        """Current executable close-side price used for favorable excursion."""
        return self.close_price

    @property
    def mae_price(self) -> float:
        """Current executable close-side price used for adverse excursion."""
        return self.close_price

    def as_dict(self) -> dict[str, float | str]:
        return {"direction": self.direction_upper, "bid": self.bid, "ask": self.ask,
                "spread": self.spread, "mark_price": self.mark_price,
                "entry_executable_price": self.entry_price,
                "close_executable_price": self.close_price,
                "stop_cross_price": self.stop_cross_price,
                "target_cross_price": self.target_cross_price,
                "mfe_price": self.mfe_price, "mae_price": self.mae_price}


def price_semantics(direction: str, bid: float, ask: float) -> PriceSemantics:
    return PriceSemantics(direction, float(bid), float(ask))
