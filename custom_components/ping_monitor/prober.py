"""Continuous ICMP prober with a rolling sample window. No Home Assistant imports."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import ipaddress
import logging
import os
import socket

from .icmp import IcmpSocket, IcmpSocketError
from .stats import WindowStats, compute

_LOGGER = logging.getLogger(__name__)

RESOLVE_INTERVAL = 300.0  # seconds between DNS re-resolutions while healthy
FAILING_RESOLVE_INTERVAL = 10.0  # ... while no replies arrive (or no address yet)
RESOLVE_TIMEOUT = 10.0  # a hung resolver must not stall probing
REOPEN_AFTER = 30.0  # recreate the socket after this long without a reply


@dataclass(slots=True)
class Probe:
    """One echo request and its outcome."""

    seq: int
    sent_at: float  # event-loop monotonic time, for windowing
    sent_ns: int = 0  # wall-clock ns, for RTT (matches kernel receive stamps)
    rtt: float | None = None  # ms; None = lost (or still pending)
    done: bool = False
    timer: asyncio.TimerHandle | None = field(default=None, repr=False)


@dataclass(slots=True)
class Transition:
    """A connectivity change detected by ConnectivityTracker."""

    connected: bool
    at: float  # event-loop time
    # When the outage began (first lost probe). None for the initial "up".
    outage_started: float | None = None
    lost_packets: int = 0  # probes lost during the outage, set on recovery


class ConnectivityTracker:
    """Up/down state with hysteresis over consecutive probe results."""

    def __init__(self, down_after: int, up_after: int) -> None:
        self.down_after = down_after
        self.up_after = up_after
        self.connected: bool | None = None  # unknown until enough samples
        self.outage_started: float | None = None
        self._consecutive_lost = 0
        self._consecutive_ok = 0
        self._first_lost_at: float | None = None
        self._first_ok_at = 0.0
        self._outage_lost = 0

    def update(self, ok: bool, sent_at: float) -> Transition | None:
        """Feed one result (in completion order). Return a transition, if any."""
        if ok:
            self._consecutive_ok += 1
            if self._consecutive_ok == 1:
                self._first_ok_at = sent_at
            self._consecutive_lost = 0
            self._first_lost_at = None
            if self.connected is not True and self._consecutive_ok >= self.up_after:
                # The outage ended with the first good probe of the streak.
                tr = Transition(True, self._first_ok_at, self.outage_started, self._outage_lost)
                self.connected = True
                self.outage_started = None
                self._outage_lost = 0
                return tr
            return None

        self._consecutive_lost += 1
        self._consecutive_ok = 0
        if self.connected is False:
            self._outage_lost += 1
            return None
        if self._first_lost_at is None:
            self._first_lost_at = sent_at
        if self._consecutive_lost >= self.down_after:
            self.connected = False
            # The outage started with the first lost probe, not when we noticed.
            self.outage_started = self._first_lost_at
            self._outage_lost = self._consecutive_lost
            return Transition(False, sent_at, outage_started=self.outage_started)
        return None


class Prober:
    """Sends one echo request every `interval` seconds and records outcomes."""

    def __init__(
        self,
        host: str,
        *,
        interval: float,
        timeout: float,
        window: float,
        payload_size: int,
        ttl: int,
        socket_factory: Callable[[], IcmpSocket] | None = None,
        on_result: Callable[[Probe], None] | None = None,
    ) -> None:
        self.host = host
        self.interval = interval
        self.timeout = timeout
        self.window = window
        self.ttl = ttl
        self.payload = bytes(i & 0xFF for i in range(payload_size))
        self._socket_factory = socket_factory
        self._on_result = on_result

        self.address: str | None = None
        self.socket_mode: str | None = None
        self.sent = 0
        self.received = 0
        self.lost = 0
        self.last_rtt: float | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._sock: IcmpSocket | None = None
        self._probes: deque[Probe] = deque()
        self._pending: dict[int, Probe] = {}
        self._seq = int.from_bytes(os.urandom(2), "big")
        self._literal = _ipv4_literal(host)
        self._last_resolve: float | None = None
        self._resolve_task: asyncio.Task[None] | None = None
        # Loop time of the first lost probe in the current failure streak.
        self._failing_since: float | None = None
        self._last_reopen = 0.0
        self.socket_reopens = 0
        self._send_error_logged = False
        self._resolve_error_logged = False

    # -- lifecycle ---------------------------------------------------------

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        """Open the socket. Raises IcmpSocketError if not permitted."""
        self._loop = loop
        self._sock = self._new_socket()
        self.socket_mode = self._sock.mode
        if self._literal is not None:
            self.address = self._literal

    async def run(self) -> None:
        """Probe until cancelled or closed.

        Nothing here may end the loop except close() or cancellation: a dead
        loop would silently freeze every sensor until the entry is reloaded.
        """
        assert self._loop is not None, "call open() first"
        loop = self._loop
        if self._literal is None:
            await self._resolve()  # so the first probes don't count as lost
        next_at = loop.time()
        while self._sock is not None:  # close() ends the loop
            try:
                self._maybe_resolve()
                self._maybe_reopen()
                self._send_one()
            except Exception:  # keep probing whatever happens
                _LOGGER.exception("Unexpected error while probing %s", self.host)
            next_at += self.interval
            delay = next_at - loop.time()
            if delay < 0:  # event loop stalled; don't burst to catch up
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    def close(self) -> None:
        """Cancel pending timers and close the socket."""
        if self._resolve_task is not None:
            self._resolve_task.cancel()
            self._resolve_task = None
        for probe in self._pending.values():
            if probe.timer:
                probe.timer.cancel()
        self._pending.clear()
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- statistics --------------------------------------------------------

    def window_stats(self) -> WindowStats:
        """Statistics over completed probes sent within the window."""
        assert self._loop is not None
        cutoff = self._loop.time() - self.window
        while self._probes and self._probes[0].done and self._probes[0].sent_at < cutoff:
            self._probes.popleft()
        return compute([p.rtt for p in self._probes if p.done and p.sent_at >= cutoff])

    # -- internals ---------------------------------------------------------

    def _maybe_resolve(self) -> None:
        """Re-resolve in the background; probes keep using the last address."""
        loop = self._loop
        assert loop is not None
        if self._literal is not None or self._resolve_task is not None:
            return
        failing = self.address is None or self._failing_since is not None
        interval = FAILING_RESOLVE_INTERVAL if failing else RESOLVE_INTERVAL
        if self._last_resolve is not None and loop.time() - self._last_resolve < interval:
            return
        self._resolve_task = loop.create_task(self._resolve())

    async def _resolve(self) -> None:
        loop = self._loop
        assert loop is not None
        self._last_resolve = loop.time()
        try:
            async with asyncio.timeout(RESOLVE_TIMEOUT):
                infos = await loop.getaddrinfo(
                    self.host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
            new = infos[0][4][0]
        except (OSError, TimeoutError, IndexError) as err:
            log = _LOGGER.debug if self._resolve_error_logged else _LOGGER.warning
            log("Cannot resolve %s: %s", self.host, str(err) or "timed out")
            self._resolve_error_logged = True
            return
        finally:
            self._resolve_task = None
        self._resolve_error_logged = False
        if new != self.address:
            _LOGGER.info("%s resolved to %s (was %s)", self.host, new, self.address)
        self.address = new

    def _maybe_reopen(self) -> None:
        """Recreate the socket while nothing gets through.

        The kernel normally recovers on its own after a network change, but a
        fresh socket is cheap and guarantees we never keep using a broken one.
        """
        loop = self._loop
        assert loop is not None
        if self._failing_since is None or self.address is None:
            return
        now = loop.time()
        if now - max(self._failing_since, self._last_reopen) < REOPEN_AFTER:
            return
        self._last_reopen = now
        try:
            new = self._new_socket()
        except (IcmpSocketError, OSError) as err:
            _LOGGER.debug("Cannot reopen ICMP socket for %s: %s", self.host, err)
            return
        _LOGGER.debug("No replies from %s, reopened ICMP socket", self.host)
        old, self._sock = self._sock, new
        self.socket_reopens += 1
        if old is not None:
            old.close()  # its pending probes simply time out as lost

    def _new_socket(self) -> IcmpSocket:
        assert self._loop is not None
        sock = (self._socket_factory or IcmpSocket.open)()
        try:
            sock.set_ttl(self.ttl)
            sock.start(self._loop, self._on_reply)
        except BaseException:
            sock.close()
            raise
        return sock

    def _send_one(self) -> None:
        loop = self._loop
        assert loop is not None
        if self._sock is None:  # closed while the task was still scheduled
            return
        self._seq = (self._seq + 1) & 0xFFFF
        probe = Probe(seq=self._seq, sent_at=loop.time())
        self._probes.append(probe)
        self.sent += 1
        if self.address is None:
            self._complete(probe, None)
            return
        try:
            probe.sent_ns = self._sock.send(self.address, probe.seq, self.payload)
        except OSError as err:
            log = _LOGGER.debug if self._send_error_logged else _LOGGER.warning
            log("Sending ICMP echo to %s failed: %s", self.address, err)
            self._send_error_logged = True
            self._complete(probe, None)
            return
        self._send_error_logged = False
        stale = self._pending.pop(probe.seq, None)
        if stale is not None:  # only possible with absurd timeout/interval ratios
            self._expire(stale)
        self._pending[probe.seq] = probe
        probe.timer = loop.call_later(self.timeout, self._expire, probe)

    def _on_reply(self, seq: int, recv_ns: int) -> None:
        probe = self._pending.pop(seq, None)
        if probe is None:
            return  # late, duplicate or foreign reply
        if probe.timer:
            probe.timer.cancel()
        rtt = max(0.0, (recv_ns - probe.sent_ns) / 1_000_000)
        self._complete(probe, rtt)

    def _expire(self, probe: Probe) -> None:
        if self._pending.get(probe.seq) is probe:
            del self._pending[probe.seq]
        if not probe.done:
            self._complete(probe, None)

    def _complete(self, probe: Probe, rtt: float | None) -> None:
        probe.rtt = rtt
        probe.done = True
        probe.timer = None
        if rtt is None:
            self.lost += 1
            if self._failing_since is None:
                self._failing_since = probe.sent_at
        else:
            self.received += 1
            self.last_rtt = rtt
            self._failing_since = None
        if self._on_result is not None:
            self._on_result(probe)


def _ipv4_literal(host: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return None
