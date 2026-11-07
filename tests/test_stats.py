"""Tests for the pure statistics module."""

import math

import pytest

from custom_components.ping_monitor.stats import compute, percentile


def test_empty():
    s = compute([])
    assert s.sent == 0
    assert s.loss_pct is None
    assert s.rtt_avg is None
    assert s.longest_loss_burst == 0


def test_all_lost():
    s = compute([None, None, None])
    assert (s.sent, s.received, s.lost) == (3, 0, 3)
    assert s.loss_pct == 100.0
    assert s.rtt_min is None and s.jitter is None
    assert s.longest_loss_burst == 3


def test_mixed():
    s = compute([10.0, None, 20.0, 30.0, None, None, 40.0])
    assert (s.sent, s.received, s.lost) == (7, 4, 3)
    assert s.loss_pct == pytest.approx(300 / 7)
    assert s.rtt_min == 10.0 and s.rtt_max == 40.0
    assert s.rtt_avg == 25.0
    assert s.rtt_median == 25.0
    assert s.rtt_stddev == pytest.approx(math.sqrt(125))
    assert s.jitter == 10.0  # consecutive received RTTs differ by 10
    assert s.longest_loss_burst == 2


def test_single_reply_has_no_jitter():
    s = compute([5.0])
    assert s.jitter is None
    assert s.rtt_p95 == 5.0


def test_percentile_matches_numpy_linear():
    values = sorted(float(v) for v in range(1, 101))
    assert percentile(values, 50) == 50.5
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile(values, 99) == pytest.approx(99.01)
    assert percentile([1.0, 2.0], 100) == 2.0
