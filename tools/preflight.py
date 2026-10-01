"""
tools/preflight.py — run this BEFORE arming a live test. It prints the CURRENT effective
config (read live from config.py, so it's always up to date) and confirms BOTH venues are
actually returning data + creds are present, so you never arm into a broken or empty feed.
Read-only: places no orders, touches no money.

Run:  .venv/bin/python -m tools.preflight
"""

import sys

import httpx

import config as c
from execute.kalshi_broker import KalshiBroker
from execute.polymarket_us_broker import PolymarketUSBroker
from fetch import kalshi as k
from fetch.polymarket_us import fetch_events, fetch_polymarket_us_all


def main() -> int:
    rl = c.RISK_LIMITS
    print("=== CONFIG (live from config.py) ===")
    print(f"  leagues={c.ACTIVE_LEAGUES}  bet_types={c.ACTIVE_BET_TYPES}  window_days={c.WINDOW_DAYS}")
    print(f"  maker: MAKER_ENABLED={c.MAKER_ENABLED}  mode={c.MAKER_MODE}  contracts={c.MAKER_CONTRACTS}  "
          f"universe_n={c.MAKER_UNIVERSE_SIZE}  margins={c.MAKER_PREGAME_MARGIN_CENTS}/{c.MAKER_INPLAY_MARGIN_CENTS} "
          f"(cap {c.MAKER_MAX_MARGIN_CENTS})  hedge_cross_cap={c.SLIPPAGE_CAP_CENTS.get('kalshi')}")
    print(f"  risk:  max_exposure=${rl['max_total_exposure']}  max_resting=${rl['max_resting_exposure']}  "
          f"max_open_orders={rl['max_open_maker_orders']}  max_per_trade=${rl['max_capital_per_trade']}  "
          f"STREAM_ENABLED={c.STREAM_ENABLED}")

    print("\n=== LIVE GATES ===")
    kb, pb = KalshiBroker(), PolymarketUSBroker()
    print(f"  LIVE_TRADING_ENABLED={c.LIVE_TRADING_ENABLED}  kalshi_creds={kb.configured}  pmus_creds={pb.configured}")

    print("\n=== VENUE FEED CHECK (both venues must have data to find arbs) ===")
    lgs = [l for l in c.ACTIVE_LEAGUES if l != "wc"]
    bts = list(c.ACTIVE_BET_TYPES)
    feeds_ok = True

    try:
        kml, ktot, kspr = k.fetch_kalshi_all(lgs, bts)
        ktot_n = len(kml) + len(ktot) + len(kspr)
        print(f"  kalshi:        ml={len(kml)} tot={len(ktot)} spr={len(kspr)}")
        feeds_ok &= ktot_n > 0
    except Exception as e:
        print(f"  kalshi: ERROR {type(e).__name__}: {e}"); feeds_ok = False

    try:
        with httpx.Client(timeout=c.PM_US_TIMEOUT) as cl:
            pmus_events = sum(len(fetch_events(lg, cl)) for lg in lgs)
        pml, ptot, pspr = fetch_polymarket_us_all(lgs, bts)
        pm_n = len(pml) + len(ptot) + len(pspr)
        print(f"  polymarket_us: ml={len(pml)} tot={len(ptot)} spr={len(pspr)}  (gateway events={pmus_events})")
        if pm_n == 0:
            feeds_ok = False
            if pmus_events == 0:
                print("  ^^ PM US has NO markets posted yet for these leagues. This is TIMING (PM US posts")
                print("     markets nearer game time, later than Kalshi) — NOT your network, NOT a bug.")
                print("     Wait and re-run preflight; arm once PM US shows lines.")
            else:
                print("  ^^ PM US events exist but 0 lines converted -> a SCHEMA change broke parsing.")
                print("     Not your network. Tell Claude to fix the converter before arming.")
    except Exception as e:
        print(f"  polymarket_us: ERROR {type(e).__name__}: {e}"); feeds_ok = False

    ready = c.LIVE_TRADING_ENABLED and kb.configured and pb.configured and feeds_ok
    print("\n" + ("GO  — both feeds live, creds present, live armed-able." if ready
                  else "NOT READY — fix the flagged item(s) above before arming."))
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
