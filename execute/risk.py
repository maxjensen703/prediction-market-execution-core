"""
execute/risk.py — risk rails and the kill switch. Construct ONE RiskManager per process
and route every order through it: it is the single risk book that placements, hedges and the
kill switch all share. The brokers do not consult it. Per-trade capital cap, total session
exposure cap, and a hard kill-switch. check() approves (and caps) capital or rejects with a reason
(hedge=True marks a flattening trade — kill + exposure still bind); record()/release()
track committed exposure; record_resting()/release_resting() track open-quote dollars
(one book: resting counts against total). TokenBucket is a global action-rate primitive
for a quoting loop — meter REPRICE actions with it, never hedges, kills or protective
cancels — and it is not thread-safe, so call it under your own lock.
"""

import time
from dataclasses import dataclass


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""
    capital: float = 0.0   # approved capital after capping


class RiskManager:
    def __init__(self, limits: dict):
        self.limits = dict(limits)
        self.exposure = 0.0
        self.resting_exposure = 0.0   # $ sum of price*qty across open maker quotes
        self.kill = False

    def check(self, opp: dict, requested_capital: float, hedge: bool = False) -> RiskDecision:
        """Kill-switch + capital caps on a would-be commitment (`opp` is accepted and unused).
        `hedge` marks flattening trades at the call site — kill and
        exposure ALWAYS bind, hedge or not (a hedge is not a new position, but it is money)."""
        if self.kill:
            return RiskDecision(False, "kill-switch engaged")
        cap = min(float(requested_capital), self.limits["max_capital_per_trade"])
        room = self.limits["max_total_exposure"] - self.exposure - self.resting_exposure
        if room <= 0:
            return RiskDecision(False, "max total exposure reached")
        cap = min(cap, room)
        if cap <= 0:
            return RiskDecision(False, "no capital available after limits")
        return RiskDecision(True, "", round(cap, 2))

    def check_resting(self, amount: float, open_orders: int) -> RiskDecision:
        """Gate one NEW maker quote of $`amount` given `open_orders` already resting.
        Fail-closed: limits absent from the dict reject (a maker needs maker limits)."""
        if self.kill:
            return RiskDecision(False, "kill-switch engaged")
        if open_orders >= self.limits.get("max_open_maker_orders", 0):
            return RiskDecision(False, f"max open maker orders ({self.limits.get('max_open_maker_orders', 0)}) reached")
        if self.resting_exposure + amount > self.limits.get("max_resting_exposure", 0.0) + 1e-9:
            return RiskDecision(False, f"resting exposure cap ${self.limits.get('max_resting_exposure', 0.0):.2f} reached")
        if self.exposure + self.resting_exposure + amount > self.limits["max_total_exposure"] + 1e-9:
            return RiskDecision(False, "max total exposure reached")
        return RiskDecision(True, "", round(amount, 2))

    def record(self, capital: float) -> None:
        self.exposure = round(self.exposure + capital, 2)

    def release(self, capital: float) -> None:
        self.exposure = round(max(0.0, self.exposure - capital), 2)

    def record_resting(self, amount: float) -> None:
        self.resting_exposure = round(self.resting_exposure + amount, 4)

    def release_resting(self, amount: float) -> None:
        self.resting_exposure = round(max(0.0, self.resting_exposure - amount), 4)

    def trip(self) -> None:
        self.kill = True

    def reset(self) -> None:
        self.kill = False


class TokenBucket:
    """Monotonic-clock token bucket. try_take(n) is non-blocking:
    True and debit, or False. Injectable clock for tests. NOT thread-safe on its own — call
    it under your own lock. Why it exists: PM US shares ~20 rps per IP between
    fetches and authed order calls; this bucket hard-caps the REPRICE burn so a wild night
    degrades to slower repricing, never to 429s starving the kill/hedge path."""

    def __init__(self, rate_per_s: float, burst: float, clock=time.monotonic):
        self.rate = float(rate_per_s)
        self.burst = float(burst)
        self._clock = clock
        self._available = float(burst)
        self._last = clock()
        self._taken = 0.0
        self._denied = 0

    def _refill(self) -> None:
        now = self._clock()
        self._available = min(self.burst, self._available + max(0.0, now - self._last) * self.rate)
        self._last = now

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if self._available + 1e-9 >= n:
            self._available -= n
            self._taken += n
            return True
        self._denied += 1
        return False

    def stats(self) -> dict:
        self._refill()
        return {"rate": self.rate, "burst": self.burst, "available": round(self._available, 3),
                "taken": self._taken, "denied": self._denied}
