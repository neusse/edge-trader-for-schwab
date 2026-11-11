"""Guarded Alpaca Paper equity entry plans. This module never sends an order."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .models import (AssetType, BracketOrderIntent, OcoOrderIntent, OrderClass, OrderGroup,
                     OrderInstruction, OrderLegIntent, OrderType, Position,
                     PositionSide, OrderRole, OrderStatus, ReplaceOrder,
                     SimpleOrderIntent, StopLossIntent, TimeInForce, TrailingOrderIntent)

SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}\Z")


def _positive(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a positive number")
    return number


def entry_plan(body: dict, quote, now: datetime | None = None) -> BracketOrderIntent | SimpleOrderIntent:
    """Plan a flat equity entry, with explicit broker-held bracket or no protection."""
    if not isinstance(body, dict):
        raise ValueError("entry request must be an object")
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("use an uppercase equity symbol of at most 10 characters")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before ordering")
    reference = quote.mark or quote.last
    if reference is None or reference <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    side = body.get("side")
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    quantity = _positive(body.get("quantity"), "quantity")
    if quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("quantity must be 1 to 10000 whole shares")
    kind = body.get("order_type")
    if kind not in ("market", "limit"):
        raise ValueError("entry type must be market or limit")
    limit = _positive(body.get("limit_price"), "limit price") if kind == "limit" else None
    if kind == "market" and body.get("limit_price") not in (None, ""):
        raise ValueError("market entry cannot include a limit price")
    protection = body.get("protection", "bracket")
    if protection not in ("bracket", "none"):
        raise ValueError("protection must be bracket or none")
    if protection == "none":
        if body.get("target_price") not in (None, "") or body.get("stop_price") not in (None, ""):
            raise ValueError("unprotected entry cannot include target or stop prices")
        target = stop = None
    else:
        target = _positive(body.get("target_price"), "target price")
        stop = _positive(body.get("stop_price"), "stop price")
        basis = limit if limit is not None else reference
        if side == "long" and not stop < basis < target:
            raise ValueError("long bracket requires stop < entry < target")
        if side == "short" and not target < basis < stop:
            raise ValueError("short bracket requires target < entry < stop")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = OrderInstruction.BUY if side == "long" else OrderInstruction.SELL_SHORT
    entry = SimpleOrderIntent(
        OrderLegIntent(symbol, AssetType.EQUITY, instruction, quantity),
        OrderType(kind), TimeInForce.DAY, f"et-{request_id.hex}", limit_price=limit)
    return BracketOrderIntent(entry, target, StopLossIntent(stop)) if protection == "bracket" else entry


def oco_plan(body: dict, position: Position, quote,
             now: datetime | None = None) -> OcoOrderIntent:
    """Pair one whole equity position with a broker-held target and fixed stop."""
    if not isinstance(body, dict) or position.asset_type != AssetType.EQUITY:
        raise ValueError("OCO exits require an equity position")
    symbol = body.get("symbol")
    if symbol != position.symbol or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("OCO symbol must match the held equity position")
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("OCO exits require 1 to 10000 whole shares")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before ordering")
    mark = quote.mark or quote.last
    if mark is None or mark <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    target = _positive(body.get("target_price"), "target price")
    stop = _positive(body.get("stop_price"), "stop price")
    if position.side == PositionSide.LONG and not stop < mark < target:
        raise ValueError("long OCO requires stop < current mark < target")
    if position.side == PositionSide.SHORT and not target < mark < stop:
        raise ValueError("short OCO requires target < current mark < stop")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.SELL if position.side == PositionSide.LONG else
                   OrderInstruction.BUY_TO_COVER)
    return OcoOrderIntent(OrderLegIntent(symbol, AssetType.EQUITY, instruction, quantity),
                          target, StopLossIntent(stop), TimeInForce.GTC,
                          f"et-oco-{request_id.hex}")


def close_plan(body: dict, position: Position, quote,
               now: datetime | None = None) -> SimpleOrderIntent:
    """Close all or part of an order-free position with one reviewed market order."""
    if not isinstance(body, dict) or position.asset_type != AssetType.EQUITY:
        raise ValueError("Paper close requires an equity position")
    symbol = body.get("symbol")
    if symbol != position.symbol or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("close symbol must match the held equity position")
    held = position.quantity
    if held <= 0 or held != held.to_integral_value() or held > 10000:
        raise ValueError("Paper close requires 1 to 10000 whole shares")
    quantity = _positive(body["shares"], "shares") if body.get("shares") not in (None, "") else held
    if quantity != quantity.to_integral_value() or quantity > held:
        raise ValueError("shares must be whole and no greater than the held quantity")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before closing")
    if not (quote.mark or quote.last) or (quote.mark or quote.last) <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.SELL if position.side == PositionSide.LONG else
                   OrderInstruction.BUY_TO_COVER)
    return SimpleOrderIntent(OrderLegIntent(symbol, AssetType.EQUITY, instruction, quantity),
                             OrderType.MARKET, TimeInForce.DAY,
                             f"et-close-{request_id.hex}")


def add_plan(body: dict, position: Position, quote,
             now: datetime | None = None) -> SimpleOrderIntent:
    """Add whole shares to an order-free Paper equity position without claiming protection."""
    if not isinstance(body, dict) or position.asset_type != AssetType.EQUITY:
        raise ValueError("Paper add requires an equity position")
    symbol = body.get("symbol")
    if symbol != position.symbol or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("add symbol must match the held equity position")
    if position.quantity <= 0 or position.quantity != position.quantity.to_integral_value():
        raise ValueError("Paper add requires a whole-share held position")
    quantity = _positive(body.get("shares"), "shares")
    if quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("add shares must be 1 to 10000 whole shares")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before adding shares")
    if not (quote.mark or quote.last) or (quote.mark or quote.last) <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.BUY if position.side == PositionSide.LONG else
                   OrderInstruction.SELL_SHORT)
    return SimpleOrderIntent(OrderLegIntent(symbol, AssetType.EQUITY, instruction, quantity),
                             OrderType.MARKET, TimeInForce.DAY,
                             f"et-add-{request_id.hex}")


def trailing_plan(body: dict, position: Position, quote,
                  now: datetime | None = None) -> TrailingOrderIntent:
    """One broker-held trailing exit for an order-free whole-share Paper position."""
    if not isinstance(body, dict) or position.asset_type != AssetType.EQUITY:
        raise ValueError("Paper trailing stop requires an equity position")
    symbol = body.get("symbol")
    if symbol != position.symbol or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("trailing stop symbol must match the held position")
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("Paper trailing stop requires 1 to 10000 whole shares")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before attaching a trail")
    mark = quote.mark or quote.last
    if mark is None or mark <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    unit = body.get("unit")
    if unit not in ("dollar", "percent"):
        raise ValueError("trail unit must be dollar or percent")
    distance = _positive(body.get("distance"), "trail distance")
    if (unit == "dollar" and position.side == PositionSide.LONG and distance >= mark) or (
            unit == "percent" and distance >= 100):
        raise ValueError("trail distance would not leave a valid stop from the current mark")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.SELL if position.side == PositionSide.LONG else
                   OrderInstruction.BUY_TO_COVER)
    return TrailingOrderIntent(
        OrderLegIntent(symbol, AssetType.EQUITY, instruction, quantity),
        TimeInForce.GTC, f"et-trail-{request_id.hex}",
        trail_price=distance if unit == "dollar" else None,
        trail_percent=distance if unit == "percent" else None,
    )


def verified_oco_pair(position: Position, group: OrderGroup):
    """Return the exact, fully working target and fixed stop for one held equity position."""
    if position.asset_type != AssetType.EQUITY or group.order_class != OrderClass.OCO:
        raise ValueError("Only an equity OCO exit can be managed here")
    symbol = position.symbol
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("OCO symbol must match the held position")
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("OCO adjustment requires 1 to 10000 whole shares")
    if len(group.orders) != 2:
        raise ValueError("OCO must have exactly one target and one stop")
    stop = next((order for order in group.orders if order.role == OrderRole.STOP), None)
    target = next((order for order in group.orders if order.role == OrderRole.TARGET), None)
    if stop is None or target is None:
        raise ValueError("OCO exit no longer has exactly one target and one stop")
    exit_instructions = ((OrderInstruction.SELL,) if position.side == PositionSide.LONG else
                         (OrderInstruction.BUY, OrderInstruction.BUY_TO_COVER))
    for order in (stop, target):
        if (order.status != OrderStatus.WORKING or len(order.legs) != 1 or
                order.legs[0].symbol != symbol or order.legs[0].asset_type != AssetType.EQUITY or
                order.legs[0].instruction not in exit_instructions or
                order.legs[0].filled_quantity != 0 or
                (order.legs[0].remaining_quantity if order.legs[0].remaining_quantity is not None
                 else order.legs[0].quantity) != quantity):
            raise ValueError("OCO exit is not fully working for the current position")
    if stop.stop_price is None or target.limit_price is None:
        raise ValueError("OCO target or stop price is unavailable")
    return stop, target


def verified_bracket_pair(position: Position, group: OrderGroup):
    """Return the two working exits of an exact, app-created, filled equity bracket."""
    if (position.asset_type != AssetType.EQUITY or
            group.order_class != OrderClass.BRACKET or len(group.orders) != 3):
        raise ValueError("A filled equity bracket with two working exits is required")
    if not SYMBOL.fullmatch(position.symbol):
        raise ValueError("Bracket symbol must match the held position")
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("Bracket adjustment requires 1 to 10000 whole shares")
    entries = [order for order in group.orders if order.role == OrderRole.ENTRY]
    stops = [order for order in group.orders if order.role == OrderRole.STOP]
    targets = [order for order in group.orders if order.role == OrderRole.TARGET]
    if len(entries) != 1 or len(stops) != 1 or len(targets) != 1:
        raise ValueError("Bracket must have exactly one entry, stop, and target")
    entry, stop, target = entries[0], stops[0], targets[0]
    entry_instruction = ((OrderInstruction.BUY,) if position.side == PositionSide.LONG
                         else (OrderInstruction.SELL, OrderInstruction.SELL_SHORT))
    exit_instructions = ((OrderInstruction.SELL,) if position.side == PositionSide.LONG else
                         (OrderInstruction.BUY, OrderInstruction.BUY_TO_COVER))
    if (entry.status != OrderStatus.FILLED or len(entry.legs) != 1 or
            entry.legs[0].symbol != position.symbol or
            entry.legs[0].asset_type != AssetType.EQUITY or
            entry.legs[0].quantity != quantity or
            entry.legs[0].filled_quantity != quantity or
            entry.legs[0].instruction not in entry_instruction or
            not entry.client_order_id or not entry.client_order_id.startswith("et-") or
            stop.time_in_force not in (TimeInForce.DAY, TimeInForce.GTC) or
            target.time_in_force != stop.time_in_force or
            any(order.status != OrderStatus.WORKING or len(order.legs) != 1 or
                order.legs[0].symbol != position.symbol or
                order.legs[0].asset_type != AssetType.EQUITY or
                order.legs[0].quantity != quantity or
                order.legs[0].filled_quantity != 0 or
                order.legs[0].instruction not in exit_instructions
                for order in (stop, target)) or
            stop.stop_price is None or target.limit_price is None):
        raise ValueError("Bracket entry or exits changed from the exact held position")
    return stop, target, entry.client_order_id


def equity_exit_reprice(body: dict, position: Position, group: OrderGroup, quote,
                        now: datetime | None = None) -> ReplaceOrder:
    """Review a broker-held OCO or filled bracket exit leg replacement."""
    if not isinstance(body, dict) or body.get("symbol") != position.symbol:
        raise ValueError("Exit symbol must match the held position")
    if group.order_class == OrderClass.OCO:
        stop, target = verified_oco_pair(position, group)
    else:
        stop, target, _ = verified_bracket_pair(position, group)
    try:
        request_id = UUID(str(body.get("request_id")))
        order_id = str(UUID(str(body.get("order_id"))))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("order_id and request_id must be UUIDs") from None
    role = body.get("role")
    if role not in ("stop", "target"):
        raise ValueError("role must be stop or target")
    selected = next((order for order in group.orders if order.order_id == order_id), None)
    if (selected is None or selected.role !=
            (OrderRole.STOP if role == "stop" else OrderRole.TARGET)):
        raise ValueError("Equity exit order no longer matches the reviewed leg")
    symbol = position.symbol
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not -5 <= (now - quote.observed_at).total_seconds() <= 30:
        raise ValueError("Alpaca Paper quote is stale; refresh before adjusting the exit")
    mark = quote.mark or quote.last
    if mark is None or mark <= 0:
        raise ValueError("Alpaca Paper quote has no usable price")
    price = _positive(body.get("price"), "price")
    if role == "stop":
        if position.side == PositionSide.LONG and not stop.stop_price < price < mark:
            raise ValueError("long stop can only tighten below the current mark")
        if position.side == PositionSide.SHORT and not mark < price < stop.stop_price:
            raise ValueError("short stop can only tighten above the current mark")
        return ReplaceOrder(order_id, stop_price=price,
                            client_order_id=f"et-exit-{request_id.hex}")
    if position.side == PositionSide.LONG and not mark < price:
        raise ValueError("long target must remain above the current mark")
    if position.side == PositionSide.SHORT and not price < mark:
        raise ValueError("short target must remain below the current mark")
    return ReplaceOrder(order_id, limit_price=price,
                        client_order_id=f"et-exit-{request_id.hex}")
