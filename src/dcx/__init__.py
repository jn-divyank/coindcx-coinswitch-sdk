"""dcx - connector library for the CoinDCX and CoinSwitch trading APIs.

Transport, authentication and endpoint parity. No strategy logic, no position
sizing, no opinions about what to trade - those belong in whatever imports this.

Quick start, no credentials needed::

    from dcx import CoinDCXPublic, CoinSwitchPublic

    market = CoinDCXPublic()
    book = market.orderbook(market.resolve_pair("ETHUSDT"))
    skew = CoinSwitchPublic().server_time_ms() - int(time.time() * 1000)

With credentials (read from the environment - see ``.env.example``)::

    from dcx import CoinDCXClient, CoinSwitchClient

    CoinSwitchClient().validate_keys()      # {'message': 'Valid Access'}
    CoinDCXClient().balances()

**Order placement is dry-run by default.** Going live requires both
``allow_live=True`` on the guard and ``DCX_ALLOW_LIVE_TRADING=1`` in the
environment. This is deliberate: the intended callers are agents, and an agent
retrying a timed-out order or mis-parsing a quantity should hit a wall at the
layer that touches the wire.

Two things worth knowing before you wire up credentials:

* **CoinDCX** supports read-only keys and IP binding. Use a read-only key for
  anything that does not place orders.
* **CoinSwitch** allows one active key pair, with no permission scoping and no
  IP allowlist. Any CoinSwitch key is a full-trade production key, and
  generating a new one invalidates whatever else was using the old one.
"""

from .coindcx.client import CoinDCXClient
from .coindcx.public import CoinDCXPublic
from .coinswitch.client import CoinSwitchClient
from .coinswitch.public import CoinSwitchPublic
from .core.clock import ServerClock
from .core.config import (
    Credentials,
    coindcx_credentials,
    coinswitch_credentials,
    load_dotenv,
)
from .core.errors import (
    ApiError,
    AuthError,
    ConfigError,
    DcxError,
    GuardError,
    RateLimitError,
    ServerError,
    TransportError,
    ValidationError,
)
from .core.guards import TradingGuard, new_client_order_id

__version__ = "0.1.0"

__all__ = [
    "CoinDCXClient",
    "CoinDCXPublic",
    "CoinSwitchClient",
    "CoinSwitchPublic",
    "Credentials",
    "ServerClock",
    "TradingGuard",
    "new_client_order_id",
    "coindcx_credentials",
    "coinswitch_credentials",
    "load_dotenv",
    "DcxError",
    "ApiError",
    "AuthError",
    "ConfigError",
    "GuardError",
    "RateLimitError",
    "ServerError",
    "TransportError",
    "ValidationError",
    "__version__",
]
