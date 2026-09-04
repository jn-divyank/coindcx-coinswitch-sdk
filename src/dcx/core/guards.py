"""Pre-flight safety guards for state-changing calls.

This library is driven by agents, not by a human clicking a confirm button. An
agent will eventually retry a timed-out order, or pass a quantity it derived
from a bad parse. These guards sit at the layer that touches the wire, which is
the only place a bug further up cannot route around them.

Three protections:

* **dry run** - the default. Orders are validated, signed, logged and *not
  sent*. Going live needs both ``allow_live=True`` in code and
  ``DCX_ALLOW_LIVE_TRADING=1`` in the environment, so neither a stray flag nor
  a stray env var is enough on its own.
* **max notional** - a hard ceiling per order, in quote currency.
* **client order id** - generated when the caller omits one, so a retried
  request is de-duplicated by the exchange instead of placing a second order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .errors import GuardError


def new_client_order_id(prefix: str = "dcx") -> str:
    """A unique, exchange-safe client order id.

    Always attach one to an order. If a request times out you cannot know
    whether it landed; with a client order id you can query for it instead of
    guessing, and a retry that does reach the exchange is rejected as a
    duplicate rather than filled twice.

    >>> new_client_order_id().startswith("dcx-")
    True
    """
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


@dataclass
class TradingGuard:
    """Decides whether a state-changing request may be sent.

    >>> TradingGuard().check_order(notional=100.0, description="buy 1 ETH")
    False
    >>> TradingGuard(allow_live=True, env_allows_live=True).check_order(
    ...     notional=100.0, description="buy 1 ETH")
    True
    """

    allow_live: bool = False
    env_allows_live: bool = False
    max_notional: float = 0.0
    #: Populated with every blocked call, for the caller to inspect or log.
    blocked: list[str] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        """True only when both the code flag and the environment agree."""
        return bool(self.allow_live and self.env_allows_live)

    def check_order(self, *, notional: float | None, description: str) -> bool:
        """Return True if the order should actually be sent.

        Returns False for a dry run (the caller reports what it *would* have
        done). Raises :class:`GuardError` when a limit is genuinely violated -
        that is a bug to fix, not a mode to operate in.
        """
        if self.max_notional > 0 and notional is not None and notional > self.max_notional:
            raise GuardError(
                f"Order notional {notional:,.2f} exceeds max_notional "
                f"{self.max_notional:,.2f} - refusing to send: {description}"
            )
        if not self.is_live:
            self.blocked.append(description)
            return False
        return True

    def explain(self) -> str:
        """One line describing the current posture, for logs and the CLI."""
        if self.is_live:
            cap = f"max_notional={self.max_notional:,.2f}" if self.max_notional else "no notional cap"
            return f"LIVE TRADING ENABLED ({cap})"
        reasons = []
        if not self.allow_live:
            reasons.append("allow_live=False")
        if not self.env_allows_live:
            reasons.append("DCX_ALLOW_LIVE_TRADING != 1")
        return f"dry-run ({', '.join(reasons)}) - orders will be signed but not sent"
