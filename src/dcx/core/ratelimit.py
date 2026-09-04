"""Client-side rate limiting.

A 24/7 agent polling positions in a loop will hit these limits, and a 429 in the
middle of managing a position is a bad time to discover them. Better to pace
locally than to be throttled remotely.

The limits below are CoinSwitch's published per-key figures. CoinDCX does not
document per-endpoint numbers, so it gets a conservative global default that can
be raised once real behaviour is observed.

The limiter is a sliding window rather than a token bucket, because that is how
the published limits are phrased ("20 per 60s"): it holds the timestamps of
recent calls and waits only as long as the oldest one needs to age out.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    """``calls`` requests allowed per ``per_seconds``."""

    calls: int
    per_seconds: float

    def __str__(self) -> str:
        return f"{self.calls}/{self.per_seconds:g}s"


# CoinSwitch, per API key, from the published errors-and-rate-limits page.
COINSWITCH_LIMITS: dict[str, Limit] = {
    # Spot
    "/trade/api/v2/order": Limit(100, 10),
    "/trade/api/v2/orders": Limit(10_000, 10),
    "/trade/api/v2/user/portfolio": Limit(5_000, 10),
    "/trade/api/v2/tds": Limit(25, 10),
    # Futures - notably tighter, and per 60s rather than per 10s
    "/trade/api/v2/futures/order": Limit(20, 60),
    "/trade/api/v2/futures/cancel": Limit(10, 60),
    "/trade/api/v2/futures/cancel_all": Limit(10, 60),
    "/trade/api/v2/futures/order_status": Limit(20, 60),
    "/trade/api/v2/futures/open_orders": Limit(20, 60),
    "/trade/api/v2/futures/closed_orders": Limit(20, 60),
    "/trade/api/v2/futures/positions": Limit(20, 60),
    "/trade/api/v2/futures/leverage": Limit(20, 60),
    "/trade/api/v2/futures/update_leverage": Limit(10, 60),
    "/trade/api/v2/futures/add_margin": Limit(10, 60),
    "/trade/api/v2/futures/wallet_balance": Limit(20, 60),
    "/trade/api/v2/futures/transactions": Limit(20, 60),
    "/trade/api/v2/futures/instrument_info": Limit(100, 60),
    "/trade/api/v2/futures/depth": Limit(100, 60),
    "/trade/api/v2/futures/ticker": Limit(100, 60),
    "/trade/api/v2/futures/klines": Limit(30, 60),
    "/trade/api/v2/futures/trades": Limit(100, 60),
}

#: Applied to any path without a specific entry. CoinSwitch calls this
#: "global fair-use"; the number is ours, chosen to be unobtrusive.
DEFAULT_LIMIT = Limit(60, 10)

#: CoinDCX publishes no per-endpoint limits. Deliberately conservative.
COINDCX_DEFAULT_LIMIT = Limit(30, 10)


class RateLimiter:
    """Sliding-window limiter, keyed by endpoint path.

    Thread-safe, so a background loop and a foreground call share one budget
    rather than each assuming it has the whole thing.

    >>> limiter = RateLimiter({"/x": Limit(2, 60)}, default=Limit(100, 1))
    >>> limiter.acquire("/x", block=False)
    True
    >>> limiter.acquire("/x", block=False)
    True
    >>> limiter.acquire("/x", block=False)   # third call exceeds 2 per 60s
    False
    """

    def __init__(
        self, limits: dict[str, Limit] | None = None, *, default: Limit = DEFAULT_LIMIT
    ) -> None:
        self.limits = dict(limits or {})
        self.default = default
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def limit_for(self, path: str) -> Limit:
        """The limit governing a path. Query strings are ignored."""
        base = path.split("?", 1)[0]
        return self.limits.get(base, self.default)

    def acquire(self, path: str, *, block: bool = True, timeout: float = 30.0) -> bool:
        """Claim one slot for ``path``.

        Blocks until a slot frees up, returning True. With ``block=False``,
        returns False immediately instead of waiting. Returns False if waiting
        would exceed ``timeout`` - a caller stuck behind a minute-long window
        should hear about it rather than hang.
        """
        base = path.split("?", 1)[0]
        limit = self.limit_for(base)
        if limit.calls <= 0:
            # A zero (or negative) allowance means the endpoint is closed. Refuse
            # rather than wait for a slot that can never open.
            return False
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                window = self._calls.setdefault(base, deque())
                now = time.monotonic()
                cutoff = now - limit.per_seconds
                while window and window[0] <= cutoff:
                    window.popleft()
                if len(window) < limit.calls:
                    window.append(now)
                    return True
                wait = window[0] + limit.per_seconds - now

            if not block or time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.25) + 0.001)

    def snapshot(self) -> dict[str, str]:
        """Current usage per path, for logging: ``{"/path": "3/20 per 60s"}``."""
        with self._lock:
            out = {}
            now = time.monotonic()
            for path, window in self._calls.items():
                limit = self.limit_for(path)
                live = sum(1 for t in window if t > now - limit.per_seconds)
                out[path] = f"{live}/{limit.calls} per {limit.per_seconds:g}s"
            return out


def coinswitch_limiter() -> RateLimiter:
    """A limiter preloaded with CoinSwitch's published limits."""
    return RateLimiter(COINSWITCH_LIMITS, default=DEFAULT_LIMIT)


def coindcx_limiter() -> RateLimiter:
    """A conservative limiter for CoinDCX, which publishes no figures."""
    return RateLimiter({}, default=COINDCX_DEFAULT_LIMIT)
