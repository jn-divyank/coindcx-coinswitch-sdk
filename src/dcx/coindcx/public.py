"""CoinDCX public market data. No credentials required.

Every endpoint here was exercised against the live API while this module was
written; the response notes in each docstring describe shapes actually
returned, not shapes inferred from documentation.

CoinDCX splits public data across two hosts, which is easy to trip over:

* ``https://api.coindcx.com`` - tickers, market lists, instrument metadata
* ``https://public.coindcx.com`` - order book, trades, candles, futures prices

Pair identifiers also come in two flavours. ``markets_details`` returns both:
``symbol`` (``BTCUSDT``) for the /exchange endpoints, and ``pair``
(``B-BTC_USDT``, an exchange-code prefix plus underscore form) for the
/market_data endpoints. Passing the wrong one is the most common 404 here, so
:meth:`CoinDCXPublic.resolve_pair` converts between them.
"""

from __future__ import annotations

from typing import Any

from ..core.transport import Transport

API_BASE = "https://api.coindcx.com"
PUBLIC_BASE = "https://public.coindcx.com"


class CoinDCXPublic:
    """Read-only market data client for CoinDCX.

    >>> client = CoinDCXPublic()
    >>> book = client.orderbook("B-BTC_USDT")          # doctest: +SKIP
    >>> sorted(book)                                    # doctest: +SKIP
    ['asks', 'bids', 'timestamp']
    """

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._api = Transport(API_BASE, timeout=timeout)
        self._public = Transport(PUBLIC_BASE, timeout=timeout)
        self._markets_cache: list[dict[str, Any]] | None = None

    # -- reference data ----------------------------------------------------

    def markets(self) -> list[str]:
        """All tradable market symbols, e.g. ``["BTCINR", "ETHUSDT", ...]``."""
        return self._api.request("GET", "/exchange/v1/markets")

    def markets_details(self) -> list[dict[str, Any]]:
        """Full instrument metadata for every market.

        Each entry carries the fields you need before sizing an order:
        ``min_quantity``, ``max_quantity``, ``step``, ``min_notional``,
        ``base_currency_precision``, ``target_currency_precision``,
        ``order_types``, ``status``, plus both ``symbol`` and ``pair`` forms.
        """
        return self._api.request("GET", "/exchange/v1/markets_details")

    def ticker(self) -> list[dict[str, Any]]:
        """24h ticker for every market: ``bid``, ``ask``, ``high``, ``low``,
        ``volume``, ``last_price``, ``change_24_hour``, ``market``."""
        return self._api.request("GET", "/exchange/ticker")

    # -- order book and trades --------------------------------------------

    def orderbook(self, pair: str) -> dict[str, Any]:
        """Order book for a ``B-BTC_USDT``-style pair.

        Note the unusual shape: ``asks`` and ``bids`` are **objects keyed by
        price string**, not arrays of pairs - ``{"80580.3": "0.42447", ...}``.
        Sort the keys numerically to get depth in order; do not rely on the
        JSON object preserving a useful order.
        """
        return self._public.request("GET", "/market_data/orderbook", params={"pair": pair})

    def trade_history(self, pair: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent public trades, newest first.

        Fields are single-letter: ``p`` price, ``q`` quantity, ``s`` symbol,
        ``T`` timestamp in ms, ``m`` whether the buyer was the maker.
        """
        return self._public.request(
            "GET", "/market_data/trade_history", params={"pair": pair, "limit": limit}
        )

    def candles(self, pair: str, interval: str = "1m", limit: int = 100) -> list[dict[str, Any]]:
        """OHLCV candles, newest first.

        ``interval`` accepts the usual ``1m 5m 15m 30m 1h 2h 4h 6h 8h 1d 3d 1w 1M``.
        Each candle is ``{open, high, low, close, volume, time}`` with ``time``
        in milliseconds.
        """
        return self._public.request(
            "GET",
            "/market_data/candles",
            params={"pair": pair, "interval": interval, "limit": limit},
        )

    # -- futures -----------------------------------------------------------

    def futures_prices(self) -> dict[str, Any]:
        """Real-time futures prices for every contract, keyed by pair.

        Per-pair fields include ``ls`` last price, ``mp`` mark price, ``fr``
        funding rate, ``efr`` estimated funding rate, ``h``/``l`` 24h high/low,
        ``v`` volume, ``pc`` percent change.
        """
        return self._public.request("GET", "/market_data/v3/current_prices/futures/rt")

    def futures_instruments(self, margin_currency: str = "USDT") -> dict[str, Any]:
        """Futures instrument metadata: tick size, lot size, leverage caps.

        Returns ``price_increment``, ``quantity_increment``, ``min_trade_size``,
        ``min_price``/``max_price``, ``kind`` (``perpetual``), and ``status``.
        """
        return self._api.request(
            "GET",
            "/exchange/v1/derivatives/futures/data/instrument",
            params={"margin_currency_short_name[]": margin_currency},
        )

    # -- helpers -----------------------------------------------------------

    def resolve_pair(self, symbol_or_pair: str) -> str:
        """Convert a ``BTCUSDT`` symbol into the ``B-BTC_USDT`` pair form.

        Already-resolved pairs pass through unchanged. Raises ``KeyError`` when
        the market is unknown, which is a clearer failure than the 404 you
        would otherwise get several layers later.
        """
        if "-" in symbol_or_pair and "_" in symbol_or_pair:
            return symbol_or_pair
        if self._markets_cache is None:
            self._markets_cache = self.markets_details()
        for market in self._markets_cache:
            if market.get("symbol") == symbol_or_pair or market.get("coindcx_name") == symbol_or_pair:
                return market["pair"]
        raise KeyError(f"No CoinDCX market matching {symbol_or_pair!r}")

    def close(self) -> None:
        self._api.close()
        self._public.close()
