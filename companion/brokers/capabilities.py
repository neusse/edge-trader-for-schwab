from __future__ import annotations

from .models import BrokerCapabilities, BrokerCapability, BrokerIdentity, BrokerMode


def _matrix(supported: set[BrokerCapability], reasons: dict[BrokerCapability, str]) -> BrokerCapabilities:
    unsupported = {
        capability: reasons.get(capability, "This broker does not expose this capability.")
        for capability in BrokerCapability
        if capability not in supported
    }
    return BrokerCapabilities(frozenset(supported), unsupported)


_READS = {
    BrokerCapability.ACCOUNT_READ,
    BrokerCapability.ORDER_READ,
    BrokerCapability.FILL_READ,
    BrokerCapability.EQUITY_MARKET_DATA,
    BrokerCapability.OPTION_MARKET_DATA,
    BrokerCapability.MARKET_CALENDAR,
}

SCHWAB = BrokerIdentity(
    key="schwab",
    display_name="Schwab",
    mode=BrokerMode.LIVE,
    capabilities=_matrix(
        _READS
        | {
            BrokerCapability.EQUITY_TRADING,
            BrokerCapability.OPTION_TRADING,
            BrokerCapability.MULTI_LEG_OPTIONS,
            BrokerCapability.ORDER_MANAGEMENT,
            BrokerCapability.EQUITY_BRACKET,
            BrokerCapability.OPTION_BRACKET,
            BrokerCapability.EQUITY_OCO,
            BrokerCapability.OPTION_OCO,
            BrokerCapability.EQUITY_TRAILING_STOP,
            BrokerCapability.OPTION_TRAILING_STOP,
            BrokerCapability.OPTION_STOP,
            BrokerCapability.ORDER_PREVIEW,
        },
        {},
    ),
)

ALPACA_PAPER = BrokerIdentity(
    key="alpaca-paper",
    display_name="Alpaca Paper",
    mode=BrokerMode.PAPER,
    capabilities=_matrix(
        _READS
        | {
            BrokerCapability.CLIENT_ORDER_LOOKUP,
            BrokerCapability.MARKET_CLOCK,
            BrokerCapability.EQUITY_TRADING,
            BrokerCapability.OPTION_TRADING,
            BrokerCapability.MULTI_LEG_OPTIONS,
            BrokerCapability.ORDER_MANAGEMENT,
            BrokerCapability.EQUITY_BRACKET,
            BrokerCapability.EQUITY_OCO,
            BrokerCapability.EQUITY_TRAILING_STOP,
            BrokerCapability.OPTION_STOP,
        },
        {
            BrokerCapability.OPTION_BRACKET: "Alpaca option orders do not support the bracket order class.",
            BrokerCapability.OPTION_OCO: "Alpaca option orders do not support the OCO order class.",
            BrokerCapability.OPTION_TRAILING_STOP: "Alpaca options do not support trailing-stop orders.",
            BrokerCapability.ORDER_PREVIEW: "Alpaca retail paper trading has no order-preview operation.",
        },
    ),
)

DEMO = BrokerIdentity(
    key="demo",
    display_name="Demo",
    mode=BrokerMode.DEMO,
    capabilities=_matrix(
        _READS,
        {capability: "Demo mode never sends or changes orders." for capability in BrokerCapability},
    ),
)

BROKERS = {identity.key: identity for identity in (ALPACA_PAPER, SCHWAB, DEMO)}
