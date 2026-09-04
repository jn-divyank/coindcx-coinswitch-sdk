# coindcx-coinswitch-sdk

Python connector for the **CoinDCX** and **CoinSwitch** trading APIs.

Transport, authentication, and endpoint parity — nothing else. No strategy
logic, no position sizing, no opinions about what to trade. Those belong in
whatever imports this.

Built to be driven by agents, so the library surface *is* the interface: full
type hints, and a docstring on every public method describing the real response
shape rather than a paraphrase of the vendor docs.

---

## Install

```bash
pip install -e .          # add [dev] for the test suite
```

Requires Python 3.10+, `requests`, and `cryptography`.

## Use it

No credentials needed for CoinDCX market data:

```python
from dcx import CoinDCXPublic

market = CoinDCXPublic()
market.resolve_pair("ETHUSDT")          # 'B-ETH_USDT'  (symbol -> pair form)
market.orderbook("B-ETH_USDT")          # {'timestamp': ..., 'asks': {...}, 'bids': {...}}
market.candles("B-ETH_USDT", "1m", 50)
market.futures_prices()                 # mark price, funding rate, per contract
market.futures_active_instruments()     # every tradable futures pair
market.futures_orderbook("B-ETH_USDT")  # different shape to the spot book
market.futures_trades("B-ETH_USDT")
market.futures_candles("B-ETH_USDT", from_ts=..., to_ts=...)   # seconds, not ms
```

With credentials (read from the environment — copy `.env.example` to `.env`):

```python
from dcx import CoinDCXClient, CoinSwitchClient

CoinSwitchClient().validate_keys()      # {'message': 'Valid Access'}
CoinDCXClient().balances()
```

Or from a shell:

```bash
dcx markets --grep ETH
dcx book BTCUSDT --depth 5
dcx time                                 # server time + local clock skew
dcx status                               # what is configured (never prints a key)
dcx doctor --out doctor-report.json
```

## Placing orders

Every order is validated against **live instrument metadata** before anything is
signed — lot step, min/max quantity, price precision, min notional, supported
order types, and whether the market is still active. Arithmetic is `Decimal`,
because `0.1 + 0.2 != 0.3` becomes a rejected order and `round(2.675, 2)`
becomes a price the exchange refuses.

```python
from dcx import CoinDCXClient, OrderRequest

client = CoinDCXClient()
result = client.place_order(OrderRequest("buy", "BTCINR", "0.00123456", "8500000.99"))

result["dry_run"]       # True
result["adjustments"]   # ['quantity 0.00123456 -> 0.00123 (step 0.00001)',
                        #  'price 8500000.99 -> 8500001.0 (1 dp)']
result["would_send"]    # exact request body, unsent
```

Quantities round **down** to the lot step, never up — rounding up can exceed the
balance the order was sized against. Adjustments are reported, not hidden.

Cancels are deliberately *not* gated by the dry-run guard. A safety mechanism
that stops you closing a position is a hazard, not a protection.

## Safety

Order placement is **dry-run by default**. Going live needs *both*:

```python
TradingGuard(allow_live=True, ...)      # in code
DCX_ALLOW_LIVE_TRADING=1                # in the environment
```

Neither alone is enough. The guard also enforces a per-order `max_notional`
ceiling and attaches a `client_order_id` to every order so a retried request is
de-duplicated by the exchange rather than filled twice.

These checks live in the connector, at the layer that touches the wire, because
that is the only place a bug further up the stack cannot route around them.

## Rate limiting

CoinSwitch publishes exact per-key limits (futures order placement is 20/60s,
tighter than most); CoinDCX publishes none, so it gets a conservative default.
Both clients pace themselves with a sliding-window limiter — a 24/7 agent
polling positions will otherwise find these limits the hard way, and a 429 in
the middle of managing a position is a bad time to discover them.

Hitting the local limit raises `RateLimitError` **without sending**, rather than
blocking indefinitely. `client.limiter.snapshot()` shows current usage.

## What you should know about the two exchanges

They are not symmetric, and the differences matter more than the similarities.

|                     | CoinDCX                            | CoinSwitch                                  |
| ------------------- | ---------------------------------- | ------------------------------------------- |
| Signing             | HMAC-SHA256 over the JSON **body** | Ed25519 over `METHOD + path + epoch`         |
| Headers             | `X-AUTH-APIKEY`, `X-AUTH-SIGNATURE`| plus `X-AUTH-EPOCH` (mandatory)              |
| Public market data  | Yes, extensive                     | **No** — one public endpoint, `/time`        |
| Read-only API keys  | Yes                                | No                                           |
| IP allowlist        | Yes                                | No                                           |
| Multiple keys       | Yes                                | **One active pair**; a new one revokes it    |
| Options API         | Not public                         | Private beta, allowlisted per key            |

**The CoinSwitch credential warning is not boilerplate.** One key pair, no
permission scoping, no IP binding: any key you configure is a full-trade
production key, and rotating it breaks everything else using the old one.

### CoinSwitch is four APIs, not one

| Surface  | Base URL                                       | Notes                       |
| -------- | ---------------------------------------------- | --------------------------- |
| Spot     | `https://coinswitch.co/trade/api/v2`           |                             |
| Futures  | `https://coinswitch.co/trade/api/v2/futures`   |                             |
| HFT      | `https://dma.coinswitch.co` (`/v5`, `/dma/api/v1`) | Bybit-v5 shaped         |
| Options  | HFT surface, `category=option`                 | private beta                |

Options is not a separate module because it is not a separate API — it is a
parameter on the HFT surface. Request access from `api@coinswitch.co`.

## Gotchas this library already handles

Each of these cost a debugging cycle to find, and each is pinned by a test.

- **CoinDCX signs the body string, not the payload.** `sign_coindcx()` returns
  `(body, signature)` and the client transmits that exact body. Re-serializing
  the dict — different separators or key order — silently invalidates the
  signature.
- **CoinSwitch signs the URL-*decoded* path**, so a comma sent as `%2C` but
  signed literally produces a 401 that reads like a bad key.
- **CoinSwitch does not sign the request body at all.** Only method, path and
  epoch.
- **Clock skew is fatal on CoinSwitch.** The epoch is inside the signature and
  drift past 60s is rejected. `ServerClock` measures the offset against the
  public `/time` endpoint and corrects every signature.
- **A few CoinDCX endpoints are signed `GET`s**, not POSTs —
  `futures/wallets` and `futures/wallets/transactions`. Sending them as POST
  returns `404 not_found`, which looks like a wrong path rather than a wrong verb.
- **CoinDCX order books are keyed by price string**, not arrays of pairs. Sort
  the keys numerically; `dcx book` does this for you.
- **CoinDCX uses two pair formats.** `BTCUSDT` on `/exchange` endpoints,
  `B-BTC_USDT` on `/market_data` ones. `resolve_pair()` converts.
- **The HFT surface can report failure inside an HTTP 200** via `retCode`. The
  transport unwraps that envelope so callers see one error shape.
- **5xx on a write is never retried.** CoinSwitch documents it as "operation
  status unknown", so an automatic retry could place the same order twice.
  Reconcile by `client_order_id` instead.

## Testing without sharing credentials

CoinSwitch keys cannot safely leave the machine that owns them, so this repo is
built to be developed and verified without them.

| Layer | What it proves | Needs credentials |
| ----- | -------------- | ----------------- |
| Public endpoint tests | Live response shapes on CoinDCX | No |
| Golden signing vectors | Message construction matches CoinSwitch's own published examples | No |
| Path probes | Every endpoint path exists (401 = path good, 404 = path wrong) | No |
| `dcx doctor` | Signing works end to end against your account | Yes, locally |

```bash
pytest                    # everything
pytest -m "not live"      # offline only, no network
pytest -m live            # hits real public endpoints
```

### The `doctor` handshake

`dcx doctor` runs every read-only endpoint against your account and writes a
report containing **response shapes and error messages only** — no balances, no
ids, no keys. Values are dropped by construction: every leaf is replaced with
its type name, so a balance becomes `"str"` and an account id becomes `"str"`.
There is no code path that copies a leaf value into the report.

```bash
cp .env.example .env      # fill in your keys
dcx doctor --out doctor-report.json
```

The report is what makes typed models possible for endpoints that can only be
reached with a credential. Skim it before sharing — it is designed to be safe
to paste, but it is your account.

## Layout

```
src/dcx/
  core/       signing, transport, errors, clock offset, safety guards
  coindcx/    public market data + authenticated client
  coinswitch/ public (time only) + authenticated client, all surfaces
  doctor.py   value-free diagnostic report
  cli.py      thin argparse wrapper over the same functions
```

## Status

Verified live: all CoinDCX public endpoints, CoinSwitch server time, and every
authenticated endpoint path listed above (via signed requests with throwaway
keys — 401 confirms the path, 404 would mean it is wrong).

Spot order placement and cancellation are built, with construction and
validation fully tested against live instrument metadata. The *send* itself has
only been exercised in dry run — its wire format comes from the docs and is
confirmed by the `doctor` handshake, not by having placed a real order.

Not yet built: futures and HFT order placement, position management, and
websockets. CoinDCX uses socket.io pinned to v2.4.0 and CoinSwitch HFT uses
NATS, so sockets are two separate implementations rather than one — deferred
until they are actually needed.

CI runs the offline suite on Python 3.10–3.12, lints with ruff, scans for
secrets with gitleaks, and runs the live tests daily so an exchange changing a
response shape shows up as a failure rather than a surprise.
