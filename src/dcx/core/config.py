"""Credential and safety configuration, loaded from the environment.

Keys are read from environment variables only. Nothing in this library reads a
credential from a file it finds on disk, and nothing writes one anywhere.

Note the asymmetry between the two exchanges, because it changes how you should
handle each key:

* **CoinDCX** supports IP binding and read-only keys. Use a read-only key for
  anything that does not place orders.
* **CoinSwitch** allows exactly one active key pair at a time, with no
  permission scoping and no IP allowlist. Any CoinSwitch key is a full-trade
  production key, and rotating it invalidates the previous pair - which will
  break anything else using it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigError


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class Credentials:
    """An API key/secret pair for one exchange."""

    api_key: str
    api_secret: str
    exchange: str

    def require(self) -> "Credentials":
        """Return self, or raise :class:`ConfigError` naming what is missing."""
        missing = [
            n
            for n, v in (("api_key", self.api_key), ("api_secret", self.api_secret))
            if not v
        ]
        if missing:
            prefix = self.exchange.upper()
            raise ConfigError(
                f"{self.exchange} credentials missing: {', '.join(missing)}. "
                f"Set {prefix}_API_KEY and {prefix}_API_SECRET (see .env.example)."
            )
        return self

    @property
    def is_present(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def __repr__(self) -> str:  # never let a secret reach a log or traceback
        state = "set" if self.is_present else "unset"
        return f"Credentials(exchange={self.exchange!r}, {state})"


def coindcx_credentials() -> Credentials:
    """Read CoinDCX credentials from ``COINDCX_API_KEY`` / ``COINDCX_API_SECRET``."""
    return Credentials(_env("COINDCX_API_KEY"), _env("COINDCX_API_SECRET"), "coindcx")


def coinswitch_credentials() -> Credentials:
    """Read CoinSwitch credentials from ``COINSWITCH_API_KEY`` / ``COINSWITCH_API_SECRET``."""
    return Credentials(
        _env("COINSWITCH_API_KEY"), _env("COINSWITCH_API_SECRET"), "coinswitch"
    )


def env_allows_live_trading() -> bool:
    """True only if ``DCX_ALLOW_LIVE_TRADING`` is exactly ``1``.

    Deliberately strict: "true", "yes" and "on" do not count. Enabling live
    trading should be a considered act, not something a fuzzy value turns on.
    """
    return _env("DCX_ALLOW_LIVE_TRADING") == "1"


def env_max_notional() -> float:
    """Per-order notional ceiling from ``DCX_MAX_NOTIONAL``. 0 means no cap."""
    raw = _env("DCX_MAX_NOTIONAL")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"DCX_MAX_NOTIONAL must be a number, got {raw!r}") from exc


def load_dotenv(path: str = ".env") -> int:
    """Load ``KEY=VALUE`` lines from a .env file into ``os.environ``.

    Existing environment variables win, so a real environment always overrides
    the file. Returns the number of variables set. Missing file is not an error.
    """
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                count += 1
    return count
