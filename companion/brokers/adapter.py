from __future__ import annotations

from abc import ABC, abstractmethod
from typing import final

from .models import (
    AccountQuery,
    AssetType,
    BracketOrderIntent,
    BrokerCapability,
    BrokerCommand,
    BrokerIdentity,
    BrokerQuery,
    BrokerQueryResult,
    CancelOrder,
    ClientOrderQuery,
    FillsQuery,
    MarketSessionsQuery,
    MarketClockQuery,
    MultiLegOrderIntent,
    MutationResult,
    OcoOrderIntent,
    OptionChainQuery,
    OrderQuery,
    OrdersQuery,
    QuotesQuery,
    ReplaceOrder,
    SimpleOrderIntent,
    SubmitOrder,
    TrailingOrderIntent,
    OrderType,
)


def required_query_capability(query: BrokerQuery) -> BrokerCapability:
    if isinstance(query, AccountQuery):
        return BrokerCapability.ACCOUNT_READ
    if isinstance(query, (OrdersQuery, OrderQuery)):
        return BrokerCapability.ORDER_READ
    if isinstance(query, ClientOrderQuery):
        return BrokerCapability.CLIENT_ORDER_LOOKUP
    if isinstance(query, FillsQuery):
        return BrokerCapability.FILL_READ
    if isinstance(query, QuotesQuery):
        return (
            BrokerCapability.OPTION_MARKET_DATA
            if query.asset_type == AssetType.OPTION
            else BrokerCapability.EQUITY_MARKET_DATA
        )
    if isinstance(query, MarketSessionsQuery):
        return BrokerCapability.MARKET_CALENDAR
    if isinstance(query, MarketClockQuery):
        return BrokerCapability.MARKET_CLOCK
    if isinstance(query, OptionChainQuery):
        return BrokerCapability.OPTION_MARKET_DATA
    raise TypeError(f"unknown broker query: {type(query).__name__}")


def required_command_capability(command: BrokerCommand) -> BrokerCapability:
    if isinstance(command, (CancelOrder, ReplaceOrder)):
        return BrokerCapability.ORDER_MANAGEMENT
    if not isinstance(command, SubmitOrder):
        raise TypeError(f"unknown broker command: {type(command).__name__}")
    intent = command.intent
    if isinstance(intent, MultiLegOrderIntent):
        return BrokerCapability.MULTI_LEG_OPTIONS
    if isinstance(intent, BracketOrderIntent):
        return (
            BrokerCapability.OPTION_BRACKET
            if intent.entry.leg.asset_type == AssetType.OPTION
            else BrokerCapability.EQUITY_BRACKET
        )
    if isinstance(intent, OcoOrderIntent):
        return (
            BrokerCapability.OPTION_OCO
            if intent.leg.asset_type == AssetType.OPTION
            else BrokerCapability.EQUITY_OCO
        )
    if isinstance(intent, TrailingOrderIntent):
        return (
            BrokerCapability.OPTION_TRAILING_STOP
            if intent.leg.asset_type == AssetType.OPTION
            else BrokerCapability.EQUITY_TRAILING_STOP
        )
    if isinstance(intent, SimpleOrderIntent):
        if intent.leg.asset_type == AssetType.OPTION and intent.order_type in (
            OrderType.STOP,
            OrderType.STOP_LIMIT,
        ):
            return BrokerCapability.OPTION_STOP
        return (
            BrokerCapability.OPTION_TRADING
            if intent.leg.asset_type == AssetType.OPTION
            else BrokerCapability.EQUITY_TRADING
        )
    raise TypeError(f"unknown order intent: {type(intent).__name__}")


class BrokerAdapter(ABC):
    """The broker seam used by application callers and adapter contract tests.

    Implementations translate only canonical queries, commands, and results. The public
    methods enforce the selected broker's capability matrix before an implementation can
    make an external call.
    """

    def __init__(self, identity: BrokerIdentity):
        self.identity = identity

    @final
    def query(self, query: BrokerQuery) -> BrokerQueryResult:
        capability = required_query_capability(query)
        self.identity.capabilities.require(self.identity.display_name, capability)
        return self._query(query)

    @final
    def execute(self, command: BrokerCommand) -> MutationResult:
        capability = required_command_capability(command)
        self.identity.capabilities.require(self.identity.display_name, capability)
        return self._execute(command)

    @abstractmethod
    def _query(self, query: BrokerQuery) -> BrokerQueryResult:
        raise NotImplementedError

    @abstractmethod
    def _execute(self, command: BrokerCommand) -> MutationResult:
        raise NotImplementedError
