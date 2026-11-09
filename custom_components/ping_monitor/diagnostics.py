"""Diagnostics for Ping Monitor."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import PingMonitorConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PingMonitorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    prober = coordinator.prober
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "socket_mode": prober.socket_mode,
        "socket_reopens": prober.socket_reopens,
        "address": prober.address,
        "counters": {"sent": prober.sent, "received": prober.received, "lost": prober.lost},
        "window_stats": asdict(prober.window_stats()),
        "connected": coordinator.tracker.connected,
        "outages": coordinator.outages,
    }
