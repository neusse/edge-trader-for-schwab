"""Read-only Alpaca Paper option discovery, with explicit OCC validation."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import (AccountSnapshot, AssetType, OptionChain, OptionChainQuery,
                     OptionExpirationsQuery, OptionType, OrderInstruction,
                     OrderLegIntent, OrderType, SimpleOrderIntent, TimeInForce)

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
