"""RiskManager: capital capping, exposure ceiling (incl. resting), and the kill-switch.
TokenBucket: rate/burst math with an injected clock + the stats counters."""

import pytest

from execute.risk import RiskManager, TokenBucket

LIMITS = {"max_capital_per_trade": 500.0, "max_total_exposure": 1000.0}


def _opp(return_pct=5.0, liq_a=1000.0, liq_b=1000.0):
    return {"return_pct": return_pct, "bet_a": {"liquidity": liq_a}, "bet_b": {"liquidity": liq_b}}


def test_caps_capital_per_trade():
    d = RiskManager(LIMITS).check(_opp(), 5000.0)
    assert d.ok and d.capital == 500.0


def test_exposure_ceiling():
    rm = RiskManager(LIMITS)
    rm.record(900.0)
    d = rm.check(_opp(), 500.0)
    assert d.ok and d.capital == 100.0                   # only 100 of room left
    rm.record(100.0)
    assert not rm.check(_opp(), 100.0).ok                # full


def test_kill_switch_blocks_then_resets():
    rm = RiskManager(LIMITS)
    rm.trip()
    assert not rm.check(_opp(), 100.0).ok
    rm.reset()
    assert rm.check(_opp(), 100.0).ok


# ── resting exposure + hedge flag ───────────────────────────────────────────────

MAKER_LIMITS = {**LIMITS, "max_open_maker_orders": 2, "max_resting_exposure": 10.0}


def test_resting_record_release_floor_at_zero():
    rm = RiskManager(MAKER_LIMITS)
    rm.record_resting(4.0)
    assert rm.resting_exposure == 4.0
    rm.release_resting(6.0)
    assert rm.resting_exposure == 0.0                    # floored, never negative


def test_resting_caps_bind():
    rm = RiskManager(MAKER_LIMITS)
    assert rm.check_resting(9.0, 0).ok
    assert not rm.check_resting(11.0, 0).ok              # $ resting cap
    rm.record_resting(9.5)
    assert not rm.check_resting(1.0, 0).ok               # cap reached incrementally
    assert not rm.check_resting(0.1, 2).ok               # open-order count cap


def test_resting_missing_limits_fail_closed():
    assert not RiskManager(LIMITS).check_resting(1.0, 0).ok   # no maker limits -> reject


def test_resting_counts_against_total_exposure():
    rm = RiskManager(MAKER_LIMITS)
    rm.record_resting(9.0)
    rm.record(990.0)
    d = rm.check(_opp(), 500.0)
    assert d.ok and d.capital == 1.0                     # 1000 - 990 - 9 of room left
    assert not rm.check_resting(2.0, 0).ok               # resting side sees the same one book


def test_hedge_flag_never_bypasses_kill_or_exposure():
    # `hedge=True` marks a flattening trade at the call site — kill + exposure ALWAYS bind.
    rm = RiskManager(MAKER_LIMITS)
    hedge_opp = {"return_pct": 0.0, "bet_a": {}, "bet_b": {}}
    assert rm.check(hedge_opp, 10.0, hedge=True).ok      # clean book: hedge approved
    rm.trip()
    assert not rm.check(hedge_opp, 10.0, hedge=True).ok  # kill still enforced
    rm.reset()
    rm.record(1000.0)
    assert not rm.check(hedge_opp, 10.0, hedge=True).ok  # exposure still enforced


# ── TokenBucket: the global reprice-rate primitive ──────────────────────────────

def test_token_bucket_burst_then_refills_at_rate():
    t = {"now": 0.0}
    tb = TokenBucket(2.0, burst=4.0, clock=lambda: t["now"])
    assert tb.try_take(4.0)                    # full burst available at start
    assert not tb.try_take(1.0)                # drained
    t["now"] = 1.0                             # +2 tokens (rate 2/s)
    assert tb.try_take(2.0)
    assert not tb.try_take(0.5)
    t["now"] = 100.0                           # long idle: refill CAPS at burst, not rate*dt
    assert tb.try_take(4.0)
    assert not tb.try_take(0.1)


def test_token_bucket_fractional_takes_and_boundary():
    t = {"now": 0.0}
    tb = TokenBucket(1.0, burst=2.0, clock=lambda: t["now"])
    assert tb.try_take(2.0)                    # exact-boundary take succeeds (>= with epsilon)
    t["now"] = 0.5                             # +0.5 tokens
    assert tb.try_take(0.5)
    assert not tb.try_take(0.01)


def test_token_bucket_stats_counters():
    t = {"now": 0.0}
    tb = TokenBucket(3.0, burst=3.0, clock=lambda: t["now"])
    assert tb.try_take(2.0)
    assert not tb.try_take(2.0)                # only 1 left -> denied
    s = tb.stats()
    assert s["rate"] == 3.0 and s["burst"] == 3.0
    assert s["taken"] == 2.0 and s["denied"] == 1
    assert s["available"] == pytest.approx(1.0)
