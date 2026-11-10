"""Guarded Alpaca Paper equity entry plans. This module never sends an order."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .models import (AssetType, BracketOrderIntent, OrderInstruction,
                     OrderLegIntent, OrderType, SimpleOrderIntent,
                     StopLossIntent, TimeInForce)

SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}\Z")


def _positive(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a positive number")
    return number


def entry_plan(body: dict, quote, now: datetime | None = None) -> BracketOrderIntent:
    """Accept only a flat equity bracket with one broker-held target and stop."""
    if not isinstance(body, dict):
        raise ValueError("entry request must be an object")
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("use an uppercase equity symbol of at most 10 characters")
    if quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or not quote.realtime:
        raise ValueError("a current Alpaca Paper IEX quote is required")
    now = now or datetime.now(timezone.utc)
    if quote.observed_at.tzinfo is None or not 0 <= (now - quote.observed_at).total_seconds() <= 30:
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
    return BracketOrderIntent(entry, target, StopLossIntent(stop))
