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
        self._http = Transport(API_BASE, timeout=timeout)
        self.public = CoinDCXPublic(timeout=timeout)

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
            {"status": status, "page": str(page), "size": str(size),
             "margin_currency_short_name": ["USDT"]},
        )

    def futures_trades(self, page: int = 1, size: int = 10) -> Any:
        """Your filled futures trades."""
        return self.signed(
            "/exchange/v1/derivatives/futures/trades",
            {"page": str(page), "size": str(size),
             "margin_currency_short_name": ["USDT"]},
        )

    def futures_positions(self, page: int = 1, size: int = 10) -> Any:
        """Open futures positions."""
        return self.signed(
            "/exchange/v1/derivatives/futures/positions",
            {"page": str(page), "size": str(size), "margin_currency_short_name": ["USDT"]},
        )

    def close(self) -> None:
        self._http.close()
        self.public.close()
