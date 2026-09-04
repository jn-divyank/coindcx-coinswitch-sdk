"""Rate limiter tests. Offline, and fast - windows are milliseconds, not minutes."""

from __future__ import annotations

import threading
import time

import pytest

from dcx.core.errors import RateLimitError
from dcx.core.ratelimit import (
    COINSWITCH_LIMITS,
    Limit,
    RateLimiter,
    coindcx_limiter,
    coinswitch_limiter,
)
from dcx.core.transport import Transport


def test_allows_calls_up_to_the_limit():
    limiter = RateLimiter({"/x": Limit(3, 60)})
    assert all(limiter.acquire("/x", block=False) for _ in range(3))
    assert limiter.acquire("/x", block=False) is False


def test_window_slides_so_slots_come_back():
    limiter = RateLimiter({"/x": Limit(2, 0.2)})
    assert limiter.acquire("/x", block=False)
    assert limiter.acquire("/x", block=False)
    assert limiter.acquire("/x", block=False) is False
    time.sleep(0.25)
    assert limiter.acquire("/x", block=False) is True


def test_blocking_acquire_waits_then_succeeds():
    limiter = RateLimiter({"/x": Limit(1, 0.15)})
    assert limiter.acquire("/x")
    started = time.monotonic()
    assert limiter.acquire("/x", block=True, timeout=2.0)
    assert time.monotonic() - started >= 0.1


def test_blocking_acquire_gives_up_at_timeout():
    """A caller stuck behind a 60s window should hear about it, not hang."""
    limiter = RateLimiter({"/x": Limit(1, 60)})
    assert limiter.acquire("/x")
    assert limiter.acquire("/x", block=True, timeout=0.1) is False


def test_paths_have_independent_budgets():
    limiter = RateLimiter({"/a": Limit(1, 60), "/b": Limit(1, 60)})
    assert limiter.acquire("/a", block=False)
    assert limiter.acquire("/b", block=False)
    assert limiter.acquire("/a", block=False) is False


def test_query_string_does_not_split_the_budget():
    """Otherwise varying a parameter would silently bypass the limit."""
    limiter = RateLimiter({"/x": Limit(1, 60)})
    assert limiter.acquire("/x?a=1", block=False)
    assert limiter.acquire("/x?a=2", block=False) is False


def test_unknown_paths_use_the_default():
    limiter = RateLimiter({}, default=Limit(2, 60))
    assert limiter.acquire("/anything", block=False)
    assert limiter.acquire("/anything", block=False)
    assert limiter.acquire("/anything", block=False) is False


def test_limiter_is_thread_safe():
    """A background loop and a foreground call share one budget."""
    limiter = RateLimiter({"/x": Limit(50, 60)})
    granted: list[bool] = []
    lock = threading.Lock()

    def worker():
        got = limiter.acquire("/x", block=False)
        with lock:
            granted.append(got)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 50, "limiter over- or under-granted under contention"


def test_snapshot_reports_usage():
    limiter = RateLimiter({"/x": Limit(5, 60)})
    limiter.acquire("/x")
    limiter.acquire("/x")
    assert limiter.snapshot()["/x"] == "2/5 per 60s"


def test_published_coinswitch_limits_are_loaded():
    limiter = coinswitch_limiter()
    # Futures order placement is the tightest one that matters: 20 per 60s.
    assert limiter.limit_for("/trade/api/v2/futures/order") == Limit(20, 60)
    assert limiter.limit_for("/trade/api/v2/order") == Limit(100, 10)
    assert len(COINSWITCH_LIMITS) >= 18


def test_coindcx_limiter_is_conservative():
    """CoinDCX publishes no figures, so the default must be defensive."""
    limit = coindcx_limiter().limit_for("/exchange/v1/orders/create")
    assert limit.calls / limit.per_seconds <= 5


def test_transport_refuses_rather_than_queueing_forever():
    """Hitting the local limit raises, and crucially sends nothing."""

    class NeverCalled:
        headers: dict = {}

        def request(self, *a, **kw):
            raise AssertionError("request must not reach the network")

        def close(self):
            pass

    transport = Transport(
        "https://example.test",
        session=NeverCalled(),
        limiter=RateLimiter({"/x": Limit(0, 60)}),
    )
    with pytest.raises(RateLimitError, match="not sent"):
        transport.request("GET", "/x")
