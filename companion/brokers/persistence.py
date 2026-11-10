"""Persist canonical fill snapshots in the account-scoped versioned transaction store."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .models import AssetType, Fill, OrderInstruction
from ..store import Store


def save_fills(store: Store, fills: tuple[Fill, ...]) -> None:
    if not store.scope or store.scope[0] != "alpaca-paper":
        raise ValueError("canonical paper fills require an Alpaca Paper account store")
    store.save_transactions([{
        "activityId": fill.activity_id, "time": fill.occurred_at.isoformat(),
        "orderId": fill.order_id, "symbol": fill.symbol,
        "assetType": fill.asset_type.value, "instruction": fill.instruction.value,
        "quantity": str(fill.quantity), "price": str(fill.price), "fees": str(fill.fees),
    } for fill in fills])


def load_fills(store: Store, since: datetime) -> tuple[Fill, ...]:
    if not store.scope or store.scope[0] != "alpaca-paper":
        raise ValueError("canonical paper fills require an Alpaca Paper account store")
    return tuple(Fill(
        activity_id=row["activityId"], order_id=row["orderId"],
        symbol=row["symbol"], asset_type=AssetType(row["assetType"]),
        instruction=OrderInstruction(row["instruction"]),
        quantity=Decimal(row["quantity"]), price=Decimal(row["price"]),
        occurred_at=datetime.fromisoformat(row["time"]), fees=Decimal(row["fees"]),
    ) for row in store.transactions_since(since.isoformat()))
