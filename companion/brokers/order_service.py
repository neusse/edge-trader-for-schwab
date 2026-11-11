"""Account-bound mutation gate shared by broker order workflows."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from .adapter import BrokerAdapter
from .policy import contains_option_leg
from .models import (
    AccountQuery, AccountSnapshot, AssetType, BrokerCommand, CancelOrder, ClientOrderQuery, MarketSessionsQuery,
    MutationResult, MutationStatus, OrderClass, OrderInstruction, OrderQuery, OrderRole,
    OrderStatus, ReplaceOrder, SubmitOrder, TimeInForce,
)
from ..store import Store

ET = ZoneInfo("America/New_York")


class BrokerOrderService:
    def __init__(self, adapter: BrokerAdapter, store: Store, account_ref: str,
                 clock: Callable[[], datetime] | None = None,
                 *, options_trading_enabled: bool = True):
        if store.scope != (adapter.identity.key, account_ref):
            raise ValueError("order store must match broker and account")
        self.adapter = adapter
        self.store = store
        self.account_ref = account_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.options_trading_enabled = options_trading_enabled
        self._lock = threading.Lock()
        self.arm(False)

    def arm(self, enabled: bool) -> None:
        day = self.clock().astimezone(ET).date().isoformat()
        self.store.set_setting("broker_order_armed_for", day if enabled else "")

    @property
    def armed(self) -> bool:
        day = self.clock().astimezone(ET).date().isoformat()
        return self.store.get_setting("broker_order_armed_for") == day

    def _market_open(self, now: datetime) -> bool:
        day = now.astimezone(ET).date()
        sessions = self.adapter.query(MarketSessionsQuery(day, day))
        return bool(sessions and sessions[0].is_open and
                    sessions[0].opens_at <= now < sessions[0].closes_at)

    @staticmethod
    def _previous(row: dict) -> MutationResult:
        status = row["status"]
        if status == "SENDING":
            status = MutationStatus.UNKNOWN.value
        return MutationResult(MutationStatus(status), row["order_id"],
                              row["message"] or "This request was already sent; reconcile its outcome")

    def execute(self, command: BrokerCommand, request_id: str,
                preflight: Callable[[AccountSnapshot], None] | None = None) -> MutationResult:
        if not self.options_trading_enabled and contains_option_leg(command):
            return MutationResult(MutationStatus.REJECTED,
                                  message="Option trading is disabled for both brokers")
        if not self._lock.acquire(blocking=False):
            return MutationResult(MutationStatus.UNKNOWN, message="Another order action is in progress")
        try:
            payload = json.loads(json.dumps(asdict(command), default=str))
            client_order_id = None
            if isinstance(command, SubmitOrder):
                intent = getattr(command.intent, "entry", command.intent)
                client_order_id = intent.client_order_id
            elif isinstance(command, ReplaceOrder):
                client_order_id = command.client_order_id
            prior = self.store.get_mutation(request_id)
            if prior:
                if prior["command"] != type(command).__name__ or prior["payload"] != payload:
                    return MutationResult(MutationStatus.REJECTED,
                                          message="Request ID belongs to a different order action")
                return self._previous(prior)
            if self.store.open_mutations() or self.store.open_tickets():
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="An earlier order outcome must be reconciled first")
            if client_order_id and self.store.get_mutation_by_client_id(client_order_id):
                return MutationResult(MutationStatus.REJECTED,
                                      message="Client order ID was already used by another request")
            try:
                account = self.adapter.query(AccountQuery())
                if account.account_ref != self.account_ref:
                    return MutationResult(MutationStatus.REJECTED,
                                          message="Active account changed; no order was sent")
                if not self.options_trading_enabled and isinstance(command, (CancelOrder, ReplaceOrder)):
                    group = self.adapter.query(OrderQuery(command.order_id))
                    legs = tuple(leg for order in group.orders for leg in order.legs) if group else ()
                    if not legs or any(leg.asset_type != AssetType.EQUITY for leg in legs):
                        return MutationResult(MutationStatus.REJECTED,
                                              message="Only equity orders may be changed in this app")
                if preflight is not None:
                    preflight(account)
                if isinstance(command, SubmitOrder):
                    if not self.armed:
                        return MutationResult(MutationStatus.REJECTED,
                                              message="Arm entries for this session before submitting")
                    if not self._market_open(self.clock()):
                        return MutationResult(MutationStatus.REJECTED,
                                              message="Regular market session is closed")
            except ValueError as exc:
                return MutationResult(MutationStatus.REJECTED, message=str(exc))
            except Exception:
                return MutationResult(MutationStatus.REJECTED,
                                      message="Account or market verification failed; no order was sent")
            if not self.store.record_mutation(request_id, type(command).__name__, payload,
                                              client_order_id=client_order_id):
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Another order action or request ID reserved this account")
            try:
                result = self.adapter.execute(command)
            except Exception:
                result = MutationResult(MutationStatus.UNKNOWN,
                                        message="Order outcome unknown; reconcile before another action")
            self.store.finish_mutation(request_id, result.status.value,
                                       result.order_id, result.message)
            return result
        finally:
            self._lock.release()

    def reconcile_submit(self, request_id: str) -> MutationResult:
        """Resolve a submit only when the broker confirms its exact client order ID."""
        if not self._lock.acquire(blocking=False):
            return MutationResult(MutationStatus.UNKNOWN, message="Another order action is in progress")
        try:
            row = self.store.get_mutation(request_id)
            if row is None:
                return MutationResult(MutationStatus.REJECTED, message="Unknown request ID")
            if row["command"] != "SubmitOrder":
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="This action requires order-state reconciliation")
            if row["status"] not in ("SENDING", MutationStatus.UNKNOWN.value):
                return self._previous(row)
            intent = row["payload"]["intent"]
            client_order_id = intent.get("client_order_id") or intent.get("entry", {}).get("client_order_id")
            if not client_order_id:
                return MutationResult(MutationStatus.UNKNOWN, message="Client order ID is unavailable")
            try:
                account = self.adapter.query(AccountQuery())
                if account.account_ref != self.account_ref:
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Active account changed; cannot reconcile")
                group = self.adapter.query(ClientOrderQuery(client_order_id))
            except Exception:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker lookup failed; outcome is still unknown")
            if group is None:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker has not confirmed this client order ID")
            match = next((order for order in group.orders
                          if order.client_order_id == client_order_id), None)
            if match is None:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker response did not match the client order ID")
            if client_order_id.startswith("et-vertical-") and match.status != OrderStatus.REJECTED:
                expected_legs = intent.get("legs") or ()
                if (group.order_class != OrderClass.MULTI_LEG or
                        len(group.orders) != 1 or len(match.legs) != 2 or
                        match.parent_id is not None or len(expected_legs) != 2 or
                        match.limit_price != Decimal(str(intent.get("limit_price"))) or
                        match.time_in_force.value != intent.get("time_in_force") or
                        tuple((leg.symbol, leg.asset_type.value, leg.instruction.value,
                               leg.quantity) for leg in match.legs) !=
                        tuple((leg["symbol"], leg["asset_type"], leg["instruction"],
                               Decimal(str(leg["quantity"]))) for leg in expected_legs)):
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Broker multi-leg order does not match the reviewed vertical")
            status = (MutationStatus.REJECTED if match.status == OrderStatus.REJECTED
                      else MutationStatus.ACCEPTED)
            message = f"Reconciled against broker order status {match.status.value}"
            self.store.finish_mutation(request_id, status.value, match.order_id, message)
            return MutationResult(status, match.order_id, message)
        finally:
            self._lock.release()

    def reconcile_cancel(self, request_id: str) -> MutationResult:
        """Release an ambiguous cancel only after a fresh broker read confirms it is final."""
        if not self._lock.acquire(blocking=False):
            return MutationResult(MutationStatus.UNKNOWN, message="Another order action is in progress")
        try:
            row = self.store.get_mutation(request_id)
            if row is None:
                return MutationResult(MutationStatus.REJECTED, message="Unknown request ID")
            if row["command"] != "CancelOrder":
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="This request is not a cancel action")
            if row["status"] not in ("SENDING", MutationStatus.UNKNOWN.value):
                return self._previous(row)
            order_id = row["payload"]["order_id"]
            try:
                account = self.adapter.query(AccountQuery())
                if account.account_ref != self.account_ref:
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Active account changed; cannot reconcile")
                group = self.adapter.query(OrderQuery(order_id))
            except Exception:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker lookup failed; cancel outcome is still unknown")
            order = next((item for item in group.orders if item.order_id == order_id), None)
            if order is None:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker response did not match the order ID")
            if order.status == OrderStatus.CANCELED:
                result = MutationResult(MutationStatus.ACCEPTED, order_id,
                                        "Broker confirms the order was canceled")
            elif order.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                result = MutationResult(MutationStatus.REJECTED, order_id,
                                        f"Broker confirms order status {order.status.value}; cancel did not protect it")
            else:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message=f"Broker order is {order.status.value}; cancel outcome remains unknown")
            self.store.finish_mutation(request_id, result.status.value,
                                       result.order_id, result.message)
            return result
        finally:
            self._lock.release()

    def reconcile_replace(self, request_id: str) -> MutationResult:
        """Confirm an exact replacement client ID after Alpaca finishes the replace."""
        if not self._lock.acquire(blocking=False):
            return MutationResult(MutationStatus.UNKNOWN, message="Another order action is in progress")
        try:
            row = self.store.get_mutation(request_id)
            if row is None:
                return MutationResult(MutationStatus.REJECTED, message="Unknown request ID")
            if row["command"] != "ReplaceOrder":
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="This request is not a replace action")
            if row["status"] not in ("SENDING", MutationStatus.UNKNOWN.value):
                return self._previous(row)
            client_order_id = row["payload"].get("client_order_id")
            if not client_order_id:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Replacement client order ID is unavailable")
            try:
                account = self.adapter.query(AccountQuery())
                if account.account_ref != self.account_ref:
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Active account changed; cannot reconcile")
                group = self.adapter.query(ClientOrderQuery(client_order_id))
            except Exception:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker lookup failed; replacement outcome is still unknown")
            if group is None:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker has not confirmed this replacement client ID")
            order = next((item for item in group.orders
                          if item.client_order_id == client_order_id), None)
            if order is None:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker response did not match the replacement client ID")
            expected_limit = row["payload"].get("limit_price")
            if expected_limit is not None and order.limit_price != Decimal(str(expected_limit)):
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker replacement price does not match the reviewed price")
            expected_stop = row["payload"].get("stop_price")
            if expected_stop is not None and order.stop_price != Decimal(str(expected_stop)):
                return MutationResult(MutationStatus.UNKNOWN,
                                      message="Broker replacement stop does not match the reviewed price")
            if client_order_id.startswith("et-autobe-") and order.status != OrderStatus.REJECTED:
                target = next((item for item in group.orders
                               if item.role == OrderRole.TARGET), None)
                entry = next((item for item in group.orders
                              if item.role == OrderRole.ENTRY), None)
                shape_ok = (
                    group.order_class == OrderClass.OCO and len(group.orders) == 2 or
                    group.order_class == OrderClass.BRACKET and len(group.orders) == 3 and
                    len(order.legs) == 1 and entry is not None and
                    entry.status == OrderStatus.FILLED and
                    len(entry.legs) == 1 and entry.legs[0].asset_type == AssetType.EQUITY and
                    entry.legs[0].quantity == order.legs[0].quantity and
                    entry.legs[0].filled_quantity == order.legs[0].quantity and
                    entry.legs[0].symbol == order.legs[0].symbol and
                    entry.legs[0].instruction != order.legs[0].instruction)
                if (not shape_ok or
                        order.role != OrderRole.STOP or target is None or
                        target.status != OrderStatus.WORKING or
                        len(order.legs) != 1 or len(target.legs) != 1 or
                        order.legs[0].asset_type != AssetType.EQUITY or
                        target.legs[0].asset_type != AssetType.EQUITY or
                        order.legs[0].symbol != target.legs[0].symbol or
                        order.legs[0].quantity != target.legs[0].quantity or
                        order.legs[0].instruction != target.legs[0].instruction or
                        order.legs[0].instruction not in (OrderInstruction.SELL,
                                                          OrderInstruction.BUY) or
                        order.time_in_force not in (TimeInForce.DAY, TimeInForce.GTC) or
                        target.time_in_force != order.time_in_force or
                        target.limit_price is None or
                        order.legs[0].filled_quantity != 0 or
                        target.legs[0].filled_quantity != 0):
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Broker Auto B/E replacement did not retain the paired equity target")
            if client_order_id.startswith("et-vertical-target-") and order.status != OrderStatus.REJECTED:
                try:
                    original = self.adapter.query(OrderQuery(row["payload"]["order_id"]))
                except Exception:
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Original vertical target could not be verified")
                old = next((item for item in original.orders if item.order_id ==
                            row["payload"]["order_id"]), None)
                if (group.order_class != OrderClass.MULTI_LEG or
                        original.order_class != OrderClass.MULTI_LEG or old is None or
                        order.role != OrderRole.TARGET or len(order.legs) != 2 or
                        order.time_in_force != TimeInForce.GTC or
                        tuple((leg.symbol, leg.quantity, leg.instruction, leg.asset_type)
                              for leg in order.legs) !=
                        tuple((leg.symbol, leg.quantity, leg.instruction, leg.asset_type)
                              for leg in old.legs) or
                        {leg.instruction for leg in order.legs} != {
                            OrderInstruction.SELL_TO_CLOSE, OrderInstruction.BUY_TO_CLOSE} or
                        any(leg.asset_type != AssetType.OPTION for leg in order.legs)):
                    return MutationResult(MutationStatus.UNKNOWN,
                                          message="Broker replacement vertical legs do not match the reviewed target")
            if order.status == OrderStatus.REJECTED:
                result = MutationResult(MutationStatus.REJECTED, order.order_id,
                                        "Broker rejected the replacement; inspect the original order")
            elif order.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED,
                                  OrderStatus.FILLED):
                result = MutationResult(MutationStatus.ACCEPTED, order.order_id,
                                        f"Broker confirms replacement status {order.status.value}")
            else:
                return MutationResult(MutationStatus.UNKNOWN,
                                      message=f"Replacement is {order.status.value}; check again before another action")
            self.store.finish_mutation(request_id, result.status.value,
                                       result.order_id, result.message)
            return result
        finally:
            self._lock.release()
