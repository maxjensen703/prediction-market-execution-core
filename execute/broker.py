"""
execute/broker.py — the shared order plumbing: the Broker interface every order goes
through, so paper and live execution share identical orchestration — an OrderRequest in,
an OrderResult (a fill, a miss, a resting order, or an explicit AMBIGUOUS) out — plus
venue_tick(), the per-market price-tick resolver the brokers and any caller's price math
read. Concrete brokers: paper_broker.py (simulated, default)
and the live kalshi_broker.py / polymarket_us_broker.py.
"""

import math
from dataclasses import dataclass, field
from enum import Enum


def venue_tick(platform: str, market_id: str) -> float:
    """Per-market price tick (dollars) from the fetch-time tick dicts; falls back to the
    venue default (Kalshi 1c, PM US 0.5c) when the market is unknown. Lazy in-function
    fetch imports keep this module import-cycle-free. Belt-and-suspenders: a resolved
    tick that isn't a finite value > 0 (e.g. a corrupt cached 0) would zero-divide in a
    caller's price floor, so re-fall-back here too."""
    fallback = 0.005 if platform == "polymarket_us" else 0.01
    if platform == "kalshi":
        from fetch.kalshi import kalshi_ticks
        tick = kalshi_ticks.get(market_id, fallback)
    elif platform == "polymarket_us":
        from fetch.polymarket_us import polymarket_us_ticks
        tick = polymarket_us_ticks.get(market_id, fallback)
    else:
        tick = fallback
    return tick if isinstance(tick, (int, float)) and math.isfinite(tick) and tick > 0 else fallback


class OrderStatus(str, Enum):
    FILLED = "filled"       # fully filled (FOK satisfied)
    REJECTED = "rejected"   # venue refused / FOK could not fill
    ERROR = "error"         # not configured / exception
    RESTING = "resting"     # open at venue, unfilled (GTC / post-only)
    PARTIAL = "partial"     # open at venue, partially filled (GTC / post-only)
    CANCELED = "canceled"   # canceled at venue (confirmed, not just requested)
    CANCEL_REQUESTED = "cancel_requested"   # cancel sent; NOT confirmed — confirm via get_order
    AMBIGUOUS = "ambiguous"  # transport failed AFTER the venue may have accepted — the
                             # order can be live/filled. Caller must treat as possibly-live:
                             # never blind-retry, never unwind against it; verify first.


@dataclass
class OrderRequest:
    platform: str              # venue chosen for this leg
    market_id: str             # platform-native market/token id
    side_label: str            # outcome being bought, e.g. "Yankees" / "Over"
    price: float               # limit ask, 0-1 — never pay worse than this
    quantity: int              # number of $1 contracts
    tif: str = "ioc"           # immediate-or-cancel: take what rests now, cancel the rest.
                               # "fok" = all-or-nothing; "gtc" = good-til-canceled (rests).
                               # An IOC/FOK shortfall is REJECTED with filled_qty set; a GTC or
                               # post-only acceptance short of quantity is RESTING / PARTIAL.
    client_order_id: str = ""  # idempotency key
    slip: float | None = None  # caller's per-leg cross budget ($) — PM US market orders need it
    tick: float | None = None  # per-market price tick ($) resolved at build time (live only)
    post_only: bool = False    # maker-only: reject instead of crossing (Kalshi post_only / PM US participateDontInitiate)


@dataclass
class OrderResult:
    status: OrderStatus
    platform: str
    market_id: str
    filled_qty: int = 0
    avg_price: float = 0.0     # realized average ask paid
    cost: float = 0.0          # filled_qty * avg_price (gross, pre-fee)
    fee: float = 0.0           # venue fee charged
    order_id: str = ""         # venue (or paper) order id
    reason: str = ""           # rejection / error detail
    realized_pnl: float = 0.0  # on an UNWIND: side-aware realized round-trip $ P&L (loss = negative, fees in)
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OrderStatus.FILLED


class Broker:
    """Uniform venue interface; subclasses implement the venue specifics."""

    platform = "base"
    is_live = False
    configured = False

    def place_order(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def unwind(self, fill: OrderResult, leg: dict | None = None) -> OrderResult:
        """Best-effort SELL to flatten a leg whose hedge never filled. `leg` is the
        original opportunity leg (platform/market_id/team/ask) when the venue needs
        the bought side to construct the closing order."""
        raise NotImplementedError

    def balance(self) -> float:
        return 0.0

    def positions(self) -> list:
        return []

    # ── resting-order lifecycle (live brokers and paper implement) ──
    def cancel_order(self, order_id: str, market_id: str = "") -> OrderResult:
        raise NotImplementedError

    def get_order(self, order_id: str, market_id: str = "") -> OrderResult:
        raise NotImplementedError

    def open_orders(self, market_id: str | None = None) -> list:
        raise NotImplementedError

    def cancel_all(self, market_ids: list | None = None) -> dict:
        raise NotImplementedError
