"""
stream/polymarket_us_ws.py — live books.
Polymarket US MARKET_DATA WebSocket stream. PMUSOrderBook derives the ask to BUY each side
from a marketData-style book (offers + bids, px.value/qty), mirroring fetch.polymarket_us:
buy the long side at the best offer, buy the short side at 1 - best bid. PMUSWSClient owns the
authenticated connection and writes the derived top-of-book to the BookStore.

NOTE: the exact WS payload shape is the least-documented piece, so _extract_book() probes a
few likely shapes and we treat each market-data message as a fresh top-of-book. The shadow
comparison (manager.run_shadow) is what proves this parsing matches the REST book before any
fire trusts it — if a field name is wrong, agreement drops and we see it without risking money.
"""

import json
import threading
import time
from datetime import datetime

import config
from core import perf
from fetch.polymarket_us import _book_quote, _px, _num


def _extract_book(payload) -> dict | None:
    """Find the {offers/asks, bids} order book inside a PM US WS message; None if absent.
    Normalizes 'asks' -> 'offers' so _book_quote can read it. Searches one level deep."""
    def normalize(d: dict) -> dict | None:
        if not isinstance(d, dict):
            return None
        offers = d.get("offers") if d.get("offers") is not None else d.get("asks")
        bids = d.get("bids")
        book = {"offers": offers or [], "bids": bids or []}
        # require a REAL book: a stats-only / last-trade message has empty bids+offers and must
        # NOT be treated as a snapshot (replacing the live book with it would blank top-of-book).
        return book if (book["offers"] or book["bids"]) else None

    if not isinstance(payload, dict):
        return None
    direct = normalize(payload)
    if direct:
        return direct
    for v in payload.values():                      # one level deep (e.g. {"market_data_snapshot": {...}})
        book = normalize(v) if isinstance(v, dict) else None
        if book:
            return book
        if isinstance(v, dict):                     # two levels (e.g. {...: {"marketData": {...}}})
            for vv in v.values():
                book = normalize(vv) if isinstance(vv, dict) else None
                if book:
                    return book
    return None


class PMUSOrderBook:
    """Top-of-book for one PM US market; each market-data message replaces it (full snapshot)."""

    def __init__(self):
        self.md: dict = {}
        self.seq: int = 0

    def apply(self, md: dict, seq: int = 0) -> None:
        if not md or not (md.get("offers") or md.get("bids")):
            return                      # empty/stats message -> keep the prior book, never blank it
        self.md = md
        self.seq = seq

    def top(self):
        """(ask_yes, ask_no, depth_yes, depth_no): buy long at best offer, short at 1 - best bid."""
        best_ask, best_bid, ask_usd, bid_usd = _book_quote(self.md)
        ask_yes = round(best_ask, 4) if best_ask is not None else None
        ask_no = round(1.0 - best_bid, 4) if best_bid is not None else None
        return ask_yes, ask_no, float(ask_usd or 0.0), float(bid_usd or 0.0)

    def top_n(self, n: int):
        """(levels_yes, levels_no): top-N (price,size_usd) ask levels, best first.
        Yes asks = offers as-is; no asks = 1 - bids (short side). Malformed rows (no px, or
        qty <= 0) are filtered out BEFORE slicing to n, so a deeper valid level backfills instead
        of a bad row eating a top-n slot; size is dollars (px*qty), matching _book_quote's
        ask_usd/bid_usd convention."""
        offers, bids = self.md.get("offers") or [], self.md.get("bids") or []
        levels_yes = [(round(px, 4), round(px * qty, 2))
                      for o in offers if (px := _px(o)) is not None
                      and (qty := (_num(o.get("qty")) or 0.0)) > 0][:n]
        levels_no = [(round(1.0 - px, 4), round(px * qty, 2))
                     for b in bids if (px := _px(b)) is not None
                     and (qty := (_num(b.get("qty")) or 0.0)) > 0][:n]
        return levels_yes, levels_no


class PMUSWSClient:
    VENUE = "polymarket_us"

    def __init__(self, store, header_factory, slugs: list[str], log=print, dump: int = 0,
                 on_book_change=None):
        self.store = store
        self._headers = header_factory          # () -> {X-PM-*: ...} signed fresh per connect
        # Book-change fast path: called (venue, slug) after every
        # store.update so a caller's repricer sees the tick; exceptions are swallowed.
        self._on_book_change = on_book_change
        self.slugs = [s for s in dict.fromkeys(slugs) if s]
        self._log = log
        self._dump = dump                       # print this many raw messages verbatim (schema discovery)
        self._warned = False
        self._books: dict[str, PMUSOrderBook] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._ws = None

    def start(self) -> None:
        if self._running or not self.slugs:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="pmus-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self) -> None:
        import certifi      # CA bundle so TLS verifies (bare ssl ctx misses system roots on macOS)
        import websocket
        backoff = config.STREAM_RECONNECT_BASE
        while self._running:
            try:
                hdr = [f"{k}: {v}" for k, v in self._headers().items()]
                self._ws = websocket.WebSocketApp(
                    config.PM_US_WS_URL, header=hdr,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=lambda _ws, e: self._log(f"[pmus-ws] error: {e}"),
                    on_close=lambda _ws, *a: self._log("[pmus-ws] closed"))
                self._ws.run_forever(ping_interval=10, ping_timeout=5,
                                     sslopt={"ca_certs": certifi.where()})
            except Exception as e:
                self._log(f"[pmus-ws] run error: {e}")
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, config.STREAM_RECONNECT_CAP)

    def _on_open(self, ws) -> None:
        self._log(f"[pmus-ws] connected; subscribing {len(self.slugs)} slugs")
        ws.send(json.dumps({"subscribe": {"request_id": "xb-stream", "subscription_type": 1,
                                          "market_slugs": self.slugs}}))

    def _on_message(self, ws, raw: str) -> None:
        if self._dump > 0:
            self._log(f"[pmus-ws RAW] {raw[:700]}")
            self._dump -= 1
        try:
            data = json.loads(raw)
            slug = self._slug_of(data)
            book = _extract_book(data)
            if not slug or book is None:
                if not self._warned and not self._dump:   # surface ONE unparsed sample
                    self._warned = True
                    self._log(f"[pmus-ws] unparsed (slug={slug}, book={'yes' if book else 'no'}): {raw[:400]}")
                return
            seq = int(data.get("seq") or data.get("sequence") or 0)
            self._books.setdefault(slug, PMUSOrderBook()).apply(book, seq)
            self._publish(slug, self._transact_time(data))
        except Exception as e:
            if not self._warned:
                self._warned = True
                self._log(f"[pmus-ws] parse error: {e} | sample: {raw[:400]}")

    @staticmethod
    def _slug_of(data: dict) -> str | None:
        for k in ("market_slug", "marketSlug", "slug"):
            if data.get(k):
                return str(data[k])
        for v in data.values():                  # nested payloads carry the slug too
            if isinstance(v, dict):
                for k in ("market_slug", "marketSlug", "slug"):
                    if v.get(k):
                        return str(v[k])
        return None

    @staticmethod
    def _find_transact_time(d, depth: int = 0) -> str | None:
        if not isinstance(d, dict) or depth > 2:
            return None
        if d.get("transactTime"):
            return d["transactTime"]
        for v in d.values():
            found = PMUSWSClient._find_transact_time(v, depth + 1)
            if found:
                return found
        return None

    @classmethod
    def _transact_time(cls, data: dict) -> float | None:
        """Epoch seconds from a nested 'transactTime' ISO-8601 string; None on absence/parse
        failure/naive (no offset) — a naive .timestamp() assumes LOCAL time, which would skew
        venue_ts by hours vs the UTC wire value."""
        raw = cls._find_transact_time(data)
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    def _publish(self, slug: str, venue_ts: float | None = None) -> None:
        with perf.sample("ws_pmus_publish"):
            book = self._books[slug]
            ask_yes, ask_no, d_yes, d_no = book.top()
            levels_yes, levels_no = book.top_n(config.BOOK_DEPTH_LEVELS)
            self.store.update(self.VENUE, slug, ask_yes=ask_yes, ask_no=ask_no,
                              depth_yes=d_yes, depth_no=d_no, seq=book.seq,
                              venue_ts=venue_ts, levels_yes=levels_yes, levels_no=levels_no)
        if self._on_book_change is not None:   # strictly AFTER store.update: consumers re-read the book
            try:
                self._on_book_change(self.VENUE, slug)
            except Exception:
                pass   # the hook must NEVER take down the WS loop
