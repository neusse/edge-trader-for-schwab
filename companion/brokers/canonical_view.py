"""Dashboard projection from broker-neutral snapshots.

The read path is deliberately pure. A missing quote or fill history is surfaced as
incomplete coverage rather than converted to a zero P&L or a protected position.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .models import (
    AccountSnapshot, Fill, OrderGroup, OrderInstruction, OrderRole, OrderStatus,
    OrderType, Quote,
)
from ..orders import PendingOrder, stop_coverage
from ..pnl import Fill as LedgerFill, Opening, build_book, session_day
from ..view import _trade_row, pretty_symbol

_BUY = {OrderInstruction.BUY, OrderInstruction.BUY_TO_COVER,
        OrderInstruction.BUY_TO_OPEN, OrderInstruction.BUY_TO_CLOSE}
_OPEN = {OrderInstruction.BUY_TO_OPEN, OrderInstruction.SELL_TO_OPEN,
         OrderInstruction.SELL_SHORT}
_CLOSE = {OrderInstruction.BUY_TO_CLOSE, OrderInstruction.SELL_TO_CLOSE,
          OrderInstruction.BUY_TO_COVER}
_TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
             OrderStatus.EXPIRED, OrderStatus.REPLACED}
_ACTIVE = {OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED}


def _ledger_fills(fills: tuple[Fill, ...]) -> list[LedgerFill]:
    rows = []
    for fill in fills:
        rows.append(LedgerFill(
            id=fill.activity_id, order_id=fill.order_id, time=fill.occurred_at,
            symbol=fill.symbol, asset_type=fill.asset_type.value.upper(),
            underlying=fill.underlying or fill.symbol, qty=float(fill.quantity) *
            (1 if fill.instruction in _BUY else -1), price=float(fill.price),
            multiplier=float(fill.multiplier) if fill.multiplier is not None else
            100.0 if fill.asset_type.value == "option" else 1.0,
            fees=-abs(float(fill.fees)),
            effect="OPENING" if fill.instruction in _OPEN else
                   "CLOSING" if fill.instruction in _CLOSE else None,
        ))
    def sequence(row):
        activity, _, index = row.id.partition(":")
        return (int(activity) if activity.isdigit() else 0,
                int(index) if index.isdigit() else 0)
    return sorted(rows, key=lambda row: (row.time, sequence(row)))


def _pending(groups: tuple[OrderGroup, ...],
             known_terminal: set[str] | None = None) -> list[PendingOrder]:
    rows = []
    seen = set()
    done = {str(value) for value in (known_terminal or ())} | {
        order.order_id for group in groups for order in group.orders
        if order.status in _TERMINAL
    }
    for group in groups:
        for order in group.orders:
            if order.order_id in done:
                continue
            for index, leg in enumerate(order.legs):
                key = (order.order_id, leg.leg_ref or index)
                if key in seen:
                    continue
                seen.add(key)
                role = order.role
                if (role == OrderRole.EXIT and not (order.client_order_id or "").startswith(
                        ("et-close-", "et-opt-close-", "et-vertical-close-",
                         "et-vertical-stop-"))):
                    native_type = order.order_type_detail
                    if (native_type in ("STOP", "STOP_LIMIT", "TRAILING_STOP", "TRAILING_STOP_LIMIT")
                            or (native_type is None and order.order_type in (
                                OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP))):
                        role = OrderRole.STOP
                    elif native_type == "LIMIT" or (native_type is None and order.order_type == OrderType.LIMIT):
                        role = OrderRole.TARGET
                rows.append(PendingOrder(
                    order_id=order.order_id, leg_id=leg.leg_ref or str(index + 1),
                    legs=len(order.legs), group_id=group.group_id,
                    group_type=group.strategy_detail or group.order_class.value.upper(),
                    parent_id=order.parent_id,
                    oco_id=order.oco_id or (group.group_id if group.order_class.value in ("oco", "bracket")
                    and role != OrderRole.ENTRY else None),
                    # Pending activation is deliberately not counted as broker-held protection.
                    status=order.status_detail or
                    ("WORKING" if order.status in _ACTIVE else "PENDING_NEW"),
                    symbol=leg.symbol, underlying=leg.underlying or leg.symbol,
                    asset_type=leg.asset_type.value.upper(),
                    instruction=leg.instruction.value.upper().replace("-", "_"),
                    effect=leg.position_effect or ("OPENING" if leg.instruction in _OPEN else
                            "CLOSING" if leg.instruction in _CLOSE else None),
                    order_type=order.order_type_detail or
                    order.order_type.value.upper().replace("-", "_"),
                    qty=float(leg.remaining_quantity if leg.remaining_quantity is not None else
                              leg.quantity - leg.filled_quantity),
                    filled=float(leg.filled_quantity),
                    price=float(order.limit_price) if order.limit_price is not None else None,
                    stop=float(order.stop_price) if order.stop_price is not None else None,
                    duration=order.duration_detail or order.time_in_force.value.upper(),
                    entered=order.entered_at.isoformat() if order.entered_at else "",
                    tag=order.client_order_id, role=role.value.upper(),
                    trail_offset=float(order.trail_price or order.trail_percent)
                    if order.trail_price is not None or order.trail_percent is not None else None,
                    trail_link=order.trail_link or ("PERCENT" if order.trail_percent is not None else
                               "VALUE" if order.trail_price is not None else None),
                    trail_basis=order.trail_basis,
                ))
    return sorted(rows, key=lambda row: (row.underlying, str(row.group_id),
                                         row.role, str(row.order_id), str(row.leg_id)))


def _mark(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    value = quote.mark if quote.mark is not None else quote.last
    if value is None and quote.bid is not None and quote.ask is not None:
        value = (quote.bid + quote.ask) / 2
    return float(value) if value is not None else None


def build_canonical_state(account: AccountSnapshot, groups: tuple[OrderGroup, ...],
                          fills: tuple[Fill, ...], quotes: dict[str, Quote], *,
                          now: datetime | None = None, orders_fresh: bool = True,
                          history_complete: bool = False, freshness: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = session_day(now, "EQUITY")
    ledger = _ledger_fills(fills)
    signed_fills: dict[str, float] = defaultdict(float)
    meta = {}
    for fill in ledger:
        signed_fills[fill.symbol] += fill.qty
        meta[fill.symbol] = (fill.asset_type, fill.underlying, fill.multiplier)
    account_qty = {p.symbol: float(p.quantity) * (1 if p.side.value == "long" else -1)
                   for p in account.positions}
    for p in account.positions:
        meta[p.symbol] = (p.asset_type.value.upper(), p.underlying or p.symbol,
                          100.0 if p.asset_type.value == "option" else 1.0)
    opening = {}
    for symbol in set(account_qty) | set(signed_fills):
        qty = account_qty.get(symbol, 0.0) - signed_fills.get(symbol, 0.0)
        if abs(qty) > 1e-9:
            opening[symbol] = Opening(qty, None, *meta[symbol])
    book = build_book(ledger, opening)
    pending = _pending(groups)
    positions = []
    for p in account.positions:
        symbol, size = p.symbol, float(p.quantity)
        side = p.side.value.upper()
        mult = 100.0 if p.asset_type.value == "option" else 1.0
        quote = quotes.get(symbol)
        mark = _mark(quote)
        quote_stale = (quote is None or quote.observed_at.tzinfo is None or
                       (now - quote.observed_at).total_seconds() > 60)
        avg = float(p.average_price) if p.average_price is not None else None
        direction = 1 if side == "LONG" else -1
        pnl = (mark - avg) * size * mult * direction if mark is not None and avg is not None else None
        covered, alloc = stop_coverage(pending, symbol, side, size)
        protection = ("UNKNOWN" if not orders_fresh else "NONE" if covered < 1e-9 else
                      "PARTIAL" if covered + 1e-9 < size else "FULL")
        stop_prices = [px for px, _ in alloc if px is not None]
        targets = [o for o in pending if o.symbol == symbol and o.role == "TARGET"
                   and o.live and o.exits(side) and o.price is not None]
        day_fills = [f for f in ledger if f.symbol == symbol and f.day == today]
        held = ("INTRADAY" if not opening.get(symbol) and day_fills else
                "MIXED" if day_fills else "CARRIED")
        trade = next((t for t in book.open_trades() if t.symbol == symbol), None)
        positions.append({
            "symbol": symbol, "display": pretty_symbol(symbol, p.asset_type.value.upper()),
            "asset_type": p.asset_type.value.upper(), "underlying": p.underlying or symbol,
            "side": side, "qty": size, "avg_price": avg, "last": mark,
            "quote_source": quote.source if quote else None,
            "quote_stale": quote_stale,
            "quote": {"mark": mark, "bid": float(quotes[symbol].bid) if symbol in quotes
                      and quotes[symbol].bid is not None else None,
                      "ask": float(quotes[symbol].ask) if symbol in quotes
                      and quotes[symbol].ask is not None else None},
            "multiplier": mult, "market_value": mark * size * mult if mark is not None else None,
            "open_pnl": pnl, "open_pnl_pct": pnl / (avg * size * mult) * 100
            if pnl is not None and avg else None,
            "day_pnl": None, "held": held,
            "stop_price": (min(stop_prices) if side == "LONG" else max(stop_prices))
            if stop_prices else None,
            "stop_best": (max(stop_prices) if side == "LONG" else min(stop_prices))
            if stop_prices else None,
            "target_price": (max(o.price for o in targets) if side == "LONG" else
                             min(o.price for o in targets)) if targets else None,
            "stop_qty": covered, "protection": protection,
            "pnl_at_stop": sum((px - avg) * qty * direction * mult for px, qty in alloc)
            if avg is not None and alloc and len(stop_prices) == len(alloc) else None,
            "trail": None,
            "targets_all": [{"price": o.price, "qty": o.qty} for o in targets],
            "ledger": "MATCH" if abs(sum(lot[0] for lot in book.open_lots.get(symbol, []))
                                     - account_qty[symbol]) < 1e-9 else "MISMATCH",
            "opened": trade.opened.isoformat() if trade and trade.opened else None,
            "realized_qty": trade.exit_qty if trade else 0.0,
            "realized": trade.net if trade and trade.exit_qty else None,
            "fill_ids": list(trade.fills) if trade else [],
        })
    positions.sort(key=lambda p: ({"INTRADAY": 0, "MIXED": 1, "CARRIED": 2}[p["held"]],
                                  p["underlying"], p["symbol"]))
    closed = sorted(book.closed_on(today, {"EQUITY", "OPTION"}),
                    key=lambda trade: trade.closed, reverse=True)
    realized = book.realized_on(today, ledger, {"EQUITY", "OPTION"})
    realized.update(complete=realized["lots_complete"] and history_complete,
                    fills_pending=0.0)
    empty_futures = {"matched": 0.0, "estimated": 0.0, "gross": 0.0, "fees": 0.0,
                     "net": 0.0, "unresolved_closes": 0, "unresolved_qty": 0.0,
                     "lots_complete": True, "complete": True, "fills_pending": 0.0}
    known = [trade for trade in closed if trade.net is not None]
    order_rows = []
    for order in pending:
        quote = quotes.get(order.symbol)
        order_rows.append({
            "order_id": order.order_id, "leg_id": order.leg_id, "legs": order.legs,
            "group_id": order.group_id, "group_type": order.group_type,
            "status": order.status, "live": order.live,
            "symbol": order.symbol, "display": pretty_symbol(order.symbol, order.asset_type),
            "underlying": order.underlying, "asset_type": order.asset_type,
            "instruction": order.instruction, "effect": order.effect,
            "order_type": order.order_type, "role": order.role, "qty": order.qty,
            "filled": order.filled, "price": order.price, "stop": order.stop,
            "duration": order.duration, "entered": order.entered, "tag": order.tag,
            "last": _mark(quote), "distance": None, "distance_pct": None,
            "transitional": not order.live,
            "cancellable": order.role == "ENTRY" and order.parent_id is None
            and order.filled == 0 and order.live,
        })
    balances = account.balances
    return {
        "generated": now.isoformat(), "today": today.isoformat(),
        "futures_session": today.isoformat(), "auth_hours_left": None,
        "freshness": freshness or {}, "history_complete": history_complete,
        "summary": {
            "realized": {"securities": realized, "futures": empty_futures,
                         "net": realized["net"], "complete": realized["complete"]},
            "open_pnl": {
                "intraday": sum(p["open_pnl"] or 0 for p in positions if p["held"] == "INTRADAY"),
                "mixed": sum(p["open_pnl"] or 0 for p in positions if p["held"] == "MIXED"),
                "carried": sum(p["open_pnl"] or 0 for p in positions if p["held"] == "CARRIED"),
                "positions": len(positions),
                "note": "Alpaca Paper mark; missing quotes excluded from total",
                "complete": all(p["open_pnl"] is not None and not p["quote_stale"]
                                for p in positions)},
            "trades_closed": len(closed), "wins": sum(1 for t in known if t.net > 0),
            "losses": sum(1 for t in known if t.net <= 0),
            "unknown": len(closed) - len(known), "fees_today": realized["fees"],
        },
        "positions": positions, "orders": order_rows,
        "closed": [_trade_row(t, {f.id: f for f in ledger}) for t in closed],
        "futures": {"observed_net_today": {}, "note": "Alpaca Paper has no futures trading."},
        "balances": {
            "liquidationValue": float(balances.liquidation_value) if balances.liquidation_value is not None else None,
            "equity": float(balances.equity) if balances.equity is not None else None,
            "buyingPower": float(balances.buying_power) if balances.buying_power is not None else None,
            "dayTradingBuyingPower": float(balances.day_trading_buying_power)
            if balances.day_trading_buying_power is not None else None,
            "availableFunds": float(balances.available_funds) if balances.available_funds is not None else None,
            "maintenanceRequirement": float(balances.maintenance_requirement)
            if balances.maintenance_requirement is not None else None,
        },
    }
