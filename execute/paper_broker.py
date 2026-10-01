"""
execute/paper_broker.py — the default Broker (execute/broker.py). Simulates
fill-or-kill fills at the requested ask and charges each venue's modelled taker fee
(theta*p*(1-p) per contract), tracking a virtual per-venue balance and positions; also
supports GTC resting orders (_rest). No network, no real money, and it ignores
LIVE_TRADING_ENABLED.
"""

from config import PLATFORM_FEES
from .broker import Broker, OrderRequest, OrderResult, OrderStatus

STARTING_PAPER_BALANCE = 100_000.0  # virtual $ per venue


def _fee(platform: str, price: float, qty: int) -> float:
    """Real per-trade taker fee for `qty` contracts at `price` (see config)."""
    theta = PLATFORM_FEES.get(platform, {}).get("theta", 0.0)
    return round(theta * price * (1.0 - price) * qty, 4)


class PaperBroker(Broker):
    is_live = False
    configured = True   # paper is always ready

    def __init__(self, platform: str, starting_balance: float = STARTING_PAPER_BALANCE):
        self.platform = platform
        self._balance = float(starting_balance)
        self._positions: list[dict] = []
        self._n = 0
        self.open: dict[str, dict] = {}    # order_id -> resting GTC order state
        self.closed: dict[str, dict] = {}  # order_id -> canceled order state (get_order parity with live)

    def place_order(self, req: OrderRequest) -> OrderResult:
        if req.quantity < 1:
            return OrderResult(OrderStatus.REJECTED, self.platform, req.market_id,
                               reason="quantity below 1 contract")
        if req.tif == "gtc":
            return self._rest(req)
        cost = round(req.quantity * req.price, 2)
        fee = _fee(self.platform, req.price, req.quantity)
        if cost + fee > self._balance:
            return OrderResult(OrderStatus.REJECTED, self.platform, req.market_id,
                               reason="insufficient paper balance")
        self._balance -= cost + fee
        self._n += 1
        oid = f"paper-{self.platform}-{self._n}"
        self._positions.append({"market_id": req.market_id, "side": req.side_label,
                                "qty": req.quantity, "price": req.price, "order_id": oid})
        return OrderResult(OrderStatus.FILLED, self.platform, req.market_id,
                           filled_qty=req.quantity, avg_price=req.price, cost=cost,
                           fee=fee, order_id=oid)

    def _rest(self, req: OrderRequest) -> OrderResult:
        """Book a GTC order as RESTING; never self-fills — a separate fill simulator (yours)
        decides when/if it fills, so paper matches live's cancel-then-place discipline."""
        self._n += 1
        oid = f"paper-{self.platform}-{self._n}"
        self.open[oid] = {"market_id": req.market_id, "side_label": req.side_label,
                          "price": req.price, "quantity": req.quantity, "filled_qty": 0}
        return OrderResult(OrderStatus.RESTING, self.platform, req.market_id,
                           avg_price=req.price, order_id=oid)

    def cancel_order(self, order_id: str, market_id: str = "") -> OrderResult:
        o = self.open.pop(order_id, None)
        if not o:
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason="unknown paper order")
        self.closed[order_id] = o   # keep it queryable: get_order confirms CANCELED (live parity)
        return OrderResult(OrderStatus.CANCELED, self.platform, o["market_id"],
                           filled_qty=o["filled_qty"], avg_price=o["price"], order_id=order_id)

    def get_order(self, order_id: str, market_id: str = "") -> OrderResult:
        o = self.open.get(order_id)
        if not o:
            c = self.closed.get(order_id)
            if c:   # canceled orders stay queryable, matching live PM semantics
                return OrderResult(OrderStatus.CANCELED, self.platform, c["market_id"],
                                   filled_qty=c["filled_qty"], avg_price=c["price"], order_id=order_id)
            return OrderResult(OrderStatus.ERROR, self.platform, market_id, order_id=order_id,
                               reason="unknown paper order")
        status = OrderStatus.PARTIAL if o["filled_qty"] else OrderStatus.RESTING
        return OrderResult(status, self.platform, o["market_id"], filled_qty=o["filled_qty"],
                           avg_price=o["price"], order_id=order_id)

    def open_orders(self, market_id: str | None = None) -> list:
        return [self.get_order(oid) for oid, o in self.open.items()
                if market_id is None or o["market_id"] == market_id]

    def cancel_all(self, market_ids: list | None = None) -> dict:
        ids = [oid for oid, o in self.open.items()
              if market_ids is None or o["market_id"] in market_ids]
        for oid in ids:
            o = self.open.pop(oid, None)
            if o:
                self.closed[oid] = o   # stay queryable as CANCELED (live parity)
        return {"canceled": len(ids)}

    def unwind(self, fill: OrderResult, leg: dict | None = None) -> OrderResult:
        """Sell the stranded leg back at its fill price (paper); refund net of fee."""
        refund = round(fill.filled_qty * fill.avg_price, 2)
        fee = _fee(self.platform, fill.avg_price, fill.filled_qty)
        self._balance += refund - fee
        return OrderResult(OrderStatus.FILLED, self.platform, fill.market_id,
                           filled_qty=fill.filled_qty, avg_price=fill.avg_price,
                           cost=-refund, fee=fee, order_id=fill.order_id + "-unwind",
                           reason="unwound")

    def balance(self) -> float:
        return round(self._balance, 2)

    def positions(self) -> list:
        return list(self._positions)
