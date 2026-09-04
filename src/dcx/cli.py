"""Command-line entry point.

The library is the primary interface - an agent that can import Python should
do that. This exists because agents driving a shell reach for a command before
they reach for ``python -c``, and because ``dcx doctor`` needs to be runnable
by a human who is not writing code.

Everything prints JSON to stdout so output pipes into ``jq`` cleanly::

    dcx markets --grep ETH
    dcx book B-ETH_USDT --depth 5
    dcx doctor --out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import doctor as doctor_module
from .coindcx.public import CoinDCXPublic
from .coinswitch.public import CoinSwitchPublic
from .core import config
from .core.errors import DcxError
from .core.guards import TradingGuard


def _emit(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _cmd_markets(args: argparse.Namespace) -> int:
    client = CoinDCXPublic()
    markets = client.markets()
    if args.grep:
        needle = args.grep.upper()
        markets = [m for m in markets if needle in m.upper()]
    _emit(markets[: args.limit])
    client.close()
    return 0


def _cmd_instrument(args: argparse.Namespace) -> int:
    client = CoinDCXPublic()
    wanted = args.symbol.upper()
    matches = [
        m
        for m in client.markets_details()
        if wanted in (m.get("symbol", "").upper(), m.get("pair", "").upper())
    ]
    _emit(matches or {"error": f"no CoinDCX market matching {args.symbol!r}"})
    client.close()
    return 0 if matches else 1


def _cmd_ticker(args: argparse.Namespace) -> int:
    client = CoinDCXPublic()
    rows = client.ticker()
    if args.symbol:
        needle = args.symbol.upper()
        rows = [r for r in rows if needle in r.get("market", "").upper()]
    _emit(rows[: args.limit])
    client.close()
    return 0


def _cmd_book(args: argparse.Namespace) -> int:
    client = CoinDCXPublic()
    book = client.orderbook(client.resolve_pair(args.pair))
    # Price-keyed maps arrive unordered; sort into real depth.
    asks = sorted(((float(p), float(q)) for p, q in book["asks"].items()))[: args.depth]
    bids = sorted(((float(p), float(q)) for p, q in book["bids"].items()), reverse=True)[
        : args.depth
    ]
    spread = (asks[0][0] - bids[0][0]) if asks and bids else None
    _emit({"timestamp": book["timestamp"], "asks": asks, "bids": bids, "spread": spread})
    client.close()
    return 0


def _cmd_candles(args: argparse.Namespace) -> int:
    client = CoinDCXPublic()
    _emit(client.candles(client.resolve_pair(args.pair), args.interval, args.limit))
    client.close()
    return 0


def _cmd_time(_: argparse.Namespace) -> int:
    import time as _time

    client = CoinSwitchPublic()
    server = client.server_time_ms()
    local = int(_time.time() * 1000)
    _emit({"coinswitch_server_ms": server, "local_ms": local, "skew_ms": server - local})
    client.close()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Report configuration without printing any credential."""
    config.load_dotenv(args.env_file)
    guard = TradingGuard(
        allow_live=False,
        env_allows_live=config.env_allows_live_trading(),
        max_notional=config.env_max_notional(),
    )
    _emit(
        {
            "coindcx_credentials": "set" if config.coindcx_credentials().is_present else "unset",
            "coinswitch_credentials": "set"
            if config.coinswitch_credentials().is_present
            else "unset",
            "env_allows_live_trading": config.env_allows_live_trading(),
            "max_notional": config.env_max_notional(),
            "posture": guard.explain(),
        }
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    return doctor_module.main(
        ["--out", args.out, "--env-file", args.env_file]
        + (["--public-only"] if args.public_only else [])
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcx",
        description="CoinDCX and CoinSwitch connector. Market data, diagnostics, config.",
    )
    parser.add_argument("--env-file", default=".env", help="dotenv file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("markets", help="list CoinDCX market symbols")
    p.add_argument("--grep", help="substring filter, case-insensitive")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=_cmd_markets)

    p = sub.add_parser("instrument", help="full metadata for one market (tick size, min notional)")
    p.add_argument("symbol", help="e.g. BTCINR or B-BTC_USDT")
    p.set_defaults(func=_cmd_instrument)

    p = sub.add_parser("ticker", help="24h ticker")
    p.add_argument("symbol", nargs="?", help="optional substring filter")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_ticker)

    p = sub.add_parser("book", help="order book, sorted into depth")
    p.add_argument("pair", help="e.g. B-BTC_USDT or BTCUSDT")
    p.add_argument("--depth", type=int, default=10)
    p.set_defaults(func=_cmd_book)

    p = sub.add_parser("candles", help="OHLCV candles")
    p.add_argument("pair")
    p.add_argument("--interval", default="1m")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_candles)

    p = sub.add_parser("time", help="CoinSwitch server time and local clock skew")
    p.set_defaults(func=_cmd_time)

    p = sub.add_parser("status", help="which credentials are configured, and trading posture")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("doctor", help="run diagnostics and write a value-free report")
    p.add_argument("--out", default="doctor-report.json")
    p.add_argument("--public-only", action="store_true")
    p.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DcxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
