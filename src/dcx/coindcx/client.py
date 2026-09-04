"""Authenticated CoinDCX client.

CoinDCX signs the serialized JSON body with HMAC-SHA256, which means almost
every authenticated call is a ``POST`` - even the ones that only read - because
there has to be a body to sign. Each body must carry a millisecond
``timestamp``; requests far from server time are rejected.

Unlike CoinSwitch, CoinDCX supports **IP binding and read-only keys**. Use a
read-only key for anything that does not place orders; there is no reason to
hand a full-trade key to a balance query.
"""

from __future__ import annotations

from typing import Any

from ..core.config import Credentials, coindcx_credentials
from ..core.guards import TradingGuard
from ..core.orders import (
    InstrumentSpec,
    OrderRequest,
    ValidatedOrder,
    find_instrument,
    validate_order,
)
from ..core.ratelimit import coindcx_limiter
from ..core.signing import coindcx_headers, epoch_ms, sign_coindcx
from ..core.transport import Transport
from .public import API_BASE, CoinDCXPublic


class CoinDCXClient:
    """Signed access to CoinDCX private endpoints.

    >>> client = CoinDCXClient()                        # doctest: +SKIP
    >>> client.balances()[:1]                           # doctest: +SKIP
    [{'currency': 'INR', 'balance': '0.0', 'locked_balance': '0.0'}]
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        guard: TradingGuard | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.credentials = (credentials or coindcx_credentials()).require()
        self.guard = guard or TradingGuard()
        self.limiter = coindcx_limiter()
        self._http = Transport(API_BASE, timeout=timeout, limiter=self.limiter)
        self.public = CoinDCXPublic(timeout=timeout)
        self._specs: dict[str, InstrumentSpec] = {}

    def signed(
        self, path: str, payload: dict[str, Any] | None = None, *, method: str = "POST"
    ) -> Any:
        """Sign and send a request to a private endpoint.

        A ``timestamp`` is injected when absent. The body that gets signed is
        the body that gets sent - :func:`~dcx.core.signing.sign_coindcx` returns
        both so they cannot drift apart.

        Most CoinDCX private endpoints are ``POST``, because the signature
        covers a JSON body and there has to be one to sign. A few are ``GET``
        and *still* carry a signed body - ``futures/wallets`` and
        ``futures/wallets/transactions`` are the ones confirmed against the live
        API. Sending those as POST returns ``404 not_found``, which reads like a
        wrong path rather than a wrong verb, so the method is explicit here.
        """
        body_payload = dict(payload or {})
        body_payload.setdefault("timestamp", epoch_ms())
        body, signature = sign_coindcx(body_payload, self.credentials.api_secret)
        headers = coindcx_headers(self.credentials.api_key, signature)
        return self._http.request(method, path, headers=headers, body=body)

    # -- account -----------------------------------------------------------

    def balances(self) -> list[dict[str, Any]]:
        """Spot balances per currency: ``currency``, ``balance``, ``locked_balance``."""
        return self.signed("/exchange/v1/users/balances")

    def user_info(self) -> dict[str, Any]:
        """Account profile: id, email, verification state."""
        return self.signed("/exchange/v1/users/info")

    # -- spot orders (read-only) ------------------------------------------

    def active_orders(self, market: str, side: str | None = None) -> Any:
        """Open spot orders for a market symbol such as ``BTCINR``."""
        payload: dict[str, Any] = {"market": market}
        if side:
            payload["side"] = side
        return self.signed("/exchange/v1/orders/active_orders", payload)

    def order_status(self, order_id: str) -> Any:
        """Status of one spot order by its exchange id."""
        return self.signed("/exchange/v1/orders/status", {"id": order_id})

    def trade_history(self, limit: int = 50) -> Any:
        """Your own filled trades, most recent first."""
        return self.signed("/exchange/v1/orders/trade_history", {"limit": limit})

    # -- futures (read-only) ----------------------------------------------

    def futures_wallets(self) -> Any:
        """INR and USDT futures wallet balances.

        A signed **GET**, unlike almost everything else here. Returns per-wallet
        ``balance``, ``locked_balance``, ``cross_order_margin`` and
        ``cross_user_margin``.
        """
        return self.signed("/exchange/v1/derivatives/futures/wallets", method="GET")

    def futures_wallet_transactions(self, page: int = 1, size: int = 100) -> Any:
        """Futures wallet ledger. Also a signed GET."""
        return self.signed(
            f"/exchange/v1/derivatives/futures/wallets/transactions?page={page}&size={size}",
            method="GET",
        )

    def futures_orders(self, status: str = "open", page: int = 1, size: int = 10) -> Any:
        """List futures orders by status (``open``, ``filled``, ``cancelled``)."""
        return self.signed(
            "/exchange/v1/derivatives/futures/orders",
            {
                "status": status,
                "page": str(page),
                "size": str(size),
                "margin_currency_short_name": ["USDT"],
            },
        )

    def futures_trades(self, page: int = 1, size: int = 10) -> Any:
        """Your filled futures trades."""
        return self.signed(
            "/exchange/v1/derivatives/futures/trades",
            {"page": str(page), "size": str(size), "margin_currency_short_name": ["USDT"]},
        )

    def futures_positions(self, page: int = 1, size: int = 10) -> Any:
        """Open futures positions."""
        return self.signed(
            "/exchange/v1/derivatives/futures/positions",
            {"page": str(page), "size": str(size), "margin_currency_short_name": ["USDT"]},
        )

    # -- orders ------------------------------------------------------------

    def instrument(self, symbol: str) -> InstrumentSpec:
        """Trading constraints for a market, cached per client.

        Fetched from live ``markets_details``, so lot steps and minimums are
        whatever the exchange says today rather than whatever was true when
        this code was written.
        """
        key = symbol.upper()
        if key not in self._specs:
            self._specs[key] = find_instrument(self.public.markets_details(), symbol)
        return self._specs[key]

    def build_order(self, request: OrderRequest) -> ValidatedOrder:
        """Validate and round an order against live instrument metadata.

        Sends nothing. Useful on its own to check an order is well formed
        before deciding whether to place it.
        """
        return validate_order(request, self.instrument(request.symbol))

    def place_order(self, request: OrderRequest) -> dict[str, Any]:
        """Validate, guard, and place a spot order.

        **Dry run by default.** Unless the guard has live trading enabled, this
        validates and builds the exact request body, returns it under
        ``would_send``, and sends nothing. The returned shape is the same in
        both modes apart from the ``dry_run`` flag, so calling code does not
        branch on it.

        Raises :class:`~dcx.core.orders.OrderValidationError` for a malformed
        order and :class:`~dcx.core.errors.GuardError` if it breaches
        ``max_notional`` - both before anything reaches the network.
        """
        order = self.build_order(request)
        body: dict[str, Any] = {
            "side": order.side,
            "order_type": order.order_type,
            "market": order.symbol,
            "total_quantity": float(order.quantity),
            "client_order_id": order.client_order_id,
        }
        if order.price is not None:
            body["price_per_unit"] = float(order.price)

        notional = float(order.notional) if order.notional is not None else None
        should_send = self.guard.check_order(notional=notional, description=order.describe())

        result: dict[str, Any] = {
            "dry_run": not should_send,
            "order": order,
            "would_send": body,
            "adjustments": list(order.adjustments),
        }
        if should_send:
            result["response"] = self.signed("/exchange/v1/orders/create", body)
        return result

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel one spot order by its exchange id.

        Not gated by the dry-run guard: cancelling reduces exposure, and a
        safety mechanism that blocks you from *closing* a position is a hazard,
        not a protection.
        """
        return {"response": self.signed("/exchange/v1/orders/cancel", {"id": order_id})}

    def close(self) -> None:
        self._http.close()
        self.public.close()
