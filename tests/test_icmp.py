"""Tests for the ICMP packet helpers and a real loopback ping."""

import asyncio
import struct

import pytest

from custom_components.ping_monitor.icmp import (
    IcmpSocket,
    IcmpSocketError,
    build_echo_request,
    checksum,
    parse_echo_reply,
)


def test_checksum_of_packet_is_zero():
    packet = build_echo_request(0x1234, 7, b"abc")  # odd payload length
    assert checksum(packet) == 0


def test_parse_reply_without_ip_header():
    reply = struct.pack("!BBHHH", 0, 0, 0, 0x1234, 7) + b"payload"
    assert parse_echo_reply(reply, has_ip_header=False) == (0x1234, 7)


def test_parse_reply_with_ip_header():
    ip_header = bytes([0x45]) + bytes(19)
    reply = ip_header + struct.pack("!BBHHH", 0, 0, 0, 1, 2)
    assert parse_echo_reply(reply, has_ip_header=True) == (1, 2)


def test_parse_ignores_non_replies():
    request = build_echo_request(1, 2, b"")
    assert parse_echo_reply(request, has_ip_header=False) is None
    unreachable = struct.pack("!BBHHH", 3, 1, 0, 0, 0)
    assert parse_echo_reply(unreachable, has_ip_header=False) is None
    assert parse_echo_reply(b"\x00", has_ip_header=False) is None


async def test_loopback_ping(socket_enabled):
    """Real ICMP to 127.0.0.1; skipped where ICMP sockets are not permitted."""
    try:
        sock = IcmpSocket.open()
    except IcmpSocketError as err:
        pytest.skip(f"no ICMP socket: {err}")
    loop = asyncio.get_running_loop()
    got: asyncio.Future = loop.create_future()
    sent = {}
    sock.start(loop, lambda seq, ns: got.done() or got.set_result((seq, ns)))
    try:
        sent[42] = sock.send("127.0.0.1", 42, bytes(1400))
        seq, recv_ns = await asyncio.wait_for(got, 2)
    finally:
        sock.close()
    assert seq == 42
    assert 0 <= (recv_ns - sent[42]) / 1e6 < 100
