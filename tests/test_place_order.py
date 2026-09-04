"""Order placement tests.

Offline: the instrument spec is injected into the client's cache, so no network
call is made and no credential is used beyond a throwaway pair. What is being
tested is the decision path - validate, guard, build body, send or don't - not
the HTTP call itself.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dcx.coindcx.client import CoinDCXClient
from dcx.core.errors import GuardError
from dcx.core.guards import TradingGuard
from dcx.core.orders import InstrumentSpec, OrderRequest, OrderValidationError

BTCINR = {
    "symbol": "BTCINR",
    "pair": "B-BTC_INR",
    "status": "active",
    "min_quantity": 0.00001,
    "max_quantity": 100,
    "step": 0.00001,
    "min_price": 1,
    "max_price": 100_000_000,
    "min_notional": 100,
    "base_currency_precision": 1,
    "target_currency_precision": 5,
    "order_types": ["limit_order", "market_order"],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("COINDCX_API_KEY", "k" * 32)
    monkeypatch.setenv("COINDCX_API_SECRET", "s" * 32)
    c = CoinDCXClient()
    # Inject the spec so nothing touches the network.
    c._specs["BTCINR"] = InstrumentSpec.from_coindcx(BTCINR)
    yield c
    c.close()


def _no_network(client):
    def boom(*a, **kw):
        raise AssertionError("a dry run must not send anything")

    client._http.request = boom


def test_dry_run_is_default_and_sends_nothing(client):
    _no_network(client)
    result = client.place_order(OrderRequest("buy", "BTCINR", "0.001", "8500000"))
    assert result["dry_run"] is True
    assert "response" not in result


def test_dry_run_still_builds_the_exact_body(client):
    """The value of a dry run is seeing precisely what would have been sent."""
    _no_network(client)
    body = client.place_order(OrderRequest("buy", "BTCINR", "0.001", "8500000"))["would_send"]
    assert body == {
        "side": "buy",
        "order_type": "limit_order",
        "market": "BTCINR",
        "total_quantity": 0.001,
        "client_order_id": body["client_order_id"],
        "price_per_unit": 8500000.0,
    }
    assert body["client_order_id"].startswith("dcx-")


def test_rounding_adjustments_are_reported_not_hidden(client):
    _no_network(client)
    result = client.place_order(OrderRequest("buy", "BTCINR", "0.00123456", "8500000.99"))
    assert result["adjustments"] == [
        "quantity 0.00123456 -> 0.00123 (step 0.00001)",
        "price 8500000.99 -> 8500001.0 (1 dp)",
    ]


def test_live_mode_sends_and_returns_the_response(client):
    sent = {}

    def capture(method, path, **kw):
        sent.update(method=method, path=path, body=kw.get("body"))
        return {"status": "success", "orders": [{"id": "abc"}]}

    client._http.request = capture
    client.guard = TradingGuard(allow_live=True, env_allows_live=True)
    result = client.place_order(OrderRequest("buy", "BTCINR", "0.001", "8500000"))

    assert result["dry_run"] is False
    assert result["response"]["status"] == "success"
    assert sent["method"] == "POST"
    assert sent["path"] == "/exchange/v1/orders/create"
    assert '"client_order_id"' in sent["body"]


def test_max_notional_blocks_before_the_network(client):
    _no_network(client)
    client.guard = TradingGuard(allow_live=True, env_allows_live=True, max_notional=1000.0)
    with pytest.raises(GuardError, match="exceeds max_notional"):
        client.place_order(OrderRequest("buy", "BTCINR", "1", "8500000"))


def test_invalid_order_never_reaches_the_guard_or_network(client):
    _no_network(client)
    with pytest.raises(OrderValidationError, match="notional"):
        client.place_order(OrderRequest("buy", "BTCINR", "0.00001", "1"))


def test_build_order_validates_without_placing(client):
    _no_network(client)
    order = client.build_order(OrderRequest("sell", "BTCINR", "0.5", "8500000"))
    assert order.quantity == Decimal("0.50000")
    assert order.side == "sell"


def test_cancel_is_not_blocked_by_dry_run(client):
    """Blocking a cancel would trap you in a position. That is a hazard."""
    calls = []
    client._http.request = lambda method, path, **kw: calls.append(path) or {"ok": True}
    assert client.guard.is_live is False
    client.cancel_order("order-123")
    assert calls == ["/exchange/v1/orders/cancel"]
