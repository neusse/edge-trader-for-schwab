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
