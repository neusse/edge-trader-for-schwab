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
from .brokers.models import (AccountQuery, AssetType, CancelOrder,
                             OrderClass, OrderInstruction, OrderQuery, OrderRole,
                             OrderStatus, OrderType, OrdersQuery, QuotesQuery, ReplaceOrder,
                             SubmitOrder)
from .brokers.paper_equity import (add_plan, close_plan, entry_plan, oco_exit_reprice,
                                   oco_plan, trailing_plan)
from .brokers.paper_options import (chain_query, chain_response, expirations_query,
                                    expirations_response, long_entry_plan,
                                    option_close_plan)
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
    "alpaca-paper": ("equity-bracket", "equity-oco", "equity-close", "equity-partial",
                     "equity-oco-adjust", "equity-add", "equity-trailing",
                     "option-long-entry", "option-close"),
    "demo": (),
}
BROKER_HELP = {
    "schwab": "Schwab ticket, position management, and spreads are available when account data is fresh.",
    "alpaca-paper": "Paper equity bracket and held-position management are available. A level-2-or-higher Paper account can open a long call or put with a reviewed limit order, and an order-free held option can be closed with a reviewed limit order. Other option and spread workflows are still being connected.",
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
    return POLLERS[broker_key]()


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
        return {"ok": True, "symbol": intent.entry.leg.symbol,
                "side": intent.entry.leg.instruction.value,
                "quantity": str(intent.entry.leg.quantity),
                "entry": intent.entry.order_type.value,
                "limit_price": str(intent.entry.limit_price) if intent.entry.limit_price else None,
                "quote": str(quote.mark or quote.last), "quote_source": quote.source,
                "target": str(intent.take_profit_price), "stop": str(intent.stop_loss.stop_price),
                "protection": "Broker-held bracket target and stop after entry fills"}
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
    return service, positions[0], quote


def _paper_oco(body: dict):
    service, position, quote = _paper_order_free_position(body)
    return service, oco_plan(body, position, quote), quote


def _paper_close(body: dict):
    service, position, quote = _paper_order_free_position(body)
    return service, close_plan(body, position, quote), quote, position.quantity


def _paper_add(body: dict):
    service, position, quote = _paper_order_free_position(body)
    return service, add_plan(body, position, quote), quote, position.quantity


def _paper_trailing(body: dict):
    service, position, quote = _paper_order_free_position(body)
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
        if (order is None or group.order_class != OrderClass.BRACKET or
                order.role != OrderRole.ENTRY or order.parent_id is not None or
                order.status not in active or order.status_detail in ("PENDING_CANCEL", "PENDING_REPLACE") or
                len(order.legs) != 1 or
                order.legs[0].asset_type != AssetType.EQUITY or
                order.legs[0].filled_quantity != 0):
            raise ValueError("Only an unfilled Alpaca Paper equity entry can be canceled here")
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
    if (entry is None or group.order_class != OrderClass.BRACKET or
            entry.role != OrderRole.ENTRY or entry.parent_id is not None or
            entry.order_type != OrderType.LIMIT or entry.status != OrderStatus.WORKING or
            entry.status_detail not in (None, "NEW", "WORKING") or
            len(entry.legs) != 1 or entry.legs[0].asset_type != AssetType.EQUITY or
            entry.legs[0].filled_quantity != 0 or entry.limit_price is None):
        raise ValueError("Only a working, unfilled Alpaca Paper equity bracket limit entry can be repriced")
    targets = [order.limit_price for order in group.orders if order.role == OrderRole.TARGET
               and order.limit_price is not None]
    stops = [order.stop_price for order in group.orders if order.role == OrderRole.STOP
             and order.stop_price is not None]
    if len(targets) != 1 or len(stops) != 1:
        raise ValueError("Bracket target and stop could not be verified; nothing was sent")
    instruction = entry.legs[0].instruction
    if instruction in (OrderInstruction.BUY, OrderInstruction.BUY_TO_OPEN):
        valid = stops[0] < price < targets[0]
    elif instruction in (OrderInstruction.SELL, OrderInstruction.SELL_SHORT):
        valid = targets[0] < price < stops[0]
    else:
        valid = False
    if not valid or price == entry.limit_price:
        raise ValueError("New entry price must differ and stay between this bracket's stop and target")
    account = service.adapter.query(AccountQuery())
    if account.account_ref != service.account_ref or any(
            position.symbol == entry.legs[0].symbol for position in account.positions):
        raise ValueError("Account or position changed; no replace was sent")
    return service, entry, stops[0], targets[0]


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
                        "stop": str(stop), "target": str(target)}
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
    service, _ = _paper_service()
    result = await run_in_threadpool(service.reconcile_submit, request_id)
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
