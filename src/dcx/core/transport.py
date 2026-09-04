"""HTTP transport: one place for timeouts, retries, and error mapping.

Retry policy is deliberately asymmetric, because the two failure classes have
very different consequences for a trading client:

* **Idempotent reads** (GET) retry on network errors, 429 and 5xx.
* **State-changing writes** (POST/DELETE/PUT) retry **only** on errors that
  prove the request never reached the exchange - a connection or DNS failure.
  A 5xx means "operation status unknown" in CoinSwitch's own words, and a
  timeout after the bytes went out means the order may well be live. Retrying
  either can place the same order twice, so we surface the error and let the
  caller reconcile by client order id.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Mapping

import requests

from .errors import ApiError, TransportError, error_for_status

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
USER_AGENT = "dcx-sdk/0.1 (+https://github.com/jn-divyank/coindcx-coinswitch-sdk)"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _extract_message(payload: Any, status: int) -> str:
    """Pull a human message out of either exchange's error envelope."""
    if isinstance(payload, dict):
        for key in ("message", "retMsg", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:400]
    return f"HTTP {status}"


class Transport:
    """A thin, retrying JSON HTTP client.

    One instance per exchange surface, since each has its own base URL. The
    session is reused, so TCP and TLS setup is paid once rather than per call -
    which matters when an agent makes a burst of calls in a loop.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a request and return decoded JSON, or raise a mapped error.

        ``body`` is a pre-serialized string, never a dict. That is not a style
        choice: CoinDCX signs the exact body bytes, so the caller must hand us
        the string it signed rather than let us re-encode a dict differently.
        """
        method = method.upper()
        url = f"{self.base_url}{path}"
        retryable = method in _IDEMPOTENT_METHODS
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= self.max_retries:
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=dict(headers or {}),
                    params=dict(params) if params else None,
                    data=body,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                # No response came back at all. For a write this is the one
                # case we can retry safely: the request never landed.
                last_exc = TransportError(f"{method} {url} failed: {exc}")
                if attempt >= self.max_retries:
                    raise last_exc from exc
                self._sleep(attempt)
                attempt += 1
                continue

            if response.status_code in _RETRY_STATUSES and retryable and attempt < self.max_retries:
                self._sleep(attempt, response)
                attempt += 1
                continue

            return self._decode(response, url)

        raise last_exc or TransportError(f"{method} {url} exhausted retries")

    def _decode(self, response: requests.Response, url: str) -> Any:
        text = response.text
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = text

        if response.status_code >= 400:
            retry_after = response.headers.get("Retry-After")
            raise error_for_status(
                response.status_code,
                _extract_message(payload, response.status_code),
                body=payload,
                request_id=(payload or {}).get("request_id") if isinstance(payload, dict) else None,
                url=url,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )

        # CoinSwitch's HFT surface wraps everything in a Bybit-style envelope and
        # can report failure inside a 200. Unwrap it so callers see one shape.
        if isinstance(payload, dict) and "retCode" in payload:
            if payload.get("retCode") not in (0, None):
                raise ApiError(
                    payload.get("retMsg") or "HFT request failed",
                    status=response.status_code,
                    body=payload,
                    url=url,
                )
        return payload

    def _sleep(self, attempt: int, response: requests.Response | None = None) -> None:
        """Exponential backoff with jitter, honouring Retry-After when present."""
        if response is not None:
            hinted = response.headers.get("Retry-After")
            if hinted and hinted.isdigit():
                time.sleep(min(float(hinted), 30.0))
                return
        time.sleep(min(2.0**attempt, 8.0) * (0.5 + random.random() / 2))

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
