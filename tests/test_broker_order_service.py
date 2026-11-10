from datetime import date, datetime, timezone
from decimal import Decimal

from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, CancelOrder,
    BrokerOrder, ClientOrderQuery, MarketSession, MarketSessionsQuery, OrderQuery,
    MutationResult, MutationStatus, OrderClass, OrderGroup, OrderRole,
    OrderStatus, OrderLeg,
    OrderInstruction, OrderLegIntent, OrderType, SimpleOrderIntent,
    SubmitOrder, TimeInForce,
)
from companion.brokers.order_service import BrokerOrderService
from companion.store import Store

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
ACCOUNT = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot())
SESSION = (MarketSession(date(2026, 11, 10),
                         datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc),
                         datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc), True),)


def command():
    return SubmitOrder(SimpleOrderIntent(
        OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(1)),
        OrderType.LIMIT, TimeInForce.DAY, "stable-client-id", limit_price=Decimal(100)))


def test_submit_is_account_bound_armed_and_deduplicated(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT)
    adapter.given_query(MarketSessionsQuery, SESSION)
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "order-uuid"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    try:
        assert not service.armed
        service.arm(True)
        first = service.execute(command(), "request-1")
        second = service.execute(command(), "request-1")
        assert first.accepted and second.accepted
        assert first.order_id == second.order_id == "order-uuid"
        assert len(adapter.commands) == 1
        assert store.get_mutation("request-1")["payload"]["intent"]["client_order_id"] == "stable-client-id"
        mismatch = service.execute(CancelOrder("order-uuid"), "request-1")
        assert mismatch.status == MutationStatus.REJECTED
        assert len(adapter.commands) == 1
        reused_client_id = service.execute(command(), "request-2")
        assert reused_client_id.status == MutationStatus.REJECTED
        assert len(adapter.commands) == 1
    finally:
        store.close()


def test_unknown_result_blocks_followup_even_after_restart(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT)
    adapter.given_query(MarketSessionsQuery, SESSION)
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.UNKNOWN, message="timeout"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    service.arm(True)
    assert service.execute(command(), "request-1").status == MutationStatus.UNKNOWN
    store.close()

    reopened = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        restarted = BrokerOrderService(adapter, reopened, "paper-account", clock=lambda: NOW)
        restarted.arm(True)
        blocked = restarted.execute(command(), "request-2")
        assert blocked.status == MutationStatus.UNKNOWN
        assert len(adapter.commands) == 1
        assert reopened.open_mutations()[0]["request_id"] == "request-1"
        adapter.given_query(AccountQuery, ACCOUNT)
        adapter.given_query(ClientOrderQuery, confirmed_order())
        resolved = restarted.reconcile_submit("request-1")
        assert resolved.status == MutationStatus.ACCEPTED
        assert resolved.order_id == "confirmed-uuid"
        assert reopened.open_mutations() == []
        assert len(adapter.commands) == 1
    finally:
        reopened.close()


def confirmed_order(client_order_id="stable-client-id"):
    order = BrokerOrder("confirmed-uuid", OrderStatus.WORKING, OrderType.LIMIT,
                        TimeInForce.DAY,
                        (OrderLeg("AAPL", AssetType.EQUITY,
                                  OrderInstruction.BUY, Decimal(1)),),
                        OrderRole.ENTRY, client_order_id=client_order_id)
    return OrderGroup("confirmed-uuid", OrderClass.SIMPLE, OrderStatus.WORKING, (order,))


def test_reconcile_requires_exact_client_order_id(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_query(MarketSessionsQuery, SESSION)
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.UNKNOWN))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    service.arm(True)
    try:
        assert service.execute(command(), "request-1").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery, None, confirmed_order("other-client-id"))
        assert service.reconcile_submit("request-1").status == MutationStatus.UNKNOWN
        assert service.reconcile_submit("request-1").status == MutationStatus.UNKNOWN
        assert len(store.open_mutations()) == 1
    finally:
        store.close()


def test_account_change_refuses_cancel_before_broker_call(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, AccountSnapshot("different-account", "ACTIVE", BalanceSnapshot()))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    try:
        result = service.execute(CancelOrder("order-uuid"), "cancel-1")
        assert result.status == MutationStatus.REJECTED
        assert adapter.commands == []
        assert store.get_mutation("cancel-1") is None
    finally:
        store.close()


def test_submit_refused_without_arming_or_regular_session(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT)
    adapter.given_query(MarketSessionsQuery, ())
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    try:
        assert service.execute(command(), "first").status == MutationStatus.REJECTED
        service.arm(True)
        assert service.execute(command(), "second").status == MutationStatus.REJECTED
        assert adapter.commands == []
    finally:
        store.close()


def test_unknown_cancel_blocks_actions_until_broker_confirms_final_status(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(CancelOrder, MutationResult(MutationStatus.UNKNOWN,
                                                      message="timeout"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    def group(status):
        order = BrokerOrder("order-uuid", status, OrderType.LIMIT, TimeInForce.DAY,
                            (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY,
                                      Decimal(1)),), OrderRole.ENTRY)
        return OrderGroup("order-uuid", OrderClass.SIMPLE, status, (order,))
    try:
        assert service.execute(CancelOrder("order-uuid"), "cancel-1").status == MutationStatus.UNKNOWN
        assert service.execute(CancelOrder("another-order"), "cancel-2").status == MutationStatus.UNKNOWN
        assert len(adapter.commands) == 1
        adapter.given_query(OrderQuery, group(OrderStatus.WORKING), group(OrderStatus.CANCELED))
        assert service.reconcile_cancel("cancel-1").status == MutationStatus.UNKNOWN
        assert service.reconcile_cancel("cancel-1").status == MutationStatus.ACCEPTED
        assert store.open_mutations() == []
        assert service.reconcile_cancel("cancel-1").status == MutationStatus.ACCEPTED
    finally:
        store.close()
