"""Safety guard tests. These are the checks that stop an agent misfiring."""

from __future__ import annotations

import pytest

from dcx.core.errors import GuardError
from dcx.core.guards import TradingGuard, new_client_order_id


def test_dry_run_is_the_default():
    """The whole point: a freshly constructed guard sends nothing."""
    assert TradingGuard().check_order(notional=10.0, description="test") is False


@pytest.mark.parametrize(
    "allow_live,env_allows",
    [(True, False), (False, True), (False, False)],
)
def test_live_requires_both_switches(allow_live, env_allows):
    """Neither the code flag nor the env var is sufficient alone."""
    guard = TradingGuard(allow_live=allow_live, env_allows_live=env_allows)
    assert guard.is_live is False
    assert guard.check_order(notional=1.0, description="test") is False


def test_live_when_both_switches_set():
    guard = TradingGuard(allow_live=True, env_allows_live=True)
    assert guard.is_live is True
    assert guard.check_order(notional=1.0, description="test") is True


def test_max_notional_raises_rather_than_silently_skipping():
    """Breaching the cap is a bug, not a mode - it must be loud."""
    guard = TradingGuard(allow_live=True, env_allows_live=True, max_notional=100.0)
    assert guard.check_order(notional=99.0, description="ok") is True
    with pytest.raises(GuardError, match="exceeds max_notional"):
        guard.check_order(notional=101.0, description="too big")


def test_max_notional_is_enforced_even_in_dry_run():
    """A dry run must not mask a sizing bug that would fail live."""
    guard = TradingGuard(max_notional=100.0)
    with pytest.raises(GuardError):
        guard.check_order(notional=500.0, description="too big")


def test_zero_max_notional_means_no_cap():
    guard = TradingGuard(allow_live=True, env_allows_live=True, max_notional=0.0)
    assert guard.check_order(notional=10**9, description="huge") is True


def test_blocked_orders_are_recorded():
    guard = TradingGuard()
    guard.check_order(notional=1.0, description="sell 1 ETH 3000C")
    assert guard.blocked == ["sell 1 ETH 3000C"]


def test_client_order_ids_are_unique():
    ids = {new_client_order_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_explain_states_the_posture():
    assert "dry-run" in TradingGuard().explain()
    assert "LIVE" in TradingGuard(allow_live=True, env_allows_live=True).explain()
