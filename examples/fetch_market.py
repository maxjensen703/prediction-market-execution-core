"""Fetch open Kalshi game markets and show the order body the broker WOULD send. Places nothing.

What it does:
  1. Builds a KalshiBroker from environment variables (or ./.env). Credentials are optional:
     the script only reports whether they are present and never prints their values.
  2. Pulls the open markets of one Kalshi winner series over the public, keyless REST API.
  3. Converts each market to a MoneyLine, which also fills the YES-side registry the broker
     needs to orient an order.
  4. For the first market, prints the exact JSON body KalshiBroker.build_order() produces for
     a 1-contract buy of the YES team at its current ask. build_order() is pure: it signs
     nothing and sends nothing. place_order() is never called.

Usage (from the repository root):
    .venv/bin/python examples/fetch_market.py              # KXMLBGAME
    .venv/bin/python examples/fetch_market.py KXNFLGAME
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import config  # noqa: E402  (loads ./.env if present)
from execute.broker import OrderRequest  # noqa: E402
from execute.kalshi_broker import KalshiBroker  # noqa: E402
from fetch.kalshi import SERIES, fetch_markets, kalshi_market_to_line, kalshi_sides  # noqa: E402


def main(argv: list[str]) -> int:
    series = argv[1] if len(argv) > 1 else "KXMLBGAME"
    sport = next((league for league, names in SERIES.items() if series in names), None)
    if sport is None or not series.endswith("GAME"):
        print(f"Unknown or non-winner series '{series}'. Try one of: "
              + ", ".join(names[0] for names in SERIES.values()))
        return 2

    broker = KalshiBroker()   # reads KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_API_BASE
    print(f"Kalshi credentials present: {broker.configured}")
    print(f"LIVE_TRADING_ENABLED: {config.LIVE_TRADING_ENABLED}")

    with httpx.Client(timeout=config.KALSHI_TIMEOUT) as client:
        markets = fetch_markets(series, client)   # public, keyless market-data read
    lines = [line for line in (kalshi_market_to_line(m, sport) for m in markets) if line]
    print(f"{series}: {len(markets)} open markets, {len(lines)} converted to MoneyLines")
    if not lines:
        return 0

    for line in lines[:5]:
        print(f"  {line.market_id}: YES buys {kalshi_sides[line.market_id]} | "
              f"{line.team_a} {line.best_ask_yes:.2f} / {line.team_b} {line.best_ask_no:.2f} | "
              f"{line.start_et}")

    first = lines[0]
    yes_team = kalshi_sides[first.market_id]
    ask = first.best_ask_yes if first.team_a == yes_team else first.best_ask_no
    req = OrderRequest("kalshi", first.market_id, yes_team, price=ask, quantity=1, tif="ioc",
                       client_order_id="example-dry-run")
    print("\nOrder body build_order() would POST (NOT sent):")
    print(json.dumps(broker.build_order(req), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
