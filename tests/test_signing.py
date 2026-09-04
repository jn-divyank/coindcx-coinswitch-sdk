"""Signing conformance tests. These run offline, with no credentials.

The CoinSwitch cases assert against the exact worked vectors published in
CoinSwitch's authentication docs, so a passing run means our message
construction matches the vendor's own specification rather than our reading of
it. The CoinDCX cases pin the byte-level behaviour that breaks signatures in
practice: JSON separators, key order, and re-serialization.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dcx.core.errors import ConfigError
from dcx.core.signing import (
    coinswitch_message,
    serialize_coindcx_payload,
    sign_coindcx,
    sign_coinswitch,
)

# --------------------------------------------------------------------------
# CoinSwitch - vectors lifted verbatim from the published auth docs
# --------------------------------------------------------------------------

DOC_EPOCH = 1719905777483

VENDOR_VECTORS = [
    ("GET", "/trade/api/v2/time", "GET/trade/api/v2/time1719905777483"),
    ("POST", "/trade/api/v2/order", "POST/trade/api/v2/order1719905777483"),
    (
        "GET",
        "/trade/api/v2/orders?open=true&exchanges=coinswitchx,c2c1",
        "GET/trade/api/v2/orders?open=true&exchanges=coinswitchx,c2c11719905777483",
    ),
]


@pytest.mark.parametrize("method,path,expected", VENDOR_VECTORS)
def test_coinswitch_message_matches_vendor_vectors(method, path, expected):
    assert coinswitch_message(method, path, DOC_EPOCH) == expected


def test_coinswitch_path_is_signed_url_decoded():
    """A percent-encoded comma must be decoded before signing.

    CoinSwitch signs the decoded path. Signing the encoded form produces a
    valid-looking signature that the server rejects - a 401 that looks like a
    bad key rather than an encoding bug.
    """
    encoded = coinswitch_message(
        "GET", "/trade/api/v2/orders?exchanges=coinswitchx%2Cc2c1", DOC_EPOCH
    )
    plain = coinswitch_message(
        "GET", "/trade/api/v2/orders?exchanges=coinswitchx,c2c1", DOC_EPOCH
    )
    assert encoded == plain


def test_coinswitch_method_is_uppercased():
    assert coinswitch_message("get", "/x", 1) == coinswitch_message("GET", "/x", 1)


def test_coinswitch_signature_verifies_against_public_key():
    """Round-trip: sign with a throwaway key, verify with its public half."""
    private = Ed25519PrivateKey.generate()
    secret_hex = private.private_bytes_raw().hex()
    signature, epoch = sign_coinswitch("GET", "/trade/api/v2/time", secret_hex, epoch=DOC_EPOCH)
    assert epoch == DOC_EPOCH
    message = coinswitch_message("GET", "/trade/api/v2/time", DOC_EPOCH).encode()
    private.public_key().verify(bytes.fromhex(signature), message)  # raises if bad


def test_coinswitch_accepts_64_byte_seed_and_public_key_blob():
    """Some tools export Ed25519 as seed||public. Only the seed is key material."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes_raw()
    public = private.public_key().public_bytes_raw()
    from_seed, _ = sign_coinswitch("GET", "/x", seed.hex(), epoch=1)
    from_blob, _ = sign_coinswitch("GET", "/x", (seed + public).hex(), epoch=1)
    assert from_seed == from_blob


def test_coinswitch_signature_is_epoch_bound():
    """Changing only the epoch must change the signature - it is inside the message."""
    secret = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    a, _ = sign_coinswitch("GET", "/trade/api/v2/time", secret, epoch=1)
    b, _ = sign_coinswitch("GET", "/trade/api/v2/time", secret, epoch=2)
    assert a != b


@pytest.mark.parametrize("bad", ["", "nothex!!", "abcd"])
def test_coinswitch_rejects_malformed_secret(bad):
    with pytest.raises(ConfigError):
        sign_coinswitch("GET", "/x", bad, epoch=1)


# --------------------------------------------------------------------------
# CoinDCX - HMAC-SHA256 over the serialized body
# --------------------------------------------------------------------------

def test_coindcx_signature_is_stable():
    """A pinned vector, so an accidental change to serialization is caught."""
    body, signature = sign_coindcx({"timestamp": 1700000000000}, "secret")
    assert body == '{"timestamp":1700000000000}'
    assert signature == "62eb7dcbcda62b6f07fd37f0a5efb8de204e2700505235aec6b79a40f9d5b295"


def test_coindcx_body_has_no_whitespace():
    """Compact separators. json.dumps defaults to ", " and ": ", which would
    change every byte of the signed message."""
    body = serialize_coindcx_payload({"a": 1, "b": 2})
    assert body == '{"a":1,"b":2}'
    assert " " not in body


def test_coindcx_returned_body_is_what_must_be_sent():
    """Re-serializing the payload instead of sending the returned body is the
    single most common cause of CoinDCX signature failures."""
    payload = {"b": 2, "a": 1}
    body, signature = sign_coindcx(payload, "secret")
    naive = json.dumps(payload)  # default separators, same key order
    assert naive != body, "test is meaningless if these already match"
    _, resigned = sign_coindcx(json.loads(naive), "secret")
    assert resigned == signature  # same data signs the same
    assert naive != body  # but the transmitted bytes would differ


def test_coindcx_key_order_changes_the_signature():
    """Dict order is preserved by json.dumps, so it is part of the signature."""
    a, sig_a = sign_coindcx({"x": 1, "y": 2}, "secret")
    b, sig_b = sign_coindcx({"y": 2, "x": 1}, "secret")
    assert a != b and sig_a != sig_b


def test_coindcx_rejects_empty_secret():
    with pytest.raises(ConfigError):
        sign_coindcx({"timestamp": 1}, "")
