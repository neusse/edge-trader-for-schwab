"""The one background poller. REST only (Schwab allows one streamer per user; another app may own it).

Request schedule, per minute (Schwab allows about 120 for everything, shared with any other app
using the same Schwab login):
    account + positions   every 5 s   12
    orders entered today  every 5 s   12
    orders, 59-day sweep  every 30 s   4   (status WORKING + PENDING_ACTIVATION: older
                                            GTC orders; an unfiltered 59-day query times out)
    transactions today    every 10 s   6
    quotes (batched)      every 3 s   20   (open positions and working orders, one request)
    transactions, 3 days  every 10 min     (late corrections to earlier sessions)
    order_details         as needed        (orders that drop out of the working set)
                                     ----
                                     ~54
    ticket quotes (outside this limiter) every 5 s while a ticket window has focus, else every
    30 s: up to ~12 more. Order actions are on top, at most one at a time.
Enforced, not just scheduled: every request goes through the Schwab read policy limiter,
schwabdev's internal retries are disabled (companion/schwab.py), and a 429 pauses
ALL companion requests. Each endpoint keeps its own last-success time.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import schwab
from .pnl import ET, Fill, session_day
from .brokers.canonical_view import _ledger_fills, _pending
from .brokers.models import AccountSnapshot, Quote
from .brokers.schwab_adapter import (canonical_terminal_ids, index_schwab_orders,
                                     linked_account_ref, normalize_account, normalize_fills,
                                     normalize_market_session, normalize_order, normalize_quotes)
from .brokers.models import OrderStatus
from .brokers.schwab_policy import SchwabReadPolicy
from .store import Store, now_iso
from .view import build_state, ticket_pnl

log = logging.getLogger("companion.poller")

class Poller:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.policy = SchwabReadPolicy()
        self.client = None
        self.account_hash: str | None = None
        self.account: dict | None = None
        self.orders_today: dict[int, dict] = {}
        self.orders_sweep: dict[int, dict] = {}
        self.orders_followed: dict[int, dict] = {}   # final state of vanished orders
        self.followed_at: dict[int, float] = {}      # when each followed order was last asked
        self.spread_follow: list[int] = []           # companion spread orders to ask about (private)
        self.quotes: dict = {}
        self.trail_best: dict[int, float] = {}   # trailing stop order id -> best price seen
        self.hours: dict = {}       # today's and the next regular session (Schwab, holidays and early closes)
        self.trader = None   # set by the app; the poller never sends orders (Auto B/E hands its one move to it)
        # Every order id ever seen finished. Schwab's WORKING sweep can return a frozen
        # stand-alone copy of an order that actually filled inside an OCO.
        self.terminal: set[int] = canonical_terminal_ids(
            tuple(normalize_order(row) for row in self.store.all_orders()))
        self.history_complete = False
        self.fresh: dict[str, dict] = {k: {"ok_at": None, "error": None, "interval": v}
                                       for k, v in self.policy.intervals.items()}
        self.fresh["backfill"] = {"ok_at": None, "error": None, "interval": None}
        self.state: dict = {"booting": True, "status": "starting"}
        self.status = "starting"
        self.last_rebuild: str | None = None
        self._next = {k: 0.0 for k in self.policy.intervals}
        self._sent: deque[float] = deque()
        self._cooldown_until = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._auto_threads: list[threading.Thread] = []

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="schwab-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        deadline = time.monotonic() + 20
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise RuntimeError("Schwab polling did not stop; restart before switching brokers")
        for worker in self._auto_threads:
            worker.join(timeout=max(0, deadline - time.monotonic()))
            if worker.is_alive():
                raise RuntimeError("Schwab Auto B/E is still sending; restart before switching brokers")

    def snapshot(self) -> dict:
        with self._lock:
            return self.state

    # ---- requests --------------------------------------------------------

    def _throttle(self) -> None:
        """Wait until the selected broker policy allows another REST request."""
        while not self._stop.is_set():
            now = time.monotonic()
            wait = self.policy.throttle_delay(self._sent, now, self._cooldown_until)
            if wait <= 0:
                self._sent.append(now)
                return
            self._stop.wait(min(wait, 1.0))

    def note_external_request(self, retry_after=None) -> None:
        """Order traffic from the trader: counted in the same per-minute window (without blocking
        an order), and a 429 on it pauses the poller's own requests."""
        self._sent.append(time.monotonic())
        if retry_after is not None:
            self._cooldown_until = time.monotonic() + self.policy.retry_after(str(retry_after))

    def cooldown_left(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _call(self, name: str, fn):
        """One HTTP request. Returns parsed JSON, or None on any failure (never raises)."""
        self._throttle()
        started = now_iso()
        fr = self.fresh.setdefault(name, {"ok_at": None, "error": None, "interval": None})
        try:
            r = fn()
            if r.status_code == 429:
                wait = self.policy.retry_after(r.headers.get("Retry-After"))
                self._cooldown_until = time.monotonic() + wait
                fr["error"] = f"rate limited, all requests paused {wait:.0f}s"
                self.store.log_request(name, started, False, 429, r.text[:300])
                return None
            if r.status_code != 200:
                fr["error"] = f"HTTP {r.status_code}"
                self.store.log_request(name, started, False, r.status_code, r.text[:300])
                return None
            data = r.json() if r.content else None
        except Exception as e:  # network, auth, permission, bad JSON
            fr["error"] = repr(e)[:200]
            self.store.log_request(name, started, False, None, repr(e)[:300])
            log.warning("%s failed: %r", name, e)
            return None
        self.store.log_request(name, started, True, 200, None)
        return data

    def _ok(self, name: str) -> None:
        """Mark an endpoint fresh only after its data was validated and stored."""
        self.fresh[name]["ok_at"] = now_iso()
        self.fresh[name]["error"] = None

    def _connect(self) -> bool:
        try:
            client = schwab.make_client()
        except Exception as e:
            self.status = f"auth: {e}"
            return False
        self.client = client
        accts = self._call("account", lambda: client.linked_accounts())
        if isinstance(accts, list) and len(accts) > 1:
            log.warning("%d linked accounts: the companion uses the first one", len(accts))
        try:
            account_hash = linked_account_ref(accts)
        except ValueError:
            self.client = None   # retry discovery next time; never poll without an account
            self.status = "could not list linked accounts"
            return False
        try:
            self.store.bind_scope("schwab", account_hash)
        except ValueError as exc:
            self.client = None
            self.status = f"account storage: {exc}"
            return False
        self.account_hash = account_hash
        if self.trader is not None:
            self.trader.expected_hash = self.account_hash      # orders only to the account shown
        return True

    def _fetch_transactions(self, name: str, start: datetime, end: datetime) -> bool:
        """Store complete TRADE history or leave freshness stale when a response hits its cap."""
        try:
            data = self.policy.complete_transactions(
                lambda a, b: self._call(name, lambda: self.client.transactions(self.account_hash, a, b, "TRADE")),
                start, end)
        except ValueError as exc:
            self.fresh[name]["error"] = str(exc)
            return False
        self.store.save_transactions(data)
        return True

    def _backfill(self) -> bool:
        end = datetime.now(timezone.utc)
        t0 = end - timedelta(days=self.policy.lookback_days)
        while t0 < end and not self._stop.is_set():
            t1 = min(t0 + timedelta(days=10), end)
            if not self._fetch_transactions("backfill", t0, t1):
                self.fresh["backfill"]["error"] = "backfill incomplete, retrying"
                return False
            t0 = t1
        self._ok("backfill")
        return True

    def _session_start_utc(self) -> datetime:
        # Futures session for "today" opened at 18:00 ET yesterday; equities are inside it.
        et_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return (et_midnight - timedelta(hours=6)).astimezone(timezone.utc)

    def _poll(self, name: str) -> bool:
        h = self.account_hash
        now = datetime.now(timezone.utc)
        if name == "account":
            data = self._call(name, lambda: self.client.account_details(h, fields="positions"))
            if not isinstance(data, dict):
                return False
            try:
                canonical = normalize_account(data, h)
            except (KeyError, TypeError, ValueError):
                self.fresh[name]["error"] = "account normalization incomplete"
                return False
            self.account = data
            self.store.save_positions(data)
            self._record_prev_session(canonical)
        elif name == "orders_today":
            data = self._call(name, lambda: self.client.account_orders(h, self._session_start_utc(), now))
            if not isinstance(data, list):
                return False
            try:
                indexed, groups = index_schwab_orders(data)
            except (KeyError, TypeError, ValueError):
                self.fresh[name]["error"] = "order normalization incomplete"
                return False
            self.store.save_orders(data)
            self.terminal |= canonical_terminal_ids(groups)
            self.orders_today = indexed
        elif name == "orders_sweep":
            found: dict[int, dict] = {}
            for status in self.policy.sweep_statuses:
                data = self._call(name, lambda st=status: self.client.account_orders(
                    h, now - timedelta(days=self.policy.order_sweep_days), now, status=st))
                if not isinstance(data, list):
                    return False
                try:
                    indexed, groups = index_schwab_orders(data)
                except (KeyError, TypeError, ValueError):
                    self.fresh[name]["error"] = "order normalization incomplete"
                    return False
                found.update(indexed)
                self.terminal |= canonical_terminal_ids(groups)
            self.store.save_orders(list(found.values()))
            vanished = [oid for oid in self.orders_sweep
                        if oid not in found and oid not in self.orders_today]
            self.orders_sweep = found
            # Orders followed earlier while still live are asked again, oldest first, so a stop that
            # was cancelled later never keeps counting as protection.
            stale = sorted((oid for oid in self.orders_followed if oid not in found and oid not in self.orders_today),
                           key=lambda o: self.followed_at.get(o, 0.0))      # least recently asked first
            # A companion spread's own orders come first: their final fills settle the spread.
            spread = [o for o in self.spread_follow if o not in found and o not in self.orders_today]
            self._follow(list(dict.fromkeys(spread + vanished + stale))[:self.policy.details_per_sweep])
        elif name == "transactions":
            if not self._fetch_transactions(name, self._session_start_utc() - timedelta(hours=1), now):
                return False
        elif name == "hours":
            if not self._fetch_hours(name, now):
                return False
        elif name == "corrections":
            if not self._fetch_transactions(name, now - timedelta(days=3), now):
                return False
        elif name == "quotes":
            syms = self._quote_symbols()
            if syms:
                data = self._call(name, lambda: self.client.quotes(syms))
                if not isinstance(data, dict):
                    return False
                self.quotes = data
                self.store.save_quotes({k: v for k, v in data.items() if isinstance(v, dict)})
        self._ok(name)
        return True

    def _follow(self, order_ids: list[int]) -> None:
        """Orders that left the working set: fetch their final state once."""
        for oid in order_ids:
            self.followed_at[oid] = time.monotonic()
            data = self._call("order_details", lambda o=oid: self.client.order_details(self.account_hash, o))
            if isinstance(data, dict):
                try:
                    group = normalize_order(data)
                except (KeyError, TypeError, ValueError):
                    self.fresh["order_details"]["error"] = "order normalization incomplete"
                    continue
                if group.group_id != str(oid):
                    self.fresh["order_details"]["error"] = "order lookup returned another ID"
                    continue
                self.store.save_orders([data])
                self.terminal |= canonical_terminal_ids((group,))
                if group.status not in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                        OrderStatus.REJECTED, OrderStatus.EXPIRED,
                                        OrderStatus.REPLACED):
                    self.orders_followed[oid] = data
                else:
                    self.orders_followed.pop(oid, None)

    def _record_prev_session(self, account: AccountSnapshot) -> None:
        day = session_day(datetime.now(timezone.utc), "EQUITY").isoformat()
        rows: dict[str, tuple[float, float | None]] = {}
        for position in account.previous_session_positions:
            rows[position.symbol] = (float(position.quantity),
                                     float(position.average_price) if position.average_price else None)
        if rows:
            self.store.save_prev_session(day, rows)

    def _quote_symbols(self) -> list[str]:
        account = normalize_account(self.account, self.account_hash) if self.account else None
        groups = tuple(normalize_order(order) for order in self._merged_orders())
        syms = {position.symbol for position in account.positions} if account else set()
        syms.update(order.symbol for order in _pending(groups, self.terminal))
        return sorted(syms)[:200]

    def _merged_orders(self) -> list[dict]:
        merged = dict(self.orders_followed)
        merged.update(self.orders_sweep)
        merged.update(self.orders_today)   # the 5 s call is fresher
        return list(merged.values())

    def _fresh(self, name: str) -> bool:
        """Last call succeeded and is younger than 3 polling intervals."""
        f = self.fresh[name]
        if f["error"] or not f["ok_at"]:
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(f["ok_at"])).total_seconds()
        return age <= f["interval"] * 3

    def _regular_session(self) -> bool:
        """Inside today's regular session: Schwab's own hours when known (holidays, early closes),
        else the plain 9:30 to 16:00 weekday rule. Same answer the trader's order checks get."""
        from .trading import market_open
        return market_open()

    def _fetch_hours(self, name: str, now: datetime) -> bool:
        """Today's regular session and the next one that has not ended (up to a week ahead, so a
        weekend or holiday costs a few calls every 30 min)."""
        today = now.astimezone(ET).date()
        out: dict = {"today": None, "next": None}
        for i in range(7):
            d = today + timedelta(days=i)
            data = self._call(name, lambda d=d: self.client.market_hours(["equity"], date=d.isoformat()))
            if not isinstance(data, dict):
                return False
            try:
                session = normalize_market_session(data, d)
            except ValueError:
                if hasattr(self, "fresh"):
                    self.fresh[name]["error"] = "market hours response incomplete"
                return False
            sess = ({"date": d.isoformat(), "open": session.opens_at.isoformat(),
                     "close": session.closes_at.isoformat()} if session.is_open else None)
            if i == 0:
                out["today"] = sess
                from .trading import set_market_hours
                set_market_hours(today.isoformat(), sess)   # the order checks get today even if the next-day read fails
            if session.closes_at and session.closes_at > now:
                out["next"] = sess
                break
        self.hours = out
        return True

    def _orders_fresh(self) -> bool:
        return self._fresh("orders_today") and self._fresh("orders_sweep")

    # ---- loop ------------------------------------------------------------

    def _rebuild(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=self.policy.lookback_days)).strftime("%Y-%m-%dT%H:%M:%S")
        day = session_day(datetime.now(timezone.utc), "EQUITY").isoformat()
        txns = self.store.transactions_since(since)
        fills = _ledger_fills(normalize_fills(txns))
        merged = self._merged_orders()
        try:
            groups = tuple(normalize_order(order) for order in merged)
            pending = _pending(groups, self.terminal)
            orders_fresh = self._orders_fresh()
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("canonical Schwab order normalization failed: %r", exc)
            groups = ()
            pending = []
            orders_fresh = False
            self.fresh["orders_sweep"]["error"] = "order normalization incomplete"
        try:
            account = normalize_account(self.account, self.account_hash) if self.account else None
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("canonical Schwab account normalization failed: %r", exc)
            account = None
            self.fresh["account"]["error"] = "account normalization incomplete"
        try:
            quotes = normalize_quotes(self.quotes, tuple(self.quotes))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("canonical Schwab quote normalization failed: %r", exc)
            quotes = {}
            self.fresh["quotes"]["error"] = "quote normalization incomplete"
        st = build_state(account=account, groups=groups, pending=pending,
                         fills=fills, quotes=quotes,
                         prev_session=self.store.load_prev_session(day),
                         freshness=self.fresh, history_complete=self.history_complete,
                         orders_fresh=orders_fresh, auth_hours_left=schwab.auth_hours_left())
        self.last_rebuild = now_iso()
        st["status"] = self.status
        st["last_rebuild"] = self.last_rebuild
        st["market_hours"] = self.hours
        if st.get("summary") and not self._fresh("transactions"):
            st["summary"]["realized"]["complete"] = False
            st["summary"]["realized"]["stale"] = True
        if self.trader is not None:
            today = list(self.orders_today.values())
            self.trader.reconcile(today)
            positions = {position.symbol: float(position.quantity) *
                         (1 if position.side.value == "long" else -1)
                         for position in account.positions} if account else {}
            if self.trader.spreads and getattr(self.trader, "options_trading_enabled", True):
                # Companion spreads: persist fills, judge each one against the account, show them.
                fresh = orders_fresh and self._fresh("account") and account is not None
                merged = self._merged_orders()
                st["spreads"] = [self.trader.spreads.view(sp) for sp in self.trader.spreads.reconcile(
                                     merged, positions if fresh else None, datetime.now(ET).date(), fresh,
                                     observation=(self.fresh["account"]["ok_at"],
                                                  self.fresh["orders_sweep"]["ok_at"]))]
                self.spread_follow = self.trader.spreads.to_follow(merged)
                st["spreads"] = self.trader.spreads.watch_stops(st["spreads"], self.quotes, self._regular_session())
                midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
                st["spreads_closed"] = [self.trader.spreads.view(sp) for sp in self.trader.spreads.closed_since(
                    midnight.astimezone(timezone.utc).isoformat())]
                # The journal keys a spread's entry on its own fills (open workbench and Closed today row).
                self.trader.spreads.attach_fill_ids(st["spreads"] + st["spreads_closed"])
            st["trading"] = self.trader.status()
            st["brackets"] = self.trader.brackets(
                today, pending, positions, orders_fresh and self._fresh("account"))
            ticket_pnl(st["brackets"], st.get("positions") or [], st.get("closed") or [])
            self._add_r_and_log(st, today, fills)
            self._auto_be(st, quotes)
        self._trail_estimates(st, quotes)
        with self._lock:
            self.state = st

    def _trail_estimates(self, st: dict, quotes: dict[str, Quote] | None = None) -> None:
        """Where each Schwab trailing stop sits now. Schwab's own stop price when it reports one;
        otherwise the best mark seen since we first saw the order, minus the offset. Seen from
        polls only, so the estimate never runs ahead of the real stop (it can lag behind it)."""
        from decimal import Decimal
        from .bracket import ticks_for, trail_level
        quotes = quotes if quotes is not None else normalize_quotes(self.quotes, tuple(self.quotes))
        live_ids = set()
        for p in st.get("positions") or []:
            t = p.get("trail")
            if not t:
                continue
            live_ids.add(t["order_id"])
            quote = quotes.get(p["symbol"])
            basis = (t.get("basis") or "MARK").upper()
            px = ({"MARK": quote.mark or quote.last, "LAST": quote.last,
                   "BID": quote.bid, "ASK": quote.ask}.get(basis) if quote else None)
            # A quote from before the order was entered could be a better price the trail never
            # saw: only quotes stamped after entry count.
            qt = (quote.last_observed_at if basis == "LAST" else
                  quote.observed_at if quote.timestamp_verified else None) if quote else None
            try:
                entered = datetime.fromisoformat((t.get("entered") or "").replace("Z", "+00:00"))
            except ValueError:
                entered = None
            if entered is None or entered.tzinfo is None or qt is None or qt < entered:
                px = None
            best = self.trail_best.get(t["order_id"])
            if px is not None:
                px = float(px)
                better = best is None or (px > best if p["side"] == "LONG" else px < best)
                best = px if better else best
                self.trail_best[t["order_id"]] = best
            if t.get("schwab_stop") is not None:
                t.update({"level": t["schwab_stop"], "estimated": False, "best": best})
            elif best and t.get("offset") and t.get("whole"):
                spec = {"offset": Decimal(str(t["offset"])), "link": t.get("link") or "VALUE"}
                pen = quote.penny_pilot if quote else None
                tk = ticks_for(p.get("asset_type"), p.get("underlying"), pen if isinstance(pen, bool) else None)
                lvl = float(trail_level(Decimal(str(best)), spec, p["side"], tk))
                t.update({"level": lvl, "estimated": True, "best": best})
                p["stop_price"] = lvl          # ruler and P&L at stop use the estimate, flagged
                if p.get("avg_price"):
                    d = 1 if p["side"] == "LONG" else -1
                    p["pnl_at_stop"] = (lvl - p["avg_price"]) * p["qty"] * d * (p.get("multiplier") or 1)
        for oid in list(self.trail_best):
            if oid not in live_ids:
                del self.trail_best[oid]

    def _auto_be(self, st: dict, quotes: dict[str, Quote] | None = None) -> None:
        """Tickets sent with Auto B/E: show where each one fires, and when the mark touches +1R
        (frozen R of the ticket that opened the position) claim it and hand the one stop move to the
        trader in its own thread. The poller itself never waits on an order."""
        from .trading import market_open
        quotes = quotes if quotes is not None else normalize_quotes(self.quotes, tuple(self.quotes))
        live = (self._fresh("quotes") and self._fresh("account") and self._fresh("transactions")
                and self._orders_fresh() and market_open())
        now = datetime.now(timezone.utc)
        for p in st.get("positions") or []:
            if p.get("asset_type") == "OPTION" and not getattr(self.trader, "options_trading_enabled", True):
                continue
            r = p.get("r")
            ab = self.trader.store.auto_be(r["ticket_id"]) if r else None
            if not ab:
                continue
            long = p["side"] == "LONG"
            trigger = round(r["entry"] + r["rps"] if long else r["entry"] - r["rps"], 4)
            p["auto_be"] = {"state": ab["state"], "trigger": trigger, "detail": ab["detail"]}
            if ab["state"] != "PENDING" or not live:
                continue
            quote = quotes.get(p["symbol"])
            if quote is None or not quote.realtime or not quote.timestamp_verified:
                continue
            mark = float(quote.mark) if quote.mark is not None else None
            try:   # only a real-time quote from after the entry fill and under 15 s old counts
                filled = datetime.fromisoformat((r.get("first_fill") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            age = (now - quote.observed_at).total_seconds()
            if mark is None or filled.tzinfo is None or quote.observed_at < filled or not 0 <= age <= 15:
                continue
            if mark < trigger - 1e-9 if long else mark > trigger + 1e-9:
                continue
            if self._stop.is_set():
                return
            if not self.trader.store.set_auto_be(r["ticket_id"], "SENDING", f"mark {mark:g} touched +1R {trigger:g}",
                                                 only_from="PENDING"):
                continue
            p["auto_be"]["state"] = "SENDING"
            worker = threading.Thread(target=self.trader.auto_breakeven,
                                      name=f"auto-be-{p['symbol']}", daemon=True,
                                      args=(r["ticket_id"], p["symbol"], p["side"], r["entry"],
                                            self._merged_orders(), set(self.terminal)))
            self._auto_threads.append(worker)
            worker.start()

    def _add_r_and_log(self, st: dict, orders_today: list[dict], fills: list[Fill]) -> None:
        """Live R on each position (frozen basis from the companion ticket) and a per-symbol
        action log: tickets sent, cancels/closes requested and confirmed, and fills."""
        basis = self.trader.r_basis(orders_today)

        def owns(b: dict, p: dict) -> bool:
            # The ticket's entry fill must be what opened the CURRENT position (same flat-to-flat
            # trade). A later reopen in thinkorswim must not inherit an old trade's R.
            if not (b.get("first_fill") and p.get("opened")):
                return False
            try:
                a = datetime.strptime(b["first_fill"], "%Y-%m-%dT%H:%M:%S%z")
                o = datetime.fromisoformat(p["opened"])
            except ValueError:
                return False
            return abs((a - o).total_seconds()) <= 5

        resets = self.trader.store.r_resets()
        for p in st.get("positions") or []:
            top = basis.get(p["symbol"])
            b = next((c for c in ([top, *top["alts"]] if top else [])
                      if c["side"] == p["side"] and owns(c, p)), None)
            # Add Shares recalculated 1R for this position (same symbol, side and open time).
            rs = resets.get((p["symbol"], p["side"], p.get("opened"))) if p.get("opened") else None
            if rs:
                rps = abs(rs["entry"] - rs["stop"]) if rs["stop"] is not None else 0
                b = ({"side": p["side"], "entry": rs["entry"], "stop": rs["stop"], "rps": rps, "added": True,
                      "ticket_id": (b or {}).get("ticket_id"), "first_fill": (b or {}).get("first_fill") or p["opened"]}
                     if rps > 0 else None)
            if b and p.get("last"):
                direction = 1 if p["side"] == "LONG" else -1
                b = {k: v for k, v in b.items() if k != "alts"}
                p["r"] = {**b, "live_r": (float(p["last"]) - b["entry"]) * direction / b["rps"]}
            else:
                p["r"] = None
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        log_by: dict[str, list[dict]] = {}

        def add(sym, when, text, kind):
            log_by.setdefault(sym, []).append({"t": when, "text": text, "kind": kind})

        for t in self.trader.store.tickets_since(start.isoformat()):
            add(t["symbol"], t["created"], f"Ticket {t['side']} {t['qty']} @ {t['limit_price']} "
                f"stop {t['stop']} target {t['target']}: {t['status']}", "ticket")
        for a in self.store.actions_since(start.isoformat()):
            if a["symbol"]:
                kind = "auto B/E" if a["kind"] == "auto_be" else a["kind"].replace("_", " ")
                add(a["symbol"], a["created"], f"{kind} "
                    f"{a['order_id'] or ''}: {a['status']} {a['detail'] or ''}".strip(), "action")
        today = session_day(datetime.now(timezone.utc), "EQUITY")
        for f in fills:
            if f.asset_type != "FUTURE" and f.day == today:
                add(f.symbol, f.time.isoformat(), f"Fill {'bought' if f.qty > 0 else 'sold'} "
                    f"{abs(f.qty):g} @ {f.price:g}", "fill")
        for rows in log_by.values():
            rows.sort(key=lambda r: r["t"])
        st["action_log"] = log_by

    def _run(self) -> None:
        backfill_next = 0.0
        while not self._stop.is_set():
            try:
                if self.client is None:
                    if not self._connect():
                        self._publish_status()
                        self._stop.wait(30)
                        continue
                    self.status = "loading 60 days of fills"
                    self._publish_status()

                if not self.history_complete and time.monotonic() >= backfill_next:
                    self.history_complete = self._backfill()
                    backfill_next = time.monotonic() + 60
                self.status = "live" if self.history_complete else "live, fill history incomplete"

                left = schwab.auth_hours_left()
                if left is None or left < schwab.SAFETY_HOURS:
                    # Keep serving cached data, clearly stale; never trigger an interactive login.
                    self.status = "Schwab login expiring: run login.bat (python -m companion.login)"
                    self.client = None
                    self._publish_status()
                    self._stop.wait(60)
                    continue

                changed = False
                for name, every in self.policy.intervals.items():
                    if time.monotonic() >= self._next[name]:
                        self._next[name] = time.monotonic() + every
                        try:
                            changed |= self._poll(name)
                        except Exception as e:
                            self.fresh[name]["error"] = repr(e)[:200]
                            log.exception("poll %s failed", name)
                if changed:
                    try:
                        self._rebuild()
                    except Exception as e:
                        log.exception("rebuild failed")
                        self.status = f"display not updating: {e!r}"[:200]
                        self._publish_status()
            except Exception as e:   # the thread must never die silently
                log.exception("poller loop error")
                self.status = f"poller error: {e!r}"[:200]
                self._publish_status()
                self._stop.wait(5)
            self._stop.wait(0.5)

    def _publish_status(self) -> None:
        with self._lock:
            st = dict(self.state)
            st["status"] = self.status
            st["auth_hours_left"] = schwab.auth_hours_left()
            st["freshness"] = self.fresh
            st["last_rebuild"] = self.last_rebuild
            self.state = st
