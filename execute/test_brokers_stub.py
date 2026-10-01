"""Live brokers: inert without creds, and their order bodies build to spec (offline —
no network). Order-body construction is the safety-critical mapping (side + price), so
it's unit-tested deterministically; the live POST itself is verified separately."""

import pytest

from execute.broker import OrderRequest, OrderStatus
from execute.kalshi_broker import KalshiBroker
from execute.polymarket_us_broker import PolymarketUSBroker


@pytest.fixture(autouse=True)
def _live_gate_open(monkeypatch):
    """These tests exercise the brokers past the LIVE_TRADING_ENABLED gate (HTTP is always
    mocked). The gate itself, closed, is tested in execute/test_fail_closed_edges.py."""
    monkeypatch.setattr("config.LIVE_TRADING_ENABLED", True)


def _req(platform="polymarket_us", market_id="x", side="Y", price=0.4, qty=10, tif="ioc"):
    return OrderRequest(platform=platform, market_id=market_id, side_label=side,
                        price=price, quantity=qty, tif=tif)


# ── unconfigured brokers stay inert ───────────────────────────────────────────

def test_kalshi_unconfigured_errors(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    r = KalshiBroker().place_order(_req("kalshi"))
    assert r.status is OrderStatus.ERROR and "not configured" in r.reason


def test_polymarket_us_unconfigured_errors(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_API_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    r = PolymarketUSBroker().place_order(_req("polymarket_us"))
    assert r.status is OrderStatus.ERROR and "not configured" in r.reason


# ── PM US order body (BUY_LONG long team / BUY_SHORT other team; price in long terms) ──

def test_polymarket_us_build_order_long_and_short():
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-x"] = {"Angels": {"side_id": "1", "long": True},
                                    "Athletics": {"side_id": "2", "long": False}}
    bk = PolymarketUSBroker()
    long_body = bk.build_order(_req(market_id="aec-x", side="Angels", price=0.45, qty=2))
    assert long_body["intent"] == "ORDER_INTENT_BUY_LONG"
    assert long_body["price"]["value"] == "0.4500"            # long: price = the ask
    assert long_body["marketSlug"] == "aec-x" and long_body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    short_body = bk.build_order(_req(market_id="aec-x", side="Athletics", price=0.56, qty=2))
    assert short_body["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert short_body["price"]["value"] == "0.4400"          # short: price = 1 - ask
    assert bk.build_order(_req(market_id="aec-x", side="Mets")) is None   # unresolved side


def test_pm_us_order_body_synchronous_flag(monkeypatch):
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-y"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", True)
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_MAX_BLOCK_SECONDS", 3.0)
    b = bk.build_order(_req(market_id="aec-y", side="Angels", price=0.45, qty=1))
    assert b["synchronousExecution"] is True and b["maxBlockTime"] == 3.0
    assert b["type"] == "ORDER_TYPE_LIMIT" and b["quantity"] == 1   # still exact-share limit (hedge intact)
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", False)
    assert "synchronousExecution" not in bk.build_order(_req(market_id="aec-y", side="Angels", price=0.45, qty=1))


def test_pm_us_synchronous_response_parsed_without_poll(monkeypatch):
    # synchronousExecution: the fill is in the POST response -> parse directly, NEVER poll (the ~6s killer).
    bk = PolymarketUSBroker()
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", True)
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True, "side_id": "1"})
    sync_resp = {"id": "OID9", "executions": [{"order": {"state": "ORDER_STATE_FILLED", "cumQuantity": 1,
                 "leavesQuantity": 0, "avgPx": {"value": "0.45"}, "id": "OID9"}}]}
    monkeypatch.setattr(bk, "_post_order", lambda body: sync_resp)
    def _boom(*a, **k): raise AssertionError("_poll_order must not run when the sync response resolves")
    monkeypatch.setattr(bk, "_poll_order", _boom)
    r = bk.place_order(_req(price=0.46, qty=1))
    assert r.status is OrderStatus.FILLED and r.filled_qty == 1 and r.avg_price == pytest.approx(0.45)


def test_pm_us_synchronous_unresolved_falls_back_to_poll(monkeypatch):
    # If the sync response didn't resolve (e.g. maxBlockTime hit), fall back to the id poll.
    bk = PolymarketUSBroker()
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", True)
    monkeypatch.setattr("execute.polymarket_us_broker.time.sleep", lambda *a: None)
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True, "side_id": "1"})
    monkeypatch.setattr(bk, "_post_order", lambda body: {"id": "OID7", "executions": []})   # not resolved
    polled = {"order": {"state": "ORDER_STATE_FILLED", "cumQuantity": 1, "leavesQuantity": 0,
                        "avgPx": {"value": "0.40"}, "id": "OID7"}}
    monkeypatch.setattr(bk, "_poll_order", lambda oid: polled)
    r = bk.place_order(_req(price=0.41, qty=1))
    assert r.status is OrderStatus.FILLED and r.avg_price == pytest.approx(0.40)


# ── PM US fill confirmation: order-by-id poll is shape-tolerant + terminal-state aware ──
# Root cause of the 2026-06-24 false unwinds: the poll only accepted {"order":{leavesQuantity:0}}
# and a 1.5s window, so a clean EXPIRED no-fill (or a brief 404 after the async POST) was reported
# as "could not confirm — verify manually" and the engine unwound the other leg.

def _confirmable_broker(monkeypatch, get_returns):
    """A 'configured' PM US broker whose POST returns an id and whose order-by-id GET returns
    `get_returns` (a dict, or an Exception instance to raise). No network, no signing."""
    bk = PolymarketUSBroker()
    monkeypatch.setattr("execute.polymarket_us_broker.time.sleep", lambda *a: None)   # don't wait out the poll
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True, "side_id": "1"})
    monkeypatch.setattr(bk, "_post_order", lambda body: {"id": "OID1"})
    def _fake_get(method, path, body_obj=None):
        if isinstance(get_returns, Exception):
            raise get_returns
        return get_returns
    monkeypatch.setattr(bk, "_request", _fake_get)
    return bk


def test_pm_us_extract_order_handles_all_shapes():
    bk = PolymarketUSBroker()
    o = {"state": "ORDER_STATE_FILLED"}
    assert bk._extract_order({"order": o}) == o                       # nested
    assert bk._extract_order({"executions": [{"order": o}]}) == o     # under executions
    assert bk._extract_order(o) == o                                  # flat
    assert bk._extract_order({}) == {}


def test_pm_us_expired_order_is_clean_reject_not_verify_manually(monkeypatch):
    # The exact 2026-06-24 case: order placed, IOC expired with zero fill -> clean REJECTED.
    bk = _confirmable_broker(monkeypatch, {"order": {"state": "ORDER_STATE_EXPIRED",
                                                     "cumQuantity": 0, "leavesQuantity": 0, "id": "OID1"}})
    r = bk.place_order(_req(price=0.5, qty=1))
    assert r.status is OrderStatus.REJECTED and r.filled_qty == 0
    assert "EXPIRED" in r.reason and "verify manually" not in r.reason


def test_pm_us_filled_order_confirms_even_when_get_is_flat(monkeypatch):
    # GET returns the order FLAT (no {"order":...} wrapper) — previously this read as "cannot confirm".
    bk = _confirmable_broker(monkeypatch, {"state": "ORDER_STATE_FILLED", "cumQuantity": 1,
                                           "leavesQuantity": 0, "avgPx": {"value": "0.45"}, "id": "OID1"})
    r = bk.place_order(_req(price=0.46, qty=1))
    assert r.status is OrderStatus.FILLED and r.filled_qty == 1 and r.avg_price == pytest.approx(0.45)


def test_pm_us_reported_fee_reads_commission_notional_total_collected(monkeypatch):
    # Post-2026-07-01 responses carry the fee under this key (not the older ones).
    bk = _confirmable_broker(monkeypatch, {"order": {"state": "ORDER_STATE_FILLED", "cumQuantity": 1,
                              "leavesQuantity": 0, "avgPx": {"value": "0.45"}, "id": "OID1",
                              "commissionNotionalTotalCollected": "0.0123"}})
    r = bk.place_order(_req(price=0.46, qty=1))
    assert r.status is OrderStatus.FILLED and r.fee == pytest.approx(0.0123)


def test_reported_fee_prefers_commission_notional_over_other_keys():
    # commissionNotionalTotalCollected is the venue-reported canonical field post-2026-07-01 --
    # it must win even when older fee keys are also present on the payload.
    from execute.polymarket_us_broker import _reported_fee
    order = {"fee": "9.99", "commissionNotionalTotalCollected": "0.0123"}
    assert _reported_fee(order) == pytest.approx(0.0123)


def test_reported_fee_treats_present_zero_as_valid_not_a_fallthrough():
    # A present numeric 0 (flat or wrapped in {'value': 0}) is a REAL reported fee, not an
    # absence -- it must not fall through to the next key or to the modeled fee.
    from execute.polymarket_us_broker import _reported_fee
    assert _reported_fee({"commissionNotionalTotalCollected": 0}) == 0.0
    assert _reported_fee({"commissionNotionalTotalCollected": {"value": 0}, "fee": "9.99"}) == 0.0


def test_pm_us_unresolvable_order_errors_for_manual_check(monkeypatch):
    # GET never resolves (read API down the whole window) -> ERROR flagged for manual verification,
    # NOT a silent assumed-fill or assumed-miss.
    bk = _confirmable_broker(monkeypatch, RuntimeError("404"))
    r = bk.place_order(_req(price=0.5, qty=1))
    assert r.status is OrderStatus.ERROR and "never resolved" in r.reason and r.order_id == "OID1"


def test_polymarket_us_ed25519_signature_no_body(monkeypatch):
    pytest.importorskip("cryptography")
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    monkeypatch.setenv("POLYMARKET_US_API_KEY", "mykeyid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY", base64.b64encode(seed).decode())
    h = PolymarketUSBroker()._headers("POST", "/v1/orders")
    assert h["X-PM-Access-Key"] == "mykeyid" and h["X-PM-Timestamp"].isdigit()
    msg = (h["X-PM-Timestamp"] + "POST" + "/v1/orders").encode()   # signed string = ts+method+path (no body)
    sk.public_key().verify(base64.b64decode(h["X-PM-Signature"]), msg)   # raises if the signature is wrong


# ── PM US market-order style (PM_US_TAKER_ORDER_STYLE="market") ───────────────

def _market_req(monkeypatch, **kw):
    """Flip the style to market, register sides, and build a request (set .slip/.tick after)."""
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_TAKER_ORDER_STYLE", "market")
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-m"] = {"Angels": {"side_id": "1", "long": True},
                                    "Athletics": {"side_id": "2", "long": False}}
    return _req(market_id="aec-m", **kw)


def test_pm_us_market_body_mandatory_slippage_and_ceil_cash(monkeypatch):
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Angels", price=0.463, qty=2)
    r.slip, r.tick = 0.01, 0.005
    b = bk.build_order(r)
    assert b["type"] == "ORDER_TYPE_MARKET"
    assert b["slippageTolerance"] == {"ticks": 2}            # floor(0.01 / 0.005) = 2 (exact)
    assert b["cashOrderQty"]["value"] == "0.93"              # ceil-to-cent of 2 * 0.463 = 0.926
    assert "price" not in b and "quantity" not in b          # cash-sized, no share limit
    assert b["intent"] == "ORDER_INTENT_BUY_LONG"
    assert b["synchronousExecution"] is True                 # same sync/poll resolve logic kept


def test_pm_us_market_body_short_side_cash_is_outcome_terms(monkeypatch):
    # Short leg: intent flips, cash = what we PAY (req.price is already outcome terms — the
    # limit branch's long-terms complement applies to price.value only, which market omits).
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Athletics", price=0.44, qty=1)
    r.slip, r.tick = 0.01, 0.005
    b = bk.build_order(r)
    assert b["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert b["cashOrderQty"]["value"] == "0.44" and "price" not in b


def test_pm_us_market_body_tick_falls_back_to_venue_default(monkeypatch):
    # req.tick unset (paper-built req) -> per-market venue_tick fallback (PM US 0.5c) still works.
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Angels", price=0.46, qty=1)
    r.slip, r.tick = 0.01, None
    b = bk.build_order(r)
    assert b["slippageTolerance"] == {"ticks": 2}            # 0.01 / fallback 0.005


def test_pm_us_market_ticks_floor_never_round_up(monkeypatch):
    # MAJOR-1: slip 0.009 / tick 0.005 = 1.8 -> FLOOR to 1 tick (round would send 2 = 1c, past
    # the 0.9c budget). The submitted tolerance may never exceed the engine's slip budget.
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Angels", price=0.46, qty=1)
    r.slip, r.tick = 0.009, 0.005
    b = bk.build_order(r)
    assert b["type"] == "ORDER_TYPE_MARKET"
    assert b["slippageTolerance"] == {"ticks": 1}


def test_pm_us_market_zero_ticks_falls_back_to_limit_body(monkeypatch):
    # MINOR-5: floored ticks == 0 -> a 0-tolerance market order has unverified semantics;
    # build the LIMIT IOC body instead (same as style "limit_ioc").
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Angels", price=0.46, qty=1)
    r.slip, r.tick = 0.004, 0.005                            # 0.004 // 0.005 -> 0 ticks
    b = bk.build_order(r)
    assert b["type"] == "ORDER_TYPE_LIMIT" and b["price"]["value"] == "0.4600"
    assert b["quantity"] == 1 and b["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert "cashOrderQty" not in b and "slippageTolerance" not in b


def test_pm_us_market_order_refuses_without_slip(monkeypatch):
    # slippageTolerance is MANDATORY: no engine slip -> build refuses, place_order POSTs NOTHING.
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_TAKER_ORDER_STYLE", "market")
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True, "side_id": "1"})
    def _boom(body): raise AssertionError("nothing may be POSTed without a slippage tolerance")
    monkeypatch.setattr(bk, "_post_order", _boom)
    res = bk.place_order(_req(price=0.5, qty=1))             # req.slip defaults to None
    assert res.status is OrderStatus.ERROR
    assert "market order without slippage tolerance" in res.reason
    assert bk.build_order(_req(market_id="aec-m", side="Angels", price=0.5, qty=1)) is None


def test_pm_us_limit_style_ignores_slip_fields_regression(monkeypatch):
    # Default "limit_ioc" must build EXACTLY today's body even when slip/tick ride the request.
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-m"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    r = _req(market_id="aec-m", side="Angels", price=0.45, qty=2)
    r.slip, r.tick = 0.01, 0.005
    b = bk.build_order(r)
    assert b["type"] == "ORDER_TYPE_LIMIT" and b["price"]["value"] == "0.4500"
    assert b["quantity"] == 2 and "cashOrderQty" not in b and "slippageTolerance" not in b


def test_pm_us_gtc_forces_limit_branch_even_in_market_style(monkeypatch):
    # A post-only/resting MARKET order is nonsense: style="market" + tif="gtc" must build the
    # LIMIT body (with participateDontInitiate when post_only), never ORDER_TYPE_MARKET.
    bk = PolymarketUSBroker()
    r = _market_req(monkeypatch, side="Angels", price=0.45, qty=2, tif="gtc")
    r.slip, r.tick, r.post_only = 0.01, 0.005, True
    b = bk.build_order(r)
    assert b["type"] == "ORDER_TYPE_LIMIT" and b["quantity"] == 2
    assert b["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    assert b["participateDontInitiate"] is True
    assert "cashOrderQty" not in b and "slippageTolerance" not in b


# ── Kalshi order body (unified YES-priced book: bid = buy YES team, ask = buy opponent) ──

def test_kalshi_build_order_yes_and_no_side():
    from fetch.kalshi import kalshi_no_sides, kalshi_sides
    kalshi_sides["KX-T"] = "Rangers"           # this ticker's YES ('bid') buys Rangers
    kalshi_no_sides["KX-T"] = "Red Sox"        # and its NO ('ask') buys Red Sox
    bk = KalshiBroker()
    yes = bk.build_order(_req(platform="kalshi", market_id="KX-T", side="Rangers", price=0.51, qty=3))
    assert yes["side"] == "bid" and yes["price"] == "0.5100" and yes["count"] == "3"
    assert yes["time_in_force"] == "immediate_or_cancel"
    no = bk.build_order(_req(platform="kalshi", market_id="KX-T", side="Red Sox", price=0.53, qty=3))
    assert no["side"] == "ask" and no["price"] == "0.4700"     # buy opponent = sell YES at 1 - 0.53
    # Fail closed: a label that is neither registered side is refused, never built as NO.
    with pytest.raises(ValueError):
        bk.build_order(_req(platform="kalshi", market_id="KX-T", side="Mets", price=0.53, qty=3))


def test_kalshi_post_only_flag_rides_the_order_body():
    # MAJOR-5: post_only must reach the venue (confirmed field on CreateOrderV2Request).
    from fetch.kalshi import kalshi_sides
    kalshi_sides["KX-PO"] = "Rangers"
    bk = KalshiBroker()
    r = _req(platform="kalshi", market_id="KX-PO", side="Rangers", price=0.51, qty=3, tif="gtc")
    r.post_only = True
    body = bk.build_order(r)
    assert body["post_only"] is True and body["time_in_force"] == "good_till_canceled"
    r.post_only = False
    assert "post_only" not in bk.build_order(r)   # default omits the field entirely


def test_kalshi_signs_path_without_query_but_url_keeps_it(monkeypatch):
    # Kalshi docs: "When signing requests, use the path without query parameters."
    # The signed message must strip the query; the request URL must still carry it.
    import httpx
    bk = KalshiBroker()
    signed, sent = {}, {}
    def _capture_sign(method, full_path, ts):
        signed["path"] = full_path
        return "sig"
    monkeypatch.setattr(bk, "_sign", _capture_sign)
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"orders": []}
    def _fake_request(method, url, **kw):
        sent["url"] = url
        return _Resp()
    monkeypatch.setattr(httpx, "request", _fake_request)
    bk._request("GET", "/portfolio/orders?ticker=KX-T&status=resting")
    assert signed["path"] == "/trade-api/v2/portfolio/orders"          # no query in the signature
    assert sent["url"].endswith("/portfolio/orders?ticker=KX-T&status=resting")   # query on the URL


# ── PM US GTC + post_only body (participateDontInitiate, no synchronousExecution) ──

def test_pm_us_gtc_post_only_body_has_participate_dont_initiate_no_sync(monkeypatch):
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", True)
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-gtc"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    r = _req(market_id="aec-gtc", side="Angels", price=0.45, qty=2, tif="gtc")
    r.post_only = True
    b = bk.build_order(r)
    assert b["participateDontInitiate"] is True
    assert "synchronousExecution" not in b and "maxBlockTime" not in b
    assert b["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"


def test_pm_us_ioc_body_unaffected_by_post_only_flag_default(monkeypatch):
    # Regression: tif != "gtc" never gets participateDontInitiate even if post_only somehow set.
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-gtc2"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    r = _req(market_id="aec-gtc2", side="Angels", price=0.45, qty=2, tif="ioc")
    r.post_only = True
    b = bk.build_order(r)
    assert "participateDontInitiate" not in b


def test_pm_us_gtc_place_returns_resting_on_pending_new(monkeypatch):
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-gtc3"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    resp = {"id": "OID-R1", "state": "ORDER_STATE_PENDING_NEW", "cumQuantity": 0,
           "leavesQuantity": 2, "id_": None}
    monkeypatch.setattr(bk, "_post_order", lambda body: resp)
    def _boom(*a, **k): raise AssertionError("GTC resting result must not poll to terminal")
    monkeypatch.setattr(bk, "_poll_order", _boom)
    r = _req(market_id="aec-gtc3", side="Angels", price=0.45, qty=2, tif="gtc")
    result = bk.place_order(r)
    assert result.status is OrderStatus.RESTING and result.order_id == "OID-R1"


def test_pm_us_gtc_place_returns_partial_when_some_filled(monkeypatch):
    from fetch.polymarket_us import polymarket_us_sides
    polymarket_us_sides["aec-gtc4"] = {"Angels": {"side_id": "1", "long": True}}
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    resp = {"id": "OID-R2", "state": "ORDER_STATE_NEW", "cumQuantity": 1,
           "leavesQuantity": 1, "avgPx": {"value": "0.45"}}
    monkeypatch.setattr(bk, "_post_order", lambda body: resp)
    r = _req(market_id="aec-gtc4", side="Angels", price=0.45, qty=2, tif="gtc")
    result = bk.place_order(r)
    assert result.status is OrderStatus.PARTIAL and result.filled_qty == 1


def test_pm_us_cancel_order_response_is_echo_not_confirmation(monkeypatch):
    # canceledOrderIds in the cancel response is an ECHO of the request, not proof of cancellation --
    # cancel_order must report CANCEL_REQUESTED (never CANCELED, which means venue-confirmed).
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "_request", lambda method, path, body=None:
                        {"canceledOrderIds": ["OID-R1"]})
    r = bk.cancel_order("OID-R1", market_id="aec-gtc3")
    assert r.status is OrderStatus.CANCEL_REQUESTED
    assert not r.ok
    assert "confirm" in r.reason.lower()   # caller must confirm via get_order, not trust this echo


def test_pm_us_cancel_order_posts_market_slug_body(monkeypatch):
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body=None):
        captured["method"], captured["path"], captured["body"] = method, path, body
        return {"canceledOrderIds": ["OID-9"]}
    monkeypatch.setattr(bk, "_request", _fake)
    bk.cancel_order("OID-9", market_id="aec-slug")
    assert captured["method"] == "POST" and captured["path"] == "/v1/order/OID-9/cancel"
    assert captured["body"] == {"marketSlug": "aec-slug"}


def test_pm_us_get_order_parses_resting_via_extract_and_parse(monkeypatch):
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True})
    monkeypatch.setattr(bk, "_request", lambda method, path, body=None:
                        {"order": {"id": "OID-5", "state": "ORDER_STATE_NEW",
                                  "cumQuantity": 0, "leavesQuantity": 3}})
    r = bk.get_order("OID-5", market_id="aec-g")
    assert r.status is OrderStatus.RESTING and r.order_id == "OID-5"


def test_pm_us_open_orders_lists_resting(monkeypatch):
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True})
    def _fake(method, path, body=None):
        if path.startswith("/v1/orders/open"):
            return {"orders": [{"id": "OID-A", "marketSlug": "aec-o"}]}
        return {"order": {"id": "OID-A", "state": "ORDER_STATE_NEW",
                          "cumQuantity": 0, "leavesQuantity": 1}}
    monkeypatch.setattr(bk, "_request", _fake)
    orders = bk.open_orders("aec-o")
    assert len(orders) == 1 and orders[0].status is OrderStatus.RESTING


def test_pm_us_open_orders_raises_on_transport_error(monkeypatch):
    # MAJOR-3: "call failed" must never masquerade as "venue said none" (silent []).
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    def _boom(method, path, body=None): raise RuntimeError("502")
    monkeypatch.setattr(bk, "_request", _boom)
    with pytest.raises(RuntimeError):
        bk.open_orders("aec-o")


def _lifecycle_get(monkeypatch, order):
    """A configured PM US broker whose GET /v1/order/{id} returns {"order": order}."""
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "_request", lambda method, path, body=None: {"order": order})
    return bk


def test_pm_us_get_order_rejected_with_zero_fill_is_not_filled(monkeypatch):
    # MAJOR-1: the 0-qty lifecycle sentinel made filled(0) >= quantity(0) -> false FILLED.
    bk = _lifecycle_get(monkeypatch, {"id": "OID-RJ", "state": "ORDER_STATE_REJECTED",
                                      "cumQuantity": 0, "leavesQuantity": 0,
                                      "intent": "ORDER_INTENT_BUY_LONG"})
    r = bk.get_order("OID-RJ", market_id="aec-g")
    assert r.status is OrderStatus.REJECTED and r.filled_qty == 0
    assert "REJECTED" in r.reason


def test_pm_us_get_order_expired_with_zero_fill_is_not_filled(monkeypatch):
    bk = _lifecycle_get(monkeypatch, {"id": "OID-EX", "state": "ORDER_STATE_EXPIRED",
                                      "cumQuantity": 0, "leavesQuantity": 0,
                                      "intent": "ORDER_INTENT_BUY_LONG"})
    r = bk.get_order("OID-EX", market_id="aec-g")
    assert r.status is OrderStatus.REJECTED and r.filled_qty == 0
    assert "EXPIRED" in r.reason


def test_pm_us_get_order_canceled_maps_to_canceled(monkeypatch):
    bk = _lifecycle_get(monkeypatch, {"id": "OID-CX", "state": "ORDER_STATE_CANCELED",
                                      "cumQuantity": 0, "leavesQuantity": 0,
                                      "intent": "ORDER_INTENT_BUY_LONG"})
    r = bk.get_order("OID-CX", market_id="aec-g")
    assert r.status is OrderStatus.CANCELED


def test_pm_us_get_order_filled_still_confirms_via_state(monkeypatch):
    # FILLED must still be reachable through the venue's own state despite the 0-qty sentinel.
    bk = _lifecycle_get(monkeypatch, {"id": "OID-F", "state": "ORDER_STATE_FILLED",
                                      "cumQuantity": 2, "leavesQuantity": 0,
                                      "avgPx": {"value": "0.45"},
                                      "intent": "ORDER_INTENT_BUY_LONG"})
    r = bk.get_order("OID-F", market_id="aec-g")
    assert r.status is OrderStatus.FILLED and r.filled_qty == 2


def test_pm_us_get_order_avg_price_from_intent_long_and_short(monkeypatch):
    # MAJOR-2: side truth comes from the order's own `intent`, not resolve_side(market, "").
    long_bk = _lifecycle_get(monkeypatch, {"id": "L1", "state": "ORDER_STATE_FILLED",
                                           "cumQuantity": 3, "leavesQuantity": 0,
                                           "avgPx": {"value": "0.45"},
                                           "intent": "ORDER_INTENT_BUY_LONG"})
    r = long_bk.get_order("L1", market_id="aec-g")
    assert r.avg_price == pytest.approx(0.45)                 # long: avg = avgPx
    short_bk = _lifecycle_get(monkeypatch, {"id": "S1", "state": "ORDER_STATE_FILLED",
                                            "cumQuantity": 3, "leavesQuantity": 0,
                                            "avgPx": {"value": "0.45"},
                                            "intent": "ORDER_INTENT_BUY_SHORT"})
    r = short_bk.get_order("S1", market_id="aec-g")
    assert r.avg_price == pytest.approx(0.55)                 # short: avg = 1 - avgPx (outcome terms)


def test_pm_us_cancel_all_documented_endpoint_and_body(monkeypatch):
    # Documented: POST /v1/orders/open/cancel with body {"slugs": [...]} (kill-switch path).
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body=None):
        captured["method"], captured["path"], captured["body"] = method, path, body
        return {"canceledOrderIds": ["A", "B"]}
    monkeypatch.setattr(bk, "_request", _fake)
    result = bk.cancel_all(["aec-o"])
    assert captured["method"] == "POST" and captured["path"] == "/v1/orders/open/cancel"
    assert captured["body"] == {"slugs": ["aec-o"]}
    assert result["canceled"] == 2


def test_pm_us_cancel_all_no_filter_sends_empty_slugs_list(monkeypatch):
    # Body is REQUIRED: no filter must send {"slugs": []} (cancels ALL), never {}.
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body=None):
        captured["body"] = body
        return {"canceledOrderIds": ["A"]}
    monkeypatch.setattr(bk, "_request", _fake)
    result = bk.cancel_all()
    assert captured["body"] == {"slugs": []}
    assert result["canceled"] == 1


def test_pm_us_cancel_all_failure_is_loud(monkeypatch):
    # Kill-path: a transport failure must surface an error, not a silent {"canceled": 0}.
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    def _boom(method, path, body=None): raise RuntimeError("venue down")
    monkeypatch.setattr(bk, "_request", _boom)
    result = bk.cancel_all()
    assert result["canceled"] == 0 and "venue down" in result["error"]


# ── Kalshi lifecycle (cancel/get/open/cancel-all) ──────────────────────────────
# Fixtures follow the documented GET /portfolio/orders/{order_id} schema: status enum
# [resting, canceled, executed]; fixed-point STRING counts fill_count_fp / remaining_count_fp /
# initial_count_fp (e.g. "10.00"); prices yes_price_dollars / no_price_dollars; fees
# taker_fees_dollars (+ taker_fill_cost_dollars). DELETE cancel returns
# {order_id, client_order_id, reduced_by, ts_ms} — NOT an order object.

def test_kalshi_cancel_order_documented_response_is_confirmed_canceled(monkeypatch):
    # The documented DELETE response has no `status`; reduced_by/order_id without
    # a status = venue-CONFIRMED cancel, which must NOT parse as RESTING.
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body_obj=None):
        captured["method"], captured["path"] = method, path
        return {"order_id": "K1", "client_order_id": "xb-1", "reduced_by": "5.00",
                "ts_ms": 1751300000000}
    monkeypatch.setattr(bk, "_request", _fake)
    r = bk.cancel_order("K1", market_id="KX-T")
    assert captured["method"] == "DELETE" and captured["path"] == "/portfolio/events/orders/K1"
    assert r.status is OrderStatus.CANCELED
    assert r.order_id == "K1"


def test_kalshi_cancel_order_unknown_shape_is_cancel_requested(monkeypatch):
    # A 200 that carries neither an order object nor the documented receipt is only a request.
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "_request", lambda method, path, body_obj=None: {})
    r = bk.cancel_order("K9", market_id="KX-T")
    assert r.status is OrderStatus.CANCEL_REQUESTED


def test_kalshi_get_order_request_construction(monkeypatch):
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body_obj=None):
        captured["path"] = path
        return {"order": {"order_id": "K2", "status": "resting", "ticker": "KX-T",
                          "fill_count_fp": "0.00", "remaining_count_fp": "5.00",
                          "initial_count_fp": "5.00", "yes_price_dollars": "0.5100"}}
    monkeypatch.setattr(bk, "_request", _fake)
    r = bk.get_order("K2", market_id="KX-T")
    assert captured["path"] == "/portfolio/orders/K2"
    assert r.status is OrderStatus.RESTING and r.avg_price == pytest.approx(0.51)


def test_kalshi_get_order_partial_when_resting_with_fills(monkeypatch):
    # Counts are fixed-point strings; resting + fill_count_fp > 0 -> PARTIAL.
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "_request", lambda method, path, body_obj=None:
                        {"order": {"order_id": "K3", "status": "resting", "ticker": "KX-T",
                                  "fill_count_fp": "2.00", "remaining_count_fp": "3.00",
                                  "initial_count_fp": "5.00", "yes_price_dollars": "0.4800",
                                  "taker_fees_dollars": "0.0350"}})
    r = bk.get_order("K3", market_id="KX-T")
    assert r.status is OrderStatus.PARTIAL and r.filled_qty == 2
    assert r.avg_price == pytest.approx(0.48) and r.fee == pytest.approx(0.035)


def test_kalshi_get_order_executed_and_canceled_statuses(monkeypatch):
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    fixtures = {
        "KE": {"order": {"order_id": "KE", "status": "executed", "ticker": "KX-T",
                         "fill_count_fp": "5.00", "remaining_count_fp": "0.00",
                         "yes_price_dollars": "0.5200"}},
        "KC": {"order": {"order_id": "KC", "status": "canceled", "ticker": "KX-T",
                         "fill_count_fp": "0.00", "remaining_count_fp": "0.00",
                         "yes_price_dollars": "0.5200"}},
    }
    monkeypatch.setattr(bk, "_request", lambda method, path, body_obj=None:
                        fixtures[path.rsplit("/", 1)[-1]])
    assert bk.get_order("KE").status is OrderStatus.FILLED
    assert bk.get_order("KE").filled_qty == 5
    assert bk.get_order("KC").status is OrderStatus.CANCELED


def test_kalshi_lifecycle_legacy_int_count_fallback():
    # Robustness: legacy integer fields (fill_count) still parse when the _fp form is absent.
    bk = KalshiBroker()
    r = bk._parse_lifecycle({"order_id": "KL", "status": "resting", "ticker": "KX-T",
                            "fill_count": 2, "remaining_count": 3, "yes_price_dollars": "0.5000"})
    assert r.status is OrderStatus.PARTIAL and r.filled_qty == 2


def test_kalshi_open_orders_request_construction(monkeypatch):
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    captured = {}
    def _fake(method, path, body_obj=None):
        captured["path"] = path
        return {"orders": [{"order_id": "K4", "status": "resting", "ticker": "KX-T",
                            "fill_count_fp": "0.00", "remaining_count_fp": "1.00",
                            "yes_price_dollars": "0.5000"}]}
    monkeypatch.setattr(bk, "_request", _fake)
    orders = bk.open_orders("KX-T")
    assert captured["path"] == "/portfolio/orders?ticker=KX-T&status=resting"
    assert len(orders) == 1 and orders[0].status is OrderStatus.RESTING


def test_kalshi_open_orders_raises_on_transport_error(monkeypatch):
    # MAJOR-3: "call failed" must never masquerade as "venue said none" (silent []).
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    def _boom(method, path, body_obj=None): raise RuntimeError("502")
    monkeypatch.setattr(bk, "_request", _boom)
    with pytest.raises(RuntimeError):
        bk.open_orders("KX-T")


def test_kalshi_cancel_all_uses_documented_batch_endpoint(monkeypatch):
    # MINOR: DELETE /portfolio/events/orders/batched (2 tokens/order, per-item results).
    from execute.broker import OrderResult
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "open_orders", lambda mid=None:
                        [OrderResult(OrderStatus.RESTING, "kalshi", "KX-T", order_id="K5"),
                         OrderResult(OrderStatus.RESTING, "kalshi", "KX-T", order_id="K6")])
    captured = {}
    def _fake(method, path, body_obj=None):
        captured["method"], captured["path"], captured["body"] = method, path, body_obj
        return {"orders": [{"order_id": "K5", "reduced_by": "1.00"},
                           {"order_id": "K6", "error": {"code": "conflict"}}]}
    monkeypatch.setattr(bk, "_request", _fake)
    result = bk.cancel_all()
    assert captured["method"] == "DELETE" and captured["path"] == "/portfolio/events/orders/batched"
    assert captured["body"] == {"ids": ["K5", "K6"]}
    assert result["canceled"] == 1                      # only the venue-confirmed item counts
    assert "1 of 2" in result["error"]                  # per-item failure surfaced, fail-LOUD


def test_kalshi_cancel_all_open_orders_failure_is_loud(monkeypatch):
    # Kill-path: an open_orders failure must return {"canceled": 0, "error": ...}, not a bare 0.
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    def _boom(mid=None): raise RuntimeError("timeout")
    monkeypatch.setattr(bk, "open_orders", _boom)
    result = bk.cancel_all()
    assert result["canceled"] == 0 and "open_orders failed" in result["error"]


# ── venue_tick (execute/broker.py) — per-market tick resolution + fallbacks ────

def test_venue_tick_falls_back_to_venue_default_when_market_unknown():
    from execute.broker import venue_tick
    assert venue_tick("kalshi", "unknown-ticker") == 0.01
    assert venue_tick("polymarket_us", "unknown-slug") == 0.005
    assert venue_tick("polymarket", "x") == 0.01     # non-tradeable-live venue -> generic fallback


def test_venue_tick_reads_per_market_kalshi_tick_dict():
    from execute.broker import venue_tick
    import fetch.kalshi as fk
    fk.kalshi_ticks["KX-SUBPENNY"] = 0.001
    try:
        assert venue_tick("kalshi", "KX-SUBPENNY") == pytest.approx(0.001)
    finally:
        del fk.kalshi_ticks["KX-SUBPENNY"]


def test_venue_tick_reads_per_market_pmus_tick_dict():
    from execute.broker import venue_tick
    import fetch.polymarket_us as fp
    fp.polymarket_us_ticks["pmus-subpenny"] = 0.001
    try:
        assert venue_tick("polymarket_us", "pmus-subpenny") == pytest.approx(0.001)
    finally:
        del fp.polymarket_us_ticks["pmus-subpenny"]


def test_venue_tick_guards_against_a_corrupt_nonpositive_cached_tick():
    # Belt-and-suspenders: even if a bad (0/negative/non-numeric) tick got cached, venue_tick
    # must re-fall-back rather than hand a zero-divide risk to a caller's price floor.
    from execute.broker import venue_tick
    import fetch.kalshi as fk
    import fetch.polymarket_us as fp
    fk.kalshi_ticks["KX-ZERO"] = 0.0
    fp.polymarket_us_ticks["pmus-neg"] = -0.001
    try:
        assert venue_tick("kalshi", "KX-ZERO") == 0.01
        assert venue_tick("polymarket_us", "pmus-neg") == 0.005
    finally:
        del fk.kalshi_ticks["KX-ZERO"]
        del fp.polymarket_us_ticks["pmus-neg"]


# ── Ambiguous Kalshi POST classification (transport failure after send) ────────

def _kalshi_ready(monkeypatch, ticker="KX-AMB"):
    from fetch.kalshi import kalshi_sides
    kalshi_sides[ticker] = "Rangers"
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    return bk


def test_kalshi_place_timeout_unresolved_is_ambiguous(monkeypatch):
    # A transport failure with no HTTP response (timeout/reset) may have landed at the
    # venue: never report it as a clean ERROR(filled=0) — the engine would blind-retry
    # a full-size IOC and then unwind against a possibly-filled hedge.
    bk = _kalshi_ready(monkeypatch)

    def _boom(method, path, body_obj=None):
        raise RuntimeError("read timed out")   # no .response anywhere -> ambiguous class

    monkeypatch.setattr(bk, "_request", _boom)
    r = bk.place_order(_req(platform="kalshi", market_id="KX-AMB", side="Rangers", price=0.51, qty=1))
    assert r.status is OrderStatus.AMBIGUOUS
    assert "unresolved" in r.reason


def test_kalshi_place_4xx_rejection_stays_plain_error(monkeypatch):
    # A definite venue rejection (4xx response) means the order was NOT placed — the safe,
    # retryable ERROR contract must be preserved (no false AMBIGUOUS freezes).
    bk = _kalshi_ready(monkeypatch)

    class _Resp:
        status_code = 400
        text = "insufficient balance"

    class _HTTPError(Exception):
        response = _Resp()

    def _boom(method, path, body_obj=None):
        raise _HTTPError("400 bad request")

    monkeypatch.setattr(bk, "_request", _boom)
    r = bk.place_order(_req(platform="kalshi", market_id="KX-AMB", side="Rangers", price=0.51, qty=1))
    assert r.status is OrderStatus.ERROR
    assert "insufficient balance" in r.reason


def test_kalshi_place_ambiguous_resolves_terminal_unfilled_to_rejected(monkeypatch):
    # Client-id lookup finds the order terminal with zero fills -> downgrade to the safe
    # REJECTED (the engine may retry) instead of freezing a healthy hedge ladder.
    bk = _kalshi_ready(monkeypatch)
    seen = {}

    def _fake(method, path, body_obj=None):
        if method == "POST":
            seen["coid"] = body_obj["client_order_id"]
            raise RuntimeError("connection dropped mid-response")
        return {"orders": [{"client_order_id": seen["coid"], "order_id": "K-77",
                            "status": "canceled", "fill_count_fp": "0.00"}]}

    monkeypatch.setattr(bk, "_request", _fake)
    r = bk.place_order(_req(platform="kalshi", market_id="KX-AMB", side="Rangers", price=0.51, qty=1))
    assert r.status is OrderStatus.REJECTED and r.order_id == "K-77"
    assert r.filled_qty == 0


def test_kalshi_place_ambiguous_with_venue_fills_stays_ambiguous(monkeypatch):
    # Lookup finds the order WITH fills -> stays AMBIGUOUS (order id + fill info in the
    # reason) so the engine freezes loud and a human verifies before flattening anything.
    bk = _kalshi_ready(monkeypatch)
    seen = {}

    def _fake(method, path, body_obj=None):
        if method == "POST":
            seen["coid"] = body_obj["client_order_id"]
            raise RuntimeError("connection dropped mid-response")
        return {"orders": [{"client_order_id": seen["coid"], "order_id": "K-78",
                            "status": "executed", "fill_count_fp": "1.00"}]}

    monkeypatch.setattr(bk, "_request", _fake)
    r = bk.place_order(_req(platform="kalshi", market_id="KX-AMB", side="Rangers", price=0.51, qty=1))
    assert r.status is OrderStatus.AMBIGUOUS and r.order_id == "K-78"
    assert "fills=1" in r.reason and "verify" in r.reason.lower()
