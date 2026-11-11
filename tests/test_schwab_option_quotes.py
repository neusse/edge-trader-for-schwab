"""Read-only Paper option prices come from Schwab without gating Paper equities."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.capabilities import ALPACA_PAPER
from companion.brokers.models import (AccountQuery, AccountSnapshot, AssetType,
                                      BalanceSnapshot, FillsQuery, MarketClock,
                                      MarketClockQuery, MarketSessionsQuery,
                                      OrdersQuery, Position, PositionSide, Quote,
                                      QuotesQuery)
from companion.brokers.schwab_option_quotes import (SchwabOptionQuoteReader,
                                                     schwab_occ_symbol)
from companion.store import Store


def test_schwab_option_reader_batches_and_maps_compact_occ_symbols():
    first = "AAPL261113C00100000"
    second = "SPY261113P00600000"
    seen = []
    now = datetime.now(timezone.utc)

    class Client:
        def quotes(self, symbols):
            seen.append(symbols)
            return SimpleNamespace(status_code=200, json=lambda: {
                "AAPL  261113C00100000": {"assetMainType": "OPTION", "realtime": True, "quote": {
                    "bidPrice": 1.10, "askPrice": 1.20, "quoteTime": int(now.timestamp() * 1000)}},
                "SPY   261113P00600000": {"assetMainType": "OPTION", "realtime": True, "quote": {
                    "bidPrice": 2.10, "askPrice": 2.20}},
            })

    quotes = SchwabOptionQuoteReader(lambda: Client()).query((first, second))
    assert seen == [["AAPL  261113C00100000", "SPY   261113P00600000"]]
    assert set(quotes) == {first, second}
    assert quotes[first].symbol == first and quotes[first].source == "Schwab"
    assert quotes[first].mark == Decimal("1.15") and quotes[first].timestamp_verified
    assert not quotes[second].timestamp_verified and quotes[second].observed_at.year == 1970
    assert schwab_occ_symbol("AAPL  261113C00100000") == "AAPL  261113C00100000"
    with pytest.raises(ValueError, match="invalid OCC"):
        schwab_occ_symbol("AAPL")


def test_schwab_option_reader_uses_quotes_only_client(monkeypatch):
    from companion import schwab

    allowed = []
    monkeypatch.setattr(schwab, "make_client", lambda methods: (
        allowed.append(methods) or SimpleNamespace(quotes=lambda symbols:
            SimpleNamespace(status_code=403, json=lambda: {}))))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        SchwabOptionQuoteReader().query(("AAPL261113C00100000",))
    assert allowed == [frozenset({"quotes"})]


@pytest.mark.parametrize("schwab_available", [True, False])
def test_paper_option_quote_failure_does_not_disarm_equity_runtime(
        monkeypatch, tmp_path, schwab_available):
    original = Store.for_broker
    monkeypatch.setattr(Store, "for_broker", classmethod(
        lambda cls, broker, account: original(broker, account, tmp_path)))
    now = datetime.now(timezone.utc)
    option = "AAPL261113C00100000"
    account = AccountSnapshot("paper-account", "ACTIVE", BalanceSnapshot(), (
        Position("AAPL", AssetType.EQUITY, PositionSide.LONG, Decimal(2), Decimal(100)),
        Position(option, AssetType.OPTION, PositionSide.LONG, Decimal(1), Decimal(1)),
    ))

    class PaperAdapter:
        identity = ALPACA_PAPER
        def query(self, query):
            if isinstance(query, AccountQuery):
                return account
            if isinstance(query, OrdersQuery):
                return ()
            if isinstance(query, FillsQuery):
                return ()
            if isinstance(query, MarketClockQuery):
                return MarketClock(now, False, now + timedelta(days=1),
                                   now + timedelta(days=1, hours=6))
            if isinstance(query, MarketSessionsQuery):
                return ()
            if isinstance(query, QuotesQuery):
                assert query.asset_type == AssetType.EQUITY, "Paper option quote read escaped"
                return {"AAPL": Quote("AAPL", Decimal(100), Decimal(101), None,
                                       Decimal("100.5"), now, True, "Alpaca IEX")}
            raise AssertionError(type(query))

    class OptionReader:
        def query(self, symbols):
            assert symbols == (option,)
            if not schwab_available:
                raise RuntimeError("expired Schwab login")
            return {option: Quote(option, Decimal(1), Decimal(1.2), None,
                                  Decimal(1.1), now, True, "Schwab")}

    poller = AlpacaBootstrapPoller(PaperAdapter(), option_quote_reader=OptionReader())
    try:
        poller.poll_once()
        state = poller.snapshot()
        assert state["broker_ready"] is True
        assert state["option_quote_health"]["state"] == (
            "ready" if schwab_available else "unavailable")
        held = next(p for p in state["positions"] if p["symbol"] == option)
        assert held["quote_source"] == ("Schwab" if schwab_available else None)
        assert state["positions"][0]["symbol"] == "AAPL"
    finally:
        poller.stop()
