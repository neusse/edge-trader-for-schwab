"""Read-only Schwab prices for option holdings in an Alpaca Paper account."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .models import AssetType, Quote
from .paper_options import OCC
from .schwab_adapter import normalize_quotes


def schwab_occ_symbol(symbol: str) -> str:
    """Pad an Alpaca OCC symbol to Schwab's six-character root format."""
    match = OCC.fullmatch(symbol)
    if match is None:
        raise ValueError("Paper option holding has an invalid OCC symbol")
    root, year, month, day, kind, strike = match.groups()
    return f"{root:<6}{year}{month}{day}{kind}{strike}"


class SchwabOptionQuoteReader:
    def __init__(self, client_factory=None):
        self._client_factory = client_factory
        self._client = None

    def _ready(self):
        if self._client is None:
            if self._client_factory is None:
                from ..schwab import make_client
                self._client = make_client(frozenset({"quotes"}))
            else:
                self._client = self._client_factory()
        return self._client

    def query(self, symbols: tuple[str, ...]) -> dict[str, Quote]:
        """Make one batched Schwab market-data request, with no account or order calls."""
        if not symbols:
            return {}
        requested = {symbol: schwab_occ_symbol(symbol) for symbol in symbols}
        padded = tuple(dict.fromkeys(requested.values()))
        response = self._ready().quotes(list(padded))
        if response.status_code != 200:
            raise RuntimeError(f"Schwab option quote read failed (HTTP {response.status_code})")
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("Schwab option quote response is invalid")
        by_occ = {key.replace(" ", ""): value for key, value in raw.items()
                  if isinstance(key, str) and isinstance(value, dict) and
                  value.get("assetMainType", "OPTION") == "OPTION"}
        normalized_raw = {key: by_occ[key.replace(" ", "")] for key in padded
                          if key.replace(" ", "") in by_occ}
        normalized = normalize_quotes(normalized_raw, padded, AssetType.OPTION)
        return {symbol: replace(normalized[key], symbol=symbol,
                                observed_at=(normalized[key].observed_at
                                             if normalized[key].timestamp_verified else
                                             datetime(1970, 1, 1, tzinfo=timezone.utc)))
                for symbol, key in requested.items() if key in normalized}
