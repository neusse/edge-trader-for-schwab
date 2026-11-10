import pytest

from companion.brokers.configuration import (
    BrokerSelection,
    paper_credentials,
    paper_trading_client,
)


def test_selection_defaults_to_alpaca_and_persists(tmp_path):
    path = tmp_path / "selection.json"
    selection = BrokerSelection(path)
    assert selection.get() == "alpaca-paper"
    selection.set("schwab")
    assert BrokerSelection(path).get() == "schwab"
    selection.set("demo")
    assert BrokerSelection(path).get() == "demo"
    with pytest.raises(ValueError, match="unknown broker"):
        selection.set("alpaca-live")


def test_paper_credentials_prefer_mode_specific_values_and_reject_live_endpoint():
    env = {"alpaca_paper_api_key": "paper-key", "alpaca_paper_secret": "paper-secret",
           "ALPACA_API_KEY": "other-key", "ALPACA_SECRET_KEY": "other-secret",
           "alpaca_paper_endpoint": "https://paper-api.alpaca.markets"}
    assert paper_credentials(env).api_key == "paper-key"
    with pytest.raises(ValueError, match="Alpaca Paper endpoint"):
        paper_credentials({**env, "alpaca_paper_endpoint": "https://api.alpaca.markets"})
    with pytest.raises(ValueError, match="credentials are missing"):
        paper_credentials({})
    with pytest.raises(ValueError, match="credentials are missing"):
        paper_credentials({"ALPACA_API_KEY": "live-key", "ALPACA_SECRET_KEY": "live-secret"})


def test_client_constructor_is_fixed_to_paper(monkeypatch):
    import alpaca.trading.client

    seen = {}

    def capture(key, secret, *, paper):
        seen.update(key=key, secret=secret, paper=paper)
        return object()

    monkeypatch.setattr(alpaca.trading.client, "TradingClient", capture)
    paper_trading_client({"alpaca_paper_api_key": "key", "alpaca_paper_secret": "secret"})
    assert seen == {"key": "key", "secret": "secret", "paper": True}
