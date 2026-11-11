"""Account-scoped, one-shot Paper vertical stop observations.

Only verified real-time OPRA quotes can trigger. This module does not send orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from .models import Quote
from ..store import Store

MAX_QUOTE_AGE = 15.0
MAX_QUOTE_SKEW = 10.0
CONFIRM_GAP = 2.5
MAX_OBSERVATION_GAP = 20.0


class PaperSpreadStopWatch:
    def __init__(self, store: Store):
        self.store = store
        with store._lock, store._con:
            store._con.execute("""
                create table if not exists paper_spread_stops (
                    stop_id text primary key, long_symbol text not null, short_symbol text not null,
                    quantity text not null, kind text not null, basis text not null,
                    long_average text not null, short_average text not null,
                    width text not null, stop_value text not null, send integer not null,
                    set_at text not null, status text not null, request_id text not null,
                    trigger_at text, result text)
            """)
            store._con.execute("update paper_spread_stops set status='unknown', "
                               "result='App restarted during a stop close; reconcile with Alpaca Paper' "
                               "where status='sending'")
        self._observations: dict[str, tuple[int, datetime, datetime, str]] = {}

    @staticmethod
    def _row(row) -> dict:
        names = ("stop_id", "long_symbol", "short_symbol", "quantity", "kind", "basis",
                 "long_average", "short_average", "width", "stop_value", "send", "set_at", "status", "request_id",
                 "trigger_at", "result")
        result = dict(zip(names, row))
        for name in ("quantity", "basis", "long_average", "short_average", "width", "stop_value"):
            result[name] = Decimal(result[name])
        result["send"] = bool(result["send"])
        return result

    def list(self) -> list[dict]:
        with self.store._lock:
            rows = self.store._con.execute("select * from paper_spread_stops order by set_at").fetchall()
        return [self._row(row) for row in rows]

    def get(self, stop_id: str) -> dict | None:
        with self.store._lock:
            row = self.store._con.execute("select * from paper_spread_stops where stop_id=?",
                                          (stop_id,)).fetchone()
        return self._row(row) if row else None

    def set(self, long_symbol: str, short_symbol: str, quantity: Decimal,
            kind: str, basis: Decimal, long_average: Decimal, short_average: Decimal,
            width: Decimal, stop_value: Decimal,
            send: bool, now: datetime | None = None) -> dict:
        if (kind not in ("debit", "credit") or quantity <= 0 or basis <= 0 or
                long_average <= 0 or short_average <= 0 or
                width <= basis or not 0 < stop_value < width or
                (kind == "debit" and stop_value >= basis) or
                (kind == "credit" and stop_value <= basis)):
            raise ValueError("Spread stop must reduce the entry value before maximum loss")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Spread stop time must include a timezone")
        stop_id = f"{long_symbol}|{short_symbol}"
        request_id = str(uuid4())
        with self.store._lock, self.store._con:
            current = self.store._con.execute(
                "select status from paper_spread_stops where stop_id=?", (stop_id,)).fetchone()
            if current and current[0] in ("sending", "unknown"):
                raise ValueError("An earlier spread stop close needs reconciliation")
            self.store._con.execute("""
                insert into paper_spread_stops values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(stop_id) do update set
                  quantity=excluded.quantity, kind=excluded.kind, basis=excluded.basis,
                  long_average=excluded.long_average, short_average=excluded.short_average,
                  width=excluded.width, stop_value=excluded.stop_value, send=excluded.send,
                  set_at=excluded.set_at, status=excluded.status,
                  request_id=excluded.request_id, trigger_at=null, result=null
            """, (stop_id, long_symbol, short_symbol, str(quantity), kind, str(basis),
                  str(long_average), str(short_average), str(width), str(stop_value),
                  1 if send else 0, now.isoformat(),
                  "watching", request_id, None, None))
        self._observations.pop(stop_id, None)
        return self.get(stop_id)

    def clear(self, stop_id: str) -> bool:
        with self.store._lock, self.store._con:
            row = self.store._con.execute("select status from paper_spread_stops where stop_id=?",
                                          (stop_id,)).fetchone()
            if row and row[0] in ("sending", "unknown"):
                return False
            self.store._con.execute("delete from paper_spread_stops where stop_id=?", (stop_id,))
        self._observations.pop(stop_id, None)
        return True

    def record_send(self, stop_id: str, request_id: str, status: str, message: str) -> None:
        if status not in ("sent", "unknown", "refused"):
            raise ValueError("Unknown stop send status")
        with self.store._lock, self.store._con:
            self.store._con.execute("update paper_spread_stops set status=?, result=? "
                                    "where stop_id=? and request_id=? and status='sending'",
                                    (status, message[:300], stop_id, request_id))

    def resolve_request(self, request_id: str, accepted: bool, message: str) -> None:
        with self.store._lock, self.store._con:
            self.store._con.execute(
                "update paper_spread_stops set status=?, result=? where request_id=? "
                "and status='unknown'",
                ("sent" if accepted else "refused", message[:300], request_id))

    def acknowledge_unknown(self, stop_id: str) -> bool:
        stop = self.get(stop_id)
        if stop is None or stop["status"] != "unknown":
            return False
        mutation = self.store.get_mutation(stop["request_id"])
        if mutation and mutation["status"] in ("SENDING", "unknown"):
            return False
        with self.store._lock, self.store._con:
            changed = self.store._con.execute(
                "update paper_spread_stops set status='acknowledged', "
                "result='User checked Alpaca Paper before clearing an unknown stop' "
                "where stop_id=? and status='unknown'", (stop_id,)).rowcount
        return changed == 1

    def observe(self, stop_id: str, quotes: dict[str, Quote], *, market_open: bool,
                pair_matches: bool, orders_clear: bool, monitoring: bool,
                now: datetime | None = None) -> dict:
        stop = self.get(stop_id)
        if stop is None:
            raise ValueError("Spread stop is no longer set")
        now = now or datetime.now(timezone.utc)
        result = {"stop_id": stop_id, "status": stop["status"], "reason": stop["result"],
                  "mid": None, "fire": False}
        if stop["status"] != "watching":
            self._observations.pop(stop_id, None)
            return result

        def suspend(reason: str) -> dict:
            self._observations.pop(stop_id, None)
            result.update(status="suspended", reason=reason)
            return result

        if not monitoring:
            return suspend("Paper stop monitor is not running")
        if not market_open:
            return suspend("Outside regular market hours")
        if not pair_matches:
            return suspend("Held vertical quantity or entry basis changed")
        if not orders_clear:
            return suspend("Another order touches a vertical leg")
        prices = []
        times = []
        for symbol in (stop["long_symbol"], stop["short_symbol"]):
            quote = quotes.get(symbol)
            if (quote is None or quote.symbol != symbol or
                    quote.source != "Alpaca OPRA options" or not quote.realtime or
                    not quote.timestamp_verified or quote.observed_at.tzinfo is None or
                    quote.bid is None or quote.ask is None or
                    quote.bid < 0 or quote.ask <= 0 or quote.bid > quote.ask or
                    not 0 <= (now - quote.observed_at).total_seconds() <= MAX_QUOTE_AGE or
                    quote.observed_at <= datetime.fromisoformat(stop["set_at"])):
                return suspend(f"Fresh verified OPRA quote unavailable for {symbol}")
            prices.append((quote.bid, quote.ask))
            times.append(quote.observed_at)
        if prices[0][0] <= 0:
            return suspend("Long leg has no executable bid")
        if abs((times[0] - times[1]).total_seconds()) > MAX_QUOTE_SKEW:
            return suspend("Vertical leg quotes are too far apart in time")
        long_mid = sum(prices[0]) / 2
        short_mid = sum(prices[1]) / 2
        mid = long_mid - short_mid if stop["kind"] == "debit" else short_mid - long_mid
        result["mid"] = str(mid)
        hit = mid <= stop["stop_value"] if stop["kind"] == "debit" else mid >= stop["stop_value"]
        if not hit:
            self._observations.pop(stop_id, None)
            result["reason"] = "Stop has not been reached"
            return result
        latest = max(times)
        count, previous, first, revision = self._observations.get(
            stop_id, (0, latest, latest, stop["set_at"]))
        if revision != stop["set_at"] or (latest - previous).total_seconds() > MAX_OBSERVATION_GAP:
            count, first = 0, latest
        if count == 0 or latest > previous:
            count += 1
            previous = latest
        self._observations[stop_id] = (count, previous, first, stop["set_at"])
        if count < 2 or (previous - first).total_seconds() < CONFIRM_GAP:
            result["reason"] = f"At stop; confirming distinct quote ({count} of 2)"
            return result
        self._observations.pop(stop_id, None)
        with self.store._lock, self.store._con:
            status = "sending" if stop["send"] else "triggered"
            changed = self.store._con.execute(
                "update paper_spread_stops set status=?, trigger_at=? where stop_id=? "
                "and status='watching' and set_at=?",
                (status, now.isoformat(), stop_id, stop["set_at"])).rowcount
        if changed:
            result.update(status=status, reason="Stop confirmed by two fresh OPRA observations",
                          fire=stop["send"])
        else:
            result.update(status="suspended", reason="Stop changed while monitoring")
        return result
