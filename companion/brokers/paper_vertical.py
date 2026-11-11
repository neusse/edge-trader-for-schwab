"""Defined-risk, same-expiration Alpaca Paper vertical spread plans."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .models import (AccountSnapshot, AssetType, MultiLegOrderIntent, OptionChain,
                     OptionType, OrderInstruction, OrderLegIntent, OrderType,
                     TimeInForce)
from .paper_options import ET, _occ_symbol, chain_query


def vertical_entry_plan(body: dict, account: AccountSnapshot, chain: OptionChain,
                        now: datetime | None = None) -> MultiLegOrderIntent:
    """Build one atomic 1:1 debit or credit vertical; never separate legs."""
    query = chain_query(body, now)
    now = now or datetime.now(timezone.utc)
    if query.expiration <= now.astimezone(ET).date():
        raise ValueError("same-day expiring vertical entry is unavailable")
    if (account.status.upper() != "ACTIVE" or account.options_trading_level is None or
            account.options_approved_level is None or
            min(account.options_trading_level, account.options_approved_level) < 3):
        raise ValueError("Alpaca Paper account needs active level 3 options access")
    if chain.underlying != query.underlying:
        raise ValueError("vertical chain underlying changed")
    symbols = (body.get("long_contract"), body.get("short_contract"))
    if not all(isinstance(symbol, str) and symbol for symbol in symbols) or symbols[0] == symbols[1]:
        raise ValueError("choose two distinct OCC option contracts")
    contracts = []
    for symbol in symbols:
        matches = [contract for contract in chain.contracts
                   if contract.symbol.replace(" ", "") == symbol]
        if len(matches) != 1:
            raise ValueError("vertical legs must come from the current option chain")
        contract = matches[0]
        if (contract.underlying != query.underlying or contract.expiration != query.expiration or
                contract.option_type != query.option_type or not contract.tradable or
                contract.multiplier != 100 or
                _occ_symbol(contract.symbol, query, contract.strike) != symbol):
            raise ValueError("vertical requires two tradable standard contracts of one expiry and type")
        quote = contract.quote
        if (quote is None or quote.symbol.replace(" ", "") != symbol or
                quote.source != "Alpaca indicative options" or quote.realtime or
                quote.observed_at.tzinfo is None or
                not 0 <= (now - quote.observed_at).total_seconds() <= 60 or
                quote.bid is None or quote.ask is None or
                quote.bid <= 0 or quote.ask <= 0):
            raise ValueError("both vertical legs require current Alpaca indicative bid and ask")
        contracts.append(contract)
    if any(position.symbol.replace(" ", "") in symbols for position in account.positions):
        raise ValueError("a vertical leg is already held; manage that position first")
    long, short = contracts
    width = abs(long.strike - short.strike)
    if width <= 0:
        raise ValueError("vertical legs must have different strikes")
    debit = ((query.option_type == OptionType.CALL and long.strike < short.strike) or
             (query.option_type == OptionType.PUT and long.strike > short.strike))
    try:
        quantity = Decimal(str(body.get("quantity")))
        net = Decimal(str(body.get("net_price")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("vertical contracts and net price must be numbers") from None
    if (not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value() or
            quantity > 100):
        raise ValueError("vertical entry requires 1 to 100 whole spread units")
    if (not net.is_finite() or net == 0 or abs(net) >= width or
            net != net.quantize(Decimal("0.01")) or
            (debit and net < 0) or (not debit and net > 0)):
        raise ValueError("vertical net limit must be a cent-precision debit or credit inside strike width")
    max_loss = (net if debit else width + net) * quantity * 100
    available = account.balances.options_buying_power
    if available is None or available < max_loss:
        raise ValueError("vertical maximum loss exceeds available Paper options buying power")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    return MultiLegOrderIntent(
        (OrderLegIntent(symbols[0], AssetType.OPTION, OrderInstruction.BUY_TO_OPEN,
                        quantity),
         OrderLegIntent(symbols[1], AssetType.OPTION, OrderInstruction.SELL_TO_OPEN,
                        quantity)),
        OrderType.LIMIT, TimeInForce.DAY, f"et-vertical-{request_id.hex}", net)
