"""Exception hierarchy shared by both exchange clients.

Every error carries the HTTP status, the raw response body, and - where the
exchange supplies one - a ``request_id`` worth quoting in a support ticket.

Mapping is driven by observed live behaviour, not guesses. The three auth
failure shapes seen in the wild::

    CoinSwitch spot v2    401 {"message": "Invalid access"}
    CoinSwitch futures v2 401 {"message": "API Key or Signature is not Correct ..."}
    CoinSwitch HFT (dma)  401 {"message": "Unauthorized: Signature validation failed",
                               "data": null, "request_id": "..."}
"""

from __future__ import annotations

from typing import Any


class DcxError(Exception):
    """Base class for everything this library raises."""


class ConfigError(DcxError):
    """Missing or malformed credentials/configuration."""


class GuardError(DcxError):
    """A safety guard refused the call before it reached the network.

    Raised by :class:`dcx.core.guards.TradingGuard` - for example when an order
    exceeds ``max_notional``, or when live trading has not been explicitly
    enabled. This never means the exchange rejected anything; nothing was sent.
    """


class TransportError(DcxError):
    """The request never produced an HTTP response (DNS, TLS, timeout, reset)."""


class ApiError(DcxError):
    """The exchange returned a non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
        request_id: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.request_id = request_id
        self.url = url

    def __str__(self) -> str:
        bits = [self.message]
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.url:
            bits.append(f"url={self.url}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " ".join(bits)


class AuthError(ApiError):
    """401 - bad signature, wrong key, or clock skew beyond the allowed window.

    CoinSwitch rejects requests whose ``X-AUTH-EPOCH`` drifts more than 60s from
    server time, so a sudden burst of these on a previously working key usually
    means the host clock has slipped, not that the key was revoked.
    """


class ValidationError(ApiError):
    """422 - the request was understood but a field was missing or invalid."""


class RateLimitError(ApiError):
    """429 - too many requests. ``retry_after`` is seconds, when the exchange says."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ServerError(ApiError):
    """5xx - and therefore the operation's outcome is genuinely unknown.

    CoinSwitch documents 5xx as "operation status unknown". For anything that
    mutates state, do not blindly retry: query the order by its client order id
    first, or you risk placing it twice.
    """


def error_for_status(
    status: int,
    message: str,
    *,
    body: Any = None,
    request_id: str | None = None,
    url: str | None = None,
    retry_after: float | None = None,
) -> ApiError:
    """Pick the right exception subclass for an HTTP status code."""
    kw = {"status": status, "body": body, "request_id": request_id, "url": url}
    if status == 401:
        return AuthError(message, **kw)
    if status == 422:
        return ValidationError(message, **kw)
    if status == 429:
        return RateLimitError(message, retry_after=retry_after, **kw)
    if status >= 500:
        return ServerError(message, **kw)
    return ApiError(message, **kw)
