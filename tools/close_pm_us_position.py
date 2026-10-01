"""
Close an open Polymarket US position by SELLING it back through the broker's unwind
(tick-valid marketable IOC, then polls the order id to CONFIRM it actually closed).
Real money. Use to flatten a stranded leg / test position.
Run:  .venv/bin/python -m tools.close_pm_us_position <market_slug> <team> [qty]
e.g.  .venv/bin/python -m tools.close_pm_us_position aec-mlb-az-stl-2026-06-22 Cardinals 1
"""

import json
import sys

import config
from core.teams import normalize_team_name
from execute.broker import OrderResult, OrderStatus
from execute.polymarket_us_broker import PolymarketUSBroker
from fetch.polymarket_us import fetch_polymarket_us_lines, polymarket_us_sides


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python -m tools.close_pm_us_position <market_slug> <team> [qty]")
        return 1
    slug, team_in = sys.argv[1], sys.argv[2]
    qty = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if not config.LIVE_TRADING_ENABLED:
        print("LIVE_TRADING_ENABLED is not true — set it in .env first.")
        return 1
    bk = PolymarketUSBroker()
    if not bk.configured:
        print("PM US broker not configured — check .env credentials.")
        return 1

    fetch_polymarket_us_lines(config.ACTIVE_LEAGUES)   # populate side roles for resolve_side
    team = team_in
    for cand in (team_in, normalize_team_name(team_in, "mlb")):
        if cand in polymarket_us_sides.get(slug, {}):
            team = cand
            break
    if slug not in polymarket_us_sides or team not in polymarket_us_sides.get(slug, {}):
        print(f"WARNING: side for '{team}' on {slug} not in the current feed — the game may have started/ended.")
        print("If unwind reports 'needs the original leg/side', close it in the app instead.")

    fill = OrderResult(OrderStatus.FILLED, "polymarket_us", slug, filled_qty=qty, avg_price=0.0)
    leg = {"platform": "polymarket_us", "market_id": slug, "team": team}
    print(f"Close {qty} contract(s) of '{team}' in {slug} (SELL back, marketable IOC)?")
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
