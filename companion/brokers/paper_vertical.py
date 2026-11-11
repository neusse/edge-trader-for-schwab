"""Defined-risk, same-expiration Alpaca Paper vertical spread plans."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .models import (AccountSnapshot, AssetType, MultiLegOrderIntent, OptionChain,
                     OptionType, OrderInstruction, OrderLegIntent, OrderType,
                     PositionSide, Quote, TimeInForce)
from .paper_options import ET, OCC, _occ_symbol, chain_query


def _known_option_quote(quote: Quote) -> bool:
    return ((quote.source == "Alpaca indicative options" and not quote.realtime) or
            (quote.source == "Alpaca OPRA options" and quote.realtime and
             quote.timestamp_verified))


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
                not _known_option_quote(quote) or
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


def vertical_close_plan(body: dict, account: AccountSnapshot,
                        quotes: dict[str, Quote], now: datetime | None = None,
                        *, require_quotes: bool = True) -> MultiLegOrderIntent:
    """Close or target one exact held 1:1 vertical in one broker multi-leg order."""
    if not isinstance(body, dict) or account.status.upper() != "ACTIVE":
        raise ValueError("an active Paper account and vertical close request are required")
    long_symbol, short_symbol = body.get("long_contract"), body.get("short_contract")
    if (not isinstance(long_symbol, str) or not isinstance(short_symbol, str) or
            long_symbol == short_symbol):
        raise ValueError("choose two distinct held vertical contracts")
    long_match, short_match = OCC.fullmatch(long_symbol), OCC.fullmatch(short_symbol)
    if not long_match or not short_match or long_match.group(1, 2, 3, 4, 5) != short_match.group(1, 2, 3, 4, 5):
        raise ValueError("vertical close needs one underlying, expiry, and option type")
    root, year, month, day, cp, _ = long_match.groups()
    try:
        expiration = date(2000 + int(year), int(month), int(day))
    except ValueError:
        raise ValueError("held vertical OCC expiration is invalid") from None
    now = now or datetime.now(timezone.utc)
    if expiration < now.astimezone(ET).date():
        raise ValueError("expired vertical cannot be closed from this form")
    positions = {position.symbol.replace(" ", ""): position for position in account.positions
                 if position.asset_type == AssetType.OPTION and
                 (match := OCC.fullmatch(position.symbol)) and match.group(1) == root}
    if set(positions) != {long_symbol, short_symbol}:
        raise ValueError("vertical close requires exactly two held option positions on this underlying")
    long, short = positions[long_symbol], positions[short_symbol]
    if (long.side != PositionSide.LONG or short.side != PositionSide.SHORT or
            long.quantity != short.quantity or long.quantity <= 0 or
            long.quantity != long.quantity.to_integral_value() or long.quantity > 10000):
        raise ValueError("held vertical legs must be equal whole long and short quantities")
    width = abs(Decimal(long_match.group(6)) - Decimal(short_match.group(6))) / 1000
    if width <= 0:
        raise ValueError("held vertical strikes must differ")
    if require_quotes:
        for symbol in (long_symbol, short_symbol):
            quote = quotes.get(symbol)
            if (quote is None or quote.symbol.replace(" ", "") != symbol or
                    not _known_option_quote(quote) or
                    quote.observed_at.tzinfo is None or
                    not 0 <= (now - quote.observed_at).total_seconds() <= 60 or
                    quote.bid is None or quote.ask is None or
                    quote.bid <= 0 or quote.ask <= 0):
                raise ValueError("both held vertical legs require current indicative bid and ask")
    try:
        net = Decimal(str(body.get("net_price")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("vertical close net limit must be a number") from None
    debit_entry = ((cp == "C" and Decimal(long_match.group(6)) < Decimal(short_match.group(6))) or
                   (cp == "P" and Decimal(long_match.group(6)) > Decimal(short_match.group(6))))
    if (not net.is_finite() or net == 0 or abs(net) >= width or
            net != net.quantize(Decimal("0.01")) or
            (debit_entry and net > 0) or (not debit_entry and net < 0)):
        raise ValueError("vertical close limit must be a cent-precision credit or debit inside strike width")
    role = body.get("role", "close")
    if role not in ("close", "target"):
        raise ValueError("vertical exit role must be close or target")
    if role == "target":
        if long.average_price is None or short.average_price is None:
            raise ValueError("held vertical entry basis is unavailable for a profit target")
        entry_net = long.average_price - short.average_price
        if (debit_entry and entry_net <= 0) or (not debit_entry and entry_net >= 0):
            raise ValueError("held vertical entry basis does not match its strike direction")
        if net >= -entry_net:
            raise ValueError("vertical target must lock in a profit before fees")
    try:
        request_id = UUID(str(body.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    quantity = long.quantity
    return MultiLegOrderIntent(
        (OrderLegIntent(long_symbol, AssetType.OPTION, OrderInstruction.SELL_TO_CLOSE,
                        quantity),
         OrderLegIntent(short_symbol, AssetType.OPTION, OrderInstruction.BUY_TO_CLOSE,
                        quantity)),
        OrderType.LIMIT, TimeInForce.GTC if role == "target" else TimeInForce.DAY,
        f"et-vertical-{role}-{request_id.hex}", net)
