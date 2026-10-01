"""
Close an open Kalshi position by SELLING it back through the broker's unwind (opposite-side
marketable IOC; Kalshi confirms the fill synchronously). Real money. Use to flatten a stranded
leg. Needs the Kalshi TICKER and the side you hold: a team name for moneyline, "Over"/"Under"
for totals, "Cover"/"No Cover" for spreads.
Run:  .venv/bin/python -m tools.close_kalshi_position <ticker> <side> [qty]
e.g.  .venv/bin/python -m tools.close_kalshi_position KXMLBGAME-26JUN221940LADMIN-LAD Dodgers 1
"""

import json
import sys

import config
from execute.broker import OrderResult, OrderStatus
from execute.kalshi_broker import KalshiBroker
from fetch.kalshi import fetch_kalshi_all, kalshi_sides


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python -m tools.close_kalshi_position <ticker> <side> [qty]")
        return 1
    ticker, side = sys.argv[1], sys.argv[2]
    qty = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if not config.LIVE_TRADING_ENABLED:
        print("LIVE_TRADING_ENABLED is not true — set it in .env first.")
        return 1
    bk = KalshiBroker()
    if not bk.configured:
        print("Kalshi broker not configured — check .env credentials.")
        return 1

    fetch_kalshi_all(config.ACTIVE_LEAGUES, config.BET_TYPES)   # populate kalshi_sides for orientation
    if ticker not in kalshi_sides:
        print(f"WARNING: {ticker} not in the current feed — its sides can't be resolved (game may have "
              "started/ended). The unwind will refuse and send nothing; close it in the Kalshi app.")

    fill = OrderResult(OrderStatus.FILLED, "kalshi", ticker, filled_qty=qty, avg_price=0.0)
    leg = {"platform": "kalshi", "market_id": ticker, "team": side}
    print(f"Close {qty} contract(s) of '{side}' in {ticker} (SELL back, marketable IOC)?")
    if input("Proceed (yes/no): ").strip().lower() != "yes":
        print("aborted — nothing sent.")
        return 0
    r = bk.unwind(fill, leg)
    print(f"\nUNWIND -> {r.status.value} | closed {r.filled_qty}/{qty} | {r.reason}")
    if r.raw:
        print("RAW:", json.dumps(r.raw)[:900])
    if r.ok:
        print("CLOSED — confirm the position is gone in the app.")
    else:
        print("NOT CONFIRMED CLOSED — verify in the app; close manually if it persists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
