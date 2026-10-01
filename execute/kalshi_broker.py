"""
execute/kalshi_broker.py — the live Kalshi broker. LIVE order placement via Kalshi Trade API
v2. RSA-PSS request signing: sign `timestamp + method + full_path` (includes /trade-api/v2,
NEVER the query), headers KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP. Needs
KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH + `cryptography`. Spec per docs.kalshi.com.
place_order() and unwind() refuse (ERROR, nothing sent) unless config.LIVE_TRADING_ENABLED
is true.

Terms invariant (load-bearing — see the README's venue semantics): Kalshi's
book is a SINGLE, unified YES-priced book. Side 'bid' buys the ticker's YES team at the YES
dollar price; side 'ask' buys the opponent (sells YES) at 1 - the YES price. The venue's
`average_fill_price` on a fill from place_order or unwind is reported in YES terms and is
converted here to OUTCOME terms (1 - yes for a NO-side buy) before it is returned —
skipping that conversion books NO-side fills at the wrong price and shows phantom losses.
Side labels must equal the market's registered YES label (`kalshi_sides`) or NO label
(`kalshi_no_sides`) exactly, case-sensitive after strip(); anything else is refused, never
guessed. Fee reporting precedence: venue-reported average_fee_paid, else a scanned fee
field, else the modeled taker_fee() ceil'd to the cent.
"""

import math
import os
import time

import config
from config import CREDENTIAL_ENV, taker_fee
from fetch.kalshi import kalshi_no_sides, kalshi_sides
from .broker import Broker, OrderRequest, OrderResult, OrderStatus

_LIVE_GATE_REASON = ("LIVE_TRADING_ENABLED is not set: live brokers refuse to send orders "
                     "(nothing sent)")


def _resolve_yes(market_id: str, side_label: str) -> bool | None:
    """True if side_label is exactly the market's YES label, False if it is exactly the
    registered NO label, None for anything else. Case-sensitive after strip(); no fuzzy match."""
    label = (side_label or "").strip()
    yes = (kalshi_sides.get(market_id) or "").strip()
    no = (kalshi_no_sides.get(market_id) or "").strip()
    if yes and label == yes:
        return True
    if no and label == no:
        return False
    return None


def _side_error(market_id: str, side_label: str) -> str:
    return (f"side_label {side_label!r} is neither the YES label "
            f"{kalshi_sides.get(market_id, '')!r} nor the NO label "
            f"{kalshi_no_sides.get(market_id, '')!r} on {market_id} (exact match required; "
            f"nothing sent)")


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x, 6) * 100) / 100.0


def _reported_fee(*objs) -> float | None:
    """Pull a fee the venue reports, scanning common keys (number or {'value': ...});
    cents-int tolerated. None if absent -> caller falls back to the modeled fee."""
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for k in ("taker_fees", "taker_fill_cost", "fees", "fee", "fee_paid"):
            v = obj.get(k)
            if isinstance(v, dict):
                v = v.get("value") or v.get("amount")
            if v not in (None, ""):
                try:
                    f = abs(float(v))
                    return f / 100.0 if f > 5 else f   # Kalshi money is often integer cents
                except (TypeError, ValueError):
                    pass
    return None

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"   # demo: https://external-api.demo.kalshi.co/trade-api/v2
API_PREFIX = "/trade-api/v2"          # included in the signed path (full path from API root)
ORDER_PATH = "/portfolio/events/orders"
ORDER_BY_ID_PATH = "/portfolio/orders/{id}"
OPEN_ORDERS_PATH = "/portfolio/orders"
BATCH_CANCEL_PATH = "/portfolio/events/orders/batched"   # documented batch cancel (2 tokens/order)
BALANCE_PATH = "/portfolio/balance"
_TIF = {"fok": "fill_or_kill", "ioc": "immediate_or_cancel", "gtc": "good_till_canceled"}
_STATUS = {"resting": OrderStatus.RESTING, "canceled": OrderStatus.CANCELED,
          "executed": OrderStatus.FILLED}   # Kalshi order status vocabulary -> ours (PARTIAL: resting w/ fill>0)


class KalshiBroker(Broker):
    platform = "kalshi"
    is_live = True

    def __init__(self):
        env = CREDENTIAL_ENV["kalshi"]
        self.api_key_id = os.environ.get(env["api_key_id"], "")
        self.private_key_path = os.environ.get(env["private_key_path"], "")
        self.api_base = os.environ.get(env["api_base"], "") or DEFAULT_BASE

    @property
    def configured(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)

    # ── AUTH (RSA-PSS request signing) ──────────────────────────────────────
    def _load_key(self):
        """Load the RSA key from a PEM file path, or from inline PEM contents (literal
        \\n permitted) if KALSHI_PRIVATE_KEY_PATH holds the key itself instead of a path."""
        from cryptography.hazmat.primitives import serialization
        v = self.private_key_path.strip()
        if v.startswith("-----BEGIN"):
            pem = v.replace("\\n", "\n").encode()
        else:
            with open(os.path.expanduser(v), "rb") as f:
                pem = f.read()
        return serialization.load_pem_private_key(pem, password=None)

    def _sign(self, method: str, full_path: str, ts: str) -> str:
        """RSA-PSS(SHA-256) over timestamp+method+full_path; base64 signature (lazy crypto import)."""
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key = self._load_key()
        sig = key.sign((ts + method + full_path).encode(),
                       padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict:
        # Docs: "When signing requests, use the path without query parameters." The query
        # still rides the URL — only the signed message strips it (else the venue 401s).
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": self._sign(method, API_PREFIX + path.split("?", 1)[0], ts),
                "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

    # ── transport + reads ────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body_obj=None):
        import json as _json
        import httpx
        body = _json.dumps(body_obj) if body_obj is not None else None
        r = httpx.request(method, self.api_base + path, headers=self._headers(method, path),
                          content=body, timeout=10.0)
        r.raise_for_status()
        return r.json()

    def balance(self) -> float:
        if not self.configured:
            return 0.0
        try:
            d = self._request("GET", BALANCE_PATH)
            return float(d.get("balance_dollars") or (d.get("balance", 0) / 100.0))
        except Exception:
            return 0.0

    def quote(self, market_id: str, side_label: str):
        """Last-look: fresh (ask, depth_contracts) to BUY side_label right now, or None if
        unresolved/no quote. Used to re-confirm an edge at fresh prices before firing."""
        if not kalshi_sides.get(market_id):
            return None
        try:
            data = self._request("GET", f"/markets/{market_id}")
            m = data.get("market", data)
            buying_yes = _resolve_yes(market_id, side_label)
            if buying_yes is None:
                return None   # unknown label: no quote rather than the other side's price
            ask = float((m.get("yes_ask_dollars") if buying_yes else m.get("no_ask_dollars")) or 0.0)
            depth = float((m.get("yes_ask_size_fp") if buying_yes else m.get("no_ask_size_fp")) or 0.0)
            return (round(ask, 4), depth) if 0 < ask < 1 else None
        except Exception:
            return None

    # ── ORDER BODY (unified YES-priced book) ────────────────────────────────
    def _side_price(self, req: OrderRequest):
        """(side, yes_price) to BUY req.side_label. Buying the ticker's YES team = a 'bid'
        at its ask; buying the opponent = an 'ask' (sell YES) at 1 - its ask. Raises
        ValueError when side_label is neither registered label exactly (never guesses)."""
        buying_yes = _resolve_yes(req.market_id, req.side_label)
        if buying_yes is None:
            raise ValueError(_side_error(req.market_id, req.side_label))
        return ("bid", req.price) if buying_yes else ("ask", round(1.0 - req.price, 4))

    def _order_body(self, req: OrderRequest) -> dict:
        side, price = self._side_price(req)
        body = {
            "ticker": req.market_id, "side": side,
            "count": str(int(req.quantity)),             # integer contract count (docs: plain count)
            "price": f"{price:.4f}",                      # YES dollar price (0-1), not cents
            "time_in_force": _TIF.get(req.tif, "fill_or_kill"),
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": req.client_order_id or f"xb-{int(time.time() * 1000)}",
        }
        if req.post_only:
            body["post_only"] = True   # maker-only (confirmed field on CreateOrderV2Request)
        return body

    def build_order(self, req: OrderRequest) -> dict:
        return self._order_body(req)

    # ── PLACE ────────────────────────────────────────────────────────────────
    def place_order(self, req: OrderRequest) -> OrderResult:
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason="KalshiBroker not configured (set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH)")
        if not config.LIVE_TRADING_ENABLED:
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id, reason=_LIVE_GATE_REASON)
        if not kalshi_sides.get(req.market_id):
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason="could not resolve Kalshi YES side for this ticker (run a fetch first)")
        buying_yes = _resolve_yes(req.market_id, req.side_label)
        if buying_yes is None:   # fail closed: an inexact label never falls through to NO
            return OrderResult(OrderStatus.ERROR, self.platform, req.market_id,
                               reason=_side_error(req.market_id, req.side_label))
        body = self._order_body(req)   # hoisted: the client_order_id must survive into except
        try:
            data = self._request("POST", ORDER_PATH, body)
            # The venue may return the order flat or wrapped in "order"; read counts through the
            # same fixed-point helper the lifecycle reads use (fill_count_fp, legacy fill_count).
            o = data.get("order") if isinstance(data.get("order"), dict) else data
            filled = self._fp_count(o, "fill_count")
            # average_fill_price is in YES terms; convert to the OUTCOME we actually bought
            # (YES for a 'bid', the opponent/NO for an 'ask') so cost = real cash, not YES price.
            # WITHOUT this, NO-side fills are booked at 1-cost -> phantom losses.
            avg_yes = float(o.get("average_fill_price") or data.get("average_fill_price")
                            or (req.price if buying_yes else round(1.0 - req.price, 4)))
            avg = avg_yes if buying_yes else round(1.0 - avg_yes, 4)
            # fee: Kalshi returns average_fee_paid (per contract) -> exact; else the confirmed
            # model (0.07*p*(1-p)*qty) ceil to cent. Verified live: avg_fee_paid 0.0170 @ 0.59.
            afp = o.get("average_fee_paid")
            if afp in (None, ""):
                afp = data.get("average_fee_paid")
            if afp not in (None, ""):
                fee = round(float(afp) * filled, 4)
            else:
                fee = _reported_fee(data, data.get("order"))
                if fee is None:
                    fee = _ceil_cent(taker_fee("kalshi", avg, filled)) if filled else 0.0
            oid = str(o.get("order_id") or data.get("order_id") or "")
            venue_status = str(o.get("status") or "").lower()
            if filled >= int(req.quantity):
                status, reason = OrderStatus.FILLED, ""
            elif (req.tif == "gtc" or req.post_only) and venue_status not in ("canceled", "cancelled", "expired"):
                # accepted and still working at the venue: NOT a rejection (a caller that read
                # REJECTED here could place a duplicate while this order rests)
                status = OrderStatus.PARTIAL if filled > 0 else OrderStatus.RESTING
                reason = "accepted, resting at the venue (manage with get_order / cancel_order)"
            else:
                status = OrderStatus.REJECTED
                reason = f"not fully filled ({req.tif}{', venue status ' + venue_status if venue_status else ''})"
            return OrderResult(status, self.platform, req.market_id, filled_qty=int(filled),
                               avg_price=avg, cost=round(filled * avg, 2), fee=round(fee, 4),
                               order_id=oid, reason=reason, raw=data)
        except Exception as e:
            etext = getattr(getattr(e, "response", None), "text", "")
            code = getattr(getattr(e, "response", None), "status_code", None)
            reason = f"{type(e).__name__}: {e}" + (f" | {etext[:200]}" if etext else "")
            if code is not None and 400 <= code < 500:
                # definite venue rejection: the order was NOT placed -> plain ERROR is safe
                return OrderResult(OrderStatus.ERROR, self.platform, req.market_id, reason=reason)
            # Timeout / transport drop / 5xx AFTER the venue may have accepted = AMBIGUOUS:
            # the order can be live or filled. A plain ERROR(filled=0) here would invite a
            # caller to retry at full size or unwind a paired leg: a possible double position
            # or an untracked naked one. One read-only client-id lookup tries to resolve; only
            # "found, terminal, zero fills" downgrades to a safe REJECTED — everything else
            # stays AMBIGUOUS and the caller must verify at the venue before acting.
            o = self._find_by_client_id(body.get("client_order_id", ""))
            if o is not None:
                filled = self._fp_count(o, "fill_count")
                status = str(o.get("status") or "").lower()
                oid = str(o.get("order_id") or o.get("id") or "")
                if filled <= 0 and status in ("canceled", "cancelled", "expired"):
                    return OrderResult(OrderStatus.REJECTED, self.platform, req.market_id,
                                       order_id=oid,
                                       reason=f"resolved by client id: terminal, no fill ({reason})")
                return OrderResult(OrderStatus.AMBIGUOUS, self.platform, req.market_id,
                                   order_id=oid,
                                   reason=f"order AT VENUE (status={status or '?'}, "
                                          f"fills={filled}) after transport error — verify "
                                          f"before acting ({reason})")
            return OrderResult(OrderStatus.AMBIGUOUS, self.platform, req.market_id,
                               reason=f"transport-ambiguous, unresolved by client id: {reason}")

    def _find_by_client_id(self, coid: str):
        """Ambiguity resolution read: scan recent portfolio orders for our client_order_id.
        Read-only; returns the raw order dict or None on any failure (the outage that broke
        the POST usually breaks this too — the caller then stays AMBIGUOUS, fail-safe)."""
        if not coid:
            return None
        try:
            data = self._request("GET", OPEN_ORDERS_PATH)   # unfiltered: recent, all statuses
            for o in (data.get("orders") or []):
                if str(o.get("client_order_id") or "") == coid:
                    return o
        except Exception:
            return None
        return None

    # ── LIFECYCLE (resting-order safety and reconciliation: get, open orders, cancel,
    # cancel all) ──
    @staticmethod
    def _fp_count(o: dict, key: str) -> float:
        """Documented fixed-point STRING count (`fill_count_fp` = "10.00"); legacy int
        (`fill_count`) tolerated as a fallback for robustness."""
        v = o.get(key + "_fp")
        if v in (None, ""):
            v = o.get(key)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def _parse_lifecycle(self, o: dict) -> OrderResult:
        """A GET /portfolio/orders/{id} order object -> OrderResult. Documented schema:
        status enum [resting, canceled, executed]; counts fixed-point strings (fill_count_fp /
        remaining_count_fp / initial_count_fp); prices yes_price_dollars / no_price_dollars;
        fees taker_fees_dollars (+ taker_fill_cost_dollars)."""
        status = o.get("status", "")
        filled = self._fp_count(o, "fill_count")
        st = _STATUS.get(status, OrderStatus.RESTING)
        if st is OrderStatus.RESTING and filled > 0:
            st = OrderStatus.PARTIAL
        avg = float(o.get("yes_price_dollars") or 0.0)
        fee = float(o.get("taker_fees_dollars") or 0.0)
        return OrderResult(st, self.platform, str(o.get("ticker", "")), filled_qty=int(filled),
                           avg_price=avg, cost=round(filled * avg, 2), fee=round(fee, 4),
                           order_id=str(o.get("order_id", "")), reason=status, raw=o)

    def cancel_order(self, order_id: str, market_id: str = "") -> OrderResult:
        """DELETE /portfolio/events/orders/{id}. The documented success response is NOT an
        order object: {order_id, client_order_id, reduced_by, ts_ms} = venue-CONFIRMED cancel."""
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, reason="not configured")
        try:
            data = self._request("DELETE", f"/portfolio/events/orders/{order_id}")
        except Exception as e:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason=f"cancel failed: {e}")
        o = data.get("order") if isinstance(data, dict) else None
        if isinstance(o, dict) and o.get("status"):
            return self._parse_lifecycle(o)   # some responses may still wrap an order object
        if isinstance(data, dict) and ("reduced_by" in data or data.get("order_id")):
            return OrderResult(OrderStatus.CANCELED, self.platform, market_id,
                               order_id=str(data.get("order_id") or order_id),
                               reason="canceled (venue confirmed)", raw=data)
        return OrderResult(OrderStatus.CANCEL_REQUESTED, self.platform, market_id,
                           order_id=order_id,
                           reason="cancel requested — confirm via get_order", raw=data)

    def get_order(self, order_id: str, market_id: str = "") -> OrderResult:
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, reason="not configured")
        try:
            data = self._request("GET", ORDER_BY_ID_PATH.format(id=order_id))
        except Exception as e:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason=f"get_order failed: {e}")
        o = data.get("order", data) if isinstance(data, dict) else {}
        return self._parse_lifecycle(o) if o else OrderResult(
            OrderStatus.ERROR, self.platform, market_id, order_id=order_id, reason="order not found")

    def open_orders(self, market_id: str | None = None) -> list:
        """GET /portfolio/orders?status=resting. A transport/HTTP failure RAISES (caller must
        distinguish 'venue said none' from 'call failed' — kill-path safety)."""
        if not self.configured:
            return []
        params = f"?ticker={market_id}&status=resting" if market_id else "?status=resting"
        data = self._request("GET", OPEN_ORDERS_PATH + params)
        return [self._parse_lifecycle(o) for o in (data.get("orders") or [])]

    def cancel_all(self, market_ids: list | None = None) -> dict:
        """Gather open order ids, then DELETE /portfolio/events/orders/batched (documented
        batch cancel, 2 tokens/order, per-item results). Fail-LOUD: an open_orders or batch
        failure surfaces as {'canceled': N, 'error': ...}, never a bare silent 0."""
        if not self.configured:
            return {"canceled": 0, "reason": "not configured"}
        try:
            ids = [o.order_id for mid in (market_ids or [None])
                  for o in self.open_orders(mid) if o.order_id]
        except Exception as e:
            return {"canceled": 0, "error": f"open_orders failed: {e}"}
        if not ids:
            return {"canceled": 0}
        try:
            data = self._request("DELETE", BATCH_CANCEL_PATH, {"ids": ids})
        except Exception as e:
            return {"canceled": 0, "error": f"batch cancel failed: {e}"}
        canceled, failures = 0, []
        items = (data.get("orders") or []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and not item.get("error"):
                canceled += 1   # per-item confirmed cancel (order object or reduced_by receipt)
            else:
                failures.append(item)
        out = {"canceled": canceled, "raw": data}
        if failures:
            out["error"] = f"{len(failures)} of {len(ids)} cancels failed"
            out["failures"] = failures
        elif not items:
            out["error"] = f"batch response carried no per-item results for {len(ids)} ids"
        return out

    # ── UNWIND ───────────────────────────────────────────────────────────────
    def unwind(self, fill: OrderResult, leg: dict | None = None) -> OrderResult:
        """Flatten by selling the bought side back, marketable IOC. Needs `leg` (the bought
        team) to know which side to close; without it, or when the team is neither registered
        label exactly, nothing is sent and the position must be flattened manually."""
        if not self.configured:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id, reason="not configured")
        if not config.LIVE_TRADING_ENABLED:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id, reason=_LIVE_GATE_REASON)
        if not leg:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id,
                               reason="kalshi unwind needs the original leg — flatten manually")
        bought_yes = _resolve_yes(fill.market_id, leg.get("team", ""))
        if bought_yes is None:   # fail closed: closing the wrong side would OPEN a new position
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id,
                               reason=_side_error(fill.market_id, leg.get("team", ""))
                               + " — flatten manually")
        try:
            close_side = "ask" if bought_yes else "bid"          # opposite of entry
            price = 0.01 if bought_yes else 0.99                  # marketable limit; IOC takes the touch
            body = {"ticker": fill.market_id, "side": close_side,
                    "count": f"{fill.filled_qty}.00",   # live-exercised format; DO NOT change without live verification
                    "price": f"{price:.4f}", "time_in_force": "immediate_or_cancel",
                    "self_trade_prevention_type": "taker_at_cross"}
            data = self._request("POST", ORDER_PATH, body)
            o = data.get("order") if isinstance(data.get("order"), dict) else data
            closed = self._fp_count(o, "fill_count")   # same fixed-point read as place_order
            close_yes = float(o.get("average_fill_price") or data.get("average_fill_price")
                              or 0.0)   # close price in YES terms
            # realized round-trip P&L in OUTCOME terms (we bought the outcome, now sold it back).
            # fill.avg_price is ALREADY outcome terms (place_order converts); the close price here
            # is raw YES, so convert it (1-yes for the NO side we're closing).
            entry_out = fill.avg_price
            exit_out = close_yes if bought_yes else round(1.0 - close_yes, 4)
            afp = o.get("average_fee_paid")
            if afp in (None, ""):
                afp = data.get("average_fee_paid")
            exit_fee = round(float(afp) * closed, 4) if afp not in (None, "") else (
                _ceil_cent(taker_fee("kalshi", close_yes, closed)) if closed else 0.0)
            realized = round(closed * exit_out - fill.filled_qty * entry_out - fill.fee - exit_fee, 4)
            ok = closed >= fill.filled_qty
            return OrderResult(OrderStatus.FILLED if ok else OrderStatus.REJECTED, self.platform,
                               fill.market_id, filled_qty=int(closed), avg_price=exit_out,
                               cost=round(closed * exit_out, 2), fee=exit_fee, realized_pnl=realized,
                               reason="unwound" if ok else "partial unwind", raw=data)
        except Exception as e:
            return OrderResult(OrderStatus.ERROR, self.platform, fill.market_id, reason=f"unwind failed: {e}")
