"""Diagnostic handshake: prove signing works, capture response shapes, leak nothing.

The problem this solves: CoinSwitch permits one full-trade API key with no
read-only scope and no IP allowlist, so its credentials can never leave the
machine that owns them. But building typed models needs to know what the
authenticated responses actually look like.

So the credentials stay put and only a *description* travels. This runs every
read-only endpoint and records, per call:

* HTTP status, latency, and the exchange's error message verbatim
* the response **schema** - field names and types, nested - never values
* measured clock skew against each exchange

Values are dropped by construction, not filtered afterwards: :func:`describe`
replaces every leaf with its type name, so a balance becomes ``"float"`` and an
account id becomes ``"str"``. There is no code path that copies a leaf value
into the report.

Usage::

    python -m dcx.doctor --out doctor-report.json

Read the file before sharing it. It is designed to be safe to paste, but it is
your account, so check.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from typing import Any, Callable

from .coindcx.public import CoinDCXPublic
from .coinswitch.public import CoinSwitchPublic
from .core import config
from .core.errors import ApiError, ConfigError, DcxError

MAX_DEPTH = 6
SAMPLE_KEYS = 40


def describe(value: Any, depth: int = 0) -> Any:
    """Reduce a JSON value to a description of its shape.

    Leaves become type names, so no account data survives::

        >>> describe({"currency": "INR", "balance": "12345.67"})
        {'currency': 'str', 'balance': 'str'}
        >>> describe([{"a": 1}, {"a": 2}])
        ['list[1 sampled of 2]', {'a': 'int'}]
        >>> describe(None)
        'null'
    """
    if depth > MAX_DEPTH:
        return "..."
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, str)):
        return type(value).__name__
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= SAMPLE_KEYS:
                out["..."] = f"+{len(value) - SAMPLE_KEYS} more keys"
                break
            # Keys of price-indexed maps (CoinDCX order books) are data, not
            # field names, so they are summarised rather than listed.
            out[k if not _looks_numeric(k) else "<numeric-key>"] = describe(v, depth + 1)
        return out
    if isinstance(value, list):
        if not value:
            return "list[empty]"
        return [f"list[1 sampled of {len(value)}]", describe(value[0], depth + 1)]
    return type(value).__name__


def _looks_numeric(key: str) -> bool:
    try:
        float(key)
        return True
    except (TypeError, ValueError):
        return False


def probe(name: str, call: Callable[[], Any]) -> dict[str, Any]:
    """Run one endpoint and record the outcome as shape-only."""
    started = time.perf_counter()
    entry: dict[str, Any] = {"endpoint": name}
    try:
        result = call()
    except ApiError as exc:
        entry.update(
            ok=False,
            status=exc.status,
            # Vendor error strings, not account data - kept verbatim because
            # they are exactly what makes a failure diagnosable.
            error=exc.message,
            request_id=exc.request_id,
        )
    except ConfigError as exc:
        entry.update(ok=False, status=None, error=f"config: {exc}")
    except DcxError as exc:
        entry.update(ok=False, status=None, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - a doctor must never crash
        entry.update(ok=False, status=None, error=f"unexpected {type(exc).__name__}: {exc}")
    else:
        entry.update(ok=True, status=200, schema=describe(result))
    entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return entry


def run(include_private: bool = True) -> dict[str, Any]:
    """Run the full diagnostic and return the report."""
    report: dict[str, Any] = {
        "dcx_version": "0.1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": platform.python_version(),
        "platform": platform.system(),
        "checks": [],
        "notes": [],
    }
    checks = report["checks"]

    # --- public (always available) ---------------------------------------
    dcx_public = CoinDCXPublic()
    cs_public = CoinSwitchPublic()
    checks.append(probe("coindcx.public.markets_details", dcx_public.markets_details))
    checks.append(probe("coindcx.public.ticker", dcx_public.ticker))
    checks.append(probe("coindcx.public.orderbook", lambda: dcx_public.orderbook("B-BTC_USDT")))
    checks.append(probe("coindcx.public.candles", lambda: dcx_public.candles("B-BTC_USDT", "1m", 5)))
    checks.append(probe("coindcx.public.futures_prices", dcx_public.futures_prices))
    checks.append(probe("coinswitch.public.server_time", cs_public.server_time_ms))

    # --- clock skew -------------------------------------------------------
    try:
        server_ms = cs_public.server_time_ms()
        skew = server_ms - int(time.time() * 1000)
        report["clock_skew_ms"] = skew
        if abs(skew) >= 60_000:
            report["notes"].append(
                f"CRITICAL: local clock is {skew}ms from CoinSwitch server time. "
                "Every signed CoinSwitch request will be rejected. Enable NTP."
            )
        elif abs(skew) >= 2_000:
            report["notes"].append(
                f"Local clock is {skew}ms from server time; requests are corrected "
                "in-library, but the host clock should be fixed."
            )
    except Exception as exc:  # noqa: BLE001
        report["clock_skew_ms"] = None
        report["notes"].append(f"Could not measure clock skew: {exc}")

    dcx_public.close()
    cs_public.close()

    if not include_private:
        return report

    # --- CoinDCX private --------------------------------------------------
    creds = config.coindcx_credentials()
    if not creds.is_present:
        report["notes"].append("CoinDCX credentials not set - private checks skipped.")
    else:
        from .coindcx.client import CoinDCXClient

        client = CoinDCXClient()
        checks.append(probe("coindcx.balances", client.balances))
        checks.append(probe("coindcx.user_info", client.user_info))
        checks.append(probe("coindcx.futures_wallets", client.futures_wallets))
        checks.append(probe("coindcx.futures_positions", client.futures_positions))
        checks.append(probe("coindcx.futures_orders", client.futures_orders))
        checks.append(probe("coindcx.trade_history", client.trade_history))
        client.close()

    # --- CoinSwitch private ----------------------------------------------
    cs_creds = config.coinswitch_credentials()
    if not cs_creds.is_present:
        report["notes"].append("CoinSwitch credentials not set - private checks skipped.")
    else:
        from .coinswitch.client import CoinSwitchClient

        cs = CoinSwitchClient()
        checks.append(probe("coinswitch.validate_keys", cs.validate_keys))
        checks.append(probe("coinswitch.portfolio", cs.portfolio))
        checks.append(probe("coinswitch.orders", cs.orders))
        checks.append(probe("coinswitch.futures_wallet_balance", cs.futures_wallet_balance))
        checks.append(probe("coinswitch.hft_positions[linear]", lambda: cs.hft_positions("linear")))
        # Expected to fail unless the key is on the options private-beta
        # allowlist. Its error message tells us how that gate presents.
        checks.append(probe("coinswitch.hft_positions[option]", lambda: cs.hft_positions("option")))
        cs.close()

    return report


def summarize(report: dict[str, Any]) -> str:
    """Human-readable summary for the terminal."""
    lines = ["", "dcx doctor", "=" * 60]
    for check in report["checks"]:
        mark = "PASS" if check.get("ok") else "FAIL"
        detail = "" if check.get("ok") else f"  {check.get('status') or ''} {check.get('error', '')}"
        lines.append(f"  [{mark}] {check['endpoint']:44s} {check['ms']:>7.1f}ms{detail}")
    passed = sum(1 for c in report["checks"] if c.get("ok"))
    lines.append("-" * 60)
    lines.append(f"  {passed}/{len(report['checks'])} checks passed")
    skew = report.get("clock_skew_ms")
    if skew is not None:
        lines.append(f"  clock skew vs CoinSwitch: {skew}ms")
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dcx-doctor",
        description="Diagnose connectivity and auth. Writes a value-free report.",
    )
    parser.add_argument("--out", default="doctor-report.json", help="report path")
    parser.add_argument("--public-only", action="store_true", help="skip authenticated checks")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load (default: .env)")
    args = parser.parse_args(argv)

    config.load_dotenv(args.env_file)
    report = run(include_private=not args.public_only)
    print(summarize(report))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
    print(f"\n  Report written to {args.out}")
    print("  It contains response shapes and error messages only - no balances,")
    print("  no ids, no keys. Skim it, then it is safe to share.")
    return 0 if all(c.get("ok") for c in report["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
