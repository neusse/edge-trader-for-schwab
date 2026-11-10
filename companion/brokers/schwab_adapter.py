"""Schwab REST translation into canonical account and order models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .adapter import BrokerAdapter
from .capabilities import SCHWAB
from .models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, BrokerCommand,
    BrokerOrder, BrokerQuery, BrokerQueryResult, Fill, FillsQuery,
    MarketSession, MarketSessionsQuery, MutationResult,
    OptionGreeks, Quote, QuotesQuery,
    OrderClass, OrderGroup, OrderInstruction, OrderLeg, OrderQuery, OrderRole,
    OrdersQuery, OrderStatus, OrderType, Position, PositionSide, TimeInForce,
)


def _decimal(value):
    return None if value is None else Decimal(str(value))


def _response(response):
    if response.status_code != 200:
        raise RuntimeError(f"Schwab read failed (HTTP {response.status_code})")
    return response.json()


_ASSET = {"EQUITY": AssetType.EQUITY, "OPTION": AssetType.OPTION,
          "FUTURE": AssetType.FUTURE}
_STATUS = {
    "WORKING": OrderStatus.WORKING, "QUEUED": OrderStatus.PENDING,
    "PENDING_ACTIVATION": OrderStatus.PENDING, "AWAITING_PARENT_ORDER": OrderStatus.PENDING,
    "AWAITING_CONDITION": OrderStatus.PENDING, "PENDING_CANCEL": OrderStatus.PENDING,
    "PENDING_REPLACE": OrderStatus.PENDING, "FILLED": OrderStatus.FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "CANCELED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED, "REPLACED": OrderStatus.REPLACED,
}
_ORDER_TYPE = {"MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT,
               "STOP": OrderType.STOP, "STOP_LIMIT": OrderType.STOP_LIMIT,
               "TRAILING_STOP": OrderType.TRAILING_STOP,
               "NET_DEBIT": OrderType.LIMIT, "NET_CREDIT": OrderType.LIMIT,
               "NET_ZERO": OrderType.LIMIT}


def normalize_account(raw: dict, account_ref: str) -> AccountSnapshot:
    account = raw["securitiesAccount"]
    balances = account.get("currentBalances") or {}
    positions = []
    for row in account.get("positions") or ():
        instrument = row.get("instrument") or {}
        asset = _ASSET.get(instrument.get("assetType"))
        symbol = instrument.get("symbol")
        qty = Decimal(str(row.get("longQuantity") or 0)) - Decimal(str(row.get("shortQuantity") or 0))
        if not asset or not symbol or not qty:
            continue
        positions.append(Position(
            symbol=symbol, asset_type=asset,
            side=PositionSide.LONG if qty > 0 else PositionSide.SHORT,
            quantity=abs(qty), average_price=_decimal(row.get("averagePrice")),
            underlying=instrument.get("underlyingSymbol") or instrument.get("rootSymbol") or symbol,
        ))
    return AccountSnapshot(
        account_ref=account_ref, status="CONNECTED",
        balances=BalanceSnapshot(
            liquidation_value=_decimal(balances.get("liquidationValue")),
            equity=_decimal(balances.get("equity")),
            buying_power=_decimal(balances.get("buyingPower")),
            day_trading_buying_power=_decimal(balances.get("dayTradingBuyingPower")),
            available_funds=_decimal(balances.get("availableFunds")),
            maintenance_requirement=_decimal(balances.get("maintenanceRequirement")),
        ),
        positions=tuple(positions), observed_at=datetime.now(timezone.utc),
    )


def normalize_order(raw: dict) -> OrderGroup:
    root_id = str(raw["orderId"])
    strategy = raw.get("orderStrategyType")
    if strategy == "TRIGGER":
        order_class = OrderClass.BRACKET
    elif strategy == "OCO":
        order_class = OrderClass.OCO
    elif len(raw.get("orderLegCollection") or ()) > 1:
        order_class = OrderClass.MULTI_LEG
    else:
        order_class = OrderClass.SIMPLE
    members = []

    def visit(node: dict, parent_id: str | None, role: OrderRole) -> None:
        node_id = str(node.get("orderId") or root_id)
        legs = []
        for row in node.get("orderLegCollection") or ():
            instrument = row.get("instrument") or {}
            asset = _ASSET.get(instrument.get("assetType"))
            symbol = instrument.get("symbol")
            quantity = _decimal(row.get("quantity"))
            name = (row.get("instruction") or "").lower().replace("_", "-")
            if not asset or not symbol or not quantity or not name:
                continue
            legs.append(OrderLeg(
                symbol=symbol, asset_type=asset, instruction=OrderInstruction(name),
                quantity=quantity,
                filled_quantity=min(quantity, _decimal(node.get("filledQuantity")) or Decimal(0)),
                role=role, underlying=instrument.get("underlyingSymbol"),
                leg_ref=str(row.get("legId")) if row.get("legId") is not None else None,
            ))
        status = _STATUS.get(node.get("status"), OrderStatus.UNKNOWN)
        if legs:
            kind = node.get("orderType") or "LIMIT"
            members.append(BrokerOrder(
                order_id=node_id, status=status,
                order_type=_ORDER_TYPE.get(kind, OrderType.LIMIT),
                time_in_force=TimeInForce.GTC if node.get("duration") in
                ("GOOD_TILL_CANCEL", "GTC") else TimeInForce.DAY,
                legs=tuple(legs), role=role, group_id=root_id, parent_id=parent_id,
                limit_price=_decimal(node.get("price")), stop_price=_decimal(node.get("stopPrice")),
                entered_at=datetime.fromisoformat(node["enteredTime"].replace("Z", "+00:00"))
                if node.get("enteredTime") else None,
            ))
        for child in node.get("childOrderStrategies") or ():
            kind = child.get("orderType")
            child_role = (OrderRole.STOP if kind in ("STOP", "STOP_LIMIT", "TRAILING_STOP")
                          else OrderRole.TARGET if kind == "LIMIT" else OrderRole.EXIT)
            visit(child, node_id if legs else parent_id, child_role)

    visit(raw, None, OrderRole.ENTRY)
    return OrderGroup(root_id, order_class,
                      _STATUS.get(raw.get("status"), OrderStatus.UNKNOWN), tuple(members))


def normalize_fills(transactions: list[dict]) -> tuple[Fill, ...]:
    fills = []
    seen = set()
    fee_charged = set()
    for transaction in transactions:
        if transaction.get("type") != "TRADE" or transaction.get("status") not in (None, "VALID"):
            continue
        items = transaction.get("transferItems") or ()
        fees = sum((Decimal(str(row.get("cost") or 0)) for row in items
                    if (row.get("instrument") or {}).get("assetType") == "CURRENCY"), Decimal(0))
        transaction_id = str(transaction["activityId"])
        charged = transaction_id in fee_charged
        for index, row in enumerate(items):
            instrument = row.get("instrument") or {}
            asset = _ASSET.get(instrument.get("assetType"))
            symbol = instrument.get("quoteSymbol") or instrument.get("symbol")
            amount, price = _decimal(row.get("amount")), _decimal(row.get("price"))
            if not asset or not symbol or not amount or not price or price <= 0:
                continue
            activity_id = f"{transaction['activityId']}:{index}"
            if activity_id in seen:
                continue
            seen.add(activity_id)
            effect = row.get("positionEffect")
            if effect == "OPENING":
                instruction = (OrderInstruction.BUY_TO_OPEN if amount > 0
                               else OrderInstruction.SELL_TO_OPEN)
            elif effect == "CLOSING":
                instruction = (OrderInstruction.BUY_TO_CLOSE if amount > 0
                               else OrderInstruction.SELL_TO_CLOSE)
            else:
                instruction = OrderInstruction.BUY if amount > 0 else OrderInstruction.SELL
            occurred_at = datetime.fromisoformat(transaction["time"].replace("Z", "+00:00"))
            fills.append(Fill(
                activity_id=activity_id,
                order_id=str(transaction["orderId"]) if transaction.get("orderId") is not None else None,
                symbol=symbol, asset_type=asset, instruction=instruction,
                quantity=abs(amount), price=price, occurred_at=occurred_at,
                fees=min(fees, Decimal(0)) if not charged else Decimal(0),
            ))
            charged = True
            fee_charged.add(transaction_id)
    return tuple(sorted(fills, key=lambda fill: (fill.occurred_at, fill.activity_id)))


class SchwabAdapter(BrokerAdapter):
    def __init__(self, client=None, account_hash: str | None = None):
        super().__init__(SCHWAB)
        self._client = client
        self._account_hash = account_hash

    def _ready(self):
        if self._client is None:
            from .. import schwab
            self._client = schwab.make_client()
        if self._account_hash is None:
            accounts = _response(self._client.linked_accounts())
            if not accounts or "hashValue" not in accounts[0]:
                raise RuntimeError("Schwab account discovery failed")
            self._account_hash = accounts[0]["hashValue"]
        return self._client, self._account_hash

    def _query(self, query: BrokerQuery) -> BrokerQueryResult:
        client, account_hash = self._ready()
        if isinstance(query, AccountQuery):
            return normalize_account(_response(client.account_details(account_hash, fields="positions")),
                                     account_hash)
        if isinstance(query, OrdersQuery):
            raw = _response(client.account_orders(account_hash, query.start, query.end))
            groups = tuple(normalize_order(row) for row in raw)
            return tuple(group for group in groups if not query.statuses or group.status in query.statuses)
        if isinstance(query, OrderQuery):
            return normalize_order(_response(client.order_details(account_hash, query.order_id)))
        if isinstance(query, FillsQuery):
            def collect(start, end):
                rows = _response(client.transactions(account_hash, start, end, "TRADE"))
                if len(rows) < 3000:
                    return rows
                if end - start <= timedelta(hours=1):
                    raise ValueError("Schwab transaction result reached its cap inside one hour")
                mid = start + (end - start) / 2
                return collect(start, mid) + collect(mid, end)

            return normalize_fills(collect(query.start, query.end))
        if isinstance(query, QuotesQuery):
            raw = _response(client.quotes(list(query.symbols)))
            out = {}
            for symbol in query.symbols:
                item = raw.get(symbol) or {}
                quote = item.get("quote") or {}
                if not quote:
                    continue
                bid, ask = _decimal(quote.get("bidPrice")), _decimal(quote.get("askPrice"))
                mark = _decimal(quote.get("mark"))
                if mark is None and bid is not None and ask is not None:
                    mark = (bid + ask) / 2
                at = quote.get("quoteTime")
                observed = (datetime.fromtimestamp(at / 1000, timezone.utc)
                            if isinstance(at, (int, float)) else datetime.now(timezone.utc))
                greeks = OptionGreeks(
                    delta=_decimal(quote.get("delta")), gamma=_decimal(quote.get("gamma")),
                    theta=_decimal(quote.get("theta")), vega=_decimal(quote.get("vega")),
                    rho=_decimal(quote.get("rho"))) if query.asset_type == AssetType.OPTION else None
                out[symbol] = Quote(
                    symbol=symbol, bid=bid, ask=ask, last=_decimal(quote.get("lastPrice")),
                    mark=mark,
                    observed_at=observed, realtime=bool(item.get("realtime")), source="Schwab",
                    volume=_decimal(quote.get("totalVolume")), greeks=greeks,
                )
            return out
        if isinstance(query, MarketSessionsQuery):
            sessions = []
            day = query.start
            while day <= query.end:
                raw = _response(client.market_hours(["equity"], date=day.isoformat()))
                equity = next(iter((raw.get("equity") or {}).values()), None)
                if not isinstance(equity, dict) or not isinstance(equity.get("isOpen"), bool):
                    raise ValueError("Schwab market-hours response is incomplete")
                hours = (equity.get("sessionHours") or {}).get("regularMarket") or []
                if equity["isOpen"]:
                    if not hours or not hours[0].get("start") or not hours[-1].get("end"):
                        raise ValueError("Schwab open session has no regular hours")
                    opened = datetime.fromisoformat(hours[0]["start"])
                    closed = datetime.fromisoformat(hours[-1]["end"])
                    if opened.tzinfo is None or closed.tzinfo is None:
                        raise ValueError("Schwab market hours have no timezone")
                else:
                    opened = closed = None
                sessions.append(MarketSession(day, opened, closed, equity["isOpen"]))
                day += timedelta(days=1)
            return tuple(sessions)
        raise NotImplementedError(f"Schwab {type(query).__name__} canonical query is not connected yet")

    def _execute(self, command: BrokerCommand) -> MutationResult:
        raise NotImplementedError("Schwab canonical execution is not connected yet")
