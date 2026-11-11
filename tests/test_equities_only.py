from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (AccountQuery, AccountSnapshot, AssetType,
                                      BalanceSnapshot, MarketSession, MarketSessionsQuery,
                                      MutationResult, MutationStatus, OrderInstruction,
                                      OrderLegIntent, OrderType, SimpleOrderIntent,
                                      SubmitOrder, TimeInForce)
from companion.brokers.order_service import BrokerOrderService
from companion.store import Store


def test_option_endpoints_and_schwab_option_ticket_are_blocked():
    import companion.app as appmod

    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    assert "option-trading" not in appmod.broker_info("alpaca-paper")["capabilities"]
    assert "option-trading" not in appmod.broker_info("schwab")["capabilities"]
    for path in ("/api/paper/options/long/submit", "/api/paper/vertical/entry/submit",
                 "/api/spread/stop", "/api/ticket/submit", "/api/position/close"):
        body = {"symbol": "SPY   261120C00600000"}
        response = client.post(path, json=body, headers={"X-Companion": "1"})
        assert response.status_code == 403
        assert "Option trading is disabled" in response.json()["error"]


def test_paper_order_service_rejects_option_intent_before_broker_call(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    service = BrokerOrderService(adapter, store, "paper-account", options_trading_enabled=False)
    intent = SimpleOrderIntent(
        OrderLegIntent("SPY   261120C00600000", AssetType.OPTION,
                       OrderInstruction.BUY_TO_OPEN, Decimal(1)),
        OrderType.LIMIT, TimeInForce.DAY, "option-client", limit_price=Decimal("1.00"))
    try:
        result = service.execute(SubmitOrder(intent), "option-request")
        assert result.status == MutationStatus.REJECTED
        assert adapter.commands == []
    finally:
        store.close()


def test_equities_only_service_can_submit_an_equity_order(tmp_path):
    now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(AccountQuery,
                        AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot()))
    adapter.given_query(MarketSessionsQuery, (
        MarketSession(date(2026, 11, 11),
                      datetime(2026, 11, 11, 14, 30, tzinfo=timezone.utc),
                      datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "order-id"))
    service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now,
                                 options_trading_enabled=False)
    intent = SimpleOrderIntent(
        OrderLegIntent("AAPL", AssetType.EQUITY, OrderInstruction.BUY, Decimal(1)),
        OrderType.LIMIT, TimeInForce.DAY, "equity-client", limit_price=Decimal(100))
    try:
        service.arm(True)
        result = service.execute(SubmitOrder(intent), "equity-request")
        assert result.status == MutationStatus.ACCEPTED
        assert len(adapter.commands) == 1
    finally:
        store.close()


def test_paper_background_spread_watch_cannot_send():
    poller = AlpacaBootstrapPoller()
    try:
        poller.spread_stops = object()
        assert poller._spread_stop_rows(None, (), None, None, None) == []
        poller._send_spread_stop({"stop_id": "old-watch"})
    finally:
        poller.store.close()
