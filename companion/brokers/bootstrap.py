"""Read-only Alpaca startup while the full polling and order paths are being migrated."""
from __future__ import annotations

import threading
from pathlib import Path

from .alpaca_paper import AlpacaPaperAdapter
from .models import AccountQuery
from ..store import Store


class AlpacaBootstrapPoller:
    def __init__(self):
        self.store = Store(Path(":memory:"))
        self.trader = None
        self.terminal: set[str] = set()
        self.orders_today: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._state = {"status": "Alpaca Paper connecting", "booting": True,
                       "positions": [], "orders": []}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_account, name="alpaca-paper-bootstrap", daemon=True)
        self._thread.start()

    def _read_account(self) -> None:
        try:
            account = AlpacaPaperAdapter().query(AccountQuery())
            # The account identity is discovered before touching any persistent paper state.
            scoped = Store.for_broker("alpaca-paper", account.account_ref)
            scoped.close()
            status = "Alpaca Paper connected; dashboard and trading integration in progress"
            state = {"status": status, "booting": False, "positions": [], "orders": [],
                     "account_status": account.status, "broker_ready": True}
        except Exception as exc:
            state = {"status": f"Alpaca Paper: {exc}"[:200], "booting": False,
                     "positions": [], "orders": [], "broker_ready": False}
        with self._lock:
            self._state = state

    def stop(self) -> None:
        if self._thread:
            self._thread.join(timeout=2)
        self.store.close()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _merged_orders(self) -> list[dict]:
        return []

    def note_external_request(self, retry_after=None) -> None:
        pass

    def cooldown_left(self) -> float:
        return 0.0
