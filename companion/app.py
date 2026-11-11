"""FastAPI server: serves the dashboard, the cached state, and the order ticket.

    python -m companion            (http://127.0.0.1:8770)

GET endpoints never call Schwab per request (cached state), except the ticket's plan quote.
POST endpoints (ticket) require the X-Companion header and a same-origin request, so another
web page open in the browser cannot place orders through this server.
"""
from __future__ import annotations

import json
import asyncio
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .brokers.capabilities import BROKERS
from .brokers.configuration import BrokerSelection
from .brokers.bootstrap import AlpacaBootstrapPoller
from .brokers.models import (AccountQuery, AssetType, BracketOrderIntent, CancelOrder,
                             OrderClass, OrderInstruction, OrderQuery, OrderRole,
                             OrderStatus, OrderType, OrdersQuery, QuotesQuery, ReplaceOrder,
                             SubmitOrder)
from .brokers.paper_equity import (add_plan, close_plan, entry_plan, oco_exit_reprice,
                                   oco_plan, trailing_plan)
from .brokers.paper_auto_be import oco_auto_be_plan
from .brokers.paper_options import (chain_query, chain_response, expirations_query,
                                    expirations_response, long_entry_plan,
                                    option_close_plan, option_fixed_exit_plan,
                                    short_entry_plan, covered_call_reserved_shares, OCC)
from .brokers.paper_vertical import vertical_close_plan, vertical_entry_plan
from .poller import Poller
from .trading import OCC_RE, Trader
from .view import private_view

STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("COMPANION_PORT", "8770"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
DEMO_OVERRIDE = os.environ.get("EDGE_TRADER_DEMO") == "1"
BROKER_KEY = "demo" if DEMO_OVERRIDE else BrokerSelection().get()
DEMO = BROKER_KEY == "demo"     # synthetic data, no broker calls or orders
PRIVATE = os.environ.get("EDGE_TRADER_PRIVATE") == "1"   # hide carried positions and account value


class DemoPoller:
    """Stands in for the poller in demo mode: synthetic state, never touches Schwab."""

    def __init__(self):
        from .store import Store
        self.store = Store(Path(__file__).resolve().parent.parent / "data" / "demo.db")
        self.terminal, self.orders_today, self.trader = set(), {}, None

    def snapshot(self) -> dict:
        from .demo import demo_state
        st = demo_state()
        if self.trader is not None:
            st["trading"] = {**self.trader.status(), "market_open": True}
        return st

    def start(self): pass
    def stop(self): self.store.close()
    def _merged_orders(self): return []
    def note_external_request(self, retry_after=None): pass
    def cooldown_left(self): return 0.0


POLLERS = {"demo": DemoPoller, "schwab": Poller, "alpaca-paper": AlpacaBootstrapPoller}
BROKER_ACTION_LOCK = threading.RLock()
BROKER_REQUEST_LOCK = asyncio.Lock()
APP_WORKFLOWS = {
    "schwab": ("schwab-ticket", "schwab-spreads"),
    "alpaca-paper": ("equity-bracket", "equity-simple", "equity-oco", "equity-close", "equity-partial",
                     "equity-oco-adjust", "equity-auto-be", "equity-add", "equity-trailing",
                     "option-long-entry", "option-close", "option-cancel",
                     "option-fixed-exit", "option-covered-short", "vertical-entry",
                     "vertical-close", "vertical-target", "vertical-cancel",
                     "vertical-target-reprice", "vertical-stop"),
    "demo": (),
}
BROKER_HELP = {
    "schwab": "Schwab ticket, position management, and spreads are available when account data is fresh.",
    "alpaca-paper": "Paper equity bracket and held-position management are available. An existing app-created OCO can opt into app-watched Auto B/E with fresh IEX quotes. A level-2-or-higher Paper account can open a long call or put; level 1 can sell a covered call or cash-secured put. Level 3 can open a defined-risk vertical with one multi-leg order; an exact held vertical can be closed or given one broker-held target. Held options can be closed or given one fixed stop, stop-limit, or target. Spread stops are app-watched and require fresh OPRA option quotes; the indicative feed never triggers one.",
    "demo": "Invented demo data only. No broker orders can be sent or changed.",
}


def broker_info(key: str) -> dict:
    broker = BROKERS[key]
    workflows = tuple(workflow for workflow in APP_WORKFLOWS[key]
                      if workflow != "schwab-spreads" or globals().get("_feature_routes"))
    return {"key": broker.key, "name": broker.display_name, "mode": broker.mode.value,
            "capabilities": sorted(c.value for c in broker.capabilities.supported),
            "unsupported": {c.value: reason for c, reason in broker.capabilities.unsupported.items()},
            "workflows": workflows, "help": BROKER_HELP[key]}


def make_poller(broker_key: str):
    """Select exactly one broker runtime without falling back to another broker."""
    if broker_key not in BROKERS:
        raise ValueError("unknown broker runtime")
    selected = POLLERS[broker_key]()
    if isinstance(selected, AlpacaBootstrapPoller):
        selected.action_lock = BROKER_ACTION_LOCK
    return selected


poller = make_poller(BROKER_KEY)
trader = Trader(poller.store)
poller.trader = trader
trader.on_request = poller.note_external_request
trader.cooldown_left = poller.cooldown_left
if DEMO:      # the ticket plans on invented quotes and an invented option chain; nothing reaches Schwab
    from . import demo as _demo
    trader.quote, trader.expirations, trader.chain = _demo.demo_quote, _demo.demo_expirations, _demo.demo_chain


@asynccontextmanager
async def lifespan(_app: FastAPI):
    trader.arm(False, reason="startup")   # arming never survives a restart
    poller.start()
    if not DEMO:
        import threading
        from . import update_check
        threading.Thread(target=lambda: UPDATE.update({"info": update_check.check()}), daemon=True).start()
    yield
    poller.stop()


app = FastAPI(title="Edge Trader", lifespan=lifespan, docs_url=None, redoc_url=None)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def only_local_host(request: Request, call_next):
    """A page on another domain that resolves to 127.0.0.1 (DNS rebinding) sends its own Host
    header: refuse it, so no other site can read positions or balances."""
    if request.headers.get("host") not in HOSTS:
        return JSONResponse({"error": "unexpected host"}, status_code=403)
    broker_mutation = request.url.path.startswith(
        ("/api/ticket/", "/api/position/", "/api/spread/", "/api/paper/"))
    if (request.method == "POST" and broker_mutation and not DEMO_OVERRIDE and
            BrokerSelection().get() != BROKER_KEY):
        return JSONResponse({"ok": False,
                             "error": "Broker selection changed; restart before any order action"},
                            status_code=409)
    demo_mutation = DEMO and (request.url.path.startswith("/api/position/") or
                              request.url.path in ("/api/ticket/submit", "/api/ticket/cancel",
                                                       "/api/ticket/cancel_entries", "/api/ticket/arm",
                                                       "/api/ticket/preview"))
    alpaca_unavailable = BROKER_KEY == "alpaca-paper" and request.url.path.startswith(
        ("/api/position/", "/api/ticket/", "/api/spread/"))
    if request.method == "POST" and (demo_mutation or alpaca_unavailable):
        reason = ("Demo mode: no orders and no Schwab calls." if DEMO else
                  "Alpaca Paper order and ticket integration is not ready yet; no order was sent.")
        return JSONResponse({"ok": False, "error": reason})
    runtime_dependent = (request.method == "POST" and broker_mutation or
                         request.url.path in ("/api/state", "/api/brokers", "/api/ui/prefs",
                                              "/api/paper/status",
                                              "/api/auth/login", "/api/floating/open"))
    if runtime_dependent:
        async with BROKER_REQUEST_LOCK:
            return await call_next(request)
    return await call_next(request)


def _guard(request: Request) -> None:
    if request.headers.get("x-companion") != "1":
        raise HTTPException(403, "missing X-Companion header")
    origin = request.headers.get("origin")
    if origin and origin not in ORIGINS:
        raise HTTPException(403, "cross-origin request refused")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


LOGIN_PROC = None


@app.post("/api/auth/login")
def auth_login(request: Request):
    """Open the Schwab sign-in (companion.login --reauth) in its own console window, on Simon's click.
    A fixed command, nothing from the request is passed on. The new tokens go to the shared tokens.db,
    which the running poller and the Scanner both read. Never opens a streamer."""
    global LOGIN_PROC
    _guard(request)
    if BROKER_KEY == "alpaca-paper":
        return {"ok": False, "error": "Alpaca Paper uses environment credentials; no browser login is needed."}
    if DEMO:
        return {"ok": False, "error": "Demo mode: no Schwab sign-in."}
    if LOGIN_PROC is not None and LOGIN_PROC.poll() is None:
        return {"ok": False, "error": "The sign-in window is already open."}
    import subprocess
    import sys
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")          # the windowless server needs a console for the prompt
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        LOGIN_PROC = subprocess.Popen([str(exe), "-m", "companion.login", "--reauth", "--pause"],
                                      cwd=str(Path(__file__).resolve().parent.parent), creationflags=flags)
    except OSError as exc:
        return {"ok": False, "error": f"Could not open the sign-in window: {exc}"}
    return {"ok": True, "message": "Sign-in window opened. Approve in the browser, then paste the address there."}


@app.get("/cockpit.js", include_in_schema=False)
def cockpit_js():
    """The Cockpit layout (a switch in the header; Classic stays the default)."""
    return FileResponse(STATIC / "cockpit.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/cockpit.css", include_in_schema=False)
def cockpit_css():
    return FileResponse(STATIC / "cockpit.css", media_type="text/css", headers={"Cache-Control": "no-store"})


ICON = Path(__file__).resolve().parent.parent / "assets" / "companion.ico"


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Same candlestick icon as the desktop shortcut, so the tab is easy to spot."""
    return FileResponse(ICON, media_type="image/x-icon", headers={"Cache-Control": "max-age=86400"})


@app.get("/icon.png", include_in_schema=False)
def icon_png():
    return FileResponse(ICON.with_name("companion-64.png"), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


# Shared by the main page and the floating windows (they run in different browsers, so
# localStorage is not shared): the selected position and the UI preferences (hotkeys, partial mode).
UI = {"sel": None, "sel_seq": 0, "prefs_rev": 0}
UPDATE: dict = {"info": None}        # a newer release on GitHub, if any (notify only)


@app.get("/api/state")
def state():
    snap = poller.snapshot()
    if PRIVATE:
        snap = private_view(snap)
    selected_broker = BROKER_KEY if DEMO_OVERRIDE else BrokerSelection().get()
    return JSONResponse({**snap, "ui": UI, "update": UPDATE["info"], "demo": DEMO,
                         "broker": broker_info(BROKER_KEY),
                         "pending_broker": selected_broker if selected_broker != BROKER_KEY else None,
                         "version": __version__, "features": FEATURES}, headers={"Cache-Control": "no-store"})


@app.get("/api/brokers")
def brokers():
    return {"active": BROKER_KEY, "selected": BROKER_KEY if DEMO_OVERRIDE else BrokerSelection().get(),
            "brokers": [broker_info(key) for key in BROKERS]}


@app.post("/api/brokers/select")
async def broker_select(request: Request):
    _guard(request)
    if DEMO_OVERRIDE:
        return {"ok": False, "error": "Demo mode is fixed for this run; restart without --demo to switch brokers."}
    key = (await request.json()).get("broker")
    if not isinstance(key, str) or key not in BROKERS:
        raise HTTPException(400, "unknown broker selection")
    async with BROKER_REQUEST_LOCK:
        try:
            await run_in_threadpool(_switch_broker, key)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc),
                                 "restart_required": True}, status_code=503)
    return {"ok": True, "active": BROKER_KEY, "selected": key,
            "restart_required": False}


def _switch_broker(key: str) -> None:
    global BROKER_KEY, DEMO, poller, trader
    with BROKER_ACTION_LOCK:
        trader.arm(False, reason="broker selection")
        if isinstance(poller, AlpacaBootstrapPoller) and poller.order_service is not None:
            poller.order_service.arm(False)
        if key == BROKER_KEY:
            return
        # Persist first: an interrupted stop leaves a safe, disarmed runtime that
        # activates the requested broker on process restart.
        BrokerSelection().set(key)
        old_poller = poller
        old_poller.stop()
        if isinstance(old_poller, Poller):
            old_poller.store.close()
        next_poller = make_poller(key)
        next_trader = Trader(next_poller.store)
        next_poller.trader = next_trader
        next_trader.on_request = next_poller.note_external_request
        next_trader.cooldown_left = next_poller.cooldown_left
        next_trader.arm(False, reason="broker selection")
        if key == "demo":
            from . import demo as _demo
            next_trader.quote = _demo.demo_quote
            next_trader.expirations = _demo.demo_expirations
            next_trader.chain = _demo.demo_chain
        if _feature_routes and hasattr(app.state, "bind_spread_runtime"):
            app.state.bind_spread_runtime(next_trader, next_poller, key == "demo")
        poller, trader = next_poller, next_trader
        BROKER_KEY, DEMO = key, key == "demo"
        UI["sel"] = None
        UI["sel_seq"] += 1
        next_poller.start()


def _paper_service():
    if (BROKER_KEY != "alpaca-paper" or BrokerSelection().get() != BROKER_KEY or
            not isinstance(poller, AlpacaBootstrapPoller) or poller.order_service is None):
        raise HTTPException(409, "Alpaca Paper is not the active selected broker")
    snap = poller.snapshot()
    account = snap.get("alpaca") or {}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(account["updated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = float("inf")
    if not snap.get("broker_ready") or not 0 <= age <= 30 or account.get("account_status", "").upper() != "ACTIVE":
        raise HTTPException(409, "Alpaca Paper account data is not fresh and ready")
    return poller.order_service, snap


@app.post("/api/paper/options/chain")
async def paper_option_chain(request: Request):
    """Discover exact-expiry OCC contracts; quotes are indicative and never arm an order."""
    _guard(request)
    try:
        body = await request.json()
        query = chain_query(body)
        def fetch():
            service, _ = _paper_service()
            return chain_response(query, service.adapter.query(query))
        return await run_in_threadpool(fetch)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option discovery failed; no order was sent"}


@app.post("/api/paper/options/expirations")
async def paper_option_expirations(request: Request):
    """List currently active Paper contract expirations in a bounded date range."""
    _guard(request)
    try:
        query = expirations_query(await request.json())
        def fetch():
            service, _ = _paper_service()
            return expirations_response(query, service.adapter.query(query))
        return await run_in_threadpool(fetch)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper expiration lookup failed; no order was sent"}


def _paper_option_long_entry(body: dict):
    service, _ = _paper_service()
    query = chain_query(body)
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref:
        raise ValueError("Alpaca Paper account changed; no option order was sent")
    symbol = body.get("contract")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    if any(symbol == leg.symbol.replace(" ", "") for group in groups
           for order in group.orders if order.status in active for leg in order.legs):
        raise ValueError("an active order already touches this option contract")
    chain = service.adapter.query(query)
    intent = long_entry_plan(body, account, chain)
    contract = next(contract for contract in chain.contracts
                    if contract.symbol.replace(" ", "") == intent.leg.symbol)
    return service, intent, contract


@app.post("/api/paper/options/long/preview")
async def paper_option_long_preview(request: Request):
    _guard(request)
    try:
        _, intent, contract = await run_in_threadpool(_paper_option_long_entry,
                                                       await request.json())
        quote = contract.quote
        return {"ok": True, "contract": intent.leg.symbol,
                "quantity": str(intent.leg.quantity), "limit_price": str(intent.limit_price),
                "maximum_premium": str(intent.limit_price * intent.leg.quantity * contract.multiplier),
                "indicative_bid": str(quote.bid) if quote and quote.bid is not None else None,
                "indicative_ask": str(quote.ask) if quote and quote.ask is not None else None,
                "quote_at": quote.observed_at.isoformat() if quote else None,
                "protection": "No broker-held stop or target is attached; maximum premium can be lost"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option preview failed; no order was sent"}


@app.post("/api/paper/options/long/submit")
async def paper_option_long_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_option_long, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_option_long(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _ = _paper_option_long_entry(body)
        symbol = intent.leg.symbol
        def preflight(account):
            if (account.status.upper() != "ACTIVE" or account.options_trading_level is None or
                    account.options_approved_level is None or
                    min(account.options_trading_level, account.options_approved_level) < 2 or
                    any(position.symbol.replace(" ", "") == symbol for position in account.positions) or
                    account.balances.buying_power is None or
                    intent.limit_price * intent.leg.quantity * 100 > account.balances.buying_power):
                raise ValueError("option account level, buying power, or position changed; no order was sent")
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol.replace(" ", "") for group in groups
                   for order in group.orders if order.status in active for leg in order.legs):
                raise ValueError("another order appeared for this option; no order was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _short_collateral_order_conflict(groups, underlying: str) -> bool:
    active = {OrderStatus.PENDING, OrderStatus.WORKING,
              OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN}
    for group in groups:
        for order in group.orders:
            if order.status not in active:
                continue
            for leg in order.legs:
                symbol = leg.symbol.replace(" ", "")
                match = OCC.fullmatch(symbol)
                if symbol == underlying or (match and match.group(1) == underlying):
                    return True
    return False


def _paper_option_short_entry(body: dict):
    service, _ = _paper_service()
    query = chain_query(body)
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref:
        raise ValueError("Alpaca Paper account changed; no option order was sent")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    if _short_collateral_order_conflict(groups, query.underlying):
        raise ValueError("an active order touches this underlying or its options; resolve it first")
    chain = service.adapter.query(query)
    intent = short_entry_plan(body, account, chain)
    contract = next(contract for contract in chain.contracts
                    if contract.symbol.replace(" ", "") == intent.leg.symbol)
    return service, intent, contract, chain


@app.post("/api/paper/options/short/preview")
async def paper_option_short_preview(request: Request):
    _guard(request)
    try:
        _, intent, contract, _ = await run_in_threadpool(_paper_option_short_entry,
                                                          await request.json())
        quote = contract.quote
        call = contract.option_type.value == "call"
        return {"ok": True, "contract": intent.leg.symbol,
                "quantity": str(intent.leg.quantity), "limit_price": str(intent.limit_price),
                "collateral_type": "covered shares" if call else "options buying power",
                "collateral_required": str(intent.leg.quantity *
                                           (100 if call else contract.strike * 100)),
                "indicative_bid": str(quote.bid),
                "indicative_ask": str(quote.ask) if quote.ask is not None else None,
                "protection": "No broker-held stop or target is attached. Assignment may occur; collateral must remain available."}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper short option preview failed; no order was sent"}


@app.post("/api/paper/options/short/submit")
async def paper_option_short_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_option_short, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper short option preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_option_short(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _, chain = _paper_option_short_entry(body)
        underlying = chain.underlying
        def preflight(account):
            if account.account_ref != service.account_ref:
                raise ValueError("option account changed; no short order was sent")
            short_entry_plan(body, account, chain)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if _short_collateral_order_conflict(groups, underlying):
                raise ValueError("another order appeared for this underlying; no short order was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_vertical_entry(body: dict):
    service, _ = _paper_service()
    query = chain_query(body)
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref:
        raise ValueError("Alpaca Paper account changed; no vertical was sent")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                               statuses=active))
    if _short_collateral_order_conflict(groups, query.underlying):
        raise ValueError("an active order touches this underlying; resolve it before a vertical")
    chain = service.adapter.query(query)
    intent = vertical_entry_plan(body, account, chain)
    return service, intent, chain


@app.post("/api/paper/vertical/entry/preview")
async def paper_vertical_entry_preview(request: Request):
    _guard(request)
    try:
        _, intent, chain = await run_in_threadpool(_paper_vertical_entry, await request.json())
        contracts = {contract.symbol.replace(" ", ""): contract
                     for contract in chain.contracts}
        long, short = (contracts[leg.symbol] for leg in intent.legs)
        width = abs(long.strike - short.strike)
        net = intent.limit_price
        debit = net > 0
        return {"ok": True, "long_contract": intent.legs[0].symbol,
                "short_contract": intent.legs[1].symbol,
                "quantity": str(intent.legs[0].quantity),
                "net_price": str(net), "kind": "debit" if debit else "credit",
                "strike_width": str(width),
                "maximum_loss": str((net if debit else width + net) *
                                    intent.legs[0].quantity * 100),
                "protection": "One broker-held two-leg order. No broker-held spread stop or target is attached after fill; an app-watched stop is unavailable until monitoring is connected."}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical preview failed; no order was sent"}


@app.post("/api/paper/vertical/entry/submit")
async def paper_vertical_entry_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_vertical_entry, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_vertical_entry(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, chain = _paper_vertical_entry(body)
        def preflight(account):
            if account.account_ref != service.account_ref:
                raise ValueError("vertical account changed; no order was sent")
            vertical_entry_plan(body, account, chain)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if _short_collateral_order_conflict(groups, chain.underlying):
                raise ValueError("another order appeared for this underlying; no vertical was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_vertical_close(body: dict, *, require_quotes: bool = True):
    service, _ = _paper_service()
    if not isinstance(body, dict):
        raise ValueError("vertical close request must be an object")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref:
        raise ValueError("Alpaca Paper account changed; no vertical close was sent")
    symbol = body.get("long_contract")
    match = OCC.fullmatch(symbol) if isinstance(symbol, str) else None
    if match is None:
        raise ValueError("a held OCC vertical long contract is required")
    underlying = match.group(1)
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                               statuses=active))
    if _short_collateral_order_conflict(groups, underlying):
        raise ValueError("an active order touches this underlying; resolve it before closing the vertical")
    symbols = (body.get("long_contract"), body.get("short_contract"))
    if not all(isinstance(item, str) and item for item in symbols):
        raise ValueError("both held vertical OCC contracts are required")
    quotes = (service.adapter.query(QuotesQuery(symbols, AssetType.OPTION))
              if require_quotes else {})
    intent = vertical_close_plan(body, account, quotes, require_quotes=require_quotes)
    return service, intent, quotes


@app.post("/api/paper/vertical/close/preview")
async def paper_vertical_close_preview(request: Request):
    _guard(request)
    try:
        _, intent, quotes = await run_in_threadpool(_paper_vertical_close,
                                                     await request.json())
        long, short = (quotes[leg.symbol] for leg in intent.legs)
        indicative = short.ask - long.bid
        return {"ok": True, "long_contract": intent.legs[0].symbol,
                "short_contract": intent.legs[1].symbol,
                "quantity": str(intent.legs[0].quantity),
                "net_price": str(intent.limit_price),
                "role": "target" if intent.time_in_force.value == "gtc" else "close",
                "time_in_force": intent.time_in_force.value,
                "indicative_close_cost": str(indicative),
                "protection": ("One broker-held multi-leg GTC profit target after Alpaca accepts it. "
                               "No broker-held stop is paired; both legs remain exposed until a complete fill."
                               if intent.time_in_force.value == "gtc" else
                               "One broker multi-leg limit close. Both held legs remain exposed until the broker confirms a complete fill; a partial or unknown outcome blocks another action.")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical close preview failed; no order was sent"}


@app.post("/api/paper/vertical/close/submit")
async def paper_vertical_close_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_vertical_close, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical close preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_vertical_close(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, quotes = _paper_vertical_close(body)
        match = OCC.fullmatch(intent.legs[0].symbol)
        underlying = match.group(1)
        def preflight(account):
            if account.account_ref != service.account_ref:
                raise ValueError("vertical account changed; no close was sent")
            vertical_close_plan(body, account, quotes)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if _short_collateral_order_conflict(groups, underlying):
                raise ValueError("another order appeared for this underlying; no vertical close was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_option_close(body: dict):
    service, _ = _paper_service()
    symbol = body.get("contract") if isinstance(body, dict) else None
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("an OCC option contract is required")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca Paper account changed or is inactive")
    positions = [position for position in account.positions
                 if position.symbol.replace(" ", "") == symbol]
    if len(positions) != 1:
        raise ValueError("exactly one held option position is required")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    if any(symbol == leg.symbol.replace(" ", "") for group in groups
           for order in group.orders if order.status in active for leg in order.legs):
        raise ValueError("an active order touches this option; resolve it before closing")
    intent = option_close_plan(body, positions[0])
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.OPTION)).get(symbol)
    return service, intent, positions[0], quote


@app.post("/api/paper/options/close/preview")
async def paper_option_close_preview(request: Request):
    _guard(request)
    try:
        _, intent, position, quote = await run_in_threadpool(_paper_option_close,
                                                              await request.json())
        return {"ok": True, "contract": intent.leg.symbol,
                "side": intent.leg.instruction.value,
                "held": str(position.quantity), "closing": str(intent.leg.quantity),
                "remaining": str(position.quantity - intent.leg.quantity),
                "limit_price": str(intent.limit_price),
                "indicative_bid": str(quote.bid) if quote and quote.bid is not None else None,
                "indicative_ask": str(quote.ask) if quote and quote.ask is not None else None,
                "protection": "Limit fill is not guaranteed; any remaining contracts have no new exit attached"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option close preview failed; no order was sent"}


@app.post("/api/paper/options/close/submit")
async def paper_option_close_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_option_close, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option close preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_option_close(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, position, _ = _paper_option_close(body)
        symbol = intent.leg.symbol
        def preflight(account):
            positions = [held for held in account.positions
                         if held.symbol.replace(" ", "") == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.OPTION or
                    positions[0].side != position.side or
                    positions[0].quantity != position.quantity):
                raise ValueError("option position changed; no close was sent")
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol.replace(" ", "") for group in groups
                   for order in group.orders if order.status in active for leg in order.legs):
                raise ValueError("another option order appeared; no close was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_option_cancel_check(order_id: str):
    service, _ = _paper_service()
    group = service.adapter.query(OrderQuery(order_id))
    order = next((item for item in group.orders if item.order_id == order_id), None)
    if (order is None or group.order_class != OrderClass.SIMPLE or
            order.parent_id is not None or len(order.legs) != 1 or
            order.legs[0].asset_type != AssetType.OPTION or
            not str(order.client_order_id or "").startswith("et-opt-") or
            order.status not in (OrderStatus.PENDING, OrderStatus.WORKING,
                                 OrderStatus.PARTIALLY_FILLED) or
            order.status_detail in ("PENDING_CANCEL", "PENDING_REPLACE")):
        raise ValueError("Only an active Edge Trader Paper single-option order can be canceled here")
    leg = order.legs[0]
    remaining = leg.remaining_quantity if leg.remaining_quantity is not None else leg.quantity - leg.filled_quantity
    if remaining <= 0:
        raise ValueError("No unfilled option contracts remain to cancel")
    return service, order, leg, remaining


@app.post("/api/paper/options/cancel/preview")
async def paper_option_cancel_preview(request: Request):
    _guard(request)
    try:
        order_id = str(UUID(str((await request.json()).get("order_id"))))
        _, order, leg, remaining = await run_in_threadpool(_paper_option_cancel_check, order_id)
        return {"ok": True, "order_id": order_id, "contract": leg.symbol,
                "instruction": leg.instruction.value,
                "filled": str(leg.filled_quantity), "remaining": str(remaining),
                "warning": "Cancellation only requests that unfilled contracts stop; filled contracts remain in the account"}
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option cancel preview failed; no cancel was sent"}


@app.post("/api/paper/options/cancel/submit")
async def paper_option_cancel_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_cancel_paper_option, order_id, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option cancel preflight failed; no cancel was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _cancel_paper_option(order_id: str, request_id: str):
    with BROKER_ACTION_LOCK:
        service, _, _, _ = _paper_option_cancel_check(order_id)
        return service.execute(CancelOrder(order_id), request_id)


def _paper_vertical_cancel_check(order_id: str):
    service, _ = _paper_service()
    group = service.adapter.query(OrderQuery(order_id))
    order = next((item for item in group.orders if item.order_id == order_id), None)
    if (order is None or group.order_class != OrderClass.MULTI_LEG or
            order.parent_id is not None or len(order.legs) != 2 or
            any(leg.asset_type != AssetType.OPTION for leg in order.legs) or
            not str(order.client_order_id or "").startswith("et-vertical-") or
            order.status not in (OrderStatus.PENDING, OrderStatus.WORKING,
                                 OrderStatus.PARTIALLY_FILLED) or
            order.status_detail in ("PENDING_CANCEL", "PENDING_REPLACE")):
        raise ValueError("Only an active Edge Trader Paper two-leg vertical can be canceled here")
    if service.store.get_mutation_by_client_id(order.client_order_id) is None:
        raise ValueError("Vertical order is not owned by this Paper account store")
    remaining = [leg.remaining_quantity if leg.remaining_quantity is not None else
                 leg.quantity - leg.filled_quantity for leg in order.legs]
    if not any(value > 0 for value in remaining):
        raise ValueError("No unfilled vertical units remain to cancel")
    return service, order, remaining


@app.post("/api/paper/vertical/cancel/preview")
async def paper_vertical_cancel_preview(request: Request):
    _guard(request)
    try:
        order_id = str(UUID(str((await request.json()).get("order_id"))))
        _, order, remaining = await run_in_threadpool(_paper_vertical_cancel_check, order_id)
        return {"ok": True, "order_id": order_id,
                "legs": [{"contract": leg.symbol, "instruction": leg.instruction.value,
                          "filled": str(leg.filled_quantity), "remaining": str(rest)}
                         for leg, rest in zip(order.legs, remaining)],
                "warning": "Cancellation requests only the unfilled remainder of the one multi-leg order; any filled legs remain held and must be reconciled"}
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical cancel preview failed; no cancel was sent"}


@app.post("/api/paper/vertical/cancel/submit")
async def paper_vertical_cancel_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_cancel_paper_vertical, order_id, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical cancel preflight failed; no cancel was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _cancel_paper_vertical(order_id: str, request_id: str):
    with BROKER_ACTION_LOCK:
        service, _, _ = _paper_vertical_cancel_check(order_id)
        return service.execute(CancelOrder(order_id), request_id)


def _paper_vertical_target_reprice_check(order_id: str, price: Decimal, request_id: str):
    service, _ = _paper_service()
    group = service.adapter.query(OrderQuery(order_id))
    order = next((item for item in group.orders if item.order_id == order_id), None)
    if (order is None or group.order_class != OrderClass.MULTI_LEG or
            len(group.orders) != 1 or len(order.legs) != 2 or
            order.parent_id is not None or order.role != OrderRole.TARGET or
            order.order_type != OrderType.LIMIT or order.time_in_force.value != "gtc" or
            order.status != OrderStatus.WORKING or
            order.status_detail not in (None, "NEW", "WORKING") or
            not str(order.client_order_id or "").startswith("et-vertical-target-") or
            order.limit_price is None or order.limit_price == price or
            any(leg.asset_type != AssetType.OPTION or leg.filled_quantity != 0 or
                (leg.remaining_quantity is not None and leg.quantity != leg.remaining_quantity)
                for leg in order.legs)):
        raise ValueError("Only a fully working, unfilled app-created Paper vertical target can be repriced")
    if service.store.get_mutation_by_client_id(order.client_order_id) is None:
        raise ValueError("Vertical target is not owned by this Paper account store")
    long = next((leg for leg in order.legs if leg.instruction == OrderInstruction.SELL_TO_CLOSE), None)
    short = next((leg for leg in order.legs if leg.instruction == OrderInstruction.BUY_TO_CLOSE), None)
    if long is None or short is None or long.quantity != short.quantity:
        raise ValueError("Target is not an exact two-leg vertical close")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref:
        raise ValueError("Paper account changed; no vertical target was repriced")
    symbols = (long.symbol, short.symbol)
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                               statuses=active))
    if any(other.order_id != order_id and other.status in active and
           any(leg.symbol in symbols for leg in other.legs)
           for other_group in groups for other in other_group.orders):
        raise ValueError("another order touches a vertical leg; resolve it before repricing")
    quotes = service.adapter.query(QuotesQuery(symbols, AssetType.OPTION))
    intent = vertical_close_plan({"long_contract": long.symbol,
                                  "short_contract": short.symbol,
                                  "net_price": str(price), "role": "target",
                                  "request_id": request_id}, account, quotes)
    if tuple((leg.symbol, leg.quantity, leg.instruction) for leg in intent.legs) != tuple(
            (leg.symbol, leg.quantity, leg.instruction) for leg in order.legs):
        raise ValueError("Held vertical no longer matches the working target")
    return service, order, account


@app.post("/api/paper/vertical/target/reprice/preview")
async def paper_vertical_target_reprice_preview(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        price = Decimal(str(body.get("net_price")))
        _, order, _ = await run_in_threadpool(
            _paper_vertical_target_reprice_check, order_id, price, request_id)
        return {"ok": True, "order_id": order_id,
                "legs": [leg.symbol for leg in order.legs],
                "quantity": str(order.legs[0].quantity),
                "old_price": str(order.limit_price), "new_price": str(price),
                "warning": "One broker-held multi-leg profit target remains without a paired stop. Replacement can be refused or fill before confirmation."}
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical target replace preview failed; nothing was sent"}


@app.post("/api/paper/vertical/target/reprice/submit")
async def paper_vertical_target_reprice_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        price = Decimal(str(body.get("net_price")))
        result = await run_in_threadpool(_replace_paper_vertical_target,
                                         order_id, price, request_id)
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper vertical target replace preflight failed; nothing was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _replace_paper_vertical_target(order_id: str, price: Decimal, request_id: str):
    with BROKER_ACTION_LOCK:
        service, original, account = _paper_vertical_target_reprice_check(
            order_id, price, request_id)
        if not service.armed or not service._market_open(datetime.now(timezone.utc)):
            raise ValueError("Arm Paper orders during regular hours before moving a vertical target")
        def preflight(current):
            if (current.account_ref != account.account_ref or
                    current.status != account.status or current.positions != account.positions):
                raise ValueError("Vertical positions changed before replacement; nothing was sent")
            _, checked, _ = _paper_vertical_target_reprice_check(
                order_id, price, request_id)
            if checked != original:
                raise ValueError("Vertical target changed before replacement; nothing was sent")
        return service.execute(ReplaceOrder(order_id, limit_price=price,
                                            client_order_id=f"et-vertical-target-r-{UUID(request_id).hex}"),
                               request_id, preflight=preflight)


def _paper_vertical_stop_plan(body: dict):
    service, _ = _paper_service()
    if not isinstance(body, dict) or type(body.get("send")) is not bool:
        raise ValueError("Paper spread stop requires an explicit send or watch-only choice")
    long_symbol, short_symbol = body.get("long_contract"), body.get("short_contract")
    if not isinstance(long_symbol, str) or not isinstance(short_symbol, str):
        raise ValueError("Choose both held vertical legs")
    try:
        request_id = str(UUID(str(body.get("request_id"))))
        stop_value = Decimal(str(body.get("stop_value")))
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        raise ValueError("Spread stop value and request ID are invalid") from None
    if not stop_value.is_finite() or stop_value <= 0 or stop_value != stop_value.quantize(Decimal("0.01")):
        raise ValueError("Spread stop must be a positive cent-precision premium")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Paper account changed or is inactive")
    held = {position.symbol.replace(" ", ""): position for position in account.positions}
    long, short = held.get(long_symbol), held.get(short_symbol)
    if long is None or short is None or long.average_price is None or short.average_price is None:
        raise ValueError("Both held vertical legs need a known entry basis")
    entry_net = long.average_price - short.average_price
    if entry_net == 0:
        raise ValueError("Vertical entry basis is zero; stop risk cannot be measured")
    kind = "debit" if entry_net > 0 else "credit"
    signed_close = -stop_value if kind == "debit" else stop_value
    plan_body = {"long_contract": long_symbol, "short_contract": short_symbol,
                 "net_price": str(signed_close), "role": "close",
                 "request_id": request_id}
    _, intent, _ = _paper_vertical_close(plan_body, require_quotes=False)
    if intent.legs[0].quantity != long.quantity or intent.legs[1].quantity != short.quantity:
        raise ValueError("Vertical quantity changed during stop review")
    current = service.adapter.query(AccountQuery())
    if current.positions != account.positions:
        raise ValueError("Vertical position changed during stop review")
    match_long, match_short = OCC.fullmatch(long_symbol), OCC.fullmatch(short_symbol)
    width = abs(Decimal(match_long.group(6)) - Decimal(match_short.group(6))) / 1000
    basis = abs(entry_net)
    if ((kind == "debit" and stop_value >= basis) or
            (kind == "credit" and stop_value <= basis) or stop_value >= width):
        raise ValueError("Spread stop must cut loss before maximum spread loss")
    feed_ready = False
    feed_reason = None
    try:
        now = datetime.now(timezone.utc)
        opra = service.adapter.query(QuotesQuery((long_symbol, short_symbol), AssetType.OPTION,
                                                 realtime_required=True))
        feed_ready = all(quote and quote.source == "Alpaca OPRA options" and
                         quote.realtime and quote.timestamp_verified and
                         quote.observed_at.tzinfo is not None and
                         0 <= (now - quote.observed_at).total_seconds() <= 15 and
                         quote.bid is not None and quote.ask is not None and
                         0 <= quote.bid <= quote.ask and quote.ask > 0
                         for quote in (opra.get(long_symbol), opra.get(short_symbol)))
        if not feed_ready:
            feed_reason = "Fresh verified OPRA quotes are unavailable"
    except Exception as exc:
        feed_reason = ("OPRA subscription is unavailable for this Paper account" if
                       getattr(exc, "status_code", None) == 403 else
                       "OPRA quote verification failed")
    if body["send"] and not feed_ready:
        raise ValueError(f"Automatic spread stop is unavailable: {feed_reason}")
    watch = getattr(poller, "spread_stops", None)
    if watch is None or watch.store is not service.store:
        raise ValueError("Paper stop store is not active for this account")
    maximum_loss = (basis if kind == "debit" else width - basis) * long.quantity * 100
    stop_loss = ((basis - stop_value) if kind == "debit" else
                 (stop_value - basis)) * long.quantity * 100
    return service, watch, {"long_contract": long_symbol,
                            "short_contract": short_symbol,
                            "quantity": long.quantity, "kind": kind,
                            "basis": basis, "long_average": long.average_price,
                            "short_average": short.average_price,
                            "width": width, "stop_value": stop_value,
                            "send": body["send"], "feed_ready": feed_ready,
                            "feed_reason": feed_reason,
                            "maximum_loss": maximum_loss, "stop_loss": stop_loss}


@app.post("/api/paper/vertical/stop/preview")
async def paper_vertical_stop_preview(request: Request):
    _guard(request)
    try:
        _, _, plan = await run_in_threadpool(_paper_vertical_stop_plan, await request.json())
        return {"ok": True, **{key: str(value) if isinstance(value, Decimal) else value
                               for key, value in plan.items()},
                "warning": ("APP-WATCHED: one close attempt only while this app runs, Paper orders are armed, regular hours are open, and both OPRA quotes stay fresh. No broker-held stop; a limit close can remain unfilled." if plan["send"] else
                            "WATCH ONLY: no order will be sent. This alert needs a running app and fresh OPRA quotes.")}
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Paper spread stop review failed; no stop was set"}


@app.post("/api/paper/vertical/stop/set")
async def paper_vertical_stop_set(request: Request):
    _guard(request)
    try:
        body = await request.json()
        result = await run_in_threadpool(_set_paper_vertical_stop, body)
        return {"ok": True, **result}
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Paper spread stop preflight failed; no stop was set"}


def _set_paper_vertical_stop(body: dict) -> dict:
    with BROKER_ACTION_LOCK:
        service, watch, plan = _paper_vertical_stop_plan(body)
        expected = body.get("reviewed") if isinstance(body, dict) else None
        if expected != {key: str(plan[key]) for key in ("quantity", "basis", "width", "maximum_loss")}:
            raise ValueError("Vertical size or risk changed since preview; review again")
        if plan["send"] and (not service.armed or
                             not service._market_open(datetime.now(timezone.utc)) or
                             service.store.open_mutations()):
            raise ValueError("Arm Paper orders during regular hours and resolve earlier outcomes before enabling a send stop")
        stop = watch.set(plan["long_contract"], plan["short_contract"], plan["quantity"],
                         plan["kind"], plan["basis"], plan["long_average"],
                         plan["short_average"], plan["width"], plan["stop_value"],
                         plan["send"])
        return {"stop_id": stop["stop_id"], "status": stop["status"],
                "send": stop["send"], "feed_ready": plan["feed_ready"],
                "message": ("App-watched Paper spread stop set; no broker-held stop exists" if
                            plan["feed_ready"] else
                            "Watch-only stop saved but suspended until fresh OPRA quotes are available")}


@app.post("/api/paper/vertical/stop/clear")
async def paper_vertical_stop_clear(request: Request):
    _guard(request)
    try:
        body = await request.json()
        stop_id = body.get("stop_id")
        if not isinstance(stop_id, str) or len(stop_id) > 100:
            raise ValueError("A Paper spread stop ID is required")
        def clear():
            with BROKER_ACTION_LOCK:
                service, _ = _paper_service()
                watch = getattr(poller, "spread_stops", None)
                if watch is None or watch.store is not service.store:
                    raise ValueError("Paper stop store is not active for this account")
                if body.get("acknowledged") is True and not watch.acknowledge_unknown(stop_id):
                    raise ValueError("Unknown stop outcome is still unresolved; inspect the Paper order first")
                if not watch.clear(stop_id):
                    raise ValueError("A stop close may be in flight; reconcile before clearing it")
                return {"ok": True, "stop_id": stop_id}
        return await run_in_threadpool(clear)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}


def _paper_option_fixed_exit(body: dict):
    service, _ = _paper_service()
    symbol = body.get("contract") if isinstance(body, dict) else None
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("an OCC option contract is required")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca Paper account changed or is inactive")
    positions = [position for position in account.positions
                 if position.symbol.replace(" ", "") == symbol]
    if len(positions) != 1:
        raise ValueError("exactly one held option position is required")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    if any(symbol == leg.symbol.replace(" ", "") for group in groups
           for order in group.orders if order.status in active for leg in order.legs):
        raise ValueError("an active option order already exists; resolve it before attaching an exit")
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.OPTION)).get(symbol)
    intent = option_fixed_exit_plan(body, positions[0], quote)
    return service, intent, positions[0], quote


@app.post("/api/paper/options/exit/preview")
async def paper_option_exit_preview(request: Request):
    _guard(request)
    try:
        _, intent, position, quote = await run_in_threadpool(_paper_option_fixed_exit,
                                                               await request.json())
        role = ("stop-limit" if intent.order_type == OrderType.STOP_LIMIT else
                "stop" if intent.stop_price is not None else "target")
        return {"ok": True, "contract": intent.leg.symbol,
                "side": intent.leg.instruction.value,
                "quantity": str(position.quantity), "role": role,
                "price": str(intent.stop_price or intent.limit_price),
                "limit_price": str(intent.limit_price) if role == "stop-limit" else None,
                "time_in_force": intent.time_in_force.value,
                "indicative_mark": str(quote.mark),
                "protection": ("One broker-held fixed stop after Alpaca accepts it; no target is paired. "
                               "A triggered stop becomes a market order and may fill at another price."
                               if role == "stop" else
                               "One broker-held stop-limit after Alpaca accepts it; no target is paired. "
                               "A triggered limit order may remain unfilled while losses grow."
                               if role == "stop-limit" else
                               "One broker-held target after Alpaca accepts it; no stop is paired.")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option exit preview failed; no order was sent"}


@app.post("/api/paper/options/exit/submit")
async def paper_option_exit_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_option_exit, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper option exit preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_option_exit(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, position, _ = _paper_option_fixed_exit(body)
        symbol = intent.leg.symbol
        def preflight(account):
            positions = [held for held in account.positions
                         if held.symbol.replace(" ", "") == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.OPTION or
                    positions[0].side != position.side or
                    positions[0].quantity != position.quantity):
                raise ValueError("option position changed; no exit was sent")
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol.replace(" ", "") for group in groups
                   for order in group.orders if order.status in active for leg in order.legs):
                raise ValueError("another option order appeared; no exit was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_entry(body: dict, for_submit: bool = False):
    service, snap = _paper_service()
    symbol = body.get("symbol") if isinstance(body, dict) else None
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("an equity symbol is required")
    if any(p["symbol"] == symbol for p in snap["alpaca"]["positions"]):
        raise ValueError("bracket entry requires a flat symbol; manage the existing position first")
    if any(symbol in o["symbols"] and o["status"] in ("pending", "working", "partially-filled")
           for o in snap["alpaca"]["orders"]):
        raise ValueError("an order is already active for this symbol")
    if for_submit:
        # The dashboard may be ten seconds behind an external account action.
        account = service.adapter.query(AccountQuery())
        if account.account_ref != service.account_ref or any(
                p.symbol == symbol for p in account.positions):
            raise ValueError("account or position changed; refresh before ordering")
        now = datetime.now(timezone.utc)
        active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                            OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
        groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                   statuses=active))
        if any(symbol == leg.symbol for group in groups for order in group.orders
               if order.status in active for leg in order.legs):
            raise ValueError("an order is already active for this symbol")
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.EQUITY)).get(symbol)
    intent = entry_plan(body, quote)
    return service, intent, quote


@app.get("/api/paper/status")
def paper_status():
    service, _ = _paper_service()
    return {"ok": True, "armed": service.armed,
            "unresolved": [{"request_id": row["request_id"], "command": row["command"]}
                           for row in service.store.open_mutations()]}


@app.post("/api/paper/arm")
async def paper_arm(request: Request):
    _guard(request)
    body = await request.json()
    if not isinstance(body, dict) or type(body.get("armed")) is not bool:
        raise HTTPException(400, "armed must be true or false")
    return await run_in_threadpool(_set_paper_arming, body["armed"])


def _set_paper_arming(enabled: bool) -> dict:
    with BROKER_ACTION_LOCK:
        service, _ = _paper_service()
        service.arm(enabled)
        return {"ok": True, "armed": service.armed}


@app.post("/api/paper/entry/preview")
async def paper_entry_preview(request: Request):
    _guard(request)
    try:
        _, intent, quote = await run_in_threadpool(_paper_entry, await request.json())
        entry = intent.entry if isinstance(intent, BracketOrderIntent) else intent
        bracket = isinstance(intent, BracketOrderIntent)
        return {"ok": True, "symbol": entry.leg.symbol,
                "side": entry.leg.instruction.value,
                "quantity": str(entry.leg.quantity),
                "entry": entry.order_type.value,
                "limit_price": str(entry.limit_price) if entry.limit_price else None,
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "target": str(intent.take_profit_price) if bracket else None,
                "stop": str(intent.stop_loss.stop_price) if bracket else None,
                "protection": ("Broker-held bracket target and stop after entry fills" if bracket else
                               "UNPROTECTED simple entry: no stop or target is attached")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper quote or account verification failed; no order was sent"}


@app.post("/api/paper/entry/submit")
async def paper_entry_submit(request: Request):
    _guard(request)
    body = await request.json()
    try:
        if not isinstance(body, dict):
            raise ValueError("entry request must be an object")
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_entry, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_entry(body: dict, request_id: str):
    # Selection and arming cannot change between the fresh preflight and mutation.
    with BROKER_ACTION_LOCK:
        service, intent, _ = _paper_entry(body, True)
        return service.execute(SubmitOrder(intent), request_id)


def _paper_order_free_position(body: dict):
    service, _ = _paper_service()
    symbol = body.get("symbol") if isinstance(body, dict) else None
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("an equity symbol is required")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca Paper account changed or is inactive")
    positions = [position for position in account.positions if position.symbol == symbol]
    if len(positions) != 1:
        raise ValueError("exactly one held equity position is required")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    if any(symbol == leg.symbol for group in groups for order in group.orders
           if order.status in active for leg in order.legs):
        raise ValueError("an active order already touches this symbol; resolve it before another action")
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.EQUITY)).get(symbol)
    return service, positions[0], quote, account


def _refuse_covered_share_exit(account, symbol: str) -> None:
    if covered_call_reserved_shares(account, symbol):
        raise ValueError("held short calls reserve these shares; close the calls before an equity exit")


def _paper_oco(body: dict):
    service, position, quote, account = _paper_order_free_position(body)
    if position.side.value == "long":
        _refuse_covered_share_exit(account, position.symbol)
    return service, oco_plan(body, position, quote), quote


def _paper_close(body: dict):
    service, position, quote, account = _paper_order_free_position(body)
    if position.side.value == "long":
        _refuse_covered_share_exit(account, position.symbol)
    return service, close_plan(body, position, quote), quote, position.quantity


def _paper_add(body: dict):
    service, position, quote, _ = _paper_order_free_position(body)
    return service, add_plan(body, position, quote), quote, position.quantity


def _paper_trailing(body: dict):
    service, position, quote, account = _paper_order_free_position(body)
    if position.side.value == "long":
        _refuse_covered_share_exit(account, position.symbol)
    return service, trailing_plan(body, position, quote), quote, position.quantity


@app.post("/api/paper/position/trailing/preview")
async def paper_trailing_preview(request: Request):
    _guard(request)
    try:
        _, intent, quote, held = await run_in_threadpool(_paper_trailing, await request.json())
        mark = quote.mark or quote.last
        distance = intent.trail_price or (mark * intent.trail_percent / 100)
        initial = (mark - distance if intent.leg.instruction == OrderInstruction.SELL
                   else mark + distance)
        return {"ok": True, "symbol": intent.leg.symbol,
                "side": intent.leg.instruction.value, "quantity": str(held),
                "unit": "dollar" if intent.trail_price is not None else "percent",
                "distance": str(intent.trail_price or intent.trail_percent),
                "indicative_initial_stop": str(initial),
                "quote": str(mark), "quote_source": quote.source,
                "protection": "Broker-held standalone trailing stop after Alpaca accepts it; no target is paired"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper position, order, or quote verification failed; no order was sent"}


@app.post("/api/paper/position/trailing/submit")
async def paper_trailing_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_trailing, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper trailing-stop preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_trailing(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _, held = _paper_trailing(body)
        symbol, instruction = intent.leg.symbol, intent.leg.instruction
        def preflight(account):
            positions = [position for position in account.positions if position.symbol == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.EQUITY or
                    positions[0].quantity != held or
                    (positions[0].side.value == "long") !=
                    (instruction == OrderInstruction.SELL)):
                raise ValueError("position changed before trailing stop submission; no order was sent")
            if instruction == OrderInstruction.SELL:
                _refuse_covered_share_exit(account, symbol)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol for group in groups for order in group.orders
                   if order.status in active for leg in order.legs):
                raise ValueError("another order appeared for this symbol; no trail was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


@app.post("/api/paper/position/add/preview")
async def paper_add_preview(request: Request):
    _guard(request)
    try:
        _, intent, quote, held = await run_in_threadpool(_paper_add, await request.json())
        return {"ok": True, "symbol": intent.leg.symbol,
                "side": intent.leg.instruction.value,
                "quantity": str(intent.leg.quantity), "held_quantity": str(held),
                "total_if_filled": str(held + intent.leg.quantity),
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "order_type": "market",
                "protection": "No stop is attached; the added shares remain unprotected until a new OCO is accepted"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper position, order, or quote verification failed; no order was sent"}


@app.post("/api/paper/position/add/submit")
async def paper_add_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_add, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper add preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_add(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _, held = _paper_add(body)
        symbol, instruction = intent.leg.symbol, intent.leg.instruction
        def preflight(account):
            positions = [position for position in account.positions if position.symbol == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.EQUITY or
                    positions[0].quantity != held or
                    (positions[0].side.value == "long") !=
                    (instruction == OrderInstruction.BUY)):
                raise ValueError("position changed before add submission; no order was sent")
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol for group in groups for order in group.orders
                   if order.status in active for leg in order.legs):
                raise ValueError("another order appeared for this symbol; no add was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


@app.post("/api/paper/position/close/preview")
async def paper_close_preview(request: Request):
    _guard(request)
    try:
        _, intent, quote, held = await run_in_threadpool(_paper_close, await request.json())
        return {"ok": True, "symbol": intent.leg.symbol,
                "side": intent.leg.instruction.value,
                "quantity": str(intent.leg.quantity),
                "held_quantity": str(held),
                "remaining_if_filled": str(held - intent.leg.quantity),
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "order_type": "market",
                "protection": "No stop is attached; remaining shares, if any, stay unprotected"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper position, order, or quote verification failed; no order was sent"}


@app.post("/api/paper/position/close/submit")
async def paper_close_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_close, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper close preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_close(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _, held = _paper_close(body)
        symbol, quantity, instruction = (intent.leg.symbol, intent.leg.quantity,
                                         intent.leg.instruction)
        def preflight(account):
            positions = [position for position in account.positions if position.symbol == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.EQUITY or
                    positions[0].quantity != held or
                    (positions[0].side.value == "long") !=
                    (instruction == OrderInstruction.SELL)):
                raise ValueError("position changed before close submission; no order was sent")
            if instruction == OrderInstruction.SELL:
                _refuse_covered_share_exit(account, symbol)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol for group in groups for order in group.orders
                   if order.status in active for leg in order.legs):
                raise ValueError("another order appeared for this symbol; no close was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


@app.post("/api/paper/oco/preview")
async def paper_oco_preview(request: Request):
    _guard(request)
    try:
        _, intent, quote = await run_in_threadpool(_paper_oco, await request.json())
        return {"ok": True, "symbol": intent.leg.symbol,
                "side": intent.leg.instruction.value,
                "quantity": str(intent.leg.quantity),
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "target": str(intent.take_profit_price), "stop": str(intent.stop_loss.stop_price),
                "protection": "Broker-held OCO target and stop after Alpaca accepts the order"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper position, order, or quote verification failed; no order was sent"}


@app.post("/api/paper/oco/submit")
async def paper_oco_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_oco, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper OCO preflight failed; no order was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_oco(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, intent, _ = _paper_oco(body)
        symbol, quantity, instruction = (intent.leg.symbol, intent.leg.quantity,
                                         intent.leg.instruction)
        def preflight(account):
            positions = [position for position in account.positions if position.symbol == symbol]
            if (account.status.upper() != "ACTIVE" or len(positions) != 1 or
                    positions[0].asset_type != AssetType.EQUITY or
                    positions[0].quantity != quantity or
                    (positions[0].side.value == "long") !=
                    (instruction == OrderInstruction.SELL)):
                raise ValueError("position changed before OCO submission; no order was sent")
            if instruction == OrderInstruction.SELL:
                _refuse_covered_share_exit(account, symbol)
            now = datetime.now(timezone.utc)
            active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                                OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
            groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                                       statuses=active))
            if any(symbol == leg.symbol for group in groups for order in group.orders
                   if order.status in active for leg in order.legs):
                raise ValueError("another order appeared for this symbol; no OCO was sent")
        return service.execute(SubmitOrder(intent), request_id, preflight=preflight)


def _paper_oco_adjust(body: dict, account=None):
    service, _ = _paper_service()
    if not isinstance(body, dict):
        raise ValueError("OCO adjustment request must be an object")
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("an equity symbol is required")
    account = account or service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca Paper account changed or is inactive")
    positions = [position for position in account.positions if position.symbol == symbol]
    if len(positions) != 1:
        raise ValueError("exactly one held equity position is required")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=active))
    touching = [group for group in groups if any(
        leg.symbol == symbol for order in group.orders if order.status in active for leg in order.legs)]
    if len(touching) != 1:
        raise ValueError("exactly one active OCO group must protect this symbol")
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.EQUITY)).get(symbol)
    command = oco_exit_reprice(body, positions[0], touching[0], quote)
    return service, command, touching[0], quote, positions[0]


@app.post("/api/paper/oco/adjust/preview")
async def paper_oco_adjust_preview(request: Request):
    _guard(request)
    try:
        _, command, group, quote, position = await run_in_threadpool(
            _paper_oco_adjust, await request.json())
        selected = next(order for order in group.orders if order.order_id == command.order_id)
        other = next(order for order in group.orders if order.order_id != command.order_id)
        return {"ok": True, "symbol": position.symbol, "side": position.side.value,
                "quantity": str(position.quantity),
                "role": "stop" if command.stop_price is not None else "target",
                "old_price": str(selected.stop_price or selected.limit_price),
                "new_price": str(command.stop_price or command.limit_price),
                "other_price": str(other.stop_price or other.limit_price),
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "protection": "OCO replacement protection is unconfirmed until Alpaca verifies the new order"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper OCO verification failed; no replace was sent"}


@app.post("/api/paper/oco/adjust/submit")
async def paper_oco_adjust_submit(request: Request):
    _guard(request)
    try:
        body = await request.json()
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_send_paper_oco_adjust, body, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper OCO adjustment preflight failed; no replace was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _send_paper_oco_adjust(body: dict, request_id: str):
    with BROKER_ACTION_LOCK:
        service, command, original_group, _, original_position = _paper_oco_adjust(body)
        def preflight(account):
            _, checked, group, _, position = _paper_oco_adjust(body, account)
            if (checked != command or group != original_group or
                    position != original_position):
                raise ValueError("OCO changed before replacement; no replace was sent")
        return service.execute(command, request_id, preflight=preflight)


def _paper_oco_auto_be_plan(body: dict):
    service, _ = _paper_service()
    if (not isinstance(body, dict) or not isinstance(body.get("symbol"), str) or
            not body["symbol"]):
        raise ValueError("Choose a held Paper equity position for Auto B/E")
    symbol = body["symbol"]
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or account.status.upper() != "ACTIVE":
        raise ValueError("Paper account changed or is inactive")
    positions = [position for position in account.positions if position.symbol == symbol]
    if len(positions) != 1:
        raise ValueError("Exactly one held equity position is required")
    now = datetime.now(timezone.utc)
    active = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
    groups = service.adapter.query(OrdersQuery(now - timedelta(days=3650), now,
                                               statuses=active))
    touching = [group for group in groups if any(
        leg.symbol == symbol for order in group.orders
        if order.status in active for leg in order.legs)]
    if len(touching) != 1:
        raise ValueError("Exactly one active OCO group must protect this symbol")
    plan = oco_auto_be_plan(positions[0], touching[0])
    owned = service.store.get_mutation_by_client_id(plan["oco_client_id"])
    if owned is None or owned["status"] != "accepted" or owned["command"] != "SubmitOrder":
        raise ValueError("Auto B/E requires an OCO accepted through this app")
    quote = service.adapter.query(QuotesQuery((symbol,), AssetType.EQUITY)).get(symbol)
    if (quote is None or quote.symbol != symbol or quote.source != "Alpaca IEX" or
            not quote.realtime or not quote.timestamp_verified or
            quote.observed_at.tzinfo is None or
            not 0 <= (now - quote.observed_at).total_seconds() <= 15 or
            (quote.mark or quote.last) is None or (quote.mark or quote.last) <= 0):
        raise ValueError("Fresh verified IEX quote is required to set Auto B/E")
    watch = getattr(poller, "auto_be", None)
    if watch is None or watch.store is not service.store:
        raise ValueError("Paper Auto B/E monitor is not active for this account")
    return service, watch, plan, quote


@app.post("/api/paper/oco/auto-be/preview")
async def paper_oco_auto_be_preview(request: Request):
    _guard(request)
    try:
        _, watch, plan, quote = await run_in_threadpool(
            _paper_oco_auto_be_plan, await request.json())
        existing = watch.get(plan["symbol"])
        if existing and existing["status"] in ("watching", "sending", "unknown"):
            raise ValueError("An earlier Paper Auto B/E watch is still active or unresolved")
        return {"ok": True, **{key: str(value) if isinstance(value, Decimal) else value
                               for key, value in plan.items()},
                "quote": str(quote.mark or quote.last),
                "warning": "APP-WATCHED: at +1R the app attempts one fixed-stop replacement. The current OCO stop is broker-held; replacement protection is unconfirmed until Alpaca verifies the new OCO. Auto B/E needs this app running and fresh IEX quotes."}
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Paper Auto B/E preview failed; no watch was set"}


@app.post("/api/paper/oco/auto-be/set")
async def paper_oco_auto_be_set(request: Request):
    _guard(request)
    try:
        body = await request.json()
        result = await run_in_threadpool(_set_paper_oco_auto_be, body)
        return {"ok": True, **result}
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Paper Auto B/E setup failed; no watch was set"}


def _set_paper_oco_auto_be(body: dict) -> dict:
    with BROKER_ACTION_LOCK:
        service, watch, plan, _ = _paper_oco_auto_be_plan(body)
        if not service._market_open(datetime.now(timezone.utc)) or service.store.open_mutations():
            raise ValueError("Regular hours and resolved order outcomes are required for Auto B/E setup")
        reviewed = body.get("reviewed")
        if reviewed != {key: str(plan[key]) for key in
                        ("quantity", "basis", "initial_stop", "target", "trigger",
                         "new_stop", "group_id", "stop_id", "target_id")}:
            raise ValueError("OCO or Auto B/E risk changed since preview; review again")
        row = watch.set(plan)
        return {"symbol": row["symbol"], "status": row["status"],
                "trigger": str(row["trigger"]), "new_stop": str(row["new_stop"])}


@app.post("/api/paper/oco/auto-be/clear")
async def paper_oco_auto_be_clear(request: Request):
    _guard(request)
    try:
        body = await request.json()
        symbol = body.get("symbol") if isinstance(body, dict) else None
        if not isinstance(symbol, str) or len(symbol) > 10:
            raise ValueError("Paper Auto B/E symbol is required")
        def clear():
            with BROKER_ACTION_LOCK:
                service, _ = _paper_service()
                watch = getattr(poller, "auto_be", None)
                if watch is None or watch.store is not service.store:
                    raise ValueError("Paper Auto B/E monitor is unavailable")
                row = watch.get(symbol)
                if row and row["status"] == "unknown":
                    if body.get("acknowledge") is not True or not watch.acknowledge_unknown(symbol):
                        raise ValueError("Check Alpaca Paper and reconcile the unknown replacement before clearing")
                if not watch.clear(symbol):
                    raise ValueError("Auto B/E replacement is in progress; check Alpaca Paper")
                return {"ok": True, "symbol": symbol}
        return await run_in_threadpool(clear)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/paper/oco/auto-be/reconcile")
async def paper_oco_auto_be_reconcile(request: Request):
    _guard(request)
    try:
        request_id = str(UUID(str((await request.json()).get("request_id"))))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "request_id must be a UUID") from None
    def reconcile():
        with BROKER_ACTION_LOCK:
            service, _ = _paper_service()
            watch = getattr(poller, "auto_be", None)
            if watch is None or watch.store is not service.store or not any(
                    row["request_id"] == request_id for row in watch.list()):
                raise ValueError("Auto B/E request is not active for this Paper account")
            result = service.reconcile_replace(request_id)
            if result.status.value in ("accepted", "rejected"):
                watch.resolve_request(request_id, result.accepted,
                                      result.message or "Paper stop replacement reconciled")
            return result
    try:
        result = await run_in_threadpool(reconcile)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


@app.post("/api/paper/entry/cancel")
async def paper_entry_cancel(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        result = await run_in_threadpool(_cancel_paper_entry, order_id, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper cancel preflight failed; no cancel was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _cancel_paper_entry(order_id: str, request_id: str):
    with BROKER_ACTION_LOCK:
        service, _ = _paper_service()
        group = service.adapter.query(OrderQuery(order_id))
        order = next((item for item in group.orders if item.order_id == order_id), None)
        active = {OrderStatus.PENDING, OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED}
        if (order is None or group.order_class not in (OrderClass.BRACKET, OrderClass.SIMPLE) or
                order.role != OrderRole.ENTRY or order.parent_id is not None or
                order.status not in active or order.status_detail in ("PENDING_CANCEL", "PENDING_REPLACE") or
                len(order.legs) != 1 or
                order.legs[0].asset_type != AssetType.EQUITY or
                order.legs[0].filled_quantity != 0):
            raise ValueError("Only an unfilled Alpaca Paper equity entry can be canceled here")
        if group.order_class == OrderClass.SIMPLE and (
                not str(order.client_order_id or "").startswith("et-") or
                service.store.get_mutation_by_client_id(order.client_order_id) is None):
            raise ValueError("Only an app-created Paper simple entry can be canceled here")
        return service.execute(CancelOrder(order_id), request_id)


@app.post("/api/paper/entry/reprice")
async def paper_entry_reprice(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        request_id = str(UUID(str(body.get("request_id"))))
        price = Decimal(str(body.get("limit_price")))
        if not price.is_finite() or price <= 0:
            raise ValueError("limit price must be a positive finite number")
        result = await run_in_threadpool(_reprice_paper_entry, order_id, request_id, price)
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper replace preflight failed; no replace was sent"}
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


def _paper_reprice_check(order_id: str, price: Decimal):
    service, _ = _paper_service()
    group = service.adapter.query(OrderQuery(order_id))
    entry = next((item for item in group.orders if item.order_id == order_id), None)
    if (entry is None or group.order_class not in (OrderClass.BRACKET, OrderClass.SIMPLE) or
            entry.role != OrderRole.ENTRY or entry.parent_id is not None or
            entry.order_type != OrderType.LIMIT or entry.status != OrderStatus.WORKING or
            entry.status_detail not in (None, "NEW", "WORKING") or
            len(entry.legs) != 1 or entry.legs[0].asset_type != AssetType.EQUITY or
            entry.legs[0].filled_quantity != 0 or entry.limit_price is None):
        raise ValueError("Only a working, unfilled Alpaca Paper equity limit entry can be repriced")
    if group.order_class == OrderClass.SIMPLE:
        if (len(group.orders) != 1 or
                not str(entry.client_order_id or "").startswith("et-") or
                service.store.get_mutation_by_client_id(entry.client_order_id) is None):
            raise ValueError("Only an app-created Paper simple entry can be repriced here")
        stop = target = None
    else:
        targets = [order.limit_price for order in group.orders if order.role == OrderRole.TARGET
                   and order.limit_price is not None]
        stops = [order.stop_price for order in group.orders if order.role == OrderRole.STOP
                 and order.stop_price is not None]
        if len(targets) != 1 or len(stops) != 1:
            raise ValueError("Bracket target and stop could not be verified; nothing was sent")
        stop, target = stops[0], targets[0]
        instruction = entry.legs[0].instruction
        if instruction in (OrderInstruction.BUY, OrderInstruction.BUY_TO_OPEN):
            valid = stop < price < target
        elif instruction in (OrderInstruction.SELL, OrderInstruction.SELL_SHORT):
            valid = target < price < stop
        else:
            valid = False
        if not valid:
            raise ValueError("New entry price must stay between this bracket's stop and target")
    if price == entry.limit_price:
        raise ValueError("New entry price must differ from the current price")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or any(
            position.symbol == entry.legs[0].symbol for position in account.positions):
        raise ValueError("Account or position changed; no replace was sent")
    return service, entry, stop, target


@app.post("/api/paper/entry/reprice/preview")
async def paper_entry_reprice_preview(request: Request):
    _guard(request)
    try:
        body = await request.json()
        order_id = str(UUID(str(body.get("order_id"))))
        price = Decimal(str(body.get("limit_price")))
        if not price.is_finite() or price <= 0:
            raise ValueError("limit price must be a positive finite number")
        def check():
            with BROKER_ACTION_LOCK:
                _, entry, stop, target = _paper_reprice_check(order_id, price)
                return {"ok": True, "order_id": order_id, "symbol": entry.legs[0].symbol,
                        "old_price": str(entry.limit_price), "new_price": str(price),
                        "stop": str(stop) if stop is not None else None,
                        "target": str(target) if target is not None else None}
        return await run_in_threadpool(check)
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        return {"ok": False, "error": str(exc)}
    except HTTPException:
        raise
    except Exception:
        return {"ok": False, "error": "Alpaca Paper replace preflight failed; no replace was sent"}


def _reprice_paper_entry(order_id: str, request_id: str, price: Decimal):
    with BROKER_ACTION_LOCK:
        service, _, _, _ = _paper_reprice_check(order_id, price)
        return service.execute(ReplaceOrder(order_id, limit_price=price,
                                            client_order_id=f"et-reprice-{request_id}"), request_id)


@app.post("/api/paper/entry/reprice/reconcile")
async def paper_entry_reprice_reconcile(request: Request):
    _guard(request)
    try:
        request_id = str(UUID(str((await request.json()).get("request_id"))))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "request_id must be a UUID") from None
    def reconcile():
        with BROKER_ACTION_LOCK:
            service, _ = _paper_service()
            return service.reconcile_replace(request_id)
    result = await run_in_threadpool(reconcile)
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


@app.post("/api/paper/entry/cancel/reconcile")
async def paper_entry_cancel_reconcile(request: Request):
    _guard(request)
    try:
        request_id = str(UUID(str((await request.json()).get("request_id"))))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "request_id must be a UUID") from None
    def reconcile():
        with BROKER_ACTION_LOCK:
            service, _ = _paper_service()
            return service.reconcile_cancel(request_id)
    result = await run_in_threadpool(reconcile)
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


@app.post("/api/paper/entry/reconcile")
async def paper_entry_reconcile(request: Request):
    _guard(request)
    try:
        request_id = str(UUID(str((await request.json()).get("request_id"))))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "request_id must be a UUID") from None
    def reconcile():
        with BROKER_ACTION_LOCK:
            service, _ = _paper_service()
            result = service.reconcile_submit(request_id)
            watch = getattr(poller, "spread_stops", None)
            if watch is not None and watch.store is service.store and result.status.value in (
                    "accepted", "rejected"):
                watch.resolve_request(request_id, result.accepted, result.message or "Paper order reconciled")
            return result
    result = await run_in_threadpool(reconcile)
    return {"ok": result.accepted, "status": result.status.value,
            "order_id": result.order_id, "message": result.message,
            "request_id": request_id}


@app.post("/api/ui/select")
async def ui_select(request: Request):
    _guard(request)
    sym = (await request.json()).get("symbol")
    UI["sel"] = str(sym)[:40] if sym else None
    UI["sel_seq"] += 1
    return UI


@app.get("/api/ui/prefs")
def ui_prefs_get():
    try:
        return json.loads(trader.store.get_setting("ui_prefs") or "{}")
    except ValueError:
        return {}


@app.post("/api/ui/prefs")
async def ui_prefs_set(request: Request):
    _guard(request)
    body = await request.json()
    prefs = {k: body[k] for k in ("hotkeys", "partMode") if k in body}
    if "riskChoices" in body:
        try:
            vals = [round(float(v), 2) for v in body["riskChoices"]][:4]
        except (TypeError, ValueError):
            raise HTTPException(400, "risk amounts must be numbers")
        if len(vals) != 4 or any(not (0 < v <= 100000) for v in vals):
            raise HTTPException(400, "four risk amounts above $0")
        prefs["riskChoices"] = vals
    if "nostopOk" in body:
        # Cockpit: positions Simon holds without a stop on purpose ("SYM|SIDE|QTY" -> when). A new
        # size or side is a new key, so the alarm comes back by itself.
        v = body["nostopOk"]
        if not isinstance(v, dict) or len(v) > 200 or not all(
                isinstance(k, str) and len(k) <= 80 and isinstance(t, str) and len(t) <= 40 for k, t in v.items()):
            raise HTTPException(400, "nostopOk must be a small map of strings")
        prefs["nostopOk"] = v
    cur = ui_prefs_get()
    cur.update(prefs)
    trader.store.set_setting("ui_prefs", json.dumps(cur))
    UI["prefs_rev"] += 1
    return cur


@app.post("/api/floating/open")
async def floating_open(request: Request):
    """Open a floating Entry or Manage window (a separate windowless process)."""
    _guard(request)
    kind = (await request.json()).get("kind")
    if kind not in ("entry", "manage"):
        raise HTTPException(400, "kind must be entry or manage")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "companion.floating", kind],
                     cwd=str(Path(__file__).resolve().parent.parent))
    return {"ok": True}


@app.post("/api/ticket/plan")
async def ticket_plan(request: Request):
    _guard(request)
    try:
        req = await request.json()
        out = (await run_in_threadpool(trader.make_plan, req)).as_dict()
        q = trader.quote(req["symbol"], max_age=5.0)   # cache hit from make_plan: no extra call
        out.update({"name": q.get("name"), "last": q.get("last"), "change_pct": q.get("change_pct")})
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "errors": [f"{e}"[:300]]}, status_code=200)


@app.post("/api/ticket/lookup")
async def ticket_lookup(request: Request):
    """Company name and last price, so you can confirm the symbol before anything else."""
    _guard(request)
    sym = str((await request.json()).get("symbol") or "").strip().upper()
    if not sym or (len(sym) > 10 and not OCC_RE.match(sym)):      # a stock, or an OCC option symbol
        return {"found": False, "symbol": sym}
    try:
        return await run_in_threadpool(trader.quote, sym, 2.0)
    except Exception as e:
        return {"found": False, "symbol": sym, "error": f"{e}"[:200]}


@app.post("/api/ticket/expirations")
async def ticket_expirations(request: Request):
    """Option mode: the expiry dates of an underlying (cached 5 minutes)."""
    _guard(request)
    und = str((await request.json()).get("underlying") or "")
    try:
        return {"ok": True, "underlying": und.strip().upper(),
                "expirations": await run_in_threadpool(trader.expirations, und)}
    except Exception as e:
        return {"ok": False, "error": f"{e}"[:200]}


@app.post("/api/ticket/chain")
async def ticket_chain(request: Request):
    """Option mode: the strikes of one expiry around the money, calls or puts."""
    _guard(request)
    body = await request.json()
    try:
        out = await run_in_threadpool(trader.chain, str(body.get("underlying") or ""),
                                      str(body.get("expiration") or ""), str(body.get("put_call") or ""),
                                      int(body.get("strikes") or 16))
        return {"ok": True, **out}
    except Exception as e:
        return {"ok": False, "error": f"{e}"[:200]}


@app.post("/api/ticket/preview")
async def ticket_preview(request: Request):
    _guard(request)
    return await run_in_threadpool(trader.preview, await request.json())


# Order actions call Schwab and wait on it; they run in worker threads so the dashboard
# (/api/state) keeps updating while a close or cancel is in flight.

@app.post("/api/ticket/submit")
async def ticket_submit(request: Request):
    _guard(request)
    return await run_in_threadpool(trader.submit, await request.json(), poller._merged_orders(), set(poller.terminal))


@app.post("/api/ticket/cancel")
async def ticket_cancel(request: Request):
    """Cancel one unfilled entry. No arming needed: cancelling an entry only removes risk."""
    _guard(request)
    return await run_in_threadpool(trader.cancel_entry, int((await request.json()).get("order_id")))


@app.post("/api/ticket/cancel_entries")
async def ticket_cancel_entries(request: Request):
    _guard(request)
    tops = [o for o in poller._merged_orders() if o.get("orderId") not in poller.terminal]
    return await run_in_threadpool(trader.cancel_all_entries, tops)


@app.post("/api/position/cancel_orders")
async def position_cancel_orders(request: Request):
    """Cancel every working order for one symbol (entries, stops, targets); the position stays."""
    _guard(request)
    sym = str((await request.json()).get("symbol") or "")
    return await run_in_threadpool(trader.cancel_symbol_orders, sym, poller._merged_orders(),
                                   set(poller.terminal))


@app.post("/api/position/close")
async def position_close(request: Request):
    """One market order for the whole position; Schwab cancels its stop/target as it enters."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "")
    return await run_in_threadpool(trader.close_position, sym, poller._merged_orders(),
                                   set(poller.terminal), body.get("confirm_leg") is True)


@app.post("/api/position/move_stop")
async def position_move_stop(request: Request):
    """Move the position's stop(s) to a price, or to break-even with {"be": true}."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    price = body.get("stop")
    if body.get("be"):
        pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
        price = ((pos.get("r") or {}).get("entry") or pos.get("avg_price")) if pos else None
        if not price:
            return {"ok": False, "symbol": sym, "error": "No entry price known for break-even."}
    try:
        float(price)
    except (TypeError, ValueError):
        return {"ok": False, "symbol": sym, "error": "Enter a stop price."}
    return await run_in_threadpool(trader.move_stop, sym, price, poller._merged_orders(), set(poller.terminal))


@app.post("/api/position/partial")
async def position_partial(request: Request):
    """Take part of the position (pct 0-1 or a share count), mid or market, then rebuild the
    stop and target on the rest."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    prior = ((pos or {}).get("trail") or {}).get("level")
    return await run_in_threadpool(trader.take_partial, sym, body.get("pct"), body.get("shares"),
                                   poller._merged_orders(), set(poller.terminal), body.get("mode") or "mid", prior,
                                   body.get("confirm_leg") is True, _trail_choice(body), (pos or {}).get("trail"))


@app.post("/api/position/add")
async def position_add(request: Request):
    """Add shares to an open stock position ({"shares": n, "mode": mid|market|ask|bid,
    "stop_mode": price|risk}), then one exit OCO for the whole new size. An entry: needs arming."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    return await run_in_threadpool(trader.add_shares, sym, body.get("shares"), body.get("mode") or "mid",
                                   body.get("stop_mode") or "price", poller._merged_orders(), set(poller.terminal),
                                   (pos or {}).get("opened"))


@app.post("/api/position/set_target")
async def position_set_target(request: Request):
    """One target for the whole position at {"price": x} or {"r": n} (frozen R), paired with
    the current stop in one OCO."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    r = body.get("r")
    # R only from the ticket that opened THIS position (the row's Live R basis), never an older one.
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    return await run_in_threadpool(trader.set_target, sym, body.get("price"), float(r) if r else None,
                                   poller._merged_orders(), set(poller.terminal), pos and pos.get("r"),
                                   ((pos or {}).get("trail") or {}).get("level"), _trail_choice(body),
                                   (pos or {}).get("trail"))


def _trail_choice(body: dict) -> dict | None:
    """A rebuild of a trailing stop asks first: "restart" or "fixed", for the trailing order and the
    level the user was shown (sent back as trail_order / trail_reached)."""
    c = body.get("trail_choice")
    if c not in ("restart", "fixed"):
        return None
    return {"choice": c, "order": body.get("trail_order"), "reached": body.get("trail_reached")}


@app.post("/api/position/trail")
async def position_trail(request: Request):
    """Replace the exits with the kept target + a Schwab trailing stop: {"unit": "R"|"$"|"%",
    "value": n, "basis": "MARK"}. R uses the frozen basis of the ticket that opened this position."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    return await run_in_threadpool(trader.set_trail, sym, body.get("unit") or "R", body.get("value"),
                                   body.get("basis") or "MARK", poller._merged_orders(), set(poller.terminal),
                                   pos and pos.get("r"), ((pos or {}).get("trail") or {}).get("level"))


# Optional features (companion/journal, companion/spreads; a build may leave a feature out) add their own
# endpoints and name the page scripts the dashboard loads for them. The journal goes first: its
# /features/journal.js route must come before the spreads' /features/{name}.js.
try:
    from .journal import routes as _journal_routes
except ImportError:
    _journal_routes = None
try:
    from .spreads import routes as _feature_routes
except ImportError:
    _feature_routes = None
FEATURES = (_journal_routes.register(app, poller.store, _guard, demo=DEMO) if _journal_routes else []) + (
    _feature_routes.register(app, trader, poller, _guard, demo=DEMO,
                             action_lock=BROKER_ACTION_LOCK) if _feature_routes else [])


@app.post("/api/position/resolve_close")
async def position_resolve_close(request: Request):
    _guard(request)
    trader.resolve_close(str((await request.json()).get("symbol") or ""))
    return {"ok": True}


@app.post("/api/ticket/arm")
async def ticket_arm(request: Request):
    _guard(request)
    trader.arm(bool((await request.json()).get("armed")), reason="dashboard click")
    return trader.status()


@app.post("/api/ticket/resolve")
async def ticket_resolve(request: Request):
    _guard(request)
    trader.resolve(str((await request.json()).get("ticket_id")))
    return trader.status()


LOG = Path(__file__).resolve().parent.parent / "data" / "companion.log"


def main() -> None:
    import sys
    import uvicorn
    # The launcher runs us windowless (pythonw: no console to close by accident), so all output,
    # including any crash traceback, goes to data/companion.log.
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
        handlers = [logging.StreamHandler(sys.stderr)]               # already the log file
    else:
        handlers = [logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("companion").info("Edge Trader %s starting (pid %s)%s", __version__, os.getpid(),
                                        " in DEMO mode" if DEMO else "")
    # One process, no reload: exactly one poller.
    uvicorn.run(app, host="127.0.0.1", port=PORT, workers=1, log_level="warning")
