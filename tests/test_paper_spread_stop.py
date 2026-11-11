from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from threading import RLock
from types import SimpleNamespace

import pytest

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.models import (AccountQuery, AccountSnapshot, AssetType,
                                      BalanceSnapshot, MarketSession, MutationResult,
                                      MutationStatus, OrderInstruction, OrdersQuery,
                                      Position, PositionSide, Quote, QuotesQuery,
                                      SubmitOrder)
from companion.brokers.paper_spread_stop import PaperSpreadStopWatch
from companion.store import Store


NOW = datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc)
LONG = "AAPL261113C00100000"
SHORT = "AAPL261113C00105000"


def quotes(at, *, source="Alpaca OPRA options", verified=True):
    return {
        LONG: Quote(LONG, D("1.90"), D("2.10"), None, D("2.00"), at,
                    source == "Alpaca OPRA options", source, timestamp_verified=verified),
        SHORT: Quote(SHORT, D("0.90"), D("1.10"), None, D("1.00"), at,
                     source == "Alpaca OPRA options", source, timestamp_verified=verified),
    }


def watch(tmp_path, *, send=True, kind="debit", stop="1.10"):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    monitor = PaperSpreadStopWatch(store)
    row = monitor.set(LONG, SHORT, D(2), kind, D("1.30") if kind == "debit" else D("0.70"),
                      D("2.20"), D("0.90") if kind == "debit" else D("1.50"),
                      D(5), D(stop), send, NOW - timedelta(seconds=5))
    return store, monitor, row


def observe(monitor, row, at, **changes):
    args = dict(market_open=True, pair_matches=True, orders_clear=True, monitoring=True)
    args.update(changes)
    return monitor.observe(row["stop_id"], quotes(at), now=at, **args)


def test_paper_stop_requires_two_distinct_fresh_opra_observations(tmp_path):
    store, monitor, row = watch(tmp_path)
    try:
        first = observe(monitor, row, NOW)
        assert first["status"] == "watching" and "1 of 2" in first["reason"]
        repeated = observe(monitor, row, NOW)
        assert repeated["status"] == "watching" and not repeated["fire"]
        fired = observe(monitor, row, NOW + timedelta(seconds=3))
        assert fired["status"] == "sending" and fired["fire"]
        assert monitor.get(row["stop_id"])["status"] == "sending"
        assert not monitor.clear(row["stop_id"])
        monitor.record_send(row["stop_id"], row["request_id"], "unknown", "Check Alpaca")
        assert monitor.get(row["stop_id"])["status"] == "unknown"
        assert not monitor.clear(row["stop_id"])
        assert monitor.acknowledge_unknown(row["stop_id"])
        assert monitor.clear(row["stop_id"])
    finally:
        store.close()


def test_paper_stop_suspends_and_resets_on_indicative_stale_or_changed_pair(tmp_path):
    store, monitor, row = watch(tmp_path)
    try:
        first = observe(monitor, row, NOW)
        assert first["status"] == "watching"
        indicative = monitor.observe(row["stop_id"], quotes(NOW + timedelta(seconds=3),
                                      source="Alpaca indicative options"), now=NOW + timedelta(seconds=3),
                                      market_open=True, pair_matches=True, orders_clear=True,
                                      monitoring=True)
        assert indicative["status"] == "suspended" and "OPRA" in indicative["reason"]
        again = observe(monitor, row, NOW + timedelta(seconds=6))
        assert again["status"] == "watching" and not again["fire"]
        assert observe(monitor, row, NOW + timedelta(seconds=9), pair_matches=False)["status"] == "suspended"
        assert observe(monitor, row, NOW + timedelta(seconds=12), orders_clear=False)["status"] == "suspended"
        assert observe(monitor, row, NOW + timedelta(seconds=15), market_open=False)["status"] == "suspended"
        stale = monitor.observe(row["stop_id"], quotes(NOW), now=NOW + timedelta(seconds=30),
                                market_open=True, pair_matches=True, orders_clear=True,
                                monitoring=True)
        assert stale["status"] == "suspended"
    finally:
        store.close()


def test_watch_only_triggers_without_send_and_restart_marks_interrupted_send_unknown(tmp_path):
    store, monitor, row = watch(tmp_path, send=False)
    try:
        observe(monitor, row, NOW)
        fired = observe(monitor, row, NOW + timedelta(seconds=3))
        assert fired["status"] == "triggered" and not fired["fire"]
        assert monitor.clear(row["stop_id"])
        row = monitor.set(LONG, SHORT, D(2), "debit", D("1.30"), D("2.20"), D("0.90"),
                          D(5), D("1.10"), True,
                          NOW - timedelta(seconds=5))
        observe(monitor, row, NOW)
        assert observe(monitor, row, NOW + timedelta(seconds=3))["status"] == "sending"
        restarted = PaperSpreadStopWatch(store)
        assert restarted.get(row["stop_id"])["status"] == "unknown"
    finally:
        store.close()


def test_unknown_stop_cannot_be_acknowledged_while_order_mutation_is_unresolved(tmp_path):
    store, monitor, row = watch(tmp_path)
    try:
        observe(monitor, row, NOW)
        assert observe(monitor, row, NOW + timedelta(seconds=3))["status"] == "sending"
        assert store.record_mutation(row["request_id"], "SubmitOrder", {},
                                     f"et-vertical-stop-{row['request_id'].replace('-', '')}")
        store.finish_mutation(row["request_id"], "unknown", None)
        restarted = PaperSpreadStopWatch(store)
        assert restarted.get(row["stop_id"])["status"] == "unknown"
        assert not restarted.acknowledge_unknown(row["stop_id"])
        store.finish_mutation(row["request_id"], "accepted", "broker-close")
        restarted.resolve_request(row["request_id"], True, "Broker confirms mleg close")
        assert restarted.get(row["stop_id"])["status"] == "sent"
        assert restarted.clear(row["stop_id"])
    finally:
        store.close()


def test_stop_price_must_reduce_risk_inside_strike_width(tmp_path):
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    try:
        monitor = PaperSpreadStopWatch(store)
        with pytest.raises(ValueError, match="reduce"):
            monitor.set(LONG, SHORT, D(1), "debit", D("1.30"), D("2.20"), D("0.90"),
                        D(5), D("1.40"), True, NOW)
        with pytest.raises(ValueError, match="reduce"):
            monitor.set(LONG, SHORT, D(1), "credit", D("0.70"), D("2.20"), D("1.50"),
                        D(5), D("0.60"), True, NOW)
        with pytest.raises(ValueError, match="reduce"):
            monitor.set(LONG, SHORT, D(1), "credit", D("0.70"), D("2.20"), D("1.50"),
                        D(5), D("5.01"), True, NOW)
    finally:
        store.close()


@pytest.mark.parametrize("recovered", [False, True])
def test_poller_sends_one_atomic_close_only_after_confirmed_opra_hit(tmp_path, recovered):
    now = datetime.now(timezone.utc)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    monitor = PaperSpreadStopWatch(store)
    stop = monitor.set(LONG, SHORT, D(2), "debit", D("1.30"), D("2.20"),
                       D("0.90"), D(5), D("1.10"), True, now - timedelta(seconds=10))
    positions = (Position(LONG, AssetType.OPTION, PositionSide.LONG, D(2), D("2.20")),
                 Position(SHORT, AssetType.OPTION, PositionSide.SHORT, D(2), D("0.90")))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), positions)
    final = quotes(now - timedelta(seconds=1))
    if recovered:
        final[LONG] = Quote(LONG, D("2.40"), D("2.60"), None, D("2.50"),
                            now - timedelta(seconds=1), True,
                            "Alpaca OPRA options", timestamp_verified=True)
    sequence = [quotes(now - timedelta(seconds=4)),
                quotes(now - timedelta(seconds=1)), final]
    commands = []
    def query(value):
        if isinstance(value, QuotesQuery):
            assert value.realtime_required
            return sequence.pop(0)
        if isinstance(value, AccountQuery): return account
        if isinstance(value, OrdersQuery): return ()
        raise AssertionError(type(value))
    def execute(command, request_id, preflight):
        preflight(account)
        commands.append(command)
        return MutationResult(MutationStatus.ACCEPTED, "one-mleg-close")
    adapter = SimpleNamespace(query=query)
    service = SimpleNamespace(adapter=adapter, store=store, account_ref="paper-account",
                              armed=True, execute=execute)
    poller = AlpacaBootstrapPoller(adapter=adapter)
    poller.spread_stops = monitor
    poller.order_service = service
    poller.action_lock = RLock()
    session = MarketSession(now.date(), now - timedelta(hours=1),
                            now + timedelta(hours=1), True)
    clock = SimpleNamespace(is_open=True)
    try:
        first = poller._spread_stop_rows(account, (), clock, session, now)
        assert first[0]["status"] == "watching" and commands == []
        second = poller._spread_stop_rows(account, (), clock, session, now)
        if recovered:
            assert second[0]["status"] == "refused" and commands == []
        else:
            assert second[0]["status"] == "sent" and len(commands) == 1
            assert isinstance(commands[0], SubmitOrder)
            assert commands[0].intent.client_order_id.startswith("et-vertical-stop-")
            assert [leg.instruction for leg in commands[0].intent.legs] == [
                OrderInstruction.SELL_TO_CLOSE, OrderInstruction.BUY_TO_CLOSE]
            assert commands[0].intent.limit_price == D("-0.80")
            assert poller._spread_stop_rows(account, (), clock, session, now)[0]["status"] == "sent"
            assert len(commands) == 1
    finally:
        poller.store.close()
        store.close()


def test_poller_suspends_send_stop_when_opra_is_unavailable(tmp_path):
    now = datetime.now(timezone.utc)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    monitor = PaperSpreadStopWatch(store)
    stop = monitor.set(LONG, SHORT, D(2), "debit", D("1.30"), D("2.20"),
                       D("0.90"), D(5), D("1.10"), True, now - timedelta(seconds=10))
    positions = (Position(LONG, AssetType.OPTION, PositionSide.LONG, D(2), D("2.20")),
                 Position(SHORT, AssetType.OPTION, PositionSide.SHORT, D(2), D("0.90")))
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), positions)
    class Forbidden(Exception):
        status_code = 403
    def query(value):
        if isinstance(value, QuotesQuery): raise Forbidden()
        raise AssertionError(type(value))
    poller = AlpacaBootstrapPoller(adapter=SimpleNamespace(query=query))
    poller.spread_stops = monitor
    poller.order_service = SimpleNamespace(armed=True)
    session = MarketSession(now.date(), now - timedelta(hours=1),
                            now + timedelta(hours=1), True)
    try:
        row = poller._spread_stop_rows(account, (), SimpleNamespace(is_open=True),
                                       session, now)[0]
        assert row["status"] == "suspended" and "OPRA subscription" in row["reason"]
        assert monitor.get(stop["stop_id"])["status"] == "watching"
    finally:
        poller.store.close()
        store.close()
