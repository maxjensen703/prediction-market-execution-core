"""
stream/ — continuously-fresh order books over authenticated WebSockets.

Built shadow-first: the WS clients maintain live
in-memory books in a shared BookStore; manager.run_shadow() logs how well the streamed
top-of-book agrees with the REST quote we already trust, BEFORE anything fires from it.
Only once agreement is proven do we point the scan/last-look at the store.

  book_store.py        — thread-safe {(venue, market_id): LiveBook}; one writer per venue, many readers
  kalshi_ws.py         — Kalshi orderbook_delta stream + the yes/no -> ask derivation (unit-tested)
  polymarket_us_ws.py  — PM US MARKET_DATA stream + bids/offers -> ask derivation
  manager.py           — wires clients to the store, runs the shadow comparison + CLI
"""
