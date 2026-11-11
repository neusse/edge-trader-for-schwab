"""Translate canonical order intents into Schwab's REST order trees."""
from __future__ import annotations

from decimal import Decimal

from .models import (
    AssetType, BracketOrderIntent, MultiLegOrderIntent, OcoOrderIntent, OrderInstruction,
    OrderLegIntent, OrderType, SimpleOrderIntent, TimeInForce, TrailingOrderIntent,
)

_EXIT = {
    OrderInstruction.BUY: OrderInstruction.SELL,
    OrderInstruction.SELL_SHORT: OrderInstruction.BUY_TO_COVER,
    OrderInstruction.BUY_TO_OPEN: OrderInstruction.SELL_TO_CLOSE,
    OrderInstruction.SELL_TO_OPEN: OrderInstruction.BUY_TO_CLOSE,
}


def _qty(value: Decimal) -> int:
    if value <= 0 or value != value.to_integral_value():
        raise ValueError("Schwab order quantity must be a positive whole number")
    return int(value)


def _price(value: Decimal) -> str:
    if value <= 0:
        raise ValueError("Schwab order price must be positive")
    return format(value, "f")


def _duration(value: TimeInForce) -> str:
    return "GOOD_TILL_CANCEL" if value == TimeInForce.GTC else value.value.upper()


def _leg(leg: OrderLegIntent, instruction: OrderInstruction | None = None) -> dict:
    if leg.asset_type not in (AssetType.EQUITY, AssetType.OPTION):
        raise ValueError("Schwab canonical orders support equities and options only")
    action = instruction or leg.instruction
    if leg.asset_type == AssetType.OPTION and action not in (
            OrderInstruction.BUY_TO_OPEN, OrderInstruction.SELL_TO_OPEN,
            OrderInstruction.BUY_TO_CLOSE, OrderInstruction.SELL_TO_CLOSE):
        raise ValueError("Schwab option orders require an explicit open or close instruction")
    quantity = (float(leg.quantity) if leg.asset_type == AssetType.EQUITY and
                leg.quantity != leg.quantity.to_integral_value() else _qty(leg.quantity))
    return {"instruction": action.value.upper().replace("-", "_"),
            "quantity": quantity,
            "instrument": {"symbol": leg.symbol, "assetType": leg.asset_type.value.upper()}}


def _simple(intent: SimpleOrderIntent) -> dict:
    body = {"orderStrategyType": "SINGLE", "orderType": intent.order_type.value.upper().replace("-", "_"),
            "session": "NORMAL", "duration": _duration(intent.time_in_force),
            "orderLegCollection": [_leg(intent.leg)]}
    if intent.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
        body["price"] = _price(intent.limit_price)
    if intent.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        body["stopPrice"] = _price(intent.stop_price)
    if intent.order_type not in (OrderType.MARKET, OrderType.LIMIT,
                                 OrderType.STOP, OrderType.STOP_LIMIT):
        raise ValueError("unsupported Schwab simple order type")
    return body


def _stop(leg: OrderLegIntent, stop, duration: str) -> dict:
    body = {"orderStrategyType": "SINGLE", "orderType": "STOP_LIMIT" if stop.limit_price is not None else "STOP",
            "session": "NORMAL", "duration": duration,
            "stopPrice": _price(stop.stop_price), "orderLegCollection": [_leg(leg)]}
    if stop.limit_price is not None:
        body["price"] = _price(stop.limit_price)
    return body


def _target(leg: OrderLegIntent, price: Decimal, duration: str) -> dict:
    return {"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL",
            "duration": duration, "price": _price(price), "orderLegCollection": [_leg(leg)]}


def build_schwab_order(intent) -> dict:
    """Return an unsent Schwab order tree; the caller owns all action guards."""
    if not isinstance(intent, (SimpleOrderIntent, BracketOrderIntent, OcoOrderIntent,
                               TrailingOrderIntent, MultiLegOrderIntent)):
        raise TypeError(f"unknown Schwab order intent: {type(intent).__name__}")
    duration = (intent.entry.time_in_force if isinstance(intent, BracketOrderIntent)
                else intent.time_in_force)
    if duration not in (TimeInForce.DAY, TimeInForce.GTC):
        raise ValueError("Schwab canonical orders require DAY or GTC time in force")
    if isinstance(intent, SimpleOrderIntent):
        return _simple(intent)
    if isinstance(intent, BracketOrderIntent):
        if intent.entry.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("Schwab bracket entry must be market or limit")
        exit_instruction = _EXIT.get(intent.entry.leg.instruction)
        if exit_instruction is None:
            raise ValueError("Schwab bracket entry requires an opening instruction")
        entry = _simple(intent.entry)
        exit_leg = OrderLegIntent(
            intent.entry.leg.symbol, intent.entry.leg.asset_type,
            exit_instruction, intent.entry.leg.quantity)
        entry["orderStrategyType"] = "TRIGGER"
        entry["childOrderStrategies"] = [{
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                _target(exit_leg, intent.take_profit_price, _duration(intent.entry.time_in_force)),
                _stop(exit_leg, intent.stop_loss, _duration(intent.entry.time_in_force)),
            ],
        }]
        return entry
    if isinstance(intent, OcoOrderIntent):
        if intent.leg.instruction not in (
                OrderInstruction.SELL, OrderInstruction.BUY_TO_COVER,
                OrderInstruction.SELL_TO_CLOSE, OrderInstruction.BUY_TO_CLOSE):
            raise ValueError("Schwab OCO legs must close an existing position")
        duration = _duration(intent.time_in_force)
        return {"orderStrategyType": "OCO", "childOrderStrategies": [
            _target(intent.leg, intent.take_profit_price, duration),
            _stop(intent.leg, intent.stop_loss, duration),
        ]}
    if isinstance(intent, TrailingOrderIntent):
        body = {"orderStrategyType": "SINGLE", "orderType": "TRAILING_STOP",
                "session": "NORMAL", "duration": _duration(intent.time_in_force),
                "stopPriceLinkBasis": "MARK",
                "stopPriceLinkType": "VALUE" if intent.trail_price is not None else "PERCENT",
                "stopPriceOffset": float(intent.trail_price if intent.trail_price is not None
                                         else intent.trail_percent),
                "orderLegCollection": [_leg(intent.leg)]}
        if body["stopPriceOffset"] <= 0:
            raise ValueError("Schwab trail offset must be positive")
        return body
    if isinstance(intent, MultiLegOrderIntent):
        if len(intent.legs) != 2:
            raise ValueError("Schwab canonical multi-leg orders support verticals only")
        if intent.order_type != OrderType.LIMIT or intent.limit_price is None:
            raise ValueError("Schwab vertical spreads require a net limit price")
        if intent.limit_price == 0:
            raise ValueError("Schwab vertical spread net price must be nonzero")
        units = {leg.quantity / leg.ratio_quantity for leg in intent.legs}
        if len(units) != 1:
            raise ValueError("Schwab spread legs must share a group quantity")
        qty = _qty(units.pop())
        return {"orderStrategyType": "SINGLE",
                "orderType": "NET_DEBIT" if intent.limit_price > 0 else "NET_CREDIT",
                "complexOrderStrategyType": "VERTICAL", "session": "NORMAL",
                "duration": _duration(intent.time_in_force),
                "price": _price(abs(intent.limit_price)), "quantity": qty,
                "orderLegCollection": [_leg(leg) for leg in intent.legs]}
    raise TypeError(f"unknown Schwab order intent: {type(intent).__name__}")
