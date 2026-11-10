from types import SimpleNamespace

from companion.brokers.errors import read_health


def test_read_health_classifies_rate_limit_and_redacts_error_body():
    class RateLimited(Exception):
        status_code = 429
        _http_error = SimpleNamespace(response=SimpleNamespace(headers={"Retry-After": "12"}))

    result = read_health(RateLimited("secret response body"))
    assert result == {"state": "rate-limited",
                      "message": "Alpaca Paper rate limit; waiting before retry",
                      "retry_after_seconds": 12.0}
    assert "secret" not in str(result)


def test_read_health_keeps_actionable_missing_credentials():
    result = read_health(ValueError(
        "Alpaca Paper credentials are missing: set alpaca_paper_api_key and alpaca_paper_secret"))
    assert result["state"] == "configuration-error"
    assert "alpaca_paper_api_key" in result["message"]


def test_read_health_classifies_transport_and_server_failures():
    assert read_health(ConnectionError("secret"))["state"] == "unavailable"

    class ServerError(Exception):
        status_code = 500

    assert read_health(ServerError("secret"))["state"] == "unavailable"
