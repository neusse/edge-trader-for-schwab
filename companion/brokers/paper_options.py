"""Read-only Alpaca Paper option discovery, with explicit OCC validation."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import OptionChain, OptionChainQuery, OptionExpirationsQuery, OptionType

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
