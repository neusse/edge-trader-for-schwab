"""Schwab REST read cadence, response caps, and shared rate-limit policy."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SchwabReadPolicy:
    intervals: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({
        "account": 5, "orders_today": 5, "orders_sweep": 30,
        "transactions": 10, "quotes": 3, "corrections": 600, "hours": 1800,
    }))
    lookback_days: int = 60
    transaction_cap: int = 3000
    order_sweep_days: int = 59
    sweep_statuses: tuple[str, ...] = ("WORKING", "PENDING_ACTIVATION")
    max_requests_per_minute: int = 70
    details_per_sweep: int = 3

    def throttle_delay(self, sent: deque[float], now: float,
                       cooldown_until: float) -> float:
        while sent and now - sent[0] > 60:
            sent.popleft()
        return max(cooldown_until - now,
                   sent[0] + 60 - now if len(sent) >= self.max_requests_per_minute else 0)

    @staticmethod
    def retry_after(value: str | None) -> float:
        if not value:
            return 30.0
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                return max(1.0, (parsedate_to_datetime(value) -
                                 datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 30.0
