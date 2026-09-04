"""CoinSwitch public endpoints.

There is exactly one. Verified live: every other CoinSwitch endpoint - including
plain market data like tickers, depth and candles, on all three surfaces -
returns 401 without a signature::

    GET https://coinswitch.co/trade/api/v2/time                 -> 200 {"serverTime": ...}
    GET https://coinswitch.co/trade/api/v2/depth?...            -> 401 {"message": "Invalid access"}
    GET https://dma.coinswitch.co/v5/market/tickers?...         -> 401 {"message": "Unauthorized: ..."}

That single unauthenticated endpoint is still worth having: it is what lets the
clock offset be measured before you hold any credentials, and it is a clean
liveness check for the API surface.
"""

from __future__ import annotations

from ..core.transport import Transport

SPOT_BASE = "https://coinswitch.co"


class CoinSwitchPublic:
    """The unauthenticated slice of the CoinSwitch API."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._http = Transport(SPOT_BASE, timeout=timeout)

    def server_time_ms(self) -> int:
        """CoinSwitch server time in milliseconds.

        Feed this to :class:`dcx.core.clock.ServerClock` - the epoch is part of
        every CoinSwitch signature and drift past 60s is rejected outright.
        """
        return int(self._http.request("GET", "/trade/api/v2/time")["serverTime"])

    def close(self) -> None:
        self._http.close()
