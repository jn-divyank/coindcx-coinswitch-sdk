"""Order validation tests.

These matter more than most: this is the layer that decides whether a number
reaches an exchange. The live test at the bottom runs the same validator
against real CoinDCX instrument metadata, so it fails if the exchange changes a
lot step or a minimum underneath us.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dcx.core.orders import (
    InstrumentSpec,
    OrderRequest,
    OrderValidationError,
    find_instrument,
    validate_order,
)

# Shaped exactly like a real markets_details entry.
BNB = {
    "symbol": "BNBUSDT",
    "pair": "B-BNB_USDT",
    "status": "active",
    "min_quantity": 0.001,
    "max_quantity": 900000,
    "step": 0.001,
    "min_price": 0.01,
    "max_price": 100000,
    "min_notional": 5,
    "base_currency_precision": 2,
    "target_currency_precision": 3,
    "order_types": ["limit_order", "market_order"],
}


@pytest.fixture
def spec():
    return InstrumentSpec.from_coindcx(BNB)


def test_valid_order_passes_through(spec):
    order = validate_order(OrderRequest("buy", "BNBUSDT", "1.5", "600.00"), spec)
    assert order.quantity == Decimal("1.5")
    assert order.price == Decimal("600.00")
    assert order.notional == Decimal("900.00")
    assert order.adjustments == ()


def test_quantity_rounds_down_to_the_step(spec):
    """Down, never up - rounding up can exceed the balance you sized against."""
    order = validate_order(OrderRequest("buy", "BNBUSDT", "1.23456", "600"), spec)
    assert order.quantity == Decimal("1.234")
    assert "step" in order.adjustments[0]


def test_price_rounds_to_instrument_precision(spec):
    order = validate_order(OrderRequest("buy", "BNBUSDT", "1", "600.98765"), spec)
    assert order.price == Decimal("600.99")


def test_decimal_arithmetic_avoids_float_drift(spec):
    """0.1 + 0.2 != 0.3 in floats, and that difference is a rejected order."""
    order = validate_order(OrderRequest("buy", "BNBUSDT", 0.1 + 0.2, "600"), spec)
    assert order.quantity == Decimal("0.300")


def test_notional_below_minimum_is_rejected(spec):
    """The failure that would otherwise happen *after* signing and sending."""
    with pytest.raises(OrderValidationError, match="notional"):
        validate_order(OrderRequest("buy", "BNBUSDT", "0.001", "600"), spec)


def test_quantity_below_minimum_is_rejected():
    spec = InstrumentSpec.from_coindcx({**BNB, "min_quantity": 10, "min_notional": 0})
    with pytest.raises(OrderValidationError, match="below the minimum"):
        validate_order(OrderRequest("buy", "BNBUSDT", "1", "600"), spec)


def test_quantity_above_maximum_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="exceeds the maximum"):
        validate_order(OrderRequest("buy", "BNBUSDT", "10000000", "600"), spec)


def test_quantity_that_rounds_to_zero_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="rounds to zero"):
        validate_order(OrderRequest("buy", "BNBUSDT", "0.0001", "600"), spec)


@pytest.mark.parametrize("quantity", ["0", "-1"])
def test_non_positive_quantity_is_rejected(spec, quantity):
    with pytest.raises(OrderValidationError, match="must be positive"):
        validate_order(OrderRequest("buy", "BNBUSDT", quantity, "600"), spec)


def test_price_outside_bounds_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="exceeds the maximum"):
        validate_order(OrderRequest("buy", "BNBUSDT", "1", "999999999"), spec)


def test_unsupported_order_type_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="does not support"):
        validate_order(OrderRequest("buy", "BNBUSDT", "1", "600", order_type="stop_limit"), spec)


def test_delisted_instrument_is_rejected():
    spec = InstrumentSpec.from_coindcx({**BNB, "status": "inactive"})
    with pytest.raises(OrderValidationError, match="not tradable"):
        validate_order(OrderRequest("buy", "BNBUSDT", "1", "600"), spec)


def test_limit_order_without_a_price_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="requires a price"):
        validate_order(OrderRequest("buy", "BNBUSDT", "1"), spec)


def test_market_order_needs_no_price(spec):
    order = validate_order(OrderRequest("buy", "BNBUSDT", "1", order_type="market_order"), spec)
    assert order.price is None and order.notional is None


def test_bad_side_is_rejected_at_construction():
    with pytest.raises(OrderValidationError, match="side must be"):
        OrderRequest("hodl", "BNBUSDT", "1", "600")  # type: ignore[arg-type]


def test_non_numeric_quantity_is_rejected(spec):
    with pytest.raises(OrderValidationError, match="not a number"):
        validate_order(OrderRequest("buy", "BNBUSDT", "lots", "600"), spec)


def test_client_order_id_is_generated_and_carried(spec):
    request = OrderRequest("buy", "BNBUSDT", "1", "600")
    assert request.client_order_id.startswith("dcx-")
    assert validate_order(request, spec).client_order_id == request.client_order_id


def test_describe_is_readable(spec):
    order = validate_order(OrderRequest("sell", "BNBUSDT", "2", "600"), spec)
    assert order.describe() == "sell 2.000 BNBUSDT @ 600.00 (notional 1200.00)"


def test_find_instrument_matches_symbol_or_pair():
    assert find_instrument([BNB], "BNBUSDT").pair == "B-BNB_USDT"
    assert find_instrument([BNB], "b-bnb_usdt").symbol == "BNBUSDT"
    with pytest.raises(OrderValidationError, match="no instrument"):
        find_instrument([BNB], "NOPE")


@pytest.mark.live
def test_validator_agrees_with_live_instrument_metadata():
    """Run the validator against what CoinDCX actually publishes today."""
    from dcx.coindcx.public import CoinDCXPublic

    client = CoinDCXPublic()
    details = client.markets_details()
    spec = find_instrument(details, "BTCINR")

    assert spec.is_tradable
    assert spec.step > 0
    assert spec.min_notional > 0, "min_notional vanished from markets_details"

    # An order at exactly the minimum notional must pass; just under must fail.
    price = spec.min_price if spec.min_price > 1 else Decimal("1000000")
    quantity = spec.round_quantity((spec.min_notional / price) + spec.step)
    validate_order(OrderRequest("buy", "BTCINR", quantity, price), spec)

    with pytest.raises(OrderValidationError):
        validate_order(OrderRequest("buy", "BTCINR", spec.step, price / 1000), spec)
    client.close()
