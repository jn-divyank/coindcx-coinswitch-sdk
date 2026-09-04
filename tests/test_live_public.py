"""Live tests against real public endpoints. No credentials needed.

Marked ``live`` because they hit the network; skip with ``-m "not live"``.
They exist to catch the failure this library cannot detect any other way: an
exchange changing a response shape underneath us.

Note what is *not* here. CoinSwitch has exactly one public endpoint - verified
by :func:`test_coinswitch_market_data_requires_auth` below - so there is no
public coverage to be had for its market data. That gap closes only with a
signed request.
"""

from __future__ import annotations

import time

import pytest

from dcx.coindcx.public import CoinDCXPublic
from dcx.coinswitch.public import CoinSwitchPublic
from dcx.core.errors import AuthError
from dcx.core.transport import Transport

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def coindcx():
    client = CoinDCXPublic()
    yield client
    client.close()


def test_markets_returns_symbols(coindcx):
    markets = coindcx.markets()
    assert isinstance(markets, list) and len(markets) > 100
    assert "BTCINR" in markets


def test_markets_details_carry_sizing_fields(coindcx):
    """These fields are what any order builder needs; if they vanish, sizing breaks."""
    details = coindcx.markets_details()
    entry = next(m for m in details if m["symbol"] == "BTCINR")
    for field in (
        "min_quantity",
        "max_quantity",
        "step",
        "min_notional",
        "base_currency_precision",
        "target_currency_precision",
        "order_types",
        "pair",
        "status",
    ):
        assert field in entry, f"markets_details lost {field!r}"


def test_ticker_shape(coindcx):
    entry = coindcx.ticker()[0]
    for field in ("market", "last_price", "bid", "ask", "volume"):
        assert field in entry


def test_orderbook_is_keyed_by_price_string(coindcx):
    """Documented oddity: asks/bids are objects keyed by price, not arrays."""
    book = coindcx.orderbook("B-BTC_USDT")
    assert set(book) >= {"asks", "bids", "timestamp"}
    assert isinstance(book["asks"], dict)
    price, qty = next(iter(book["asks"].items()))
    assert float(price) > 0 and float(qty) >= 0


def test_trade_history_uses_short_field_names(coindcx):
    trades = coindcx.trade_history("B-BTC_USDT", limit=3)
    assert len(trades) <= 3
    assert set(trades[0]) >= {"p", "q", "s", "T", "m"}


def test_candles_are_ohlcv(coindcx):
    candles = coindcx.candles("B-BTC_USDT", "1m", 5)
    assert len(candles) <= 5
    assert set(candles[0]) >= {"open", "high", "low", "close", "volume", "time"}
    assert candles[0]["high"] >= candles[0]["low"]


def test_futures_prices_include_mark_and_funding(coindcx):
    prices = coindcx.futures_prices()["prices"]
    entry = next(iter(prices.values()))
    for field in ("ls", "mp", "fr"):
        assert field in entry, f"futures price feed lost {field!r}"


def test_resolve_pair_maps_symbol_to_pair(coindcx):
    assert coindcx.resolve_pair("BTCUSDT") == "B-BTC_USDT"
    assert coindcx.resolve_pair("B-BTC_USDT") == "B-BTC_USDT"
    with pytest.raises(KeyError):
        coindcx.resolve_pair("NOTAMARKET")


def test_coinswitch_server_time_is_sane():
    """The one public CoinSwitch endpoint, and our clock-offset source."""
    server_ms = CoinSwitchPublic().server_time_ms()
    drift = abs(server_ms - int(time.time() * 1000))
    assert drift < 60_000, (
        f"Clock differs from CoinSwitch by {drift}ms. Beyond 60s every signed "
        "request is rejected - fix the host clock."
    )


@pytest.mark.parametrize(
    "base,path",
    [
        ("https://coinswitch.co", "/trade/api/v2/depth?exchange=coinswitchx&symbol=BTC/INR"),
        ("https://coinswitch.co", "/trade/api/v2/coins?exchange=coinswitchx"),
        ("https://dma.coinswitch.co", "/v5/market/tickers?category=linear"),
    ],
)
def test_coinswitch_market_data_requires_auth(base, path):
    """Documents a real constraint rather than an assumption.

    Unlike most exchanges, CoinSwitch gates even plain market data. If one of
    these ever starts returning 200, that is good news worth acting on - and
    this test failing is how we would find out.
    """
    with Transport(base) as http, pytest.raises(AuthError):
        http.request("GET", path)


def test_futures_active_instruments(coindcx):
    pairs = coindcx.futures_active_instruments()
    assert isinstance(pairs, list) and len(pairs) > 50
    assert all(p.startswith("B-") for p in pairs[:20])


def test_futures_trades_use_spelled_out_field_names(coindcx):
    """Unlike the single-letter spot trade feed."""
    trade = coindcx.futures_trades("B-ETH_USDT")[0]
    assert set(trade) >= {"price", "quantity", "timestamp", "is_maker"}


def test_futures_orderbook_has_its_own_shape(coindcx):
    """`ts`/`vs`, not `timestamp` - a different shape to the spot book."""
    book = coindcx.futures_orderbook("B-ETH_USDT", 10)
    assert set(book) >= {"asks", "bids", "ts"}
    assert isinstance(book["asks"], dict)


def test_futures_candles_take_seconds_not_milliseconds(coindcx):
    now = int(time.time())
    candles = coindcx.futures_candles("B-ETH_USDT", from_ts=now - 3600, to_ts=now)
    assert candles, "no futures candles returned for the last hour"
    assert set(candles[0]) >= {"open", "high", "low", "close", "volume", "time"}
    # A millisecond value here would silently return nothing useful.
    assert candles[0]["time"] > 1_000_000_000_000, "candle time should be ms"
