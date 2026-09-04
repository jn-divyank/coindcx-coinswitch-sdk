"""Server-clock tracking.

CoinSwitch puts the epoch inside the signature and rejects requests whose
``X-AUTH-EPOCH`` drifts too far from its own clock (docs: ~5s tolerance, hard
reject past 60s). On a long-running host - a VM that has been up for weeks, a
container that suspended - local time slips, and the failure presents as
intermittent 401s that look exactly like a bad key.

So we measure the offset once against a public endpoint and apply it to every
signature. ``https://coinswitch.co/trade/api/v2/time`` needs no auth, which is
what makes this possible before you have credentials.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Re-measure at most this often; the offset does not move fast.
DEFAULT_REFRESH_SECONDS = 300.0

# Warn above this. CoinSwitch hard-rejects past 60_000ms.
SKEW_WARN_MS = 2_000


class ServerClock:
    """Tracks the offset between local time and an exchange's server time.

    ``fetch_server_time_ms`` is any callable returning the exchange's current
    time in milliseconds. Thread-safe: a background trading loop and a
    foreground call can share one instance.

    >>> clock = ServerClock(lambda: 1_700_000_000_000)
    >>> isinstance(clock.now_ms(), int)
    True
    """

    def __init__(
        self,
        fetch_server_time_ms: Callable[[], int],
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._fetch = fetch_server_time_ms
        self._refresh_seconds = refresh_seconds
        self._offset_ms = 0
        self._measured_at = 0.0
        self._lock = threading.Lock()

    @property
    def offset_ms(self) -> int:
        """Milliseconds to add to local time to match the server. May be negative."""
        return self._offset_ms

    def sync(self) -> int:
        """Force a measurement now. Returns the new offset in milliseconds.

        The round trip is halved and subtracted, so the offset reflects clock
        difference rather than network latency.
        """
        started = time.time()
        server_ms = int(self._fetch())
        elapsed_ms = (time.time() - started) * 1000
        local_mid_ms = started * 1000 + elapsed_ms / 2
        with self._lock:
            self._offset_ms = int(server_ms - local_mid_ms)
            self._measured_at = time.time()
        return self._offset_ms

    def now_ms(self) -> int:
        """Current server time in milliseconds, syncing first if the offset is stale."""
        if time.time() - self._measured_at > self._refresh_seconds:
            try:
                self.sync()
            except Exception:
                # A failed sync must never block a request: fall back to the
                # last known offset (or zero). A real skew problem will surface
                # as a 401 with a clear message, which is more actionable than
                # a transport error raised from inside the signing path.
                pass
        return int(time.time() * 1000) + self._offset_ms

    def skew_warning(self) -> str | None:
        """A human-readable warning if the measured offset looks dangerous."""
        if self._measured_at == 0.0:
            return None
        drift = abs(self._offset_ms)
        if drift >= 60_000:
            return (
                f"Local clock is {drift}ms off server time - CoinSwitch will reject "
                "every signed request. Fix the host clock (enable NTP)."
            )
        if drift >= SKEW_WARN_MS:
            return (
                f"Local clock is {drift}ms off server time. Requests are being "
                "corrected, but the host clock should be fixed."
            )
        return None
