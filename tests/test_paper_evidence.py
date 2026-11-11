"""The Paper evidence file must preserve order state without disclosing broker IDs."""

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from companion.brokers.models import (AssetType, OrderClass, OrderInstruction,
                                      OrderRole, OrderStatus, PositionSide)
from scripts.capture_paper_evidence import (PreflightRefused, RuntimeMismatch,
                                            collect, ref, require_clean_market,
                                            require_matching_runtime)


def test_capture_redacts_account_order_fill_and_symbol_identifiers():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    symbol = "PRIVATE1"
    position = SimpleNamespace(symbol=symbol, asset_type=AssetType.EQUITY,
                               side=PositionSide.LONG, quantity=Decimal(2))
    account = SimpleNamespace(account_ref="private-account-123", status="ACTIVE",
                              positions=(position,))
    leg = SimpleNamespace(symbol=symbol, asset_type=AssetType.EQUITY,
                          quantity=Decimal(2), filled_quantity=Decimal(0))
    order = SimpleNamespace(order_id="private-order-123", role=OrderRole.STOP,
                            status=OrderStatus.WORKING,
                            client_order_id="et-private-client-123", legs=(leg,))
    group = SimpleNamespace(group_id="private-group-123", order_class=OrderClass.OCO,
                            orders=(order,))
    fill = SimpleNamespace(activity_id="private-fill-123", order_id="private-order-123",
                           symbol=symbol, asset_type=AssetType.EQUITY,
                           instruction=OrderInstruction.BUY, quantity=Decimal(2),
                           occurred_at=now)
    app = {"broker": {"key": "alpaca-paper", "mode": "paper"},
           "broker_ready": True, "market_hours": {"is_open": True},
           "positions": [{"symbol": symbol, "asset_type": "EQUITY", "side": "LONG",
                          "qty": 2, "protection": "FULL", "stop_qty": 2}],
           "orders": [{"order_id": "private-order-123", "symbol": symbol,
                       "asset_type": "EQUITY", "role": "STOP", "status": "WORKING",
                       "group_type": "OCO", "qty": 2, "tag": "et-private-client-123"}]}
    status = {"ok": True, "armed": False,
              "unresolved": [{"request_id": "private-request-123"}]}

    record = collect("oco-working", app, status, account, (group,), (fill,),
                     now=now, revision="test-revision", salt=b"local-test-salt")
    encoded = json.dumps(record)
    for secret in (symbol, "private-account-123", "private-order-123",
                   "private-group-123", "et-private-client-123",
                   "private-fill-123", "private-request-123"):
        assert secret not in encoded
    assert record["paper"]["working_orders"][0]["role"] == "stop"
    assert record["paper"]["fills_today"][0]["quantity"] == "2"
    assert record["app"]["positions"][0]["protection"] == "FULL"


def test_capture_refuses_non_paper_app():
    app = {"broker": {"key": "schwab", "mode": "live"}}
    account = SimpleNamespace(status="ACTIVE")
    with pytest.raises(ValueError, match="Alpaca Paper"):
        collect("baseline", app, {"ok": True}, account, (), (),
                now=datetime.now(timezone.utc), revision="test", salt=b"local-test-salt")


def test_local_salt_keeps_references_stable_only_within_one_evidence_file():
    assert ref("private-order", b"first") == ref("private-order", b"first")
    assert ref("private-order", b"first") != ref("private-order", b"second")


def test_clean_market_preflight_requires_independent_open_paper_clock_and_empty_views():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    clock = SimpleNamespace(is_open=True, observed_at=now - timedelta(seconds=5),
                            next_close=now + timedelta(hours=5))
    record = {"app": {"ready": True, "market_open": True, "armed": False,
                      "unresolved_request_refs": [], "positions": [],
                      "working_orders": []},
              "paper": {"positions": [], "working_orders": [], "fills_today": []}}
    require_clean_market(record, clock, now)
    later_clock = SimpleNamespace(is_open=True,
                                  observed_at=now + timedelta(seconds=2),
                                  next_close=now + timedelta(hours=5))
    # The clock is fetched after the account snapshot; freshness uses a later time.
    require_clean_market(record, later_clock, now + timedelta(seconds=3))
    with pytest.raises(PreflightRefused, match="open session"):
        require_clean_market(record, later_clock, now)
    with pytest.raises(PreflightRefused, match="open session"):
        require_clean_market(record, SimpleNamespace(**{**vars(clock), "is_open": False}), now)
    with pytest.raises(PreflightRefused, match="open session"):
        require_clean_market(record, SimpleNamespace(**{**vars(clock),
                                                      "observed_at": now - timedelta(minutes=2)}), now)
    with pytest.raises(PreflightRefused, match="open session"):
        require_clean_market({**record, "app": {**record["app"], "market_open": False}},
                             clock, now)
    with pytest.raises(PreflightRefused, match="disarmed"):
        require_clean_market({**record, "app": {**record["app"], "armed": True}}, clock, now)
    with pytest.raises(PreflightRefused, match="unresolved"):
        require_clean_market({**record, "app": {**record["app"],
                                                "unresolved_request_refs": ["opaque"]}}, clock, now)
    with pytest.raises(PreflightRefused, match="no positions"):
        require_clean_market({**record, "paper": {**record["paper"],
                                                    "working_orders": [{"order_ref": "opaque"}]}},
                             clock, now)


def test_evidence_refuses_stale_app_before_any_broker_read(monkeypatch):
    import scripts.capture_paper_evidence as capture

    checkout = {"commit": "current-commit", "clean": True}
    assert require_matching_runtime(checkout, checkout) == "current-commit"
    with pytest.raises(RuntimeMismatch, match="clean Git checkout"):
        require_matching_runtime(checkout, {**checkout, "clean": False})
    with pytest.raises(RuntimeMismatch, match="restart Edge Trader"):
        require_matching_runtime({"commit": "old-commit", "clean": True}, checkout)

    monkeypatch.setattr(sys, "argv", ["capture_paper_evidence.py", "baseline"])
    monkeypatch.setattr(capture, "checkout_identity", lambda root: checkout)
    monkeypatch.setattr(capture, "_json", lambda port, path: (
        {"commit": "old-commit", "clean": True} if path == "/api/build" else
        pytest.fail("app state was read after a runtime mismatch")))
    monkeypatch.setattr(capture, "AlpacaPaperAdapter", lambda: pytest.fail(
        "Paper broker was read after a runtime mismatch"))
    with pytest.raises(SystemExit) as refused:
        capture.main()
    assert refused.value.code == 2
