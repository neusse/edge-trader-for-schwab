from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.fake import FakeBrokerAdapter
from companion.brokers.models import (
    AccountQuery, AccountSnapshot, BalanceSnapshot, MarketSession,
    MarketSessionsQuery, MutationResult, MutationStatus, OrdersQuery, Quote, QuotesQuery,
    SubmitOrder,
)
from companion.brokers.order_service import BrokerOrderService
from companion.brokers.paper_equity import entry_plan
from companion.store import Store


def quote(now, symbol="AAPL"):
    return Quote(symbol, Decimal("99.95"), Decimal("100.05"), Decimal("100"),
                 Decimal("100"), now, True, "Alpaca IEX")


def entry(**changes):
    body = {"symbol": "AAPL", "side": "long", "quantity": "2", "order_type": "limit",
            "limit_price": "100", "target_price": "110", "stop_price": "95",
            "request_id": str(uuid4())}
    body.update(changes)
    return body


def test_entry_plan_checks_bracket_direction_and_quote_freshness():
    now = datetime.now(timezone.utc)
    plan = entry_plan(entry(), quote(now), now)
    assert plan.entry.client_order_id.startswith("et-")
    assert plan.entry.leg.quantity == 2
    assert plan.take_profit_price == 110
    short = entry_plan(entry(side="short", target_price="90", stop_price="105"), quote(now), now)
    assert short.entry.leg.instruction.value == "sell-short"
    with pytest.raises(ValueError, match="stop < entry < target"):
        entry_plan(entry(stop_price="101"), quote(now), now)
    with pytest.raises(ValueError, match="stale"):
        entry_plan(entry(), quote(now - timedelta(seconds=31)), now)
    with pytest.raises(ValueError, match="whole shares"):
        entry_plan(entry(quantity="1.5"), quote(now), now)
    with pytest.raises(ValueError, match="positive number"):
        entry_plan(entry(target_price="NaN"), quote(now), now)


def test_paper_entry_route_arms_previews_sends_once_and_disarms_on_selection(monkeypatch, tmp_path):
    import companion.app as appmod

    now = datetime.now(timezone.utc)
    adapter = FakeBrokerAdapter(ALPACA_PAPER)
    adapter.given_query(QuotesQuery, *({"AAPL": quote(now)} for _ in range(3)))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot())
    adapter.given_query(AccountQuery, account, account, account, account)
    adapter.given_query(OrdersQuery, (), ())
    session_day = now.astimezone(timezone(timedelta(hours=-5))).date()
    adapter.given_query(MarketSessionsQuery, (MarketSession(
        session_day, now - timedelta(hours=1), now + timedelta(hours=1), True),))
    adapter.given_command(SubmitOrder, MutationResult(MutationStatus.ACCEPTED, "order-1"))
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.order_service = BrokerOrderService(adapter, store, "paper-account", clock=lambda: now)
    poller._state = {"broker_ready": True, "alpaca": {"account_status": "ACTIVE",
                     "updated_at": now.isoformat(), "positions": [], "orders": []}}
    class Selection:
        selected = "alpaca-paper"
        def get(self): return self.selected
        def set(self, key): self.selected = key
    selected = Selection()
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "poller", poller)
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selected)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    headers = {"X-Companion": "1"}
    body = entry()
    try:
        assert client.post("/api/paper/entry/preview", json=body, headers=headers).json()["ok"]
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).json()["status"] == "rejected"
        assert client.post("/api/paper/arm", json={"armed": True}, headers=headers).json()["armed"]
        result = client.post("/api/paper/entry/submit", json=body, headers=headers).json()
        assert result["status"] == "accepted" and result["order_id"] == "order-1"
        assert len(adapter.commands) == 1
        assert client.post("/api/brokers/select", json={"broker": "schwab"}, headers=headers).json()["restart_required"]
        assert not poller.order_service.armed
        assert client.post("/api/paper/entry/submit", json=body, headers=headers).status_code == 409
    finally:
        store.close()
        poller.store.close()
