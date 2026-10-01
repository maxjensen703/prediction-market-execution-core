"""
stream/polymarket_us_private_ws.py — fill notifications, feeding a caller's on_execution
callback (for example, fill -> hedge).
Polymarket US PRIVATE order-events WebSocket client. Mirrors
polymarket_us_ws.py's structure but on the authenticated order stream: snapshot then
per-order updates, surfacing each execution to a caller callback. No sequence numbers exist,
so the client is stateless beyond an execution-id dedupe set; the owner reconciles on reconnect.
"""

import json
import os
import threading
import time

import config

MAKER_EVENTS_FILE = "data/maker_events.jsonl"   # every raw frame, append-only (audit trail of venue order events)


def _extract_executions(msg: dict) -> list:
    """Pull execution payloads out of an orderSubscriptionUpdate frame; [] if none. The real
    orderSubscriptionSnapshot carries `orders` + `eof` (no executions) — the client ignores it
    (eof unused) and the owner reconciles via open_orders(); the snapshot key is still scanned
    only as tolerance in case executions ever appear there."""
    for key in ("orderSubscriptionUpdate", "orderSubscriptionSnapshot"):
        body = msg.get(key)
        if isinstance(body, dict):
            execs = body.get("executions") or body.get("execution")
            if isinstance(execs, dict):
                return [execs]
            if isinstance(execs, list):
                return execs
    execs = msg.get("executions")
    return execs if isinstance(execs, list) else []


def _append_event(raw: dict) -> None:
    """Failure-isolated append of the raw frame to MAKER_EVENTS_FILE."""
    try:
        os.makedirs(os.path.dirname(MAKER_EVENTS_FILE) or ".", exist_ok=True)
        with open(MAKER_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(raw, default=str) + "\n")
    except Exception:
        pass


class PMUSPrivateWSClient:
    """Authenticated PM US private order-events connection. The snapshot carries orders+eof
    (not executions) and is ignored — the owner reconciles via open_orders() on (re)connect.
    Updates' executions are deduped by id and handed to on_execution, which runs ON THE WS
    THREAD (keep it fast; hand off heavy work). An execution is marked seen only AFTER
    on_execution returns cleanly, so a crashed callback gets the event redelivered rather
    than silently lost (the owner dedupes/hedges by qty delta)."""

    VENUE = "polymarket_us"

    def __init__(self, header_factory, on_execution, log=print, market_slugs=None,
                 on_reconnect=None):
        self._headers = header_factory          # () -> {X-PM-*: ...} signed fresh per connect
        self._on_execution = on_execution        # (evt: dict) -> None; runs on the WS thread
        self._on_reconnect = on_reconnect        # () -> None; fires on every (re)connect
        self._log = log
        self._slugs = list(market_slugs or [])   # empty = all markets
        self._seen: set[str] = set()
        self._backoff = config.STREAM_RECONNECT_BASE
        self._running = False
        # `_running` means "the reconnect loop exists" and stays True through a real
        # outage — `connected` is the SOCKET truth an owner's polling fallback must read
        # (fills during a gap are invisible to anyone who trusts _running instead).
        self.connected = False
        self._thread: threading.Thread | None = None
        self._ws = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="pmus-private-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self.connected = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self) -> None:
        import certifi
        import websocket
        self._backoff = config.STREAM_RECONNECT_BASE
        while self._running:
            try:
                hdr = [f"{k}: {v}" for k, v in self._headers().items()]
                self._ws = websocket.WebSocketApp(
                    config.PM_US_PRIVATE_WS_URL, header=hdr,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close)
                self._ws.run_forever(ping_interval=10, ping_timeout=5,
                                     sslopt={"ca_certs": certifi.where()})
            except Exception as e:
                self._log(f"[pmus-private-ws] run error: {e}")
            self.connected = False   # run_forever returned: socket is down whatever the cause
            if not self._running:
                break
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, config.STREAM_RECONNECT_CAP)

    def _on_open(self, ws) -> None:
        self._log("[pmus-private-ws] connected; subscribing order events")
        self._seen.clear()   # fresh connection: nothing is replayed, so this only bounds memory
        self._backoff = config.STREAM_RECONNECT_BASE   # successful connect -> reset the backoff
        self.connected = True
        ws.send(json.dumps({"subscribe": {"requestId": "xb-maker", "subscriptionType":
                            "SUBSCRIPTION_TYPE_ORDER", "marketSlugs": self._slugs}}))
        if self._on_reconnect is not None:
            try:   # nothing is replayed on reconnect — the owner should sweep open orders via
                   # get_order to catch any fill that landed in the gap.
                self._on_reconnect()
            except Exception as e:   # the hook must never take down the WS thread
                self._log(f"[pmus-private-ws] on_reconnect hook error: {e}")

    def _on_error(self, _ws, e) -> None:
        self.connected = False
        self._log(f"[pmus-private-ws] error: {e}")

    def _on_close(self, _ws, *a) -> None:
        self.connected = False
        self._log("[pmus-private-ws] closed")

    def _on_message(self, ws, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception as e:
            self._log(f"[pmus-private-ws] parse error: {e} | sample: {raw[:400]}")
            return
        _append_event(data)
        self.handle_frame(data)

    def handle_frame(self, data: dict) -> None:
        """Process one decoded frame: dedupe executions by id, then invoke on_execution per
        event with PER-EVENT isolation — a crashing callback logs, leaves its event UNSEEN
        (redelivery beats silent loss) and continues to the next event. No raise ever."""
        if not isinstance(data, dict):
            return
        try:
            events = _extract_executions(data)
        except Exception as e:
            self._log(f"[pmus-private-ws] handle error: {e}")
            return
        for evt in events:
            if not isinstance(evt, dict):
                continue
            eid = str(evt.get("id") or "")
            if not eid or eid in self._seen:
                continue
            try:
                self._on_execution(evt)
            except Exception as e:
                self._log(f"[pmus-private-ws] on_execution error for {eid}: {e}")
                continue   # eid NOT marked seen -> eligible for redelivery
            self._seen.add(eid)   # seen only after a clean callback return
