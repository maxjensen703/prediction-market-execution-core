"""Stream unit tests: the order-book derivations (the money-critical math) and the store.
Live connections aren't tested here — that's what manager.run_shadow validates against REST."""

import json

import pytest

from stream.book_store import BookStore
from stream.kalshi_ws import KalshiOrderBook, KalshiWSClient
from stream.polymarket_us_ws import PMUSOrderBook, PMUSWSClient, _extract_book


# ── Kalshi: yes/no bid ladders -> ask to buy each side ──────────────────────

def test_kalshi_snapshot_derives_asks_from_opposing_bids():
    ob = KalshiOrderBook()
    # Live wire format: dollar-string ladders. YES bids up to $0.40, NO bids up to $0.55.
    # ask to BUY YES = 1 - best NO bid = 0.45; ask to BUY NO = 1 - best YES bid = 0.60.
    ob.snapshot({"yes_dollars_fp": [["0.4000", "100.00"], ["0.3900", "50.00"]],
                 "no_dollars_fp": [["0.5500", "30.00"], ["0.5400", "70.00"]]}, seq=1)
    ask_yes, ask_no, d_yes, d_no = ob.top()
    assert ask_yes == 0.45 and ask_no == 0.60
    assert d_yes == 30.0 and d_no == 100.0      # contracts resting at the best opposing bid


def test_kalshi_delta_updates_and_removes_levels():
    ob = KalshiOrderBook()
    ob.snapshot({"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": [["0.5500", "30.00"]]}, seq=1)
    ob.delta({"side": "no", "price_dollars": "0.5600", "delta_fp": "20.00"}, seq=2)   # better NO bid $0.56
    assert ob.top()[0] == 0.44                                                        # ask YES = 1 - 0.56
    ob.delta({"side": "no", "price_dollars": "0.5600", "delta_fp": "-20.00"}, seq=3)  # remove it
    assert ob.top()[0] == 0.45 and ob.seq == 3                                        # back to 1 - 0.55


def test_kalshi_empty_side_is_none():
    ob = KalshiOrderBook()
    ob.snapshot({"yes_dollars_fp": [["0.4000", "100.00"]], "no_dollars_fp": []}, seq=1)
    ask_yes, ask_no, _, _ = ob.top()
    assert ask_yes is None and ask_no == 0.60     # no NO bids -> no YES ask; YES bid still gives NO ask


def test_kalshi_top_n_derives_ordered_levels_from_opposing_bids():
    ob = KalshiOrderBook()
    ob.snapshot({"yes_dollars_fp": [["0.4000", "100.00"], ["0.3900", "50.00"]],
                 "no_dollars_fp": [["0.5500", "30.00"], ["0.5400", "70.00"]]}, seq=1)
    levels_yes, levels_no = ob.top_n(5)
    # YES asks mirror NO bids descending: best NO bid 0.55 -> ask 0.45 first, then 0.54 -> 0.46
    assert levels_yes == [(0.45, 30.0), (0.46, 70.0)]
    # NO asks mirror YES bids descending: best YES bid 0.40 -> ask 0.60 first, then 0.39 -> 0.61
    assert levels_no == [(0.60, 100.0), (0.61, 50.0)]


def test_kalshi_top_n_caps_at_n():
    ob = KalshiOrderBook()
    ob.snapshot({"yes_dollars_fp": [], "no_dollars_fp": [["0.50", "1"], ["0.40", "2"], ["0.30", "3"]]}, seq=1)
    levels_yes, _ = ob.top_n(2)
    assert len(levels_yes) == 2 and levels_yes[0][0] == 0.50   # best (highest NO bid) first, capped to N


# ── PM US: offers/bids -> ask to buy long / short ───────────────────────────

def _lvl(px, qty):
    return {"px": {"value": str(px)}, "qty": str(qty)}


def test_pmus_top_buys_long_at_offer_short_at_one_minus_bid():
    ob = PMUSOrderBook()
    ob.apply({"offers": [_lvl(0.62, 100)], "bids": [_lvl(0.60, 200)]}, seq=1)
    ask_yes, ask_no, d_yes, d_no = ob.top()
    assert ask_yes == 0.62 and ask_no == 0.40              # short ask = 1 - 0.60
    assert round(d_yes, 2) == 62.0 and round(d_no, 2) == 120.0   # usd depth = px*qty


def test_extract_book_finds_nested_shapes():
    nested = {"market_data_snapshot": {"offers": [1], "bids": [2]}}
    assert _extract_book(nested) == {"offers": [1], "bids": [2]}
    two_deep = {"payload": {"marketData": {"asks": [1], "bids": [2]}}}      # 'asks' normalized to 'offers'
    assert _extract_book(two_deep) == {"offers": [1], "bids": [2]}
    assert _extract_book({"unrelated": 1}) is None


def test_extract_book_rejects_empty_and_stats_only():
    assert _extract_book({"offers": [], "bids": []}) is None              # empty -> not a real book
    assert _extract_book({"marketData": {"lastTradePrice": "0.5", "bids": [], "offers": []}}) is None
    assert _extract_book({"marketData": {"offers": [1], "bids": []}}) == {"offers": [1], "bids": []}  # one side ok


def test_pmus_apply_keeps_prior_on_empty():
    ob = PMUSOrderBook()
    ob.apply({"offers": [_lvl(0.62, 100)], "bids": [_lvl(0.60, 200)]}, seq=1)
    ob.apply({"offers": [], "bids": []}, seq=2)        # stats/empty message -> must be ignored
    assert ob.top()[0] == 0.62 and ob.seq == 1         # prior book preserved, not blanked


def test_pmus_top_n_slices_offers_and_bids_best_first():
    ob = PMUSOrderBook()
    ob.apply({"offers": [_lvl(0.62, 100), _lvl(0.65, 50)],
              "bids": [_lvl(0.60, 200), _lvl(0.58, 40)]}, seq=1)
    levels_yes, levels_no = ob.top_n(5)
    # size is DOLLARS (px*qty), matching _book_quote's ask_usd/bid_usd convention
    assert levels_yes == [(0.62, 62.0), (0.65, 32.5)]         # offers as-is, best (lowest) first
    assert levels_no == [(0.40, 120.0), (0.42, 23.2)]         # 1 - bid, best (highest bid) first


def test_pmus_top_n_caps_at_n():
    ob = PMUSOrderBook()
    ob.apply({"offers": [_lvl(0.60, 1), _lvl(0.61, 2), _lvl(0.62, 3)], "bids": []}, seq=1)
    levels_yes, _ = ob.top_n(2)
    assert len(levels_yes) == 2 and levels_yes[0] == (0.60, 0.6)


def test_pmus_top_n_filters_malformed_before_slicing_and_drops_zero_size():
    ob = PMUSOrderBook()
    # row[1] has no px (malformed) -> skipped, so a deeper valid offer (row[3]) backfills the
    # top-2 slot instead of a bad row eating it. row[2] has qty=0 -> dropped entirely (never
    # appears, even outside the requested top-n).
    ob.apply({"offers": [_lvl(0.60, 1), {"px": {"value": None}, "qty": "5"},
                         _lvl(0.61, 0), _lvl(0.62, 3)],
              "bids": []}, seq=1)
    levels_yes, _ = ob.top_n(2)
    assert levels_yes == [(0.60, 0.6), (0.62, 1.86)]   # malformed skipped, 0.62 backfills; 0.61(qty=0) dropped


# ── venue timestamps ─────────────────────────────────────────────────────────

def test_kalshi_publish_stores_venue_ts_from_ts_ms():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    c._books["T"] = KalshiOrderBook()
    c._books["T"].snapshot({"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "10"]]}, seq=1)
    c._publish("T", ts_ms=1_750_000_000_000)
    lb = store.get("kalshi", "T")
    assert lb.venue_ts == pytest.approx(1_750_000_000.0)
    assert lb.venue_age(now=1_750_000_010.0) == pytest.approx(10.0)


def test_kalshi_publish_venue_ts_none_when_absent():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    c._books["T"] = KalshiOrderBook()
    c._books["T"].snapshot({"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "10"]]}, seq=1)
    c._publish("T")   # no ts_ms passed
    lb = store.get("kalshi", "T")
    assert lb.venue_ts is None and lb.venue_age() is None


def test_pmus_transact_time_parses_nested_iso_string():
    # exact wire format (9-digit nanoseconds) from data/audit/pmus_ws_raw.json
    data = {"marketData": {"transactTime": "2026-06-29T16:04:55.598243097Z", "offers": [], "bids": []}}
    ts = PMUSWSClient._transact_time(data)
    assert ts == pytest.approx(1782749095.598243)   # Python truncates ns -> us (drops trailing 097)


def test_pmus_transact_time_none_on_absence_or_bad_parse():
    assert PMUSWSClient._transact_time({"marketData": {"offers": []}}) is None
    assert PMUSWSClient._transact_time({"marketData": {"transactTime": "not-a-date"}}) is None


def test_pmus_transact_time_none_on_naive_string():
    # no 'Z'/offset -> naive datetime; .timestamp() would assume LOCAL time and skew venue_ts
    data = {"marketData": {"transactTime": "2026-06-29T16:04:55.598243", "offers": [], "bids": []}}
    assert PMUSWSClient._transact_time(data) is None


def test_pmus_publish_stores_venue_ts():
    store = BookStore()
    c = PMUSWSClient(store, lambda: {}, ["s1"])
    c._books["s1"] = PMUSOrderBook()
    c._books["s1"].apply({"offers": [_lvl(0.62, 100)], "bids": [_lvl(0.60, 200)]}, seq=1)
    c._publish("s1", venue_ts=1_750_000_000.0)
    lb = store.get("polymarket_us", "s1")
    assert lb.venue_ts == 1_750_000_000.0


def test_kalshi_seq_gap_detection():
    from stream.kalshi_ws import KalshiWSClient
    c = KalshiWSClient(BookStore(), lambda: {}, [])
    assert c._seq_gap(1) is False        # first message, no baseline
    assert c._seq_gap(2) is False        # consecutive
    assert c._seq_gap(4) is True         # missed seq 3 -> gap (triggers reconnect)
    assert c._seq_gap(5) is False        # re-initialized after the gap
    assert c._seq_gap(None) is False     # non-data message (no seq) -> no check


# ── ticker channel: venue_ts harvesting only, ladder stays orderbook-only ────

class _FakeWS:
    """Minimal stand-in for the websocket-client connection object."""
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def send(self, *_a, **_kw):
        pass


def _snapshot(c, ticker="T"):
    c._books[ticker] = KalshiOrderBook()
    c._books[ticker].snapshot({"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "10"]]}, seq=1)


def test_kalshi_ticker_stamps_venue_ts_without_touching_ladder():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    _snapshot(c)
    c._publish("T")                          # simulate the prior orderbook publish (no venue_ts yet)
    before = store.get("kalshi", "T")
    # snapshot the pre-ticker record values (the ticker path mutates the record in place)
    pre = (before.ask_yes, before.ask_no, before.depth_yes, before.depth_no,
           list(before.levels_yes), list(before.levels_no), before.updated_at)
    assert before.venue_ts is None
    msg = json.dumps({"type": "ticker", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": "T", "ts_ms": 1_750_000_000_000, "price": 41}})
    c._on_message(_FakeWS(), msg)
    after = store.get("kalshi", "T")
    assert after.venue_ts == pytest.approx(1_750_000_000.0)
    # ladder-derived fields must be byte-identical to the pre-ticker publish
    assert (after.ask_yes, after.ask_no, after.depth_yes, after.depth_no) == pre[:4]
    assert after.levels_yes == pre[4] and after.levels_no == pre[5]
    assert after.updated_at == pre[6]        # ticker frames never refresh wall-clock freshness


def test_kalshi_ticker_does_not_extend_fresh_but_ladder_does():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    _snapshot(c)
    c._publish("T")
    store.get("kalshi", "T").updated_at = 123.0    # force a stale wall-clock stamp
    msg = json.dumps({"type": "ticker", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": "T", "ts_ms": 1_750_000_000_000}})
    c._on_message(_FakeWS(), msg)
    lb = store.get("kalshi", "T")
    assert lb.updated_at == 123.0                  # ticker did NOT reset ladder freshness
    assert lb.fresh(3.0) is False                  # the book stays honestly stale
    assert lb.venue_ts == pytest.approx(1_750_000_000.0)   # venue_ts still harvested
    c._publish("T")                                # a real ladder publish...
    lb = store.get("kalshi", "T")
    assert lb.updated_at != 123.0 and lb.fresh(3.0) is True   # ...does refresh updated_at
    assert lb.venue_ts == pytest.approx(1_750_000_000.0)      # and preserves venue_ts (cache)


def test_kalshi_ticker_for_untracked_market_is_noop():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])   # no snapshot -> "OTHER" never tracked
    msg = json.dumps({"type": "ticker", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": "OTHER", "ts_ms": 1_750_000_000_000}})
    c._on_message(_FakeWS(), msg)
    assert store.get("kalshi", "OTHER") is None
    assert c._venue_ts_cache == {}


def test_kalshi_ladder_publish_after_ticker_preserves_venue_ts():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    _snapshot(c)
    c._publish("T", ts_ms=1_750_000_000_000)   # ticker-equivalent stamp
    c._publish("T")                            # later orderbook publish, no ts_ms of its own
    lb = store.get("kalshi", "T")
    # documented choice: preserve the last known ticker ts across ladder-only publishes rather
    # than resetting to None, since Kalshi orderbook frames never carry their own ts_ms
    assert lb.venue_ts == pytest.approx(1_750_000_000.0)


def test_kalshi_subscribe_includes_ticker_and_orderbook_channels():
    sent = {}
    class _OpenWS:
        def send(self, payload):
            sent["payload"] = json.loads(payload)
    c = KalshiWSClient(BookStore(), lambda: {}, ["T"])
    c._on_open(_OpenWS())
    params = sent["payload"]["params"]
    assert set(params["channels"]) == {"orderbook_delta", "ticker"}
    assert params["market_tickers"] == ["T"]


def test_kalshi_ticker_seq_does_not_feed_orderbook_gap_check():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    _snapshot(c)
    c._last_seq = 5                            # orderbook subscription mid-stream at seq 5
    ws = _FakeWS()
    # ticker has its OWN independent seq counter (confirmed live: distinct sid); its seq=1 would
    # look like a huge gap if merged into the orderbook's counter
    msg = json.dumps({"type": "ticker", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": "T", "ts_ms": 1_750_000_000_000}})
    c._on_message(ws, msg)
    assert ws.closed is False                  # no spurious reconnect
    assert c._last_seq == 5                     # orderbook's own counter untouched by the ticker frame


def test_kalshi_orderbook_seq_gap_still_reconnects_with_ticker_subscribed():
    store = BookStore()
    c = KalshiWSClient(store, lambda: {}, ["T"])
    _snapshot(c)
    c._last_seq = 5
    ws = _FakeWS()
    msg = json.dumps({"type": "orderbook_delta", "sid": 2, "seq": 7,
                      "msg": {"market_ticker": "T", "side": "yes",
                              "price_dollars": "0.41", "delta_fp": "1.0"}})
    c._on_message(ws, msg)                      # missed seq 6 -> gap
    assert ws.closed is True
    assert c._last_seq is None


# ── on_book_change tick hook: book update -> caller fast path ──────────────────

def test_kalshi_publish_invokes_on_book_change_after_update():
    store = BookStore()
    calls = []

    def cb(venue, mid):
        assert store.get("kalshi", "T") is not None   # hook fires strictly AFTER store.update
        calls.append((venue, mid))

    c = KalshiWSClient(store, lambda: {}, ["T"], on_book_change=cb)
    _snapshot(c)
    c._publish("T")
    assert calls == [("kalshi", "T")]


def test_kalshi_hook_exception_never_kills_publish():
    store = BookStore()

    def boom(v, m):
        raise RuntimeError("hook bug")

    c = KalshiWSClient(store, lambda: {}, ["T"], on_book_change=boom)
    _snapshot(c)
    c._publish("T")                                    # must not raise
    assert store.get("kalshi", "T").ask_yes == 0.45    # book still published


def test_kalshi_ticker_frame_does_not_invoke_hook():
    # the ticker frame carries no price change — hooking it would ~double event volume
    # for zero repricing information (spec §A.1: do NOT hook the touch_venue_ts path)
    store = BookStore()
    calls = []
    c = KalshiWSClient(store, lambda: {}, ["T"], on_book_change=lambda v, m: calls.append(m))
    _snapshot(c)
    c._publish("T")
    assert len(calls) == 1
    msg = json.dumps({"type": "ticker", "sid": 1, "seq": 1,
                      "msg": {"market_ticker": "T", "ts_ms": 1_750_000_000_000}})
    c._on_message(_FakeWS(), msg)
    assert len(calls) == 1                             # venue_ts touched, hook NOT invoked


def test_pmus_publish_invokes_on_book_change():
    store = BookStore()
    calls = []
    c = PMUSWSClient(store, lambda: {}, ["s1"], on_book_change=lambda v, m: calls.append((v, m)))
    c._books["s1"] = PMUSOrderBook()
    c._books["s1"].apply({"offers": [_lvl(0.62, 100)], "bids": [_lvl(0.60, 200)]}, seq=1)
    c._publish("s1")
    assert calls == [("polymarket_us", "s1")]


def test_pmus_hook_exception_never_kills_publish():
    store = BookStore()

    def boom(v, m):
        raise RuntimeError("hook bug")

    c = PMUSWSClient(store, lambda: {}, ["s1"], on_book_change=boom)
    c._books["s1"] = PMUSOrderBook()
    c._books["s1"].apply({"offers": [_lvl(0.62, 100)], "bids": [_lvl(0.60, 200)]}, seq=1)
    c._publish("s1")                                   # must not raise
    assert store.get("polymarket_us", "s1").ask_yes == 0.62


def test_manager_threads_on_book_change_to_both_clients(monkeypatch):
    import stream.manager as sm
    import execute.kalshi_broker as kbm
    import execute.polymarket_us_broker as pbm

    captured = []

    class _CapClient:
        def __init__(self, store, hf, ids, log=print, dump=0, on_book_change=None):
            captured.append(on_book_change)

        def start(self):
            pass

        def stop(self):
            pass

    class _StubBroker:
        configured = True
        api_key_id = "k"

        def _sign(self, *a):
            return "sig"

        def _headers(self, *a):
            return {}

    monkeypatch.setattr(kbm, "KalshiBroker", _StubBroker)
    monkeypatch.setattr(pbm, "PolymarketUSBroker", _StubBroker)
    monkeypatch.setattr(sm, "KalshiWSClient", _CapClient)
    monkeypatch.setattr(sm, "PMUSWSClient", _CapClient)
    cb = lambda v, m: None
    mgr = sm.StreamManager(BookStore(), on_book_change=cb)
    mgr.start(["T"], ["s1"])
    assert captured == [cb, cb]                        # same callback reaches BOTH clients


def test_store_health_flag():
    s = BookStore()
    assert s.healthy is False                         # untrusted until the audit confirms
    s.set_health(True, {"compared": 12, "within_1c": 0.95})
    assert s.healthy is True and s.audit["within_1c"] == 0.95
    s.set_health(False, {"compared": 12, "within_1c": 0.4})
    assert s.healthy is False


def test_audit_earns_then_revokes_trust(monkeypatch):
    from stream.manager import StreamManager
    s = BookStore()
    s.update("kalshi", "T", ask_yes=0.45, ask_no=0.55, now=1000.0)
    mgr = StreamManager(s)
    mgr._subscribed = (frozenset(["T"]), frozenset())
    monkeypatch.setattr(StreamManager, "_kalshi_rest", staticmethod(lambda kb, t: (0.45, 0.55)))
    assert mgr.audit_once(kb=None, client=None)["healthy"] is True and s.healthy is True   # REST agrees -> trust
    monkeypatch.setattr(StreamManager, "_kalshi_rest", staticmethod(lambda kb, t: (0.30, 0.70)))
    assert mgr.audit_once(kb=None, client=None)["healthy"] is False and s.healthy is False  # diverges -> revoke


# ── store ────────────────────────────────────────────────────────────────────

def test_store_update_get_and_freshness():
    s = BookStore()
    s.update("kalshi", "T1", ask_yes=0.45, ask_no=0.60, depth_yes=30, depth_no=100, seq=5, now=1000.0)
    lb = s.get("kalshi", "T1")
    assert lb.ask_yes == 0.45 and lb.seq == 5
    assert lb.age(now=1002.0) == 2.0
    assert lb.fresh(max_age=3.0, now=1002.0) and not lb.fresh(max_age=1.0, now=1002.0)
    assert s.get("kalshi", "missing") is None


def test_store_update_accepts_venue_ts_and_levels():
    s = BookStore()
    s.update("kalshi", "T1", ask_yes=0.45, venue_ts=1000.0, now=1005.0,
             levels_yes=[(0.45, 30.0)], levels_no=[(0.60, 100.0)])
    lb = s.get("kalshi", "T1")
    assert lb.venue_ts == 1000.0 and lb.venue_age(now=1008.0) == pytest.approx(8.0)
    assert lb.levels_yes == [(0.45, 30.0)] and lb.levels_no == [(0.60, 100.0)]
    assert lb.age(now=1008.0) == pytest.approx(3.0)   # wall-clock age unchanged/independent of venue_ts


def test_store_touch_venue_ts_preserves_updated_at():
    s = BookStore()
    s.update("kalshi", "T1", ask_yes=0.45, now=1000.0)
    s.touch_venue_ts("kalshi", "T1", 999.5)
    lb = s.get("kalshi", "T1")
    assert lb.venue_ts == 999.5 and lb.updated_at == 1000.0   # venue_ts only; freshness untouched
    s.touch_venue_ts("kalshi", "T1", None)                    # None -> no-op
    assert s.get("kalshi", "T1").venue_ts == 999.5
    s.touch_venue_ts("kalshi", "MISSING", 5.0)                # unknown book -> no-op, no crash
    assert s.get("kalshi", "MISSING") is None


def test_store_update_defaults_venue_ts_and_levels_to_none():
    s = BookStore()
    s.update("kalshi", "T2", ask_yes=0.45, now=1000.0)   # no venue_ts/levels passed
    lb = s.get("kalshi", "T2")
    assert lb.venue_ts is None and lb.venue_age(now=1010.0) is None
    assert lb.levels_yes is None and lb.levels_no is None


def test_store_stats_counts_fresh_per_venue():
    s = BookStore()
    s.update("kalshi", "A", ask_yes=0.5, now=1000.0)
    s.update("kalshi", "B", ask_yes=0.5, now=990.0)      # stale
    s.update("polymarket_us", "C", ask_yes=0.5, now=1000.0)
    st = s.stats(max_age=3.0, now=1001.0)
    assert st["kalshi"] == {"books": 2, "fresh": 1}
    assert st["polymarket_us"] == {"books": 1, "fresh": 1}
