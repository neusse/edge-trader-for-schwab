"""Read-only Alpaca paper polling while order workflows are migrated."""
from __future__ import annotations

import threading
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .alpaca_paper import AlpacaPaperAdapter
from .canonical_view import build_canonical_state
from .errors import read_health
from .models import (AccountQuery, AssetType, FillsQuery, MarketClockQuery,
                     MarketSessionsQuery, OrdersQuery, OrderStatus, QuotesQuery)
from .order_service import BrokerOrderService
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
        self._last_fill_read: datetime | None = None
        self._sessions_date = None
        self._sessions = ()
        self._cooldown_until: datetime | None = None

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
