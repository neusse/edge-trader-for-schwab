"""Translate canonical intents into Alpaca paper order requests without sending them."""
from __future__ import annotations

from decimal import Decimal

from .models import (
    AssetType, BracketOrderIntent, MultiLegOrderIntent, OcoOrderIntent,
    OrderInstruction, OrderLegIntent, OrderType, SimpleOrderIntent,
    TrailingOrderIntent, TrailingStopSpec, TriggeredOrderIntent,
)


def _side_and_intent(leg: OrderLegIntent):
    from alpaca.trading.enums import OrderSide, PositionIntent

    sides = {
        OrderInstruction.BUY: OrderSide.BUY,
        OrderInstruction.BUY_TO_COVER: OrderSide.BUY,
        OrderInstruction.BUY_TO_OPEN: OrderSide.BUY,
        OrderInstruction.BUY_TO_CLOSE: OrderSide.BUY,
        OrderInstruction.SELL: OrderSide.SELL,
        OrderInstruction.SELL_SHORT: OrderSide.SELL,
        OrderInstruction.SELL_TO_OPEN: OrderSide.SELL,
        OrderInstruction.SELL_TO_CLOSE: OrderSide.SELL,
    }
    intents = {
        OrderInstruction.BUY_TO_OPEN: PositionIntent.BUY_TO_OPEN,
        OrderInstruction.BUY_TO_CLOSE: PositionIntent.BUY_TO_CLOSE,
        OrderInstruction.SELL_TO_OPEN: PositionIntent.SELL_TO_OPEN,
        OrderInstruction.SELL_TO_CLOSE: PositionIntent.SELL_TO_CLOSE,
    }
    if leg.asset_type == AssetType.OPTION and leg.instruction not in intents:
        raise ValueError("option orders require an explicit open or close instruction")
    if leg.asset_type not in (AssetType.EQUITY, AssetType.OPTION):
        raise ValueError("Alpaca paper order asset must be an equity or option")
    return sides[leg.instruction], intents.get(leg.instruction)


def _quantity(value: Decimal, asset_type: AssetType) -> float:
    if value <= 0:
        raise ValueError("order quantity must be positive")
    if asset_type == AssetType.OPTION and value != value.to_integral_value():
        raise ValueError("option quantity must be a whole contract")
    return float(value)


def _stop_loss(stop):
    from alpaca.trading.requests import StopLossRequest

    if isinstance(stop, TrailingStopSpec):
        raise ValueError("Alpaca paper cannot attach a trailing stop to a bracket or OCO")
    return StopLossRequest(stop_price=float(stop.stop_price),
                           limit_price=float(stop.limit_price) if stop.limit_price is not None else None)


def _simple(intent: SimpleOrderIntent, **extra):
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest, StopOrderRequest,
    )

    side, position_intent = _side_and_intent(intent.leg)
    common = {"symbol": intent.leg.symbol, "qty": _quantity(intent.leg.quantity, intent.leg.asset_type),
              "side": side, "time_in_force": TimeInForce(intent.time_in_force.value),
              "client_order_id": intent.client_order_id, "position_intent": position_intent,
              **extra}
    if intent.order_type == OrderType.MARKET:
        return MarketOrderRequest(**common)
    if intent.order_type == OrderType.LIMIT:
        return LimitOrderRequest(limit_price=float(intent.limit_price), **common)
    if intent.order_type == OrderType.STOP:
        return StopOrderRequest(stop_price=float(intent.stop_price), **common)
    if intent.order_type == OrderType.STOP_LIMIT:
        return StopLimitOrderRequest(stop_price=float(intent.stop_price),
                                     limit_price=float(intent.limit_price), **common)
    raise ValueError(f"unsupported simple order type: {intent.order_type.value}")


def build_order_request(intent):
    """Return an SDK request; caller owns all idempotency and account safety checks."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest, MarketOrderRequest, OptionLegRequest, OrderRequest,
        TakeProfitRequest, TrailingStopOrderRequest,
    )

    if isinstance(intent, SimpleOrderIntent):
        return _simple(intent)
    if isinstance(intent, TriggeredOrderIntent):
        raise ValueError("Alpaca paper requires a supported bracket plan; this triggered exit structure is unavailable")
    if isinstance(intent, BracketOrderIntent):
        if intent.entry.leg.asset_type != AssetType.EQUITY:
            raise ValueError("Alpaca paper brackets require equity entry")
        if intent.entry.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("Alpaca paper brackets require market or limit entry")
        return _simple(intent.entry, order_class=OrderClass.BRACKET,
                       take_profit=TakeProfitRequest(limit_price=float(intent.take_profit_price)),
                       stop_loss=_stop_loss(intent.stop_loss))
    if isinstance(intent, OcoOrderIntent):
        if intent.leg.asset_type != AssetType.EQUITY:
            raise ValueError("Alpaca paper OCO requires equity exits")
        if intent.leg.instruction not in (OrderInstruction.SELL, OrderInstruction.BUY_TO_COVER):
            raise ValueError("Alpaca paper OCO must close an existing equity position")
        side, _ = _side_and_intent(intent.leg)
        return OrderRequest(
            symbol=intent.leg.symbol, qty=_quantity(intent.leg.quantity, intent.leg.asset_type),
            side=side, type=OrderType.LIMIT.value, time_in_force=TimeInForce(intent.time_in_force.value),
            order_class=OrderClass.OCO, client_order_id=intent.client_order_id,
            take_profit=TakeProfitRequest(limit_price=float(intent.take_profit_price)),
            stop_loss=_stop_loss(intent.stop_loss),
        )
    if isinstance(intent, TrailingOrderIntent):
        if intent.leg.asset_type != AssetType.EQUITY:
            raise ValueError("Alpaca paper trailing stops require equities")
        if intent.basis != "MARK":
            raise ValueError("Alpaca paper trailing stops do not support a chosen quote basis")
        side, _ = _side_and_intent(intent.leg)
        return TrailingStopOrderRequest(
            symbol=intent.leg.symbol, qty=_quantity(intent.leg.quantity, intent.leg.asset_type),
            side=side, time_in_force=TimeInForce(intent.time_in_force.value),
            client_order_id=intent.client_order_id,
            trail_price=float(intent.trail_price) if intent.trail_price is not None else None,
            trail_percent=float(intent.trail_percent) if intent.trail_percent is not None else None,
        )
    if isinstance(intent, MultiLegOrderIntent):
        units = {leg.quantity / leg.ratio_quantity for leg in intent.legs}
        if len(units) != 1:
            raise ValueError("all option legs must use the same group quantity")
        quantity = _quantity(units.pop(), AssetType.OPTION)
        legs = []
        for leg in intent.legs:
            side, position_intent = _side_and_intent(leg)
            legs.append(OptionLegRequest(symbol=leg.symbol, ratio_qty=float(leg.ratio_quantity),
                                         side=side, position_intent=position_intent))
        common = {"qty": quantity, "legs": legs, "order_class": OrderClass.MLEG,
                  "time_in_force": TimeInForce(intent.time_in_force.value),
                  "client_order_id": intent.client_order_id}
        if intent.order_type == OrderType.MARKET:
            return MarketOrderRequest(**common)
        if intent.order_type == OrderType.LIMIT:
            return LimitOrderRequest(limit_price=float(intent.limit_price), **common)
        raise ValueError("Alpaca paper multi-leg orders require market or limit")
    raise TypeError(f"unknown Alpaca order intent: {type(intent).__name__}")
