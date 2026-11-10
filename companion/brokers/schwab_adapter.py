"""Schwab REST translation into canonical account and order models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .adapter import BrokerAdapter
from .capabilities import SCHWAB
from .schwab_orders import build_schwab_order
from .models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, BrokerCommand, CancelOrder,
    BrokerOrder, BrokerQuery, BrokerQueryResult, Fill, FillsQuery,
    MarketSession, MarketSessionsQuery, MutationResult, MutationStatus,
    OptionGreeks, Quote, QuotesQuery,
    OrderClass, OrderGroup, OrderInstruction, OrderLeg, OrderQuery, OrderRole,
    OrdersQuery, OrderStatus, OrderType, Position, PositionSide, ReplaceOrder, SubmitOrder,
    TimeInForce, MultiLegOrderIntent, TrailingOrderIntent,
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
            previous_session_quantity=(Decimal(str(row.get("previousSessionLongQuantity") or 0)) -
                                       Decimal(str(row.get("previousSessionShortQuantity") or 0))),
            current_day_cost=_decimal(row.get("currentDayCost")),
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
    closing = {OrderInstruction.SELL, OrderInstruction.BUY_TO_COVER,
               OrderInstruction.BUY_TO_CLOSE, OrderInstruction.SELL_TO_CLOSE}
    root_instructions = set()
    for leg in raw.get("orderLegCollection") or ():
        name = (leg.get("instruction") or "").lower().replace("_", "-")
        try:
            root_instructions.add(OrderInstruction(name))
        except ValueError:
            pass
    root_role = (OrderRole.EXIT if strategy != "TRIGGER" and root_instructions and
                 root_instructions <= closing else OrderRole.ENTRY)

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

    visit(raw, None, root_role)
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
            if asset == AssetType.FUTURE:
                symbol = symbol.split(":")[0]
            cost = _decimal(row.get("cost"))
            multiplier = (abs(cost / (amount * price)).quantize(Decimal("0.0001"))
                          if cost else None)
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
                underlying=(instrument.get("underlyingSymbol") or
                            instrument.get("rootSymbol") or symbol),
                multiplier=multiplier,
            ))
            charged = True
            fee_charged.add(transaction_id)
    def sequence(fill):
        activity, _, index = fill.activity_id.partition(":")
        return (int(activity) if activity.isdigit() else 0,
                int(index) if index.isdigit() else 0)
    return tuple(sorted(fills, key=lambda fill: (fill.occurred_at, sequence(fill))))


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
            return tuple(group for group in groups if not query.statuses or
                         any(order.status in query.statuses for order in group.orders))
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
        if isinstance(command, ReplaceOrder) and command.replacement is None:
            return MutationResult(MutationStatus.REJECTED,
                                  message="Schwab replace needs a complete reviewed order plan")
        if isinstance(command, (CancelOrder, ReplaceOrder)) and not str(command.order_id).isdigit():
            return MutationResult(MutationStatus.REJECTED,
                                  message="Schwab order ID must be numeric; nothing was sent")
        intent = (command.intent if isinstance(command, SubmitOrder) else
                  command.replacement if isinstance(command, ReplaceOrder) else None)
        if intent is not None:
            try:
                body = build_schwab_order(intent)
            except (TypeError, ValueError) as exc:
                return MutationResult(MutationStatus.REJECTED, message=str(exc))
        elif not isinstance(command, CancelOrder):
            raise TypeError(f"unknown Schwab command: {type(command).__name__}")
        try:
            client, account_hash = self._ready()
        except Exception:
            return MutationResult(MutationStatus.REJECTED,
                                  message="Schwab account is unavailable; no action was sent")
        if intent is not None and (
                isinstance(intent, MultiLegOrderIntent) or
                isinstance(intent, TrailingOrderIntent) and
                intent.leg.asset_type == AssetType.OPTION):
            try:
                preview = client.preview_order(account_hash, body)
                if preview.status_code == 429:
                    return MutationResult(MutationStatus.RATE_LIMITED,
                                          message="Schwab rate limited the order preview; nothing was sent")
                if preview.status_code not in (200, 201):
                    return MutationResult(MutationStatus.REJECTED,
                                          message="Schwab did not confirm the order preview; nothing was sent")
                answer = preview.json()
                if not isinstance(answer, dict):
                    raise ValueError("unreadable preview")
                validation = answer.get("orderValidationResult")
                if not isinstance(validation, dict) and not isinstance(answer.get("orderStrategy"), dict):
                    raise ValueError("unreadable preview")
                rejects = validation.get("rejects", []) if isinstance(validation, dict) else []
                if not isinstance(rejects, list):
                    raise ValueError("unreadable preview")
                if rejects:
                    return MutationResult(MutationStatus.REJECTED,
                                          message="Schwab preview rejected the order; nothing was sent")
            except Exception:
                return MutationResult(MutationStatus.REJECTED,
                                      message="Schwab preview could not be verified; nothing was sent")
        try:
            if isinstance(command, SubmitOrder):
                response = client.place_order(account_hash, body)
            elif isinstance(command, ReplaceOrder):
                response = client.replace_order(account_hash, int(command.order_id), body)
            else:
                response = client.cancel_order(account_hash, int(command.order_id))
        except Exception:
            return MutationResult(MutationStatus.UNKNOWN,
                                  message="Schwab order outcome unknown; reconcile before another action")
        code = response.status_code
        if code in (200, 201, 202, 204):
            if isinstance(command, CancelOrder):
                return MutationResult(MutationStatus.ACCEPTED, str(command.order_id))
            location = (getattr(response, "headers", {}) or {}).get("Location", "")
            tail = location.rstrip("/").split("/")[-1]
            if tail.isdigit():
                return MutationResult(MutationStatus.ACCEPTED, tail)
            return MutationResult(MutationStatus.UNKNOWN,
                                  message="Schwab accepted the order without a trackable ID")
        if code == 429:
            raw = (getattr(response, "headers", {}) or {}).get("Retry-After")
            try:
                retry = float(raw) if raw is not None else None
            except ValueError:
                retry = None
            return MutationResult(MutationStatus.RATE_LIMITED,
                                  message="Schwab rate limited the order action",
                                  retry_after_seconds=retry)
        if code == 408 or code >= 500:
            return MutationResult(MutationStatus.UNKNOWN,
                                  message="Schwab order outcome unknown; reconcile before another action")
        if 400 <= code < 500:
            return MutationResult(MutationStatus.REJECTED,
                                  message=f"Schwab rejected the order action (HTTP {code})")
        return MutationResult(MutationStatus.UNKNOWN,
                              message=f"Schwab order response was unclear (HTTP {code})")
