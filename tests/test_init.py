"""End-to-end tests: entry setup, entities, connectivity and outage events."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ping_monitor.const import (
    DEFAULTS,
    DOMAIN,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
)
from custom_components.ping_monitor.icmp import IcmpSocketError

FAST = {**DEFAULTS, "interval": 0.2, "timeout": 0.1, "window": 60, "down_after": 3, "up_after": 2}


def _entry(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Router", data={"host": "192.0.2.1"}, options={**FAST, **options}
    )
    entry.add_to_hass(hass)
    return entry


async def _run_for(hass: HomeAssistant, seconds: float) -> None:
    """Let the prober run in real time (it uses loop.call_later/asyncio.sleep)."""
    import asyncio

    await asyncio.sleep(seconds)
    await hass.async_block_till_done()


async def test_setup_creates_entities_and_updates(hass: HomeAssistant, fake_socket) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    enabled = {e.translation_key for e in entities if not e.disabled}
    assert {"packet_loss", "rtt_avg", "rtt_p95", "jitter", "connectivity"} <= enabled
    assert "rtt_p99" not in enabled

    await _run_for(hass, 1.0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.router_packet_loss").state == "0.0"
    rtt = hass.states.get("sensor.router_rtt_average")
    assert float(rtt.state) >= 0
    assert rtt.attributes["unit_of_measurement"] == "ms"
    assert rtt.attributes["window"] == 60
    assert hass.states.get("binary_sensor.router_connectivity").state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert fake_socket.closed


async def test_outage_events(hass: HomeAssistant, fake_socket) -> None:
    started = async_capture_events(hass, EVENT_OUTAGE_STARTED)
    ended = async_capture_events(hass, EVENT_OUTAGE_ENDED)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _run_for(hass, 0.7)
    assert hass.states.get("binary_sensor.router_connectivity").state == "on"

    fake_socket.responder = lambda seq: None  # target goes dark
    await _run_for(hass, 1.2)
    assert len(started) == 1
    assert started[0].data["host"] == "192.0.2.1"
    # Connectivity is pushed immediately, without waiting for the publish interval.
    assert hass.states.get("binary_sensor.router_connectivity").state == "off"

    fake_socket.responder = lambda seq: 0.001
    await _run_for(hass, 0.8)
    assert len(ended) == 1
    assert ended[0].data["duration"] > 0.5
    assert ended[0].data["lost_packets"] >= 3
    assert hass.states.get("binary_sensor.router_connectivity").state == "on"
    assert hass.states.get("sensor.router_outages").state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_socket_error_fails_setup(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with patch(
        "custom_components.ping_monitor.icmp.IcmpSocket.open",
        side_effect=IcmpSocketError("Operation not permitted"),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
