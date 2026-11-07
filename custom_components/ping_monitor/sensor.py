"""Sensors for Ping Monitor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PingMonitorConfigEntry, PingMonitorData
from .entity import PingMonitorEntity


@dataclass(frozen=True, kw_only=True)
class PingSensorDescription(SensorEntityDescription):
    """Sensor description with a value getter."""

    value_fn: Callable[[PingMonitorData], Any]
    window_stat: bool = False  # computed over the rolling window


def _rtt(key: str, fn: Callable[[PingMonitorData], Any], enabled: bool = True):
    return PingSensorDescription(
        key=key,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=enabled,
        window_stat=True,
        value_fn=fn,
    )


def _counter(key: str, fn: Callable[[PingMonitorData], int], enabled: bool = False):
    return PingSensorDescription(
        key=key,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=enabled,
        value_fn=fn,
    )


SENSORS: tuple[PingSensorDescription, ...] = (
    PingSensorDescription(
        key="packet_loss",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        window_stat=True,
        value_fn=lambda d: d.stats.loss_pct,
    ),
    _rtt("rtt_avg", lambda d: d.stats.rtt_avg),
    _rtt("rtt_median", lambda d: d.stats.rtt_median),
    _rtt("rtt_min", lambda d: d.stats.rtt_min),
    _rtt("rtt_max", lambda d: d.stats.rtt_max),
    _rtt("rtt_p95", lambda d: d.stats.rtt_p95),
    _rtt("rtt_p99", lambda d: d.stats.rtt_p99, enabled=False),
    _rtt("rtt_stddev", lambda d: d.stats.rtt_stddev, enabled=False),
    _rtt("jitter", lambda d: d.stats.jitter),
    PingSensorDescription(
        key="longest_loss_burst",
        native_unit_of_measurement="packets",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        window_stat=True,
        value_fn=lambda d: d.stats.longest_loss_burst,
    ),
    PingSensorDescription(
        key="last_rtt",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.last_rtt,
    ),
    _counter("packets_sent", lambda d: d.sent),
    _counter("packets_received", lambda d: d.received),
    _counter("packets_lost", lambda d: d.lost, enabled=True),
    _counter("outages", lambda d: d.outages, enabled=True),
    PingSensorDescription(
        key="last_outage_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        suggested_display_precision=1,
        value_fn=lambda d: d.last_outage_duration,
    ),
    PingSensorDescription(
        key="address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.address,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PingMonitorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for one target."""
    coordinator = entry.runtime_data
    async_add_entities(PingMonitorSensor(coordinator, d) for d in SENSORS)


class PingMonitorSensor(PingMonitorEntity, SensorEntity):
    """A statistic of one monitored target."""

    entity_description: PingSensorDescription

    def __init__(self, coordinator, description: PingSensorDescription) -> None:
        """Initialize."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.window_stat:
            # Static, so the recorder stores it once rather than on every update.
            self._attr_extra_state_attributes = {"window": coordinator.window}

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data)
