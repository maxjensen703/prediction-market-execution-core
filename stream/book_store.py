"""
stream/book_store.py — live books. Thread-safe live order-book
store: one LiveBook per (venue, market_id), written by the venue WS clients and read by the
scanner / last-look. Both venues normalize into the same shape — the ask to BUY each of the
two sides ('yes'/'no'), depth behind each, and the wall-clock time of the last update so
readers can reject a book that's gone stale. `healthy` here is the earned-trust flag
stream/manager.py's self-audit promotes/demotes (see that file's header for the narrative).
"""

import threading
import time
from dataclasses import dataclass


@dataclass
class LiveBook:
    venue: str
    market_id: str
    ask_yes: float | None = None   # $ to BUY side 1 (kalshi YES / PM US long) right now
    ask_no: float | None = None    # $ to BUY side 2 (kalshi NO  / PM US short)
    depth_yes: float = 0.0         # size resting behind ask_yes (kalshi contracts / PM US $)
    depth_no: float = 0.0
    updated_at: float = 0.0        # epoch seconds of the last update (0 = never, WALL clock)
    seq: int = 0                   # venue sequence number (gap detection)
    venue_ts: float | None = None  # venue-reported epoch seconds (None if the venue sent none)
    levels_yes: list | None = None  # top-N (price, size) asks to BUY yes, best first (observational)
    levels_no: list | None = None   # top-N (price, size) asks to BUY no, best first

    def age(self, now: float | None = None) -> float | None:
        return (now if now is not None else time.time()) - self.updated_at if self.updated_at else None

    def fresh(self, max_age: float, now: float | None = None) -> bool:
        a = self.age(now)
        return a is not None and a <= max_age

    def venue_age(self, now: float | None = None) -> float | None:
        if self.venue_ts is None:
            return None
        return (now if now is not None else time.time()) - self.venue_ts


class BookStore:
    def __init__(self):
        self._books: dict[tuple[str, str], LiveBook] = {}
        self._lock = threading.RLock()
        self.healthy = False           # WS is UNTRUSTED until the live self-audit confirms it matches REST
                                       # (start on the proven REST path; earn WS trust, don't assume it)
        self.audit: dict = {}          # last self-audit result (for /api/trading/status)

    def set_health(self, healthy: bool, audit: dict | None = None) -> None:
        with self._lock:
            self.healthy = bool(healthy)
            if audit is not None:
                self.audit = audit

    def update(self, venue: str, market_id: str, *, ask_yes=None, ask_no=None,
               depth_yes=0.0, depth_no=0.0, seq: int = 0, now: float | None = None,
               venue_ts: float | None = None, levels_yes: list | None = None,
               levels_no: list | None = None) -> None:
        """Replace the top-of-book for one market with the latest derived quote (stamped now)."""
        with self._lock:
            self._books[(venue, market_id)] = LiveBook(
                venue=venue, market_id=market_id, ask_yes=ask_yes, ask_no=ask_no,
                depth_yes=depth_yes, depth_no=depth_no, seq=seq,
                updated_at=now if now is not None else time.time(),
                venue_ts=venue_ts, levels_yes=levels_yes, levels_no=levels_no)

    def touch_venue_ts(self, venue: str, market_id: str, venue_ts: float | None) -> None:
        """Update venue_ts ONLY — updated_at (wall-clock freshness) is deliberately untouched,
        so a ticker frame can never make a quiet ladder look fresh."""
        if venue_ts is None:
            return
        with self._lock:
            lb = self._books.get((venue, market_id))
            if lb is not None:
                lb.venue_ts = venue_ts

    def get(self, venue: str, market_id: str) -> LiveBook | None:
        with self._lock:
            return self._books.get((venue, market_id))

    def stats(self, max_age: float, now: float | None = None) -> dict:
        """Coverage snapshot for monitoring: total books and how many are fresh, per venue."""
        with self._lock:
            books = list(self._books.values())
        now = now if now is not None else time.time()
        out: dict[str, dict] = {}
        for b in books:
            s = out.setdefault(b.venue, {"books": 0, "fresh": 0})
            s["books"] += 1
            if b.fresh(max_age, now):
                s["fresh"] += 1
        return out
