"""Configuration tests, with emphasis on not leaking secrets."""

from __future__ import annotations

import pytest

from dcx.core.config import (
    Credentials,
    env_allows_live_trading,
    env_max_notional,
    load_dotenv,
)
from dcx.core.errors import ConfigError


def test_repr_never_contains_the_secret():
    """Credentials end up in tracebacks and logs. They must not carry the key."""
    creds = Credentials("PUBLICKEYVALUE", "SUPERSECRETVALUE", "coindcx")
    text = repr(creds)
    assert "SUPERSECRETVALUE" not in text
    assert "PUBLICKEYVALUE" not in text
    assert "coindcx" in text


def test_require_names_what_is_missing():
    with pytest.raises(ConfigError, match="api_secret"):
        Credentials("key", "", "coindcx").require()


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("true", False),
                                            ("yes", False), ("", False)])
def test_live_trading_flag_is_strict(monkeypatch, value, expected):
    """Only "1" counts. Enabling live trading should not happen by accident."""
    monkeypatch.setenv("DCX_ALLOW_LIVE_TRADING", value)
    assert env_allows_live_trading() is expected


def test_max_notional_rejects_garbage(monkeypatch):
    monkeypatch.setenv("DCX_MAX_NOTIONAL", "lots")
    with pytest.raises(ConfigError):
        env_max_notional()


def test_dotenv_does_not_override_real_environment(monkeypatch, tmp_path):
    """A real env var must beat the file, so production config always wins."""
    env_file = tmp_path / ".env"
    env_file.write_text('COINDCX_API_KEY=from_file\nOTHER=from_file\n')
    monkeypatch.setenv("COINDCX_API_KEY", "from_env")
    monkeypatch.delenv("OTHER", raising=False)
    load_dotenv(str(env_file))
    import os
    assert os.environ["COINDCX_API_KEY"] == "from_env"
    assert os.environ["OTHER"] == "from_file"


def test_dotenv_missing_file_is_not_an_error():
    assert load_dotenv("/nonexistent/.env") == 0
