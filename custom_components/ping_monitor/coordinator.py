"""Coordinator: runs the prober and publishes window statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DOWN_AFTER,
    CONF_INTERVAL,
    CONF_PAYLOAD_SIZE,
    CONF_PUBLISH_INTERVAL,
    CONF_TIMEOUT,
    CONF_TTL,
    CONF_UP_AFTER,
    CONF_WINDOW,
    DEFAULTS,
    DOMAIN,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
)
from .prober import ConnectivityTracker, Probe, Prober
from .stats import WindowStats

_LOGGER = logging.getLogger(__name__)

type PingMonitorConfigEntry = ConfigEntry[PingMonitorCoordinator]


@dataclass(frozen=True, slots=True)
class PingMonitorData:
    """Snapshot published to entities."""

    stats: WindowStats
    connected: bool | None
    last_rtt: float | None
    sent: int
    received: int
    lost: int
    outages: int
    last_outage_duration: float | None
    outage_started: datetime | None
    address: str | None


def option(entry: ConfigEntry, key: str):
    """Read an option with its default."""
    return entry.options.get(key, DEFAULTS[key])


class PingMonitorCoordinator(DataUpdateCoordinator[PingMonitorData]):
    """Owns one target's prober and turns its samples into entity data."""

    config_entry: PingMonitorConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PingMonitorConfigEntry,
    ) -> None:
        """Initialize from the config entry."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=option(entry, CONF_PUBLISH_INTERVAL)),
            always_update=False,
        )
        self.host: str = entry.data[CONF_HOST]
        self.window: int = option(entry, CONF_WINDOW)
        self.prober = Prober(
            self.host,
            interval=option(entry, CONF_INTERVAL),
            timeout=option(entry, CONF_TIMEOUT),
            window=self.window,
            payload_size=option(entry, CONF_PAYLOAD_SIZE),
            ttl=option(entry, CONF_TTL),
            on_result=self._on_result,
        )
        self.tracker = ConnectivityTracker(
            option(entry, CONF_DOWN_AFTER), option(entry, CONF_UP_AFTER)
        )
        self.outages = 0
        self.last_outage_duration: float | None = None

    def start(self) -> None:
        """Open the socket and start probing in the background."""
        self.prober.open(self.hass.loop)
        self.config_entry.async_create_background_task(
            self.hass, self.prober.run(), f"{DOMAIN} prober {self.host}"
        )

    def stop(self) -> None:
        """Close the socket. The background task is cancelled by HA on unload."""
        self.prober.close()

    async def _async_update_data(self) -> PingMonitorData:
        return self._snapshot()

    def _snapshot(self) -> PingMonitorData:
        p = self.prober
        return PingMonitorData(
            stats=p.window_stats(),
            connected=self.tracker.connected,
            last_rtt=p.last_rtt,
            sent=p.sent,
            received=p.received,
            lost=p.lost,
            outages=self.outages,
            last_outage_duration=self.last_outage_duration,
            outage_started=self._to_wall(self.tracker.outage_started),
            address=p.address,
        )

    def _to_wall(self, loop_time: float | None) -> datetime | None:
        if loop_time is None:
            return None
        return dt_util.utcnow() - timedelta(seconds=self.hass.loop.time() - loop_time)

    @callback
    def _on_result(self, probe: Probe) -> None:
        transition = self.tracker.update(probe.rtt is not None, probe.sent_at)
        if transition is None:
            return
        event_data = {
            "entry_id": self.config_entry.entry_id,
            "name": self.config_entry.title,
            "host": self.host,
            "address": self.prober.address,
        }
        if not transition.connected:
            self.outages += 1
            event_data["started"] = self._to_wall(transition.outage_started).isoformat()
            self.hass.bus.async_fire(EVENT_OUTAGE_STARTED, event_data)
        elif transition.outage_started is not None:
            duration = transition.at - transition.outage_started
            self.last_outage_duration = round(duration, 1)
            event_data |= {
                "started": self._to_wall(transition.outage_started).isoformat(),
                "ended": self._to_wall(transition.at).isoformat(),
                "duration": self.last_outage_duration,
                "lost_packets": transition.lost_packets,
            }
            self.hass.bus.async_fire(EVENT_OUTAGE_ENDED, event_data)
        # Push connectivity changes immediately instead of at the next publish.
        self.async_set_updated_data(self._snapshot())
