# Changes

## 2026-10-01: public release

The live round trip of 2026-09-16 was made with the original core. Five changes were made
afterward for the public release and are covered by unit tests, not by live trading:

1. Fail-closed side matching: a Kalshi `side_label` that is not exactly the market's YES or NO
   label returns `ERROR` and sends nothing, instead of being treated as the NO side.
2. Fill counts read from the venue's current response shape: Kalshi `place_order()` and
   `unwind()` read the fixed-point `fill_count_fp` (flat or wrapped in `order`), so a real fill
   is no longer reported as `REJECTED`.
3. RESTING for accepted GTC orders: a Kalshi GTC or post-only order accepted and not fully filled
   returns `RESTING` (or `PARTIAL`), not `REJECTED`.
4. AMBIGUOUS after a sent Polymarket US request fails: a timeout, dropped connection or 5xx after
   the POST returns `AMBIGUOUS`, matching the Kalshi broker; a 4xx stays `ERROR`.
5. The LIVE_TRADING_ENABLED gate enforced in the brokers: `KalshiBroker` and
   `PolymarketUSBroker` `place_order()` and `unwind()` refuse without it. `PaperBroker` is
   unaffected.
