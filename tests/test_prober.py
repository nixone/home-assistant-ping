"""Tests for the prober and connectivity tracker (no Home Assistant needed)."""

import asyncio

import pytest

from custom_components.ping_monitor.prober import ConnectivityTracker, Prober

from .fakes import FakeIcmpSocket


async def _run(prober: Prober, seconds: float) -> None:
    prober.open(asyncio.get_running_loop())
    task = asyncio.create_task(prober.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_counts_loss_and_rtt():
    sock = FakeIcmpSocket(lambda seq: None if seq % 4 == 0 else 0.005)
    results = []
    prober = Prober(
        "127.0.0.1",
        interval=0.02,
        timeout=0.05,
        window=60,
        payload_size=100,
        ttl=42,
        socket_factory=lambda: sock,
        on_result=results.append,
    )
    await _run(prober, 0.5)
    await asyncio.sleep(0.1)  # let pending probes time out
    prober.close()

    assert sock.ttl == 42
    assert {size for _, _, size in sock.sent} == {100}
    assert prober.sent == len(sock.sent) >= 15
    assert prober.received + prober.lost == prober.sent
    expected_lost = sum(1 for _, seq, _ in sock.sent if seq % 4 == 0)
    assert prober.lost == expected_lost
    stats = prober.window_stats()
    assert stats.sent == prober.sent
    assert stats.loss_pct == pytest.approx(100 * expected_lost / prober.sent)
    assert 4 <= stats.rtt_min <= stats.rtt_max < 50


async def test_late_reply_counts_as_lost():
    sock = FakeIcmpSocket(lambda seq: 0.1)  # always later than the timeout
    prober = Prober(
        "127.0.0.1",
        interval=0.02,
        timeout=0.03,
        window=60,
        payload_size=0,
        ttl=64,
        socket_factory=lambda: sock,
    )
    await _run(prober, 0.2)
    await asyncio.sleep(0.15)
    prober.close()
    assert prober.received == 0
    assert prober.lost == prober.sent > 0


async def test_window_excludes_pending_and_old_probes():
    sock = FakeIcmpSocket(lambda seq: 0.001)
    prober = Prober(
        "127.0.0.1",
        interval=0.01,
        timeout=0.5,
        window=0.1,
        payload_size=0,
        ttl=64,
        socket_factory=lambda: sock,
    )
    await _run(prober, 0.4)
    stats = prober.window_stats()
    prober.close()
    assert prober.sent >= 30
    assert 0 < stats.sent <= 12  # only ~0.1 s worth of probes
    assert stats.loss_pct == 0


async def test_unresolvable_host_counts_as_loss():
    sock = FakeIcmpSocket()
    prober = Prober(
        "does-not-exist.invalid",
        interval=0.02,
        timeout=0.05,
        window=60,
        payload_size=0,
        ttl=64,
        socket_factory=lambda: sock,
    )
    await _run(prober, 0.1)
    prober.close()
    assert sock.sent == []
    assert prober.lost == prober.sent > 0
    assert prober.window_stats().loss_pct == 100.0


def test_tracker_hysteresis():
    t = ConnectivityTracker(down_after=3, up_after=2)
    assert t.update(True, 0) is None and t.connected is None
    up = t.update(True, 1)
    assert up.connected and up.outage_started is None and t.connected

    assert t.update(False, 2) is None
    assert t.update(False, 3) is None
    assert t.update(True, 4) is None  # streak broken
    assert t.update(False, 5) is None
    assert t.update(False, 6) is None
    down = t.update(False, 7)
    assert not down.connected and down.outage_started == 5 and t.connected is False

    assert t.update(False, 8) is None
    assert t.update(True, 9) is None  # needs 2 good ones
    assert t.update(False, 10) is None
    assert t.update(True, 11) is None
    rec = t.update(True, 12)
    assert rec.connected and rec.at == 11 and rec.outage_started == 5
    assert rec.lost_packets == 5  # 5, 6, 7, 8, 10
    assert t.outage_started is None


async def test_close_stops_run_loop():
    sock = FakeIcmpSocket()
    prober = Prober(
        "127.0.0.1",
        interval=0.01,
        timeout=0.05,
        window=60,
        payload_size=0,
        ttl=64,
        socket_factory=lambda: sock,
    )
    prober.open(asyncio.get_running_loop())
    task = asyncio.create_task(prober.run())
    await asyncio.sleep(0.05)
    prober.close()
    await asyncio.wait_for(task, 1)  # returns on its own, no exception
    assert sock.closed
