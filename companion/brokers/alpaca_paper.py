"""Alpaca paper adapter. Read operations are added as their dashboard paths migrate."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re

from .adapter import BrokerAdapter
from .capabilities import ALPACA_PAPER
from .configuration import paper_credentials, paper_trading_client
from .models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, BrokerCommand,
    BrokerOrder, BrokerQuery, BrokerQueryResult, Fill, FillsQuery, MarketSession, MarketSessionsQuery,
    MutationResult, OptionChain, OptionChainQuery, OptionContract, OptionType,
    OrderClass, OrderGroup, OrderInstruction, OrderLeg, OrderQuery,
    OrderRole, OrdersQuery, OrderStatus, OrderType, Position, PositionSide, Quote,
    QuotesQuery,
    TimeInForce,
)


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


_ORDER_STATUS = {
    "new": OrderStatus.WORKING, "accepted": OrderStatus.PENDING,
    "pending_new": OrderStatus.PENDING, "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.PENDING, "pending_replace": OrderStatus.PENDING,
    "rejected": OrderStatus.REJECTED, "expired": OrderStatus.EXPIRED,
    "replaced": OrderStatus.REPLACED,
}
_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_FILL_INSTRUCTION = {
    "buy": OrderInstruction.BUY, "sell": OrderInstruction.SELL,
    "buy_to_open": OrderInstruction.BUY_TO_OPEN,
    "sell_to_open": OrderInstruction.SELL_TO_OPEN,
    "buy_to_close": OrderInstruction.BUY_TO_CLOSE,
    "sell_to_close": OrderInstruction.SELL_TO_CLOSE,
    "sell_short": OrderInstruction.SELL_SHORT,
    "buy_to_cover": OrderInstruction.BUY_TO_COVER,
}


def _fill(raw: dict) -> Fill:
    symbol = raw["symbol"]
    return Fill(
        activity_id=str(raw["id"]), order_id=str(raw["order_id"]), symbol=symbol,
        asset_type=AssetType.OPTION if _OPTION_SYMBOL.fullmatch(symbol) else AssetType.EQUITY,
        instruction=_FILL_INSTRUCTION[raw["side"].lower()],
        quantity=Decimal(raw["qty"]), price=Decimal(raw["price"]),
        occurred_at=datetime.fromisoformat(raw["transaction_time"].replace("Z", "+00:00")),
    )


def _order_group(raw) -> OrderGroup:
    group_id = str(raw.group_id or raw.id)
    class_name = _enum_value(raw.order_class)
    group_class = OrderClass(class_name.replace("mleg", "multi-leg") or "simple")
    members = []

    def leg_from(member, role: OrderRole) -> OrderLeg | None:
        kind = _enum_value(member.asset_class) if member.asset_class else ""
        asset = AssetType.OPTION if kind == "us_option" else AssetType.EQUITY
        symbol = member.symbol
        if not symbol or member.qty is None:
            return None
        intent = _enum_value(member.position_intent) if member.position_intent else ""
        side = _enum_value(member.side) if member.side else ""
        instruction = {
            "buy_to_open": OrderInstruction.BUY_TO_OPEN,
            "sell_to_open": OrderInstruction.SELL_TO_OPEN,
            "buy_to_close": OrderInstruction.BUY_TO_CLOSE,
            "sell_to_close": OrderInstruction.SELL_TO_CLOSE,
        }.get(intent, OrderInstruction.BUY if side == "buy" else OrderInstruction.SELL)
        return OrderLeg(symbol=symbol, asset_type=asset, instruction=instruction,
                        quantity=Decimal(str(member.qty)),
                        filled_quantity=Decimal(str(member.filled_qty or 0)), role=role)

    def add(member, role: OrderRole, parent_id: str | None = None) -> None:
        if group_class == OrderClass.MULTI_LEG and member is raw:
            legs = tuple(leg for child in raw.legs or ()
                         if (leg := leg_from(child, OrderRole.ENTRY)) is not None)
        else:
            leg = leg_from(member, role)
            legs = (leg,) if leg else ()
        if legs:
            members.append(BrokerOrder(
                order_id=str(member.id), status=_ORDER_STATUS.get(_enum_value(member.status), OrderStatus.UNKNOWN),
                order_type=OrderType(_enum_value(member.type or member.order_type or "limit").replace("_", "-")),
                time_in_force=TimeInForce(_enum_value(member.time_in_force)), legs=legs, role=role,
                group_id=group_id, parent_id=parent_id, client_order_id=member.client_order_id,
                limit_price=_decimal(member.limit_price), stop_price=_decimal(member.stop_price),
                trail_price=_decimal(member.trail_price), trail_percent=_decimal(member.trail_percent),
                entered_at=member.submitted_at, updated_at=member.updated_at,
            ))
        for child in () if group_class == OrderClass.MULTI_LEG else member.legs or ():
            child_role = {"take_profit": OrderRole.TARGET, "stop_loss": OrderRole.STOP,
                          "entry": OrderRole.ENTRY}.get(_enum_value(child.group_role), OrderRole.EXIT)
            add(child, child_role, str(member.id))

    root_role = {"take_profit": OrderRole.TARGET, "stop_loss": OrderRole.STOP}.get(
        _enum_value(raw.group_role), OrderRole.ENTRY)
    add(raw, root_role)
    return OrderGroup(group_id=group_id, order_class=group_class,
                      status=_ORDER_STATUS.get(_enum_value(raw.status), OrderStatus.UNKNOWN),
                      orders=tuple(members))


class AlpacaPaperAdapter(BrokerAdapter):
    def __init__(self, client=None, stock_data=None, option_data=None):
        super().__init__(ALPACA_PAPER)
        self._client = client if client is not None else paper_trading_client()
        self._stock_data = stock_data
        self._option_data = option_data

    def _market_client(self, asset_type: AssetType):
        if asset_type == AssetType.EQUITY:
            if self._stock_data is None:
                from alpaca.data.historical.stock import StockHistoricalDataClient
                credentials = paper_credentials()
                self._stock_data = StockHistoricalDataClient(credentials.api_key, credentials.secret_key)
            return self._stock_data
        if asset_type == AssetType.OPTION:
            if self._option_data is None:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                credentials = paper_credentials()
                self._option_data = OptionHistoricalDataClient(credentials.api_key, credentials.secret_key)
            return self._option_data
        raise NotImplementedError(f"Alpaca Paper quotes for {asset_type.value} are not supported")

    def _query(self, query: BrokerQuery) -> BrokerQueryResult:
        if isinstance(query, OptionChainQuery):
            from alpaca.trading.enums import ContractType
            from alpaca.trading.requests import GetOptionContractsRequest
            filters = {"underlying_symbols": [query.underlying.upper()], "limit": 1000}
            if query.expiration:
                filters["expiration_date"] = query.expiration
            else:
                filters["expiration_date_gte"] = date.today()
                filters["expiration_date_lte"] = date.today() + timedelta(days=30)
            if query.option_type:
                filters["type"] = ContractType(query.option_type.value)
            contracts = []
            while True:
                page = self._client.get_option_contracts(GetOptionContractsRequest(**filters))
                for raw in page.option_contracts or ():
                    contracts.append(OptionContract(
                        symbol=raw.symbol, underlying=raw.underlying_symbol,
                        expiration=raw.expiration_date, strike=Decimal(str(raw.strike_price)),
                        option_type=OptionType(_enum_value(raw.type)),
                        multiplier=Decimal(str(raw.multiplier or 100)),
                        tradable=bool(raw.tradable), open_interest=_decimal(raw.open_interest),
                    ))
                if not page.next_page_token:
                    return OptionChain(query.underlying.upper(), tuple(contracts),
                                       datetime.now(timezone.utc))
                filters["page_token"] = page.next_page_token
        if isinstance(query, QuotesQuery):
            from alpaca.data.enums import DataFeed, OptionsFeed
            from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest
            if query.asset_type == AssetType.OPTION:
                request = OptionLatestQuoteRequest(symbol_or_symbols=list(query.symbols),
                                                   feed=OptionsFeed.INDICATIVE)
                raw = self._market_client(query.asset_type).get_option_latest_quote(request)
                source, realtime = "Alpaca indicative options", False
            elif query.asset_type == AssetType.EQUITY:
                request = StockLatestQuoteRequest(symbol_or_symbols=list(query.symbols), feed=DataFeed.IEX)
                raw = self._market_client(query.asset_type).get_stock_latest_quote(request)
                source, realtime = "Alpaca IEX", True
            else:
                raise NotImplementedError(f"Alpaca Paper quotes for {query.asset_type.value} are not supported")
            return {symbol: Quote(
                symbol=symbol, bid=_decimal(item.bid_price), ask=_decimal(item.ask_price),
                last=None, mark=(Decimal(str(item.bid_price)) + Decimal(str(item.ask_price))) / 2
                if item.bid_price is not None and item.ask_price is not None else None,
                observed_at=item.timestamp, realtime=realtime, source=source,
            ) for symbol, item in raw.items()}
        if isinstance(query, FillsQuery):
            params = {"after": query.start.isoformat(), "until": query.end.isoformat(),
                      "direction": "asc", "page_size": 100}
            fills = []
            while True:
                page = self._client.get("/account/activities/FILL", params)
                if not isinstance(page, list):
                    raise ValueError("unexpected Alpaca fill response")
                fills.extend(_fill(row) for row in page)
                if len(page) < 100:
                    return tuple(fills)
                params["page_token"] = page[-1]["id"]
        if isinstance(query, OrdersQuery):
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            raw = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=query.start, until=query.end,
                limit=500, nested=True,
            ))
            groups = tuple(_order_group(order) for order in raw)
            return tuple(group for group in groups if not query.statuses or group.status in query.statuses)
        if isinstance(query, OrderQuery):
            from alpaca.trading.requests import GetOrderByIdRequest
            return _order_group(self._client.get_order_by_id(query.order_id, GetOrderByIdRequest(nested=True)))
        if isinstance(query, MarketSessionsQuery):
            from alpaca.trading.requests import GetCalendarRequest
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
            rows = self._client.get_calendar(GetCalendarRequest(start=query.start, end=query.end))
            return tuple(MarketSession(day.date, day.open.replace(tzinfo=et),
                                       day.close.replace(tzinfo=et), True) for day in rows)
        if not isinstance(query, AccountQuery):
            raise NotImplementedError(f"Alpaca Paper {type(query).__name__} is not connected yet")
        account = self._client.get_account()
        positions = []
        for raw in self._client.get_all_positions():
            kind = _enum_value(raw.asset_class)
            asset = {"us_equity": AssetType.EQUITY, "us_option": AssetType.OPTION,
                     "crypto": AssetType.CRYPTO}.get(kind)
            if asset is None:
                continue
            quantity = Decimal(str(raw.qty))
            side = PositionSide(_enum_value(raw.side))
            positions.append(Position(
                symbol=raw.symbol, asset_type=asset, side=side,
                quantity=abs(quantity), average_price=Decimal(str(raw.avg_entry_price)),
                underlying=raw.symbol if asset == AssetType.EQUITY else None,
            ))
        return AccountSnapshot(
            account_ref=str(account.id), status=_enum_value(account.status),
            balances=BalanceSnapshot(
                liquidation_value=_decimal(account.equity), equity=_decimal(account.equity),
                buying_power=_decimal(account.buying_power),
                day_trading_buying_power=_decimal(account.daytrading_buying_power),
                available_funds=_decimal(account.cash),
                maintenance_requirement=_decimal(account.maintenance_margin),
            ),
            positions=tuple(positions), observed_at=datetime.now(timezone.utc),
        )

    def _execute(self, command: BrokerCommand) -> MutationResult:
        raise NotImplementedError("Alpaca Paper order execution is not connected yet")
