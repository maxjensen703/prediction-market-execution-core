# prediction-market-execution-core

Order placement for **Kalshi** and **Polymarket US** in Python: request signing, order
lifecycle, a fee model, live order books, and a kill switch.

> **WARNING: this software places real orders on regulated exchanges with real money.**
> It is provided as is, with no warranty of any kind (see [LICENSE](LICENSE)). You are solely
> responsible for your venue agreements, position limits, tax and reporting obligations, and
> whether trading these markets is legal where you are. Nothing trades until you supply
> credentials; with credentials and `LIVE_TRADING_ENABLED=1`, the live brokers trade
> immediately. Read [Known limitations](#known-limitations-read-before-trading-live) first.

## What it is

This is the execution layer of a sports prediction-market trading system: the part that turns
"buy 1 contract of this outcome at no worse than this price" into a correctly signed, correctly
oriented order on the venue, and turns the venue's response back into a fill, a miss, or an
explicit "ambiguous, verify before acting". It covers Kalshi (RSA-PSS signed Trade API v2) and
Polymarket US (Ed25519 signed API), plus a paper broker with the same interface and the same fee
model, a risk manager with per-trade and total exposure caps and a kill switch, keyless market
data fetchers that build the side and tick registries the brokers need, and WebSocket clients
that keep live order books.

It is deliberately only the execution layer. There is no strategy, no signal, no sizing logic,
no web service and no persistence: you bring the decision of what to trade, and this code
places it. Everything runs in process and the test suite runs offline with HTTP mocked.

## What is proven and what is not

| Venue | Status |
|---|---|
| **Kalshi** | **Proven live.** One real $1 order was placed, filled and settled through this code on 2026-09-16. Signing, the YES/NO orientation and the fee model were also checked against real fills before that. Five fail-closed changes made after that round trip are covered by unit tests only; see [Origins](#origins). |
| **Polymarket US** | **Not proven.** The broker was built against the venue's published API documentation and is covered by offline tests, but no order has been traded live through this code. Treat it as untested against the real venue. |

## Install

Python 3.12 is what the suite and CI run on (3.10 or newer is required for the syntax used).

```bash
git clone https://github.com/maxjensen703/prediction-market-execution-core.git
cd prediction-market-execution-core
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

## Configuration

Everything secret comes from environment variables; nothing is hardcoded. Copy
[.env.example](.env.example) to `.env` and fill in only what you need. `config.py` loads `./.env`
from the **current working directory** when it is first imported, and real environment variables
always win over the file.

| Variable | Purpose |
|---|---|
| `KALSHI_API_BASE` | Kalshi trading API base URL. Blank means production, `https://external-api.kalshi.com/trade-api/v2`. Kalshi's demo base is `https://external-api.demo.kalshi.co/trade-api/v2`. |
| `KALSHI_API_KEY_ID` | Kalshi API key id (the `KALSHI-ACCESS-KEY` header). |
| `KALSHI_PRIVATE_KEY_PATH` | Path to your Kalshi RSA private key PEM. Inline PEM text is also accepted. |
| `POLYMARKET_US_API_BASE` | Polymarket US trading API base URL. Blank means `https://api.polymarket.us`. |
| `POLYMARKET_US_API_KEY` | Polymarket US key id (the `X-PM-Access-Key` header). |
| `POLYMARKET_US_PRIVATE_KEY` | Polymarket US Ed25519 secret key: a base64 or hex seed, or a PEM. |
| `LIVE_TRADING_ENABLED` | Default off. Set it to `1` (or `true`, `yes`, `on`) to let the live brokers send orders. While it is off, `KalshiBroker` and `PolymarketUSBroker` `place_order()` and `unwind()` return `ERROR` and send nothing. Reads, cancels and `cancel_all()` are not gated. `PaperBroker` ignores it. It is read once, when `config` is first imported. |

Market data reads (`fetch/`, the public WebSocket books) are keyless. Credentials are needed only
for account reads and orders. The market-data URLs and the WebSocket URLs are constants in
`config.py`, not environment variables, and point at production.

## Example

[examples/fetch_market.py](examples/fetch_market.py) builds a `KalshiBroker` from the environment,
pulls the open markets of one Kalshi winner series over the public API, converts them (which fills
the YES and NO side registries), and prints the exact order body the broker *would* send for one market.
It never calls `place_order` and works with no credentials at all.

```bash
.venv/bin/python examples/fetch_market.py            # KXMLBGAME
.venv/bin/python examples/fetch_market.py KXNFLGAME
```

Placing an order looks like this (shown, not run by the example):

```python
from execute.broker import OrderRequest, OrderStatus
from execute.kalshi_broker import KalshiBroker
from execute.paper_broker import PaperBroker
from execute.risk import RiskManager
from config import RISK_LIMITS

risk = RiskManager(dict(RISK_LIMITS))          # one per process, shared by every order call

req = OrderRequest("kalshi", "TICKER", "Yankees", price=0.46, quantity=1, tif="ioc",
                   client_order_id="your-unique-id")
decision = risk.check({}, req.price * req.quantity)
if decision.ok:
    broker = PaperBroker("kalshi")             # swap for KalshiBroker() only when you mean it
    result = broker.place_order(req)
    if result.status == OrderStatus.AMBIGUOUS:
        ...                                    # transport failed after send: verify at the venue first
    elif result.ok:
        risk.record(result.cost + result.fee)
    else:
        ...                                    # not FILLED: check open_orders() before any retry
```

A live `KalshiBroker` or `PolymarketUSBroker` returns `ERROR` and sends nothing unless
`LIVE_TRADING_ENABLED` is on.

`place_order()` results:

| Status | Meaning |
|---|---|
| `FILLED` | The full quantity filled. |
| `REJECTED` | An IOC or FOK order did not fill in full and is not working at the venue. `filled_qty` can be above 0 for an IOC partial. |
| `RESTING` / `PARTIAL` | A GTC or post-only order was accepted and is working at the venue, unfilled or partly filled. Cancel it with `cancel_order()`. |
| `ERROR` | Nothing was placed: not configured, live flag off, unknown market or side label, or a 4xx rejection from the venue. One exception: a Polymarket US order whose id never resolved after polling also returns `ERROR` (with "verify manually" in the reason). |
| `AMBIGUOUS` | The request may have reached the venue and then failed (timeout, dropped connection, 5xx). The order may be live or filled. Verify before acting. |

`side_label` must equal one of the market's registered labels exactly (case-sensitive, surrounding
whitespace ignored on Kalshi). Anything else returns `ERROR` and sends nothing, on both venues.

## API surface

| Module | Purpose |
|---|---|
| `execute/broker.py` | `OrderRequest`, `OrderResult`, `OrderStatus`, the `Broker` interface, and `venue_tick()` (per-market price tick). |
| `execute/kalshi_broker.py` | `KalshiBroker`: RSA-PSS signing, place, cancel, get, open orders, cancel all, unwind, balance, quote. |
| `execute/polymarket_us_broker.py` | `PolymarketUSBroker`: Ed25519 signing, place (IOC, FOK, GTC post-only, market style), preview, cancel, cancel all, unwind, balances. |
| `execute/paper_broker.py` | `PaperBroker`: same interface, simulated fills at the requested price, the modelled taker fee, a virtual balance. No network. |
| `execute/risk.py` | `RiskManager` (per-trade cap, total exposure cap, resting-order caps, kill switch) and `TokenBucket` (rate limiter). |
| `config.py` | Fee model (`PLATFORM_FEES`, `PLATFORM_MAKER_FEES`, `taker_fee`, `maker_fee`), `RISK_LIMITS`, `CREDENTIAL_ENV`, venue URLs, timeouts, the `.env` loader. |
| `fetch/kalshi.py` | Keyless Kalshi market fetch and conversion to canonical lines; fills `kalshi_sides`, `kalshi_no_sides` and `kalshi_ticks`. |
| `fetch/polymarket_us.py` | Keyless Polymarket US market and book fetch; fills `polymarket_us_sides` and `polymarket_us_ticks`. |
| `stream/kalshi_ws.py`, `stream/polymarket_us_ws.py` | Signed WebSocket order-book clients and pure snapshot/delta book state machines. |
| `stream/book_store.py` | Thread-safe store of live books, with a `healthy` flag that starts false. |
| `stream/manager.py` | `StreamManager`: wires the WebSocket clients to a `BookStore` and audits WebSocket books against REST before trusting them. |
| `stream/polymarket_us_private_ws.py` | Authenticated Polymarket US order-events stream (fills), de-duplicated by execution id. |
| `core/schema.py`, `core/teams.py`, `core/teams_data.py`, `core/perf.py` | Line models (moneyline, total, spread), team-name normalisation registry, timing helpers. |
| `tools/` | Command-line operator tools, listed below. |

### Tools

| Command | What it does |
|---|---|
| `python -m tools.preflight` | Read only. Prints the effective config and checks that both venues return data and that credentials are present. |
| `python -m tools.verify_kalshi_auth` | Read only. Signed `GET /portfolio/balance` against production and demo; prints status codes only. |
| `python -m tools.verify_pm_us_auth` | Read only. Signed GET sweep over candidate Polymarket US read paths. |
| `python -m tools.verify_kalshi_order` | **Sends a real order**: a 1-contract fill-or-kill bid at $0.01 on a market whose ask is well above that, so the venue kills it unfilled. It posts through the broker's signed transport directly, so it does **not** check `LIVE_TRADING_ENABLED`. |
| `python -m tools.close_kalshi_position <ticker> <side> [qty]` | **Real money.** Sells an open Kalshi position back with a marketable IOC. Requires `LIVE_TRADING_ENABLED`. |
| `python -m tools.close_pm_us_position <slug> <team> [qty]` | **Real money.** Sells an open Polymarket US position back and polls to confirm the close. Requires `LIVE_TRADING_ENABLED`. |

## Venue semantics (load-bearing)

All prices on an `OrderRequest` and an `OrderResult` are **dollars between 0 and 1, in outcome
terms**: the price to buy the outcome named by `side_label`. Each broker converts to and from the
venue's own terms. Do not re-derive these conversions.

**Kalshi** has one YES-priced book per market. Buying the market's YES team is side `"bid"` at your
price. Buying the other outcome is side `"ask"` (selling YES) at `1 - price`. The venue reports
`average_fill_price` in YES terms; the broker converts it back to outcome terms (`1 - yes`) for a
NO-side buy. Requests are signed with RSA-PSS (SHA-256, digest-length salt) over
`timestamp + method + path`, where the path includes `/trade-api/v2` and never the query string.
The order body sends `price` as a 4-decimal YES dollar string, `count` as an integer string, and
`self_trade_prevention_type: taker_at_cross`.

**Polymarket US** prices everything in LONG terms. `ORDER_INTENT_BUY_LONG` buys the market's long
side at `price`; `ORDER_INTENT_BUY_SHORT` buys the other side at `1 - price`. `avgPx` converts back
the same way. Requests are signed with Ed25519 over `timestamp + method + path`, with no body and no
query string. Order placement is asynchronous: the broker polls `GET /v1/order/{id}` for the
result. A cancel response's `canceledOrderIds` is an echo, not a confirmation; confirm with
`get_order`. Tick sizes can be as small as 0.001. The venue's rate limit is shared per IP between
market data and order calls (about 20 requests a second), so do not add polling loops.

**The registries.** A broker cannot orient an order without knowing which side of a market is YES
(Kalshi) or LONG (Polymarket US), and which tick it trades in. `fetch/kalshi.py` and
`fetch/polymarket_us.py` fill module-level dictionaries (`kalshi_sides`, `kalshi_no_sides`,
`kalshi_ticks`, `polymarket_us_sides`, `polymarket_us_ticks`) when they convert markets, and the
brokers read them at order time. Run a fetch, or set the entries yourself, before placing an
order. Both brokers refuse to place on a market that is missing from its side registry, and on a
`side_label` that is not one of the market's registered labels exactly. On Kalshi that means
`kalshi_sides` (YES) or `kalshi_no_sides` (NO): the opponent for a winner market, `Under` for a
total, the other of `Cover`/`No Cover` for a spread. The registries live in memory and are empty
in a new process.

## Fee model and rounding

Taker fees on both venues are modelled as

    fee = theta * p * (1 - p) * contracts        (p = price in dollars)

with `theta = 0.07` for Kalshi (checked against real fills) and `0.06` for Polymarket US (from the
venue's published schedule, not yet checked against a real fill). `taker_fee()` never returns a
negative value. Maker fees use the same formula with `theta = 0.0175` for Kalshi (provisional) and
`-0.0125` for Polymarket US (a rebate); `maker_fee()` is not clamped, so a rebate is negative.

What a live result reports as `fee`, in order of precedence:

1. **Kalshi:** `average_fee_paid * filled` from the venue, rounded to 4 decimals; else a fee field
   the venue reports on the order (integer values above 5 are read as cents); else the modelled fee
   rounded **up** to the next cent.
2. **Polymarket US:** the venue-reported fee (`commissionNotionalTotalCollected` first; a reported 0
   is honoured); else the modelled fee rounded **up** to the next cent.
3. **Paper:** the modelled fee rounded to 4 decimals, not rounded up, so paper fees can be up to a
   cent lower per order than the live fallback.

`cost` is `filled * average price`, rounded to the cent, before fees. When a market's tick is not in
the registry, `venue_tick()` falls back to 0.01 for Kalshi and 0.005 for Polymarket US.

## Kill switch and risk caps

`RiskManager` is a single in-process risk book. `check(opp, capital)` approves a commitment, capped
at `max_capital_per_trade` and at the room left under `max_total_exposure` (filled plus resting
exposure), or rejects it with a reason. `check_resting()` gates new resting orders against
`max_open_maker_orders` and `max_resting_exposure`. `record()`/`release()` and
`record_resting()`/`release_resting()` keep the book current.

`trip()` sets the kill switch. While it is set, **every** `check()` and `check_resting()` call
rejects with `"kill-switch engaged"`, including hedges and flattening trades. `reset()` clears it.

What the kill switch is not:

- It is not persisted. It lives in one `RiskManager` object in one process.
- The brokers do not consult it. It protects you only if every order passes through
  `risk.check()` first. Build your code so that no path reaches `place_order` without it.
- It does not cancel anything by itself. On a kill, call each live broker's `cancel_all()` and then
  confirm with `open_orders()` that nothing is left. Kalshi's `cancel_all()` reports failures
  rather than a silent zero, and `open_orders()` raises on a failed call so that "the venue says
  none" is never confused with "the call failed". Polymarket US documents its cancel-all endpoint
  as exempt from rate limits.

The default `RISK_LIMITS` are deliberately small ($10 per trade, $40 total). Set your own.

## Known limitations (read before trading live)

These are behaviours of the code as it stands.

- **Check `open_orders()` after any result that is not `FILLED`.** A `REJECTED`, `ERROR`,
  `RESTING`, `PARTIAL` or `AMBIGUOUS` result does not prove that nothing is working at the venue.
  Before retrying or unwinding anything, list the open orders on that market (and `get_order()`
  any id you were given) so a retry never doubles a live order.
- **The live-trading gate covers the brokers' `place_order()` and `unwind()` only.** Reads, cancels
  and `cancel_all()` are never gated, so a kill can always cancel. `tools.verify_kalshi_order`
  posts through the signed transport directly and is not gated. The flag is read once, when
  `config` is first imported.
- **Kalshi `get_order()` and `open_orders()` report `avg_price` as the order's YES limit price**
  (`yes_price_dollars`), not an average fill in outcome terms. For an order on the NO side, convert
  it yourself (`1 - price`). `place_order()` and `unwind()` do convert to outcome terms.
- **Polymarket US: an order id that never resolves after polling returns `ERROR`,** with the order
  id and "verify manually" in the reason, not `AMBIGUOUS`. The order may be live. Check
  `get_order()` before acting. `unwind()` on either venue also returns `ERROR` on a transport
  failure; check the position before retrying it.
- **If you do not supply a `client_order_id`,** Kalshi orders get a timestamp-based one. Supply your
  own unique id so a retry can be matched to the original.
- **Import-time side effects.** Importing `config` reads `./.env`. Importing `core.teams` creates a
  `data/` directory next to the package and logs unrecognised team names to
  `data/unmapped_teams.log`.
- **`config.py` contains settings for a strategy layer that is not included** (the `MAKER_*`,
  `TAKER_*`, scan, ranking and league settings, and `CROSSBOOK_*` environment overrides). The core
  reads only the fee, risk, venue, network, stream and credential settings. The rest is inert here.
- **Sports only.** The fetchers, the team registry and the side conversion are written for
  NFL, NBA, MLB, NHL, WNBA and World Cup markets.

Before any live use: run `python -m tools.preflight`, run `python -m tools.verify_kalshi_auth`, and
place one minimum-size order per venue that you are ready to lose and flatten by hand.

## Tests and CI

```bash
.venv/bin/python -m pytest -q
```

The suite is offline: HTTP is mocked, no credentials are read, and `conftest.py` runs it from an
empty temporary directory so no `.env` is ever loaded into a test process. The public Kalshi
market-list pages under `tests/fixtures/` are real, keyless API responses captured on 2026-09-16.
[CI](.github/workflows/tests.yml) installs the pinned requirements into a fresh virtual
environment and runs the same command on every push and pull request.

## Licence

MIT, copyright 2026 Max Jensen. See [LICENSE](LICENSE), which repeats the warning above.

## Origins

This code was extracted in 2026 from a private trading project, where it was the execution layer
under a market-making and cross-venue strategy that is not included here. The execution code was
held frozen from 2026-07-20, apart from September 2026 fixes to the Kalshi market-title parsing
and to the `verify_kalshi_auth` diagnostic. Comments and docstrings were rewritten for
publication.

The live round trip of 2026-09-16 was made with the original core. Five changes were made
afterward for the public release and are covered by unit tests, not by live trading: fail-closed
side matching, fill counts read from the venue's current response shape, RESTING for accepted GTC
orders, AMBIGUOUS after a sent Polymarket US request fails, and the LIVE_TRADING_ENABLED gate
enforced in the brokers. See [CHANGES.md](CHANGES.md).
