from __future__ import annotations


class BrokerError(RuntimeError):
    """Base error raised at the broker seam."""


class UnsupportedBrokerOperation(BrokerError):
    """Raised before an adapter call when the selected broker lacks a capability."""

    def __init__(self, broker: str, capability: str, reason: str):
        self.broker = broker
        self.capability = capability
        self.reason = reason
        super().__init__(f"{broker} does not support {capability}: {reason}")


class MissingFakeResponse(BrokerError):
    """The in-memory adapter was called without a configured result."""


def read_health(error: Exception) -> dict:
    """Translate a broker read failure without exposing SDK payloads or credentials."""
    code = getattr(error, "status_code", None)
    if code == 429:
        response = getattr(getattr(error, "_http_error", None), "response", None)
        raw = response.headers.get("Retry-After") if response is not None else None
        try:
            retry = max(1.0, min(float(raw), 300.0)) if raw is not None else 60.0
        except (TypeError, ValueError):
            retry = 60.0
        return {"state": "rate-limited", "message": "Alpaca Paper rate limit; waiting before retry",
                "retry_after_seconds": retry}
    if code in (401, 403):
        return {"state": "auth-error", "message": "Alpaca Paper rejected the paper credentials",
                "retry_after_seconds": None}
    if isinstance(error, ValueError) and str(error).startswith((
            "Alpaca Paper credentials are missing", "Alpaca Paper endpoint must be")):
        return {"state": "configuration-error", "message": str(error),
                "retry_after_seconds": None}
    if isinstance(error, (TimeoutError, ConnectionError)) or code == 408 or \
            isinstance(code, int) and code >= 500:
        return {"state": "unavailable", "message": "Alpaca Paper connection is unavailable",
                "retry_after_seconds": None}
    return {"state": "stale", "message": "Alpaca Paper read failed; check connection",
            "retry_after_seconds": None}
