"""Application authorization rules for broker order intents."""

from dataclasses import fields, is_dataclass

from .models import AssetType, OrderLegIntent


def contains_option_leg(value: object) -> bool:
    """Inspect nested canonical intents before any broker mutation is sent."""
    if isinstance(value, OrderLegIntent):
        return value.asset_type == AssetType.OPTION
    if isinstance(value, (tuple, list)):
        return any(contains_option_leg(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(contains_option_leg(getattr(value, field.name)) for field in fields(value))
    return False
