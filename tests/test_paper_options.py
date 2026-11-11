from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion.brokers.models import (OptionChain, OptionContract, OptionGreeks,
                                      OptionType, Quote)
from companion.brokers.paper_options import chain_query, chain_response


NOW = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
BODY = {"underlying": "AAPL", "expiration": "2026-11-13", "option_type": "call"}


def _chain():
    quote = Quote("AAPL261113C00225000", D("1.20"), D("1.40"), None,
                  D("1.30"), NOW, False, "Alpaca indicative options",
                  greeks=OptionGreeks(delta=D("0.45")))
    contract = OptionContract("AAPL  261113C00225000", "AAPL", date(2026, 11, 13),
                              D("225"), OptionType.CALL, quote=quote,
                              open_interest=D("111"), implied_volatility=D("0.27"))
    return OptionChain("AAPL", (contract,), NOW)


def test_chain_query_requires_exact_near_expiry_and_type():
    query = chain_query(BODY, NOW)
    assert query.underlying == "AAPL" and query.expiration == date(2026, 11, 13)
    assert query.option_type == OptionType.CALL
    for wrong in ({**BODY, "underlying": "aapl"},
                  {**BODY, "expiration": "2027-03-13"},
                  {**BODY, "option_type": "both"}):
        with pytest.raises(ValueError):
            chain_query(wrong, NOW)


def test_chain_response_normalizes_occ_and_labels_indicative_quote():
    result = chain_response(chain_query(BODY, NOW), _chain())
    assert result["source"] == "Alpaca indicative options" and not result["realtime"]
    row = result["contracts"][0]
    assert row["symbol"] == "AAPL261113C00225000"
    assert row["bid"] == "1.20" and row["ask"] == "1.40"
    assert row["delta"] == "0.45" and row["implied_volatility"] == "0.27"


def test_chain_response_refuses_mismatched_terms_or_unexpected_quote():
    query = chain_query(BODY, NOW)
    contract = _chain().contracts[0]
    with pytest.raises(ValueError, match="requested OCC terms"):
        chain_response(query, OptionChain("AAPL", (
            OptionContract(contract.symbol, "AAPL", contract.expiration, D("226"),
                           contract.option_type),), NOW))
    with pytest.raises(ValueError, match="source"):
        wrong = Quote(contract.symbol, D("1"), D("2"), None, D("1.5"), NOW,
                      True, "Alpaca IEX")
        chain_response(query, OptionChain("AAPL", (
            OptionContract(contract.symbol, "AAPL", contract.expiration, contract.strike,
                           contract.option_type, quote=wrong),), NOW))


def test_option_discovery_route_uses_active_paper_adapter_without_mutation(monkeypatch):
    import companion.app as appmod

    observed = []
    adapter = SimpleNamespace(query=lambda query: (observed.append(query), _chain())[1])
    monkeypatch.setattr(appmod, "_paper_service", lambda: (SimpleNamespace(adapter=adapter), {}))
    monkeypatch.setattr("companion.brokers.paper_options.datetime", SimpleNamespace(
        now=lambda _tz: NOW))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    result = client.post("/api/paper/options/chain", json=BODY,
                         headers={"X-Companion": "1"})
    assert result.status_code == 200 and result.json()["ok"]
    assert len(observed) == 1 and observed[0].underlying == "AAPL"
    assert result.json()["contracts"][0]["symbol"] == "AAPL261113C00225000"
