"""Broker-aware, read-only valuation for NET_PROFIT_USD exits."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


class ExitValuationError(ValueError):
    pass


@dataclass(frozen=True)
class ExitValuation:
    account_currency: str
    close_price: float
    gross_profit: float
    charged_commission: float
    estimated_close_commission: float
    swap: float
    other_fees: float
    estimated_net_profit: float
    pip_size: float
    commission_estimate_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_currency": self.account_currency,
            "close_price": self.close_price,
            "gross_profit": self.gross_profit,
            "charged_commission": self.charged_commission,
            "estimated_close_commission": self.estimated_close_commission,
            "swap": self.swap,
            "other_fees": self.other_fees,
            "estimated_net_profit": self.estimated_net_profit,
            "pip_size": self.pip_size,
            "commission_estimate_source": self.commission_estimate_source,
            "spread_counted_separately": False,
        }


def estimate_net_liquidation_profit(*, direction: str, position: dict[str, Any],
                                    symbol_info: dict[str, Any], account_info: dict[str, Any],
                                    bid: float, ask: float,
                                    charged_commission: float | None = None,
                                    estimated_close_commission: float | None = None,
                                    require_usd: bool = True) -> ExitValuation:
    """Value the close side once, using actual open price and executable BID/ASK.

    ``tick_value`` is assumed to be account-currency value as supplied by MT5.  No separate
    spread subtraction is made because the executable close side already includes spread.
    """
    direction = str(direction).upper()
    if direction not in {"LONG", "SHORT"}:
        raise ExitValuationError("UNSUPPORTED_DIRECTION")
    currency = str(account_info.get("currency") or "").upper()
    if require_usd and currency != "USD":
        raise ExitValuationError("ACCOUNT_CURRENCY_NOT_USD")
    try:
        entry = float(position["price_open"])
        volume = float(position["volume"])
        tick_size = float(symbol_info["tick_size"])
        tick_value = float(symbol_info["tick_value"])
        bid, ask = float(bid), float(ask)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExitValuationError("BROKER_VALUATION_FIELDS_MISSING") from exc
    if not all(isfinite(x) for x in (entry, volume, tick_size, tick_value, bid, ask)) or volume <= 0 or tick_size <= 0 or tick_value <= 0 or ask < bid:
        raise ExitValuationError("BROKER_VALUATION_FIELDS_INVALID")
    close_price = bid if direction == "LONG" else ask
    movement = close_price - entry if direction == "LONG" else entry - close_price
    gross = movement / tick_size * tick_value * volume
    charged = float(charged_commission if charged_commission is not None else position.get("commission") or 0.0)
    swap = float(position.get("swap") or 0.0)
    other = float(position.get("fees") or position.get("fee") or 0.0)
    if estimated_close_commission is not None:
        closing = abs(float(estimated_close_commission))
        source = "EXPLICIT_SYMBOL_ESTIMATE"
    elif symbol_info.get("commission_per_lot") is not None:
        closing = abs(float(symbol_info["commission_per_lot"])) * volume
        source = "SYMBOL_COMMISSION_PER_LOT"
    elif charged != 0:
        closing = abs(charged)
        source = "MIRROR_CHARGED_COMMISSION"
    else:
        closing = 0.0
        source = "ZERO_UNAVAILABLE"
    return ExitValuation(currency, close_price, gross, charged, closing, swap, other,
                         gross + charged + swap + other - closing,
                         float(symbol_info.get("pip_size") or symbol_info.get("point") or tick_size), source)


def estimate_initial_monetary_risk(*, position: dict[str, Any], symbol_info: dict[str, Any],
                                  initial_stop: float, charged_commission: float = 0.0,
                                  estimated_close_commission: float | None = None) -> float:
    """Return immutable initial risk in the broker account currency.

    The stop is the ManagedTrade's captured initial stop; current SL changes never enter this
    calculation.  Costs are included once so PROFIT_R remains stable after stop movement.
    """
    try:
        entry = float(position["price_open"])
        volume = float(position["volume"])
        tick_size = float(symbol_info["tick_size"])
        tick_value = float(symbol_info["tick_value"])
        initial_stop = float(initial_stop)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExitValuationError("INITIAL_RISK_FIELDS_MISSING") from exc
    if not all(isfinite(x) for x in (entry, volume, tick_size, tick_value, initial_stop)) or volume <= 0 or tick_size <= 0 or tick_value <= 0:
        raise ExitValuationError("INITIAL_RISK_FIELDS_INVALID")
    gross = abs(entry - initial_stop) / tick_size * tick_value * volume
    closing = (abs(float(estimated_close_commission)) if estimated_close_commission is not None
               else abs(float(symbol_info.get("commission_per_lot") or 0.0)) * volume)
    risk = gross + abs(float(charged_commission)) + closing
    if not isfinite(risk) or risk <= 0:
        raise ExitValuationError("INITIAL_RISK_NOT_POSITIVE")
    return risk
