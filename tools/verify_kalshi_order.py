"""
Diagnostic — verify the Kalshi ORDER (write) path with a guaranteed NO-FILL order
(no money). Places a 1-contract FILL_OR_KILL bid at $0.01 on a real open market whose
YES ask is well above $0.01: FOK + non-marketable => it fills 0 and auto-cancels, so
nothing rests and no money moves. A 201 with fill_count 0 confirms signing + endpoint
+ order body. Run:  .venv/bin/python -m tools.verify_kalshi_order
"""

import sys

import httpx

from execute.kalshi_broker import KalshiBroker
from fetch.kalshi import fetch_markets


def main() -> int:
    bk = KalshiBroker()
    if not bk.configured:
        print("Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env first.")
        return 1
    ticker = None
    with httpx.Client(timeout=10.0) as c:
        for series in ("KXMLBGAME", "KXWNBAGAME", "KXNBAGAME", "KXNHLGAME"):
            for m in fetch_markets(series, c):
                try:
                    ya = float(m.get("yes_ask_dollars"))
                except (TypeError, ValueError):
                    continue
                if m.get("ticker") and ya > 0.05:   # a $0.01 bid cannot cross -> guaranteed no fill
                    ticker = m["ticker"]
                    break
            if ticker:
                break
    if not ticker:
        print("No open Kalshi market found right now — try again later.")
        return 1

    import time
    body = {"ticker": ticker, "side": "bid", "count": "1.00", "price": "0.0100",
            "time_in_force": "fill_or_kill", "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": f"verify-{int(time.time() * 1000)}"}
    print(f"ticker {ticker}: FOK bid 1 @ $0.01 (cannot fill -> auto-cancels; no rest, no money)")
    try:
        data = bk._request("POST", "/portfolio/events/orders", body)
        print("ACCEPTED. order_id:", data.get("order_id"), "| fill_count:", data.get("fill_count"), "(expect 0)")
        print("=> signing + endpoint + order body all correct.")
    except Exception as e:
        body_txt = getattr(getattr(e, "response", None), "text", "")
        if "fill_or_kill_insufficient_resting_volume" in body_txt:
            print("VERIFIED: order accepted + processed; FOK correctly killed (no volume at $0.01, no money).")
            print("=> signing + endpoint + order body all correct. Kalshi write path is GO.")
            return 0
        print(f"FAILED: {type(e).__name__}: {e}")
        if body_txt:
            print("response body:", body_txt[:1200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
