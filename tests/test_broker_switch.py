import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.brokers.bootstrap import AlpacaBootstrapPoller
from companion.brokers.configuration import BrokerSelection
from companion.spreads import routes
from companion.poller import Poller
from companion.store import Store
from companion.trading import Trader


def test_switch_disarms_paper_service_stops_poller_and_isolates_store(monkeypatch, tmp_path):
    import companion.app as appmod

    events = []

    class PaperService:
        def arm(self, enabled):
            events.append(("paper-arm", enabled))

    class OldPaperPoller(AlpacaBootstrapPoller):
        def stop(self):
            events.append(("stop", "alpaca-paper"))
            super().stop()

    class NextPoller:
        def __init__(self, key):
            self.key = key
            self.store = Store(tmp_path / f"{key}.db")
            self.trader = None
        def start(self): events.append(("start", self.key))
        def stop(self): self.store.close()
        def note_external_request(self, retry_after=None): pass
        def cooldown_left(self): return 0.0

    old = OldPaperPoller()
    old.order_service = PaperService()
    old.store.set_setting("scope_marker", "old Paper state")
    old_trader = Trader(old.store)
    old_trader.arm(True)
    old_arm = old_trader.arm

    def recorded_arm(enabled, reason="dashboard"):
        events.append(("trader-arm", enabled))
        return old_arm(enabled, reason=reason)

    monkeypatch.setattr(old_trader, "arm", recorded_arm)
    selected = BrokerSelection(tmp_path / "selection.json")
    created = {}

    def make_poller(key):
        created[key] = NextPoller(key)
        return created[key]

    monkeypatch.setattr(appmod, "poller", old)
    monkeypatch.setattr(appmod, "trader", old_trader)
    monkeypatch.setattr(appmod, "BROKER_KEY", "alpaca-paper")
    monkeypatch.setattr(appmod, "DEMO", False)
    monkeypatch.setattr(appmod, "DEMO_OVERRIDE", False)
    monkeypatch.setattr(appmod, "_feature_routes", None)
    monkeypatch.setattr(appmod, "UI", dict(appmod.UI))
    monkeypatch.setattr(appmod, "BrokerSelection", lambda: selected)
    monkeypatch.setattr(appmod, "make_poller", make_poller)
    client = TestClient(appmod.app, base_url=f"http://127.0.0.1:{appmod.PORT}")
    try:
        result = client.post("/api/brokers/select", json={"broker": "demo"},
                             headers={"X-Companion": "1"})
        assert result.status_code == 200 and result.json()["active"] == "demo"
        assert selected.get() == "demo"
        assert events[:3] == [("trader-arm", False), ("paper-arm", False),
                              ("stop", "alpaca-paper")]
        assert events[-1] == ("start", "demo")
        assert appmod.trader.store is created["demo"].store
        assert not appmod.trader.armed
        assert created["demo"].store.get_setting("scope_marker") is None
    finally:
        if "demo" in created:
            created["demo"].stop()


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
