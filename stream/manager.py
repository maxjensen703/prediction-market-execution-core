"""
stream/manager.py — live books. StreamManager wires the venue
WS clients (kalshi_ws.py, polymarket_us_ws.py) to a shared BookStore (book_store.py), and
runs the runtime WS-vs-REST self-audit that decides whether a caller may open NEW exposure
off the stream (hedging a real fill should never be gated on it).

Earned-trust audit narrative: the store's `healthy` flag starts False (UNTRUSTED) at
process start — nothing has been compared yet, so fires use REST last-look, the known-good
slower path. Every STREAM_AUDIT_SECONDS, audit_once() samples currently-subscribed markets,
compares the streamed top-of-book ask to a fresh REST quote on BOTH venues, and computes
the fraction agreeing within 1 cent. The store is PROMOTED to healthy once that fraction
reaches STREAM_AUDIT_MIN_AGREE, and DEMOTED back to unhealthy the moment a
later audit cycle falls below it (this is the 26c-divergence lesson — a temporarily-correct
WS book can still drift, so trust is re-earned continuously, not granted once). Every
consumer must check `store.healthy` itself and fall back to REST/None when it's False —
fail-closed, never a stale or unverified price behind an order.

CLI:  .venv/bin/python -m stream.manager --league mlb --seconds 90
"""

import argparse
import statistics
import sys
import threading
import time

import httpx

import config
from fetch import kalshi as kalshi_main
from fetch.polymarket_us import fetch_polymarket_us_all, fetch_book, _book_quote, _num
from .book_store import BookStore
from .kalshi_ws import KalshiWSClient
from .polymarket_us_ws import PMUSWSClient

MAX_MARKETS_PER_VENUE = 60   # cap subscriptions during validation (keeps REST sampling light)


def _kalshi_header_factory(kb):
    def make():
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": kb.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": kb._sign("GET", config.KALSHI_WS_PATH, ts),
                "KALSHI-ACCESS-TIMESTAMP": ts}
    return make


def _pmus_header_factory(pb):
    return lambda: pb._headers("GET", config.PM_US_WS_PATH)


class StreamManager:
    def __init__(self, store: BookStore | None = None, log=print, on_book_change=None):
        self.store = store or BookStore()
        self._log = log
        # Threaded into both WS clients so every book publish reaches the caller's
        # on_book_change hook; ensure()'s reconnects rebuild clients with the same callback.
        self._on_book_change = on_book_change
        self.kalshi: KalshiWSClient | None = None
        self.pmus: PMUSWSClient | None = None
        self._subscribed: tuple = (frozenset(), frozenset())

    def ensure(self, kalshi_tickers, pmus_slugs, cap: int = 500) -> None:
        """Idempotent: (re)start the WS clients only when the market set actually grew, so the
        server can call this every scan cheaply. New markets -> reconnect with the union."""
        want = (frozenset(kalshi_tickers) | self._subscribed[0],
                frozenset(pmus_slugs) | self._subscribed[1])
        running = bool(self.kalshi or self.pmus)
        if running and want == self._subscribed:
            return
        self.stop()
        self.kalshi = self.pmus = None
        self._subscribed = want
        self.start(list(want[0])[:cap], list(want[1])[:cap])

    def discover(self, leagues, bet_types=None) -> tuple[list[str], list[str]]:
        """REST scan once to learn which Kalshi tickers + PM US slugs to subscribe to (also
        populates the side maps as a side effect). Returns (kalshi_tickers, pmus_slugs)."""
        k_ml, k_tot, k_spr = kalshi_main.fetch_kalshi_all(leagues, bet_types)
        p_ml, p_tot, p_spr = fetch_polymarket_us_all(leagues, bet_types)
        ktix = [l.market_id for l in (k_ml + k_tot + k_spr)]
        pslugs = [l.market_id for l in (p_ml + p_tot + p_spr)]
        ktix = list(dict.fromkeys(ktix))[:MAX_MARKETS_PER_VENUE]
        pslugs = list(dict.fromkeys(pslugs))[:MAX_MARKETS_PER_VENUE]
        self._log(f"[stream] discovered {len(ktix)} kalshi tickers, {len(pslugs)} pm us slugs")
        return ktix, pslugs

    def start(self, kalshi_tickers, pmus_slugs, dump: int = 0) -> None:
        from execute.kalshi_broker import KalshiBroker
        from execute.polymarket_us_broker import PolymarketUSBroker
        kb, pb = KalshiBroker(), PolymarketUSBroker()
        if kalshi_tickers and kb.configured:
            self.kalshi = KalshiWSClient(self.store, _kalshi_header_factory(kb), kalshi_tickers,
                                         self._log, dump, on_book_change=self._on_book_change)
            self.kalshi.start()
        elif kalshi_tickers:
            self._log("[stream] kalshi creds missing — skipping kalshi WS")
        if pmus_slugs and pb.configured:
            self.pmus = PMUSWSClient(self.store, _pmus_header_factory(pb), pmus_slugs,
                                     self._log, dump, on_book_change=self._on_book_change)
            self.pmus.start()
        elif pmus_slugs:
            self._log("[stream] pm us creds missing — skipping pm us WS")

    def stop(self) -> None:
        for c in (self.kalshi, self.pmus):
            if c:
                c.stop()

    # ── continuous self-audit (runtime accuracy watchdog) ─────────────────────
    def audit_once(self, kb, client, sample: int = 12) -> dict:
        """Sample currently-subscribed markets, compare the streamed top-of-book to a REST quote,
        and set the store's health flag. While healthy the engine trusts WS; when it diverges the
        engine falls back to REST automatically. Compares only where BOTH sources have a price."""
        agree = total = 0
        ktix, pslugs = list(self._subscribed[0]), list(self._subscribed[1])
        for venue, ids, restfn in (("kalshi", ktix[:sample], lambda t: self._kalshi_rest(kb, t)),
                                   ("polymarket_us", pslugs[:sample], lambda s: self._pmus_rest(s, client))):
            for mid in ids:
                rest, lb = restfn(mid), self.store.get(venue, mid)
                if rest is None or lb is None:
                    continue
                for ws_ask, rest_ask in ((lb.ask_yes, rest[0]), (lb.ask_no, rest[1])):
                    if ws_ask is None or rest_ask is None:
                        continue
                    total += 1
                    if abs(ws_ask - rest_ask) <= 0.01:
                        agree += 1
        frac = (agree / total) if total else None
        # earned trust: only a REAL comparison moves health. With data -> agreement decides;
        # with no data this cycle (books warming up) -> keep current (starts UNTRUSTED -> REST).
        healthy = (frac >= config.STREAM_AUDIT_MIN_AGREE) if total else self.store.healthy
        audit = {"compared": total, "within_1c": round(frac, 3) if frac is not None else None, "healthy": healthy}
        self.store.set_health(healthy, audit)
        return audit

    def start_audit(self) -> None:
        """Background watchdog: every STREAM_AUDIT_SECONDS, re-check WS-vs-REST and (un)flag health."""
        from execute.kalshi_broker import KalshiBroker
        kb = KalshiBroker()

        def loop():
            while True:
                time.sleep(config.STREAM_AUDIT_SECONDS)
                try:
                    with httpx.Client(timeout=config.PM_US_TIMEOUT) as c:
                        a = self.audit_once(kb, c)
                    if a["compared"] and not a["healthy"]:
                        self._log(f"[audit] STREAM UNHEALTHY {a} -> fires fall back to REST")
                except Exception:
                    pass
        threading.Thread(target=loop, name="ws-audit", daemon=True).start()

    # ── shadow validation ───────────────────────────────────────────────────
    def run_shadow(self, kalshi_tickers, pmus_slugs, seconds=90, warmup=8) -> dict:
        """Let the books warm up, then repeatedly compare WS vs REST and report agreement.
        Agreement within ~1 tick = the streamed book is trustworthy; large gaps or many
        WS-missing = a parsing/subscription bug to fix BEFORE the fire path reads the stream."""
        from execute.kalshi_broker import KalshiBroker
        kb = KalshiBroker()
        self._log(f"[shadow] warming up {warmup}s ...")
        time.sleep(warmup)
        acc = {v: {"diffs": [], "signed": [], "missing": 0, "samples": 0, "offenders": []}
               for v in ("kalshi", "polymarket_us")}
        deadline = _mono() + seconds
        with httpx.Client(timeout=config.PM_US_TIMEOUT) as pmc:
            while _mono() < deadline:
                for tk in kalshi_tickers[:25]:
                    self._sample("kalshi", tk, self._kalshi_rest(kb, tk), acc["kalshi"])
                for sl in pmus_slugs[:25]:
                    self._sample("polymarket_us", sl, self._pmus_rest(sl, pmc), acc["polymarket_us"])
                self._log("[shadow] " + self._line(acc)
                          + " | " + str(self.store.stats(config.STREAM_STALE_SECONDS)))
                time.sleep(5)
        report = {v: self._summary(acc[v]) for v in acc}
        self._log("\n=== SHADOW REPORT (ws vs REST) ===")
        for v, r in report.items():
            self._log(f"  {v:14s} {r}")
        self._report_offenders(acc)
        self._log("\n  verdict: within_1c high + missing low + disagreements that are FRESH/moving (not")
        self._log("  stale, not one-directional) => ws is leading REST = good. stale/biased => a bug.")
        return report

    def _sample(self, venue, mid, rest, a) -> None:
        if rest is None:
            return
        lb = self.store.get(venue, mid)
        for label, ws_ask, rest_ask in (("yes", getattr(lb, "ask_yes", None) if lb else None, rest[0]),
                                        ("no", getattr(lb, "ask_no", None) if lb else None, rest[1])):
            if rest_ask is None:
                continue
            a["samples"] += 1
            if ws_ask is None:
                a["missing"] += 1
                continue
            d = ws_ask - rest_ask
            a["diffs"].append(abs(d))
            a["signed"].append(d)
            if abs(d) > 0.01:
                age = lb.age() if lb else None
                a["offenders"].append({"mkt": mid[-24:], "side": label, "ws": round(ws_ask, 4),
                                       "rest": round(rest_ask, 4), "diff_c": round(d * 100, 1),
                                       "age_s": round(age, 2) if age is not None else None})

    @staticmethod
    def _kalshi_rest(kb, ticker):
        try:
            d = kb._request("GET", f"/markets/{ticker}")
            m = d.get("market", d)
            return _num(m.get("yes_ask_dollars")), _num(m.get("no_ask_dollars"))
        except Exception:
            return None

    @staticmethod
    def _pmus_rest(slug, client):
        md = fetch_book(slug, client)
        if md is None:
            return None
        ba, bb, _, _ = _book_quote(md)
        return (round(ba, 4) if ba is not None else None,
                round(1.0 - bb, 4) if bb is not None else None)

    @staticmethod
    def _line(acc) -> str:
        parts = []
        for v, a in acc.items():
            w1 = sum(1 for d in a["diffs"] if d <= 0.01)
            parts.append(f"{v[:3]} n={a['samples']} miss={a['missing']} <=1c={w1}/{len(a['diffs'])}")
        return " | ".join(parts)

    @staticmethod
    def _summary(a) -> dict:
        diffs, samples = a["diffs"], a["samples"]
        if not samples:
            return {"samples": 0}
        return {
            "samples": samples, "ws_present": len(diffs), "ws_missing": a["missing"],
            "median_abs_diff_c": round(statistics.median(diffs) * 100, 2) if diffs else None,
            "p90_abs_diff_c": round(sorted(diffs)[int(len(diffs) * 0.9)] * 100, 2) if diffs else None,
            "mean_signed_c": round(statistics.mean(a["signed"]) * 100, 2) if a["signed"] else None,
            "within_1c": round(sum(1 for d in diffs if d <= 0.01) / len(diffs), 3) if diffs else None,
            "within_2c": round(sum(1 for d in diffs if d <= 0.02) / len(diffs), 3) if diffs else None,
        }

    def _report_offenders(self, acc, top=12) -> None:
        """Show the worst ws-vs-REST gaps with the ws book's age — the tell for freshness vs bug."""
        for v, a in acc.items():
            offs = a["offenders"]
            if not offs:
                continue
            fresh = sum(1 for o in offs if o["age_s"] is not None and o["age_s"] <= config.STREAM_STALE_SECONDS)
            self._log(f"\n  {v} disagreements >1c: {len(offs)} | on a FRESH ws book: {fresh}/{len(offs)}")
            for o in sorted(offs, key=lambda x: abs(x["diff_c"]), reverse=True)[:top]:
                self._log(f"     {o['mkt']:26s} {o['side']:3s} ws={o['ws']:.3f} rest={o['rest']:.3f} "
                          f"diff={o['diff_c']:+.1f}c age={o['age_s']}s")


def _mono() -> float:
    return time.monotonic()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Shadow-validate the WS order-book stream vs REST.")
    ap.add_argument("--league", action="append", choices=config.LEAGUES, help="leagues (repeatable)")
    ap.add_argument("--seconds", type=float, default=90, help="shadow sampling duration")
    ap.add_argument("--dump", type=int, default=0,
                    help="print this many raw WS messages PER VENUE then exit (schema discovery)")
    args = ap.parse_args(argv)
    leagues = args.league or list(config.ACTIVE_LEAGUES)

    mgr = StreamManager()
    ktix, pslugs = mgr.discover(leagues)
    if not ktix and not pslugs:
        print("nothing to stream (no markets discovered)")
        return 1
    mgr.start(ktix, pslugs, dump=args.dump)
    try:
        if args.dump:
            print(f"[dump] collecting ~{args.dump} raw msgs/venue for ~15s ...")
            time.sleep(15)
        else:
            mgr.run_shadow(ktix, pslugs, seconds=args.seconds)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        mgr.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
