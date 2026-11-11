r"""Capture redacted, read-only Alpaca Paper evidence for a controlled scenario.

Run from the repository root with the app already running::

    .venv\Scripts\python.exe scripts\capture_paper_evidence.py baseline

The JSONL output stays under the ignored data/ directory. This script never arms or sends orders.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from companion.brokers.alpaca_paper import AlpacaPaperAdapter  # noqa: E402
from companion.brokers.models import (AccountQuery, FillsQuery, MarketClockQuery,
                                      OrderStatus, OrdersQuery)  # noqa: E402

ACTIVE = frozenset((OrderStatus.PENDING, OrderStatus.WORKING,
                    OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN))
ET = ZoneInfo("America/New_York")
DATA = ROOT / "data"


class PreflightRefused(ValueError):
    """Safe, identifier-free reason to delay the first controlled Paper action."""


def ref(value: object, salt: bytes) -> str:
    """Stable opaque reference for comparing snapshots without disclosing broker IDs."""
    return hmac.new(salt, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def _json(port: int, path: str) -> dict:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as response:
        return json.load(response)


def _git_revision() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def collect(scenario: str, app: dict, status: dict, account, groups, fills,
            *, now: datetime, revision: str, salt: bytes) -> dict:
    """Build a redacted snapshot from independent app and broker reads."""
    broker = app.get("broker") or {}
    if broker.get("key") != "alpaca-paper" or broker.get("mode") != "paper":
        raise ValueError("Alpaca Paper must be the selected app broker")
    if not status.get("ok") or account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca Paper status is not ready")
    opaque = lambda value: ref(value, salt)
    app_positions = [{
        "symbol_ref": opaque(p.get("symbol")), "asset": p.get("asset_type"),
        "side": p.get("side"), "quantity": str(p.get("qty")),
        "protection": p.get("protection"), "stop_quantity": str(p.get("stop_qty") or 0),
        "quote_stale": bool(p.get("quote_stale")),
    } for p in app.get("positions") or ()]
    app_orders = [{
        "order_ref": opaque(o["order_id"]) if o.get("order_id") else None,
        "symbol_ref": opaque(o["symbol"]) if o.get("symbol") else None,
        "asset": o.get("asset_type"), "role": o.get("role"),
        "status": o.get("status"), "group_class": o.get("group_type"),
        "quantity": str(o.get("qty")), "filled": str(o.get("filled") or 0),
        "app_created": str(o.get("tag") or "").startswith("et-"),
    } for o in app.get("orders") or ()]
    paper_positions = [{
        "symbol_ref": opaque(p.symbol), "asset": p.asset_type.value,
        "side": p.side.value, "quantity": str(p.quantity),
    } for p in account.positions]
    paper_orders = [{
        "group_ref": opaque(group.group_id), "group_class": group.order_class.value,
        "order_ref": opaque(order.order_id), "role": order.role.value,
        "status": order.status.value,
        "client_ref": opaque(order.client_order_id) if order.client_order_id else None,
        "app_created": str(order.client_order_id or "").startswith("et-"),
        "legs": [{"symbol_ref": opaque(leg.symbol), "asset": leg.asset_type.value,
                  "quantity": str(leg.quantity), "filled": str(leg.filled_quantity)}
                 for leg in order.legs],
    } for group in groups for order in group.orders if order.status in ACTIVE]
    paper_fills = [{
        "fill_ref": opaque(fill.activity_id), "order_ref": opaque(fill.order_id) if fill.order_id else None,
        "symbol_ref": opaque(fill.symbol), "asset": fill.asset_type.value,
        "instruction": fill.instruction.value, "quantity": str(fill.quantity),
        "occurred_at": fill.occurred_at.isoformat(),
    } for fill in fills]
    return {
        "scenario": scenario, "captured_at": now.isoformat(), "commit": revision,
        "app": {"broker": "alpaca-paper", "ready": bool(app.get("broker_ready")),
                "market_open": bool((app.get("market_hours") or {}).get("is_open")),
                "armed": bool(status.get("armed")),
                "positions": app_positions, "working_orders": app_orders,
                "unresolved_request_refs": [opaque(row.get("request_id"))
                                            for row in status.get("unresolved") or ()]},
        "paper": {"account_ref": opaque(account.account_ref), "status": account.status,
                  "positions": paper_positions, "working_orders": paper_orders,
                  "fills_today": paper_fills},
    }


def require_clean_market(record: dict, broker_clock, now: datetime) -> None:
    """Verify an empty, open Paper session before the first controlled order."""
    app, paper = record["app"], record["paper"]
    if (not broker_clock.is_open or broker_clock.observed_at.tzinfo is None or
            not 0 <= (now - broker_clock.observed_at).total_seconds() <= 60 or
            broker_clock.next_close <= now or not app["market_open"]):
        raise PreflightRefused("the app and Alpaca Paper must both report a fresh open session")
    if not app["ready"]:
        raise PreflightRefused("the selected Paper app is not ready")
    if app["armed"] or app["unresolved_request_refs"]:
        raise PreflightRefused("the app must be disarmed with no unresolved mutation")
    if (app["positions"] or app["working_orders"] or paper["positions"] or
            paper["working_orders"] or paper["fills_today"]):
        raise PreflightRefused("the Paper account must have no positions, working orders, or same-day fills")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", help="Short scenario label, such as baseline or bracket-filled")
    parser.add_argument("--port", type=int, default=8770, help="Local Edge Trader port (default: 8770)")
    parser.add_argument("--output", default=str(DATA / "paper-e2e-evidence.jsonl"),
                        help="JSONL file under the repository's ignored data/ directory")
    parser.add_argument("--require-clean-market", action="store_true",
                        help="Require a fresh open Paper session and empty account before writing")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.scenario):
        parser.error("scenario must be 1-40 lowercase letters, digits, or hyphens")
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1-65535")
    output = Path(args.output).resolve()
    if not output.is_relative_to(DATA.resolve()):
        parser.error("output must stay under the repository's ignored data/ directory")
    record_written = False
    try:
        app = _json(args.port, "/api/state")
        status = _json(args.port, "/api/paper/status")
        if (app.get("broker") or {}).get("key") != "alpaca-paper":
            raise ValueError("Alpaca Paper must be selected before any broker read")
        load_dotenv(ROOT / ".env", override=False)
        adapter = AlpacaPaperAdapter()
        now = datetime.now(timezone.utc)
        account = adapter.query(AccountQuery())
        groups = adapter.query(OrdersQuery(now - timedelta(days=3650), now, statuses=ACTIVE))
        et_midnight = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        fills = adapter.query(FillsQuery(et_midnight.astimezone(timezone.utc), now))
        salt_path = DATA / ".paper-e2e-salt"
        DATA.mkdir(parents=True, exist_ok=True)
        if not salt_path.exists():
            try:
                with salt_path.open("xb") as stream:
                    stream.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
        salt = salt_path.read_bytes()
        if len(salt) != 32:
            raise ValueError("local evidence salt is invalid")
        record = collect(args.scenario, app, status, account, groups, fills,
                         now=now, revision=_git_revision(), salt=salt)
        if args.require_clean_market:
            broker_clock = adapter.query(MarketClockQuery())
            require_clean_market(record, broker_clock, now)
            record["preflight"] = {"clean_market": True,
                                   "broker_clock_at": broker_clock.observed_at.isoformat()}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        record_written = True
        print(f"Saved {args.scenario}: {len(record['paper']['positions'])} positions, "
              f"{len(record['paper']['working_orders'])} working orders, "
              f"{len(record['paper']['fills_today'])} fills today -> {output}")
    except PreflightRefused as exc:
        parser.exit(2, f"Preflight refused: {exc}; no record written.\n")
    except Exception as exc:
        detail = "record may already be written" if record_written else "no record written"
        parser.exit(1, f"Evidence capture failed ({type(exc).__name__}); {detail}.\n")


if __name__ == "__main__":
    main()
