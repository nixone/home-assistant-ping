"""Test doubles."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time


class FakeIcmpSocket:
    """In-memory ICMP socket. `responder(seq)` returns a reply delay in s or None (drop)."""

    mode = "fake"
    privileged = False

    def __init__(self, responder: Callable[[int], float | None] = lambda seq: 0.001) -> None:
        self.responder = responder
        self.sent: list[tuple[str, int, int]] = []
        self.ttl: int | None = None
        self.closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_reply = None

    def set_ttl(self, ttl: int) -> None:
        self.ttl = ttl

    def start(self, loop, on_reply) -> None:
        self._loop = loop
        self._on_reply = on_reply

    def send(self, address: str, seq: int, payload: bytes) -> int:
        self.sent.append((address, seq, len(payload)))
        delay = self.responder(seq)
        if delay is not None:
            self._loop.call_later(delay, lambda: self._on_reply(seq, time.time_ns()))
        return time.time_ns()

    def close(self) -> None:
        self.closed = True
