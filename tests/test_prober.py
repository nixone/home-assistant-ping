"""Tests for the prober and connectivity tracker (no Home Assistant needed)."""

import asyncio

import pytest

from custom_components.ping_monitor import prober as prober_mod
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


def _prober(host: str, sock_factory, **kw) -> Prober:
    args = dict(interval=0.02, timeout=0.05, window=60, payload_size=0, ttl=64)
    return Prober(host, **{**args, **kw}, socket_factory=sock_factory)


async def test_stale_dns_address_is_replaced_quickly_while_failing(monkeypatch):
    """After a network change the host may resolve elsewhere; don't wait 5 minutes."""
    monkeypatch.setattr(prober_mod, "FAILING_RESOLVE_INTERVAL", 0.1)
    answers = {"addr": "192.0.2.1"}

    async def getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, "", (answers["addr"], 0))]

    sock = FakeIcmpSocket(lambda seq: None)  # nothing answers at the old address
    prober = _prober("router.lan", lambda: sock)
    prober.open(asyncio.get_running_loop())
    monkeypatch.setattr(prober._loop, "getaddrinfo", getaddrinfo)
    task = asyncio.create_task(prober.run())
    await asyncio.sleep(0.2)
    assert prober.address == "192.0.2.1"
    answers["addr"] = "192.0.2.99"
    sock.responder = lambda seq: 0.001
    await asyncio.sleep(0.4)
    assert prober.address == "192.0.2.99"
    received = prober.received
    await asyncio.sleep(0.2)
    task.cancel()
    prober.close()
    assert prober.received > received


async def test_hung_resolver_does_not_stall_probing(monkeypatch):
    monkeypatch.setattr(prober_mod, "RESOLVE_INTERVAL", 0.05)
    calls = 0

    async def getaddrinfo(host, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.sleep(3600)  # resolver hangs after the first lookup
        return [(None, None, None, "", ("192.0.2.1", 0))]

    sock = FakeIcmpSocket()
    prober = _prober("router.lan", lambda: sock)
    prober.open(asyncio.get_running_loop())
    monkeypatch.setattr(prober._loop, "getaddrinfo", getaddrinfo)
    task = asyncio.create_task(prober.run())
    await asyncio.sleep(0.5)
    task.cancel()
    prober.close()
    assert calls == 2
    assert prober.sent >= 15 and prober.received >= 10


async def test_callback_error_does_not_kill_probing(monkeypatch):
    """Before, one exception here ended the task and froze all sensors until a reload."""

    async def getaddrinfo(*args, **kwargs):
        raise OSError("no network")  # no address: probes complete inside the send loop

    def boom(probe):
        raise RuntimeError("listener bug")

    prober = _prober("router.lan", FakeIcmpSocket)
    prober._on_result = boom
    prober.open(asyncio.get_running_loop())
    monkeypatch.setattr(prober._loop, "getaddrinfo", getaddrinfo)
    task = asyncio.create_task(prober.run())
    await asyncio.sleep(0.2)
    assert not task.done()
    task.cancel()
    prober.close()
    assert prober.sent >= 5


async def test_socket_is_reopened_while_nothing_gets_through(monkeypatch):
    monkeypatch.setattr(prober_mod, "REOPEN_AFTER", 0.1)
    broken = FakeIcmpSocket(lambda seq: None)
    healthy = FakeIcmpSocket()
    sockets = iter([broken, healthy])
    prober = _prober("127.0.0.1", lambda: next(sockets))
    await _run(prober, 0.5)
    prober.close()
    assert broken.closed and healthy.closed
    assert prober.socket_reopens == 1
    assert healthy.ttl == 64 and healthy.sent
    assert prober.received > 0
