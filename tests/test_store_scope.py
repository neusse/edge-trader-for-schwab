import sqlite3

import pytest

from companion.store import Store


def test_alpaca_account_stores_isolate_uuid_orders_and_tickets(tmp_path):
    a = Store.for_broker("alpaca-paper", "paper-account-a", tmp_path)
    b = Store.for_broker("alpaca-paper", "paper-account-b", tmp_path)
    schwab = Store.for_broker("schwab", "schwab-account", tmp_path)
    try:
        order = {"orderId": "c19e9c0f-4ab1-40ca-a953-7292caee453e", "status": "WORKING"}
        a.save_orders([order])
        b.save_orders([{**order, "status": "FILLED"}])
        a.save_transactions([{"activityId": "2026::opaque-uuid", "time": "2026-11-10T12:00:00Z",
                              "orderId": order["orderId"]}])
        ticket = {"ticket_id": "same-ticket", "symbol": "AAPL", "side": "LONG", "qty": 1,
                  "limit": "10", "stop": "9", "target": "11", "risk": 1, "request": {}}
        assert a.insert_ticket(ticket)
        assert b.insert_ticket(ticket)
        a.update_ticket("same-ticket", "ACCEPTED", order["orderId"])
        assert a.get_ticket("same-ticket")["order_id"] == order["orderId"]
        assert b.get_ticket("same-ticket")["order_id"] is None
        assert a.get_order(order["orderId"])["status"] == "WORKING"
        assert b.get_order(order["orderId"])["status"] == "FILLED"
        assert schwab.get_order(order["orderId"]) is None
        assert a.transactions_since("2026-01-01")[0]["activityId"] == "2026::opaque-uuid"
        assert not b.transactions_since("2026-01-01")
        assert "paper-account" not in a._con.execute("pragma database_list").fetchone()[2]
    finally:
        a.close()
        b.close()
        schwab.close()


def test_schwab_legacy_database_is_preserved_and_bound_to_one_account(tmp_path):
    path = tmp_path / "companion.db"
    legacy = Store(path)
    legacy.save_orders([{"orderId": 42, "status": "WORKING"}])
    legacy.close()
    scoped = Store.for_broker("schwab", "first-account", tmp_path)
    assert scoped.get_order(42)["status"] == "WORKING"
    scoped.close()
    with pytest.raises(ValueError, match="different broker or account"):
        Store.for_broker("schwab", "another-account", tmp_path)
    with sqlite3.connect(path) as con:
        assert con.execute("select order_id from orders").fetchone() == (42,)


def test_scope_rejects_path_injection_and_wrong_metadata(tmp_path):
    with pytest.raises(ValueError, match="invalid broker"):
        Store.for_broker("../../outside", "account", tmp_path)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    path = tmp_path / "manual.db"
    store.close()
    scoped = Store(path, scope=("alpaca-paper", "paper-account"))
    scoped.close()
    with pytest.raises(ValueError, match="different broker or account"):
        Store(path, scope=("schwab", "paper-account"))


def test_existing_schwab_store_binds_after_account_discovery(tmp_path):
    path = tmp_path / "companion.db"
    store = Store(path)
    store.save_orders([{"orderId": 42, "status": "WORKING"}])
    store.bind_scope("schwab", "account-a")
    assert store.scope == ("schwab", "account-a")
    assert store.get_order(42)["status"] == "WORKING"
    with pytest.raises(ValueError, match="different broker or account"):
        store.bind_scope("schwab", "account-b")
    store.close()
    with pytest.raises(ValueError, match="different broker or account"):
        Store(path, scope=("schwab", "account-b"))
