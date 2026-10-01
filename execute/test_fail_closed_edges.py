"""The four money edges that fail closed, plus the LIVE_TRADING_ENABLED gate (offline, HTTP mocked).

1. Kalshi side matching: a side_label that is not exactly the YES or the NO label is refused.
2. Kalshi fill counts on place_order / unwind are read from the current response shape
   (fixed-point `fill_count_fp`, flat or wrapped in "order"), so a real fill is never REJECTED.
3. Kalshi GTC / post-only orders accepted and not fully filled are RESTING (or PARTIAL), not
   REJECTED; IOC / FOK shortfalls stay REJECTED.
4. Polymarket US: an exception after the POST may have been sent is AMBIGUOUS, not ERROR.
5. Live brokers refuse place_order and unwind unless config.LIVE_TRADING_ENABLED is true;
   PaperBroker is unaffected.
"""

import json
from pathlib import Path

import pytest

import fetch.kalshi as fk
from execute.broker import OrderRequest, OrderResult, OrderStatus
from execute.kalshi_broker import KalshiBroker
from execute.paper_broker import PaperBroker
from execute.polymarket_us_broker import PolymarketUSBroker

ROOT = Path(__file__).resolve().parent.parent
MLB_PAGE = ROOT / "tests" / "fixtures" / "kalshi_markets_list" / "KXMLBGAME_real_2026-09-16.json"

TICKER = "KX-EDGE"


def _boom(*a, **k):
    raise AssertionError("nothing may be sent to the venue")


@pytest.fixture
def gate_open(monkeypatch):
    monkeypatch.setattr("config.LIVE_TRADING_ENABLED", True)


@pytest.fixture
def gate_closed(monkeypatch):
    monkeypatch.setattr("config.LIVE_TRADING_ENABLED", False)


@pytest.fixture
def kalshi(monkeypatch):
    """A 'configured' KalshiBroker with TICKER registered: YES = Rangers, NO = Red Sox."""
    monkeypatch.setitem(fk.kalshi_sides, TICKER, "Rangers")
    monkeypatch.setitem(fk.kalshi_no_sides, TICKER, "Red Sox")
    bk = KalshiBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    return bk


@pytest.fixture
def pmus(monkeypatch):
    bk = PolymarketUSBroker()
    monkeypatch.setattr(type(bk), "configured", property(lambda s: True))
    monkeypatch.setattr(bk, "resolve_side", lambda m, s: {"long": True, "side_id": "1"})
    return bk


def _kreq(side="Rangers", price=0.51, qty=1, tif="ioc", post_only=False):
    return OrderRequest("kalshi", TICKER, side, price=price, quantity=qty, tif=tif,
                        client_order_id="edge-1", post_only=post_only)


def _post_returns(monkeypatch, bk, response, sent=None):
    """Mock the Kalshi transport: POST returns `response`, recording the body in `sent`."""
    def _fake(method, path, body_obj=None):
        if method != "POST":
            raise AssertionError(f"unexpected {method} {path}")
        if sent is not None:
            sent.append(body_obj)
        return response
    monkeypatch.setattr(bk, "_request", _fake)


# ── 1. Kalshi side matching fails closed ─────────────────────────────────────

@pytest.mark.parametrize("label", ["Ranger", "rangers", "Red Sox.", "Mets", ""])
def test_kalshi_inexact_side_label_errors_and_sends_nothing(gate_open, kalshi, monkeypatch, label):
    monkeypatch.setattr(kalshi, "_request", _boom)
    r = kalshi.place_order(_kreq(side=label))
    assert r.status is OrderStatus.ERROR
    assert repr(label) in r.reason and "'Rangers'" in r.reason and "'Red Sox'" in r.reason


def test_kalshi_exact_yes_and_no_labels_still_place(gate_open, kalshi, monkeypatch):
    sent = []
    _post_returns(monkeypatch, kalshi, {"order_id": "K1", "fill_count_fp": "1.00",
                                        "average_fill_price": "0.5100"}, sent)
    yes = kalshi.place_order(_kreq(side="Rangers", price=0.51))
    assert yes.status is OrderStatus.FILLED and sent[-1]["side"] == "bid"
    assert sent[-1]["price"] == "0.5100"
    no = kalshi.place_order(_kreq(side=" Red Sox ", price=0.49))   # strip() tolerated
    assert no.status is OrderStatus.FILLED and sent[-1]["side"] == "ask"
    assert sent[-1]["price"] == "0.5100"                           # 1 - 0.49 in YES terms
    assert no.avg_price == pytest.approx(0.49)                     # outcome terms back


def test_kalshi_unwind_with_unknown_team_sends_nothing(gate_open, kalshi, monkeypatch):
    monkeypatch.setattr(kalshi, "_request", _boom)
    fill = OrderResult(OrderStatus.FILLED, "kalshi", TICKER, filled_qty=1, avg_price=0.5)
    r = kalshi.unwind(fill, {"team": "Rangerz"})
    assert r.status is OrderStatus.ERROR and "flatten manually" in r.reason


def test_kalshi_quote_with_unknown_label_is_none(kalshi, monkeypatch):
    monkeypatch.setattr(kalshi, "_request", lambda m, p, b=None: {
        "market": {"yes_ask_dollars": "0.40", "no_ask_dollars": "0.62"}})
    assert kalshi.quote(TICKER, "Mets") is None
    assert kalshi.quote(TICKER, "Rangers") == (0.4, 0.0)
    assert kalshi.quote(TICKER, "Red Sox") == (0.62, 0.0)


def test_fetch_registers_the_no_label_for_every_kalshi_market_type(monkeypatch):
    monkeypatch.setattr(fk, "kalshi_sides", dict(fk.kalshi_sides))
    monkeypatch.setattr(fk, "kalshi_no_sides", {})
    page = json.loads(MLB_PAGE.read_text())["markets"]
    lines = [ln for ln in (fk.kalshi_market_to_line(m, "mlb") for m in page) if ln]
    assert lines
    for ln in lines:
        pair = {fk.kalshi_sides[ln.market_id], fk.kalshi_no_sides[ln.market_id]}
        assert pair == {ln.team_a, ln.team_b}                     # NO = the other team

    key = "26JUN121940LADCWS"
    teams = {key: ("Dodgers", "White Sox")}
    base = {"event_ticker": f"KXMLBX-{key}", "yes_ask_dollars": "0.50", "no_ask_dollars": "0.52"}
    total = dict(base, ticker="T-TOT", yes_sub_title="Over 8.5 runs", floor_strike=8.5)
    assert fk.kalshi_market_to_total_line(total, "mlb", teams)
    assert (fk.kalshi_sides["T-TOT"], fk.kalshi_no_sides["T-TOT"]) == ("Over", "Under")
    fav_a = dict(base, ticker="T-SPA", title="Dodgers wins by over 1.5 runs", floor_strike=1.5)
    assert fk.kalshi_market_to_spread_line(fav_a, "mlb", teams)
    assert (fk.kalshi_sides["T-SPA"], fk.kalshi_no_sides["T-SPA"]) == ("Cover", "No Cover")
    fav_b = dict(base, ticker="T-SPB", title="White Sox wins by over 1.5 runs", floor_strike=1.5)
    assert fk.kalshi_market_to_spread_line(fav_b, "mlb", teams)
    assert (fk.kalshi_sides["T-SPB"], fk.kalshi_no_sides["T-SPB"]) == ("No Cover", "Cover")
    wc_teams = {key: ("Brazil", "France")}
    wc_total = dict(base, ticker="T-WCT", yes_sub_title="Over 2.5 goals", floor_strike=2.5)
    assert fk.kalshi_wc_total_line(wc_total, wc_teams, "full")
    assert (fk.kalshi_sides["T-WCT"], fk.kalshi_no_sides["T-WCT"]) == ("Over", "Under")


# ── 2. Kalshi fills read from the current response shape ────────────────────

@pytest.mark.parametrize("response", [
    {"order": {"order_id": "K2", "status": "executed", "fill_count_fp": "2.00",
               "average_fill_price": "0.5100"}},                       # wrapped, fixed-point
    {"order_id": "K2", "status": "executed", "fill_count_fp": "2.00",
     "average_fill_price": "0.5100"},                                  # flat, fixed-point
    {"order_id": "K2", "fill_count": 2, "average_fill_price": "0.5100"},   # legacy integer
])
def test_kalshi_place_real_fill_in_current_shape_is_filled(gate_open, kalshi, monkeypatch, response):
    _post_returns(monkeypatch, kalshi, response)
    r = kalshi.place_order(_kreq(qty=2))
    assert r.status is OrderStatus.FILLED and r.filled_qty == 2 and r.order_id == "K2"
    assert r.avg_price == pytest.approx(0.51)


def test_kalshi_unwind_reads_fixed_point_fill(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K3", "fill_count_fp": "1.00",
                                                  "average_fill_price": "0.6000"}})
    fill = OrderResult(OrderStatus.FILLED, "kalshi", TICKER, filled_qty=1, avg_price=0.5)
    r = kalshi.unwind(fill, {"team": "Rangers"})
    assert r.status is OrderStatus.FILLED and r.filled_qty == 1
    assert r.avg_price == pytest.approx(0.60)


# ── 3. Kalshi GTC / post-only acceptance is RESTING, IOC / FOK shortfalls REJECTED ──

def test_kalshi_gtc_accepted_unfilled_is_resting(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K4", "status": "resting",
                                                  "fill_count_fp": "0.00"}})
    r = kalshi.place_order(_kreq(qty=2, tif="gtc"))
    assert r.status is OrderStatus.RESTING and r.order_id == "K4" and r.filled_qty == 0


def test_kalshi_post_only_accepted_unfilled_is_resting(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order_id": "K5", "status": "resting", "fill_count_fp": "0.00"})
    r = kalshi.place_order(_kreq(qty=1, tif="gtc", post_only=True))
    assert r.status is OrderStatus.RESTING


def test_kalshi_gtc_partly_filled_is_partial_not_rejected(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K6", "status": "resting",
                                                  "fill_count_fp": "1.00"}})
    r = kalshi.place_order(_kreq(qty=3, tif="gtc"))
    assert r.status is OrderStatus.PARTIAL and r.filled_qty == 1


def test_kalshi_gtc_reported_canceled_by_venue_is_rejected(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K7", "status": "canceled",
                                                  "fill_count_fp": "0.00"}})
    r = kalshi.place_order(_kreq(qty=1, tif="gtc"))
    assert r.status is OrderStatus.REJECTED


def test_kalshi_ioc_partial_fill_stays_rejected(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K8", "status": "canceled",
                                                  "fill_count_fp": "1.00"}})
    r = kalshi.place_order(_kreq(qty=2, tif="ioc"))
    assert r.status is OrderStatus.REJECTED and r.filled_qty == 1


def test_kalshi_fok_unfilled_stays_rejected(gate_open, kalshi, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order": {"order_id": "K9", "status": "canceled",
                                                  "fill_count_fp": "0.00"}})
    r = kalshi.place_order(_kreq(qty=1, tif="fok"))
    assert r.status is OrderStatus.REJECTED and r.filled_qty == 0


# ── 4. Polymarket US: exception after send is AMBIGUOUS ──────────────────────

def _pm_req():
    return OrderRequest("polymarket_us", "aec-x", "Angels", price=0.45, quantity=1, tif="ioc")


def _http_error(code):
    class _Resp:
        status_code = code
        text = f"status {code}"

    class _HTTPError(Exception):
        response = _Resp()
    return _HTTPError(f"{code}")


def test_pm_us_timeout_after_send_is_ambiguous(gate_open, pmus, monkeypatch):
    def _timeout(body):
        raise RuntimeError("read timed out")   # no response: the POST may have landed
    monkeypatch.setattr(pmus, "_post_order", _timeout)
    r = pmus.place_order(_pm_req())
    assert r.status is OrderStatus.AMBIGUOUS and "verify" in r.reason


def test_pm_us_5xx_after_send_is_ambiguous(gate_open, pmus, monkeypatch):
    def _five(body):
        raise _http_error(502)
    monkeypatch.setattr(pmus, "_post_order", _five)
    assert pmus.place_order(_pm_req()).status is OrderStatus.AMBIGUOUS


def test_pm_us_4xx_rejection_stays_plain_error(gate_open, pmus, monkeypatch):
    def _four(body):
        raise _http_error(400)
    monkeypatch.setattr(pmus, "_post_order", _four)
    r = pmus.place_order(_pm_req())
    assert r.status is OrderStatus.ERROR and "status 400" in r.reason


# ── 5. LIVE_TRADING_ENABLED is enforced in the live brokers ──────────────────

def test_kalshi_refuses_place_and_unwind_without_live_flag(gate_closed, kalshi, monkeypatch):
    monkeypatch.setattr(kalshi, "_request", _boom)
    r = kalshi.place_order(_kreq())
    assert r.status is OrderStatus.ERROR and "LIVE_TRADING_ENABLED" in r.reason
    fill = OrderResult(OrderStatus.FILLED, "kalshi", TICKER, filled_qty=1, avg_price=0.5)
    u = kalshi.unwind(fill, {"team": "Rangers"})
    assert u.status is OrderStatus.ERROR and "LIVE_TRADING_ENABLED" in u.reason


def test_pm_us_refuses_place_and_unwind_without_live_flag(gate_closed, pmus, monkeypatch):
    monkeypatch.setattr(pmus, "_post_order", _boom)
    monkeypatch.setattr(pmus, "_request", _boom)
    r = pmus.place_order(_pm_req())
    assert r.status is OrderStatus.ERROR and "LIVE_TRADING_ENABLED" in r.reason
    fill = OrderResult(OrderStatus.FILLED, "polymarket_us", "aec-x", filled_qty=1, avg_price=0.45)
    u = pmus.unwind(fill, {"team": "Angels"})
    assert u.status is OrderStatus.ERROR and "LIVE_TRADING_ENABLED" in u.reason


def test_live_brokers_place_with_live_flag(gate_open, kalshi, pmus, monkeypatch):
    _post_returns(monkeypatch, kalshi, {"order_id": "K10", "fill_count_fp": "1.00"})
    assert kalshi.place_order(_kreq()).status is OrderStatus.FILLED
    monkeypatch.setattr("execute.polymarket_us_broker.PM_US_SYNCHRONOUS_EXECUTION", True)
    monkeypatch.setattr(pmus, "_post_order", lambda body: {"id": "P1", "executions": [{"order": {
        "state": "ORDER_STATE_FILLED", "cumQuantity": 1, "leavesQuantity": 0,
        "avgPx": {"value": "0.45"}, "id": "P1"}}]})
    assert pmus.place_order(_pm_req()).status is OrderStatus.FILLED


def test_paper_broker_ignores_the_live_flag(gate_closed):
    pb = PaperBroker("kalshi")
    r = pb.place_order(_kreq())
    assert r.status is OrderStatus.FILLED
    assert pb.unwind(r).status is OrderStatus.FILLED
