"""Order construction and validation against live instrument metadata.

This is where order bugs actually live. The HTTP call that places an order is
three lines; getting the *numbers* right is the hard part, and it is the part
an exchange rejects - or worse, accepts in a form you did not intend.

Every check here runs against real instrument metadata pulled from the exchange
(``markets_details``), so it catches the whole family of quiet failures:

* a quantity that is not a multiple of the lot step
* a notional under the exchange minimum, which fails *after* you have signed
* a price with more decimal places than the instrument allows
* an order type the instrument does not support
* a delisted or suspended market

Arithmetic uses :class:`~decimal.Decimal`. Float rounding on a lot step is not
a theoretical concern: ``0.1 + 0.2 != 0.3`` becomes a rejected order, and
``round(2.675, 2) == 2.67`` becomes a price the exchange will not accept.

Quantities round **down** to the step, never up - rounding up can exceed the
balance you sized against, which turns a validation helper into a source of
failed orders.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from .errors import DcxError
from .guards import new_client_order_id

Side = Literal["buy", "sell"]


class OrderValidationError(DcxError):
    """An order is malformed for this instrument. Nothing was sent."""


def _dec(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderValidationError(f"{name} is not a number: {value!r}") from exc


@dataclass(frozen=True)
class InstrumentSpec:
    """The trading constraints for one market.

    Build with :meth:`from_coindcx`; the field names are normalized so a
    CoinSwitch equivalent can populate the same shape later.
    """

    symbol: str
    pair: str
    status: str
    min_quantity: Decimal
    max_quantity: Decimal
    step: Decimal
    min_price: Decimal
    max_price: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    order_types: tuple[str, ...] = ()

    @classmethod
    def from_coindcx(cls, entry: dict[str, Any]) -> InstrumentSpec:
        """Build from one ``markets_details`` entry.

        >>> spec = InstrumentSpec.from_coindcx({
        ...     "symbol": "BNBUSDT", "pair": "B-BNB_USDT", "status": "active",
        ...     "min_quantity": 0.001, "max_quantity": 900000, "step": 0.001,
        ...     "min_price": 0.01, "max_price": 100000, "min_notional": 5,
        ...     "base_currency_precision": 2, "target_currency_precision": 3,
        ...     "order_types": ["limit_order", "market_order"]})
        >>> spec.step
        Decimal('0.001')
        >>> spec.is_tradable
        True
        """
        return cls(
            symbol=entry.get("symbol", ""),
            pair=entry.get("pair", ""),
            status=entry.get("status", "unknown"),
            min_quantity=_dec(entry.get("min_quantity", 0), "min_quantity"),
            max_quantity=_dec(entry.get("max_quantity", 0), "max_quantity"),
            step=_dec(entry.get("step", 0), "step"),
            min_price=_dec(entry.get("min_price", 0), "min_price"),
            max_price=_dec(entry.get("max_price", 0), "max_price"),
            min_notional=_dec(entry.get("min_notional", 0), "min_notional"),
            # CoinDCX names these by currency role: base = quote side = price,
            # target = the asset being traded = quantity.
            price_precision=int(entry.get("base_currency_precision", 8)),
            quantity_precision=int(entry.get("target_currency_precision", 8)),
            order_types=tuple(entry.get("order_types", ()) or ()),
        )

    @property
    def is_tradable(self) -> bool:
        return self.status == "active"

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Round a quantity **down** to a whole multiple of the lot step.

        Down, always: rounding up can push an order past the balance it was
        sized against.

        >>> spec = InstrumentSpec("X", "X", "active", Decimal("0.001"),
        ...     Decimal("100"), Decimal("0.001"), Decimal("0"), Decimal("1e9"),
        ...     Decimal("0"), 2, 3)
        >>> spec.round_quantity(Decimal("1.23456"))
        Decimal('1.234')
        """
        if self.step <= 0:
            return quantity
        stepped = (quantity / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        # Quantize to the step's own exponent so the result always renders at
        # the instrument's precision - Decimal exponent arithmetic otherwise
        # yields "2" for one quantity and "0.300" for another, and can produce
        # scientific notation in a log an agent is meant to read.
        return stepped.quantize(self.step)

    def round_price(self, price: Decimal) -> Decimal:
        """Round a price to the instrument's allowed decimal places."""
        if self.price_precision < 0:
            return price
        quantum = Decimal(1).scaleb(-self.price_precision)
        return price.quantize(quantum, rounding=ROUND_HALF_UP)


@dataclass
class OrderRequest:
    """A proposed order, before validation.

    ``client_order_id`` is generated when omitted. Always send one: if a request
    times out you cannot tell whether it landed, and a client order id lets you
    query for it instead of guessing - and makes a retry that *does* arrive a
    duplicate rather than a second fill.
    """

    side: Side
    symbol: str
    quantity: Decimal | float | str
    price: Decimal | float | str | None = None
    order_type: str = "limit_order"
    client_order_id: str = field(default_factory=new_client_order_id)

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise OrderValidationError(f"side must be 'buy' or 'sell', got {self.side!r}")


@dataclass(frozen=True)
class ValidatedOrder:
    """An order checked and rounded to the instrument's constraints."""

    side: Side
    symbol: str
    pair: str
    quantity: Decimal
    price: Decimal | None
    order_type: str
    client_order_id: str
    notional: Decimal | None
    #: Non-fatal adjustments made during validation, e.g. quantity rounding.
    adjustments: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line suitable for a dry-run log or an approval prompt."""
        price = f"@ {self.price}" if self.price is not None else "@ market"
        notional = f" (notional {self.notional})" if self.notional is not None else ""
        return f"{self.side} {self.quantity} {self.symbol} {price}{notional}"


def validate_order(request: OrderRequest, spec: InstrumentSpec) -> ValidatedOrder:
    """Check and normalize an order against an instrument. Raises on anything fatal.

    Returns a :class:`ValidatedOrder` with quantity rounded to the lot step and
    price to the allowed precision. Adjustments are recorded rather than hidden,
    so a caller can log what changed.
    """
    adjustments: list[str] = []

    if not spec.is_tradable:
        raise OrderValidationError(
            f"{spec.symbol or request.symbol} is not tradable (status={spec.status!r})"
        )

    if spec.order_types and request.order_type not in spec.order_types:
        raise OrderValidationError(
            f"{spec.symbol} does not support {request.order_type!r}; "
            f"supported: {', '.join(spec.order_types)}"
        )

    quantity = _dec(request.quantity, "quantity")
    if quantity <= 0:
        raise OrderValidationError(f"quantity must be positive, got {quantity}")

    rounded_qty = spec.round_quantity(quantity)
    if rounded_qty != quantity:
        adjustments.append(f"quantity {quantity} -> {rounded_qty} (step {spec.step})")
    if rounded_qty <= 0:
        raise OrderValidationError(
            f"quantity {quantity} rounds to zero at step {spec.step} for {spec.symbol}"
        )
    if spec.min_quantity > 0 and rounded_qty < spec.min_quantity:
        raise OrderValidationError(
            f"quantity {rounded_qty} is below the minimum {spec.min_quantity} for {spec.symbol}"
        )
    if spec.max_quantity > 0 and rounded_qty > spec.max_quantity:
        raise OrderValidationError(
            f"quantity {rounded_qty} exceeds the maximum {spec.max_quantity} for {spec.symbol}"
        )

    price: Decimal | None = None
    notional: Decimal | None = None
    if request.price is not None:
        price = _dec(request.price, "price")
        if price <= 0:
            raise OrderValidationError(f"price must be positive, got {price}")
        rounded_price = spec.round_price(price)
        if rounded_price != price:
            adjustments.append(f"price {price} -> {rounded_price} ({spec.price_precision} dp)")
        price = rounded_price
        if spec.min_price > 0 and price < spec.min_price:
            raise OrderValidationError(
                f"price {price} is below the minimum {spec.min_price} for {spec.symbol}"
            )
        if spec.max_price > 0 and price > spec.max_price:
            raise OrderValidationError(
                f"price {price} exceeds the maximum {spec.max_price} for {spec.symbol}"
            )
        exact_notional = price * rounded_qty
        if spec.min_notional > 0 and exact_notional < spec.min_notional:
            raise OrderValidationError(
                f"notional {exact_notional} is below the minimum {spec.min_notional} "
                f"for {spec.symbol} - increase quantity or price"
            )
        # Compare at full precision above, then present at the precision of the
        # quote currency the notional is denominated in.
        notional = spec.round_price(exact_notional)
    elif request.order_type.startswith("limit"):
        raise OrderValidationError(f"{request.order_type} requires a price")

    return ValidatedOrder(
        side=request.side,
        symbol=spec.symbol or request.symbol,
        pair=spec.pair,
        quantity=rounded_qty,
        price=price,
        order_type=request.order_type,
        client_order_id=request.client_order_id,
        notional=notional,
        adjustments=tuple(adjustments),
    )


def find_instrument(details: Iterable[dict[str, Any]], symbol: str) -> InstrumentSpec:
    """Locate one instrument in a ``markets_details`` list by symbol or pair."""
    wanted = symbol.upper()
    for entry in details:
        if wanted in (
            str(entry.get("symbol", "")).upper(),
            str(entry.get("pair", "")).upper(),
            str(entry.get("coindcx_name", "")).upper(),
        ):
            return InstrumentSpec.from_coindcx(entry)
    raise OrderValidationError(f"no instrument matching {symbol!r}")
