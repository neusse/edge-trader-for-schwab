"""The Schwab cadence and rate limit remain broker-owned and fail conservatively."""
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from companion.brokers.schwab_policy import SchwabReadPolicy


def test_rate_limit_prunes_old_requests_and_honors_cooldown():
    policy = SchwabReadPolicy()
    sent = deque([1.0] * policy.max_requests_per_minute)
    assert policy.throttle_delay(sent, 30.0, 0.0) == 31.0
    assert policy.throttle_delay(sent, 30.0, 65.0) == 35.0
    assert policy.throttle_delay(sent, 62.0, 0.0) == 0.0
    assert not sent


def test_retry_after_accepts_seconds_and_http_date():
    policy = SchwabReadPolicy()
    assert policy.retry_after("0") == 1.0
    assert policy.retry_after("12") == 12.0
    assert policy.retry_after("bad") == 30.0
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 50.0 < policy.retry_after(future) <= 60.0
