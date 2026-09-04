"""Doctor tests, focused on the promise that matters: no values escape.

The report is meant to be safe to paste into a chat. That claim is only worth
anything if it is tested adversarially, so this plants realistic secrets in a
realistic payload and asserts that none of them survive.
"""

from __future__ import annotations

import json

from dcx.doctor import describe, probe, summarize

# A payload shaped like a real authenticated response, salted with values that
# must not appear in the output.
SENSITIVE_PAYLOAD = {
    "user_id": "u_7f3a9c21",
    "email": "trader@example.com",
    "api_key": "e3b0c44298fc1c149afbf4c8996fb924",
    "balances": [
        {"currency": "INR", "balance": "482915.33", "locked_balance": "1200.00"},
        {"currency": "USDT", "balance": "9142.87", "locked_balance": "0.0"},
    ],
    "orderbook": {"80580.3": "0.42447", "80581.35": "0.00014"},
    "leverage": 12,
    "verified": True,
    "note": None,
}

SECRETS = [
    "u_7f3a9c21",
    "trader@example.com",
    "e3b0c44298fc1c149afbf4c8996fb924",
    "482915.33",
    "9142.87",
    "1200.00",
    "0.42447",
    "80580.3",
]


def test_no_sensitive_value_survives_description():
    rendered = json.dumps(describe(SENSITIVE_PAYLOAD))
    for secret in SECRETS:
        assert secret not in rendered, f"{secret!r} leaked into the doctor report"


def test_field_names_are_preserved():
    """Names must survive - they are the entire point of the report."""
    shape = describe(SENSITIVE_PAYLOAD)
    assert set(shape) >= {"user_id", "email", "balances", "leverage", "verified"}
    assert shape["leverage"] == "int"
    assert shape["verified"] == "bool"
    assert shape["note"] == "null"


def test_nested_list_shape_is_sampled_not_copied():
    shape = describe(SENSITIVE_PAYLOAD)["balances"]
    assert shape[0] == "list[1 sampled of 2]"
    assert shape[1] == {"currency": "str", "balance": "str", "locked_balance": "str"}


def test_price_keyed_maps_are_collapsed():
    """CoinDCX order books key by price, so the *keys* are data too."""
    assert describe(SENSITIVE_PAYLOAD)["orderbook"] == {"<numeric-key>": "str"}


def test_deeply_nested_input_terminates():
    deep: dict = {}
    node = deep
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    assert "..." in json.dumps(describe(deep))


def test_probe_records_failure_without_raising():
    def boom():
        raise RuntimeError("connection reset by peer")

    entry = probe("some.endpoint", boom)
    assert entry["ok"] is False
    assert "connection reset" in entry["error"]
    assert "ms" in entry


def test_probe_never_propagates_an_exception():
    """A doctor that crashes on the first failure diagnoses nothing."""
    entry = probe("bad", lambda: (_ for _ in ()).throw(ValueError("nope")))
    assert entry["ok"] is False


def test_summarize_reports_pass_and_fail():
    report = {
        "checks": [
            {"endpoint": "a", "ok": True, "ms": 1.0},
            {"endpoint": "b", "ok": False, "ms": 2.0, "status": 401, "error": "Invalid access"},
        ],
        "notes": ["something worth knowing"],
        "clock_skew_ms": 42,
    }
    text = summarize(report)
    assert "[PASS] a" in text and "[FAIL] b" in text
    assert "Invalid access" in text
    assert "1/2 checks passed" in text
    assert "42ms" in text
    assert "something worth knowing" in text
