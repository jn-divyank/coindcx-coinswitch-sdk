"""Request signing for both exchanges. This is the load-bearing module.

The two schemes are genuinely different, and each has one detail that silently
breaks everything if you get it wrong:

**CoinDCX** signs the *serialized JSON body string* with HMAC-SHA256::

    body      = json.dumps(payload, separators=(",", ":"))
    signature = hmac_sha256(secret, body).hexdigest()
    headers   = {"X-AUTH-APIKEY": key, "X-AUTH-SIGNATURE": signature}

The trap: you must transmit *the exact string you signed*. Re-serializing the
dict for the request - different separators, different key order - produces a
different byte sequence and the signature no longer matches. That is why
:func:`sign_coindcx` returns the body alongside the signature: callers send
what came back, they never re-encode. A timestamp in milliseconds must be
present inside the payload.

**CoinSwitch** signs ``METHOD + path_with_query + epoch`` with Ed25519::

    message   = "GET" + "/trade/api/v2/time" + "1719905777483"
    signature = ed25519_sign(secret, message).hex()
    headers   = {"X-AUTH-APIKEY": key, "X-AUTH-SIGNATURE": signature,
                 "X-AUTH-EPOCH": epoch}

Two traps: the path is signed **URL-decoded** (a literal comma, not ``%2C``),
and the **request body is not signed at all**. So CoinSwitch has no
canonicalization problem - but it does have a clock problem, since the epoch is
inside the signature and the server rejects drift beyond 60s.

The docstring examples below are the exact worked vectors published in
CoinSwitch's authentication docs; ``tests/test_signing.py`` asserts against
them, which is how this module is verified without any real credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping
from urllib.parse import unquote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .errors import ConfigError

# CoinDCX signs the body string; these separators keep it compact and, more
# importantly, deterministic between the signing step and the send step.
_JSON_SEPARATORS = (",", ":")


def epoch_ms(offset_ms: int = 0) -> int:
    """Current Unix time in milliseconds, plus a server-clock correction."""
    return int(time.time() * 1000) + offset_ms


# --------------------------------------------------------------------------
# CoinDCX - HMAC-SHA256 over the JSON body
# --------------------------------------------------------------------------

def serialize_coindcx_payload(payload: Mapping[str, Any]) -> str:
    """Serialize a payload to the exact string that will be signed and sent."""
    return json.dumps(payload, separators=_JSON_SEPARATORS)


def sign_coindcx(payload: Mapping[str, Any], secret: str) -> tuple[str, str]:
    """Sign a CoinDCX request body.

    Returns ``(body, signature)``. **Send the returned body verbatim** - do not
    re-serialize the payload, or the signature will not match.

    >>> body, sig = sign_coindcx({"timestamp": 1700000000000}, "secret")
    >>> body
    '{"timestamp":1700000000000}'
    >>> sig
    '62eb7dcbcda62b6f07fd37f0a5efb8de204e2700505235aec6b79a40f9d5b295'
    """
    if not secret:
        raise ConfigError("CoinDCX API secret is empty")
    body = serialize_coindcx_payload(payload)
    signature = hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return body, signature


def coindcx_headers(api_key: str, signature: str) -> dict[str, str]:
    """Auth headers for a signed CoinDCX request."""
    return {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": api_key,
        "X-AUTH-SIGNATURE": signature,
    }


# --------------------------------------------------------------------------
# CoinSwitch - Ed25519 over METHOD + path_with_query + epoch
# --------------------------------------------------------------------------

def coinswitch_message(method: str, path_with_query: str, epoch: int | str) -> str:
    """Build the exact string CoinSwitch expects to be signed.

    ``path_with_query`` is the path plus query string with no scheme or host,
    URL-decoded. The method is uppercased; the epoch is decimal milliseconds.

    The three vectors published in CoinSwitch's docs:

    >>> coinswitch_message("GET", "/trade/api/v2/time", 1719905777483)
    'GET/trade/api/v2/time1719905777483'
    >>> coinswitch_message("POST", "/trade/api/v2/order", 1719905777483)
    'POST/trade/api/v2/order1719905777483'
    >>> coinswitch_message(
    ...     "GET",
    ...     "/trade/api/v2/orders?open=true&exchanges=coinswitchx%2Cc2c1",
    ...     1719905777483,
    ... )
    'GET/trade/api/v2/orders?open=true&exchanges=coinswitchx,c2c11719905777483'
    """
    return f"{method.upper()}{unquote(path_with_query)}{epoch}"


def _load_ed25519_key(secret_hex: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from CoinSwitch's hex-encoded secret.

    Accepts either a 32-byte seed (64 hex chars) or a 64-byte
    ``seed || public_key`` blob (128 hex chars), which is how some tools export
    Ed25519 keys. Only the leading 32-byte seed is key material.
    """
    if not secret_hex:
        raise ConfigError("CoinSwitch API secret is empty")
    cleaned = secret_hex.strip().lower()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ConfigError(
            "CoinSwitch API secret must be hex-encoded Ed25519 key material"
        ) from exc
    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise ConfigError(
            f"CoinSwitch API secret must decode to 32 or 64 bytes, got {len(raw)}"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_coinswitch(
    method: str, path_with_query: str, secret_hex: str, epoch: int | None = None
) -> tuple[str, int]:
    """Sign a CoinSwitch request. Returns ``(signature_hex, epoch_ms)``.

    Pass ``epoch`` to reproduce a signature deterministically (tests); omit it
    and the current time is used. The same epoch **must** go out in the
    ``X-AUTH-EPOCH`` header, since it is part of the signed message.
    """
    stamp = epoch_ms() if epoch is None else int(epoch)
    message = coinswitch_message(method, path_with_query, stamp)
    key = _load_ed25519_key(secret_hex)
    return key.sign(message.encode("utf-8")).hex(), stamp


def coinswitch_headers(api_key: str, signature: str, epoch: int) -> dict[str, str]:
    """Auth headers for a signed CoinSwitch request.

    ``X-AUTH-EPOCH`` is mandatory on the HFT surface - omitting it is a 400, not
    a 401 - so it is always sent, on every surface.
    """
    return {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": api_key,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH": str(epoch),
    }
