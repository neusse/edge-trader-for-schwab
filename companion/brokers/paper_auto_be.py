"""Account-scoped, one-shot Auto B/E watch for an existing Paper equity OCO."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from uuid import uuid4

from .models import (AssetType, OrderGroup, OrderStatus, Position, PositionSide,
                     Quote)
from .paper_equity import verified_oco_pair
from ..store import Store

MAX_QUOTE_AGE = 15
CONFIRM_GAP = 2.5
MAX_OBSERVATION_GAP = 20


def oco_auto_be_plan(position: Position, group: OrderGroup) -> dict:
    """Derive +1R and a tighter cent-priced stop from the held basis and exact OCO."""
    stop, target = verified_oco_pair(position, group)
    if (position.average_price is None or not position.average_price.is_finite() or
            position.average_price <= 0):
        raise ValueError("Paper Auto B/E needs a known positive held entry basis")
    if (not target.client_order_id or
            not target.client_order_id.startswith("et-oco-")):
        raise ValueError("Paper Auto B/E needs an app-created, unchanged OCO")
    basis = position.average_price
    if position.side == PositionSide.LONG:
        risk = basis - stop.stop_price
        trigger = basis + risk
        price = basis.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        if not stop.stop_price < price < target.limit_price:
            raise ValueError("Break-even would not tighten the OCO stop below its target")
        if target.limit_price <= trigger:
            raise ValueError("OCO target is reached before the +1R Auto B/E trigger")
    else:
        risk = stop.stop_price - basis
        trigger = basis - risk
        price = basis.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        if not target.limit_price < price < stop.stop_price:
            raise ValueError("Break-even would not tighten the OCO stop above its target")
        if target.limit_price >= trigger:
            raise ValueError("OCO target is reached before the +1R Auto B/E trigger")
    if risk <= 0 or trigger <= 0:
        raise ValueError("OCO stop must define a positive +1R trigger")
    return {"symbol": position.symbol, "side": position.side.value,
            "quantity": position.quantity, "basis": basis, "initial_stop": stop.stop_price,
            "target": target.limit_price, "trigger": trigger, "new_stop": price,
            "group_id": group.group_id, "stop_id": stop.order_id,
            "target_id": target.order_id,
            "oco_client_id": target.client_order_id}


class PaperAutoBEWatch:
    def __init__(self, store: Store):
        self.store = store
        with store._lock, store._con:
            store._con.execute("""
                create table if not exists paper_auto_be (
                    symbol text primary key, side text not null, quantity text not null,
                    basis text not null, initial_stop text not null, target text not null,
                    trigger text not null, new_stop text not null,
                    group_id text not null, stop_id text not null, target_id text not null,
                    oco_client_id text not null, request_id text not null,
                    set_at text not null, status text not null, result text)
            """)
            store._con.execute("update paper_auto_be set status='unknown', "
                               "result='App restarted during Auto B/E send; check Alpaca Paper' "
                               "where status='sending'")
        self._observations: dict[str, tuple[int, datetime, datetime, str]] = {}

    @staticmethod
    def _row(row) -> dict:
        fields = ("symbol", "side", "quantity", "basis", "initial_stop", "target",
                  "trigger", "new_stop", "group_id", "stop_id", "target_id",
                  "oco_client_id", "request_id", "set_at", "status", "result")
        result = dict(zip(fields, row))
        for field in ("quantity", "basis", "initial_stop", "target", "trigger", "new_stop"):
            result[field] = Decimal(result[field])
        return result

    def list(self) -> list[dict]:
        with self.store._lock:
            rows = self.store._con.execute("select * from paper_auto_be order by set_at").fetchall()
        return [self._row(row) for row in rows]

    def get(self, symbol: str) -> dict | None:
        with self.store._lock:
            row = self.store._con.execute("select * from paper_auto_be where symbol=?",
                                          (symbol,)).fetchone()
        return self._row(row) if row else None

    def set(self, plan: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Auto B/E setup time must include a timezone")
        symbol = plan["symbol"]
        request_id = str(uuid4())
        with self.store._lock, self.store._con:
            old = self.store._con.execute(
                "select status from paper_auto_be where symbol=?", (symbol,)).fetchone()
            if old and old[0] in ("watching", "sending", "unknown"):
                raise ValueError("An earlier Paper Auto B/E watch must finish or be reconciled")
            self.store._con.execute("""
                insert into paper_auto_be values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(symbol) do update set
                  side=excluded.side, quantity=excluded.quantity, basis=excluded.basis,
                  initial_stop=excluded.initial_stop, target=excluded.target,
                  trigger=excluded.trigger, new_stop=excluded.new_stop,
                  group_id=excluded.group_id, stop_id=excluded.stop_id,
                  target_id=excluded.target_id, oco_client_id=excluded.oco_client_id,
                  request_id=excluded.request_id, set_at=excluded.set_at,
                  status=excluded.status, result=null
            """, (symbol, plan["side"], str(plan["quantity"]), str(plan["basis"]),
                  str(plan["initial_stop"]), str(plan["target"]), str(plan["trigger"]),
                  str(plan["new_stop"]), plan["group_id"], plan["stop_id"],
                  plan["target_id"], plan["oco_client_id"], request_id,
                  now.isoformat(), "watching", None))
        self._observations.pop(symbol, None)
        return self.get(symbol)

    def clear(self, symbol: str) -> bool:
        with self.store._lock, self.store._con:
            row = self.store._con.execute("select status from paper_auto_be where symbol=?",
                                          (symbol,)).fetchone()
            if row and row[0] in ("sending", "unknown"):
                return False
            self.store._con.execute("delete from paper_auto_be where symbol=?", (symbol,))
        self._observations.pop(symbol, None)
        return True

    def acknowledge_unknown(self, symbol: str) -> bool:
        row = self.get(symbol)
        if row is None or row["status"] != "unknown":
            return False
        mutation = self.store.get_mutation(row["request_id"])
        if mutation and mutation["status"] in ("SENDING", "unknown"):
            return False
        with self.store._lock, self.store._con:
            changed = self.store._con.execute(
                "update paper_auto_be set status='acknowledged', "
                "result='User checked Alpaca Paper before clearing an unknown Auto B/E' "
                "where symbol=? and status='unknown'", (symbol,)).rowcount
        return changed == 1

    def record_send(self, symbol: str, request_id: str, status: str, message: str) -> None:
        if status not in ("done", "unknown", "refused"):
            raise ValueError("Unknown Paper Auto B/E send status")
        with self.store._lock, self.store._con:
            self.store._con.execute("update paper_auto_be set status=?, result=? "
                                    "where symbol=? and request_id=? and status='sending'",
                                    (status, message[:300], symbol, request_id))

    def resolve_request(self, request_id: str, accepted: bool, message: str) -> None:
        with self.store._lock, self.store._con:
            self.store._con.execute("update paper_auto_be set status=?, result=? "
                                    "where request_id=? and status='unknown'",
                                    ("done" if accepted else "refused", message[:300], request_id))

    def observe(self, symbol: str, position: Position | None, groups: tuple[OrderGroup, ...],
                quote: Quote | None, *, market_open: bool, monitoring: bool,
                now: datetime | None = None) -> dict:
        row = self.get(symbol)
        if row is None:
            raise ValueError("Paper Auto B/E watch is no longer set")
        now = now or datetime.now(timezone.utc)
        result = {"symbol": symbol, "status": row["status"], "reason": row["result"],
                  "mark": None, "fire": False}
        if row["status"] != "watching":
            self._observations.pop(symbol, None)
            return result

        def suspend(reason: str) -> dict:
            self._observations.pop(symbol, None)
            result.update(status="suspended", reason=reason)
            return result

        def end(reason: str) -> dict:
            self._observations.pop(symbol, None)
            with self.store._lock, self.store._con:
                changed = self.store._con.execute(
                    "update paper_auto_be set status='ended', result=? "
                    "where symbol=? and status='watching' and set_at=?",
                    (reason, symbol, row["set_at"])).rowcount
            result.update(status="ended" if changed else "suspended", reason=reason)
            return result

        if not monitoring:
            return suspend("Paper Auto B/E monitor is not running")
        if not market_open:
            return suspend("Outside regular market hours")
        if (position is None or position.asset_type != AssetType.EQUITY or
                position.side.value != row["side"] or
                position.quantity != row["quantity"] or
                position.average_price != row["basis"]):
            return end("Held equity position or entry basis changed; Auto B/E ended")
        touching = [group for group in groups if any(
            leg.symbol == symbol for order in group.orders
            if order.status in (OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN)
            for leg in order.legs)]
        if len(touching) != 1 or touching[0].group_id != row["group_id"]:
            return end("Reviewed OCO is not the only active order; Auto B/E ended")
        try:
            plan = oco_auto_be_plan(position, touching[0])
        except ValueError:
            return end("Broker-held OCO changed or is no longer fully working; Auto B/E ended")
        if any(plan[key] != row[key] for key in
               ("side", "quantity", "basis", "initial_stop", "target", "trigger",
                "new_stop", "group_id", "stop_id", "target_id", "oco_client_id")):
            return end("Broker-held OCO changed since setup; Auto B/E ended")
        if (quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or
                not quote.realtime or not quote.timestamp_verified or
                quote.observed_at.tzinfo is None or
                not 0 <= (now - quote.observed_at).total_seconds() <= MAX_QUOTE_AGE or
                quote.observed_at <= datetime.fromisoformat(row["set_at"])):
            return suspend("Fresh verified IEX quote is unavailable")
        mark = quote.mark or quote.last
        if mark is None or mark <= 0:
            return suspend("IEX quote has no usable mark")
        result["mark"] = str(mark)
        hit = mark >= row["trigger"] if row["side"] == "long" else mark <= row["trigger"]
        if not hit:
            self._observations.pop(symbol, None)
            result["reason"] = "Waiting for +1R"
            return result
        previous_count, previous, first, revision = self._observations.get(
            symbol, (0, quote.observed_at, quote.observed_at, row["set_at"]))
        if revision != row["set_at"] or (quote.observed_at - previous).total_seconds() > MAX_OBSERVATION_GAP:
            previous_count, first = 0, quote.observed_at
        if previous_count == 0 or quote.observed_at > previous:
            previous_count += 1
            previous = quote.observed_at
        self._observations[symbol] = (previous_count, previous, first, row["set_at"])
        if previous_count < 2 or (previous - first).total_seconds() < CONFIRM_GAP:
            result["reason"] = f"At +1R; confirming distinct quote ({previous_count} of 2)"
            return result
        self._observations.pop(symbol, None)
        with self.store._lock, self.store._con:
            changed = self.store._con.execute(
                "update paper_auto_be set status='sending' where symbol=? "
                "and status='watching' and set_at=?", (symbol, row["set_at"])).rowcount
        if changed:
            result.update(status="sending", reason="+1R confirmed by two fresh IEX observations",
                          fire=True)
        else:
            result.update(status="suspended", reason="Auto B/E watch changed while monitoring")
        return result
