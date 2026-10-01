"""
stream/kalshi_ws.py — live books.
Kalshi WebSocket order-book stream. KalshiOrderBook is a pure snapshot+delta state machine
(unit-tested, no network); KalshiWSClient owns the authenticated connection, routes messages
to a per-ticker book, and writes the derived top-of-book into the BookStore.

Kalshi's book is two BID ladders (yes, no) in integer cents. A NO bid at price p is
economically a YES offer at (100 - p), so the ask to BUY YES = 100 - best NO bid, and the
ask to BUY NO = 100 - best YES bid. Depth behind each ask is the size on the opposing ladder.
"""

import json
import threading
import time

import config
from core import perf


class KalshiOrderBook:
    """Maintains the yes/no bid ladders (in DOLLARS) for one market from snapshot + delta.
    Wire format (confirmed live 2026-06-23): snapshot carries `yes_dollars_fp`/`no_dollars_fp`
    as [price_str, size_str] rows; deltas carry `price_dollars`/`delta_fp`/`side`."""

    def __init__(self):
        self.yes: dict[float, float] = {}   # YES-bid price ($) -> resting size (contracts)
        self.no: dict[float, float] = {}    # NO-bid  price ($) -> resting size (contracts)
        self.seq: int = 0

    @staticmethod
    def _levels(rows) -> dict[float, float]:
        out: dict[float, float] = {}
        for row in rows or []:
            try:
                price, size = round(float(row[0]), 4), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if size > 0:
                out[price] = size
        return out

    def snapshot(self, msg: dict, seq: int = 0) -> None:
        self.yes = self._levels(msg.get("yes_dollars_fp"))
        self.no = self._levels(msg.get("no_dollars_fp"))
        self.seq = seq

    def delta(self, msg: dict, seq: int = 0) -> None:
        side = msg.get("side")
        book = self.yes if side == "yes" else self.no if side == "no" else None
        if book is None:
            return
        try:
            price, d = round(float(msg.get("price_dollars")), 4), float(msg.get("delta_fp"))
        except (TypeError, ValueError):
            return
        book[price] = book.get(price, 0.0) + d
        if book[price] <= 1e-9:
            book.pop(price, None)
        self.seq = seq

    def top(self):
        """(ask_yes, ask_no, depth_yes, depth_no): a NO bid at p is a YES offer at 1-p, so the
        ask to BUY YES = 1 - best NO bid (and vice-versa). Depth = size on the opposing ladder."""
        best_yes_bid = max(self.yes) if self.yes else None
        best_no_bid = max(self.no) if self.no else None
        ask_yes = round(1.0 - best_no_bid, 4) if best_no_bid is not None else None
        ask_no = round(1.0 - best_yes_bid, 4) if best_yes_bid is not None else None
        depth_yes = self.no.get(best_no_bid, 0.0) if best_no_bid is not None else 0.0
        depth_no = self.yes.get(best_yes_bid, 0.0) if best_yes_bid is not None else 0.0
        return ask_yes, ask_no, depth_yes, depth_no

    def top_n(self, n: int):
        """(levels_yes, levels_no): top-N (price,size) ask levels via the $1 mirror, best first.
        YES asks come from the NO-bid ladder sorted descending (highest NO bid = best YES ask)."""
        no_sorted = sorted(self.no.items(), key=lambda kv: -kv[0])[:n]
        yes_sorted = sorted(self.yes.items(), key=lambda kv: -kv[0])[:n]
        levels_yes = [(round(1.0 - p, 4), sz) for p, sz in no_sorted]
        levels_no = [(round(1.0 - p, 4), sz) for p, sz in yes_sorted]
        return levels_yes, levels_no


class KalshiWSClient:
    """Authenticated Kalshi WS connection that keeps a BookStore fresh for `tickers`.
    Reconnects with exponential backoff, re-signing the handshake each attempt.
    Also subscribes `ticker` (Kalshi's only channel carrying ts_ms) purely to harvest venue
    timestamps; ticker prices never touch the ladder — top/depth/levels stay orderbook-only."""

    VENUE = "kalshi"

    def __init__(self, store, header_factory, tickers: list[str], log=print, dump: int = 0,
                 on_book_change=None):
        self.store = store
        self._headers = header_factory          # () -> {KALSHI-ACCESS-*: ...} signed fresh per connect
        # Book-change fast path: called (venue, ticker) after every
        # store.update so a caller's repricer sees the tick; exceptions are swallowed. The
        # ticker-frame venue_ts touch deliberately does NOT fire it — no price change there.
        self._on_book_change = on_book_change
        self.tickers = [t for t in dict.fromkeys(tickers) if t]
        self._log = log
        self._dump = dump                       # print this many raw messages verbatim (schema discovery)
        self._warned = False
        self._books: dict[str, KalshiOrderBook] = {}
        self._venue_ts_cache: dict[str, float] = {}  # last known ticker-stamped venue_ts per market
        self._last_seq = None                   # orderbook_delta subscription's own message counter
        self._running = False
        self._thread: threading.Thread | None = None
        self._ws = None

    def start(self) -> None:
        if self._running or not self.tickers:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="kalshi-ws", daemon=True)
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
        import websocket    # lazy: keeps the order-book logic importable without the lib
        backoff = config.STREAM_RECONNECT_BASE
        while self._running:
            try:
                hdr = [f"{k}: {v}" for k, v in self._headers().items()]
                self._ws = websocket.WebSocketApp(
                    config.KALSHI_WS_URL, header=hdr,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=lambda _ws, e: self._log(f"[kalshi-ws] error: {e}"),
                    on_close=lambda _ws, *a: self._log("[kalshi-ws] closed"))
                self._ws.run_forever(ping_interval=10, ping_timeout=5,
                                     sslopt={"ca_certs": certifi.where()})
            except Exception as e:
                self._log(f"[kalshi-ws] run error: {e}")
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, config.STREAM_RECONNECT_CAP)

    def _on_open(self, ws) -> None:
        self._books = {}            # fresh connection -> discard any stale books, re-snapshot all
        self._venue_ts_cache = {}   # discard stale ticker timestamps too
        self._last_seq = None
        self._log(f"[kalshi-ws] connected; subscribing {len(self.tickers)} tickers")
        ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                            "params": {"channels": ["orderbook_delta", "ticker"],
                                       "market_tickers": self.tickers}}))

    def _seq_gap(self, raw_seq) -> bool:
        """Monotonic counter check for the orderbook_delta subscription. Returns True (and resets)
        if a message was missed — meaning some book may be desynced and we must resync. None/absent
        seq -> no check. Confirmed live 2026-06-23: `seq` is scoped per subscription `sid`, not
        connection-wide (ticker/orderbook_delta/trade each get their own sid + independent seq
        counter) — only orderbook_delta/orderbook_snapshot messages must feed this counter."""
        if raw_seq is None:
            return False
        seq = int(raw_seq)
        gap = self._last_seq is not None and seq != self._last_seq + 1
        self._last_seq = None if gap else seq
        return gap

    def _on_message(self, ws, raw: str) -> None:
        if self._dump > 0:
            self._log(f"[kalshi-ws RAW] {raw[:700]}")
            self._dump -= 1
        try:
            data = json.loads(raw)
            typ, msg = data.get("type"), data.get("msg") or {}
            ticker = msg.get("market_ticker")
            ts_ms = msg.get("ts_ms", data.get("ts_ms"))   # venue timestamp, either level, when present
            is_orderbook = typ == "orderbook_snapshot" or typ == "orderbook_delta"
            # ticker has its own sid/seq counter (see _seq_gap) -> must never feed the orderbook gap check
            if is_orderbook and self._seq_gap(data.get("seq")):
                self._log(f"[kalshi-ws] SEQ GAP -> reconnecting to resync (books go stale -> REST fallback)")
                ws.close()                        # _run reconnects; _on_open clears books + re-subscribes
                return
            if typ == "orderbook_snapshot" and ticker:
                self._books.setdefault(ticker, KalshiOrderBook()).snapshot(msg, self._last_seq or 0)
                self._publish(ticker, ts_ms)
            elif typ == "orderbook_delta" and ticker:
                bk = self._books.get(ticker)
                if bk is None:                    # delta with no base snapshot -> can't trust; wait for snapshot
                    return
                bk.delta(msg, self._last_seq or 0)
                self._publish(ticker, ts_ms)
            elif typ == "ticker" and ticker and ticker in self._books:
                # harvest venue_ts ONLY: touch (not publish) so a ticker frame never resets the
                # ladder's wall-clock freshness — fresh() must reflect real orderbook updates
                vts = self._venue_ts(ts_ms)
                if vts is not None:
                    self._venue_ts_cache[ticker] = vts
                    self.store.touch_venue_ts(self.VENUE, ticker, vts)
            elif typ == "error":
                self._log(f"[kalshi-ws] sub error: {data}")
        except Exception as e:   # log ONE sample so we see the real shape instead of spamming
            if not self._warned:
                self._warned = True
                self._log(f"[kalshi-ws] parse error: {e} | sample: {raw[:400]}")

    @staticmethod
    def _venue_ts(ts_ms) -> float | None:
        try:
            return float(ts_ms) / 1000.0 if ts_ms is not None else None
        except (TypeError, ValueError):
            return None

    def _publish(self, ticker: str, ts_ms=None) -> None:
        with perf.sample("ws_kalshi_publish"):
            # orderbook messages never carry ts_ms in practice; a ladder publish preserves the
            # last ticker-stamped venue_ts instead of resetting it to None, so freshness reflects
            # the last real venue print rather than going stale on every quiet ticker gap
            vts = self._venue_ts(ts_ms)
            if vts is not None:
                self._venue_ts_cache[ticker] = vts
            else:
                vts = self._venue_ts_cache.get(ticker)
            book = self._books[ticker]
            ask_yes, ask_no, d_yes, d_no = book.top()
            levels_yes, levels_no = book.top_n(config.BOOK_DEPTH_LEVELS)
            self.store.update(self.VENUE, ticker, ask_yes=ask_yes, ask_no=ask_no,
                              depth_yes=d_yes, depth_no=d_no, seq=book.seq,
                              venue_ts=vts, levels_yes=levels_yes, levels_no=levels_no)
        if self._on_book_change is not None:   # strictly AFTER store.update: consumers re-read the book
            try:
                self._on_book_change(self.VENUE, ticker)
            except Exception:
                pass   # the hook must NEVER take down the WS loop
