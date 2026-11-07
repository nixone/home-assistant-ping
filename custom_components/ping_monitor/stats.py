"""Pure statistics over ping samples. No Home Assistant imports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import itertools
import math


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Aggregated statistics over one window of completed probes."""

    sent: int
    received: int
    lost: int
    loss_pct: float | None
    rtt_min: float | None
    rtt_avg: float | None
    rtt_median: float | None
    rtt_max: float | None
    rtt_p95: float | None
    rtt_p99: float | None
    rtt_stddev: float | None
    jitter: float | None
    longest_loss_burst: int


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (same method as numpy's default)."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def compute(results: Sequence[float | None]) -> WindowStats:
    """Compute window statistics.

    `results` holds one entry per completed probe in send order: the RTT in
    milliseconds, or None if the probe was lost.
    """
    sent = len(results)
    rtts = [r for r in results if r is not None]
    received = len(rtts)
    lost = sent - received

    longest = run = 0
    for r in results:
        run = run + 1 if r is None else 0
        longest = max(longest, run)

    if not rtts:
        return WindowStats(
            sent=sent,
            received=0,
            lost=lost,
            loss_pct=100.0 if sent else None,
            rtt_min=None,
            rtt_avg=None,
            rtt_median=None,
            rtt_max=None,
            rtt_p95=None,
            rtt_p99=None,
            rtt_stddev=None,
            jitter=None,
            longest_loss_burst=longest,
        )

    ordered = sorted(rtts)
    avg = sum(rtts) / received
    # Population standard deviation (what `ping` reports as mdev-like spread).
    stddev = math.sqrt(sum((r - avg) ** 2 for r in rtts) / received)
    # Jitter: mean absolute difference between consecutive received RTTs.
    jitter = (
        sum(abs(b - a) for a, b in itertools.pairwise(rtts)) / (received - 1)
        if received > 1
        else None
    )

    return WindowStats(
        sent=sent,
        received=received,
        lost=lost,
        loss_pct=lost * 100.0 / sent,
        rtt_min=ordered[0],
        rtt_avg=avg,
        rtt_median=percentile(ordered, 50),
        rtt_max=ordered[-1],
        rtt_p95=percentile(ordered, 95),
        rtt_p99=percentile(ordered, 99),
        rtt_stddev=stddev,
        jitter=jitter,
        longest_loss_burst=longest,
    )
