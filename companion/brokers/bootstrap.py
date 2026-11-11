"""Read-only Alpaca paper polling while order workflows are migrated."""
from __future__ import annotations

import threading
import copy
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca_paper import AlpacaPaperAdapter
from .canonical_view import build_canonical_state
from .errors import read_health
from .models import (AccountQuery, AssetType, FillsQuery, MarketClockQuery,
                     MarketSessionsQuery, OrdersQuery, OrderStatus, PositionSide,
                     QuotesQuery, ReplaceOrder, SubmitOrder)
from .order_service import BrokerOrderService
from .paper_auto_be import PaperAutoBEWatch, oco_auto_be_plan
from .paper_spread_stop import PaperSpreadStopWatch
from .paper_vertical import vertical_close_plan
from .paper_options import OCC
from .persistence import load_fills, save_fills
from ..store import Store


class AlpacaBootstrapPoller:
    def __init__(self, adapter: AlpacaPaperAdapter | None = None):
        self.store = Store(Path(":memory:"))
        self._stores = [self.store]
        self.trader = None
        self.terminal: set[str] = set()
        self.orders_today: dict[str, dict] = {}
        self._adapter = adapter
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = {"status": "Alpaca Paper connecting", "booting": True,
                       "positions": [], "orders": [], "broker_ready": False}
        self._thread: threading.Thread | None = None
        self._verified_account: str | None = None
        self.order_service: BrokerOrderService | None = None
        self.auto_be: PaperAutoBEWatch | None = None
        self.spread_stops: PaperSpreadStopWatch | None = None
        self.action_lock = None  # injected by app; never wait for it while broker selection stops us
        self._last_fill_read: datetime | None = None
        self._sessions_date = None
        self._sessions = ()
        self._cooldown_until: datetime | None = None
        self._opra_retry_at: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alpaca-paper-poller", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(10)

    def poll_once(self) -> None:
        now = datetime.now(timezone.utc)
        if self._cooldown_until and now < self._cooldown_until:
            return
        stage = "account"
        try:
            if self._adapter is None:
                self._adapter = AlpacaPaperAdapter()
            account = self._adapter.query(AccountQuery())
            if self._verified_account != account.account_ref:
                # Keep earlier handles open until shutdown so an in-flight settings read
                # can finish even while the active account scope is replaced.
                scoped = Store.for_broker("alpaca-paper", account.account_ref)
                if self.trader is not None:
                    self.trader.arm(False, reason="account scope change")
                with self._lock:
                    self.store = scoped
                    self.order_service = BrokerOrderService(self._adapter, scoped,
                                                             account.account_ref)
                    self.auto_be = PaperAutoBEWatch(scoped)
                    self.spread_stops = PaperSpreadStopWatch(scoped)
                    if self.trader is not None:
                        self.trader.store = scoped
                    self._stores.append(scoped)
                    self._state = {"status": "Alpaca Paper loading selected account",
                                   "booting": True, "positions": [], "orders": [],
                                   "broker_ready": False}
                if self.trader is not None:
                    self.trader.arm(False, reason="account scope activated")
                self._verified_account = account.account_ref
                self._last_fill_read = None
                self._sessions_date = None
                self._opra_retry_at = None
            stage = "orders_today"
            recent = self._adapter.query(OrdersQuery(now - timedelta(days=30), now))
            active = frozenset({OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN})
            stage = "orders_sweep"
            working = self._adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                        statuses=active))
            groups = tuple({group.group_id: group for group in (*recent, *working)}.values())
            health_errors = []
            stage = "market_hours"
            try:
                clock = self._adapter.query(MarketClockQuery())
            except Exception as exc:
                clock = None
                health = read_health(exc)
                if health["state"] == "rate-limited":
                    raise
                health_errors.append(health)
            et_day = now.astimezone(ZoneInfo("America/New_York")).date()
            if self._sessions_date != et_day:
                try:
                    self._sessions = self._adapter.query(MarketSessionsQuery(
                        et_day, et_day + timedelta(days=7)))
                    self._sessions_date = et_day
                except Exception as exc:
                    self._sessions = ()
                    health = read_health(exc)
                    if health["state"] == "rate-limited":
                        raise
                    health_errors.append(health)
            today_session = next((s for s in self._sessions if s.session_date == et_day), None)
            next_session = next((s for s in self._sessions if s.session_date > et_day), None)
            def session_row(session):
                return ({"date": session.session_date.isoformat(),
                         "open": session.opens_at.isoformat(),
                         "close": session.closes_at.isoformat()}
                        if session and session.is_open else None)
            market_hours = {"today": session_row(today_session),
                            "next": session_row(next_session),
                            "is_open": clock.is_open if clock else None,
                            "next_open": clock.next_open.isoformat() if clock else None,
                            "next_close": clock.next_close.isoformat() if clock else None}
            fill_error = None
            since = now - timedelta(days=60)
            if self._last_fill_read is None or (now - self._last_fill_read).total_seconds() >= 60:
                stage = "transactions"
                try:
                    fresh_fills = self._adapter.query(FillsQuery(since, now))
                    save_fills(self.store, fresh_fills)
                    self._last_fill_read = now
                except Exception as exc:
                    health = read_health(exc)
                    if health["state"] == "rate-limited":
                        raise
                    health_errors.append(health)
                    fill_error = health["message"]
            fills = load_fills(self.store, since)
            symbols = [(position.symbol, position.asset_type) for position in account.positions]
            symbols.extend((leg.symbol, leg.asset_type) for group in groups
                           for order in group.orders if order.status in active for leg in order.legs)
            quotes = {}
            quote_error = None
            for asset_type in (AssetType.EQUITY, AssetType.OPTION):
                names = tuple(dict.fromkeys(symbol for symbol, kind in symbols
                                            if kind == asset_type))[:30]
                if len({symbol for symbol, kind in symbols if kind == asset_type}) > 30:
                    quote_error = "Quote limit reached; some symbols are unpriced"
                    health_errors.append({"state": "stale", "message": quote_error,
                                          "retry_after_seconds": None})
                if names:
                    stage = "quotes"
                    try:
                        quotes.update(self._adapter.query(QuotesQuery(names, asset_type)))
                    except Exception as exc:
                        health = read_health(exc)
                        if health["state"] == "rate-limited":
                            raise
                        health_errors.append(health)
                        quote_error = health["message"]
            rate_limit = next((h for h in health_errors if h["state"] == "rate-limited"), None)
            if rate_limit:
                self._cooldown_until = now + timedelta(seconds=rate_limit["retry_after_seconds"])
            else:
                self._cooldown_until = None
            freshness = {
                "account": {"ok_at": now.isoformat(), "error": None, "interval": 10},
                "orders_today": {"ok_at": now.isoformat(), "error": None, "interval": 10},
                "orders_sweep": {"ok_at": now.isoformat(), "error": None, "interval": 10},
                "transactions": {"ok_at": self._last_fill_read.isoformat()
                                 if self._last_fill_read else None,
                                 "error": fill_error, "interval": 60},
                "quotes": {"ok_at": now.isoformat() if not quote_error else None,
                           "error": quote_error, "interval": 10},
                "market_hours": {"ok_at": now.isoformat() if clock else None,
                                 "error": None if clock else "Market clock unavailable",
                                 "interval": 10},
            }
            canonical = build_canonical_state(
                account, groups, fills, quotes, now=now,
                orders_fresh=True, history_complete=fill_error is None and
                self._last_fill_read is not None, freshness=freshness)
            paper_spread_stops = self._spread_stop_rows(account, groups, clock,
                                                        today_session, now)
            paper_auto_be = self._auto_be_rows(account, groups, quotes, clock,
                                               today_session, now)
            positions = [{"symbol": p.symbol, "asset_type": p.asset_type.value,
                          "side": p.side.value, "quantity": str(p.quantity),
                          "average_price": str(p.average_price)} for p in account.positions]
            orders = [{"id": o.order_id, "status": o.status.value,
                       "type": o.order_type.value, "role": o.role.value,
                       "symbols": [leg.symbol for leg in o.legs],
                       "quantities": [str(leg.quantity) for leg in o.legs]}
                      for group in groups for o in group.orders]
            health = health_errors[0] if health_errors else {"state": "ready", "message": "Alpaca Paper connected",
                                                        "retry_after_seconds": None}
            state = {**canonical, "status": health["message"],
                     "booting": False, "broker_ready": not health_errors,
                     "broker_health": health, "market_hours": market_hours,
                     "paper_spread_stops": paper_spread_stops,
                     "paper_auto_be": paper_auto_be,
                     "fills": [{"activity_id": fill.activity_id, "symbol": fill.symbol,
                                "instruction": fill.instruction.value, "quantity": str(fill.quantity),
                                "price": str(fill.price), "time": fill.occurred_at.isoformat()}
                               for fill in sorted(fills, key=lambda f: f.occurred_at, reverse=True)[:100]],
                     "alpaca": {"account_status": account.status,
                                "options_trading_level": account.options_trading_level,
                                "options_approved_level": account.options_approved_level,
                                "equity": str(account.balances.equity) if account.balances.equity is not None else None,
                                "cash": str(account.balances.available_funds) if account.balances.available_funds is not None else None,
                                "buying_power": str(account.balances.buying_power) if account.balances.buying_power is not None else None,
                                "positions": positions, "orders": orders,
                                "updated_at": now.isoformat()}}
        except Exception as exc:
            health = read_health(exc)
            if health["state"] == "rate-limited":
                self._cooldown_until = now + timedelta(seconds=health["retry_after_seconds"])
            with self._lock:
                state = copy.deepcopy(self._state)
            state.update(status=health["message"], booting=False, broker_ready=False,
                         broker_health=health)
            state["paper_spread_stops"] = [
                {**row, "status": "suspended", "reason": "Paper account or poller is not ready"}
                for row in state.get("paper_spread_stops") or ()]
            state["paper_auto_be"] = [
                {**row, "status": "suspended", "reason": "Paper account or poller is not ready"}
                for row in state.get("paper_auto_be") or ()]
            for position in state.get("positions") or ():
                position["protection"] = "UNKNOWN"
                position["quote_stale"] = True
            if state.get("summary"):
                state["summary"]["realized"]["complete"] = False
                state["summary"]["open_pnl"]["complete"] = False
            fresh = state.get("freshness") or {}
            fresh.setdefault(stage, {"ok_at": None, "interval": 10})["error"] = health["message"]
            state["freshness"] = fresh
        with self._lock:
            self._state = state

    @staticmethod
    def _stop_pair_matches(account, stop: dict) -> bool:
        match = OCC.fullmatch(stop["long_symbol"])
        if match is None or account.status.upper() != "ACTIVE":
            return False
        underlying = match.group(1)
        held = [position for position in account.positions
                if position.asset_type == AssetType.OPTION and
                (found := OCC.fullmatch(position.symbol.replace(" ", ""))) and
                found.group(1) == underlying]
        if len(held) != 2:
            return False
        by_symbol = {position.symbol.replace(" ", ""): position for position in held}
        long, short = by_symbol.get(stop["long_symbol"]), by_symbol.get(stop["short_symbol"])
        return bool(long and short and long.side == PositionSide.LONG and
                    short.side == PositionSide.SHORT and
                    long.quantity == short.quantity == stop["quantity"] and
                    long.average_price == stop["long_average"] and
                    short.average_price == stop["short_average"])

    @staticmethod
    def _stop_orders_clear(groups, stop: dict) -> bool:
        underlying = OCC.fullmatch(stop["long_symbol"]).group(1)
        active = {OrderStatus.PENDING, OrderStatus.WORKING,
                  OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
        return not any(order.status in active and any(
            (match := OCC.fullmatch(leg.symbol.replace(" ", ""))) and
            match.group(1) == underlying for leg in order.legs)
            for group in groups for order in group.orders)

    def _send_spread_stop(self, stop: dict) -> None:
        watch = self.spread_stops
        lock = self.action_lock
        if lock is None or not lock.acquire(blocking=False):
            watch.record_send(stop["stop_id"], stop["request_id"], "refused",
                              "Another broker action was in progress; no close was sent")
            return
        attempted = False
        try:
            service = self.order_service
            if (self._stop.is_set() or service is None or service.store is not watch.store or
                    not service.armed):
                raise ValueError("Paper runtime or arming changed; no close was sent")
            account = self._adapter.query(AccountQuery())
            if account.account_ref != service.account_ref or not self._stop_pair_matches(account, stop):
                raise ValueError("Held vertical changed; no close was sent")
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = self._adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                     statuses=active))
            if not self._stop_orders_clear(groups, stop):
                raise ValueError("Another order touches the vertical; no close was sent")
            symbols = (stop["long_symbol"], stop["short_symbol"])
            quotes = self._adapter.query(QuotesQuery(symbols, AssetType.OPTION,
                                                     realtime_required=True))
            long, short = (quotes.get(symbol) for symbol in symbols)
            if any(quote is None or quote.symbol != symbol or
                   quote.source != "Alpaca OPRA options" or
                   not quote.realtime or not quote.timestamp_verified or
                   quote.observed_at.tzinfo is None or
                   not 0 <= (now - quote.observed_at).total_seconds() <= 15 or
                   quote.bid is None or quote.ask is None or quote.bid < 0 or
                   quote.ask <= 0 or quote.bid > quote.ask
                   for symbol, quote in zip(symbols, (long, short))):
                raise ValueError("Verified fresh OPRA quotes disappeared; no close was sent")
            if long.bid <= 0:
                raise ValueError("Long leg has no bid; no close was sent")
            if abs((long.observed_at - short.observed_at).total_seconds()) > 10:
                raise ValueError("OPRA leg quotes are too far apart; no close was sent")
            long_mid = (long.bid + long.ask) / 2
            short_mid = (short.bid + short.ask) / 2
            latest_mid = long_mid - short_mid if stop["kind"] == "debit" else short_mid - long_mid
            if ((stop["kind"] == "debit" and latest_mid > stop["stop_value"]) or
                    (stop["kind"] == "credit" and latest_mid < stop["stop_value"])):
                raise ValueError("OPRA spread recovered above the stop; no close was sent")
            if stop["kind"] == "debit":
                executable = long.bid - short.ask
                net = -executable.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            else:
                executable = short.ask - long.bid
                net = executable.quantize(Decimal("0.01"), rounding=ROUND_UP)
            if executable <= 0 or abs(net) >= stop["width"] or net == 0:
                raise ValueError("Executable net price exceeds the spread loss guard; no close was sent")
            body = {"long_contract": symbols[0], "short_contract": symbols[1],
                    "net_price": str(net), "role": "close",
                    "request_id": stop["request_id"]}
            intent = replace(vertical_close_plan(body, account, quotes, now),
                             client_order_id=f"et-vertical-stop-{stop['request_id'].replace('-', '')}")
            def preflight(current):
                if current.account_ref != service.account_ref or not self._stop_pair_matches(current, stop):
                    raise ValueError("Vertical changed immediately before close; nothing was sent")
                later = self._adapter.query(OrdersQuery(now - timedelta(days=3650),
                                                        datetime.now(timezone.utc), statuses=active))
                if not self._stop_orders_clear(later, stop):
                    raise ValueError("Another vertical order appeared; nothing was sent")
            attempted = True
            result = service.execute(SubmitOrder(intent), stop["request_id"], preflight=preflight)
            status = ("sent" if result.accepted else
                      "unknown" if result.status.value == "unknown" else "refused")
            watch.record_send(stop["stop_id"], stop["request_id"], status,
                              result.message or f"Paper close result {result.status.value}")
        except Exception as exc:
            watch.record_send(stop["stop_id"], stop["request_id"],
                              "unknown" if attempted else "refused",
                              (f"Stop close outcome unknown after {type(exc).__name__}; reconcile with Alpaca Paper" if attempted
                               else f"Stop close preflight failed: {type(exc).__name__}; no close was sent"))
        finally:
            lock.release()

    def _spread_stop_rows(self, account, groups, clock, today_session, now) -> list[dict]:
        watch = self.spread_stops
        if watch is None:
            return []
        stops = watch.list()
        active = [stop for stop in stops if stop["status"] == "watching"]
        symbols = tuple(dict.fromkeys(symbol for stop in active
                                      for symbol in (stop["long_symbol"], stop["short_symbol"])))
        quotes = {}
        feed_error = None
        if symbols and clock is not None and clock.is_open:
            if self._opra_retry_at and now < self._opra_retry_at:
                feed_error = "OPRA subscription is unavailable for Paper stop monitoring"
            else:
                try:
                    quotes = self._adapter.query(QuotesQuery(symbols, AssetType.OPTION,
                                                             realtime_required=True))
                    self._opra_retry_at = None
                except Exception as exc:
                    forbidden = getattr(exc, "status_code", None) == 403
                    if forbidden:
                        self._opra_retry_at = now + timedelta(minutes=5)
                    feed_error = ("OPRA subscription is unavailable for Paper stop monitoring" if
                                  forbidden else f"OPRA quote read failed: {read_health(exc)['message']}")
        market_open = bool(clock and clock.is_open and today_session and
                           today_session.is_open and today_session.opens_at <= now < today_session.closes_at)
        rows = []
        for stop in stops:
            if feed_error and stop["status"] == "watching":
                watch.observe(stop["stop_id"], {}, market_open=market_open,
                              pair_matches=True, orders_clear=True,
                              monitoring=True, now=now)
                observation = {"status": "suspended", "reason": feed_error, "mid": None,
                               "fire": False}
            else:
                observation = watch.observe(
                    stop["stop_id"], quotes, market_open=market_open,
                    pair_matches=self._stop_pair_matches(account, stop),
                    orders_clear=self._stop_orders_clear(groups, stop),
                    monitoring=not self._stop.is_set() and
                    (not stop["send"] or bool(self.order_service and self.order_service.armed)),
                    now=now)
            if observation["fire"]:
                self._send_spread_stop(stop)
                current = watch.get(stop["stop_id"])
                observation["status"] = current["status"]
                observation["reason"] = current["result"]
            rows.append({"stop_id": stop["stop_id"], "long_contract": stop["long_symbol"],
                         "short_contract": stop["short_symbol"],
                         "quantity": str(stop["quantity"]), "kind": stop["kind"],
                         "stop_value": str(stop["stop_value"]), "send": stop["send"],
                         "status": observation["status"], "reason": observation["reason"],
                         "mid": observation["mid"], "trigger_at": stop["trigger_at"]})
        return rows

    @staticmethod
    def _auto_be_pair(account, groups, row: dict) -> bool:
        if account.status.upper() != "ACTIVE":
            return False
        positions = [position for position in account.positions if position.symbol == row["symbol"]]
        if len(positions) != 1:
            return False
        active = {OrderStatus.PENDING, OrderStatus.WORKING,
                  OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
        touching = [group for group in groups if any(
            leg.symbol == row["symbol"] for order in group.orders
            if order.status in active for leg in order.legs)]
        if len(touching) != 1:
            return False
        try:
            plan = oco_auto_be_plan(positions[0], touching[0])
        except ValueError:
            return False
        return all(plan[key] == row[key] for key in
                   ("side", "quantity", "basis", "initial_stop", "target", "trigger",
                    "new_stop", "group_id", "stop_id", "target_id", "oco_client_id"))

    def _send_auto_be(self, row: dict) -> None:
        watch = self.auto_be
        lock = self.action_lock
        if lock is None or not lock.acquire(blocking=False):
            watch.record_send(row["symbol"], row["request_id"], "refused",
                              "Another broker action was in progress; no stop was moved")
            return
        attempted = False
        try:
            service = self.order_service
            if (self._stop.is_set() or service is None or service.store is not watch.store or
                    service.store.open_mutations()):
                raise ValueError("Paper runtime or unresolved order changed; no stop was moved")
            now = datetime.now(timezone.utc)
            if not service._market_open(now):
                raise ValueError("Regular market session closed; no stop was moved")
            account = self._adapter.query(AccountQuery())
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = self._adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                     statuses=active))
            if account.account_ref != service.account_ref or not self._auto_be_pair(account, groups, row):
                raise ValueError("Held position or broker-held exits changed; no stop was moved")
            quote = self._adapter.query(QuotesQuery((row["symbol"],), AssetType.EQUITY)).get(row["symbol"])
            if (quote is None or quote.symbol != row["symbol"] or quote.source != "Alpaca IEX" or
                    not quote.realtime or not quote.timestamp_verified or
                    quote.observed_at.tzinfo is None or
                    not 0 <= (now - quote.observed_at).total_seconds() <= 15):
                raise ValueError("Fresh verified IEX quote disappeared; no stop was moved")
            mark = quote.mark or quote.last
            if (mark is None or mark <= 0 or
                    (row["side"] == "long" and
                     (mark < row["trigger"] or mark <= row["new_stop"])) or
                    (row["side"] == "short" and
                     (mark > row["trigger"] or mark >= row["new_stop"]))):
                raise ValueError("IEX mark left the +1R trigger; no stop was moved")
            command = ReplaceOrder(row["stop_id"], stop_price=row["new_stop"],
                                   client_order_id=f"et-autobe-{row['request_id'].replace('-', '')}")

            def preflight(current):
                if current.account_ref != service.account_ref:
                    raise ValueError("Paper account changed; no stop was moved")
                later = self._adapter.query(OrdersQuery(now - timedelta(days=3650),
                                                        datetime.now(timezone.utc),
                                                        statuses=active))
                if not self._auto_be_pair(current, later, row):
                    raise ValueError("Protected exits changed immediately before Auto B/E; nothing was sent")

            attempted = True
            result = service.execute(command, row["request_id"], preflight=preflight)
            status = ("unknown" if result.status.value == "unknown" else
                      "done" if result.accepted else "refused")
            watch.record_send(row["symbol"], row["request_id"], status,
                              result.message or f"Paper stop replacement {result.status.value}")
        except Exception as exc:
            watch.record_send(row["symbol"], row["request_id"],
                              "unknown" if attempted else "refused",
                              (f"Auto B/E outcome unknown after {type(exc).__name__}; reconcile with Alpaca Paper"
                               if attempted else
                               f"Auto B/E preflight failed: {type(exc).__name__}; no stop was moved"))
        finally:
            lock.release()

    def _auto_be_rows(self, account, groups, quotes, clock, today_session, now) -> list[dict]:
        watch = self.auto_be
        if watch is None:
            return []
        market_open = bool(clock and clock.is_open and today_session and
                           today_session.is_open and today_session.opens_at <= now < today_session.closes_at)
        rows = []
        for row in watch.list():
            position = next((p for p in account.positions if p.symbol == row["symbol"]), None)
            observation = watch.observe(
                row["symbol"], position, groups, quotes.get(row["symbol"]),
                market_open=market_open, monitoring=not self._stop.is_set(), now=now)
            if observation["fire"]:
                self._send_auto_be(row)
                current = watch.get(row["symbol"])
                observation["status"] = current["status"]
                observation["reason"] = current["result"]
            rows.append({"symbol": row["symbol"], "side": row["side"],
                         "quantity": str(row["quantity"]), "trigger": str(row["trigger"]),
                         "new_stop": str(row["new_stop"]), "status": observation["status"],
                         "reason": observation["reason"], "mark": observation["mark"],
                         "request_id": row["request_id"]})
        return rows

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                raise RuntimeError("Alpaca Paper polling did not stop; restart before switching brokers")
        for store in self._stores:
            store.close()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _merged_orders(self) -> list[dict]:
        return []

    def note_external_request(self, retry_after=None) -> None:
        pass

    def cooldown_left(self) -> float:
        return max(0.0, (self._cooldown_until - datetime.now(timezone.utc)).total_seconds()) \
            if self._cooldown_until else 0.0
