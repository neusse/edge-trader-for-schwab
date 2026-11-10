"""Paper-only Alpaca configuration and persisted broker selection.

Secrets stay in the process environment; selection contains only a broker key.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .capabilities import BROKERS

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
DEFAULT_BROKER = "alpaca-paper"
SELECTION_PATH = Path(__file__).resolve().parents[2] / "data" / "broker-selection.json"


@dataclass(frozen=True)
class AlpacaPaperCredentials:
    api_key: str
    secret_key: str


def _first(env: Mapping[str, str], *names: str) -> str:
    return next((env[name].strip() for name in names if env.get(name, "").strip()), "")


def paper_credentials(env: Mapping[str, str] | None = None) -> AlpacaPaperCredentials:
    env = os.environ if env is None else env
    endpoint = _first(env, "alpaca_paper_endpoint", "ALPACA_PAPER_ENDPOINT")
    if endpoint and endpoint.rstrip("/") != PAPER_ENDPOINT:
        raise ValueError("Alpaca Paper endpoint must be https://paper-api.alpaca.markets")
    key = _first(env, "alpaca_paper_api_key", "ALPACA_PAPER_API_KEY")
    secret = _first(env, "alpaca_paper_secret", "ALPACA_PAPER_SECRET")
    if not key or not secret:
        raise ValueError("Alpaca Paper credentials are missing: set alpaca_paper_api_key and alpaca_paper_secret")
    return AlpacaPaperCredentials(key, secret)


def paper_trading_client(env: Mapping[str, str] | None = None):
    """The only Alpaca trading client constructor used by the application."""
    credentials = paper_credentials(env)
    from alpaca.trading.client import TradingClient

    return TradingClient(credentials.api_key, credentials.secret_key, paper=True)


class BrokerSelection:
    def __init__(self, path: Path = SELECTION_PATH):
        self.path = path
        self._lock = threading.RLock()

    def get(self) -> str:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))["broker"]
            except FileNotFoundError:
                return DEFAULT_BROKER
            if value not in BROKERS:
                raise ValueError("saved broker selection is invalid")
            return value

    def set(self, broker: str) -> None:
        if broker not in BROKERS:
            raise ValueError("unknown broker selection")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"broker": broker}), encoding="utf-8")
            os.replace(temp, self.path)
