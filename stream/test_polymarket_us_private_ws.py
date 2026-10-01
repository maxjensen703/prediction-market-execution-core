"""Private order-events WS: frame-level tests (no network) — snapshot/update parsing,
execution dedupe, callback invocation, malformed-frame tolerance."""

import json

from stream.polymarket_us_private_ws import PMUSPrivateWSClient, _extract_executions


def _client(on_execution=None):
    events = []
    cb = on_execution or (lambda evt: events.append(evt))
    c = PMUSPrivateWSClient(header_factory=lambda: {}, on_execution=cb, log=lambda *_: None)
    return c, events


# ── frame extraction ────────────────────────────────────────────────────────

def test_extract_executions_ignores_documented_snapshot_orders():
    # Documented orderSubscriptionSnapshot carries `orders` + `eof` (NOT executions) — the
    # client ignores it; the owner reconciles resting state via open_orders().
    frame = {"orderSubscriptionSnapshot": {"eof": True, "orders": [
        {"id": "o1", "state": "ORDER_STATE_NEW", "marketSlug": "aec-x"}]}}
    assert _extract_executions(frame) == []


def test_extract_executions_snapshot_executions_tolerated():
    # Tolerance only: if a snapshot ever carried executions, they'd still be surfaced.
    frame = {"orderSubscriptionSnapshot": {"eof": True, "executions": [
        {"id": "e1", "order": {"id": "o1"}, "lastShares": "1", "lastPx": {"value": "0.45"},
         "type": "EXECUTION_TYPE_FILL", "tradeId": "t1"}]}}
    execs = _extract_executions(frame)
    assert len(execs) == 1 and execs[0]["id"] == "e1"


def test_extract_executions_from_update():
    frame = {"orderSubscriptionUpdate": {"executions": [{"id": "e2", "type": "EXECUTION_TYPE_PARTIAL_FILL"}]}}
    assert [e["id"] for e in _extract_executions(frame)] == ["e2"]


def test_extract_executions_handles_single_execution_object():
    frame = {"orderSubscriptionUpdate": {"execution": {"id": "e3"}}}
    assert [e["id"] for e in _extract_executions(frame)] == ["e3"]


def test_extract_executions_empty_on_unrelated_frame():
    assert _extract_executions({"unrelated": 1}) == []
    assert _extract_executions({"orderSubscriptionSnapshot": {"eof": True, "orders": []}}) == []


# ── dedupe + callback invocation ────────────────────────────────────────────

def test_handle_frame_invokes_callback_once_per_execution_id():
    c, events = _client()
    frame = {"orderSubscriptionUpdate": {"executions": [
        {"id": "e1", "order": {"id": "o1"}, "type": "EXECUTION_TYPE_FILL"}]}}
    c.handle_frame(frame)
    c.handle_frame(frame)   # duplicate id -> must not re-invoke
    assert len(events) == 1 and events[0]["id"] == "e1"


def test_handle_frame_dedupes_across_updates():
    c, events = _client()
    upd1 = {"orderSubscriptionUpdate": {"executions": [{"id": "e1"}]}}
    upd2 = {"orderSubscriptionUpdate": {"executions": [{"id": "e1"}, {"id": "e2"}]}}
    c.handle_frame(upd1)
    c.handle_frame(upd2)
    assert [e["id"] for e in events] == ["e1", "e2"]


def test_handle_frame_documented_snapshot_then_updates():
    # Documented snapshot (orders+eof) is ignored; only update executions reach the callback.
    c, events = _client()
    c.handle_frame({"orderSubscriptionSnapshot": {"eof": True,
                    "orders": [{"id": "o1", "state": "ORDER_STATE_NEW"}]}})
    c.handle_frame({"orderSubscriptionUpdate": {"executions": [{"id": "c"}]}})
    assert [e["id"] for e in events] == ["c"]


# ── malformed-frame tolerance ────────────────────────────────────────────────

def test_handle_frame_tolerates_non_dict_frame():
    c, events = _client()
    c.handle_frame("not a dict")   # must not raise
    c.handle_frame(None)
    assert events == []


def test_handle_frame_tolerates_execution_without_id():
    c, events = _client()
    c.handle_frame({"orderSubscriptionUpdate": {"executions": [{"order": {"id": "o1"}}]}})
    assert events == []   # no id -> skipped, not crashed


def test_handle_frame_tolerates_non_dict_execution_entries():
    c, events = _client()
    c.handle_frame({"orderSubscriptionUpdate": {"executions": ["garbage", 123, {"id": "e9"}]}})
    assert [e["id"] for e in events] == ["e9"]


def test_handle_frame_callback_exception_is_isolated():
    def boom(evt):
        raise RuntimeError("callback blew up")
    c, _ = _client(on_execution=boom)
    c.handle_frame({"orderSubscriptionUpdate": {"executions": [{"id": "e1"}]}})   # must not raise


def test_handle_frame_callback_crash_isolated_per_event_and_not_marked_seen():
    # MAJOR-4: event 1's crash must not swallow events 2,3, and e1 stays UNSEEN so a
    # redelivery reaches the callback (the owner dedupes/hedges by qty delta).
    delivered = []
    def cb(evt):
        if evt["id"] == "e1":
            raise RuntimeError("boom")
        delivered.append(evt["id"])
    c, _ = _client(on_execution=cb)
    c.handle_frame({"orderSubscriptionUpdate": {"executions":
                    [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]}})
    assert delivered == ["e2", "e3"]
    assert "e1" not in c._seen and {"e2", "e3"} <= c._seen


def test_handle_frame_crashed_event_is_redelivered():
    calls = []
    def cb(evt):
        calls.append(evt["id"])
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
    c, _ = _client(on_execution=cb)
    frame = {"orderSubscriptionUpdate": {"executions": [{"id": "e1"}]}}
    c.handle_frame(frame)
    c.handle_frame(frame)   # redelivery: e1 was never marked seen
    assert calls == ["e1", "e1"] and "e1" in c._seen


def test_on_open_clears_dedupe_set_and_resets_backoff():
    # MINOR: reconnect bounds memory (nothing is replayed) and resets the backoff to base.
    import config
    c, _ = _client()
    c._seen.update({"e1", "e2"})
    c._backoff = 999.0
    class FakeWS:
        def send(self, msg): pass
    c._on_open(FakeWS())
    assert c._seen == set()
    assert c._backoff == config.STREAM_RECONNECT_BASE


# ── subscribe payload + raw-event logging ───────────────────────────────────

def test_on_open_sends_subscribe_message():
    c, _ = _client()
    sent = []
    class FakeWS:
        def send(self, msg):
            sent.append(json.loads(msg))
    c._on_open(FakeWS())
    sub = sent[0]["subscribe"]
    assert sub["subscriptionType"] == "SUBSCRIPTION_TYPE_ORDER" and sub["marketSlugs"] == []


def test_on_message_appends_raw_event_and_dispatches(monkeypatch, tmp_path):
    import stream.polymarket_us_private_ws as pw
    monkeypatch.setattr(pw, "MAKER_EVENTS_FILE", str(tmp_path / "maker_events.jsonl"))
    c, events = _client()
    frame = {"orderSubscriptionUpdate": {"executions": [{"id": "e1"}]}}
    c._on_message(None, json.dumps(frame))
    assert events and events[0]["id"] == "e1"
    logged = (tmp_path / "maker_events.jsonl").read_text().strip()
    assert json.loads(logged)["orderSubscriptionUpdate"]["executions"][0]["id"] == "e1"


def test_on_message_tolerates_malformed_json():
    c, events = _client()
    c._on_message(None, "{not json")   # must not raise
    assert events == []


def test_append_event_failure_is_isolated(monkeypatch):
    import stream.polymarket_us_private_ws as pw
    monkeypatch.setattr(pw.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("no disk")))
    pw._append_event({"x": 1})   # must not raise despite the OSError


# ── socket-truth flag + reconnect hook ──────────────────────────────────────────

class _WSStub:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


def test_connected_flag_tracks_socket_lifecycle():
    calls = []
    c = PMUSPrivateWSClient(header_factory=lambda: {}, on_execution=lambda evt: None,
                            log=lambda *_: None, on_reconnect=lambda: calls.append(1))
    assert c.connected is False                       # constructed: no socket yet
    c._on_open(_WSStub())
    assert c.connected is True and calls == [1]       # connect -> flag up + owner sweep hook
    c._on_close(None)
    assert c.connected is False                       # venue closed the socket
    c._on_open(_WSStub())
    assert c.connected is True and calls == [1, 1]    # every (re)connect fires the hook
    c._on_error(None, RuntimeError("reset"))
    assert c.connected is False                       # transport error = down, whatever _running says
    c.stop()
    assert c.connected is False


def test_reconnect_hook_error_never_takes_down_ws_thread():
    c = PMUSPrivateWSClient(header_factory=lambda: {}, on_execution=lambda evt: None,
                            log=lambda *_: None, on_reconnect=lambda: 1 / 0)
    c._on_open(_WSStub())                             # hook raises inside -> swallowed + logged
    assert c.connected is True                        # the connection itself stays up
