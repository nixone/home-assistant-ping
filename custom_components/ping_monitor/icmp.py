"""Minimal non-blocking ICMPv4 echo socket for asyncio. No Home Assistant imports.

Why not icmplib: its async receive waits for one specific request at a time, so
overlapping probes (timeout > interval) on one socket would steal each other's
replies. Here a single reader callback dispatches every reply by sequence number.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import os
import socket
import struct
import sys
import time

_LOGGER = logging.getLogger(__name__)

ICMP_ECHO_REPLY = 0
ICMP_ECHO_REQUEST = 8
MAX_PAYLOAD_SIZE = 65507  # 65535 - 20 (IPv4 header) - 8 (ICMP header)
_RECV_BUFSIZE = 65535

# Kernel receive timestamps make RTTs immune to event-loop latency in HA.
# Python does not export these constants; values are from <asm-generic/socket.h>.
_SO_TIMESTAMPNS = 35 if sys.platform.startswith("linux") else None
_SCM_TIMESTAMPNS = _SO_TIMESTAMPNS

ReplyCallback = Callable[[int, int], None]
"""Called with (sequence, receive_time_ns) for each matching echo reply."""


class IcmpSocketError(Exception):
    """Raised when no ICMP socket can be opened."""


def checksum(data: bytes) -> int:
    """RFC 1071 Internet checksum."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def build_echo_request(identifier: int, sequence: int, payload: bytes) -> bytes:
    """Build an ICMP echo request packet."""
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, identifier, sequence)
    csum = checksum(header + payload)
    return struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, csum, identifier, sequence) + payload


def parse_echo_reply(packet: bytes, has_ip_header: bool) -> tuple[int, int] | None:
    """Return (identifier, sequence) for an echo reply, None for anything else."""
    if has_ip_header:
        if not packet:
            return None
        packet = packet[(packet[0] & 0x0F) * 4 :]
    if len(packet) < 8:
        return None
    icmp_type, code, _csum, identifier, sequence = struct.unpack("!BBHHH", packet[:8])
    if icmp_type != ICMP_ECHO_REPLY or code != 0:
        return None
    return identifier, sequence


def _kernel_timestamp_ns(ancdata: list[tuple[int, int, bytes]]) -> int | None:
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == _SCM_TIMESTAMPNS:
            if len(data) >= 16:
                sec, nsec = struct.unpack("@qq", data[:16])
            elif len(data) >= 8:
                sec, nsec = struct.unpack("@ii", data[:8])
            else:
                return None
            return sec * 1_000_000_000 + nsec
    return None


class IcmpSocket:
    """One ICMPv4 socket, unprivileged (SOCK_DGRAM) when possible, raw otherwise."""

    def __init__(self, sock: socket.socket, privileged: bool) -> None:
        self._sock = sock
        self.privileged = privileged
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_reply: ReplyCallback | None = None
        if privileged:
            self.identifier = int.from_bytes(os.urandom(2), "big")
        else:
            # The kernel rewrites the ICMP id to the socket's local "port" and
            # only delivers replies carrying that id to this socket.
            sock.bind(("0.0.0.0", 0))
            self.identifier = sock.getsockname()[1]
        self.kernel_timestamps = False
        if _SO_TIMESTAMPNS is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, _SO_TIMESTAMPNS, 1)
                self.kernel_timestamps = True
            except OSError:
                pass

    @classmethod
    def open(cls) -> IcmpSocket:
        """Open an unprivileged ping socket, falling back to a raw socket."""
        errors = []
        for sock_type, privileged in ((socket.SOCK_DGRAM, False), (socket.SOCK_RAW, True)):
            try:
                sock = socket.socket(socket.AF_INET, sock_type, socket.IPPROTO_ICMP)
            except OSError as err:
                errors.append(f"{'raw' if privileged else 'unprivileged'}: {err}")
                continue
            sock.setblocking(False)
            return cls(sock, privileged)
        raise IcmpSocketError("; ".join(errors))

    @property
    def mode(self) -> str:
        """Human-readable socket mode."""
        return "raw (privileged)" if self.privileged else "datagram (unprivileged)"

    def set_ttl(self, ttl: int) -> None:
        """Set the IP time-to-live for outgoing packets."""
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)

    def start(self, loop: asyncio.AbstractEventLoop, on_reply: ReplyCallback) -> None:
        """Start dispatching replies to `on_reply` from the event loop."""
        self._loop = loop
        self._on_reply = on_reply
        loop.add_reader(self._sock.fileno(), self._on_readable)

    def send(self, address: str, sequence: int, payload: bytes) -> int:
        """Send an echo request. Return the send time in ns (CLOCK_REALTIME).

        Raises OSError on failure (e.g. network unreachable).
        """
        packet = build_echo_request(self.identifier, sequence, payload)
        sent_ns = time.time_ns()
        self._sock.sendto(packet, (address, 0))
        return sent_ns

    def _on_readable(self) -> None:
        while True:
            try:
                packet, ancdata, _flags, _addr = self._sock.recvmsg(
                    _RECV_BUFSIZE, socket.CMSG_SPACE(16)
                )
            except (BlockingIOError, InterruptedError):
                return
            except OSError as err:
                # e.g. ICMP errors queued on the socket; nothing to dispatch.
                _LOGGER.debug("ICMP receive error: %s", err)
                return
            recv_ns = _kernel_timestamp_ns(ancdata) if self.kernel_timestamps else None
            if recv_ns is None:
                recv_ns = time.time_ns()
            parsed = parse_echo_reply(packet, has_ip_header=self.privileged)
            if parsed is None:
                continue
            identifier, sequence = parsed
            if self.privileged and identifier != self.identifier:
                continue  # raw sockets see every ICMP reply on the host
            if self._on_reply is not None:
                self._on_reply(sequence, recv_ns)

    def close(self) -> None:
        """Stop reading and close the socket."""
        if self._loop is not None:
            self._loop.remove_reader(self._sock.fileno())
            self._loop = None
        self._sock.close()
