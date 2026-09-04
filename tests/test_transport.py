"""Transport tests. Offline - a fake session stands in for the network.

The retry asymmetry is the thing worth testing: a GET may be retried freely,
but a POST that might have placed an order must not be.
"""

from __future__ import annotations

import json

import pytest
import requests

from dcx.core.errors import (
    ApiError,
    AuthError,
    RateLimitError,
    ServerError,
    TransportError,
    ValidationError,
)
from dcx.core.transport import Transport


class FakeResponse:
    def __init__(self, status: int, payload, headers=None):
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.headers = headers or {}


class FakeSession:
    """Replays a scripted list of responses (or exceptions) and counts calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if not self.script:
            raise AssertionError(
                f"FakeSession script exhausted after {len(self.calls)} calls - "
                "the test made more requests than it scripted"
            )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def make(script):
    return Transport("https://example.test", session=FakeSession(script), max_retries=2)


def test_successful_json_is_returned():
    t = make([FakeResponse(200, {"ok": True})])
    assert t.request("GET", "/x") == {"ok": True}


@pytest.mark.parametrize(
    "status,exc",
    [(401, AuthError), (422, ValidationError), (429, RateLimitError), (503, ServerError)],
)
def test_status_codes_map_to_typed_errors(status, exc):
    t = make([FakeResponse(status, {"message": "nope"})] * 4)
    with pytest.raises(exc):
        t.request("POST", "/x")


def test_error_message_is_extracted_from_either_envelope():
    """Spot/futures use {"message": ...}; HFT uses {"retMsg": ...}."""
    t = make([FakeResponse(401, {"message": "Invalid access"})])
    with pytest.raises(AuthError, match="Invalid access"):
        t.request("GET", "/x")

    t = make([FakeResponse(401, {"retMsg": "Unauthorized: Invalid signature"})])
    with pytest.raises(AuthError, match="Invalid signature"):
        t.request("GET", "/x")


def test_request_id_is_preserved_for_support_tickets():
    t = make([FakeResponse(401, {"message": "no", "request_id": "abc-123"})])
    with pytest.raises(AuthError) as caught:
        t.request("GET", "/x")
    assert caught.value.request_id == "abc-123"
    assert "abc-123" in str(caught.value)


def test_hft_envelope_failure_inside_a_200_still_raises():
    """The HFT surface can report failure with an HTTP 200 body."""
    t = make([FakeResponse(200, {"retCode": 10001, "retMsg": "param error", "result": {}})])
    with pytest.raises(ApiError, match="param error"):
        t.request("GET", "/v5/order/realtime")


def test_hft_envelope_success_passes_through():
    payload = {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
    t = make([FakeResponse(200, payload)])
    assert t.request("GET", "/v5/position/list") == payload


def test_get_retries_on_server_error_then_succeeds():
    session = FakeSession([FakeResponse(503, {"message": "down"}), FakeResponse(200, {"ok": 1})])
    t = Transport("https://example.test", session=session, max_retries=2)
    assert t.request("GET", "/x") == {"ok": 1}
    assert len(session.calls) == 2


def test_post_is_never_retried_on_server_error():
    """A 5xx on a write means "status unknown". Retrying could double-place an
    order, so the error must surface for the caller to reconcile."""
    session = FakeSession([FakeResponse(503, {"message": "down"}), FakeResponse(200, {"ok": 1})])
    t = Transport("https://example.test", session=session, max_retries=2)
    with pytest.raises(ServerError):
        t.request("POST", "/exchange/v1/orders/create")
    assert len(session.calls) == 1


def test_post_is_retried_when_the_request_never_left():
    """A connection error proves nothing was sent, so a retry is safe."""
    session = FakeSession([
        requests.ConnectionError("dns"),
        FakeResponse(200, {"ok": 1}),
    ])
    t = Transport("https://example.test", session=session, max_retries=2)
    assert t.request("POST", "/x") == {"ok": 1}
    assert len(session.calls) == 2


def test_exhausted_network_retries_raise_transport_error():
    session = FakeSession([requests.ConnectionError("dns")] * 5)
    t = Transport("https://example.test", session=session, max_retries=1)
    with pytest.raises(TransportError):
        t.request("GET", "/x")


def test_non_json_error_body_does_not_crash():
    # 502 is retryable for a GET, so script enough of them to exhaust retries.
    t = make([FakeResponse(502, "<html>gateway</html>")] * 3)
    with pytest.raises(ServerError):
        t.request("GET", "/x")
