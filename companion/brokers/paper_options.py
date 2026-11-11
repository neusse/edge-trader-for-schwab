"""Read-only Alpaca Paper option discovery, with explicit OCC validation."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import (AccountSnapshot, AssetType, OptionChain, OptionChainQuery,
                     OptionExpirationsQuery, OptionType, OrderInstruction,
                     OrderLegIntent, OrderType, Position, PositionSide,
                     Quote, SimpleOrderIntent, TimeInForce)

UNDERLYING = re.compile(r"[A-Z][A-Z0-9.]{0,5}\Z")
OCC = re.compile(r"([A-Z0-9.]{1,6}) {0,5}(\d{2})(\d{2})(\d{2})([CP])(\d{8})\Z")
ET = ZoneInfo("America/New_York")


def expirations_query(body: dict, now: datetime | None = None) -> OptionExpirationsQuery:
    if not isinstance(body, dict):
        raise ValueError("option expiration request must be an object")
    underlying = body.get("underlying")
    if not isinstance(underlying, str) or not UNDERLYING.fullmatch(underlying):
        raise ValueError("use an uppercase option underlying of at most 6 characters")
    today = (now or datetime.now(ET)).astimezone(ET).date()
    return OptionExpirationsQuery(underlying, today, today + timedelta(days=120))


def expirations_response(query: OptionExpirationsQuery, dates: tuple[date, ...]) -> dict:
    if len(dates) > 120 or any(not query.start <= day <= query.end for day in dates):
        raise ValueError("Alpaca option expirations exceeded the requested range")
    return {"ok": True, "underlying": query.underlying,
            "expirations": [day.isoformat() for day in sorted(set(dates))]}


def chain_query(body: dict, now: datetime | None = None) -> OptionChainQuery:
    if not isinstance(body, dict):
        raise ValueError("option discovery request must be an object")
    underlying = body.get("underlying")
    if not isinstance(underlying, str) or not UNDERLYING.fullmatch(underlying):
        raise ValueError("use an uppercase option underlying of at most 6 characters")
    try:
        expiration = date.fromisoformat(body["expiration"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("expiration must be an ISO date") from None
    today = (now or datetime.now(ET)).astimezone(ET).date()
    if not today <= expiration <= today + timedelta(days=120):
        raise ValueError("expiration must be within the next 120 days")
    try:
        option_type = OptionType(body["option_type"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("option_type must be call or put") from None
    return OptionChainQuery(underlying, expiration, option_type)


def _occ_symbol(symbol: str, query: OptionChainQuery, strike: Decimal) -> str:
    match = OCC.fullmatch(symbol)
    if match is None:
        raise ValueError("Alpaca returned an invalid OCC option symbol")
    root, year, month, day, cp, encoded_strike = match.groups()
    if (root != query.underlying or
            date(2000 + int(year), int(month), int(day)) != query.expiration or
            cp != ("C" if query.option_type == OptionType.CALL else "P") or
            Decimal(encoded_strike) / 1000 != strike):
        raise ValueError("Alpaca option contract does not match the requested OCC terms")
    return f"{root}{year}{month}{day}{cp}{encoded_strike}"


def chain_response(query: OptionChainQuery, chain: OptionChain) -> dict:
    if chain.underlying != query.underlying or len(chain.contracts) > 500:
        raise ValueError("Alpaca option chain did not match the requested underlying or size limit")
    rows = []
    for contract in chain.contracts:
        if (contract.underlying != query.underlying or contract.expiration != query.expiration or
                contract.option_type != query.option_type):
            raise ValueError("Alpaca option chain contained a contract outside the requested terms")
        symbol = _occ_symbol(contract.symbol, query, contract.strike)
        quote = contract.quote
        if quote and (quote.source != "Alpaca indicative options" or quote.realtime):
            raise ValueError("Alpaca option quote source was unexpected")
        greeks = quote.greeks if quote else None
        rows.append({
            "symbol": symbol, "strike": str(contract.strike),
            "tradable": contract.tradable, "multiplier": str(contract.multiplier),
            "bid": str(quote.bid) if quote and quote.bid is not None else None,
            "ask": str(quote.ask) if quote and quote.ask is not None else None,
            "mark": str(quote.mark) if quote and quote.mark is not None else None,
            "quote_at": quote.observed_at.isoformat() if quote else None,
            "delta": str(greeks.delta) if greeks and greeks.delta is not None else None,
            "gamma": str(greeks.gamma) if greeks and greeks.gamma is not None else None,
            "theta": str(greeks.theta) if greeks and greeks.theta is not None else None,
            "vega": str(greeks.vega) if greeks and greeks.vega is not None else None,
            "rho": str(greeks.rho) if greeks and greeks.rho is not None else None,
            "implied_volatility": str(contract.implied_volatility)
            if contract.implied_volatility is not None else None,
            "open_interest": str(contract.open_interest)
            if contract.open_interest is not None else None,
        })
    rows.sort(key=lambda row: (Decimal(row["strike"]), row["symbol"]))
    return {"ok": True, "underlying": query.underlying,
            "expiration": query.expiration.isoformat(), "option_type": query.option_type.value,
            "source": "Alpaca indicative options", "realtime": False,
            "observed_at": chain.observed_at.isoformat(), "contracts": rows}


def long_entry_plan(body: dict, account: AccountSnapshot, chain: OptionChain,
                    now: datetime | None = None) -> SimpleOrderIntent:
    """One reviewed, unprotected, long call/put limit entry for Paper only."""
    query = chain_query(body, now)
    today = (now or datetime.now(ET)).astimezone(ET).date()
    if query.expiration <= today:
        raise ValueError("opening an option on its expiration day is unavailable")
    if (account.status.upper() != "ACTIVE" or account.options_trading_level is None or
            account.options_approved_level is None or
            min(account.options_trading_level, account.options_approved_level) < 2):
        raise ValueError("Alpaca Paper account needs active level 2 options access")
    if chain.underlying != query.underlying:
        raise ValueError("option chain underlying changed")
    symbol = body.get("contract")
    candidates = [contract for contract in chain.contracts if contract.symbol.replace(" ", "") == symbol]
    if len(candidates) != 1:
        raise ValueError("choose exactly one contract from the current option chain")
    contract = candidates[0]
    if (contract.expiration != query.expiration or contract.option_type != query.option_type or
            contract.underlying != query.underlying or not contract.tradable or
            contract.multiplier != 100 or _occ_symbol(contract.symbol, query, contract.strike) != symbol):
        raise ValueError("selected option contract is not a tradable standard contract")
    if any(position.symbol.replace(" ", "") == symbol for position in account.positions):
        raise ValueError("an option position already exists; manage it before adding")
    try:
        quantity = Decimal(str(body.get("quantity")))
        limit = Decimal(str(body.get("limit_price")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("contracts and limit price must be positive numbers") from None
    if (not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value() or
            quantity > 100):
        raise ValueError("option entry requires 1 to 100 whole contracts")
    if (not limit.is_finite() or limit <= 0 or
            limit != limit.quantize(Decimal("0.01"))):
        raise ValueError("option limit price must be positive with cent precision")
    premium = quantity * limit * contract.multiplier
    if (account.balances.buying_power is None or
            premium > account.balances.buying_power):
        raise ValueError("maximum premium exceeds available Paper buying power")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    return SimpleOrderIntent(
        OrderLegIntent(symbol, AssetType.OPTION, OrderInstruction.BUY_TO_OPEN, quantity),
        OrderType.LIMIT, TimeInForce.DAY, f"et-opt-{request_id.hex}", limit_price=limit)


def option_close_plan(body: dict, position: Position,
                      now: datetime | None = None) -> SimpleOrderIntent:
    """Limit close of up to the held whole-contract option quantity, without an exit chain."""
    if not isinstance(body, dict) or position.asset_type != AssetType.OPTION:
        raise ValueError("Paper option close requires a held option position")
    symbol = body.get("contract")
    match = OCC.fullmatch(position.symbol)
    if match is None or symbol != position.symbol.replace(" ", ""):
        raise ValueError("option close contract must match the held OCC position")
    _, year, month, day, _, _ = match.groups()
    try:
        expiration = date(2000 + int(year), int(month), int(day))
    except ValueError:
        raise ValueError("held option OCC expiration is invalid") from None
    today = (now or datetime.now(ET)).astimezone(ET).date()
    if expiration < today:
        raise ValueError("expired option position cannot be closed from this form")
    held = position.quantity
    if held <= 0 or held != held.to_integral_value() or held > 10000:
        raise ValueError("held option quantity must be 1 to 10000 whole contracts")
    try:
        quantity = Decimal(str(body.get("quantity")))
        limit = Decimal(str(body.get("limit_price")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("contracts and limit price must be positive numbers") from None
    if (not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value() or
            quantity > held):
        raise ValueError("close quantity must be whole and no greater than the held contracts")
    if not limit.is_finite() or limit <= 0 or limit != limit.quantize(Decimal("0.01")):
        raise ValueError("option close limit must be positive with cent precision")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.SELL_TO_CLOSE if position.side == PositionSide.LONG else
                   OrderInstruction.BUY_TO_CLOSE)
    return SimpleOrderIntent(
        OrderLegIntent(symbol, AssetType.OPTION, instruction, quantity),
        OrderType.LIMIT, TimeInForce.DAY, f"et-opt-close-{request_id.hex}",
        limit_price=limit)


def option_fixed_exit_plan(body: dict, position: Position, quote: Quote | None,
                           now: datetime | None = None) -> SimpleOrderIntent:
    """One full-position Paper option stop OR target; never a synthetic OCO."""
    if not isinstance(body, dict) or position.asset_type != AssetType.OPTION:
        raise ValueError("Paper option exit requires a held option position")
    symbol = body.get("contract")
    match = OCC.fullmatch(position.symbol)
    if symbol != position.symbol.replace(" ", "") or match is None:
        raise ValueError("option exit contract must match the held OCC position")
    _, year, month, day, _, _ = match.groups()
    try:
        expiration = date(2000 + int(year), int(month), int(day))
    except ValueError:
        raise ValueError("held option OCC expiration is invalid") from None
    quantity = position.quantity
    if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
        raise ValueError("option exit requires 1 to 10000 whole held contracts")
    if (quote is None or quote.symbol.replace(" ", "") != symbol or
            quote.source != "Alpaca indicative options" or quote.realtime or
            quote.observed_at.tzinfo is None):
        raise ValueError("a current Alpaca indicative option quote is required")
    now = now or datetime.now(timezone.utc)
    if expiration < now.astimezone(ET).date():
        raise ValueError("expired option position cannot receive a new exit")
    if not 0 <= (now - quote.observed_at).total_seconds() <= 60:
        raise ValueError("Alpaca option quote is stale; refresh before attaching an exit")
    mark = quote.mark
    if mark is None or mark <= 0:
        raise ValueError("Alpaca option quote has no usable bid/ask mark")
    role = body.get("role")
    if role not in ("stop", "stop-limit", "target"):
        raise ValueError("option exit role must be stop, stop-limit, or target")
    try:
        price = Decimal(str(body.get("price")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("option exit price must be positive") from None
    if not price.is_finite() or price <= 0 or price != price.quantize(Decimal("0.01")):
        raise ValueError("option exit price must be positive with cent precision")
    if role in ("stop", "stop-limit") and ((position.side == PositionSide.LONG and price >= mark) or
                                              (position.side == PositionSide.SHORT and price <= mark)):
        raise ValueError("fixed stop must be on the loss side of the indicative mark")
    if role == "target" and ((position.side == PositionSide.LONG and price <= mark) or
                             (position.side == PositionSide.SHORT and price >= mark)):
        raise ValueError("target must be on the profit side of the indicative mark")
    stop_limit = None
    if role == "stop-limit":
        try:
            stop_limit = Decimal(str(body.get("limit_price")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("stop-limit exit needs a positive limit price") from None
        if (not stop_limit.is_finite() or stop_limit <= 0 or
                stop_limit != stop_limit.quantize(Decimal("0.01"))):
            raise ValueError("stop-limit exit needs a positive cent-precision limit")
        if ((position.side == PositionSide.LONG and stop_limit > price) or
                (position.side == PositionSide.SHORT and stop_limit < price)):
            raise ValueError("stop-limit sell limit must be at or below its trigger; buy limit at or above")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    instruction = (OrderInstruction.SELL_TO_CLOSE if position.side == PositionSide.LONG else
                   OrderInstruction.BUY_TO_CLOSE)
    return SimpleOrderIntent(
        OrderLegIntent(symbol, AssetType.OPTION, instruction, quantity),
        OrderType.STOP_LIMIT if role == "stop-limit" else
        OrderType.STOP if role == "stop" else OrderType.LIMIT,
        TimeInForce.GTC, f"et-opt-exit-{request_id.hex}",
        stop_price=price if role in ("stop", "stop-limit") else None,
        limit_price=stop_limit if role == "stop-limit" else price if role == "target" else None)
