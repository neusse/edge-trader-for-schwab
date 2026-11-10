from datetime import datetime, timezone
from decimal import Decimal

from companion.brokers.models import AssetType, Fill, OrderInstruction
from companion.brokers.persistence import load_fills, save_fills
from companion.store import Store


def test_paper_fill_versions_preserve_opaque_ids_and_corrections(tmp_path):
    when = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    first = Fill("activity::uuid", "order-uuid", "AAPL", AssetType.EQUITY,
                 OrderInstruction.BUY, Decimal("1"), Decimal("100"), when)
    corrected = Fill("activity::uuid", "order-uuid", "AAPL", AssetType.EQUITY,
                     OrderInstruction.BUY, Decimal("1"), Decimal("100.01"), when)
    try:
        save_fills(store, (first,))
        save_fills(store, (corrected,))
        assert load_fills(store, when)[0] == corrected
        assert store._con.execute("select count(*) from transaction_versions").fetchone()[0] == 2
    finally:
        store.close()


def test_paper_fill_round_trip_keeps_underlying_and_multiplier(tmp_path):
    when = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
    store = Store.for_broker("alpaca-paper", "paper-account", tmp_path)
    fill = Fill("activity::option", "order-option", "AAPL261113C00100000",
                AssetType.OPTION, OrderInstruction.BUY_TO_OPEN,
                Decimal("1"), Decimal("2.50"), when,
                underlying="AAPL", multiplier=Decimal("100"))
    try:
        save_fills(store, (fill,))
        assert load_fills(store, when) == (fill,)
    finally:
        store.close()
