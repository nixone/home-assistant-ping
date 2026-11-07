"""Tests for the config and options flows."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ping_monitor.const import DEFAULTS, DOMAIN
from custom_components.ping_monitor.icmp import IcmpSocketError

USER_INPUT = {"host": "192.0.2.1", "name": "Test target", **DEFAULTS}


async def test_create_entry(hass: HomeAssistant, fake_socket) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.ping_monitor.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "payload_size": 1400.0, "window": 60.0}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test target"
    assert result["data"] == {"host": "192.0.2.1"}
    assert result["options"]["payload_size"] == 1400
    assert isinstance(result["options"]["payload_size"], int)
    assert result["options"]["window"] == 60


async def test_title_defaults_to_host(hass: HomeAssistant, fake_socket) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch("custom_components.ping_monitor.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "name": ""}
        )
    assert result["title"] == "192.0.2.1"


async def test_invalid_window(hass: HomeAssistant, fake_socket) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "window": 10, "timeout": 10}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"window": "window_shorter_than_timeout"}


async def test_socket_not_permitted(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ping_monitor.icmp.IcmpSocket.open",
        side_effect=IcmpSocketError("nope"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"host": "socket_not_permitted"}


async def test_cannot_resolve(hass: HomeAssistant, fake_socket) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch.object(hass.loop, "getaddrinfo", side_effect=OSError("nxdomain")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "host": "nope.example"}
        )
    assert result["errors"] == {"host": "cannot_resolve"}


async def test_options_flow(hass: HomeAssistant, fake_socket) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, title="t", data={"host": "192.0.2.1"}, options=dict(DEFAULTS)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULTS, "window": 900, "payload_size": 8000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["window"] == 900
    assert entry.runtime_data.window == 900  # reloaded with the new options
    assert entry.runtime_data.prober.payload == bytes(i & 0xFF for i in range(8000))
    assert await hass.config_entries.async_unload(entry.entry_id)
