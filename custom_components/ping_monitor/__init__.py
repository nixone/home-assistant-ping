"""Ping Monitor: continuous ICMP packet loss and latency statistics."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .coordinator import PingMonitorConfigEntry, PingMonitorCoordinator
from .icmp import IcmpSocketError

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PingMonitorConfigEntry) -> bool:
    """Set up one monitored target."""
    coordinator = PingMonitorCoordinator(hass, entry)
    try:
        coordinator.start()
    except IcmpSocketError as err:
        raise ConfigEntryError(
            f"Cannot open an ICMP socket ({err}). Allow unprivileged ping via "
            "sysctl net.ipv4.ping_group_range or run Home Assistant with CAP_NET_RAW"
        ) from err
    entry.async_on_unload(coordinator.stop)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PingMonitorConfigEntry) -> bool:
    """Unload a target."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: PingMonitorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
