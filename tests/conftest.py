"""Fixtures."""

from unittest.mock import patch

import pytest

from .fakes import FakeIcmpSocket


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components/ in every test."""
    return


@pytest.fixture
def fake_socket():
    """Patch the ICMP socket with an in-memory fake that answers every probe."""
    sock = FakeIcmpSocket()
    with patch("custom_components.ping_monitor.icmp.IcmpSocket.open", return_value=sock):
        yield sock
