"""Lightweight timing instrumentation: named phase wall-clocks + per-call latency
samples (count/total/p50/p95). Zero deps; collection is gated by the PERF_LOG env
flag, so when off both context managers are near no-ops and the hot path is unchanged.
Served via /api/perf and printed in the summary at the end of `python -m pipeline`."""

import os
import time
from contextlib import contextmanager

_enabled = os.environ.get("PERF_LOG", "").strip().lower() in ("1", "true", "yes", "on")
_phases: dict[str, float] = {}        # name -> summed seconds
_samples: dict[str, list[float]] = {}  # bucket -> per-call seconds


def enabled() -> bool:
    return _enabled


@contextmanager
def phase(name: str):
    """Wall-clock a named phase (summed across calls with the same name)."""
    if not _enabled:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _phases[name] = _phases.get(name, 0.0) + (time.perf_counter() - t0)


@contextmanager
def sample(bucket: str):
    """Record one call's latency into a bucket (for count/p50/p95)."""
    if not _enabled:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _samples.setdefault(bucket, []).append(time.perf_counter() - t0)


def _pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile in milliseconds."""
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round((q / 100.0) * (len(s) - 1)))))
    return s[i] * 1000.0


def stats() -> dict:
    """JSON-friendly snapshot: phase wall-times + per-bucket count/total/p50/p95 (ms)."""
    return {
        "enabled": _enabled,
        "phases_ms": {k: round(v * 1000, 1) for k, v in _phases.items()},
        "calls": {b: {"n": len(xs), "total_ms": round(sum(xs) * 1000, 1),
                      "p50_ms": round(_pct(xs, 50), 1), "p95_ms": round(_pct(xs, 95), 1)}
                  for b, xs in _samples.items()},
    }


def summary() -> str:
    """Human-readable report of phase wall-times and per-bucket call latencies."""
    out = ["── perf ──────────────────────────────────────────────"]
    if _phases:
        out.append("phases (wall):")
        for name, secs in sorted(_phases.items(), key=lambda kv: -kv[1]):
            out.append(f"  {name:<28} {secs*1000:8.0f} ms")
    if _samples:
        out.append("per-call latency (count · total · p50 · p95):")
        for bucket, xs in sorted(_samples.items(), key=lambda kv: -sum(kv[1])):
            tot = sum(xs) * 1000.0
            out.append(f"  {bucket:<20} n={len(xs):<4} total={tot:7.0f} ms  "
                       f"p50={_pct(xs,50):5.0f} ms  p95={_pct(xs,95):5.0f} ms")
    if not _phases and not _samples:
        out.append("  (no samples — set PERF_LOG=1 to enable collection)")
    return "\n".join(out)
