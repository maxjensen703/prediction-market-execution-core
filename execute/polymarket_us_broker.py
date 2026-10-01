"""
execute/polymarket_us_broker.py — the live Polymarket US broker: place (IOC, FOK, GTC
post-only, market style), cancel, unwind. LIVE order placement on Polymarket
US (QCX), the only US-legal Polymarket venue. Ed25519-signed requests: headers
X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature; the signed string is
`timestamp + method + path` (NO body). Inert without creds; place_order() and unwind()
also refuse (ERROR, nothing sent) unless config.LIVE_TRADING_ENABLED is true. Spec
confirmed against docs.polymarket.us.

Terms invariant (load-bearing — see the README's venue semantics): PM US
prices everything in LONG terms. Orders go to POST /v1/orders with marketSlug + intent
(BUY_LONG = the long team at req.price / BUY_SHORT = the other team at 1 - req.price); the
venue's `avgPx` on any fill is likewise LONG terms, converted to OUTCOME terms here
(1 - avgPx for a SHORT buy) before it is returned. Side role (which team is long)
comes from the fetch-time `polymarket_us_sides` registry, never guessed. Fee reporting
precedence: venue-reported (commissionNotionalTotalCollected first, checked post-2026-07-01),
else the modeled taker_fee() ceil'd to the cent.
"""

import math
import os
import time

import config
from config import (CREDENTIAL_ENV, taker_fee, PM_US_SYNCHRONOUS_EXECUTION,
                    PM_US_MAX_BLOCK_SECONDS, PM_US_TAKER_ORDER_STYLE)
from fetch.polymarket_us import polymarket_us_sides
from .broker import Broker, OrderRequest, OrderResult, OrderStatus


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x, 6) * 100) / 100.0


def _reported_fee(order: dict) -> float | None:
    """A fee the venue reports on the order (number or {'value': ...}); None if absent.
    commissionNotionalTotalCollected is the venue-reported canonical field post-2026-07-01,
    so it's checked first. A present numeric 0 is a VALID reported fee (not a fallthrough to
    the modeled fee) -- use `is None` checks, not truthiness, so 0 survives the dict-unwrap."""
    for k in ("commissionNotionalTotalCollected", "fee", "fees", "feeAmount", "takerFee", "totalFees"):
        v = order.get(k)
        if isinstance(v, dict):
            val = v.get("value")
            v = val if val is not None else v.get("amount")
        if v not in (None, ""):
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
    return None

DEFAULT_BASE = "https://api.polymarket.us"
BALANCES_PATH = "/v1/account/balances"
ORDER_PATH = "/v1/orders"
ORDER_BY_ID = "/v1/order/{id}"          # POST /v1/orders is ASYNC ({id, executions:[]}); poll this for the fill
CANCEL_PATH = "/v1/order/{id}/cancel"
OPEN_ORDERS_PATH = "/v1/orders/open"
CANCEL_ALL_PATH = "/v1/orders/open/cancel"   # POST, body {"slugs": [...]}; [] cancels ALL
PREVIEW_PATH = "/v1/order/preview"      # validates + estimates fill WITHOUT placing (no money)
CLOSE_PATH = "/v1/order/close-position"
POLL_ATTEMPTS = 12                      # GET order-by-id retries; the read API 404s briefly right after the
POLL_DELAY = 0.35                       # async POST, so poll ~4s before giving up (was 1.5s -> false "no fill")
# Order is done resting once it reaches a terminal state (or has nothing left to fill).
TERMINAL_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_EXPIRED", "ORDER_STATE_CANCELED",
                   "ORDER_STATE_CANCELLED", "ORDER_STATE_REJECTED"}
# Terminal states that mean the venue REFUSED the order (preview gate aborts on these).
REJECT_STATES = {"ORDER_STATE_REJECTED", "ORDER_STATE_CANCELED", "ORDER_STATE_CANCELLED"}
# A GTC order accepted and still resting (queued, nothing filled yet).
RESTING_STATES = {"ORDER_STATE_PENDING_NEW", "ORDER_STATE_NEW"}
ORDER_BODY_CONFIRMED = True             # body matches the documented schema; the live POST is also gated by LIVE_TRADING_ENABLED
_TIF = {"fok": "TIME_IN_FORCE_FILL_OR_KILL", "ioc": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "gtc": "TIME_IN_FORCE_GOOD_TILL_CANCEL"}
_LIVE_GATE_REASON = ("LIVE_TRADING_ENABLED is not set: live brokers refuse to send orders "
                     "(nothing sent)")


class PolymarketUSBroker(Broker):
    platform = "polymarket_us"   # the tradeable PM US venue (QCX)
    is_live = True

    def __init__(self):
        env = CREDENTIAL_ENV["polymarket_us"]
        self.api_key = os.environ.get(env["api_key"], "")          # keyId -> X-PM-Access-Key
        self.private_key = os.environ.get(env["private_key"], "")  # Ed25519 secret key
        self.api_base = os.environ.get(env["api_base"], "") or DEFAULT_BASE

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.private_key)

    def resolve_side(self, market_id: str, team_label: str) -> dict | None:
        """{'side_id', 'long'} for a team on a market (captured by the data feed)."""
        return polymarket_us_sides.get(market_id, {}).get(team_label)

    # ── AUTH (Ed25519 request signing: timestamp + method + path; no body) ──
    def _load_key(self):
        import base64
        import binascii
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        raw = self.private_key.strip()
        if "BEGIN" in raw:
            return serialization.load_pem_private_key(raw.encode(), password=None)
        for decode in (base64.b64decode, bytes.fromhex):
            try:
                b = decode(raw)
            except (ValueError, binascii.Error):
                continue
            if len(b) >= 32:
                return Ed25519PrivateKey.from_private_bytes(b[:32])
        raise ValueError("POLYMARKET_US_PRIVATE_KEY: expected a base64/hex Ed25519 seed or PEM")

    def _headers(self, method: str, path: str) -> dict:
        import base64
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(self._load_key().sign((ts + method + path).encode())).decode()
        return {"X-PM-Access-Key": self.api_key, "X-PM-Timestamp": ts,
                "X-PM-Signature": sig, "Content-Type": "application/json"}

    # ── transport + reads ────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body_obj=None):
        import json as _json
        import httpx
        body = _json.dumps(body_obj, separators=(",", ":")) if body_obj is not None else None
        r = httpx.request(method, self.api_base + path, headers=self._headers(method, path),
                          content=body, timeout=10.0)
        r.raise_for_status()
        return r.json()

    def account_balances(self) -> dict:
        return self._request("GET", BALANCES_PATH)

    def balance(self) -> float:
        if not self.configured:
            return 0.0
        try:
            bals = self.account_balances().get("balances", [])
            top = bals[0] if bals else {}
            return float(top.get("buyingPower") or top.get("currentBalance") or 0.0)
        except Exception:
            return 0.0

    def quote(self, market_id: str, side_label: str):
        """Last-look: fresh (ask, depth_usd) to BUY side_label from the live book, or None if
        the side can't be resolved / the book is empty. Used to re-confirm an edge before firing."""
        role = self.resolve_side(market_id, side_label)
        if not role:
            return None
        try:
            import httpx
            from fetch.polymarket_us import fetch_book, _book_quote
            with httpx.Client(timeout=8.0) as c:
                md = fetch_book(market_id, c)
            best_ask, best_bid, ask_usd, bid_usd = _book_quote(md or {})
            if best_ask is None or best_bid is None:
                return None
            is_long = bool(role.get("long"))
            ask = round(best_ask if is_long else 1.0 - best_bid, 4)   # buy long at ask, short at 1-bid
            return (ask, ask_usd if is_long else bid_usd) if 0 < ask < 1 else None
        except Exception:
            return None

    # ── ORDER BODY ───────────────────────────────────────────────────────────
    def _order_body(self, req: OrderRequest, role: dict) -> dict | None:
        """Create-order body to BUY req.side_label. price.value is in LONG terms:
        long team = req.price; short team = 1 - req.price (per the API's YES-price rule).
        PM_US_TAKER_ORDER_STYLE='market' builds a native ORDER_TYPE_MARKET instead; returns
        None (refusal, fail-closed) when the market body's slip/tick inputs are unavailable."""
        is_long = bool(role.get("long"))
        # GTC always builds the LIMIT body — a post-only/resting MARKET order is nonsense.
        market_style, ticks = PM_US_TAKER_ORDER_STYLE == "market" and req.tif != "gtc", 0
        if market_style:
            tick = req.tick if (req.tick or 0) > 0 else self._market_tick(req.market_id)
            if req.slip is None or not tick or tick <= 0:
                return None   # slippageTolerance is MANDATORY (omitting it = undocumented behavior)
            ticks = int(req.slip // tick + 1e-9)   # FLOOR (never exceed the slip budget); 1e-9 guards float noise
        if market_style and ticks > 0:
            body = {
                "marketSlug": req.market_id,
                "type": "ORDER_TYPE_MARKET",
                # CAVEAT: market orders are notional-sized — cash can't guarantee exactly N
                # contracts (fights the exact Kalshi hedge); cumQuantity is truth, partials unwind
                # and price improvement can OVER-buy >N (the engine flattens the excess).
                # req.price = engine's capped limit (ask+slip floored to tick) -> max spend n*(ask+slip).
                "cashOrderQty": {"value": f"{_ceil_cent(req.quantity * req.price):.2f}", "currency": "USD"},
                "slippageTolerance": {"ticks": ticks},
                "intent": "ORDER_INTENT_BUY_LONG" if is_long else "ORDER_INTENT_BUY_SHORT",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            }
        else:
            # market style with 0 floored ticks -> 0-tolerance market semantics unverified; use the limit IOC body
            price_value = req.price if is_long else round(1.0 - req.price, 4)
            body = {
                "marketSlug": req.market_id,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{price_value:.4f}", "currency": "USD"},
                "quantity": req.quantity,
                "tif": _TIF.get(req.tif, "TIME_IN_FORCE_FILL_OR_KILL"),
                "intent": "ORDER_INTENT_BUY_LONG" if is_long else "ORDER_INTENT_BUY_SHORT",
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            }
        is_gtc_post_only = req.tif == "gtc" and req.post_only
        if is_gtc_post_only:
            body["participateDontInitiate"] = True   # maker-only: reject rather than cross
        # resting orders must not block the POST — sync execution is documented for takers only
        if PM_US_SYNCHRONOUS_EXECUTION and not is_gtc_post_only:
            body["synchronousExecution"] = True
            body["maxBlockTime"] = PM_US_MAX_BLOCK_SECONDS
        return body

    @staticmethod
    def _market_tick(market_id: str) -> float:
        """Per-market tick via the shared fetch-time resolver (execute/broker.venue_tick)."""
        from .broker import venue_tick
        return venue_tick("polymarket_us", market_id)

    def build_order(self, req: OrderRequest) -> dict | None:
        """The exact body we'd POST (for preview/inspection); None if the side is unresolved."""
        role = self.resolve_side(req.market_id, req.side_label)
        return self._order_body(req, role) if role else None

    def preview(self, req: OrderRequest) -> dict:
        """POST /v1/order/preview — validate + estimate fill WITHOUT placing (no money).
        The preview endpoint wraps the order in a top-level {"request": {...}}."""
        body = self.build_order(req)
        if body is None:
            raise ValueError(f"unresolved PM US side for '{req.side_label}' on {req.market_id}")
        return self._request("POST", PREVIEW_PATH, {"request": body})

    @staticmethod
    def _extract_order(data: dict) -> dict:
        """The order object, however the API wraps it: executions[].order, nested 'order', or flat.
        One source of truth for the shape so the POST parse and the order-by-id poll never disagree
        (missing a shape mis-reads a real FILL as rejected — or a clean no-fill as 'cannot confirm')."""
        if not isinstance(data, dict):
            return {}
        execs = data.get("executions") or []
        if execs and isinstance(execs[0], dict) and isinstance(execs[0].get("order"), dict):
            return execs[0]["order"]
        if isinstance(data.get("order"), dict):
            return data["order"]
        return data

    @staticmethod
    def _is_resolved(order: dict) -> bool:
        """True once the order has stopped resting: a terminal state, or no quantity left to fill."""
        if order.get("state") in TERMINAL_STATES:
            return True
        lv = order.get("leavesQuantity")
        return lv is not None and float(lv) == 0

    def _parse(self, req: OrderRequest, data: dict, is_long: bool | None = None) -> OrderResult:
        order = self._extract_order(data)
        state = order.get("state", "")
        cum = order.get("cumQuantity")
        filled = float(cum) if cum is not None else (req.quantity if state == "ORDER_STATE_FILLED" else 0.0)
        # avgPx is in LONG terms; a SHORT buy's true per-contract cost is 1 - avgPx.
        px = (order.get("avgPx") or {}).get("value")
        if px is not None and is_long is not None:
            avg = float(px) if is_long else round(1.0 - float(px), 4)
        else:
            avg = req.price
        # fee: venue-reported if present, else the modeled fee (0.06*p*(1-p)*qty, eff. 2026-07-01), ceil to cent
        fee = _reported_fee(order)
        if fee is None:
            fee = _ceil_cent(taker_fee("polymarket_us", avg, filled)) if filled else 0.0
        oid = str(data.get("id") or order.get("id") or "")
        # GTC only: a still-resting order is RESTING (nothing filled) or PARTIAL (some filled),
        # never REJECTED — the taker path (tif != gtc) never reaches this branch.
        if req.tif == "gtc" and not self._is_resolved(order) and state not in REJECT_STATES:
            status = OrderStatus.PARTIAL if filled > 0 else OrderStatus.RESTING
            return OrderResult(status, self.platform, req.market_id, filled_qty=int(filled),
                               avg_price=avg, cost=round(filled * avg, 2), fee=round(fee, 4),
                               order_id=oid, reason=f"state {state or 'unknown'}", raw=data)
        if state in {"ORDER_STATE_CANCELED", "ORDER_STATE_CANCELLED"}:
            return OrderResult(OrderStatus.CANCELED, self.platform, req.market_id,
                               filled_qty=int(filled), avg_price=avg, cost=round(filled * avg, 2),
                               fee=round(fee, 4), order_id=oid, reason=state, raw=data)
        # FILLED needs the venue's own FILLED state or a real target met — req.quantity may be a
        # 0-qty lifecycle sentinel (get_order), where 0>=0 must NOT read a REJECTED/EXPIRED as FILLED.
        ok = state == "ORDER_STATE_FILLED" or (req.quantity > 0 and filled >= req.quantity)
        return OrderResult(OrderStatus.FILLED if ok else OrderStatus.REJECTED, self.platform,
                           req.market_id, filled_qty=int(filled), avg_price=avg,
                           cost=round(filled * avg, 2), fee=round(fee, 4), order_id=oid,
                           reason="" if ok else f"state {state or 'unknown'}", raw=data)

    def _post_order(self, body: dict) -> dict:
        """POST the order; if the gateway demands a wrapper (code 3 'request is required'),
        retry once wrapped in {'request': ...}. A code-3 rejection places nothing, so the
        retry cannot double-fill."""
        try:
            return self._request("POST", ORDER_PATH, body)
        except Exception as e:
            txt = getattr(getattr(e, "response", None), "text", "")
            if '"code":3' in txt or "is required" in txt.lower():
                return self._request("POST", ORDER_PATH, {"request": body})
            raise

    def _poll_order(self, oid: str) -> dict | None:
        """POST /v1/orders is ASYNC; the order resolves a beat later and the read API 404s briefly
        right after the POST. Poll GET /v1/order/{id} until the order is RESOLVED (terminal state or
        leavesQuantity 0) or attempts run out. Returns the last payload that actually carried an order
        object (any shape), or None if the order never became queryable. Accepts terminal states (not
        just leavesQuantity 0) so a clean EXPIRED/CANCELED no-fill resolves instead of timing out."""
        path = ORDER_BY_ID.format(id=oid)
        last = None
        for _ in range(POLL_ATTEMPTS):
            try:
                data = self._request("GET", path)
                order = self._extract_order(data)
                if order:
                    last = data
                    if self._is_resolved(order):
                        return data
            except Exception:
                pass   # 404 during async propagation -> keep polling
            time.sleep(POLL_DELAY)
        return last

    # ── PLACE ────────────────────────────────────────────────────────────────
    def place_order(self, req: OrderRequest) -> OrderResult:
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason="PolymarketUSBroker not configured (set POLYMARKET_US_API_KEY + POLYMARKET_US_PRIVATE_KEY)")
        if not config.LIVE_TRADING_ENABLED:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id, reason=_LIVE_GATE_REASON)
        role = self.resolve_side(req.market_id, req.side_label)
        if not role:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason=f"could not resolve PM US side for '{req.side_label}' on {req.market_id} (run a fetch first)")
        if not ORDER_BODY_CONFIRMED:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason=f"PM US order POST disabled; resolved side, would BUY {req.quantity} @ {req.price}")
        body = self._order_body(req, role)
        if body is None:   # market style without the engine's slip/tick -> fail-closed, place nothing
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason="market order without slippage tolerance")
        try:
            resp = self._post_order(body)
        except Exception as e:   # never crash the caller; classify like the Kalshi broker
            body = getattr(getattr(e, "response", None), "text", "")
            code = getattr(getattr(e, "response", None), "status_code", None)
            reason = f"{type(e).__name__}: {e}" + (f" | {body[:200]}" if body else "")
            if code is not None and 400 <= code < 500:
                # definite venue rejection: the order was NOT placed -> plain ERROR is safe
                return OrderResult(OrderStatus.ERROR, self.platform, req.market_id, reason=reason)
            # Timeout / dropped connection / 5xx after the POST may have reached the venue: the
            # order can be live or filled. AMBIGUOUS, never a clean miss: check open_orders() /
            # get_order() before retrying or unwinding anything against it.
            return OrderResult(OrderStatus.AMBIGUOUS, self.platform, req.market_id,
                               reason=f"transport-ambiguous after send, order may be live — verify "
                                      f"before acting ({reason})")
        is_long = bool(role.get("long"))
        is_gtc = req.tif == "gtc"
        # GTC: a resting acceptance is the expected outcome, not something to poll to terminal —
        # this is the ONE place place_order may return a non-terminal (RESTING) result.
        if is_gtc:
            order = self._extract_order(resp)
            if order or (resp or {}).get("id"):
                return self._parse(req, resp, is_long)
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason="PM US GTC place returned no order/id", raw=resp)
        # Synchronous execution: the venue blocked and the fill is IN this response -> parse it
        # directly, no poll round-trip. Only fall through to the id poll if it didn't resolve in time.
        if PM_US_SYNCHRONOUS_EXECUTION:
            order = self._extract_order(resp)
            if order and self._is_resolved(order):
                return self._parse(req, resp, is_long)
        # Async (or sync didn't resolve): confirm the fill by polling the order id.
        oid = str((resp or {}).get("id") or "")
        if not oid:
            return self._parse(req, resp, is_long)   # no id -> nothing was placed
        polled = self._poll_order(oid)
        if not self._extract_order(polled or {}):
            # The read API never returned this order -> we genuinely don't know if it filled. Do NOT
            # assume a fill nor a clean miss; report ERROR with the order id and "verify manually"
            # so the caller checks the venue before acting on this order or any paired leg.
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id, order_id=oid,
                               reason=f"placed order {oid} but order-by-id never resolved after "
                                      f"{POLL_ATTEMPTS} polls — verify manually", raw=resp)
        return self._parse(req, polled, is_long)

    # ── LIFECYCLE (resting-order plumbing: cancel, get, open orders, cancel all) ──
    def cancel_order(self, order_id: str, market_id: str = "") -> OrderResult:
        """POST /v1/order/{id}/cancel. canceledOrderIds in the response is an ECHO of the
        request, not a confirmation the venue actually canceled — caller confirms via get_order."""
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, reason="not configured")
        try:
            resp = self._request("POST", CANCEL_PATH.format(id=order_id), {"marketSlug": market_id})
        except Exception as e:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason=f"cancel failed: {e}")
        return OrderResult(OrderStatus.CANCEL_REQUESTED, self.platform, market_id, order_id=order_id,
                           reason="cancel requested — confirm via get_order", raw=resp)

    @staticmethod
    def _intent_is_long(order: dict) -> bool | None:
        """long/short from the order's own documented `intent` (ORDER_INTENT_BUY_LONG /
        BUY_SHORT / SELL_LONG / SELL_SHORT); None when absent/unknown."""
        intent = str(order.get("intent") or "")
        if intent.endswith("_LONG"):
            return True
        if intent.endswith("_SHORT"):
            return False
        return None

    def get_order(self, order_id: str, market_id: str = "") -> OrderResult:
        """GET /v1/order/{id} through the shape-tolerant _extract_order/_parse (not a fragile
        polled['order']-only read) — the source of truth for RESTING/PARTIAL/FILLED/CANCELED.
        Side comes from the order's own `intent` field, so avg_price is real outcome terms."""
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, reason="not configured")
        try:
            data = self._request("GET", ORDER_BY_ID.format(id=order_id))
        except Exception as e:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason=f"get_order failed: {e}")
        order = self._extract_order(data)
        req = OrderRequest(self.platform, market_id, "", 0.0, 0, tif="gtc")
        return self._parse(req, data, self._intent_is_long(order)) if order else \
            OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                       reason="order not found", raw=data)

    def open_orders(self, market_id: str | None = None) -> list:
        """GET /v1/orders/open (ALWAYS query-less; filter client-side). Live-verified
        2026-07-10: the ?slugs= form returns 401 — PM's signature check and a query string
        disagree with our signing (the exact deferred verify-flag from the broker review) —
        while the query-less form is auth-proven (07-02 kill + tonight). A transport/HTTP
        failure RAISES (caller must distinguish 'venue said none' from 'call failed')."""
        if not self.configured:
            return []
        data = self._request("GET", OPEN_ORDERS_PATH)
        orders = data.get("orders") if isinstance(data, dict) else data
        out = []
        for o in orders or []:
            oid = str(o.get("id") or "")
            slug = str(o.get("marketSlug") or "")
            if market_id and slug and slug != market_id:
                continue
            out.append(self.get_order(oid, slug or (market_id or ""))) if oid else None
        return out

    def cancel_all(self, market_ids: list | None = None) -> dict:
        """POST /v1/orders/open/cancel, body {"slugs": [...]} (REQUIRED; empty list cancels ALL
        open orders); response {"canceledOrderIds": [...]}. Cancels are exempt from the taker
        5s-latency reject and rate limits (the kill-switch's guarantee)."""
        if not self.configured:
            return {"canceled": 0, "reason": "not configured"}
        body = {"slugs": list(market_ids or [])}
        try:
            resp = self._request("POST", CANCEL_ALL_PATH, body)
        except Exception as e:
            return {"canceled": 0, "error": str(e)}
        return {"canceled": len(resp.get("canceledOrderIds", []) if isinstance(resp, dict) else []), "raw": resp}

    # ── UNWIND ───────────────────────────────────────────────────────────────
    def unwind(self, fill: OrderResult, leg: dict | None = None) -> OrderResult:
        """Flatten a stranded leg by SELLING back what we bought at a tick-valid marketable
        limit, then POLLING to confirm it actually closed. (close-position auto-priced at
        0.999 — a sub-cent tick that the venue rejects — and is unverified, so we don't use it.)
        Reports FILLED only when the close is confirmed; else REJECTED, never a false success."""
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id, reason="not configured")
        if not config.LIVE_TRADING_ENABLED:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id, reason=_LIVE_GATE_REASON)
        qty = int(fill.filled_qty)
        if qty <= 0:
            return OrderResult(OrderStatus.FILLED, self.platform, fill.market_id, reason="nothing to unwind")
        role = self.resolve_side(fill.market_id, (leg or {}).get("team", "")) if leg else None
        if not role:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id,
                               reason="unwind needs the original leg/side — flatten manually")
        is_long = bool(role.get("long"))
        # SELL the held instrument at an aggressive, tick-valid (2-dp) limit so IOC takes the touch.
        body = {
            "marketSlug": fill.market_id, "type": "ORDER_TYPE_LIMIT",
            "price": {"value": "0.01" if is_long else "0.99", "currency": "USD"},
            "quantity": qty, "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "intent": "ORDER_INTENT_SELL_LONG" if is_long else "ORDER_INTENT_SELL_SHORT",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        try:
            resp = self._post_order(body)
        except Exception as e:
            body_txt = getattr(getattr(e, "response", None), "text", "")
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id,
                               reason=f"unwind place failed: {e}" + (f" | {body_txt[:160]}" if body_txt else ""))
        oid = str((resp or {}).get("id") or "")
        polled = self._poll_order(oid) if oid else None
        o = (polled or {}).get("order") if isinstance(polled, dict) else None
        closed = float(o.get("cumQuantity") or 0) if isinstance(o, dict) else 0.0
        px = (o.get("avgPx") or {}).get("value") if isinstance(o, dict) else None
        close_long = float(px) if px is not None else 0.0     # close price in long terms
        exit_out = close_long if is_long else round(1.0 - close_long, 4)   # proceeds/contract in outcome terms
        exit_fee = _ceil_cent(taker_fee("polymarket_us", close_long, closed)) if closed else 0.0
        # fill.avg_price is already in outcome terms (place_order converts short to 1-avgPx)
        realized = round(closed * exit_out - fill.filled_qty * fill.avg_price - fill.fee - exit_fee, 4)
        ok = closed >= qty
        return OrderResult(OrderStatus.FILLED if ok else OrderStatus.REJECTED, self.platform, fill.market_id,
                           filled_qty=int(closed), avg_price=exit_out, cost=round(closed * exit_out, 2),
                           fee=exit_fee, realized_pnl=realized, order_id=oid,
                           reason="position closed" if ok else f"unwind not filled (closed {int(closed)}/{qty}) — verify manually",
                           raw=polled or resp)
