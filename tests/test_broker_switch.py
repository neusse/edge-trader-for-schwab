import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.brokers.configuration import BrokerSelection
from companion.spreads import routes
from companion.poller import Poller
from companion.store import Store
from companion.trading import Trader


def test_switch_stop_failure_stays_disarmed_and_requires_restart(monkeypatch, tmp_path):
    import companion.app as appmod

    class StuckPoller:
        def __init__(self):
            self.store = Store(tmp_path / "old.db")
            self.trader = None
        def stop(self): raise RuntimeError("poller still active")
    old = StuckPoller()
    selected = BrokerSelection(tmp_path / "selection.json")
    monkeypatch.setattr(appmod, "poller", old)
    monkeypatch.setattr(appmod, "trader", Trader(old.store))
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "DEMO", False)
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selected)
    monkeypatch.setattr(appmod, "make_poller", lambda key: (_ for _ in ()).throw(
        AssertionError("new poller started before old stopped")))
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    try:
        result = client.post("/api/brokers/select", json={"broker": "schwab"},
                             headers={"X-Companion": "1"})
        assert result.status_code == 503
        assert result.json()["restart_required"]
        assert selected.get() == "schwab"
        assert appmod.BROKER_KEY == "alpaca-paper"
        assert not appmod.trader.status()["armed"]
    finally:
        old.store.close()


def test_old_spread_stop_callback_cannot_send_after_runtime_rebind(monkeypatch):
    sent = []
    ready = threading.Event()
    def fake_auto_stop(self, spread_id):
        sent.append((self.t, spread_id))
        ready.set()
    monkeypatch.setattr(routes.SpreadDesk, "auto_stop", fake_auto_stop)
    old = SimpleNamespace(spreads=SimpleNamespace(on_fire=None))
    new = SimpleNamespace(spreads=SimpleNamespace(on_fire=None))
    poller = SimpleNamespace(_merged_orders=lambda: [])
    app = FastAPI()
    routes.register(app, old, poller, lambda request: None)
    old_fire = old.spreads.on_fire
    app.state.bind_spread_runtime(new, poller, False)
    old_fire("old-spread")
    assert not ready.wait(0.1)
    new.spreads.on_fire("new-spread")
    assert ready.wait(1)
    assert sent == [(new, "new-spread")]


def test_schwab_poller_stop_waits_for_started_auto_be(tmp_path):
    store = Store(tmp_path / "poller.db")
    poller = Poller(store)
    release = threading.Event()
    started = threading.Event()
    stopped = threading.Event()
    worker = threading.Thread(target=lambda: (started.set(), release.wait(1)))
    poller._auto_threads.append(worker)
    worker.start()
    assert started.wait(1)
    stopper = threading.Thread(target=lambda: (poller.stop(), stopped.set()))
    stopper.start()
    try:
        assert not stopped.wait(0.05)
        release.set()
        assert stopped.wait(1)
    finally:
        release.set()
        stopper.join(1)
        worker.join(1)
        store.close()
