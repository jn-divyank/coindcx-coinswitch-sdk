"""CLI smoke tests. Offline apart from the ones marked live."""

from __future__ import annotations

import pytest

from dcx.cli import build_parser, main


def test_parser_exposes_every_command():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)
    assert commands == {
        "markets",
        "instrument",
        "ticker",
        "book",
        "candles",
        "time",
        "status",
        "doctor",
    }


def test_status_reports_without_credentials(monkeypatch, capsys):
    for var in (
        "COINDCX_API_KEY",
        "COINDCX_API_SECRET",
        "COINSWITCH_API_KEY",
        "COINSWITCH_API_SECRET",
        "DCX_ALLOW_LIVE_TRADING",
        "DCX_MAX_NOTIONAL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert main(["--env-file", "/nonexistent", "status"]) == 0
    out = capsys.readouterr().out
    assert '"coindcx_credentials": "unset"' in out
    assert "dry-run" in out


def test_status_never_prints_a_secret(monkeypatch, capsys):
    monkeypatch.setenv("COINDCX_API_KEY", "PUBLICKEY123")
    monkeypatch.setenv("COINDCX_API_SECRET", "SECRETVALUE456")
    main(["--env-file", "/nonexistent", "status"])
    out = capsys.readouterr().out
    assert "SECRETVALUE456" not in out
    assert "PUBLICKEY123" not in out
    assert '"coindcx_credentials": "set"' in out


@pytest.mark.live
def test_book_command_sorts_depth(capsys):
    assert main(["book", "BTCUSDT", "--depth", "3"]) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    asks = [p for p, _ in payload["asks"]]
    bids = [p for p, _ in payload["bids"]]
    assert asks == sorted(asks), "asks must ascend"
    assert bids == sorted(bids, reverse=True), "bids must descend"
    assert payload["spread"] >= 0


@pytest.mark.live
def test_instrument_unknown_symbol_exits_nonzero(capsys):
    assert main(["instrument", "NOTAREALMARKET"]) == 1
