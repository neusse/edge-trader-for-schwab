"""Broker-neutral fills, FIFO lots, realized P&L, and round-trip trades.

Legacy Schwab callers use compatibility functions that normalize stored TRADE
transactions through the Schwab adapter. Canonical fills cover equities,
options, and futures with signed quantities and contract multipliers.

Inventory older than the fill history enters the book as an opening lot
(`Opening`), so FIFO consumes it first. Its basis is either known (ESTIMATED,
from Schwab's average price) or unknown (UNRESOLVED: P&L unknown, never 0).

Pure functions, no I/O: easy to test against data/samples.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
EPS = 1e-9


def session_day(t: datetime, asset_type: str) -> date:
    """Trading day. Futures sessions open 18:00 ET the evening before."""
    et = t.astimezone(ET)
    if asset_type == "FUTURE":
        et += timedelta(hours=6)
    return et.date()


@dataclass(frozen=True)
class Fill:
    id: str              # "<activityId>:<item index>"
    order_id: int | None
    time: datetime
    symbol: str          # display symbol, e.g. TSM, /MNQZ26, COHR  270319P00260000
    asset_type: str      # EQUITY | OPTION | FUTURE | ...
    underlying: str
    qty: float           # signed: + bought, - sold
    price: float
    multiplier: float
    fees: float          # <= 0
    effect: str | None   # OPENING | CLOSING | None

    @property
    def day(self) -> date:
        return session_day(self.time, self.asset_type)


def fills_from_transaction(t: dict) -> list[Fill]:
    """Compatibility entry point for stored Schwab transactions."""
    from .brokers.canonical_view import _ledger_fills
    from .brokers.schwab_adapter import normalize_fills
    return _ledger_fills(normalize_fills([t]))


def fills_from_transactions(txns: list[dict]) -> list[Fill]:
    from .brokers.canonical_view import _ledger_fills
    from .brokers.schwab_adapter import normalize_fills
    return _ledger_fills(normalize_fills(txns))


@dataclass(frozen=True)
class Opening:
    """Inventory held before the first fill we have (signed qty), with basis if known."""
    qty: float
    basis: float | None
    asset_type: str
    underlying: str
    multiplier: float


@dataclass
class Trade:
    """One round trip: flat -> position -> flat, for one symbol.

    status: MATCHED (every closed unit has a known entry fill),
            ESTIMATED (some closed units came from opening inventory at Schwab's basis),
            UNRESOLVED (some closed units have no basis: their P&L is unknown).
    """
    symbol: str
    asset_type: str
    underlying: str
    side: str                       # LONG | SHORT
    multiplier: float
    opened: datetime | None         # None when the entry predates the fill history
    closed: datetime | None = None
    close_day: date | None = None
    max_qty: float = 0.0
    entry_qty: float = 0.0          # units with a known entry price
    entry_value: float = 0.0
    exit_qty: float = 0.0
    exit_value: float = 0.0
    realized: float = 0.0           # gross $ for units with known basis
    fees: float = 0.0
    fills: list[str] = field(default_factory=list)
    status: str = "MATCHED"
    unresolved_qty: float = 0.0
    position: float = 0.0           # signed open quantity

    @property
    def avg_entry(self) -> float | None:
        return self.entry_value / self.entry_qty if self.entry_qty else None

    @property
    def avg_exit(self) -> float | None:
        return self.exit_value / self.exit_qty if self.exit_qty else None

    @property
    def open_qty(self) -> float:
        return abs(self.position)

    @property
    def net(self) -> float | None:
        return None if self.status == "UNRESOLVED" else self.realized + self.fees

    def flag(self, status: str) -> None:
        rank = {"MATCHED": 0, "ESTIMATED": 1, "UNRESOLVED": 2}
        if rank[status] > rank[self.status]:
            self.status = status


@dataclass
class RealizedEvent:
    day: date
    symbol: str
    pnl: float
    status: str      # MATCHED | ESTIMATED | UNRESOLVED (pnl 0, qty counts)
    qty: float


@dataclass
class Book:
    trades: list[Trade]
    open_lots: dict[str, list[list]]          # symbol -> [[signed qty, price|None, from_fill], ...]
    meta: dict[str, tuple[str, str, float]]   # symbol -> (asset_type, underlying, multiplier)
    events: list[RealizedEvent]

    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.closed is None]

    def closed_on(self, d: date, asset_types: set[str] | None = None) -> list[Trade]:
        return [t for t in self.trades if t.close_day == d
                and (asset_types is None or t.asset_type in asset_types)]

    def realized_on(self, d: date, fills: list[Fill], asset_types: set[str] | None = None) -> dict:
        """Realized $ from closes on trading day d, split by confidence, plus fees paid on d."""
        ev = [e for e in self.events if e.day == d
              and (asset_types is None or self.meta[e.symbol][0] in asset_types)]
        fl = [f for f in fills if f.day == d and (asset_types is None or f.asset_type in asset_types)]
        matched = sum(e.pnl for e in ev if e.status == "MATCHED")
        estimated = sum(e.pnl for e in ev if e.status == "ESTIMATED")
        fees = sum(f.fees for f in fl)
        return {
            "matched": matched,
            "estimated": estimated,
            "estimated_closes": len({e.symbol for e in ev if e.status == "ESTIMATED"}),
            "gross": matched + estimated,
            "fees": fees,
            "net": matched + estimated + fees,
            "unresolved_closes": len({e.symbol for e in ev if e.status == "UNRESOLVED"}),
            "unresolved_qty": sum(e.qty for e in ev if e.status == "UNRESOLVED"),
            "lots_complete": all(e.status == "MATCHED" for e in ev),
        }


def build_book(fills: list[Fill], opening: dict[str, Opening] | None = None) -> Book:
    """FIFO-match fills per symbol (opening inventory first) and cut them into round trips."""
    lots: dict[str, list[list]] = defaultdict(list)
    current: dict[str, Trade] = {}
    trades: list[Trade] = []
    meta: dict[str, tuple[str, str, float]] = {}
    events: list[RealizedEvent] = []

    for sym, op in (opening or {}).items():
        if abs(op.qty) < EPS:
            continue
        meta[sym] = (op.asset_type, op.underlying, op.multiplier)
        lots[sym].append([op.qty, op.basis, False])
        t = Trade(sym, op.asset_type, op.underlying, "LONG" if op.qty > 0 else "SHORT",
                  op.multiplier, None, max_qty=abs(op.qty), position=op.qty)
        if op.basis is not None:
            t.entry_qty, t.entry_value = abs(op.qty), abs(op.qty) * op.basis
        current[sym] = t
        trades.append(t)

    def touch(t: Trade, f: Fill, fee: float) -> None:
        t.fees += fee
        if f.id not in t.fills:
            t.fills.append(f.id)

    def apply(f: Fill) -> None:
        meta[f.symbol] = (f.asset_type, f.underlying, f.multiplier)
        book = lots[f.symbol]
        remaining = f.qty
        fee_left = f.fees
        while abs(remaining) > EPS:
            pos = sum(lot[0] for lot in book)
            t = current.get(f.symbol)

            if abs(pos) < EPS and f.effect == "CLOSING":
                # A close with no inventory at all (futures history gaps, missed data).
                t = Trade(f.symbol, f.asset_type, f.underlying,
                          "LONG" if remaining < 0 else "SHORT", f.multiplier, None)
                trades.append(t)
                qty = abs(remaining)
                t.exit_qty, t.exit_value = qty, qty * f.price
                t.unresolved_qty = qty
                t.flag("UNRESOLVED")
                events.append(RealizedEvent(f.day, f.symbol, 0.0, "UNRESOLVED", qty))
                touch(t, f, fee_left)
                t.closed, t.close_day = f.time, f.day
                return

            if abs(pos) < EPS or (pos > 0) == (remaining > 0):
                # Opening or adding.
                if t is None:
                    t = Trade(f.symbol, f.asset_type, f.underlying,
                              "LONG" if remaining > 0 else "SHORT", f.multiplier, f.time)
                    current[f.symbol] = t
                    trades.append(t)
                book.append([remaining, f.price, True])
                t.entry_qty += abs(remaining)
                t.entry_value += abs(remaining) * f.price
                t.position += remaining
                t.max_qty = max(t.max_qty, abs(t.position))
                touch(t, f, fee_left)
                return

            # Reducing: consume the oldest lot.
            lot = book[0]
            take = min(abs(lot[0]), abs(remaining))
            direction = 1 if lot[0] > 0 else -1
            t.exit_qty += take
            t.exit_value += take * f.price
            if lot[1] is None:
                t.unresolved_qty += take
                t.flag("UNRESOLVED")
                events.append(RealizedEvent(f.day, f.symbol, 0.0, "UNRESOLVED", take))
            else:
                pnl = (f.price - lot[1]) * take * direction * f.multiplier
                status = "MATCHED" if lot[2] else "ESTIMATED"
                t.flag(status)
                t.realized += pnl
                events.append(RealizedEvent(f.day, f.symbol, pnl, status, take))
            touch(t, f, fee_left)
            fee_left = 0.0
            lot[0] -= take * direction
            t.position -= take * direction
            remaining += take * direction
            if abs(lot[0]) < EPS:
                book.pop(0)
            if not book:
                t.closed, t.close_day = f.time, f.day
                current.pop(f.symbol, None)
            # Leftover after a full close: older inventory (CLOSING) or a flip into a new trade.

    def position(sym: str) -> float:
        return sum(lot[0] for lot in lots[sym])

    # Same-second fills: their order is not reliable. With inventory on the books,
    # closes go first (close, then reverse); when flat, opens go first (open, then close).
    for _, grp in groupby(fills, key=lambda f: f.time):
        group = list(grp)
        if len(group) > 1:
            flat = {f.symbol: abs(position(f.symbol)) < EPS for f in group}
            group.sort(key=lambda f: (f.effect == "CLOSING") == flat[f.symbol])
        for f in group:
            apply(f)

    return Book(trades=trades, open_lots={k: v for k, v in lots.items() if v},
                meta=meta, events=events)


def opening_inventory(fills: list[Fill], prev_session: dict[str, tuple[float, float | None]],
                      today: date) -> dict[str, Opening]:
    """Equity/option inventory held before the first fill, from yesterday's closing quantity.

    prev_session: symbol -> (signed quantity at the previous session close, Schwab
    basis or None). Symbols missing from it are assumed flat at yesterday's close.
    Futures are skipped: Schwab never reports futures positions.
    """
    before: dict[str, float] = defaultdict(float)
    meta: dict[str, tuple[str, str, float]] = {}
    for f in fills:
        if f.asset_type == "FUTURE":
            continue
        meta[f.symbol] = (f.asset_type, f.underlying, f.multiplier)
        if f.day < today:
            before[f.symbol] += f.qty
    out: dict[str, Opening] = {}
    for sym in set(before) | set(prev_session):
        prev_qty, basis = prev_session.get(sym, (0.0, None))
        qty = prev_qty - before.get(sym, 0.0)
        if abs(qty) < EPS:
            continue
        at, und, mult = meta.get(sym, ("OPTION" if len(sym) > 15 else "EQUITY", sym,
                                       100.0 if len(sym) > 15 else 1.0))
        # The basis only describes the opening lot when no in-history fills mixed into it.
        out[sym] = Opening(qty, basis if abs(qty - prev_qty) < EPS else None, at, und, mult)
    return out
