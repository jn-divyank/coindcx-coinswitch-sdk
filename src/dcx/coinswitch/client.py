"""Authenticated CoinSwitch client.

CoinSwitch is not one API. It is four surfaces with different hosts and path
prefixes, sharing a single Ed25519 signing scheme and - importantly - a single
API key pair:

===========  ====================================================  ==========
Surface      Base URL + prefix                                     Status
===========  ====================================================  ==========
Spot         ``https://coinswitch.co/trade/api/v2``                open
Futures      ``https://coinswitch.co/trade/api/v2/futures``        open
HFT          ``https://dma.coinswitch.co`` (``/v5``, ``/dma/api/v1``)  open
Options      HFT surface, ``category=option``                      private beta
===========  ====================================================  ==========

Options is not a separate module because it is not a separate API: it is the
``category`` parameter on the HFT surface, allowlisted per key. Request access
by emailing api@coinswitch.co.

**Credential warning.** CoinSwitch permits one active key pair at a time, with
no read-only scope and no IP allowlist. Any key you configure here is a
full-trade production key, and generating a replacement invalidates whatever
else is using the old one.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode

from ..core.clock import ServerClock
from ..core.config import Credentials, coinswitch_credentials
from ..core.guards import TradingGuard
from ..core.signing import coinswitch_headers, sign_coinswitch
from ..core.transport import Transport
from .public import CoinSwitchPublic

SPOT_BASE = "https://coinswitch.co"
HFT_BASE = "https://dma.coinswitch.co"


def build_path(path: str, params: Mapping[str, Any] | None = None) -> str:
    """Join a path and query string into the form that gets signed.

    The query is built once, here, and the *same* string is both signed and
    sent. Letting the HTTP layer re-encode params separately is how signatures
    end up not matching - CoinSwitch signs the URL-decoded path, so a comma
    encoded as ``%2C`` on the wire but signed literally will fail.

    >>> build_path("/trade/api/v2/orders", {"open": "true", "exchanges": "coinswitchx"})
    '/trade/api/v2/orders?open=true&exchanges=coinswitchx'
    >>> build_path("/trade/api/v2/time")
    '/trade/api/v2/time'
    """
    if not params:
        return path
    return f"{path}?{urlencode(params, safe=',')}"


class CoinSwitchClient:
    """Signed access to the CoinSwitch surfaces.

    >>> client = CoinSwitchClient()                     # doctest: +SKIP
    >>> client.validate_keys()                          # doctest: +SKIP
    {'message': 'Valid Access'}
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        guard: TradingGuard | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.credentials = (credentials or coinswitch_credentials()).require()
        self.guard = guard or TradingGuard()
        self._spot = Transport(SPOT_BASE, timeout=timeout)
        self._hft = Transport(HFT_BASE, timeout=timeout)
        self._public = CoinSwitchPublic(timeout=timeout)
        self.clock = ServerClock(self._public.server_time_ms)

    # -- signing plumbing --------------------------------------------------

    def _send(
        self,
        transport: Transport,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: str | None = None,
    ) -> Any:
        """Sign and send. The signed path and the sent path are the same string."""
        full_path = build_path(path, params)
        signature, epoch = sign_coinswitch(
            method, full_path, self.credentials.api_secret, epoch=self.clock.now_ms()
        )
        headers = coinswitch_headers(self.credentials.api_key, signature, epoch)
        # params are already baked into full_path; passing them again would
        # re-encode and break the signature.
        return transport.request(method, full_path, headers=headers, body=body)

    def spot(self, method: str, path: str, **kw: Any) -> Any:
        """Raw signed call against the Spot/Futures surface (``coinswitch.co``)."""
        return self._send(self._spot, method, path, **kw)

    def hft(self, method: str, path: str, **kw: Any) -> Any:
        """Raw signed call against the HFT surface (``dma.coinswitch.co``)."""
        return self._send(self._hft, method, path, **kw)

    # -- read-only endpoints ----------------------------------------------

    def validate_keys(self) -> dict[str, Any]:
        """Check the configured key and signature. Returns ``{"message": "Valid Access"}``.

        The cheapest possible end-to-end proof that signing works: authenticated,
        no parameters, no side effects. Start here when debugging auth.
        """
        return self.spot("GET", "/trade/api/v2/validate/keys")

    def portfolio(self) -> dict[str, Any]:
        """Spot holdings per currency.

        Beware the mixed typing the docs call out: crypto rows quote balances as
        strings, the INR row quotes them as numbers. Coerce before arithmetic.
        """
        return self.spot("GET", "/trade/api/v2/user/portfolio")

    def orders(self, *, open_only: bool = True, exchange: str = "coinswitchx") -> dict[str, Any]:
        """List spot orders for one exchange venue."""
        return self.spot(
            "GET",
            "/trade/api/v2/orders",
            params={"open": "true" if open_only else "false", "exchanges": exchange},
        )

    def futures_wallet_balance(self) -> dict[str, Any]:
        """USDT-margined futures wallet balance."""
        return self.spot("GET", "/trade/api/v2/futures/wallet_balance")

    def hft_positions(self, category: str = "linear") -> dict[str, Any]:
        """Open positions on the HFT surface.

        ``category`` selects the product: ``linear`` for perpetual futures,
        ``option`` for options. Options requires the private-beta allowlist on
        your key; without it this returns an error rather than an empty list.
        """
        return self.hft("GET", "/v5/position/list", params={"category": category})

    def close(self) -> None:
        self._spot.close()
        self._hft.close()
        self._public.close()
