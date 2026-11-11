from datetime import date, datetime, timezone
from decimal import Decimal
import pytest

from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, AssetType, BalanceSnapshot, CancelOrder,
    BrokerOrder, ClientOrderQuery, MarketSession, MarketSessionsQuery, OrderQuery, OrdersQuery,
    MutationResult, MutationStatus, MultiLegOrderIntent, OrderClass, OrderGroup, OrderRole,
    OrderStatus, OrderLeg,
    OrderInstruction, OrderLegIntent, OrderType, ReplaceOrder, SimpleOrderIntent,
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


def test_replacement_waits_for_exact_client_order_id_and_working_status(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "new-order"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    def replacement(status, client_id, price="99"):
        order = BrokerOrder("new-order", status, OrderType.LIMIT, TimeInForce.DAY,
                            (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY,
                                      Decimal(1)),), OrderRole.ENTRY,
                            client_order_id=client_id, limit_price=Decimal(price))
        return OrderGroup("new-order", OrderClass.BRACKET, status, (order,))
    try:
        command = ReplaceOrder("old-order", limit_price=Decimal("99"),
                               client_order_id="replacement-client-id")
        assert service.execute(command, "replace-1").status == MutationStatus.UNKNOWN
        assert service.execute(CancelOrder("other-order"), "cancel-2").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery, None,
                            replacement(OrderStatus.WORKING, "wrong-id"),
                            replacement(OrderStatus.WORKING, "replacement-client-id", "98"),
                            replacement(OrderStatus.PENDING, "replacement-client-id"),
                            replacement(OrderStatus.WORKING, "replacement-client-id"))
        for _ in range(4):
            assert service.reconcile_replace("replace-1").status == MutationStatus.UNKNOWN
        final = service.reconcile_replace("replace-1")
        assert final.status == MutationStatus.ACCEPTED and final.order_id == "new-order"
        assert store.open_mutations() == []
        assert len(adapter.commands) == 1
    finally:
        store.close()


def test_stop_replacement_waits_for_exact_reviewed_stop(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "new-stop"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    def replacement(stop):
        order = BrokerOrder("new-stop", OrderStatus.WORKING, OrderType.STOP,
                            TimeInForce.GTC,
                            (OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL,
                                      Decimal(2)),), OrderRole.STOP,
                            client_order_id="et-exit-test", stop_price=Decimal(stop))
        return OrderGroup("oco", OrderClass.OCO, OrderStatus.WORKING, (order,))
    try:
        command = ReplaceOrder("old-stop", stop_price=Decimal(97),
                               client_order_id="et-exit-test")
        assert service.execute(command, "replace-stop").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery, replacement(96), replacement(97))
        assert service.reconcile_replace("replace-stop").status == MutationStatus.UNKNOWN
        assert service.reconcile_replace("replace-stop").status == MutationStatus.ACCEPTED
    finally:
        store.close()


def test_vertical_target_replacement_reconciles_only_same_atomic_legs(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "new-target"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    long = OrderLeg("AAPL261113C00100000", AssetType.OPTION,
                    OrderInstruction.SELL_TO_CLOSE, Decimal(2))
    short = OrderLeg("AAPL261113C00105000", AssetType.OPTION,
                     OrderInstruction.BUY_TO_CLOSE, Decimal(2))
    original = BrokerOrder("old-target", OrderStatus.REPLACED, OrderType.LIMIT,
                           TimeInForce.GTC, (long, short), OrderRole.TARGET,
                           limit_price=Decimal("-1.30"))
    old_group = OrderGroup("old-target", OrderClass.MULTI_LEG,
                           OrderStatus.REPLACED, (original,))
    def replacement(second):
        order = BrokerOrder("new-target", OrderStatus.WORKING, OrderType.LIMIT,
                            TimeInForce.GTC, (long, second), OrderRole.TARGET,
                            client_order_id="et-vertical-target-r-test",
                            limit_price=Decimal("-1.40"))
        return OrderGroup("new-target", OrderClass.MULTI_LEG,
                          OrderStatus.WORKING, (order,))
    try:
        command = ReplaceOrder("old-target", limit_price=Decimal("-1.40"),
                               client_order_id="et-vertical-target-r-test")
        assert service.execute(command, "vertical-replace").status == MutationStatus.UNKNOWN
        wrong = OrderLeg("AAPL261113C00110000", AssetType.OPTION,
                         OrderInstruction.BUY_TO_CLOSE, Decimal(2))
        adapter.given_query(ClientOrderQuery, replacement(wrong), replacement(short))
        adapter.given_query(OrderQuery, old_group, old_group)
        assert service.reconcile_replace("vertical-replace").status == MutationStatus.UNKNOWN
        assert service.reconcile_replace("vertical-replace").status == MutationStatus.ACCEPTED
        assert store.open_mutations() == []
    finally:
        store.close()


def test_vertical_submit_reconciliation_requires_exact_atomic_pair(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_query(MarketSessionsQuery, SESSION)
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.UNKNOWN,
                                                     message="timeout"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    service.arm(True)
    legs = (OrderLegIntent("AAPL261113C00100000", AssetType.OPTION,
                           OrderInstruction.BUY_TO_OPEN, Decimal(2)),
            OrderLegIntent("AAPL261113C00105000", AssetType.OPTION,
                           OrderInstruction.SELL_TO_OPEN, Decimal(2)))
    intent = MultiLegOrderIntent(legs, OrderType.LIMIT, TimeInForce.DAY,
                                 "et-vertical-test", Decimal("1.30"))
    def group(symbol):
        order = BrokerOrder("mleg-order", OrderStatus.WORKING, OrderType.LIMIT,
                            TimeInForce.DAY,
                            (OrderLeg(legs[0].symbol, AssetType.OPTION,
                                      OrderInstruction.BUY_TO_OPEN, Decimal(2)),
                             OrderLeg(symbol, AssetType.OPTION,
                                      OrderInstruction.SELL_TO_OPEN, Decimal(2))),
                            OrderRole.ENTRY, client_order_id="et-vertical-test",
                            limit_price=Decimal("1.30"))
        return OrderGroup("mleg-order", OrderClass.MULTI_LEG,
                          OrderStatus.WORKING, (order,))
    try:
        assert service.execute(SubmitOrder(intent), "vertical-submit").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery,
                            group("AAPL261113C00110000"), group(legs[1].symbol))
        assert service.reconcile_submit("vertical-submit").status == MutationStatus.UNKNOWN
        assert service.reconcile_submit("vertical-submit").status == MutationStatus.ACCEPTED
    finally:
        store.close()


def test_auto_be_replacement_reconciliation_requires_paired_equity_target(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "replacement"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, Decimal(2))
    new_stop = BrokerOrder("new-stop", OrderStatus.WORKING, OrderType.STOP,
                           TimeInForce.GTC, (leg,), OrderRole.STOP,
                           client_order_id="et-autobe-test", stop_price=Decimal(100))
    bad_target = BrokerOrder("target", OrderStatus.WORKING, OrderType.LIMIT,
                             TimeInForce.GTC,
                             (OrderLeg("AAPL", AssetType.EQUITY,
                                       OrderInstruction.SELL, Decimal(3)),),
                             OrderRole.TARGET, limit_price=Decimal(110))
    good_target = BrokerOrder("target", OrderStatus.WORKING, OrderType.LIMIT,
                              TimeInForce.GTC, (leg,), OrderRole.TARGET,
                              limit_price=Decimal(110))
    try:
        command = ReplaceOrder("old-stop", stop_price=Decimal(100),
                               client_order_id="et-autobe-test")
        assert service.execute(command, "be-replace").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery,
                            OrderGroup("oco", OrderClass.OCO, OrderStatus.WORKING,
                                       (bad_target, new_stop)),
                            OrderGroup("oco", OrderClass.OCO, OrderStatus.WORKING,
                                       (good_target, new_stop)))
        assert service.reconcile_replace("be-replace").status == MutationStatus.UNKNOWN
        assert service.reconcile_replace("be-replace").status == MutationStatus.ACCEPTED
        assert store.open_mutations() == []
    finally:
        store.close()


def test_auto_be_bracket_reconciliation_requires_filled_entry_and_both_exits(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "replacement"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    entry_leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.BUY,
                         Decimal(2), filled_quantity=Decimal(2))
    exit_leg = OrderLeg("AAPL", AssetType.EQUITY, OrderInstruction.SELL, Decimal(2))
    def group(entry_status):
        entry = BrokerOrder("entry", entry_status, OrderType.LIMIT,
                            TimeInForce.DAY, (entry_leg,), OrderRole.ENTRY)
        target = BrokerOrder("target", OrderStatus.WORKING, OrderType.LIMIT,
                             TimeInForce.DAY, (exit_leg,), OrderRole.TARGET,
                             limit_price=Decimal(110))
        stop = BrokerOrder("new-stop", OrderStatus.WORKING, OrderType.STOP,
                           TimeInForce.DAY, (exit_leg,), OrderRole.STOP,
                           client_order_id="et-autobe-bracket-test", stop_price=Decimal(100))
        return OrderGroup("entry", OrderClass.BRACKET, entry_status,
                          (entry, target, stop))
    try:
        command = ReplaceOrder("old-stop", stop_price=Decimal(100),
                               client_order_id="et-autobe-bracket-test")
        assert service.execute(command, "be-bracket").status == MutationStatus.UNKNOWN
        adapter.given_query(ClientOrderQuery, group(OrderStatus.PARTIALLY_FILLED),
                            group(OrderStatus.FILLED))
        assert service.reconcile_replace("be-bracket").status == MutationStatus.UNKNOWN
        assert service.reconcile_replace("be-bracket").status == MutationStatus.ACCEPTED
    finally:
        store.close()


@pytest.mark.parametrize("entry_instruction,exit_instruction", [
    (OrderInstruction.BUY, OrderInstruction.SELL),
    (OrderInstruction.SELL_SHORT, OrderInstruction.BUY_TO_COVER),
])
def test_auto_be_reconcile_recovers_complete_bracket_from_singleton_stop(
        tmp_path, entry_instruction, exit_instruction):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery, ACCOUNT, ACCOUNT)
    adapter.given_command(ReplaceOrder, MutationResult(MutationStatus.UNKNOWN, "replacement"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: NOW)
    entry_leg = OrderLeg("AAPL", AssetType.EQUITY, entry_instruction,
                         Decimal(1), filled_quantity=Decimal(1))
    exit_leg = OrderLeg("AAPL", AssetType.EQUITY, exit_instruction, Decimal(1))
    entry = BrokerOrder("entry", OrderStatus.FILLED, OrderType.MARKET,
                        TimeInForce.DAY, (entry_leg,), OrderRole.ENTRY)
    target = BrokerOrder("target", OrderStatus.WORKING, OrderType.LIMIT,
                         TimeInForce.DAY, (exit_leg,), OrderRole.TARGET,
                         limit_price=Decimal(120))
    stop = BrokerOrder("new-stop", OrderStatus.WORKING, OrderType.STOP,
                       TimeInForce.DAY, (exit_leg,), OrderRole.STOP,
                       client_order_id="et-autobe-singleton", stop_price=Decimal(100))
    singleton = OrderGroup("entry", OrderClass.BRACKET, OrderStatus.WORKING, (stop,))
    complete = OrderGroup("entry", OrderClass.BRACKET, OrderStatus.FILLED,
                          (entry, target, stop))
    try:
        service.execute(ReplaceOrder("old-stop", stop_price=Decimal(100),
                                     client_order_id="et-autobe-singleton"), "be-singleton")
        adapter.given_query(ClientOrderQuery, singleton)
        adapter.given_query(OrdersQuery, (singleton,), (complete,))
        assert service.reconcile_replace("be-singleton").status == MutationStatus.ACCEPTED
        assert store.open_mutations() == []
    finally:
        store.close()
