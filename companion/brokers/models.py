from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .errors import UnsupportedBrokerOperation

BrokerOrderId: TypeAlias = str
BrokerActivityId: TypeAlias = str


class BrokerMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    DEMO = "demo"


class BrokerCapability(str, Enum):
    ACCOUNT_READ = "account-read"
    ORDER_READ = "order-read"
    CLIENT_ORDER_LOOKUP = "client-order-lookup"
    FILL_READ = "fill-read"
    EQUITY_MARKET_DATA = "equity-market-data"
    OPTION_MARKET_DATA = "option-market-data"
    MARKET_CALENDAR = "market-calendar"
    MARKET_CLOCK = "market-clock"
    EQUITY_TRADING = "equity-trading"
    OPTION_TRADING = "option-trading"
    MULTI_LEG_OPTIONS = "multi-leg-options"
    ORDER_MANAGEMENT = "order-management"
    EQUITY_BRACKET = "equity-bracket"
    OPTION_BRACKET = "option-bracket"
    EQUITY_OCO = "equity-oco"
    OPTION_OCO = "option-oco"
    EQUITY_TRAILING_STOP = "equity-trailing-stop"
    OPTION_TRAILING_STOP = "option-trailing-stop"
    OPTION_STOP = "option-stop"
    ORDER_PREVIEW = "order-preview"


@dataclass(frozen=True)
class BrokerCapabilities:
    supported: frozenset[BrokerCapability]
    unsupported: Mapping[BrokerCapability, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        overlap = self.supported & self.unsupported.keys()
        if overlap:
            raise ValueError(f"capabilities cannot be both supported and unsupported: {sorted(overlap)}")
        missing = set(BrokerCapability) - self.supported - self.unsupported.keys()
        if missing:
            raise ValueError(f"capability matrix is incomplete: {sorted(c.value for c in missing)}")
        object.__setattr__(self, "unsupported", MappingProxyType(dict(self.unsupported)))

    def supports(self, capability: BrokerCapability) -> bool:
        return capability in self.supported

    def require(self, broker_name: str, capability: BrokerCapability) -> None:
        if capability not in self.supported:
            raise UnsupportedBrokerOperation(
                broker_name,
                capability.value,
                self.unsupported[capability],
            )


@dataclass(frozen=True)
class BrokerIdentity:
    key: str
    display_name: str
    mode: BrokerMode
    capabilities: BrokerCapabilities


class AssetType(str, Enum):
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"
    CRYPTO = "crypto"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OrderInstruction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell-short"
    BUY_TO_COVER = "buy-to-cover"
    BUY_TO_OPEN = "buy-to-open"
    SELL_TO_OPEN = "sell-to-open"
    BUY_TO_CLOSE = "buy-to-close"
    SELL_TO_CLOSE = "sell-to-close"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop-limit"
    TRAILING_STOP = "trailing-stop"


class OrderClass(str, Enum):
    SIMPLE = "simple"
    BRACKET = "bracket"
    OCO = "oco"
    OTO = "oto"
    MULTI_LEG = "multi-leg"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"
    CLS = "cls"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    PARTIALLY_FILLED = "partially-filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REPLACED = "replaced"
    UNKNOWN = "unknown"


class OrderRole(str, Enum):
    ENTRY = "entry"
    TARGET = "target"
    STOP = "stop"
    EXIT = "exit"


@dataclass(frozen=True)
class BalanceSnapshot:
    liquidation_value: Decimal | None = None
    equity: Decimal | None = None
    buying_power: Decimal | None = None
    day_trading_buying_power: Decimal | None = None
    available_funds: Decimal | None = None
    maintenance_requirement: Decimal | None = None


@dataclass(frozen=True)
class Position:
    symbol: str
    asset_type: AssetType
    side: PositionSide
    quantity: Decimal
    average_price: Decimal | None
    underlying: str | None = None
    previous_session_quantity: Decimal | None = None
    current_day_cost: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("position quantity must be positive; side carries direction")


@dataclass(frozen=True)
class AccountSnapshot:
    account_ref: str
    status: str
    balances: BalanceSnapshot
    positions: tuple[Position, ...] = ()
    observed_at: datetime | None = None


@dataclass(frozen=True)
class OrderLeg:
    symbol: str
    asset_type: AssetType
    instruction: OrderInstruction
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    role: OrderRole = OrderRole.ENTRY
    underlying: str | None = None
    leg_ref: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order leg quantity must be positive")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled quantity must be between zero and quantity")


@dataclass(frozen=True)
class BrokerOrder:
    order_id: BrokerOrderId
    status: OrderStatus
    order_type: OrderType
    time_in_force: TimeInForce
    legs: tuple[OrderLeg, ...]
    role: OrderRole
    group_id: str | None = None
    parent_id: BrokerOrderId | None = None
    client_order_id: str | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None
    entered_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class OrderGroup:
    group_id: str
    order_class: OrderClass
    status: OrderStatus
    orders: tuple[BrokerOrder, ...]


@dataclass(frozen=True)
class Fill:
    activity_id: BrokerActivityId
    order_id: BrokerOrderId | None
    symbol: str
    asset_type: AssetType
    instruction: OrderInstruction
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    fees: Decimal = Decimal("0")


@dataclass(frozen=True)
class OptionGreeks:
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    mark: Decimal | None
    observed_at: datetime
    realtime: bool
    source: str
    volume: Decimal | None = None
    greeks: OptionGreeks | None = None


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiration: date
    strike: Decimal
    option_type: OptionType
    multiplier: Decimal = Decimal("100")
    tradable: bool = True
    quote: Quote | None = None
    open_interest: Decimal | None = None
    implied_volatility: Decimal | None = None


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    contracts: tuple[OptionContract, ...]
    observed_at: datetime


@dataclass(frozen=True)
class MarketSession:
    session_date: date
    opens_at: datetime | None
    closes_at: datetime | None
    is_open: bool

    def __post_init__(self) -> None:
        if self.is_open and (self.opens_at is None or self.closes_at is None):
            raise ValueError("an open session requires open and close timestamps")


@dataclass(frozen=True)
class MarketClock:
    observed_at: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


# Read queries. Adapters translate these requests and return the corresponding canonical model.
@dataclass(frozen=True)
class AccountQuery:
    pass


@dataclass(frozen=True)
class OrdersQuery:
    start: datetime
    end: datetime
    statuses: frozenset[OrderStatus] = frozenset()


@dataclass(frozen=True)
class OrderQuery:
    order_id: BrokerOrderId


@dataclass(frozen=True)
class ClientOrderQuery:
    client_order_id: str


@dataclass(frozen=True)
class FillsQuery:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class QuotesQuery:
    symbols: tuple[str, ...]
    asset_type: AssetType

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("quotes query requires at least one symbol")


@dataclass(frozen=True)
class MarketSessionsQuery:
    start: date
    end: date


@dataclass(frozen=True)
class MarketClockQuery:
    pass


@dataclass(frozen=True)
class OptionChainQuery:
    underlying: str
    expiration: date | None = None
    option_type: OptionType | None = None


BrokerQuery: TypeAlias = (
    AccountQuery
    | OrdersQuery
    | OrderQuery
    | ClientOrderQuery
    | FillsQuery
    | QuotesQuery
    | MarketSessionsQuery
    | MarketClockQuery
    | OptionChainQuery
)
BrokerQueryResult: TypeAlias = (
    AccountSnapshot
    | tuple[OrderGroup, ...]
    | OrderGroup
    | None
    | tuple[Fill, ...]
    | Mapping[str, Quote]
    | tuple[MarketSession, ...]
    | MarketClock
    | OptionChain
)


@dataclass(frozen=True)
class OrderLegIntent:
    symbol: str
    asset_type: AssetType
    instruction: OrderInstruction
    quantity: Decimal
    ratio_quantity: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.ratio_quantity <= 0:
            raise ValueError("order quantities must be positive")


@dataclass(frozen=True)
class SimpleOrderIntent:
    leg: OrderLegIntent
    order_type: OrderType
    time_in_force: TimeInForce
    client_order_id: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError("limit and stop-limit orders require a limit price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError("stop and stop-limit orders require a stop price")
        if self.order_type == OrderType.TRAILING_STOP:
            raise ValueError("use TrailingOrderIntent for a trailing stop")


@dataclass(frozen=True)
class StopLossIntent:
    stop_price: Decimal
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class BracketOrderIntent:
    entry: SimpleOrderIntent
    take_profit_price: Decimal
    stop_loss: StopLossIntent


@dataclass(frozen=True)
class OcoOrderIntent:
    leg: OrderLegIntent
    take_profit_price: Decimal
    stop_loss: StopLossIntent
    time_in_force: TimeInForce
    client_order_id: str


@dataclass(frozen=True)
class TrailingOrderIntent:
    leg: OrderLegIntent
    time_in_force: TimeInForce
    client_order_id: str
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None

    def __post_init__(self) -> None:
        if (self.trail_price is None) == (self.trail_percent is None):
            raise ValueError("set exactly one of trail_price or trail_percent")


@dataclass(frozen=True)
class MultiLegOrderIntent:
    legs: tuple[OrderLegIntent, ...]
    order_type: OrderType
    time_in_force: TimeInForce
    client_order_id: str
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not 2 <= len(self.legs) <= 4:
            raise ValueError("a multi-leg option order requires two to four legs")
        if any(leg.asset_type != AssetType.OPTION for leg in self.legs):
            raise ValueError("multi-leg intents currently accept option legs only")
        if self.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("multi-leg intents support market or limit order types")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("a multi-leg limit order requires a limit price")


OrderIntent: TypeAlias = (
    SimpleOrderIntent | BracketOrderIntent | OcoOrderIntent | TrailingOrderIntent | MultiLegOrderIntent
)


@dataclass(frozen=True)
class SubmitOrder:
    intent: OrderIntent


@dataclass(frozen=True)
class CancelOrder:
    order_id: BrokerOrderId


@dataclass(frozen=True)
class ReplaceOrder:
    order_id: BrokerOrderId
    replacement: OrderIntent | None = None
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail: Decimal | None = None

    def __post_init__(self) -> None:
        if self.replacement is not None and any(
                v is not None for v in (self.quantity, self.limit_price, self.stop_price, self.trail)):
            raise ValueError("replace order cannot mix a complete plan with partial fields")
        if self.replacement is None and all(
                v is None for v in (self.quantity, self.limit_price, self.stop_price, self.trail)):
            raise ValueError("replace order requires at least one change")


BrokerCommand: TypeAlias = SubmitOrder | CancelOrder | ReplaceOrder


class MutationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RATE_LIMITED = "rate-limited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MutationResult:
    status: MutationStatus
    order_id: BrokerOrderId | None = None
    message: str | None = None
    retry_after_seconds: float | None = None

    @property
    def accepted(self) -> bool:
        return self.status == MutationStatus.ACCEPTED
