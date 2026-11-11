"""Broker-neutral pending-order rows and stop coverage.

Legacy Schwab callers use compatibility functions that normalize their payloads
through the Schwab adapter before projecting canonical order groups.
"""
from __future__ import annotations

from dataclasses import dataclass

from .brokers.models import OrderClass, OrderGroup, OrderInstruction, OrderRole, OrderStatus

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
# Resting at the broker and able to execute now: the only states that protect a position.
LIVE = {"WORKING", "PARTIALLY_FILLED", "PENDING_ACTIVATION", "ACCEPTED", "QUEUED", "AWAITING_STOP_CONDITION",
        "AWAITING_CONDITION", "AWAITING_RELEASE_TIME", "NEW"}
SELLS = {"SELL", "SELL_TO_CLOSE", "SELL_SHORT", "SELL_TO_OPEN"}
BUYS = {"BUY", "BUY_TO_COVER", "BUY_TO_CLOSE", "BUY_TO_OPEN"}


def cancellable_unfilled_entry(group: OrderGroup) -> tuple[bool, str]:
    """Only a clearly unfilled top-level entry may have its attached exits canceled."""
    if group.order_class not in (OrderClass.SIMPLE, OrderClass.BRACKET) or group.strategy_detail not in ("SINGLE", "TRIGGER"):
        return False, f"not a single entry or bracket ({group.strategy_detail})"
    root = next((order for order in group.orders
                 if order.parent_id is None and order.order_id == group.group_id), None)
    if root is None:
        return False, "order has no readable entry"
    status = root.status_detail or root.status.value.upper().replace("-", "_")
    if status not in LIVE:
        return False, f"not working ({status})"
    if not root.legs:
        return False, "order has no legs"
    opening = {OrderInstruction.SELL_SHORT, OrderInstruction.BUY_TO_OPEN,
               OrderInstruction.SELL_TO_OPEN}
    for leg in root.legs:
        if leg.position_effect == "CLOSING" or (leg.position_effect != "OPENING" and
                                                leg.instruction not in opening):
            return False, "not an entry (it closes or reduces a position)"
    filled = root.reported_filled_quantity
    if filled is None or not filled.is_finite() or filled < 0:
        return False, "broker did not report a fill count, so it cannot be shown unfilled"
    if filled > 0:
        return False, f"already {filled:g} filled: cancel the rest in the broker"
    if root.has_fill_activity:
        return False, "broker shows executions on it"
    if group.has_unreadable_live_exit:
        return False, "a live exit could not be read completely"
    if any(order.role != OrderRole.ENTRY and
           (order.status_detail in LIVE or order.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED))
           for order in group.orders if order.order_id != root.order_id):
        return False, "its stop or target is already live, so part of it filled"
    return True, ""


@dataclass
class PendingOrder:
    order_id: int
    leg_id: int
    legs: int                # legs in this order (>1: a strategy)
    group_id: int            # top-level order id: rows sharing it are one OCO / bracket
    group_type: str          # SINGLE | OCO | TRIGGER | ...
    parent_id: int | None    # direct parent order (TRIGGER / OCO)
    oco_id: int | None       # the OCO container this order is an alternative in, at any depth
    status: str
    symbol: str
    underlying: str
    asset_type: str
    instruction: str         # BUY | SELL | SELL_SHORT | BUY_TO_COVER | BUY_TO_OPEN ...
    effect: str | None       # OPENING | CLOSING | None
    order_type: str          # LIMIT | STOP | STOP_LIMIT | TRAILING_STOP | MARKET | NET_DEBIT ...
    qty: float               # remaining quantity for this leg
    filled: float
    price: float | None      # limit price (net strategy price when legs > 1)
    stop: float | None       # stop trigger; None for trailing stops Schwab has not priced
    duration: str
    entered: str
    tag: str | None
    role: str                # STOP | TARGET | ENTRY | EXIT
    # Schwab-held trailing stop: distance and what it follows (None for every other order)
    trail_offset: float | None = None
    trail_link: str | None = None      # VALUE ($) | PERCENT | TICK
    trail_basis: str | None = None     # MARK | LAST | BID | ASK ...

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def exits(self, side: str) -> bool:
        """Could this leg reduce a LONG/SHORT position (by instruction, not just symbol)?"""
        if self.effect == "OPENING":
            return False
        return self.instruction in (SELLS if side == "LONG" else BUYS) and \
            self.instruction not in ("SELL_SHORT", "SELL_TO_OPEN", "BUY_TO_OPEN")


def terminal_ids(orders: list[dict]) -> set[int]:
    """Compatibility entry point; Schwab parsing lives in its adapter."""
    from .brokers.schwab_adapter import canonical_terminal_ids, normalize_order
    return canonical_terminal_ids(tuple(normalize_order(row) for row in orders))


def pending_orders(orders: list[dict], known_terminal: set[int] | None = None) -> list[PendingOrder]:
    from .brokers.canonical_view import _pending
    from .brokers.schwab_adapter import normalize_order
    from dataclasses import replace
    groups = tuple(normalize_order(row) for row in orders)
    rows = _pending(groups, {str(value) for value in known_terminal or ()})
    # Trader still stores Schwab numeric ids. Canonical rows remain string keyed.
    return [replace(row, order_id=int(row.order_id), leg_id=int(row.leg_id),
                    group_id=int(row.group_id),
                    parent_id=int(row.parent_id) if row.parent_id else None,
                    oco_id=int(row.oco_id) if row.oco_id else None) for row in rows]


def stop_coverage(pend: list[PendingOrder], symbol: str, side: str, qty: float):
    """Live stop protection for one position.

    OCO children are alternatives (one fills, the rest cancel), so each OCO group
    contributes at most its largest stop; independent groups add up; the total is
    capped at the position size. Returns (covered_qty, [(stop_price|None, qty)]).
    """
    # Alternatives inside one OCO container (top level or nested under a TRIGGER) count once;
    # every other live stop is independent.
    groups: dict[tuple, list[PendingOrder]] = {}
    for p in pend:
        if p.symbol == symbol and p.role == "STOP" and p.live and p.legs == 1 and p.exits(side):
            key = ("oco", p.oco_id) if p.oco_id is not None else ("single", p.order_id)
            groups.setdefault(key, []).append(p)
    alloc: list[tuple[float | None, float]] = []
    left = qty
    for group in groups.values():
        if left <= 1e-9:
            break
        best = max(group, key=lambda p: p.qty)
        take = min(best.qty, left)
        if take > 1e-9:
            alloc.append((best.stop, take))
            left -= take
    return qty - max(left, 0.0), alloc
