"""PaperBroker: fills decrement balance by cost+fee, fees match theta*p*(1-p),
insufficient balance rejects, and unwind refunds. No network."""

from config import PLATFORM_FEES
from execute.broker import OrderRequest, OrderStatus
from execute.paper_broker import PaperBroker


def _req(platform="kalshi", price=0.40, qty=100, market_id="m1", side="Yankees"):
    return OrderRequest(platform=platform, market_id=market_id, side_label=side, price=price, quantity=qty)


def test_fill_decrements_balance_by_cost_plus_fee():
    bk = PaperBroker("kalshi", starting_balance=1000.0)
    r = bk.place_order(_req(price=0.40, qty=100))
    assert r.status is OrderStatus.FILLED and r.ok
    expected_fee = round(PLATFORM_FEES["kalshi"]["theta"] * 0.40 * 0.60 * 100, 4)
    assert r.cost == 40.0 and r.fee == expected_fee
    assert bk.balance() == round(1000.0 - 40.0 - expected_fee, 2)
    assert len(bk.positions()) == 1


def test_insufficient_balance_rejects():
    bk = PaperBroker("kalshi", starting_balance=10.0)
    r = bk.place_order(_req(price=0.40, qty=100))
    assert r.status is OrderStatus.REJECTED and not r.ok
    assert bk.balance() == 10.0   # untouched


def test_quantity_below_one_rejects():
    bk = PaperBroker("kalshi")
    assert bk.place_order(_req(qty=0)).status is OrderStatus.REJECTED


def test_unwind_refunds_net_of_fee():
    bk = PaperBroker("polymarket", starting_balance=1000.0)
    r = bk.place_order(_req(platform="polymarket", price=0.50, qty=100))
    after_buy = bk.balance()
    u = bk.unwind(r)
    assert u.ok and bk.balance() > after_buy   # got the notional back minus a small fee


# ── GTC resting orders ──────────────────────────────────────────────────────────

def test_gtc_order_rests_never_self_fills():
    bk = PaperBroker("kalshi", starting_balance=1000.0)
    req = _req(price=0.40, qty=10)
    req.tif = "gtc"
    r = bk.place_order(req)
    assert r.status is OrderStatus.RESTING and not r.ok
    assert r.order_id in bk.open
    assert bk.balance() == 1000.0   # resting never charges balance (no self-fill)


def test_gtc_cancel_removes_from_open_and_reports_canceled():
    bk = PaperBroker("kalshi")
    req = _req(price=0.40, qty=10)
    req.tif = "gtc"
    r = bk.place_order(req)
    c = bk.cancel_order(r.order_id)
    assert c.status is OrderStatus.CANCELED
    assert r.order_id not in bk.open


def test_gtc_get_order_reports_resting():
    bk = PaperBroker("kalshi")
    req = _req(price=0.40, qty=10)
    req.tif = "gtc"
    r = bk.place_order(req)
    g = bk.get_order(r.order_id)
    assert g.status is OrderStatus.RESTING and g.avg_price == 0.40


def test_gtc_rest_cancel_then_get_order_reports_canceled():
    # MAJOR-6 parity with live PM: a canceled order stays queryable as CANCELED, not an error.
    bk = PaperBroker("kalshi")
    req = _req(price=0.40, qty=10)
    req.tif = "gtc"
    r = bk.place_order(req)
    assert r.status is OrderStatus.RESTING
    c = bk.cancel_order(r.order_id)
    assert c.status is OrderStatus.CANCELED
    g = bk.get_order(r.order_id)
    assert g.status is OrderStatus.CANCELED and g.avg_price == 0.40
    assert g.market_id == "m1"


def test_gtc_cancel_all_orders_stay_queryable_as_canceled():
    bk = PaperBroker("kalshi")
    req = _req(price=0.40, qty=10)
    req.tif = "gtc"
    r = bk.place_order(req)
    assert bk.cancel_all()["canceled"] == 1
    assert bk.get_order(r.order_id).status is OrderStatus.CANCELED


def test_gtc_get_order_unknown_id_errors():
    bk = PaperBroker("kalshi")
    g = bk.get_order("nope")
    assert g.status is OrderStatus.ERROR


def test_gtc_open_orders_lists_and_filters_by_market():
    bk = PaperBroker("kalshi")
    r1 = _req(price=0.40, qty=10, market_id="m1")
    r1.tif = "gtc"
    r2 = _req(price=0.30, qty=5, market_id="m2")
    r2.tif = "gtc"
    bk.place_order(r1)
    bk.place_order(r2)
    assert len(bk.open_orders()) == 2
    assert len(bk.open_orders("m1")) == 1


def test_gtc_cancel_all_clears_open():
    bk = PaperBroker("kalshi")
    r1 = _req(price=0.40, qty=10, market_id="m1")
    r1.tif = "gtc"
    bk.place_order(r1)
    result = bk.cancel_all()
    assert result["canceled"] == 1 and bk.open_orders() == []


def test_ioc_behavior_byte_identical_to_before_gtc_change():
    # Regression: default (non-gtc) fills instantly, exactly as before GTC support.
    bk = PaperBroker("kalshi", starting_balance=1000.0)
    r = bk.place_order(_req(price=0.40, qty=100))
    assert r.status is OrderStatus.FILLED and r.ok
    assert bk.open == {}   # never touches the resting-order dict
