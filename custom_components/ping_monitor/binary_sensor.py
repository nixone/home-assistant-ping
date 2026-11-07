"""Connectivity binary sensor for Ping Monitor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PingMonitorConfigEntry
from .entity import PingMonitorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PingMonitorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connectivity sensor."""
    async_add_entities([PingMonitorConnectivity(entry.runtime_data, "connectivity")])


class PingMonitorConnectivity(PingMonitorEntity, BinarySensorEntity):
    """On while the target answers, with hysteresis."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        """Return connectivity; None (unknown) until enough probes completed."""
        return self.coordinator.data.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the current outage started."""
        started = self.coordinator.data.outage_started
        return {"outage_started": started.isoformat() if started else None}
