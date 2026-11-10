"""Account-bound mutation gate shared by broker order workflows."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .adapter import BrokerAdapter
from .models import (
    AccountQuery, BrokerCommand, ClientOrderQuery, MarketSessionsQuery,
    MutationResult, MutationStatus, OrderQuery, OrderStatus, SubmitOrder,
)
from ..store import Store

ET = ZoneInfo("America/New_York")


class BrokerOrderService:
    def __init__(self, adapter: BrokerAdapter, store: Store, account_ref: str,
                 clock: Callable[[], datetime] | None = None):
        if store.scope != (adapter.identity.key, account_ref):
            raise ValueError("order store must match broker and account")
        self.adapter = adapter
        self.store = store
        self.account_ref = account_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))
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

    def execute(self, command: BrokerCommand, request_id: str) -> MutationResult:
        if not self._lock.acquire(blocking=False):
            return MutationResult(MutationStatus.UNKNOWN, message="Another order action is in progress")
        try:
            payload = json.loads(json.dumps(asdict(command), default=str))
            client_order_id = None
            if isinstance(command, SubmitOrder):
                intent = getattr(command.intent, "entry", command.intent)
                client_order_id = intent.client_order_id
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
                if isinstance(command, SubmitOrder):
                    if not self.armed:
                        return MutationResult(MutationStatus.REJECTED,
                                              message="Arm entries for this session before submitting")
                    if not self._market_open(self.clock()):
                        return MutationResult(MutationStatus.REJECTED,
                                              message="Regular market session is closed")
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
